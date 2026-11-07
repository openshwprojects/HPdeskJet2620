/*
 * ARM946E-S cache maintenance. HP stage-1 leaves the I-cache and D-cache
 * enabled (SCTLR 0x510FD), so code copied into DRAM must be cleaned out of
 * the D-cache and the I-cache invalidated before it is executed, and flash
 * contents changed through the SPI controller must be invalidated from the
 * D-cache before the memory-mapped window is read again. The emulator has no
 * cache model and accepts these operations as no-ops.
 */
#ifndef FW_CACHE_H
#define FW_CACHE_H
#include <stdint.h>

#define CACHE_LINE 32U

static inline void dcache_clean_range(uint32_t start, uint32_t end)
{
    uint32_t a;
    for (a = start & ~(CACHE_LINE - 1); a < end; a += CACHE_LINE)
        __asm__ volatile ("mcr p15, 0, %0, c7, c10, 1" :: "r"(a) : "memory");
    __asm__ volatile ("mcr p15, 0, %0, c7, c10, 4" :: "r"(0) : "memory");  /* drain write buffer */
}

static inline void dcache_invalidate_range(uint32_t start, uint32_t end)
{
    uint32_t a;
    for (a = start & ~(CACHE_LINE - 1); a < end; a += CACHE_LINE)
        __asm__ volatile ("mcr p15, 0, %0, c7, c6, 1" :: "r"(a) : "memory");
}

static inline void icache_invalidate_all(void)
{
    __asm__ volatile ("mcr p15, 0, %0, c7, c5, 0" :: "r"(0) : "memory");
}

/* Make code that was written through `rw_start..rw_end` executable at its
   0xA0... alias `exec_start..exec_end`. */
static inline void cache_sync_code(uint32_t rw_start, uint32_t rw_end,
                                   uint32_t exec_start, uint32_t exec_end)
{
    dcache_clean_range(rw_start, rw_end);
    dcache_invalidate_range(exec_start, exec_end);
    icache_invalidate_all();
}

static inline uint32_t cp15_read_sctlr(void)
{
    uint32_t v;
    __asm__ volatile ("mrc p15, 0, %0, c1, c0, 0" : "=r"(v));
    return v;
}

static inline uint32_t cp15_read_cache_type(void)
{
    uint32_t v;
    __asm__ volatile ("mrc p15, 0, %0, c0, c0, 1" : "=r"(v));
    return v;
}

static inline uint32_t cpsr_read(void)
{
    uint32_t v;
    __asm__ volatile ("mrs %0, cpsr" : "=r"(v));
    return v;
}

#endif
