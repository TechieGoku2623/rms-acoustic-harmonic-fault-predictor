"""Big-endian header for one scored acoustic window.

Layout, 16 bytes: float64 sample rate, uint32 window length, uint32 flags.
Bit 0 is the fault bit, bit 1 means at least one sample was clamped, and
bit 2 means the batch contained at least one missing sequence id.
"""

from __future__ import annotations

import struct

from .exceptions import EngineKernelException

HEADER_FORMAT = ">dII"
FLAG_FAULT = 1
FLAG_CLIP = 2
FLAG_DROPOUT = 4


def pack_header(sample_rate: float, count: int, flags: int) -> bytes:
    """Pack sample rate, length, and flags. The body is not a waveform."""
    return struct.pack(
        HEADER_FORMAT, float(sample_rate), int(count), int(flags) & 0xFFFFFFFF
    )


def unpack_header(payload: bytes) -> tuple[float, int, int]:
    """Inverse of :func:`pack_header`."""
    expected = struct.calcsize(HEADER_FORMAT)
    if len(payload) != expected:
        raise EngineKernelException("acoustic header length is not 16 bytes")
    sample_rate, count, flags = struct.unpack(HEADER_FORMAT, payload)
    return float(sample_rate), int(count), int(flags)
