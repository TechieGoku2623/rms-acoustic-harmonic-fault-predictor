"""Fixed-seed latency sample for the acoustic engine."""

from __future__ import annotations

import asyncio
import math
import random
import statistics
import sys
import time
import tracemalloc

from .engine import RmsAcousticHarmonicFaultPredictor

SEED = 10816
ITERATIONS = 5000
WARMUP = 20
_SAMPLE_RATE = 1600.0
_FUNDAMENTAL_HZ = 50.0
_COUNT = 128


def _empirical_p99(samples: list[float]) -> float:
    ordered = sorted(samples)
    rank = math.ceil(0.99 * len(ordered))
    index = min(len(ordered), max(1, rank)) - 1
    return ordered[index]


def _window(rng: random.Random) -> list[float]:
    samples: list[float] = []
    for index in range(_COUNT):
        angle = 2.0 * math.pi * _FUNDAMENTAL_HZ * index / _SAMPLE_RATE
        samples.append(0.5 * math.sin(angle) + rng.uniform(-1.0e-6, 1.0e-6))
    return samples


async def _execute() -> dict[str, object]:
    rng = random.Random(SEED)
    engine = RmsAcousticHarmonicFaultPredictor(
        sample_rate=_SAMPLE_RATE,
        fundamental_hz=_FUNDAMENTAL_HZ,
        full_scale=1.0,
    )
    record = [{"sequence_id": 0, "samples": _window(rng)}]
    for _ in range(WARMUP):
        await engine.run(record)
    latencies: list[float] = []
    tracemalloc.start()
    try:
        last = None
        for _ in range(ITERATIONS):
            started = time.perf_counter_ns()
            last = await engine.run(record)
            latencies.append((time.perf_counter_ns() - started) / 1000.0)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    if (
        last is None
        or bool(last["fault"])
        or int(last["clips"])
        or int(last["dropouts"])
    ):
        raise RuntimeError("benchmark window was not a clean fundamental")
    return {
        "status": "ok",
        "seed": SEED,
        "iterations": ITERATIONS,
        "latency_us": round(latencies[-1], 3),
        "memory_peak_bytes": int(peak),
        "benchmark_avg_us": round(statistics.fmean(latencies), 3),
        "benchmark_p99_us": round(_empirical_p99(latencies), 3),
    }


def main() -> int:
    try:
        report = asyncio.run(_execute())
    except Exception as exc:
        print(f"status=error detail={type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(
        "status={status} seed={seed} iterations={iterations} "
        "latency_us={latency_us:.3f} memory_peak_bytes={memory_peak_bytes} "
        "benchmark_avg_us={benchmark_avg_us:.3f} "
        "benchmark_p99_us={benchmark_p99_us:.3f}".format(**report)
    )
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
