/*
 * rtos_switch.S - Cooperative context switch and ARM EABI division wrappers
 * for ARM946E-S (ARMv5TE big-endian)
 */
    .section .text.rtos_switch_context, "ax", %progbits
    .arm
    .align 2
    .global rtos_switch_context
    .type rtos_switch_context, %function

rtos_switch_context:
    stmia   r0, {r4-r11}
    str     sp, [r0, #32]
    str     lr, [r0, #36]

    ldmia   r1, {r4-r11}
    ldr     sp, [r1, #32]
    ldr     pc, [r1, #36]

    .section .text.aeabi_div, "ax", %progbits
    .arm
    .align 2
    .global __aeabi_uidiv
    .global __aeabi_uidivmod
    .type __aeabi_uidiv, %function
    .type __aeabi_uidivmod, %function
__aeabi_uidiv:
__aeabi_uidivmod:
    push    {r4, r5, lr}
    mov     r4, r0
    mov     r5, r1
    bl      __udivsi3
    mul     r2, r0, r5
    sub     r1, r4, r2
    pop     {r4, r5, pc}

    .global __aeabi_idiv
    .global __aeabi_idivmod
    .type __aeabi_idiv, %function
    .type __aeabi_idivmod, %function
__aeabi_idiv:
__aeabi_idivmod:
    push    {r4, r5, lr}
    mov     r4, r0
    mov     r5, r1
    bl      __divsi3
    mul     r2, r0, r5
    sub     r1, r4, r2
    pop     {r4, r5, pc}

    .global __aeabi_uldivmod
    .type __aeabi_uldivmod, %function
__aeabi_uldivmod:
    push    {r4-r7, lr}
    sub     sp, sp, #4
    mov     r4, r0
    mov     r5, r1
    mov     r6, r2
    mov     r7, r3
    bl      __udivdi3
    push    {r0, r1}
    mov     r0, r4
    mov     r1, r5
    mov     r2, r6
    mov     r3, r7
    bl      __umoddi3
    mov     r2, r0
    mov     r3, r1
    pop     {r0, r1}
    add     sp, sp, #4
    pop     {r4-r7, pc}

    .global __aeabi_ldivmod
    .type __aeabi_ldivmod, %function
__aeabi_ldivmod:
    push    {r4-r7, lr}
    sub     sp, sp, #4
    mov     r4, r0
    mov     r5, r1
    mov     r6, r2
    mov     r7, r3
    bl      __divdi3
    push    {r0, r1}
    mov     r0, r4
    mov     r1, r5
    mov     r2, r6
    mov     r3, r7
    bl      __moddi3
    mov     r2, r0
    mov     r3, r1
    pop     {r0, r1}
    add     sp, sp, #4
    pop     {r4-r7, pc}
