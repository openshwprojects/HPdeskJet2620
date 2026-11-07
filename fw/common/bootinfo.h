/* Boot information block at BOOTINFO_ADDR, written by BL, read by the app. */
#ifndef FW_BOOTINFO_H
#define FW_BOOTINFO_H
#include <stdint.h>
#include "layout.h"
#include "cache.h"

#define BOOTINFO_MAGIC   0x42494E46U   /* "BINF" */
#define BL_STAY_MAGIC    0x5354AF59U   /* passed in r0 to the BL entry: stay in the BL shell */
#define BOOTINFO_SLOT_RAM 0xFFU        /* active_slot: image was uploaded and run from RAM */

enum boot_reason { BOOT_REASON_AUTOBOOT = 0, BOOT_REASON_COMMAND = 1, BOOT_REASON_RAM = 2 };

typedef struct {
    uint32_t magic;
    uint32_t bl_version;
    uint32_t active_slot;     /* 0 = A, 1 = B, BOOTINFO_SLOT_RAM */
    uint32_t app_version;
    uint32_t app_sequence;
    uint32_t boot_count;      /* incremented by BL on every app launch (RAM only) */
    uint32_t boot_reason;     /* enum boot_reason */
    uint32_t ticks_per_ms;    /* system counter rate measured by BL against the UART, 0 = not measured */
} bootinfo_t;

#define BOOTINFO ((volatile bootinfo_t *)BOOTINFO_ADDR)

/*
 * Boot-attempt record for the crash-loop guard. The bootloader increments it
 * before every automatic launch and the application clears it once its shell
 * is up (boot_attempts_mark_alive). DRAM keeps its contents through a
 * watchdog reset, so a valid-but-crashing application shows up as a growing
 * count; a cold power-on leaves random data, which the complement check
 * rejects (reads as zero). Written through the write-back alias, so it is
 * cleaned to DRAM explicitly.
 */
#define BOOT_ATTEMPTS_ADDR   (BOOTINFO_ADDR + 0x80)
#define BOOT_ATTEMPTS_MAGIC  0x41545054U   /* "ATPT" */

typedef struct {
    uint32_t magic;
    uint32_t attempts;
    uint32_t attempts_inv;   /* ~attempts */
} boot_attempts_t;

#define BOOT_ATTEMPTS ((volatile boot_attempts_t *)BOOT_ATTEMPTS_ADDR)

static inline uint32_t boot_attempts_read(void)
{
    volatile boot_attempts_t *b = BOOT_ATTEMPTS;
    uint32_t n = b->attempts;
    return (b->magic == BOOT_ATTEMPTS_MAGIC && b->attempts_inv == ~n) ? n : 0;
}

static inline void boot_attempts_write(uint32_t n)
{
    volatile boot_attempts_t *b = BOOT_ATTEMPTS;
    b->magic = BOOT_ATTEMPTS_MAGIC;
    b->attempts = n;
    b->attempts_inv = ~n;
    dcache_clean_range(BOOT_ATTEMPTS_ADDR, BOOT_ATTEMPTS_ADDR + sizeof(boot_attempts_t));
}

/* Called by an application once its shell is reachable. */
static inline void boot_attempts_mark_alive(void) { boot_attempts_write(0); }

#endif
