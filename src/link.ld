/*
 * Patch code is appended to the end of the stock image. The stock file
 * occupies 0x0800C000-0x08043F3B; patch code starts at the next 16-byte
 * boundary. Patch RAM is a 32-byte block carved from the start of the stock
 * heap (stock .bss ends at 0x2001ED08; the patcher moves the bss end and the
 * _sbrk heap base to 0x2001ED28 so the block is zeroed at boot).
 */
MEMORY {
  PATCH_FLASH (rx)  : ORIGIN = 0x08043F40, LENGTH = 16K
  PATCH_RAM   (rw)  : ORIGIN = 0x2001ED08, LENGTH = 32
}

SECTIONS {
  .text : {
    *(.text*)
    *(.rodata*)
    . = ALIGN(4);
  } > PATCH_FLASH

  .pcram (NOLOAD) : { *(.pcram) } > PATCH_RAM

  /* Anything here would need an initializer the stock startup does not run. */
  .data : { *(.data*) } > PATCH_RAM
  .bss  : { *(.bss*) *(COMMON) } > PATCH_RAM

  /DISCARD/ : { *(.ARM.exidx*) *(.ARM.extab*) *(.comment) *(.ARM.attributes) }
}
