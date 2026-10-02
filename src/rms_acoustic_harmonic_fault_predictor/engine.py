"""Score numeric acoustic windows for harmonic imbalance.

RMS comes from the mean square. Crest factor is peak over RMS. Excess
kurtosis is the standardized fourth moment minus three. A Goertzel
recurrence, one coefficient ``2 cos(omega)`` per bin, measures the
configured fundamental and harmonics 2 and 3. The fault bit rises when
``(mag2 + mag3) / mag1`` exceeds the ratio limit.

ISO 10816 is the commentary reference for broadband RMS severity. This
module does not assign a machine zone and does not certify a balance grade.
The waveform is a sequence of floats the caller already sampled.
"""

from __future__ import annotations

import asyncio
import logging
import math
import statistics
import struct
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from . import wire
from .exceptions import EngineKernelException

MIN_WINDOW = 16
MAX_WINDOW = 8192
_LOGGER = logging.getLogger("rms_acoustic_harmonic_fault_predictor")
_LOGGER.addHandler(logging.NullHandler())


@dataclass(frozen=True, slots=True)
class _Window:
    sequence_id: int
    samples: tuple[float, ...]


def goertzel_magnitude(
    samples: Sequence[float], sample_rate: float, freq_hz: float
) -> float:
    """Single-bin magnitude via the Goertzel recurrence.

    The coefficient is ``2 cos(omega)`` with ``omega = 2 pi f / fs``.
    Sine and cosine are used once, to turn the two state variables into a
    cartesian magnitude. The result is scaled by ``2 / N`` so a pure sine
    of amplitude ``A`` reads back near ``A``.
    """
    count = len(samples)
    if count == 0 or freq_hz <= 0.0 or sample_rate <= 0.0:
        return 0.0
    omega = 2.0 * math.pi * freq_hz / sample_rate
    cosine = math.cos(omega)
    sine = math.sin(omega)
    coefficient = 2.0 * cosine
    first = 0.0
    second = 0.0
    for sample in samples:
        current = sample + coefficient * first - second
        second = first
        first = current
    real = first - second * cosine
    imag = second * sine
    return math.hypot(real, imag) * 2.0 / count


def _require_positive(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EngineKernelException(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise EngineKernelException(f"{name} must be a positive finite value")
    return number


class RmsAcousticHarmonicFaultPredictor:
    """In-process scorer for finite vibration windows.

    ``run`` takes the windows that actually arrived. A hole in
    ``sequence_id`` increments ``dropouts`` and does not synthesize
    samples. The scalar features describe the highest sequence id in the
    batch. ``clips`` and ``dropouts`` accumulate across that batch.
    """

    def __init__(
        self,
        sample_rate: float = 1600.0,
        fundamental_hz: float = 50.0,
        full_scale: float = 1.0,
        ratio_limit: float = 0.25,
        silence_rms: float = 1.0e-4,
    ) -> None:
        self._sample_rate = _require_positive("sample_rate", sample_rate)
        self._fundamental_hz = _require_positive("fundamental_hz", fundamental_hz)
        self._full_scale = _require_positive("full_scale", full_scale)
        self._ratio_limit = _require_positive("ratio_limit", ratio_limit)
        self._silence_rms = _require_positive("silence_rms", silence_rms)
        nyquist = self._sample_rate / 2.0
        if 3.0 * self._fundamental_hz >= nyquist:
            raise EngineKernelException("third harmonic is not strictly below Nyquist")
        self._lock = asyncio.Lock()
        self._spool: deque[bytes] = deque(maxlen=64)
        self._logger = _LOGGER

    async def run(self, records: Sequence[object]) -> dict[str, object]:
        """Score every present window and return the batch report."""
        async with self._lock:
            windows = await self._ingest(records)
            dropouts = self._count_dropouts(windows)
            scored = [self._score_window(window) for window in windows]
            report = self._assemble(scored, dropouts)
            await self._spool_header(report["header"])
            return report

    async def _ingest(self, records: Sequence[object]) -> tuple[_Window, ...]:
        await asyncio.sleep(0)
        if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
            raise EngineKernelException("records must be a sequence of windows")
        if not records:
            raise EngineKernelException("acoustic batch is empty")
        windows = [self._coerce(record) for record in records]
        windows.sort(key=lambda item: item.sequence_id)
        return tuple(windows)

    async def _spool_header(self, header: bytes) -> None:
        await asyncio.sleep(0)
        self._spool.append(header)

    def _coerce(self, record: object) -> _Window:
        if not isinstance(record, Mapping):
            raise EngineKernelException("acoustic record must be a mapping")
        sequence_id = record.get("sequence_id")
        if isinstance(sequence_id, bool) or not isinstance(sequence_id, int):
            raise EngineKernelException("sequence id must be an integer")
        if sequence_id < 0:
            raise EngineKernelException("sequence id must be non-negative")
        samples = self._samples(record.get("samples"))
        return _Window(sequence_id, samples)

    def _samples(self, samples: object) -> tuple[float, ...]:
        if isinstance(samples, (str, bytes)) or not isinstance(samples, Sequence):
            raise EngineKernelException("waveform must be a sequence of samples")
        cleaned: list[float] = []
        for sample in samples:
            if isinstance(sample, bool) or not isinstance(sample, (int, float)):
                raise EngineKernelException("waveform sample is not numeric")
            value = float(sample)
            if not math.isfinite(value):
                self._logger.warning("non-finite waveform sample rejected")
                raise EngineKernelException("non-finite waveform sample")
            cleaned.append(value)
        if len(cleaned) < MIN_WINDOW or len(cleaned) > MAX_WINDOW:
            raise EngineKernelException("window length outside the supported span")
        return tuple(cleaned)

    def _count_dropouts(self, windows: Sequence[_Window]) -> int:
        missing = 0
        previous = windows[0].sequence_id
        for window in windows[1:]:
            if window.sequence_id == previous:
                raise EngineKernelException("duplicate sequence id")
            gap = window.sequence_id - previous - 1
            if gap > 0:
                missing += gap
            previous = window.sequence_id
        if missing:
            self._logger.warning("sequence dropout missing=%s", missing)
        return missing

    def _score_window(self, window: _Window) -> dict[str, float | int | bool]:
        clamped, clips = self._clamp(window.samples)
        squares = [sample * sample for sample in clamped]
        rms = math.sqrt(statistics.fmean(squares))
        if rms < self._silence_rms:
            self._logger.warning("silence window rejected rms=%.8f", rms)
            raise EngineKernelException("near-zero RMS silence window rejected")
        peak = max(abs(sample) for sample in clamped)
        crest = peak / rms
        kurtosis = self._excess_kurtosis(clamped)
        centered = self._center(clamped)
        ratio, fault = self._harmonic_ratio(centered)
        if clips:
            self._logger.warning("full-scale clamp clips=%s", clips)
        return {
            "n": len(clamped),
            "rms": rms,
            "crest": crest,
            "kurtosis": kurtosis,
            "harmonic_ratio": ratio,
            "fault": fault,
            "clips": clips,
        }

    def _clamp(self, samples: Sequence[float]) -> tuple[tuple[float, ...], int]:
        limit = self._full_scale
        clips = 0
        clamped: list[float] = []
        for sample in samples:
            value = sample
            if abs(value) >= limit:
                clips += 1
                if value > limit:
                    value = limit
                elif value < -limit:
                    value = -limit
            clamped.append(value)
        return tuple(clamped), clips

    def _excess_kurtosis(self, samples: Sequence[float]) -> float:
        center = statistics.fmean(samples)
        spread = statistics.pstdev(samples)
        if spread <= 1.0e-12:
            return 0.0
        fourth = statistics.fmean((sample - center) ** 4 for sample in samples)
        return fourth / (spread**4) - 3.0

    def _center(self, samples: Sequence[float]) -> tuple[float, ...]:
        center = statistics.fmean(samples)
        return tuple(sample - center for sample in samples)

    def _harmonic_ratio(self, samples: Sequence[float]) -> tuple[float, bool]:
        fundamental = self._fundamental_hz
        mag1 = goertzel_magnitude(samples, self._sample_rate, fundamental)
        mag2 = goertzel_magnitude(samples, self._sample_rate, 2.0 * fundamental)
        mag3 = goertzel_magnitude(samples, self._sample_rate, 3.0 * fundamental)
        if mag1 <= 1.0e-12:
            ratio = 0.0 if (mag2 + mag3) <= 1.0e-12 else math.inf
        else:
            ratio = (mag2 + mag3) / mag1
        fault = ratio > self._ratio_limit
        if fault:
            self._logger.info(
                "fault window ratio=%.4f rms_bins=%.6f %.6f %.6f",
                ratio,
                mag1,
                mag2,
                mag3,
            )
        return ratio, fault

    def _assemble(
        self,
        scored: Sequence[Mapping[str, float | int | bool]],
        dropouts: int,
    ) -> dict[str, object]:
        last = scored[-1]
        clips = 0
        for item in scored:
            clips += int(item["clips"])
        flags = 0
        if bool(last["fault"]):
            flags |= wire.FLAG_FAULT
        if clips:
            flags |= wire.FLAG_CLIP
        if dropouts:
            flags |= wire.FLAG_DROPOUT
        count = int(last["n"])
        header = struct.pack(wire.HEADER_FORMAT, self._sample_rate, count, flags)
        sample_rate, unpacked_count, unpacked_flags = struct.unpack(
            wire.HEADER_FORMAT, header
        )
        if (
            unpacked_count != count
            or unpacked_flags != flags
            or sample_rate != self._sample_rate
        ):
            raise EngineKernelException("acoustic header failed struct roundtrip")
        confirmed = wire.unpack_header(header)
        if confirmed != (float(sample_rate), count, flags):
            raise EngineKernelException("acoustic header failed wire roundtrip")
        return {
            "rms": float(last["rms"]),
            "crest": float(last["crest"]),
            "kurtosis": float(last["kurtosis"]),
            "harmonic_ratio": float(last["harmonic_ratio"]),
            "fault": bool(last["fault"]),
            "clips": clips,
            "dropouts": dropouts,
            "header": header,
        }
