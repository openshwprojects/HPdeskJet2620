/*
 * First hardware IRQ for the custom firmware.
 * ARM state, big-endian, ARM946E-S. Pairs with irq.c.
 *
 * Evidence (see irq.c header):
 *   - ITCM word 0x18 = E59FF018 "ldr pc,[pc,#0x18]" -> handler address is the
 *     LITERAL at 0x38 (.reset section; stage-1 writes it via A000FCC0).
 *   - At handoff stage-1 A000FC80 rewrites all 8 literals from table A0012970,
 *     so literal[0x38] = 0x00000018 (IRQ vector jumps to itself = hang).
 *   - MPU region 1 = 0x00000000..0x00001FFF (8 KiB), data AP nibble 6
 *     (privileged read-only). HP itself turns the whole MPU off to write the
 *     literal (A000FA6C / A000FBB4). We only flip region 1's data AP to 1
 *     (privileged RW) for the two instructions that swap the literal.
 */
        .syntax unified
        .arm
        .text

/* uint32_t irq_vec_swap(uint32_t new_literal, uint32_t *readback)
   Writes new_literal to 0x38, stores the read-back value in *readback and
   returns the previous literal. Runs with IRQ+FIQ masked; restores the CPSR
   control bits and the exact c5 value before returning.
   UNCERTAIN (the one write that has never been done by our firmware): that the
   MPU region-1 permission is the only thing blocking a privileged data write
   to ITCM 0x38. HP's A000FCC0 writes the same word with the MPU off, so the
   location itself is writable RAM. Worst case if wrong: a data abort, whose
   vector is also a self-loop -> hang -> power cycle (nothing persistent). */
        .global irq_vec_swap
        .type irq_vec_swap, %function
irq_vec_swap:
        push    {r4, r5}
        mrs     r3, cpsr
        orr     r2, r3, #0xC0
        msr     cpsr_c, r2                  @ I+F masked
        mrc     p15, 0, r12, c5, c0, 2      @ extended data access permissions
        bic     r2, r12, #0xF0              @ region 1 nibble (bits 7:4)
        orr     r2, r2, #0x10               @ AP = 1: privileged RW, user none
        mcr     p15, 0, r2, c5, c0, 2
        mov     r2, #0x38
        ldr     r4, [r2]                    @ old literal
        str     r0, [r2]                    @ new literal (ITCM: never cached)
        ldr     r5, [r2]                    @ read back
        mcr     p15, 0, r12, c5, c0, 2      @ restore exact permissions (RO again)
        mov     r2, #0
        mcr     p15, 0, r2, c7, c10, 4      @ drain write buffer
        msr     cpsr_c, r3
        str     r5, [r1]
        mov     r0, r4
        pop     {r4, r5}
        bx      lr
        .size irq_vec_swap, . - irq_vec_swap

/* void irq_set_stack(uint32_t top): set banked SP_irq (8-byte aligned top). */
        .global irq_set_stack
        .type irq_set_stack, %function
irq_set_stack:
        mrs     r1, cpsr
        msr     cpsr_c, #0xD2               @ IRQ mode, I+F masked
        mov     sp, r0
        msr     cpsr_c, r1                  @ back to the caller's mode and mask
        bx      lr
        .size irq_set_stack, . - irq_set_stack

/* uint32_t irq_cpsr_disable(void): set I+F, return the previous CPSR. */
        .global irq_cpsr_disable
        .type irq_cpsr_disable, %function
irq_cpsr_disable:
        mrs     r0, cpsr
        orr     r1, r0, #0xC0
        msr     cpsr_c, r1
        bx      lr
        .size irq_cpsr_disable, . - irq_cpsr_disable

/* void irq_cpsr_enable_irq_only(void): clear I, keep F set (FIQ vector is a
   self-loop too, so F must NEVER be cleared). */
        .global irq_cpsr_enable_irq_only
        .type irq_cpsr_enable_irq_only, %function
irq_cpsr_enable_irq_only:
        mrs     r0, cpsr
        orr     r0, r0, #0x40
        bic     r0, r0, #0x80
        msr     cpsr_c, r0
        bx      lr
        .size irq_cpsr_enable_irq_only, . - irq_cpsr_enable_irq_only

/* IRQ entry. Reached via 0x18 "ldr pc,[pc,#0x18]" -> literal 0x38 -> here.
   Non-nested: I stays set for the whole handler (CPU sets it on entry).
   6 words pushed -> SP_irq stays 8-byte aligned for the C call. */
        .global irq_entry
        .type irq_entry, %function
        .balign 4
irq_entry:
        sub     lr, lr, #4                  @ IRQ LR = interrupted PC + 4
        stmfd   sp!, {r0-r3, r12, lr}
        bl      irq_dispatch_c              @ C, -marm build (AAPCS saves r4-r11)
        ldmfd   sp!, {r0-r3, r12, pc}^      @ return + CPSR <- SPSR_irq
        .size irq_entry, . - irq_entry
