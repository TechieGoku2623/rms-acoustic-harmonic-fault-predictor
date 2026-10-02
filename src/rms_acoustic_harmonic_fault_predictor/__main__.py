"""Run one faulted window through the acoustic engine."""

from __future__ import annotations

import asyncio
import logging
import math
import sys

from .engine import RmsAcousticHarmonicFaultPredictor
from .exceptions import EngineKernelException

_SAMPLE_RATE = 1600.0
_FUNDAMENTAL_HZ = 50.0
_COUNT = 128


def _demo_window() -> list[float]:
    samples: list[float] = []
    for index in range(_COUNT):
        base = 2.0 * math.pi * _FUNDAMENTAL_HZ * index / _SAMPLE_RATE
        samples.append(
            0.40 * math.sin(base)
            + 0.16 * math.sin(2.0 * base)
            + 0.10 * math.sin(3.0 * base)
        )
    return samples


async def _demo() -> dict[str, object]:
    engine = RmsAcousticHarmonicFaultPredictor(
        sample_rate=_SAMPLE_RATE,
        fundamental_hz=_FUNDAMENTAL_HZ,
        full_scale=1.0,
        ratio_limit=0.25,
    )
    return await engine.run([{"sequence_id": 0, "samples": _demo_window()}])


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    logger = logging.getLogger("rms_acoustic_harmonic_fault_predictor")
    try:
        result = asyncio.run(_demo())
    except EngineKernelException:
        logger.exception("demo window rejected")
        return 1
    if not result["fault"] or int(result["clips"]) != 0 or int(result["dropouts"]) != 0:
        logger.error("demo window did not score as a harmonic fault")
        return 1
    logger.info(
        "demo complete fault=%s harmonic_ratio=%.4f rms=%.6f clips=%s dropouts=%s",
        result["fault"],
        float(result["harmonic_ratio"]),
        float(result["rms"]),
        result["clips"],
        result["dropouts"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
