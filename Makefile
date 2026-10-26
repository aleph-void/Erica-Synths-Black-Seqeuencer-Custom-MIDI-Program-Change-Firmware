CROSS  ?= arm-none-eabi-
CC      = $(CROSS)gcc
PYTHON ?= python3

STOCK  ?= EBSEQ_V1_06_7824E1DD.BIN
BUILD   = build

CFLAGS  = -mcpu=cortex-m4 -mthumb -mfloat-abi=hard -mfpu=fpv4-sp-d16 \
          -Os -g -ffreestanding -fno-builtin -nostdlib \
          -fno-common -Wall -Wextra -Werror
LDFLAGS = -T src/link.ld -nostdlib -Wl,--build-id=none \
          -Wl,-Map=$(BUILD)/pc_patch.map -Wl,--entry=pc_midi_dispatch

SRCS = src/pc_patch.c src/stubs.S

all: firmware

$(BUILD)/pc_patch.elf: $(SRCS) src/link.ld
	@mkdir -p $(BUILD)
	$(CC) $(CFLAGS) $(LDFLAGS) $(SRCS) -o $@

firmware: $(BUILD)/pc_patch.elf tools/patch_firmware.py
	rm -f $(BUILD)/EBSEQ_*.BIN
	$(PYTHON) tools/patch_firmware.py $(STOCK) $< $(BUILD)

test: firmware
	$(PYTHON) tools/test_patch.py $(STOCK) $(BUILD)

clean:
	rm -rf $(BUILD)

.PHONY: all firmware test clean
