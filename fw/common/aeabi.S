/*
 * ARM EABI division helpers. The big-endian build cannot use the toolchain
 * libgcc (little-endian multilib only), so quotient/remainder pairs that must
 * come back in r0/r1 are assembled here on top of the C routines in libmini.c.
 */
        .syntax unified
        .arm
        .text

        .global __aeabi_uidivmod
        .type __aeabi_uidivmod, %function
__aeabi_uidivmod:                       @ r0 = n / d, r1 = n % d (unsigned)
        push    {r0, r1, r4, lr}        @ four registers keep the AAPCS 8-byte stack alignment
        bl      __aeabi_uidiv
        pop     {r1, r2, r4, lr}
        mul     r3, r0, r2
        sub     r1, r1, r3
        bx      lr
        .size __aeabi_uidivmod, . - __aeabi_uidivmod

        .global __aeabi_idivmod
        .type __aeabi_idivmod, %function
__aeabi_idivmod:                        @ r0 = n / d, r1 = n % d (signed)
        push    {r0, r1, r4, lr}
        bl      __aeabi_idiv
        pop     {r1, r2, r4, lr}
        mul     r3, r0, r2
        sub     r1, r1, r3
        bx      lr
        .size __aeabi_idivmod, . - __aeabi_idivmod
