#!/usr/bin/env python3
"""Execute the patched firmware's new code paths under Unicorn.

Usage: test_patch.py STOCK_BIN BUILD_DIR

Real firmware code is executed wherever it has no hardware or SD-card side
effects: the stock MIDI ring-buffer drain and byte parser (EXTI0 handler
body), the stock MIDI dispatcher, the stock sprintf used by the settings
rows and the patched instructions at every hook site. Firmware routines that
would touch the SD card, the EEPROM or the sequencer engine are replaced by
Python stubs that record their arguments, so the tests can check that the
patch drives them in the same order the front panel does.
"""
import struct
import sys
import zlib
from pathlib import Path

from unicorn import (UC_ARCH_ARM, UC_HOOK_CODE, UC_HOOK_MEM_INVALID,
                     UC_MODE_MCLASS, UC_MODE_THUMB, Uc, UcError)
from unicorn.arm_const import (UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_R0,
                               UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
                               UC_ARM_REG_R5, UC_ARM_REG_SP)

BASE = 0x0800C000
DATA_LOAD, DATA_START, DATA_SIZE = 0x08040CC4, 0x20000000, 0x3278
RET = 0x08000000           # parking address used as the return target
MIDI = 0x20001EEC          # stock MIDI port object
PCRAM = 0x2001ED08         # patch RAM block
PROJECT = 0x2001E274
SEQ_PTR = 0x20005A8C
PAGE_PTR = 0x2000D874

FAKE_SEQ = 0x20020000      # stand-in sequencer engine object
FAKE_PAGE = 0x20020100     # stand-in PATTERN page object
IDLE_BUF = 0x20000028      # stock "NEXT" pattern buffer
BUF = 0x20021000           # scratch buffers
STACK_TOP = 0x2002E000

STUBS = {
    0x080156B8: "song_poll", 0x080336D0: "lock", 0x080336E0: "unlock",
    0x0802C1B0: "pattern_exists", 0x0802C030: "bank_select",
    0x080156EC: "song_stop", 0x0802C0A0: "pattern_load",
    0x0801370C: "idle_buffer", 0x08031B74: "seq_cue",
    0x08031B04: "seq_state", 0x08031B80: "seq_swap",
    0x08031BDC: "seq_rebind", 0x0802C0F0: "commit_select",
    0x08025B68: "widget_next", 0x0802CB34: "setting_load",
    0x0802CB70: "setting_save", 0x0801EC58: "devcfg_save",
    0x08013970: "midi_clock_tick",
}

failures = 0


def check(cond, what):
    global failures
    print(f"  {'ok  ' if cond else 'FAIL'} {what}")
    if not cond:
        failures += 1


class Fw:
    def __init__(self, image, stub_dispatch=False):
        self.uc = uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        uc.mem_map(0x08000000, 0x00100000)
        uc.mem_write(BASE, image)
        uc.mem_write(RET, b"\xfe\xe7")  # b .
        uc.mem_map(0x20000000, 0x00030000)
        uc.mem_write(DATA_START, image[DATA_LOAD - BASE:DATA_LOAD - BASE + DATA_SIZE])
        uc.mem_map(0xE000E000, 0x1000)   # SCB/NVIC (CPACR for the FPU)
        uc.mem_write(0xE000ED88, struct.pack("<I", 0xF << 20))
        self.calls = []
        self.lock_depth = 0
        self.exists = set()
        self.state = 1
        self.load_ret = 0x14
        self.eeprom = {}
        stubs = dict(STUBS)
        if stub_dispatch:
            stubs[0x080294C4] = "midi_dispatch"
        for addr, name in stubs.items():
            uc.hook_add(UC_HOOK_CODE, self._stub, name, addr, addr)
        uc.hook_add(UC_HOOK_MEM_INVALID, self._bad)

    def _bad(self, uc, access, addr, size, value, _):
        print(f"    invalid memory access at {addr:#x} pc={uc.reg_read(UC_ARM_REG_PC):#x}")
        return False

    def _stub(self, uc, addr, size, name):
        r = [uc.reg_read(x) for x in (UC_ARM_REG_R0, UC_ARM_REG_R1,
                                      UC_ARM_REG_R2, UC_ARM_REG_R3)]
        ret = 0
        if name == "lock":
            self.lock_depth += 1
        elif name == "unlock":
            self.lock_depth -= 1
        elif name == "pattern_exists":
            ret = int((r[0], r[1]) in self.exists)
        elif name == "idle_buffer":
            ret = IDLE_BUF
        elif name == "seq_state":
            ret = self.state
        elif name == "seq_cue":
            self.w32(FAKE_SEQ + 0x0C, r[0])
        elif name == "setting_load":
            if r[0] == 0x2000201C:
                ret = 0
            else:
                addr_, size_, ptr = struct.unpack("<HHI", self.read(r[0], 8))
                if self.load_ret == 0 and addr_ in self.eeprom:
                    uc.mem_write(ptr, self.eeprom[addr_][:size_])
                ret = self.load_ret
        elif name == "setting_save":
            addr_, size_, ptr = struct.unpack("<HHI", self.read(r[0], 8))
            self.eeprom[addr_] = self.read(ptr, size_)
        rec = (name, tuple(r), self.lock_depth)
        self.calls.append(rec)
        uc.reg_write(UC_ARM_REG_R0, ret)
        uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR) | 1)

    def read(self, addr, n):
        return bytes(self.uc.mem_read(addr, n))

    def r8(self, addr):
        return self.read(addr, 1)[0]

    def r16(self, addr):
        return struct.unpack("<H", self.read(addr, 2))[0]

    def r32(self, addr):
        return struct.unpack("<I", self.read(addr, 4))[0]

    def w8(self, addr, v):
        self.uc.mem_write(addr, bytes([v]))

    def w32(self, addr, v):
        self.uc.mem_write(addr, struct.pack("<I", v))

    def call(self, func, *args, regs=None, until=RET):
        uc = self.uc
        for reg, val in zip((UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2,
                             UC_ARM_REG_R3), args):
            uc.reg_write(reg, val)
        for reg, val in (regs or {}).items():
            uc.reg_write(reg, val)
        uc.reg_write(UC_ARM_REG_SP, STACK_TOP)
        uc.reg_write(UC_ARM_REG_LR, RET | 1)
        self.calls = []
        try:
            uc.emu_start(func | 1, until, count=200000)
        except UcError as e:
            raise SystemExit(f"emulation error {e} at pc={uc.reg_read(UC_ARM_REG_PC):#x}")
        return uc.reg_read(UC_ARM_REG_R0)

    def names(self):
        return [c[0] for c in self.calls]

    # -- fixtures -----------------------------------------------------------
    def cfg(self, channel=1, instant=0, bank_select=0, bank_reg=0):
        self.uc.mem_write(PCRAM, bytes([0x50, channel, instant, bank_select]))
        self.uc.mem_write(PCRAM + 4, b"\x00\x00")
        self.w8(PCRAM + 6, bank_reg)
        self.w8(PCRAM + 7, 0)

    def pending(self):
        return self.r16(PCRAM + 4)

    def world(self, sel_bank=0, cur_bank=0, cur_pat=0, queued=0):
        self.w8(PROJECT + 0x14, cur_bank)
        self.w8(PROJECT + 0x15, cur_pat)
        self.w8(PROJECT + 0x16, sel_bank)
        self.w32(SEQ_PTR, FAKE_SEQ)
        self.w32(FAKE_SEQ + 0x0C, queued)
        self.w32(PAGE_PTR, FAKE_PAGE)
        self.w32(FAKE_PAGE + 0x0C, FAKE_PAGE)
        self.uc.mem_write(FAKE_PAGE + 0xBE, b"\x00\x00")


def feed_midi(fw, data):
    """Push raw MIDI bytes through the stock ring buffer + parser + dispatch."""
    uc = fw.uc
    ring = MIDI + 0x60
    uc.mem_write(ring, bytes(data))
    fw.w32(MIDI + 0x10, 0)            # consumer index
    fw.w32(MIDI + 0x14, len(data))    # producer index
    fw.w32(MIDI + 0x18, 0x40)         # ring size
    fw.w32(MIDI + 0x1C, ring)
    return fw.call(0x08034DB4, MIDI)  # EXTI0 handler body


def main():
    stock = Path(sys.argv[1]).read_bytes()
    built = sorted(Path(sys.argv[2]).glob("EBSEQ_V1_07_*.BIN"))
    if len(built) != 1:
        raise SystemExit("expected exactly one EBSEQ_V1_07_*.BIN in build dir")
    image = built[0].read_bytes()
    syms = {}
    for line in Path(sys.argv[2], "pc_patch.map").read_text().splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].startswith("0x") and parts[1].startswith("pc_"):
            syms[parts[1]] = int(parts[0], 16)

    print("Image")
    crc = zlib.crc32(image)
    check(built[0].name == f"EBSEQ_V1_07_{crc:08X}.BIN", "file name carries CRC32 of contents")
    check(image[:len(stock) - 0x20] != stock[:len(stock) - 0x20], "image differs from stock")
    vec = struct.unpack("<II", image[:8])
    check(vec == struct.unpack("<II", stock[:8]), "vector table unchanged")

    print("Boot settings load (pc_settings_init via hook at 0x0801EA26)")
    fw = Fw(image)
    fw.uc.mem_write(PCRAM, b"\xAA" * 8)
    fw.load_ret = 0x14
    fw.call(0x0801EA26, 0x2000201C, until=0x0801EA2A)
    check(fw.read(PCRAM, 8) == bytes([0x50, 0, 0, 0, 0, 0, 0, 0]),
          "empty EEPROM slot -> defaults (PC off, quantized, no bank select)")
    check(fw.names() == ["setting_load", "setting_load"], "stock slot 7 still loaded first")
    fw.load_ret = 0
    fw.eeprom[0x500] = bytes([0x50, 10, 1, 1])
    fw.call(0x0801EA26, 0x2000201C, until=0x0801EA2A)
    check(fw.read(PCRAM, 4) == bytes([0x50, 10, 1, 1]), "stored settings restored")
    fw.eeprom[0x500] = bytes([0x50, 40, 1, 1])
    fw.call(0x0801EA26, 0x2000201C, until=0x0801EA2A)
    check(fw.read(PCRAM, 4) == bytes([0x50, 0, 0, 0]), "corrupt channel -> defaults")
    desc = fw.calls[1][1][0]
    check(struct.unpack("<HHI", fw.read(desc, 8)) == (0x500, 4, PCRAM),
          "EEPROM descriptor = slot 0x500, 4 bytes")

    print("MIDI input through the stock UART ring buffer, parser and dispatcher")
    fw = Fw(image)
    check(fw.r32(0x20001FF8) == syms["pc_midi_dispatch"] | 1,
          "MIDI port callback now points at pc_midi_dispatch")
    fw.cfg(channel=0)
    feed_midi(fw, [0xC0, 3])
    check(fw.pending() == 0, "PC ignored while PC Channel is OFF")
    fw.cfg(channel=10)
    feed_midi(fw, [0xC0, 3])
    check(fw.pending() == 0, "PC on channel 1 ignored when listening on 10")
    feed_midi(fw, [0xC9, 3])
    check(fw.pending() == 0x8000 | 0x7F00 | 3, "PC 3 on channel 10 -> pattern 4, current bank")
    fw.cfg(channel=10)
    feed_midi(fw, [0xC9, 16])
    check(fw.pending() == 0, "PC 16 (out of range) ignored")
    feed_midi(fw, [0xC9, 127])
    check(fw.pending() == 0, "PC 127 (out of range) ignored")
    feed_midi(fw, [0xC9, 15])
    check(fw.pending() == 0x8000 | 0x7F00 | 15, "PC 15 -> pattern 16")
    fw.cfg(channel=10)
    feed_midi(fw, [0xC9, 2, 7])
    check(fw.pending() == 0x8000 | 0x7F00 | 7, "running status: second PC wins")
    fw.cfg(channel=10)
    feed_midi(fw, [0x99, 60, 100, 0xF8, 0xC9, 5])
    check(fw.pending() == 0x8000 | 0x7F00 | 5, "PC after note + clock still parsed")
    check("midi_clock_tick" in fw.names(), "clock byte still reaches the stock clock handler")
    fw.cfg(channel=10)
    feed_midi(fw, [0xB9, 0, 4, 0xC9, 1])
    check(fw.pending() == 0x8000 | 0x7F00 | 1, "Bank Select OFF: CC#0 ignored")
    fw.cfg(channel=10, bank_select=1)
    feed_midi(fw, [0xB9, 0, 4, 0xC9, 1])
    check(fw.pending() == 0x8000 | 0x0400 | 1, "Bank Select ON: CC#0=4 then PC 1 -> bank 5")
    fw.w8(PCRAM + 4, 0); fw.w8(PCRAM + 5, 0)
    feed_midi(fw, [0xC9, 2])
    check(fw.pending() == 0x8000 | 0x0400 | 2, "bank latch persists for later PCs")
    fw.w8(PCRAM + 4, 0); fw.w8(PCRAM + 5, 0)
    feed_midi(fw, [0xB9, 0, 20, 0xC9, 2])
    check(fw.pending() == 0, "CC#0=20 (no such bank) blocks PCs until a valid bank arrives")
    feed_midi(fw, [0xB9, 0, 15, 0xC9, 2])
    check(fw.pending() == 0x8000 | 0x0F00 | 2, "CC#0=15 -> bank 16")
    fw.cfg(channel=10, bank_select=1)
    feed_midi(fw, [0xB8, 0, 4, 0xC9, 1])
    check(fw.pending() == 0x8000 | 0x7F00 | 1, "CC#0 on another channel ignored")

    print("Dispatcher pass-through")
    fw = Fw(image, stub_dispatch=True)
    fw.cfg(channel=10)
    for msg in ([0xC9, 3, 0, 1], [0x90, 60, 100, 2], [0xF8, 0, 0, 1]):
        fw.uc.mem_write(BUF, bytes(msg))
        fw.call(syms["pc_midi_dispatch"], BUF)
        check(fw.calls and fw.calls[-1][0] == "midi_dispatch" and fw.calls[-1][1][0] == BUF,
              f"message {msg[0]:#04x} forwarded to the stock dispatcher unchanged")

    print("Main loop (pc_main_poll via hook at 0x08020114)")
    def run_poll(req, **world):
        fw = Fw(image)
        fw.cfg(channel=10, instant=world.pop("instant", 0))
        fw.state = world.pop("state", 1)
        fw.exists = world.pop("exists", {(0, 4), (2, 6)} | {(2, i) for i in (0, 3)})
        fw.world(**world)
        fw.uc.mem_write(PCRAM + 4, struct.pack("<H", req))
        fw.call(0x08020114, until=0x08020118)
        return fw

    fw = run_poll(0)
    check(fw.names() == ["song_poll", "lock", "unlock"], "no request: only stock song poll + atomic read")

    fw = run_poll(0x8000 | 0x7F00 | 4, state=1)
    n = fw.names()
    check(n == ["song_poll", "lock", "unlock", "pattern_exists", "song_stop", "lock",
                "seq_cue", "unlock", "pattern_load", "idle_buffer", "seq_cue",
                "seq_state", "widget_next"],
          "playing + quantized: load, queue, no immediate swap")
    cue0 = fw.calls[n.index("seq_cue")]
    check(cue0[1][0] == 0 and cue0[2] == 1, "idle buffer un-queued under IRQ lock before the SD load")
    check(fw.calls[n.index("pattern_load")][1][0] == 4, "loads pattern index 4")
    check(fw.calls[n.index("seq_cue", 7)][1][0] == IDLE_BUF, "queues the stock NEXT buffer")
    w = fw.calls[n.index("widget_next")][1]
    check(w == (FAKE_PAGE + 0x14, 0, 4, 0), "PATTERN page shows queued pattern (bank 1, pattern 5)")
    check(fw.r16(0x20008718 + 0x80) == 0, "pattern chain cleared like a panel press")
    check(fw.pending() == 0, "request consumed")

    fw = run_poll(0x8000 | 0x7F00 | 4, state=0)
    check(fw.names()[-3:] == ["seq_swap", "seq_rebind", "commit_select"],
          "stopped: switches immediately (panel behaviour)")
    check(fw.calls[fw.names().index("widget_next")][1][3] == 1, "widget told change is immediate")
    fw = run_poll(0x8000 | 0x7F00 | 4, state=2)
    check(fw.names()[-1] == "commit_select", "paused (MIDI Stop): switches immediately")
    fw = run_poll(0x8000 | 0x7F00 | 4, state=1, instant=1)
    check(fw.names()[-3:] == ["seq_swap", "seq_rebind", "commit_select"],
          "PC Quantize OFF: switches immediately while playing")

    fw = run_poll(0x8000 | 0x7F00 | 9)
    check(fw.names() == ["song_poll", "lock", "unlock", "pattern_exists"],
          "empty pattern slot ignored")
    fw = run_poll(0x8000 | 0x7F00 | 4, cur_bank=0, cur_pat=4)
    check(fw.names()[-1] == "pattern_exists", "PC for the playing pattern ignored (keeps unsaved edits)")
    fw = run_poll(0x8000 | 0x7F00 | 4, cur_bank=0, cur_pat=4, queued=IDLE_BUF)
    check("pattern_load" in fw.names(), "...but re-selects it if another change is queued")

    fw = run_poll(0x8000 | 0x0200 | 6, sel_bank=0)
    n = fw.names()
    check(n[3:6] == ["pattern_exists", "bank_select", "song_stop"], "bank change selects bank first")
    check(fw.calls[n.index("bank_select")][1][0] == 2, "selects bank 3")
    check(fw.r16(FAKE_PAGE + 0xBE) == (1 << 0) | (1 << 3) | (1 << 6),
          "PATTERN page saved-slot LEDs refreshed for the new bank")
    fw = run_poll(0x8000 | 0x0200 | 7, sel_bank=0)
    check("bank_select" not in fw.names(), "empty slot in another bank does not switch bank")
    fw = run_poll(0x8000 | 0x7F00 | 6, sel_bank=2)
    check(fw.calls[3][1][:2] == (2, 6), "current-bank request uses the selected bank")

    print("MIDI settings page, MISC tab")
    fw = Fw(image)
    fw.cfg(channel=0)

    def row(r):
        fw.uc.mem_write(BUF, b"\xEE" * 32)
        fw.call(0x0801914C, r, regs={UC_ARM_REG_R5: BUF}, until=0x08019150)
        return fw.read(BUF, 32).split(b"\0")[0].decode()

    check(row(1) == "PC Channel      OFF", f"row 1 renders {row(1)!r}")
    fw.cfg(channel=7)
    check(row(1) == "PC Channel        7", f"row 1 renders {row(1)!r}")
    check(row(2) == "PC Quantize      ON", f"row 2 renders {row(2)!r}")
    check(row(3) == "Bank Select     OFF", f"row 3 renders {row(3)!r}")
    check(row(4) == "", "row 4 stays empty")

    def edit(r, delta):
        fw.uc.mem_write(BUF, struct.pack("<BBh", 0, 0x20, delta))
        until = 0x0801943C if r == 0 else 0x080193D4
        fw.call(0x08019438, regs={UC_ARM_REG_R1: r, UC_ARM_REG_R5: BUF,
                                  UC_ARM_REG_R3: 0}, until=until)
        return fw.uc.reg_read(UC_ARM_REG_PC), fw.uc.reg_read(UC_ARM_REG_R3)

    fw.cfg(channel=0)
    pc_, r3 = edit(1, 1)
    check(fw.r8(PCRAM + 1) == 1 and fw.r8(PCRAM + 7) == 1, "encoder +1: channel OFF -> 1, marked dirty")
    check(pc_ == 0x080193D4 and r3 == 0, "returns through the stock epilogue")
    edit(1, 40)
    check(fw.r8(PCRAM + 1) == 16, "channel clamps at 16")
    edit(1, -100)
    check(fw.r8(PCRAM + 1) == 0, "channel clamps at OFF")
    edit(2, -1)
    check(fw.r8(PCRAM + 2) == 1, "Quantize turned OFF")
    edit(2, 1)
    check(fw.r8(PCRAM + 2) == 0, "Quantize turned ON")
    edit(3, 1)
    check(fw.r8(PCRAM + 3) == 1, "Bank Select turned ON")
    pc_, _ = edit(0, 1)
    check(pc_ == 0x0801943C, "row 0 still goes to the stock Cont Clock code")
    check(fw.read(0x0803ABA4, 5) == bytes([4, 4, 4, 4, 4]), "MISC tab now lists 4 rows")

    fw.w8(PCRAM + 7, 1)
    fw.call(0x0801959A, until=0x0801959E)
    check(fw.names() == ["devcfg_save", "setting_save"], "page exit saves stock config, then patch slot")
    check(fw.eeprom.get(0x500) == fw.read(PCRAM, 4), "patch settings written to EEPROM slot 0x500")
    fw.call(0x0801959A, until=0x0801959E)
    check(fw.names() == ["devcfg_save"], "no EEPROM write when nothing changed")

    print(f"\n{'All tests passed' if not failures else f'{failures} FAILED'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
