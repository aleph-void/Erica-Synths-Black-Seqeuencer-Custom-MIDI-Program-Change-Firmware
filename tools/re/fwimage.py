"""Shared loader for the stock image used by the reverse-engineering helpers."""
import os
import struct
from pathlib import Path

BASE = 0x0800C000  # application load address (bootloader occupies 0x08000000-0x0800BFFF)
DEFAULT = Path(__file__).resolve().parents[2] / "EBSEQ_V1_06_7824E1DD.BIN"


def load():
    return Path(os.environ.get("EBSEQ_BIN", DEFAULT)).read_bytes()


def u32(data, addr):
    return struct.unpack_from("<I", data, addr - BASE)[0]
