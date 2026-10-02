"""Score acoustic windows for bearing and imbalance harmonics.

A finite vibration window is reduced to an AC RMS value and a small DFT over
candidate shaft bins. The fault score is the larger of the second-harmonic and
third-harmonic amplitudes divided by the fundamental. The design is aligned
with the vibration-monitoring discipline in ISO 10816 and with SOC 2 processing
integrity for maintenance telemetry. Nothing here certifies a machine, and
nothing here describes how to defeat an interlock.
"""

from __future__ import annotations

import asyncio
import logging
import math
import statistics
import struct
import sys
from collections import deque
from collections.abc import Sequence

TELEMETRY_TOPIC = "plant.vibration.window"
FUNDAMENTAL_BIN_FLOOR = 2
FUNDAMENTAL_BIN_CEILING = 8
MIN_WINDOW = 32
MAX_WINDOW = 4096
MEASUREMENT_TAG = 1
DROPOUT_TAG = 2

__all__ = [
    "DROPOUT_TAG",
    "EngineKernelException",
    "HarmonicFaultEngine",
    "MEASUREMENT_TAG",
    "TELEMETRY_TOPIC",
    "configure_logging",
    "dft_magnitude",
    "main",
]


class EngineKernelException(Exception):
    """A window or cursor fault that must not be written as a measurement."""


def configure_logging() -> None:
    """Install a timestamped handler once, if the process has none yet."""
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


def _require_positive(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EngineKernelException(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise EngineKernelException(f"{name} must be a positive finite value")
    return number


def dft_magnitude(samples: Sequence[float], bin_index: int) -> float:
    """Return the bin magnitude of a real DFT, normalized by window length.

    The transform is the textbook sum over cos and sin. NumPy is intentionally
    not used: the candidate set is a handful of shaft harmonics, not a full
    spectrum, and the image has to stay on the standard library.
    """
    count = len(samples)
    if bin_index <= 0 or count == 0:
        return 0.0
    scale = -2.0 * math.pi * bin_index / count
    real = 0.0
    imag = 0.0
    for index, sample in enumerate(samples):
        angle = scale * index
        real += sample * math.cos(angle)
        imag += sample * math.sin(angle)
    return math.hypot(real, imag) / count


def pack_measurement_header(
    sample_rate: float, count: int, rms: float, sequence_id: int
) -> bytes:
    """Pack sample_rate, window length, and the IEEE-754 bits of RMS."""
    rms_bits = struct.unpack(">I", struct.pack(">f", rms))[0]
    return struct.pack(
        ">BfIII", MEASUREMENT_TAG, sample_rate, count, rms_bits, sequence_id
    )


def pack_dropout_header(
    sample_rate: float, sequence_id: int, expected: int, gap: int
) -> bytes:
    """Pack a gap record. The payload is not a fabricated waveform."""
    return struct.pack(">BfIII", DROPOUT_TAG, sample_rate, sequence_id, expected, gap)


def _tone(sample_count: int, bin_index: int, amplitude: float) -> list[float]:
    scale = 2.0 * math.pi * bin_index / sample_count
    return [amplitude * math.sin(scale * index) for index in range(sample_count)]


def _faulted_window(sample_count: int = 64) -> list[float]:
    fundamental = _tone(sample_count, 4, 1.2)
    second = _tone(sample_count, 8, 0.9)
    return [fundamental[index] + second[index] for index in range(sample_count)]


def _saturated_window(full_scale: float, sample_count: int = 64) -> list[float]:
    mixed = _faulted_window(sample_count)
    peak = max(abs(sample) for sample in mixed)
    scaled = [full_scale * sample / peak for sample in mixed]
    peak_index = max(range(sample_count), key=lambda index: abs(scaled[index]))
    scaled[peak_index] = math.copysign(full_scale, scaled[peak_index])
    return scaled


class HarmonicFaultEngine:
    """Ingest vibration windows and keep a bounded in-process ledger.

    ``_inbound`` is the collector queue. ``_ledger`` is the TimescaleDB
    stand-in: a maxlen deque of summary rows, not a socket. ``_outbound``
    holds packed headers that a producer for ``plant.vibration.window`` would
    drain. This process never opens that connection.
    """

    def __init__(
        self,
        sample_rate: float = 4096.0,
        full_scale: float = 10.0,
        ratio_limit: float = 0.25,
        silence_rms: float = 1.0e-4,
        ledger_limit: int = 512,
    ) -> None:
        configure_logging()
        self.sample_rate = _require_positive("sample_rate", sample_rate)
        self.full_scale = _require_positive("full_scale", full_scale)
        self.ratio_limit = _require_positive("ratio_limit", ratio_limit)
        self._silence_rms = _require_positive("silence_rms", silence_rms)
        if isinstance(ledger_limit, bool) or not isinstance(ledger_limit, int):
            raise EngineKernelException("ledger_limit must be an integer")
        if ledger_limit < 1:
            raise EngineKernelException("ledger_limit must be at least 1")
        self._ledger: deque[dict[str, object]] = deque(maxlen=ledger_limit)
        self._spool: deque[bytes] = deque(maxlen=ledger_limit)
        self._next_sequence: int | None = None
        self._lock = asyncio.Lock()
        self._inbound: asyncio.Queue[
            tuple[Sequence[float], int, asyncio.Future[dict[str, object]]]
        ] = asyncio.Queue(maxsize=256)
        self._outbound: asyncio.Queue[bytes] = asyncio.Queue(maxsize=256)
        self._worker: asyncio.Task[None] | None = None
        self._publisher: asyncio.Task[None] | None = None
        self._logger = logging.getLogger("rms.kernel")

    async def _ensure(self) -> None:
        async with self._lock:
            if self._worker is None or self._worker.done():
                self._worker = asyncio.create_task(self._consume(), name="rms-ingest")
            if self._publisher is None or self._publisher.done():
                self._publisher = asyncio.create_task(
                    self._publish_loop(), name="rms-telemetry-spool"
                )

    async def close(self) -> None:
        tasks = [
            task
            for task in (self._worker, self._publisher)
            if task is not None and not task.done()
        ]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                continue
        self._worker = None
        self._publisher = None

    async def ingest(
        self, samples: Sequence[float], sequence_id: int
    ) -> dict[str, object]:
        """Queue one window and return its ledger row."""
        await self._ensure()
        future: asyncio.Future[dict[str, object]] = (
            asyncio.get_running_loop().create_future()
        )
        await self._inbound.put((samples, sequence_id, future))
        return await future

    async def ledger_depth(self) -> int:
        async with self._lock:
            return len(self._ledger)

    async def _consume(self) -> None:
        while True:
            job = await self._inbound.get()
            try:
                await self._settle(job)
            finally:
                self._inbound.task_done()

    async def _settle(
        self,
        job: tuple[Sequence[float], int, asyncio.Future[dict[str, object]]],
    ) -> None:
        samples, sequence_id, future = job
        try:
            result, header = await self._evaluate(samples, sequence_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not future.done():
                future.set_exception(exc)
            return
        await self._outbound.put(header)
        if not future.done():
            future.set_result(result)

    async def _publish_loop(self) -> None:
        while True:
            header = await self._outbound.get()
            try:
                self._spool.append(header)
            finally:
                self._outbound.task_done()

    async def _evaluate(
        self, samples: Sequence[float], sequence_id: int
    ) -> tuple[dict[str, object], bytes]:
        async with self._lock:
            return self._score(samples, sequence_id)

    def _score(
        self, samples: Sequence[float], sequence_id: int
    ) -> tuple[dict[str, object], bytes]:
        self._guard_sequence(sequence_id)
        if self._next_sequence is None:
            self._next_sequence = sequence_id
        elif sequence_id < self._next_sequence:
            raise EngineKernelException("sequence id moved backwards")
        elif sequence_id > self._next_sequence:
            return self._record_dropout(sequence_id)
        cleaned = self._clean_samples(samples)
        clipped = self._clip_count(cleaned)
        ac_rms = statistics.pstdev(cleaned)
        energy_rms = math.sqrt(statistics.mean([sample * sample for sample in cleaned]))
        if clipped:
            return self._finish_clipped(
                cleaned, sequence_id, clipped, energy_rms, ac_rms
            )
        if ac_rms < self._silence_rms:
            self._logger.warning(
                "silence window rejected sequence=%s ac_rms=%.8f",
                sequence_id,
                ac_rms,
            )
            raise EngineKernelException("near-zero RMS silence window rejected")
        return self._finish_scored(cleaned, sequence_id, energy_rms, ac_rms, 0)

    def _guard_sequence(self, sequence_id: object) -> None:
        if isinstance(sequence_id, bool) or not isinstance(sequence_id, int):
            raise EngineKernelException("sequence id must be an integer")
        if sequence_id < 0:
            raise EngineKernelException("sequence id must be non-negative")

    def _clean_samples(self, samples: Sequence[float]) -> tuple[float, ...]:
        if isinstance(samples, (str, bytes)) or not isinstance(samples, Sequence):
            raise EngineKernelException("waveform must be a sequence of samples")
        cleaned: list[float] = []
        for sample in samples:
            cleaned.append(self._one_sample(sample))
        if len(cleaned) < MIN_WINDOW or len(cleaned) > MAX_WINDOW:
            raise EngineKernelException("window length outside the supported span")
        return tuple(cleaned)

    def _one_sample(self, sample: object) -> float:
        if isinstance(sample, bool) or not isinstance(sample, (int, float)):
            raise EngineKernelException("waveform sample is not numeric")
        value = float(sample)
        if not math.isfinite(value):
            self._logger.warning("non-finite waveform sample rejected")
            raise EngineKernelException("non-finite waveform sample")
        return value

    def _clip_count(self, samples: Sequence[float]) -> int:
        count = 0
        for sample in samples:
            if abs(sample) >= self.full_scale:
                count += 1
        return count

    def _record_dropout(self, sequence_id: int) -> tuple[dict[str, object], bytes]:
        expected = self._next_sequence
        if expected is None:
            raise EngineKernelException("dropout cursor was not initialized")
        gap = sequence_id - expected
        self._next_sequence = sequence_id + 1
        header = pack_dropout_header(self.sample_rate, sequence_id, expected, gap)
        self._logger.warning(
            "sequence gap expected=%s received=%s missing=%s",
            expected,
            sequence_id,
            gap,
        )
        result: dict[str, object] = {
            "status": "dropout",
            "sequence_id": sequence_id,
            "expected_sequence": expected,
            "gap": gap,
            "sample_rate": self.sample_rate,
            "header_hex": header.hex(),
            "telemetry_topic": TELEMETRY_TOPIC,
            "bearing_fault": False,
        }
        return self._store(result, header)

    def _finish_clipped(
        self,
        cleaned: Sequence[float],
        sequence_id: int,
        clipped: int,
        energy_rms: float,
        ac_rms: float,
    ) -> tuple[dict[str, object], bytes]:
        ratios = self._ratios(cleaned)
        fault_score = max(ratios[2], ratios[3])
        self._logger.warning(
            "full-scale clip sequence=%s clipped=%s rms=%.6f score=%.4f",
            sequence_id,
            clipped,
            energy_rms,
            fault_score,
        )
        return self._measurement(
            sequence_id,
            cleaned,
            energy_rms,
            ac_rms,
            ratios,
            clipped,
            bearing_fault=False,
            status="clipped",
        )

    def _finish_scored(
        self,
        cleaned: Sequence[float],
        sequence_id: int,
        energy_rms: float,
        ac_rms: float,
        clipped: int,
    ) -> tuple[dict[str, object], bytes]:
        ratios = self._ratios(cleaned)
        fault_score = max(ratios[2], ratios[3])
        bearing_fault = fault_score >= self.ratio_limit
        if bearing_fault:
            self._logger.info(
                "bearing harmonic sequence=%s score=%.4f bin=%s",
                sequence_id,
                fault_score,
                ratios[0],
            )
        return self._measurement(
            sequence_id,
            cleaned,
            energy_rms,
            ac_rms,
            ratios,
            clipped,
            bearing_fault=bearing_fault,
            status="scored",
        )

    def _ratios(self, cleaned: Sequence[float]) -> tuple[int, float, float, float]:
        centered = self._center(cleaned)
        return _harmonic_ratios(centered)

    def _center(self, cleaned: Sequence[float]) -> tuple[float, ...]:
        dc = statistics.mean(cleaned)
        return tuple(sample - dc for sample in cleaned)

    def _measurement(
        self,
        sequence_id: int,
        cleaned: Sequence[float],
        energy_rms: float,
        ac_rms: float,
        ratios: tuple[int, float, float, float],
        clipped: int,
        bearing_fault: bool,
        status: str,
    ) -> tuple[dict[str, object], bytes]:
        bin_index, _magnitude, second_ratio, third_ratio = ratios
        fault_score = max(second_ratio, third_ratio)
        count = len(cleaned)
        header = pack_measurement_header(
            self.sample_rate, count, energy_rms, sequence_id
        )
        self._next_sequence = sequence_id + 1
        result: dict[str, object] = {
            "status": status,
            "sequence_id": sequence_id,
            "sample_rate": self.sample_rate,
            "samples": count,
            "rms": energy_rms,
            "ac_rms": ac_rms,
            "fundamental_bin": bin_index,
            "fundamental_hz": bin_index * self.sample_rate / count,
            "second_ratio": second_ratio,
            "third_ratio": third_ratio,
            "fault_score": fault_score,
            "bearing_fault": bearing_fault,
            "clipped_count": clipped,
            "ratio_limit": self.ratio_limit,
            "header_hex": header.hex(),
            "telemetry_topic": TELEMETRY_TOPIC,
        }
        return self._store(result, header)

    def _store(
        self, result: dict[str, object], header: bytes
    ) -> tuple[dict[str, object], bytes]:
        stored = dict(result)
        self._ledger.append(stored)
        stored["ledger_depth"] = len(self._ledger)
        return dict(stored), header


def _fundamental_limit(count: int) -> int:
    nyquist = count // 2
    ceiling = min(FUNDAMENTAL_BIN_CEILING, (nyquist - 1) // 3)
    if ceiling < FUNDAMENTAL_BIN_FLOOR:
        raise EngineKernelException("window cannot host a third harmonic")
    return ceiling


def _harmonic_ratios(centered: Sequence[float]) -> tuple[int, float, float, float]:
    ceiling = _fundamental_limit(len(centered))
    best_bin = FUNDAMENTAL_BIN_FLOOR
    best_mag = -1.0
    for bin_index in range(FUNDAMENTAL_BIN_FLOOR, ceiling + 1):
        magnitude = dft_magnitude(centered, bin_index)
        if magnitude > best_mag:
            best_mag = magnitude
            best_bin = bin_index
    second = dft_magnitude(centered, best_bin * 2)
    third_bin = best_bin * 3
    third = dft_magnitude(centered, third_bin)
    if best_mag < 1.0e-8:
        return best_bin, 0.0, 0.0, 0.0
    return best_bin, best_mag, second / best_mag, third / best_mag


async def _demo() -> tuple[dict[str, object], dict[str, object]]:
    engine = HarmonicFaultEngine(sample_rate=4096.0, full_scale=10.0)
    try:
        scored = await engine.ingest(_faulted_window(), 1)
        clipped = await engine.ingest(_saturated_window(engine.full_scale), 2)
        return scored, clipped
    finally:
        await engine.close()


def main() -> int:
    configure_logging()
    logger = logging.getLogger("rms.kernel")
    try:
        scored, clipped = asyncio.run(_demo())
    except EngineKernelException:
        logger.exception("demo window rejected")
        return 1
    logger.info(
        "demo complete status=%s fault_score=%.4f clip_status=%s clipped=%s",
        scored["status"],
        float(scored["fault_score"]),
        clipped["status"],
        clipped["clipped_count"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
