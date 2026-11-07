/*
 * Common entry stub for BL and application (ARM state, big-endian).
 *
 * The caller (HP stage-1, the bootloader, or the application) branches here
 * in a privileged mode with r0 holding an argument. We:
 *   1. force SVC mode with IRQ/FIQ masked (no interrupts are used),
 *   2. set the stack (linker symbol __stack_top, in the writable alias),
 *   3. copy .data from its load address inside the code image to its VMA,
 *   4. clear .bss,
 *   5. call main(r0); main never returns, but spin if it does.
 */
        .syntax unified
        .arm
        .section .text.entry, "ax"
        .global _start
        .type _start, %function
_start:
        msr     cpsr_c, #0xD3           @ SVC mode, I and F masked (first: no window with interrupts open)
        mov     r5, r0                  @ preserve the entry argument
        ldr     sp, =__stack_top

        ldr     r1, =__data_lma
        ldr     r2, =__data_start
        ldr     r3, =__data_end
1:      cmp     r2, r3
        ldrlo   r4, [r1], #4
        strlo   r4, [r2], #4
        blo     1b

        ldr     r2, =__bss_start
        ldr     r3, =__bss_end
        mov     r4, #0
2:      cmp     r2, r3
        strlo   r4, [r2], #4
        blo     2b

        mov     r0, r5
        bl      main
3:      b       3b
        .size _start, . - _start

/* Branch to code with an argument (used to enter the app or re-enter BL). */
        .text
        .global jump_to
        .type jump_to, %function
jump_to:                                @ void jump_to(uint32_t entry, uint32_t arg)
        mov     r2, r0
        mov     r0, r1
        bx      r2
        .size jump_to, . - jump_to
