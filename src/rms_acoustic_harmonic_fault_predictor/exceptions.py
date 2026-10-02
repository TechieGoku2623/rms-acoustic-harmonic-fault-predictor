"""Kernel faults for the acoustic harmonic scorer.

A silence window, a non-finite sample, or a broken sequence cursor raises
``EngineKernelException``. Callers treat that as an absent measurement.
Nothing in this package certifies a machine under ISO 10816, and nothing
in it describes how to defeat an interlock or a trip.
"""

from __future__ import annotations


class EngineKernelException(Exception):
    """A window that must not be published as a scored measurement."""
