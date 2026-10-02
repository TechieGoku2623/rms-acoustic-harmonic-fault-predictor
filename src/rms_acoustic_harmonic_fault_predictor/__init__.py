"""RMS acoustic harmonic fault predictor.

The engine scores a numeric waveform the caller already has. It does not
talk to a sensor, and it does not tell an operator to bypass a trip.
"""

from __future__ import annotations

from .engine import RmsAcousticHarmonicFaultPredictor
from .exceptions import EngineKernelException

__all__ = [
    EngineKernelException.__name__,
    RmsAcousticHarmonicFaultPredictor.__name__,
]
__version__ = "1.0.0"
