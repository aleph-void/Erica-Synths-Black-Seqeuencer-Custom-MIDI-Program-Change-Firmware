# Reverse-engineering notes: Black Sequencer firmware 1.06

Target: `EBSEQ_V1_06_7824E1DD.BIN` (229,180 bytes, CRC32 `7824E1DD`, which
matches the hex in the filename). All addresses below are absolute and use
that image. Function names are descriptive labels chosen here, not vendor
symbols.

Tools: `tools/re/disasm.py ADDR [N]` (annotated Thumb disassembly) and
`tools/re/xrefs.py lit|call|word|litr ...` (cross-references).

## Platform

- STM32F4 (the source path `../system/src/stm32f4-hal/...` is embedded).
  It has 107 vector entries and its initial SP is `0x20030000` (192 KiB
  SRAM), so it's an F427/F429-class part.
- Application load address is **`0x0800C000`**. It was confirmed by the
  reset handler at `0x08020290`, which copies `.data` from `0x08040CC4`
  (size `0x3278`) and ends exactly at the end of the file. The bootloader
  sits in `0x08000000-0x0800BFFF`.
- RAM: `.data` `0x20000000-0x20003278`, `.bss` `0x20003278-0x2001ED08`. The
  heap (`_sbrk` at `0x08012B14`) runs from `0x2001ED08` to `0x2002E000`, and
  the stack is the 8 KiB below `0x20030000`. CCM is unused.
- Update file selection: the application scans for `EBSEQ_*.BIN`
  (`0x0802AD20`). It parses the first two decimal numbers in the name as
  major/minor (`0x0802AC4C`) and offers "RUN BOOTLOADER?" only when the file
  is newer than its hard-coded 1.6 (`cmp r2,#6` at `0x0802AD4E`). The
  bootloader version comes from backup SRAM (`0x40024000` magic
  `0xC0FFEE00`).

## MIDI input path

| Address | Role |
|---|---|
| `0x0801E961` | USART1 IRQ (vector 53). It pushes RX bytes into the ring buffer of the MIDI port object `0x20001EEC` (`0x08034D78`) and pends EXTI0 (IRQ 6) as a software interrupt. |
| `0x0801E985` | EXTI0 IRQ: `0x08034DB4(port)` drains the ring. Each byte goes to the parser `0x08033700(port+0xB4, byte)`. A complete message calls `port+0x10C(port+0xC4)`, and SysEx calls `port+0x110`. |
| `0x08033700` | Byte parser. It handles running status and records data lengths: 2 for 8x/9x/Ax/Bx/Ex, **1 for Cx (Program Change)** and Dx, and handles F0-F7 / F8-FF. The message is `{status, data1, data2, length}` at `port+0xC4`. |
| `0x080294C4` | **Stock dispatcher** (`port+0x10C`, the `.data` initializer word at `0x08042CBC` → RAM `0x20001FF8`). It handles F8 clock (`0x08013970`), FA/FB/FC transport when the clock source (`0x20003337`) is MIDI (2), and per-track Note On/Off, Pitch Bend, CC 123 and the mod-input CC. **Program Change is parsed but dropped.** |

The patch replaces the `.data` initializer word, so the port calls
`pc_midi_dispatch`. That function records PC / CC#0 and then tail-calls
`0x080294C4` with the same argument.

## Sequencer engine

Engine object at `0x20000044`; a pointer to it is kept at `0x20005A8C`.

| Offset | Field |
|---|---|
| `+0` / `+1` / `+2` | run state (0 stopped, **1 playing**, 2 paused), previous state, sub-state |
| `+3` | track count (4) |
| `+4` | track array (`0x200000A0`, stride `0x208`) |
| `+8` | playing pattern buffer |
| `+0x0C` | **queued (next) pattern buffer**, 0 = none |
| `+0x14` | master step counter |
| `+0x18` | instant-change flag |
| `+0x1C` / `+0x20` | callbacks: master-length wrap (`0x08013065`) / pattern changed (`0x08013045`, prints "Pattern changed!") |

The two pattern buffers are `"MAIN"` at `0x2000000C` and `"NEXT"` at
`0x20000028`. Each holds track data at `+0x18`, stride `0x8F4`.

| Address | Routine |
|---|---|
| `0x08031B04` | `seq_state()`: returns `+0` |
| `0x08031B68` / `0x08031B74` | get playing buffer / **`seq_cue(buf)`** (writes `+0x0C`) |
| `0x08031B80` | `seq_swap(1)`: if a buffer is queued, make it the playing buffer and rebind the tracks |
| `0x08031BDC` | per-track rebind after a swap |
| `0x08031C54` | master tick. When the master length wraps (`0x08031A34`) it swaps in the queued buffer, calls `+0x20` and then `+0x1C`. **This is the quantized change.** |
| `0x08031AAC/AC0/AD8/AEC` | stop / pause / play / resume |
| `0x0801370C` | `idle_buffer()`: whichever of MAIN/NEXT isn't playing |

## Patterns, banks, project state

Project state is at `0x2001E274`: name string at `+0`, `+0x14` playing bank,
`+0x15` playing pattern, `+0x16` selected bank, `+0x17` selected pattern.
Files are `PROJECTS/<name>/<bank 00-15>/<pattern 00-15>.SEQ`.

| Address | Routine |
|---|---|
| `0x0802C0A0` | `pattern_load(p)`: sets `+0x17`, reads the `.SEQ` of the selected bank into `idle_buffer()`, or clears it if the file is missing. **SD access.** |
| `0x0802C1B0` | `pattern_exists(bank, p)`: `f_stat` on the `.SEQ` |
| `0x0802C030` | `bank_select(bank)`: sets `+0x16` and loads `BANK.DAT` (songs) |
| `0x0802C0F0` | `commit_select()`: playing ← selected (`+0x14/15` ← `+0x16/17`) |
| `0x080156EC` | `song_stop(0)` |
| `0x080156B8` | main-loop song poll. Its flag is set by the wrap callback, and it then loads the next song pattern from the main loop (the same deferred pattern the patch follows) |
| `0x08025B68` | PATTERN page widget: show next/current (`widget, bank, pattern, instant`) |
| `0x080336D0` / `0x080336E0` | nested IRQ lock / unlock (`cpsid i` + counter at `0x20006398`) |

**Panel pattern press** (`0x0801A1C0`, inside the PATTERN page event handler
`0x0801A020`; SHIFT = `0x2001E358[0x380]`):

```
chain.count (0x20008718+0x80) = 0
song_stop(0)
pattern_load(p)
seq_cue(idle_buffer())
instant = (seq_state() == PLAYING) ? shift : 1
widget_next(page+0x14, sel_bank, p, instant)
if instant: seq_swap(1); seq_rebind(); commit_select()
```

`pc_apply()` does the same steps. Its differences:

1. It checks `pattern_exists` first and ignores empty slots.
2. It ignores a request for the playing pattern when nothing is queued.
3. It un-queues (`seq_cue(0)`) under the IRQ lock before overwriting the idle
   buffer, so a master-length wrap during the SD read can't swap in a
   half-loaded pattern.
4. After a bank change it rebuilds the PATTERN page saved-slot mask
   (`page+0xBE`) the same way page entry does (`0x0801A3C2`).

The PATTERN page object is at `*(0x2000D874)`, and `page+0x0C` points back
to itself.

## Main loop

`main` (`0x080200BC`). Its loop calls `0x080156B8` at `0x08020114` every
iteration, then runs 50 Hz UI work. The patch redirects that `BL` to
`pc_main_poll`, which calls `0x080156B8` first.

## Settings storage

Settings live in an external I2C EEPROM (address `0xA0` or `0xA8`, 16-bit
memory addresses, 128-byte pages, HAL calls at `0x08034550` and
`0x08034580`). The descriptor format is `{u16 addr, u16 size, void *ram}`.
`0x0802CB34` loads a slot: it reads a 4-byte header `{1, 6, size}` and
rejects the slot with 0x14 if the stored size differs. `0x0802CB70` saves a
slot.

| Slot | EEPROM | Size | RAM |
|---|---|---|---|
| calibration | `0x000` | 296 | `0x2000255C` |
| — | `0x200` | 34 | — |
| device config | `0x180` | 29 | `0x20001DD4` (`+0x1A` = Cont Clock) |
| MIDI in | `0x280` | 24 | `0x20001ED4` |
| MIDI out | `0x300` | 20 | `0x20001E80` |
| | `0x380` | 24 | `0x2000CFA4` |
| | `0x400` | 9 | `0x20000068` |
| | `0x480` | 8 | `0x2000D7BC` |

Factory reset (`0x0802AE20`) zeroes `0` up to the size from `0x0801EC7C`
(`0x500`). The patch uses a new slot at **`0x500`** (4 bytes) and raises the
erase size to `0x580`. Growing an existing slot instead would fail its size
check, and the unit would boot with factory settings.

Loads happen in init (`0x0801E994`). The patch takes over the `BL` at
`0x0801EA26` (slot 7 load) to load its own slot afterwards.

## MIDI settings page

- Row text formatter: `0x08019018(row, buf, ctx)`. The tab is `ctx+0xAC`
  (0-4). On tab 4 (MISC), row 0 = "Cont Clock", and other rows wrote an
  empty string at `0x0801914C`.
- Encoder handler: `0x080193B8`. The encoder index selects a field, encoder
  index 4 changes the tab, and the list row is `ctx+0x61`. On tab 4 only
  encoder index 0 with row 0 is handled (`0x08019434-0x0801944A`).
- Rows per tab: the table at `0x0803ABA4` = `{4,4,4,4,1}`.
- Page enter `0x080195C4` snapshots device config (`0x0801EC40`). Page exit
  `0x08019590` saves it if changed (`0x0801EC58`) and saves the MIDI in/out
  slots.

## Patch sites

| Address | Stock | Patched | Purpose |
|---|---|---|---|
| `0x08042CBC` | `.word 0x080294C5` | `.word pc_midi_dispatch` | MIDI port callback (`.data` init) |
| `0x08020114` | `bl 0x080156B8` | `bl pc_main_poll` | main loop |
| `0x0801EA26` | `bl 0x0802CB34` | `bl pc_settings_init` | settings load at boot |
| `0x0801959A` | `bl 0x0801EC58` | `bl pc_midi_page_exit` | save on MIDI page exit |
| `0x0801914C` | `movs r3,#0; strb r3,[r5]` | `bl pc_fmt_stub` | MISC rows 1-3 text |
| `0x08019438` | `cmp r1,#0; bne 0x080193D4` | `b.w pc_edit_stub` | MISC rows 1-3 edit |
| `0x0803ABA8` | `01` | `04` | MISC tab row count |
| `0x0801EC7C` | `mov.w r0,#0x500` | `mov.w r0,#0x580` | factory erase covers the new slot |
| `0x0800C1C8`, `0x08012B54`, `0x0802030C` | `.word 0x2001ED08` | `.word 0x2001ED28` | end of `.bss` / heap base, which reserves 32 zeroed bytes for patch RAM |
| `0x0801796A`, `0x0801EDBA`, `0x08015FAC`, `0x0802AD4E` | `#6` | `#7` | version 1.06 → 1.07 (INFO, splash, update prompt, newer-than check) |
| `0x08043F40` | (end of file) | patch code | appended |

Patch RAM (`0x2001ED08`, 8 of the 32 reserved bytes):
`{magic 'P', channel, instant, bank_select}`, then `pending` (u16) holds
`0x8000 | bank<<8 | program` (bank 0x7F = current), then `bank_reg`, then
`dirty`.
