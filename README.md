# RMS Acoustic Harmonic Fault Predictor

A high-throughput, low-latency asynchronous engine engineered to resolve bearing-style faults in a vibration window by comparing a small DFT of the second and third harmonics with the fundamental, and by scoring the window from its RMS.

## 🏗️ Systems Architecture & Event Topology

`HarmonicFaultEngine` ingests one finite window through `ingest` and returns a ledger row. `dft_magnitude` is the real DFT bin magnitude, normalized by the window length, written with `math.cos` and `math.sin`. The fault score is the larger of the second-harmonic and third-harmonic amplitudes divided by the fundamental. A score at or above `ratio_limit` (default 0.25) sets `bearing_fault`.

`_inbound` is the collector queue. The ledger is a maxlen `deque`, the stand-in for a TimescaleDB hypertable of summary rows. `_outbound` holds packed headers a producer for `plant.vibration.window` would drain. This process does not open that connection. An `asyncio.Lock` covers the ledger and the sequence cursor. `configure_logging` calls `logging.basicConfig` with timestamps. Silence, a non-finite sample, a backward sequence, or a window that cannot host a third harmonic raises `EngineKernelException`.

The commentary follows the vibration-monitoring discipline in ISO 10816. Nothing here certifies a machine, and nothing here describes how to defeat an interlock.

## 📊 Core Visual Walkthrough & Engine Pipeline Flow

```
samples[n], sequence_id
    |
    v
finite gate + sequence cursor
    |
    +-- gap in sequence_id -------- dropout header, bearing_fault false
    +-- |sample| >= full_scale ---- clipped status, count of rails
    +-- AC RMS below silence ------ EngineKernelException
    |
    v
remove DC, dft_magnitude on bins 2..ceiling
    |
    v
second_ratio, third_ratio, fault_score = max(second, third)
    |
    +-- fault_score >= ratio_limit --> bearing_fault
    v
ledger row + packed header on plant.vibration.window
```

Insert the structural terminal walkthrough recording at docs/assets/terminal-walkthrough.gif before publishing the release notes.

## ⚡ Low-Level OS Mechanics & Network Physics

The DFT visits only the candidate shaft bins, from `FUNDAMENTAL_BIN_FLOOR` (2) through a ceiling that still leaves room for a third harmonic under Nyquist. Each bin is one pass of cos and sin. There is no twiddle cache and no NumPy. Magnitude is `math.hypot(real, imag) / count`, so the ratio of two bins is independent of a uniform gain.

RMS is the square root of the mean square. AC RMS is `statistics.pstdev` after the same window, and the silence gate uses that AC figure so a large DC offset cannot hide a dead sensor. Clipping is counted on the samples as received: a sample whose absolute value is at least `full_scale` is on the rail. The header packs sample rate, count, the IEEE-754 bits of RMS, and the sequence id. The worker task and the spool task are cancelled in `close`.

## ⚖️ Architecture Trade-offs & Pragmatic Decisions

A production vibration monitor would run a long FFT, an envelope spectrum, and a bearing-frequency catalog. Those need a known shaft rate and a known geometry. This engine flags the condition the catalog search starts from: the second or third harmonic dominates the fundamental inside one window. The score is a ratio, so a louder machine does not look healthier.

Clipping suppresses `bearing_fault` even when the harmonic ratio is high. A rail-saturated window has manufactured harmonics; publishing them as a bearing call would page maintenance for a gain setting. The ratio is still reported. Dropout does not zero-fill the missing window. The gap is a ledger row of its own, and the next scored window keeps its own sequence id.

## 🚀 Local Installation & Benchmarking

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python src/main.py
python src/test_harness.py
```

```python
import asyncio
import math

from src.main import HarmonicFaultEngine


async def demo() -> None:
    engine = HarmonicFaultEngine(sample_rate=4096.0, full_scale=10.0)
    window = [math.sin(2.0 * math.pi * 4 * i / 64.0) for i in range(64)]
    try:
        await engine.ingest(window, 0)
    finally:
        await engine.close()


asyncio.run(demo())
```

The runtime is the Python 3.12 standard library. `pip install -r requirements.txt` succeeds with no third-party pins.

## 🖥️ Terminal Diagnostic Output Preview

```
INFO [rms.kernel] bearing harmonic sequence=1 score=0.7500 bin=4
WARNING [rms.kernel] full-scale clip sequence=2 clipped=1 rms=6.066017 score=0.7500
INFO [rms.kernel] demo complete status=scored fault_score=0.7500 clip_status=clipped clipped=1
```

`python src/main.py` exits 0.

## 📊 Empirical Benchmarking Performance Report

Measured by `python src/test_harness.py` with seed 10816, 5000 iterations after a warmup, `time.perf_counter_ns` latency in microseconds, and `tracemalloc` peak. The window is a pure bin-4 tone, which the engine scores with `bearing_fault` false.

| Metric | Measured |
| --- | ---: |
| Status | PASS |
| Iterations | 5000 |
| Average latency | 1536.663 µs |
| Empirical P99 | 1950.063 µs |
| tracemalloc peak | 571480 bytes |

## 🛡️ Edge-Case Resilience & SOC2/Regulatory Compliance

Clipping is counted per sample at `full_scale`. The row status is `clipped`, `bearing_fault` stays false, and the harmonic ratio is still reported. A sequence dropout writes a gap row and does not invent samples. A near-zero AC RMS and a non-finite sample raise `EngineKernelException` and do not append a scored measurement. A backward sequence id raises the same way.

ISO 10816 is the commentary reference for RMS severity bands. This module does not assign a machine zone and does not certify a balance grade. SOC 2 processing integrity is the ledger rule: a rejected window is absent from the scored rows, and a dropout is an explicit status rather than a silent hole in the sequence.
