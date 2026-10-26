#!/usr/bin/env python3
"""Find cross-references in the stock image.

Usage:
  xrefs.py lit  VALUE_HEX...      code that loads VALUE via a PC-relative literal
  xrefs.py call ADDR_HEX...       BL/BLX/B.W instructions targeting ADDR
  xrefs.py litr LO_HEX HI_HEX     all literal values in [LO, HI) and their users
  xrefs.py word VALUE_HEX...      every aligned 32-bit word equal to VALUE
                                  (finds function pointers in .data/rodata)
"""
import collections
import struct
import sys

from fwimage import BASE, load

D = load()
N = len(D)


def h(o):
    return struct.unpack_from("<H", D, o)[0]


LIT = collections.defaultdict(list)
CALL = collections.defaultdict(list)
for o in range(0, N - 4, 2):
    a, w = BASE + o, h(o)
    la = None
    if (w & 0xF800) == 0x4800:                       # ldr rt, [pc, #imm8*4]
        la = ((a + 4) & ~3) + (w & 0xFF) * 4
    elif (w & 0xFF7F) == 0xF85F:                     # ldr.w rt, [pc, #+-imm12]
        imm = h(o + 2) & 0xFFF
        la = ((a + 4) & ~3) + (imm if w & 0x80 else -imm)
    if la is not None and BASE <= la < BASE + N - 3:
        LIT[struct.unpack_from("<I", D, la - BASE)[0]].append(a)
    if (w & 0xF800) == 0xF000:
        w2 = h(o + 2)
        kind = w2 & 0xD000
        if kind in (0xD000, 0xC000, 0x9000):         # BL, BLX, B.W
            s = (w >> 10) & 1
            i1 = 1 - (((w2 >> 13) & 1) ^ s)
            i2 = 1 - (((w2 >> 11) & 1) ^ s)
            off = (s << 24) | (i1 << 23) | (i2 << 22) | ((w & 0x3FF) << 12) | ((w2 & 0x7FF) << 1)
            if s:
                off -= 1 << 25
            t = a + 4 + off
            if kind == 0xC000:
                t &= ~3
            CALL[t].append(a)

if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    mode, args = sys.argv[1], [int(x, 16) for x in sys.argv[2:]]
    if mode == "litr":
        for k in sorted(LIT):
            if args[0] <= k < args[1]:
                print(f"{k:#x}", " ".join(hex(r) for r in LIT[k]))
    else:
        for v in args:
            if mode == "lit":
                refs = LIT.get(v, [])
            elif mode == "call":
                refs = CALL.get(v & ~1, [])
            elif mode == "word":
                refs = [BASE + i for i in range(0, N - 3, 4) if struct.unpack_from("<I", D, i)[0] == v]
            else:
                raise SystemExit(__doc__)
            print(f"{v:#x}", " ".join(hex(r) for r in refs))
