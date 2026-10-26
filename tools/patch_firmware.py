#!/usr/bin/env python3
"""Apply the MIDI Program Change patch to stock Black Sequencer firmware 1.06.

Usage: patch_firmware.py STOCK_BIN PATCH_ELF OUT_DIR

The stock image is not modified. Every patched location is checked against
the expected stock bytes before it is changed, so the tool refuses to run on
any other firmware version. The output is written as
OUT_DIR/EBSEQ_V1_07_<CRC32>.BIN, the naming the stock updater expects.
"""
import struct
import subprocess
import sys
import zlib
from pathlib import Path

BASE = 0x0800C000
STOCK_SIZE = 0x37F3C
STOCK_CRC = 0x7824E1DD
PATCH_ORIGIN = 0x08043F40
VERSION = (1, 7)

OLD_BSS_END = 0x2001ED08
NEW_BSS_END = 0x2001ED28


def bl(src, dst, link=True):
    """Encode a Thumb-2 BL (or B.W when link=False) from src to dst."""
    off = (dst & ~1) - (src + 4)
    if not -(1 << 24) <= off < (1 << 24):
        raise ValueError(f"branch out of range {src:#x} -> {dst:#x}")
    s = (off >> 24) & 1
    i1 = (off >> 23) & 1
    i2 = (off >> 22) & 1
    imm10 = (off >> 12) & 0x3FF
    imm11 = (off >> 1) & 0x7FF
    j1 = (1 - i1) ^ s
    j2 = (1 - i2) ^ s
    hw1 = 0xF000 | (s << 10) | imm10
    hw2 = (0xD000 if link else 0x9000) | (j1 << 13) | (j2 << 11) | imm11
    return struct.pack("<HH", hw1, hw2)


def symbols(elf):
    out = subprocess.run(["arm-none-eabi-nm", str(elf)], check=True,
                         capture_output=True, text=True).stdout
    syms = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3:
            syms[parts[2]] = int(parts[0], 16)
    return syms


def section_bytes(elf, name):
    tmp = Path(elf).with_suffix(f".{name.strip('.')}.bin")
    subprocess.run(["arm-none-eabi-objcopy", "-O", "binary", "-j", name,
                    str(elf), str(tmp)], check=True)
    return tmp.read_bytes()


def section_sizes(elf):
    out = subprocess.run(["arm-none-eabi-size", "-A", str(elf)], check=True,
                         capture_output=True, text=True).stdout
    sizes = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0].startswith("."):
            sizes[parts[0]] = (int(parts[1]), int(parts[2]))
    return sizes


class Image:
    def __init__(self, data):
        self.data = bytearray(data)

    def patch(self, addr, old, new, what):
        off = addr - BASE
        cur = bytes(self.data[off:off + len(old)])
        if cur != old:
            raise SystemExit(f"{what}: expected {old.hex()} at {addr:#010x}, "
                             f"found {cur.hex()}")
        if len(new) != len(old):
            raise SystemExit(f"{what}: size mismatch")
        self.data[off:off + len(new)] = new
        print(f"  {addr:#010x}  {old.hex():>8} -> {new.hex():<8}  {what}")


def main():
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    stock_path, elf, out_dir = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])

    stock = stock_path.read_bytes()
    if len(stock) != STOCK_SIZE or zlib.crc32(stock) != STOCK_CRC:
        raise SystemExit(f"{stock_path} is not stock firmware 1.06 "
                         f"(expected {STOCK_SIZE} bytes, CRC32 {STOCK_CRC:08X})")

    sizes = section_sizes(elf)
    for name in (".data", ".bss"):
        if sizes.get(name, (0, 0))[0]:
            raise SystemExit(f"patch has a non-empty {name} section; "
                             "put patch variables in .pcram")
    if sizes[".pcram"][0] > NEW_BSS_END - OLD_BSS_END:
        raise SystemExit("patch RAM exceeds the reserved block")
    if sizes[".text"][1] != PATCH_ORIGIN:
        raise SystemExit("patch .text is not linked at PATCH_ORIGIN")

    code = section_bytes(elf, ".text")
    sym = symbols(elf)
    img = Image(stock)
    w = lambda v: struct.pack("<I", v)

    print("Hooks:")
    img.patch(0x08042CBC, w(0x080294C5), w(sym["pc_midi_dispatch"] | 1),
              "MIDI dispatch callback (.data init) -> pc_midi_dispatch")
    img.patch(0x08020114, bl(0x08020114, 0x080156B8),
              bl(0x08020114, sym["pc_main_poll"]),
              "main loop song poll -> pc_main_poll")
    img.patch(0x0801EA26, bl(0x0801EA26, 0x0802CB34),
              bl(0x0801EA26, sym["pc_settings_init"]),
              "boot settings load -> pc_settings_init")
    img.patch(0x0801959A, bl(0x0801959A, 0x0801EC58),
              bl(0x0801959A, sym["pc_midi_page_exit"]),
              "MIDI page exit save -> pc_midi_page_exit")
    img.patch(0x0801914C, bytes.fromhex("00232b70"),
              bl(0x0801914C, sym["pc_fmt_stub"]),
              "MIDI MISC row formatter -> pc_fmt_stub")
    img.patch(0x08019438, bytes.fromhex("0029cbd1"),
              bl(0x08019438, sym["pc_edit_stub"], link=False),
              "MIDI MISC row edit -> pc_edit_stub")

    print("Data:")
    img.patch(0x0803ABA8, b"\x01", b"\x04",
              "MIDI MISC tab row count 1 -> 4")
    img.patch(0x0801EC7C, bytes.fromhex("4ff4a060"), bytes.fromhex("4ff4b060"),
              "factory erase size 0x500 -> 0x580 (covers patch slot)")
    for addr in (0x0800C1C8, 0x08012B54, 0x0802030C):
        img.patch(addr, w(OLD_BSS_END), w(NEW_BSS_END),
                  "bss end / heap base +32 bytes for patch RAM")

    print("Version 1.06 -> 1.07:")
    minor = VERSION[1]
    img.patch(0x0801796A, bytes([6, 0x27]), bytes([minor, 0x27]), "INFO page")
    img.patch(0x0801EDBA, bytes([6, 0x23]), bytes([minor, 0x23]), "boot splash")
    img.patch(0x08015FAC, bytes([6, 0x23]), bytes([minor, 0x23]), "update prompt CURRENT")
    img.patch(0x0802AD4E, bytes([6, 0x2A]), bytes([minor, 0x2A]), "newer-firmware check")

    pad = PATCH_ORIGIN - (BASE + len(img.data))
    out = bytes(img.data) + b"\x00" * pad + code
    crc = zlib.crc32(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"EBSEQ_V{VERSION[0]}_{VERSION[1]:02d}_{crc:08X}.BIN"
    (out_dir / name).write_bytes(out)
    print(f"Patch code: {len(code)} bytes at {PATCH_ORIGIN:#010x}")
    print(f"Wrote {out_dir / name} ({len(out)} bytes)")


if __name__ == "__main__":
    main()
