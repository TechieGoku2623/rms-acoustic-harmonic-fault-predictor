"""Roundtrip, happy path, silence, and clip/dropout coverage."""

from __future__ import annotations

import asyncio
import math
import unittest

from rms_acoustic_harmonic_fault_predictor import (
    EngineKernelException,
    RmsAcousticHarmonicFaultPredictor,
)
from rms_acoustic_harmonic_fault_predictor.wire import (
    FLAG_CLIP,
    FLAG_DROPOUT,
    FLAG_FAULT,
    pack_header,
    unpack_header,
)

_RATE = 1600.0
_FUNDAMENTAL = 50.0
_COUNT = 128


def _tone(amplitude: float, harmonic: int) -> list[float]:
    samples: list[float] = []
    for index in range(_COUNT):
        angle = 2.0 * math.pi * _FUNDAMENTAL * harmonic * index / _RATE
        samples.append(amplitude * math.sin(angle))
    return samples


def _mix(*parts: list[float]) -> list[float]:
    return [sum(part[index] for part in parts) for index in range(_COUNT)]


class AcousticEngineTest(unittest.TestCase):
    def _engine(self) -> RmsAcousticHarmonicFaultPredictor:
        return RmsAcousticHarmonicFaultPredictor(
            sample_rate=_RATE,
            fundamental_hz=_FUNDAMENTAL,
            full_scale=1.0,
            ratio_limit=0.25,
        )

    def test_header_roundtrip(self) -> None:
        flags = FLAG_FAULT | FLAG_CLIP | FLAG_DROPOUT
        payload = pack_header(_RATE, _COUNT, flags)
        self.assertEqual(len(payload), 16)
        self.assertEqual(unpack_header(payload), (_RATE, _COUNT, flags))
        self.assertEqual(unpack_header(pack_header(800.0, 32, 0)), (800.0, 32, 0))

    def test_happy_path_scores_harmonics(self) -> None:
        engine = self._engine()
        pure = _tone(0.5, 1)
        rich = _mix(_tone(0.40, 1), _tone(0.16, 2), _tone(0.10, 3))
        healthy = asyncio.run(engine.run([{"sequence_id": 0, "samples": pure}]))
        faulted = asyncio.run(engine.run([{"sequence_id": 7, "samples": rich}]))
        self.assertAlmostEqual(healthy["rms"], 0.5 / math.sqrt(2.0), places=6)
        self.assertAlmostEqual(healthy["crest"], math.sqrt(2.0), places=5)
        self.assertLess(healthy["kurtosis"], -1.3)
        self.assertGreater(healthy["kurtosis"], -1.7)
        self.assertLess(healthy["harmonic_ratio"], 0.02)
        self.assertFalse(healthy["fault"])
        self.assertEqual(healthy["clips"], 0)
        self.assertEqual(healthy["dropouts"], 0)
        self.assertGreater(faulted["harmonic_ratio"], 0.60)
        self.assertLess(faulted["harmonic_ratio"], 0.70)
        self.assertTrue(faulted["fault"])
        self.assertEqual(faulted["clips"], 0)
        self.assertEqual(faulted["dropouts"], 0)
        sample_rate, count, flags = unpack_header(faulted["header"])
        self.assertEqual(sample_rate, _RATE)
        self.assertEqual(count, _COUNT)
        self.assertEqual(flags, FLAG_FAULT)

    def test_silence_rejects_near_zero_rms(self) -> None:
        engine = self._engine()
        with self.assertRaises(EngineKernelException) as silence:
            asyncio.run(engine.run([{"sequence_id": 0, "samples": [0.0] * _COUNT}]))
        self.assertIn("silence", str(silence.exception).lower())
        with self.assertRaises(EngineKernelException):
            asyncio.run(engine.run([{"sequence_id": 1, "samples": [1.0e-8] * _COUNT}]))
        poisoned = _tone(0.5, 1)
        poisoned[4] = math.nan
        with self.assertRaises(EngineKernelException) as finite:
            asyncio.run(engine.run([{"sequence_id": 2, "samples": poisoned}]))
        self.assertIn("non-finite", str(finite.exception).lower())

    def test_clip_and_dropout_do_not_invent_samples(self) -> None:
        engine = self._engine()
        pure = _tone(0.5, 1)
        clipped = list(pure)
        clipped[0] = 50.0
        clipped[1] = 1.0
        raw_rms = math.sqrt(sum(sample * sample for sample in clipped) / _COUNT)
        clamped = asyncio.run(engine.run([{"sequence_id": 0, "samples": clipped}]))
        self.assertEqual(clamped["clips"], 2)
        self.assertLess(clamped["rms"], 1.0)
        self.assertGreater(raw_rms, float(clamped["rms"]) + 1.0)
        solo = asyncio.run(engine.run([{"sequence_id": 4, "samples": pure}]))
        batch = asyncio.run(
            engine.run(
                [
                    {"sequence_id": 0, "samples": pure},
                    {"sequence_id": 1, "samples": clipped},
                    {"sequence_id": 4, "samples": pure},
                ]
            )
        )
        self.assertEqual(batch["dropouts"], 2)
        self.assertEqual(batch["clips"], 2)
        self.assertFalse(batch["fault"])
        self.assertAlmostEqual(batch["rms"], solo["rms"], places=9)
        self.assertAlmostEqual(
            batch["harmonic_ratio"], solo["harmonic_ratio"], places=9
        )
        self.assertAlmostEqual(batch["crest"], solo["crest"], places=9)
        _rate, _count, flags = unpack_header(batch["header"])
        self.assertEqual(flags & FLAG_CLIP, FLAG_CLIP)
        self.assertEqual(flags & FLAG_DROPOUT, FLAG_DROPOUT)
        self.assertEqual(flags & FLAG_FAULT, 0)


if __name__ == "__main__":
    unittest.main()
