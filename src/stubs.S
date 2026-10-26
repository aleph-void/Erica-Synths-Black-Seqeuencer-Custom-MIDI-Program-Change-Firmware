/*
 * Register-level glue for hooks placed inside stock functions, where the
 * surrounding code keeps its own state in callee-saved registers.
 */
    .syntax unified
    .thumb
    .text

/*
 * MIDI settings row formatter, MISC tab, rows 1..3.
 * Reached by BL from 0x0801914C (replacing "movs r3,#0; strb r3,[r5]").
 * In:  r0 = row index, r5 = output buffer. Returns to 0x08019150.
 */
    .global pc_fmt_stub
    .thumb_func
pc_fmt_stub:
    mov     r1, r5
    b       pc_fmt_row

/*
 * MIDI settings encoder edit, MISC tab.
 * Reached by B.W from 0x08019438 (replacing "cmp r1,#0; bne 0x080193D4").
 * In:  r1 = selected row, r5 = encoder event (int16 delta at +2), r3 = 0.
 * Row 0 continues into the stock Cont Clock code at 0x0801943C; other rows
 * return through the stock epilogue at 0x080193D4 with r3 = 0.
 */
    .global pc_edit_stub
    .thumb_func
pc_edit_stub:
    cbz     r1, 1f
    mov     r0, r1
    ldrsh.w r1, [r5, #2]
    bl      pc_edit_row
    movs    r3, #0
    ldr     r2, =0x080193D5
    bx      r2
1:
    ldr     r2, =0x0801943D
    bx      r2

    .ltorg
