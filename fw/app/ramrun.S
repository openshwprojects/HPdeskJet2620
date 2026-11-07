/*
 * Hand-over to an application image staged in RAM (ramrun.c). This code is
 * copied away from the running application before it runs, because it
 * overwrites that application: it must stay position independent and touch
 * no stack and no data outside its arguments.
 *
 *   r0 = source (uncached alias of the staged payload)
 *   r1 = destination (uncached alias of the load address)
 *   r2 = length in bytes, multiple of 4
 *   r3 = entry address (executable alias)
 *
 * The caller has cleaned everything it still needs out of the D-cache, so
 * the whole D-cache is then invalidated (the old application's lines must
 * never be written back over the new one), the I-cache too, and the new
 * application starts with r0 = BOOTINFO_ADDR like after the bootloader.
 */
        .syntax unified
        .arm
        .text
        .balign 4
        .global ramrun_tramp_start
        .global ramrun_tramp_end
ramrun_tramp_start:
        msr     cpsr_c, #0xD3               @ SVC, IRQ and FIQ masked
1:      subs    r2, r2, #4
        ldrge   r12, [r0], #4
        strge   r12, [r1], #4
        bgt     1b
        mov     r12, #0
        mcr     p15, 0, r12, c7, c10, 4     @ drain write buffer
        mcr     p15, 0, r12, c7, c6, 0      @ invalidate the whole D-cache
        mcr     p15, 0, r12, c7, c5, 0      @ invalidate the whole I-cache
        ldr     r0, 2f                      @ BOOTINFO_ADDR (literal copied along)
        bx      r3
        .balign 4
2:      .word   0x2001F000
ramrun_tramp_end:
