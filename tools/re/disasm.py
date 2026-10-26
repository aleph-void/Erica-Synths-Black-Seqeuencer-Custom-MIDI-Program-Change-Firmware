#!/usr/bin/env python3
"""Disassemble the stock image at an address, annotating PC-relative literals.

Usage: disasm.py ADDR_HEX [COUNT]     (image path from $EBSEQ_BIN or repo default)
"""
import sys

import capstone

from fwimage import BASE, load, u32

D = load()
md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_MCLASS)


def annotate(insn):
    if not (insn.mnemonic.startswith("ldr") and "[pc" in insn.op_str):
        return ""
    try:
        imm = int(insn.op_str.split("#")[-1].rstrip("]"), 0)
        v = u32(D, ((insn.address + 4) & ~3) + imm)
    except (ValueError, IndexError, Exception):
        return ""
    text = f"   ; ={v:#x}"
    if BASE <= v < BASE + len(D):
        s = D[v - BASE:v - BASE + 48].split(b"\0")[0]
        if len(s) > 3 and all(32 <= c < 127 for c in s):
            text += f' "{s.decode()}"'
    return text


def disasm(addr, count):
    off = (addr & ~1) - BASE
    out = []
    while len(out) < count and off < len(D):
        insn = next(md.disasm(D[off:off + 4], BASE + off), None)
        if insn is None:
            out.append(f"{BASE + off:08x}: .short {D[off:off + 2].hex()}")
            off += 2
            continue
        out.append(f"{insn.address:08x}: {insn.bytes.hex():10s} {insn.mnemonic:8s} "
                   f"{insn.op_str}{annotate(insn)}")
        off += insn.size
    return out


if __name__ == "__main__":
    print("\n".join(disasm(int(sys.argv[1], 16), int(sys.argv[2]) if len(sys.argv) > 2 else 40)))
