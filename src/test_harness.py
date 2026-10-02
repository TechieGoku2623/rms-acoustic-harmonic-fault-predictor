"""Deterministic checks and a latency sample for the harmonic fault engine."""

from __future__ import annotations

import asyncio
import importlib
import json
import math
import random
import statistics
import struct
import sys
import time
import tracemalloc
from pathlib import Path

SEED = 10816
ITERATIONS = 5000
WARMUP = 20
WINDOW = 64
NOISE = 0.002


def load_module():
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    return importlib.import_module("src.main")


def expect(condition: bool, detail: object) -> None:
    if not condition:
        raise AssertionError(detail)


def empirical_p99(samples: list[float]) -> float:
    ordered = sorted(samples)
    rank = math.ceil(0.99 * len(ordered))
    index = min(len(ordered), max(1, rank)) - 1
    return ordered[index]


def tone(bin_index: int, amplitude: float, count: int = WINDOW) -> list[float]:
    scale = 2.0 * math.pi * bin_index / count
    return [amplitude * math.sin(scale * index) for index in range(count)]


def mix(left: list[float], right: list[float]) -> list[float]:
    return [left[index] + right[index] for index in range(len(left))]


def add_noise(
    rng: random.Random, samples: list[float], amplitude: float
) -> list[float]:
    return [sample + rng.uniform(-amplitude, amplitude) for sample in samples]


def saturate(samples: list[float], full_scale: float) -> list[float]:
    clamped: list[float] = []
    for sample in samples:
        if sample > full_scale:
            clamped.append(full_scale)
        elif sample < -full_scale:
            clamped.append(-full_scale)
        else:
            clamped.append(sample)
    return clamped


async def check_healthy_and_fault(mod, rng: random.Random) -> None:
    engine = mod.HarmonicFaultEngine(sample_rate=4096.0, full_scale=10.0)
    healthy = add_noise(rng, tone(4, 1.0), NOISE)
    faulted = add_noise(rng, mix(tone(4, 1.2), tone(8, 0.9)), NOISE)
    try:
        healthy_row = await engine.ingest(healthy, 0)
        fault_row = await engine.ingest(faulted, 1)
    finally:
        await engine.close()
    expect(healthy_row["status"] == "scored", healthy_row)
    expect(healthy_row["bearing_fault"] is False, healthy_row)
    expect(healthy_row["fault_score"] < healthy_row["ratio_limit"], healthy_row)
    expect(fault_row["status"] == "scored", fault_row)
    expect(fault_row["bearing_fault"] is True, fault_row)
    expect(fault_row["fault_score"] >= fault_row["ratio_limit"], fault_row)
    expect(fault_row["fundamental_bin"] == 4, fault_row)
    blob = bytes.fromhex(fault_row["header_hex"])
    tag, rate, count, rms_bits, sequence_id = struct.unpack(">BfIII", blob)
    rms = struct.unpack(">f", struct.pack(">I", rms_bits))[0]
    expect(tag == mod.MEASUREMENT_TAG, tag)
    expect(math.isclose(rate, 4096.0, rel_tol=1e-6), rate)
    expect(count == WINDOW, count)
    expect(sequence_id == 1, sequence_id)
    expect(abs(rms - fault_row["rms"]) < 1e-5, (rms, fault_row["rms"]))


async def check_clipping(mod) -> None:
    engine = mod.HarmonicFaultEngine(
        sample_rate=4096.0, full_scale=10.0, ratio_limit=0.20
    )
    raw = mix(tone(4, 1.2), tone(8, 0.9))
    peak = max(abs(sample) for sample in raw)
    amplified = [10.0 * sample / peak * 4.0 for sample in raw]
    clipped_window = saturate(amplified, engine.full_scale)
    try:
        row = await engine.ingest(clipped_window, 0)
    finally:
        await engine.close()
    at_rail = sum(1 for sample in clipped_window if abs(sample) >= engine.full_scale)
    expect(row["status"] == "clipped", row)
    expect(at_rail > 0, at_rail)
    expect(row["clipped_count"] == at_rail, row)
    expect(row["bearing_fault"] is False, row)
    expect(row["fault_score"] >= row["ratio_limit"], row)


async def check_dropout_gap(mod) -> None:
    engine = mod.HarmonicFaultEngine(sample_rate=4096.0, full_scale=10.0)
    window = tone(4, 1.0)
    try:
        await engine.ingest(window, 0)
        await engine.ingest(window, 1)
        gap = await engine.ingest(window, 4)
        resumed = await engine.ingest(window, 5)
        depth = await engine.ledger_depth()
    finally:
        await engine.close()
    expect(gap["status"] == "dropout", gap)
    expect(gap["expected_sequence"] == 2, gap)
    expect(gap["gap"] == 2, gap)
    expect(gap["bearing_fault"] is False, gap)
    blob = bytes.fromhex(gap["header_hex"])
    tag, _rate, sequence_id, expected, missing = struct.unpack(">BfIII", blob)
    expect(tag == mod.DROPOUT_TAG, tag)
    expect(sequence_id == 4, sequence_id)
    expect(expected == 2, expected)
    expect(missing == 2, missing)
    expect(resumed["status"] == "scored", resumed)
    expect(resumed["sequence_id"] == 5, resumed)
    expect(depth == 4, depth)


async def check_silence_and_non_finite(mod) -> None:
    engine = mod.HarmonicFaultEngine(sample_rate=4096.0, full_scale=10.0)
    window = tone(4, 1.0)
    silent = [0.0] * WINDOW
    poisoned = list(window)
    poisoned[3] = math.nan
    try:
        await engine.ingest(window, 0)
        depth = await engine.ledger_depth()
        silence_raised = False
        try:
            await engine.ingest(silent, 1)
        except mod.EngineKernelException:
            silence_raised = True
        expect(silence_raised, "silence window was accepted")
        expect(await engine.ledger_depth() == depth, "silence wrote a ledger row")
        retried = await engine.ingest(window, 1)
        finite_raised = False
        try:
            await engine.ingest(poisoned, 2)
        except mod.EngineKernelException:
            finite_raised = True
        expect(finite_raised, "non-finite sample was accepted")
        expect(
            await engine.ledger_depth() == depth + 1, "non-finite wrote a ledger row"
        )
        backwards = False
        try:
            await engine.ingest(window, 0)
        except mod.EngineKernelException:
            backwards = True
        expect(backwards, "backwards sequence was accepted")
    finally:
        await engine.close()
    expect(retried["status"] == "scored", retried)


async def check_ledger_bound(mod) -> None:
    engine = mod.HarmonicFaultEngine(ledger_limit=3)
    window = tone(4, 1.0)
    try:
        last = None
        for sequence_id in range(5):
            last = await engine.ingest(window, sequence_id)
        depth = await engine.ledger_depth()
    finally:
        await engine.close()
    expect(depth == 3, depth)
    expect(last is not None and last["ledger_depth"] == 3, last)


async def run_benchmark(mod) -> dict[str, float | int]:
    engine = mod.HarmonicFaultEngine(sample_rate=4096.0, full_scale=10.0)
    window = tone(4, 1.0)
    sequence_id = 0
    latencies: list[float] = []
    try:
        for _ in range(WARMUP):
            await engine.ingest(window, sequence_id)
            sequence_id += 1
        tracemalloc.start()
        last = None
        for _ in range(ITERATIONS):
            started = time.perf_counter_ns()
            last = await engine.ingest(window, sequence_id)
            latencies.append((time.perf_counter_ns() - started) / 1000.0)
            sequence_id += 1
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        await engine.close()
    expect(last is not None and last["status"] == "scored", last)
    expect(last["bearing_fault"] is False, last)
    return {
        "n": ITERATIONS,
        "avg_us": round(statistics.fmean(latencies), 3),
        "p99_us": round(empirical_p99(latencies), 3),
        "peak_bytes": peak,
    }


async def execute(name: str, func) -> dict[str, object]:
    try:
        await func()
    except Exception as exc:
        return {
            "name": name,
            "passed": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {"name": name, "passed": True}


async def amain() -> dict[str, object]:
    mod = load_module()
    rng = random.Random(SEED)
    checks = [
        ("healthy_and_harmonic_fault", lambda: check_healthy_and_fault(mod, rng)),
        ("clipping_at_full_scale", lambda: check_clipping(mod)),
        ("dropout_gap", lambda: check_dropout_gap(mod)),
        ("silence_and_non_finite", lambda: check_silence_and_non_finite(mod)),
        ("ledger_bound", lambda: check_ledger_bound(mod)),
    ]
    results = []
    for name, func in checks:
        results.append(await execute(name, func))
    benchmark = await run_benchmark(mod)
    status = "PASS" if all(item["passed"] for item in results) else "FAIL"
    return {
        "status": status,
        "seed": SEED,
        "checks": results,
        "benchmark": benchmark,
    }


def main() -> int:
    summary = asyncio.run(amain())
    benchmark = summary["benchmark"]
    print(
        "benchmark "
        f"n={benchmark['n']} avg_us={benchmark['avg_us']:.3f} "
        f"p99_us={benchmark['p99_us']:.3f} peak_bytes={benchmark['peak_bytes']}"
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
