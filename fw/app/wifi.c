/*
 * Wi-Fi module (Broadcom BCM43143 on SDIO).
 *
 * Powers the module (HP's logical GPIO 8 = AXON pin 12, set high by HP's
 * Wi-Fi driver at 0xa045de18), brings up the AXON SD host controller (standard
 * SDHCI registers at 0xBD0B0000, accessed as 32-bit words like HP's driver and
 * sdio.py), enumerates the SDIO card with CMD0/5/3/7 and reads its identity
 * with single-byte CMD52 transfers ("wifi probe"). "wifi fw" loads HP's chip
 * firmware and NVRAM through CMD53/SDMA, boots the chip and talks SDPCM/BCDC
 * to it; "wifi scan", "wifi join" (WPA2-PSK handled by the chip's own
 * supplicant) and "wifi ap" build on that, and data frames go to net.c.
 * No interrupts: everything is polled, from the shell's idle loop when idle.
 */
#include "hw.h"
#include "uart.h"
#include "timer.h"
#include "fmt.h"
#include "libmini.h"
#include "spi_flash.h"
#include "wifi.h"
#include "net.h"

#define SDHC_BASE        0xBD0B0000U
#define SDHC_BLK         REG32(SDHC_BASE + 0x04)   /* block count << 16 | block size */
#define SDHC_ARG         REG32(SDHC_BASE + 0x08)
#define SDHC_CMD         REG32(SDHC_BASE + 0x0C)   /* command << 16 | transfer mode; writing starts it */
#define SDHC_RESP0       REG32(SDHC_BASE + 0x10)
#define SDHC_PRESENT     REG32(SDHC_BASE + 0x24)
#define SDHC_HOSTCTL     REG32(SDHC_BASE + 0x28)   /* host ctl | power << 8 | gap << 16 | wakeup << 24 */
#define SDHC_CLOCK       REG32(SDHC_BASE + 0x2C)   /* clock ctl | timeout << 16 | reset << 24 */
#define SDHC_INTSTAT     REG32(SDHC_BASE + 0x30)   /* normal | error << 16, write 1 to clear */
#define SDHC_INTEN       REG32(SDHC_BASE + 0x34)
#define SDHC_SIGEN       REG32(SDHC_BASE + 0x38)
#define SDHC_CAPS        REG32(SDHC_BASE + 0x40)
#define SDHC_VERSION     REG32(SDHC_BASE + 0xFC)

#define CLK_INT_EN       0x0001U
#define CLK_INT_STABLE   0x0002U
#define CLK_SD_EN        0x0004U
#define RESET_ALL        0x01000000U
#define RESET_CMD        0x02000000U
#define INT_CMD_DONE     0x00000001U
#define INT_XFER_DONE    0x00000002U
#define INT_ERRORS       0xFFFF0000U

/* SDHCI command register: index << 8 | type bits (response, CRC, index check) */
#define CMD_NORESP(i)    ((uint32_t)(i) << 8)
#define CMD_R4(i)        (((uint32_t)(i) << 8) | 0x02U)   /* 48-bit, no CRC/index check */
#define CMD_R5R6(i)      (((uint32_t)(i) << 8) | 0x1AU)   /* 48-bit, CRC and index check */
#define CMD_R1B(i)       (((uint32_t)(i) << 8) | 0x1BU)   /* 48-bit busy */

/* SD block enable, as HP's sd_ctrl init does it (0xa03c6b46): release the
   block reset (helper 0xa01775ea with list {0x2f}: bit 15 of 0xBD061C0A),
   clear the wrapper control at 0xBD0B0400, then two handshakes (0xa03c6ad4):
   write 0x0100 | n to 0xBD0B0402 (resp. 0406) byte-wise, low byte first,
   for n = 0..255 until status 0xBD0B0404 bit 0 (resp. bit 1) is set, then
   clear bit 8 again. */
#define SD_RESET_REG     REG16(0xBD061C0AU)
#define SD_RESET_BIT     0x8000U
#define SDW_CTRL         REG16(0xBD0B0400U)
#define SDW_STATUS       REG16(0xBD0B0404U)

#define WIFI_POWER_PIN   12          /* HP logical GPIO 8, WL_REG_ON, active high */
#define SDIO_VENDOR_BCM  0x02D0U

static uint32_t last_status;         /* interrupt status of the last failed command */
static int sdio_ready;
static uint32_t card_rca;

static int sdhc_command(uint32_t cmdreg, uint32_t arg, uint32_t *resp)
{
    uint32_t start = timer_ticks(), st;
    while (SDHC_PRESENT & 0x1U) {                     /* command inhibit */
        if (timer_elapsed_ms(start) > 100) { last_status = 0xC0DE0001U; return -1; }
    }
    SDHC_INTSTAT = 0xFFFFFEFFU;                       /* clear everything except the card interrupt */
    SDHC_ARG = arg;
    SDHC_CMD = cmdreg << 16;                          /* no data: transfer mode 0 */
    start = timer_ticks();
    for (;;) {
        st = SDHC_INTSTAT;
        if (st & INT_ERRORS) break;
        if (st & INT_CMD_DONE) break;
        if (timer_elapsed_ms(start) > 100) { last_status = st | 0xC0DE0000U; return -1; }
    }
    if (st & INT_ERRORS) {
        last_status = st;
        SDHC_INTSTAT = st & 0xFFFFFEFFU;
        SDHC_CLOCK = (SDHC_CLOCK & 0x00FFFFFFU) | RESET_CMD;
        start = timer_ticks();
        while ((SDHC_CLOCK & RESET_CMD) && timer_elapsed_ms(start) < 10) { }
        return -1;
    }
    if ((cmdreg & 0x3U) == 0x3U) {                    /* R1b: wait for the busy phase to end */
        start = timer_ticks();
        while (!(SDHC_INTSTAT & INT_XFER_DONE) && timer_elapsed_ms(start) < 100) { }
    }
    SDHC_INTSTAT = st & 0xFFFFFEFFU;
    if (resp) *resp = SDHC_RESP0;
    return 0;
}

/* CMD52 IO_RW_DIRECT: one byte, read or write, function fn, 17-bit address. */
static int cmd52(int write, unsigned fn, uint32_t addr, uint8_t data, uint8_t *out)
{
    uint32_t arg = ((uint32_t)(write ? 1 : 0) << 31) | ((uint32_t)(fn & 7) << 28) |
                   ((uint32_t)(write ? 1 : 0) << 27) | ((addr & 0x1FFFFU) << 9) | data;
    uint32_t resp;
    if (sdhc_command(CMD_R5R6(52), arg, &resp)) return -1;
    if (resp & 0xCB00U) { last_status = 0xC0DE5200U | ((resp >> 8) & 0xFF); return -1; }  /* R5 error flags */
    if (out) *out = (uint8_t)(resp & 0xFF);
    return 0;
}

static int read8(unsigned fn, uint32_t addr, uint8_t *out) { return cmd52(0, fn, addr, 0, out); }
static int write8(unsigned fn, uint32_t addr, uint8_t v) { return cmd52(1, fn, addr, v, 0); }

static void wifi_power(int on)
{
    GPIO_OUTPUT(WIFI_POWER_PIN) = on ? 1 : 0;
    GPIO_DIR(WIFI_POWER_PIN) = 1;
}

/* Change the SD clock divider (power of two, 0 = base clock) and wait until stable. */
static void sdhc_set_clock_div(uint32_t div)
{
    uint32_t clk = SDHC_CLOCK & 0x00FF0000U, start;
    SDHC_CLOCK = clk | (div << 8) | CLK_INT_EN;          /* SD clock off while changing */
    start = timer_ticks();
    while (!(SDHC_CLOCK & CLK_INT_STABLE) && timer_elapsed_ms(start) < 100) { }
    SDHC_CLOCK = clk | (div << 8) | CLK_INT_EN | CLK_SD_EN;
}

/* One wrapper handshake: returns the accepted value (0..255) or -1. */
static int sdw_handshake(uint32_t reg, uint16_t done_bit)
{
    volatile uint8_t *hi = (volatile uint8_t *)(uintptr_t)reg, *lo = hi + 1;
    unsigned n;
    for (n = 0; n < 256; n++) {
        *lo = (uint8_t)n;                              /* HP stores the low byte first */
        *hi = 0x01;
        if (SDW_STATUS & done_bit) {
            *lo = (uint8_t)n;
            *hi = 0x00;                                /* clear the request bit, keep the value */
            return (int)n;
        }
    }
    *lo = 0;
    *hi = 0;
    return -1;
}

static void sd_block_enable(void)
{
    uint16_t before = SD_RESET_REG;
    int a, b;
    SD_RESET_REG = (uint16_t)(before & ~SD_RESET_BIT);
    timer_delay_us(10);
    SDW_CTRL = 0;
    a = sdw_handshake(0xBD0B0402U, 0x0001U);
    b = sdw_handshake(0xBD0B0406U, 0x0002U);
    uart_printf("wifi: SD block reset %04x -> %04x, wrapper status %04x, handshakes %d %d\n",
                before, SD_RESET_REG, SDW_STATUS, a, b);
}

static int sdhc_init(void)
{
    uint32_t caps = SDHC_CAPS, base_mhz = (caps >> 8) & 0x3FU, div = 0x80U, start;
    SDHC_CLOCK = RESET_ALL;
    start = timer_ticks();
    while ((SDHC_CLOCK & RESET_ALL) && timer_elapsed_ms(start) < 100) { }
    SDHC_HOSTCTL = 0x0100U;                           /* bus power on, 1-bit, normal speed (HP: power byte 0x01) */
    if (base_mhz == 0) base_mhz = 48;
    /* identification clock <= 400 kHz: base / (2 * div), div a power of two up to 0x80 */
    while (div > 1 && base_mhz * 1000U / (2U * (div >> 1)) <= 400U) div >>= 1;
    SDHC_CLOCK = (0x0EU << 16) | (div << 8) | CLK_INT_EN;
    start = timer_ticks();
    while (!(SDHC_CLOCK & CLK_INT_STABLE)) {
        if (timer_elapsed_ms(start) > 100) { uart_puts("wifi: SD host clock never became stable\n"); return -1; }
    }
    SDHC_CLOCK = (0x0EU << 16) | (div << 8) | CLK_INT_EN | CLK_SD_EN;
    SDHC_INTEN = 0x01FF00FFU;                          /* status bits we poll (no card interrupt) */
    SDHC_SIGEN = 0;                                    /* no interrupt lines */
    timer_delay_ms(2);                                 /* >= 74 identification clocks */
    uart_printf("wifi: SD host caps %08x version %08x, base %u MHz, identification clock %u kHz\n",
                caps, SDHC_VERSION, base_mhz, base_mhz * 1000U / (2U * div));
    return 0;
}

static int sdio_enumerate(void)
{
    uint32_t ocr = 0, resp = 0, start;
    sdhc_command(CMD_NORESP(0), 0, 0);                 /* GO_IDLE: SDIO cards may ignore it */
    if (sdhc_command(CMD_R4(5), 0, &ocr)) {
        uart_printf("wifi: no answer to CMD5 (status %08x): module off, wrong pins or not an SDIO card\n", last_status);
        return -1;
    }
    uart_printf("wifi: CMD5 OCR %08x: %u I/O functions, voltage window %06x\n", ocr, (ocr >> 28) & 7, ocr & 0xFFFFFF);
    start = timer_ticks();
    do {
        if (sdhc_command(CMD_R4(5), ocr & 0x00FF8000U, &resp)) {
            uart_printf("wifi: CMD5 with voltage window failed (status %08x)\n", last_status);
            return -1;
        }
        if (resp & 0x80000000U) break;
        timer_delay_ms(10);
    } while (timer_elapsed_ms(start) < 1000);
    if (!(resp & 0x80000000U)) { uart_printf("wifi: card never reported ready (OCR %08x)\n", resp); return -1; }
    if (sdhc_command(CMD_R5R6(3), 0, &resp)) { uart_printf("wifi: CMD3 failed (status %08x)\n", last_status); return -1; }
    card_rca = resp >> 16;
    if (sdhc_command(CMD_R1B(7), card_rca << 16, &resp)) { uart_printf("wifi: CMD7 failed (status %08x)\n", last_status); return -1; }
    uart_printf("wifi: card ready, relative address %04x, selected\n", card_rca);
    return 0;
}

/* Walk the common CIS for CISTPL_MANFID (0x20): vendor and device IDs. */
static int read_manfid(uint16_t *vendor, uint16_t *device)
{
    uint8_t b0, b1, b2, code, len, d[4];
    uint32_t ptr, guard;
    if (read8(0, 0x09, &b0) || read8(0, 0x0A, &b1) || read8(0, 0x0B, &b2)) return -1;
    ptr = (uint32_t)b0 | ((uint32_t)b1 << 8) | ((uint32_t)b2 << 16);
    for (guard = 0; guard < 32; guard++) {
        if (read8(0, ptr, &code)) return -1;
        if (code == 0xFF) break;
        if (read8(0, ptr + 1, &len)) return -1;
        if (code == 0x20 && len >= 4) {
            unsigned i;
            for (i = 0; i < 4; i++) if (read8(0, ptr + 2 + i, &d[i])) return -1;
            *vendor = (uint16_t)(d[0] | (d[1] << 8));
            *device = (uint16_t)(d[2] | (d[3] << 8));
            return 0;
        }
        ptr += 2U + len;
    }
    return -1;
}

/* Read a 32-bit backplane word through the function-1 window with CMD52 bytes. */
static int backplane_read32(uint32_t addr, uint32_t *out)
{
    uint8_t b;
    uint32_t v = 0;
    unsigned i;
    static const uint32_t win_regs[3] = { 0x1000A, 0x1000B, 0x1000C };
    const uint8_t win_vals[3] = { (uint8_t)((addr >> 8) & 0x80), (uint8_t)(addr >> 16), (uint8_t)(addr >> 24) };
    for (i = 0; i < 3; i++) {
        if (write8(1, win_regs[i], win_vals[i])) {
            uart_printf("wifi: window register %05x <- %02x failed (status %08x)\n", win_regs[i], win_vals[i], last_status);
            return -1;
        }
    }
    for (i = 0; i < 4; i++) {
        if (read8(1, (addr & 0x7FFFU) + i, &b)) {
            uart_printf("wifi: backplane byte %u at %08x failed (status %08x)\n", i, addr + i, last_status);
            return -1;
        }
        v |= (uint32_t)b << (8 * i);                    /* the chip is little-endian */
    }
    *out = v;
    return 0;
}

/* ------------------------------------------------------------------------- */
/* CMD53 block transfers through an uncached DMA buffer (SDMA, polled).       */
/* ------------------------------------------------------------------------- */

#define SDHC_SDMA        REG32(SDHC_BASE + 0x00)
#define INT_DMA          0x00000008U
#define RESET_DAT        0x04000000U
#define DMA_BUF          0x60800000U          /* uncached alias of DRAM offset 8 MiB (free area) */
#define DMA_BUF_SIZE     0x00010000U
#define DMA_PTR          ((volatile uint8_t *)(uintptr_t)DMA_BUF)
#define F1_BLOCK         64U
#define F2_BLOCK         512U

static inline void drain_write_buffer(void)
{
    __asm__ volatile ("mcr p15, 0, %0, c7, c10, 4" :: "r"(0) : "memory");
}

static void sdhc_reset_lines(void)
{
    uint32_t start = timer_ticks();
    SDHC_CLOCK = (SDHC_CLOCK & 0x00FFFFFFU) | RESET_CMD | RESET_DAT;
    while ((SDHC_CLOCK & (RESET_CMD | RESET_DAT)) && timer_elapsed_ms(start) < 10) { }
}

/* CMD53 IO_RW_EXTENDED of len bytes at DMA_BUF + dma_off. Block mode when
   len is larger than one block (len must then be a multiple of blksz). */
static int cmd53(int write, unsigned fn, uint32_t addr, uint32_t len, uint32_t blksz, uint32_t dma_off)
{
    uint32_t blocks = 1, bsize = len, arg, mode, st, start, resp;
    int block_mode = 0;
    if (blksz && len > blksz) {
        if (len % blksz || len / blksz > 511) return -1;
        blocks = len / blksz;
        bsize = blksz;
        block_mode = 1;
    } else if (len == 0 || len > 512) {
        return -1;
    }
    arg = ((uint32_t)(write ? 1 : 0) << 31) | ((uint32_t)(fn & 7) << 28) | ((uint32_t)block_mode << 27) |
          (1U << 26) | ((addr & 0x1FFFFU) << 9) | (block_mode ? blocks : (len & 0x1FFU));
    mode = 0x01U | 0x02U | (write ? 0 : 0x10U) | (blocks > 1 ? 0x20U : 0);   /* DMA, block count, dir, multi */
    if (write) drain_write_buffer();
    start = timer_ticks();
    while (SDHC_PRESENT & 0x3U) {                                          /* command and data inhibit */
        if (timer_elapsed_ms(start) > 100) { last_status = 0xC0DE5301U; return -1; }
    }
    SDHC_INTSTAT = 0xFFFFFEFFU;
    SDHC_SDMA = DMA_BUF + dma_off;
    SDHC_BLK = (blocks << 16) | (7U << 12) | bsize;                         /* 512 KiB SDMA boundary */
    SDHC_ARG = arg;
    SDHC_CMD = (((53U << 8) | 0x3AU) << 16) | mode;                        /* data present, R5 */
    start = timer_ticks();
    for (;;) {
        st = SDHC_INTSTAT;
        if (st & INT_ERRORS) break;
        if (st & INT_DMA) { SDHC_INTSTAT = INT_DMA; SDHC_SDMA = SDHC_SDMA; } /* boundary: resume */
        if (st & INT_XFER_DONE) break;
        if (timer_elapsed_ms(start) > 500) { last_status = st | 0xC0DE0000U; sdhc_reset_lines(); return -1; }
    }
    resp = SDHC_RESP0;
    SDHC_INTSTAT = st & 0xFFFFFEFFU;
    if (st & INT_ERRORS) { last_status = st; sdhc_reset_lines(); return -1; }
    if (resp & 0xCB00U) { last_status = 0xC0DE5300U | ((resp >> 8) & 0xFF); return -1; }
    return 0;
}

/* --- backplane access through the function-1 window --------------------- */

static uint32_t sb_window_cur = 0xFFFFFFFFU;

static int set_window(uint32_t addr)
{
    uint32_t w = addr & ~0x7FFFU;
    if (w == sb_window_cur) return 0;
    if (write8(1, 0x1000A, (uint8_t)((w >> 8) & 0x80)) || write8(1, 0x1000B, (uint8_t)(w >> 16)) ||
        write8(1, 0x1000C, (uint8_t)(w >> 24))) {
        sb_window_cur = 0xFFFFFFFFU;
        return -1;
    }
    sb_window_cur = w;
    return 0;
}

/* 32-bit register access: CMD53 byte mode, 4 bytes, SBSDIO_SB_ACCESS_2_4B_FLAG. */
static int bp_read32(uint32_t addr, uint32_t *v)
{
    if (set_window(addr) || cmd53(0, 1, (addr & 0x7FFFU) | 0x8000U, 4, 0, 0)) return -1;
    *v = DMA_PTR[0] | ((uint32_t)DMA_PTR[1] << 8) | ((uint32_t)DMA_PTR[2] << 16) | ((uint32_t)DMA_PTR[3] << 24);
    return 0;
}

static int bp_write32(uint32_t addr, uint32_t v)
{
    DMA_PTR[0] = (uint8_t)v; DMA_PTR[1] = (uint8_t)(v >> 8); DMA_PTR[2] = (uint8_t)(v >> 16); DMA_PTR[3] = (uint8_t)(v >> 24);
    if (set_window(addr) || cmd53(1, 1, (addr & 0x7FFFU) | 0x8000U, 4, 0, 0)) return -1;
    return 0;
}

/* Memory transfer in pieces that never cross a 32 KiB window: 2 KiB in
   64-byte blocks, shorter tails in byte mode. dst/src NULL = DMA buffer only. */
static int bp_mem(int write, uint32_t addr, uint8_t *host, uint32_t len)
{
    while (len) {
        uint32_t room = 0x8000U - (addr & 0x7FFFU), n = len < 2048U ? len : 2048U, i;
        if (n > room) n = room;
        if (n > F1_BLOCK && (n % F1_BLOCK)) n -= n % F1_BLOCK;
        if (write) for (i = 0; i < n; i++) DMA_PTR[i] = host[i];
        if (set_window(addr) || cmd53(write, 1, (addr & 0x7FFFU) | 0x8000U, n, F1_BLOCK, 0)) return -1;
        if (!write) for (i = 0; i < n; i++) host[i] = DMA_PTR[i];
        addr += n; host += n; len -= n;
    }
    return 0;
}

/* --- chip cores (AXI enumeration ROM, AI wrappers) ----------------------- */

#define CORE_CC          0x800
#define CORE_SOCRAM      0x80E
#define CORE_D11         0x812
#define CORE_SDIOD       0x829
#define CORE_ARM_CM3     0x82A
#define AI_IOCTRL        0x408
#define AI_RESETCTRL     0x800
#define AI_IOCTRL_CLK    0x0001U
#define AI_IOCTRL_FGC    0x0002U

struct bcm_core { uint16_t id; uint8_t rev; uint32_t base, wrap; };
static struct bcm_core cores[12];
static unsigned ncores;

static const struct bcm_core *find_core(uint16_t id)
{
    unsigned i;
    for (i = 0; i < ncores; i++) if (cores[i].id == id) return &cores[i];
    return 0;
}

static int erom_scan(void)
{
    uint32_t erom, a, w, cib, guard;
    struct bcm_core *cur = 0;
    if (bp_read32(0x180000FCU, &erom)) return -1;
    ncores = 0;
    for (a = erom, guard = 0; guard < 256; guard++, a += 4) {
        if (bp_read32(a, &w)) return -1;
        if ((w & 0xFU) == 0xFU) break;                         /* end of table */
        if ((w & 0x7U) == 0x1U) {                              /* component: CIA then CIB */
            a += 4;
            if (bp_read32(a, &cib)) return -1;
            if (((w >> 20) & 0xFFFU) != 0x4BFU) { cur = 0; continue; }   /* only Broadcom cores (not ARM interconnect) */
            cur = ncores < sizeof(cores) / sizeof(cores[0]) ? &cores[ncores++] : 0;
            if (cur) { cur->id = (uint16_t)((w >> 8) & 0xFFF); cur->rev = (uint8_t)(cib >> 24); cur->base = cur->wrap = 0; }
        } else if ((w & 0x7U) == 0x5U) {                       /* address descriptor */
            uint32_t stype = (w >> 6) & 3U, port = (w >> 8) & 0xFU, sz = (w >> 4) & 3U;
            if (cur && stype == 0 && port == 0 && !cur->base) cur->base = w & 0xFFFFF000U;
            if (cur && (stype == 2 || stype == 3) && !cur->wrap) cur->wrap = w & 0xFFFFF000U;
            if (w & 0x8U) a += 4;                              /* 64-bit address */
            if (sz == 3) {                                     /* explicit size descriptor */
                uint32_t s;
                a += 4;
                if (bp_read32(a, &s)) return -1;
                if (s & 0x8U) a += 4;
            }
        }                                                      /* master ports, empty: skip */
    }
    return 0;
}

static int core_disable(const struct bcm_core *c, uint32_t prereset, uint32_t reset)
{
    uint32_t v = 0, start;
    if (bp_read32(c->wrap + AI_RESETCTRL, &v)) return -1;
    if (!(v & 1U)) {
        bp_write32(c->wrap + AI_IOCTRL, prereset | AI_IOCTRL_FGC | AI_IOCTRL_CLK);
        bp_read32(c->wrap + AI_IOCTRL, &v);
        bp_write32(c->wrap + AI_RESETCTRL, 1);
        timer_delay_us(20);
        start = timer_ticks();
        while (!bp_read32(c->wrap + AI_RESETCTRL, &v) && v != 1U && timer_elapsed_ms(start) < 2) { }
    }
    bp_write32(c->wrap + AI_IOCTRL, reset | AI_IOCTRL_FGC | AI_IOCTRL_CLK);
    return bp_read32(c->wrap + AI_IOCTRL, &v);
}

static int core_reset(const struct bcm_core *c, uint32_t prereset, uint32_t reset, uint32_t postreset)
{
    uint32_t v = 0;
    unsigned n;
    if (core_disable(c, prereset, reset)) return -1;
    for (n = 0; n < 50; n++) {
        if (bp_read32(c->wrap + AI_RESETCTRL, &v)) return -1;
        if (!(v & 1U)) break;
        bp_write32(c->wrap + AI_RESETCTRL, 0);
        timer_delay_us(50);
    }
    bp_write32(c->wrap + AI_IOCTRL, postreset | AI_IOCTRL_CLK);
    return bp_read32(c->wrap + AI_IOCTRL, &v);
}

/* SOCRAM size from the bank registers (brcmfmac's socram_ramsize, rev >= 8). */
static uint32_t socram_size(const struct bcm_core *ram)
{
    uint32_t coreinfo = 0, info = 0, total = 0, nb, i;
    if (bp_read32(ram->base + 0x00, &coreinfo)) return 0;
    nb = (coreinfo >> 4) & 0xFU;
    for (i = 0; i < nb; i++) {
        if (bp_write32(ram->base + 0x10, i) || bp_read32(ram->base + 0x40, &info)) return 0;
        total += ((info & 0x7FU) + 1U) * 8192U;
    }
    return total;
}

/* --- firmware and NVRAM from HP's ROM file system in flash --------------- */

/* The BCM43143 RAM image and its NVRAM text, stored uncompressed back to back
   in HP's ROM file system (dump.bin offsets; identical to what HP uploads, see
   analysis/wifi.md). The version banner and the NVRAM header are checked. */
#define WIFI_FW_FLASH    0x0053AE97U
#define WIFI_FW_LEN      0x0005E630U
#define WIFI_FW_VER_OFF  0x0005E567U
#define WIFI_NV_FLASH    0x005994C7U
#define WIFI_NV_MAX      2048U
static const char fw_banner[] = "43143b0-roml/sdio-g-p2p-mchan";
static const char nv_header[] = "#750-124646";
static uint8_t nvram_buf[WIFI_NV_MAX];

/* Broadcom DHD's process_nvram_vars plus dhdsdio_download_nvram padding:
   comments and empty lines dropped, '\r' removed, each line NUL-terminated,
   length padded to 4, one more NUL, rounded up to 4 again (HP: 892 bytes). */
static uint32_t nvram_build(void)
{
    const uint8_t *src = flash_mmap(WIFI_NV_FLASH);
    uint32_t n, len = 0, col = 0, padded, total;
    int skip = 0;
    for (n = 0; n < WIFI_NV_MAX - 8; n++) {
        uint8_t c = src[n];
        if (c != '\n' && c != '\r' && c != '\t' && (c < 0x20 || c > 0x7E)) break;   /* end of the text file */
        if (c == '\r') continue;
        if (skip && c != '\n') continue;
        skip = 0;
        if (c == '#') { skip = 1; continue; }
        if (c == '\n') { if (col) { nvram_buf[len++] = 0; col = 0; } continue; }
        nvram_buf[len++] = c;
        col++;
    }
    if (col) nvram_buf[len++] = 0;
    padded = (len + 3U) & ~3U;
    total = (padded + 1U + 3U) & ~3U;
    for (n = len; n < total; n++) nvram_buf[n] = 0;
    return total;
}

/* --- SDPCM + BCDC control channel over function 2 ------------------------ */

#define WLC_UP           2
#define WLC_DOWN         3
#define WLC_SET_INFRA    20
#define WLC_SET_AUTH     22
#define WLC_SET_SSID     26
#define WLC_SET_CHANNEL  30
#define WLC_DISASSOC     52
#define WLC_SET_PM       86
#define WLC_SET_AP       118
#define WLC_GET_RSSI     127
#define WLC_SET_WSEC     134
#define WLC_SET_WPA_AUTH 165
#define WLC_GET_VAR      262
#define WLC_SET_VAR      263
#define WLC_SET_WSEC_PMK 268
#define RX_OFF           0x8000U               /* receive area inside the DMA buffer */
#define RX_MAX           0x8000U

/* Broadcom event codes (wl_event_msg_t.event_type, brcmfmac fweh.h) */
#define E_SET_SSID       0
#define E_AUTH           3
#define E_DEAUTH         5
#define E_DEAUTH_IND     6
#define E_ASSOC          7
#define E_ASSOC_IND      8
#define E_REASSOC_IND    10
#define E_DISASSOC       11
#define E_DISASSOC_IND   12
#define E_LINK           16
#define E_PSK_SUP        46
#define SUP_KEYED        6                     /* WLC_SUP_KEYED: 4-way handshake done */

static const struct bcm_core *sdiod;
static uint8_t tx_seq, tx_max;
static uint16_t ioctl_id;
static int fw_running;
static uint32_t events_seen;

/* Link state, kept up to date by the chip's events. */
static int wmode = WIFI_MODE_OFF;
static int link_flag, keyed, ssid_ok, join_failed, join_in_progress, ap_pending, ap_started, want_sta, link_back;
static uint32_t fail_event, fail_status, fail_reason, ap_clients;
static uint8_t mac_addr[6];
static char cur_ssid[33];
static int last_rssi;
static uint32_t rssi_timer;

/* Default network for a bare "wifi join" and the join at power-on. The real
   name and passphrase live in wifi_secrets.h, which git ignores (copy
   wifi_secrets.example.h); without it both are empty and nothing is joined. */
#if defined(__has_include)
#if __has_include("wifi_secrets.h")
#include "wifi_secrets.h"
#endif
#endif
#ifndef DEFAULT_WIFI_SSID
#define DEFAULT_WIFI_SSID ""
#endif
#ifndef DEFAULT_WIFI_PASS
#define DEFAULT_WIFI_PASS ""
#endif

/* Rejoin after a lost link, without blocking the shell: the network of the
   last successful join is asked for again from wifi_poll(), 3 s after the
   loss, then with the wait doubling up to a minute. "wifi leave", "wifi ap"
   and "wifi off" end it; "wifi auto off" switches it off. With
   WIFI_BOOT_JOIN (printer builds, see fw/build.py) the default network is
   joined the same way a few seconds after power-on. */
#ifndef WIFI_BOOT_JOIN
#define WIFI_BOOT_JOIN 0
#endif
#define REJOIN_FIRST_MS  3000U
#define REJOIN_MAX_MS    60000U
#define REJOIN_TRY_MS    15000U
#define BOOT_JOIN_MS     4000U
static int auto_rejoin = 1, boot_join = WIFI_BOOT_JOIN, rejoin_active, have_saved;
static char saved_ssid[33], saved_pass[64];
static uint32_t rejoin_timer, rejoin_delay_ms = REJOIN_FIRST_MS, rejoin_attempts, rejoins_ok, links_lost;
static uint32_t rxq_pinned;                    /* slots being handed to the stack right now */

/* Data frames that arrive while an ioctl answer is awaited wait here for
   wifi_poll(); events are handled as soon as they are read. */
#define RXQ_SLOTS        8U
#define RXQ_FRAME        1600U
static uint8_t rxq[RXQ_SLOTS][RXQ_FRAME];
static uint16_t rxq_len[RXQ_SLOTS];
static unsigned rxq_head, rxq_tail;
static uint32_t rxq_drops, data_in, data_out, data_errors;
static uint32_t rx_glom_frames, rx_bad_frames, last_rx_hdr;
static uint8_t evbuf[256];

/* Read one frame. Unless `drain` is set (a frame was just read, more may be
   queued: brcmfmac reads on until an empty header), only when the chip flags
   one in SDIOD intstatus (FRAME_IND, polled). Returns the frame length
   (header included, at DMA_BUF + RX_OFF), 0 if none, -1 on error. */
static int f2_read_frame(int drain)
{
    uint32_t st = 0, len, rem;
    if (!drain) {
        if (bp_read32(sdiod->base + 0x20, &st)) return -1;
        if (!(st & 0x40U)) return 0;
        bp_write32(sdiod->base + 0x20, 0x40U);                 /* acknowledge FRAME_IND */
    }
    if (cmd53(0, 2, 0, 64, 0, RX_OFF)) return -1;
    len = DMA_PTR[RX_OFF] | ((uint32_t)DMA_PTR[RX_OFF + 1] << 8);
    if (len < 12 || len > RX_MAX ||
        ((len ^ (DMA_PTR[RX_OFF + 2] | ((uint32_t)DMA_PTR[RX_OFF + 3] << 8))) & 0xFFFFU) != 0xFFFFU)
        return 0;                                              /* no or invalid frame */
    {
        /* flow control: the chip's window (highest sequence we may send), with
           brcmfmac's sanity check */
        uint8_t w = DMA_PTR[RX_OFF + 9];
        tx_max = (uint8_t)(w - tx_seq) > 0x40U ? (uint8_t)(tx_seq + 2U) : w;
    }
    if (len > 64) {
        rem = len - 64;
        rem = rem > F2_BLOCK ? (rem + F2_BLOCK - 1) / F2_BLOCK * F2_BLOCK : (rem + 3U) & ~3U;
        if (cmd53(0, 2, 0, rem, F2_BLOCK, RX_OFF + 64)) return -1;
    }
    return (int)len;
}

static uint16_t be16(const uint8_t *p) { return (uint16_t)((p[0] << 8) | p[1]); }
static uint32_t be32(const uint8_t *p) { return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | p[3]; }

static void mac_str(const uint8_t *m, char *buf)
{
    snprintf(buf, 18, "%02x:%02x:%02x:%02x:%02x:%02x", m[0], m[1], m[2], m[3], m[4], m[5]);
}

/* The station link went away: start the rejoin clock (want_sta stays set). */
static void link_lost(void)
{
    links_lost++;
    wmode = WIFI_MODE_IDLE;
    net_link_change(0, 0);
    rejoin_timer = timer_ticks();
    rejoin_delay_ms = REJOIN_FIRST_MS;
    if (auto_rejoin && have_saved)
        uart_printf("wifi: will rejoin \"%s\" in %u s ('wifi auto off' to stop)\n", saved_ssid, REJOIN_FIRST_MS / 1000U);
}

static void note_failure(uint32_t type, uint32_t status, uint32_t reason)
{
    join_failed = 1;
    fail_event = type;
    fail_status = status;
    fail_reason = reason;
}

/* One Broadcom event (Ethernet frame with type 0x886C): BCMILCP header, then
   wl_event_msg_t in big-endian order: version, flags, event type, status,
   reason, auth type, data length, peer address, interface name, indices. */
static void handle_event(const uint8_t *e, uint32_t len)
{
    const uint8_t *m = e + 24;
    uint32_t type, status, reason, flags;
    char peer[18];
    if (len < 24 + 48 || be16(e + 14) != 0x8001U || e[19] != 0x00 || e[20] != 0x10 || e[21] != 0x18) return;
    events_seen++;
    flags = be16(m + 2);
    type = be32(m + 4);
    status = be32(m + 8);
    reason = be32(m + 12);
    mac_str(m + 24, peer);
    switch (type) {
    case E_SET_SSID:
        if (status == 0) { ssid_ok = 1; if (ap_pending) ap_started = 1; }
        else if (join_in_progress || ap_pending) note_failure(type, status, reason);
        break;
    case E_LINK:
        link_flag = (int)(flags & 1U);
        if (link_flag && ap_pending) ap_started = 1;
        if (!link_flag && wmode == WIFI_MODE_STA) {
            uart_printf("wifi: link lost (reason %u)\n", reason);
            link_lost();
        } else if (link_flag && wmode == WIFI_MODE_IDLE && want_sta && !join_in_progress) {
            uart_printf("wifi: link back to \"%s\"\n", cur_ssid);   /* the chip re-associated by itself */
            wmode = WIFI_MODE_STA;
            link_back = 1;                  /* DHCP starts from wifi_poll(), not inside an ioctl wait */
        }
        break;
    case E_PSK_SUP:
        if (status == SUP_KEYED) keyed = 1;
        else if (join_in_progress && link_flag) note_failure(type, status, reason);
        break;
    case E_AUTH:
        if (status != 0 && join_in_progress) note_failure(type, status, reason);
        break;
    case E_DEAUTH:
    case E_DEAUTH_IND:
    case E_DISASSOC:
    case E_DISASSOC_IND:
        if (wmode == WIFI_MODE_AP) {
            if (ap_clients) ap_clients--;
            uart_printf("wifi: station %s left\n", peer);
        } else if (wmode == WIFI_MODE_STA) {
            uart_printf("wifi: disconnected by the access point (event %u, reason %u)\n", type, reason);
            link_flag = 0;
            link_lost();
        } else if (join_in_progress) {
            note_failure(type, status, reason);
        }
        break;
    case E_ASSOC_IND:
    case E_REASSOC_IND:
        if (wmode == WIFI_MODE_AP || ap_pending) {
            ap_clients++;
            uart_printf("wifi: station %s joined\n", peer);
        }
        break;
    default:
        break;
    }
}

/* A frame on channel 1 (events) or 2 (data) at DMA_BUF + RX_OFF: events are
   handled at once, data frames are queued for wifi_poll(). The BDC header
   (flags, priority, interface, data offset in words) precedes the Ethernet
   frame; events are recognised by their Ethernet type on either channel. */
static void rx_dispatch(uint32_t fl)
{
    volatile uint8_t *r = DMA_PTR + RX_OFF;
    uint32_t chan = r[5] & 0x0FU, doff = r[7], boff, elen, i;
    if (chan == 3) { rx_glom_frames++; return; }
    if ((chan != 1 && chan != 2) || doff < 12 || doff + 4 > fl) {
        if (fl > 12) {
            rx_bad_frames++;
            last_rx_hdr = ((uint32_t)r[5] << 24) | ((uint32_t)r[7] << 16) | (fl & 0xFFFFU);
        }
        return;
    }
    boff = doff + 4U + 4U * r[doff + 3];
    if (boff + 14 > fl) { rx_bad_frames++; return; }
    elen = fl - boff;
    if (r[boff + 12] == 0x88 && r[boff + 13] == 0x6C) {
        uint32_t n = elen < sizeof(evbuf) ? elen : (uint32_t)sizeof(evbuf);
        for (i = 0; i < n; i++) evbuf[i] = r[boff + i];
        handle_event(evbuf, n);
        return;
    }
    if (chan != 2 || elen > RXQ_FRAME || (rxq_head + 1U) % RXQ_SLOTS == rxq_tail || (rxq_pinned & (1U << rxq_head))) {
        rxq_drops++;
        return;
    }
    for (i = 0; i < elen; i++) rxq[rxq_head][i] = r[boff + i];
    rxq_len[rxq_head] = (uint16_t)elen;
    rxq_head = (rxq_head + 1U) % RXQ_SLOTS;
    data_in++;
}

/* Hand queued data frames to the network stack. The tail moves on before
   net_input() runs, so a command started from the network that polls the
   link again (web shell: ping, wifi ...) never sees the same frame twice;
   the slot stays pinned until net_input() returns, because replies sent from
   there may queue new frames. */
static void rxq_deliver(void)
{
    while (rxq_tail != rxq_head) {
        unsigned t = rxq_tail;
        rxq_tail = (t + 1U) % RXQ_SLOTS;
        rxq_pinned |= 1U << t;
        net_input(rxq[t], rxq_len[t]);
        rxq_pinned &= ~(1U << t);
    }
}

/* Read and dispatch whatever the chip has (control answers are stale here). */
static void pump_frames(void)
{
    unsigned guard;
    for (guard = 0; guard < 16; guard++) {
        int fl = f2_read_frame(guard > 0);
        if (fl <= 0) break;
        if ((DMA_PTR[RX_OFF + 5] & 0x0FU) != 0) rx_dispatch((uint32_t)fl);
    }
}

static int f2_has_credit(void)
{
    uint8_t d = (uint8_t)(tx_max - tx_seq);
    return d != 0 && !(d & 0x80U);
}

/* Send the payload (plen bytes) already at DMA_BUF + 12 on SDPCM channel
   `chan`. Like brcmfmac's data_ok(), wait (briefly) until the chip's window
   allows another sequence number; reading frames updates the window. */
static int f2_send(uint32_t chan, uint32_t plen)
{
    volatile uint8_t *f = DMA_PTR;
    uint32_t total = 12U + plen, padded = (total + 3U) & ~3U, i, start = timer_ticks();
    if (padded > F2_BLOCK) padded = (padded + F2_BLOCK - 1U) / F2_BLOCK * F2_BLOCK;
    while (!f2_has_credit() && timer_elapsed_ms(start) < 20U) {
        int fl = f2_read_frame(0);
        if (fl > 0) { if ((DMA_PTR[RX_OFF + 5] & 0x0FU) != 0) rx_dispatch((uint32_t)fl); }
        else timer_delay_us(200);
    }
    f[0] = (uint8_t)total; f[1] = (uint8_t)(total >> 8);
    f[2] = (uint8_t)~total; f[3] = (uint8_t)(~total >> 8);
    f[4] = tx_seq++;                                           /* sequence */
    f[5] = (uint8_t)chan;                                      /* 0 control, 2 data */
    f[6] = 0;
    f[7] = 12;                                                 /* data offset */
    f[8] = f[9] = f[10] = f[11] = 0;
    for (i = total; i < padded; i++) f[i] = 0;
    return cmd53(1, 2, 0, padded, F2_BLOCK, 0);
}

int wifi_send(const uint8_t *eth, uint32_t len)
{
    volatile uint8_t *f = DMA_PTR;
    uint32_t i, send_len;
    if (!fw_running || len < 14 || len > 1518) return -1;
    send_len = len < 60U ? 60U : len;
    f[12] = 0x20;                                              /* BDC version 2 */
    f[13] = 0;                                                 /* priority: best effort */
    f[14] = 0;                                                 /* interface 0 */
    f[15] = 0;                                                 /* no extra data offset */
    for (i = 0; i < len; i++) f[16 + i] = eth[i];
    for (; i < send_len; i++) f[16 + i] = 0;
    if (f2_send(2, 4U + send_len)) { data_errors++; return -1; }
    data_out++;
    return 0;
}

/* Send one BCDC ioctl and wait for its answer. buf: in = request (len bytes),
   out = response payload (up to len bytes). Returns the firmware status (0 = ok). */
static int wl_ioctl(uint32_t cmd, int set, uint8_t *buf, uint32_t len, uint32_t timeout_ms)
{
    uint32_t flags, start, i;
    volatile uint8_t *f = DMA_PTR;
    int drain = 0;
    if (len > 2048) return -1000;
    ioctl_id++;
    for (i = 12; i < 28; i++) f[i] = 0;
    flags = ((uint32_t)ioctl_id << 16) | (set ? 0x02U : 0);
    for (i = 0; i < 4; i++) {
        f[12 + i] = (uint8_t)(cmd >> (8 * i));
        f[16 + i] = (uint8_t)(len >> (8 * i));
        f[20 + i] = (uint8_t)(flags >> (8 * i));
    }
    for (i = 0; i < len; i++) f[28 + i] = buf[i];
    if (f2_send(0, 16U + len)) return -1001;
    start = timer_ticks();
    while (timer_elapsed_ms(start) < timeout_ms) {
        volatile uint8_t *r = DMA_PTR + RX_OFF;
        uint32_t chan, doff, rlen, rflags, rstatus, n;
        int fl = f2_read_frame(drain);
        if (fl < 0) return -1002;
        if (fl == 0) { drain = 0; timer_delay_ms(2); continue; }
        drain = 1;
        chan = r[5] & 0x0FU;
        doff = r[7];
        if (chan != 0) { rx_dispatch((uint32_t)fl); continue; } /* events now, data later */
        if (doff + 16 > (uint32_t)fl) continue;
        rlen = r[doff + 4] | ((uint32_t)r[doff + 5] << 8) | ((uint32_t)r[doff + 6] << 16) | ((uint32_t)r[doff + 7] << 24);
        rflags = r[doff + 8] | ((uint32_t)r[doff + 9] << 8) | ((uint32_t)r[doff + 10] << 16) | ((uint32_t)r[doff + 11] << 24);
        rstatus = r[doff + 12] | ((uint32_t)r[doff + 13] << 8) | ((uint32_t)r[doff + 14] << 16) | ((uint32_t)r[doff + 15] << 24);
        if ((rflags >> 16) != ioctl_id) continue;              /* stale answer */
        n = rlen < len ? rlen : len;
        if (doff + 16 + n > (uint32_t)fl) n = (uint32_t)fl - doff - 16;
        for (i = 0; i < n; i++) buf[i] = r[doff + 16 + i];
        return (int)rstatus;
    }
    return -1003;                                              /* timeout */
}

static int wl_get_var(const char *name, uint8_t *buf, uint32_t len)
{
    uint32_t i, nl = (uint32_t)strlen(name) + 1;
    if (nl > len) return -1000;
    for (i = 0; i < len; i++) buf[i] = 0;
    for (i = 0; i < nl; i++) buf[i] = (uint8_t)name[i];
    return wl_ioctl(WLC_GET_VAR, 0, buf, len, 1000);
}

static void put32le(uint8_t *p, uint32_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24); }

static int wl_set_int(uint32_t cmd, uint32_t v)
{
    uint8_t b[4];
    put32le(b, v);
    return wl_ioctl(cmd, 1, b, 4, 1000);
}

static int wl_set_var(const char *name, const uint8_t *val, uint32_t vlen)
{
    uint8_t b[96];
    uint32_t nl = (uint32_t)strlen(name) + 1, i;
    if (nl + vlen > sizeof(b)) return -1000;
    for (i = 0; i < nl; i++) b[i] = (uint8_t)name[i];
    for (i = 0; i < vlen; i++) b[nl + i] = val[i];
    return wl_ioctl(WLC_SET_VAR, 1, b, nl + vlen, 1000);
}

static int wl_set_var_int(const char *name, uint32_t v)
{
    uint8_t b[4];
    put32le(b, v);
    return wl_set_var(name, b, 4);
}

/* "bsscfg:<name>" iovars take the BSS configuration index (0) first. */
static int wl_set_bsscfg_int(const char *name, uint32_t v)
{
    char full[40];
    uint8_t b[8];
    snprintf(full, sizeof(full), "bsscfg:%s", name);
    put32le(b, 0);
    put32le(b + 4, v);
    return wl_set_var(full, b, 8);
}

/* --- download and boot ---------------------------------------------------- */

static int wifi_probe(void);

static int wifi_fw(void)
{
    const struct bcm_core *arm, *ram, *d11;
    const uint8_t *fw = flash_mmap(WIFI_FW_FLASH);
    uint8_t check[64], rdy = 0, csr = 0, ver[256];
    uint32_t ramsize, nvlen, nvaddr, token, v, i, start;
    int rc;
    if (wmode == WIFI_MODE_STA || wmode == WIFI_MODE_AP) net_link_change(0, 0);
    wmode = WIFI_MODE_OFF;
    want_sta = 0;
    link_back = 0;
    rejoin_active = 0;
    join_in_progress = 0;
    rxq_head = rxq_tail = 0;
    rx_glom_frames = rx_bad_frames = last_rx_hdr = 0;
    fw_running = 0;
    if (memcmp(fw + WIFI_FW_VER_OFF, fw_banner, sizeof(fw_banner) - 1) ||
        memcmp(flash_mmap(WIFI_NV_FLASH), nv_header, sizeof(nv_header) - 1)) {
        uart_puts("wifi: the chip firmware or its NVRAM is not where HP's file system had it; refusing\n");
        return -1;
    }
    if (wifi_probe()) return -1;
    write8(0, 0x110, (uint8_t)F1_BLOCK);                       /* function 1 block size */
    write8(0, 0x111, 0);
    sb_window_cur = 0xFFFFFFFFU;
    if (erom_scan()) { uart_printf("wifi: core table read failed (status %08x)\n", last_status); return -1; }
    uart_printf("wifi: %u cores:", ncores);
    for (i = 0; i < ncores; i++) uart_printf(" %03x/%u@%08x", cores[i].id, cores[i].rev, cores[i].base);
    uart_puts("\n");
    arm = find_core(CORE_ARM_CM3);
    ram = find_core(CORE_SOCRAM);
    d11 = find_core(CORE_D11);
    sdiod = find_core(CORE_SDIOD);
    if (!arm || !ram || !d11 || !sdiod || !arm->wrap || !ram->wrap || !d11->wrap) {
        uart_puts("wifi: ARM, SOCRAM, D11 or SDIO core missing\n");
        return -1;
    }
    /* passive: ARM halted, D11 and SOCRAM reset (brcmf_chip_cm3_set_passive) */
    if (core_disable(arm, 0, 0) || core_reset(d11, 0x0CU, 0x04U, 0x04U) || core_reset(ram, 0, 0, 0)) {
        uart_printf("wifi: putting the chip into download mode failed (status %08x)\n", last_status);
        return -1;
    }
    ramsize = socram_size(ram);
    if (ramsize < 0x40000U || ramsize > 0x100000U) {
        uart_printf("wifi: implausible RAM size %u, assuming 512 KiB\n", ramsize);
        ramsize = 0x80000U;
    }
    write8(1, 0x1000E, 0x10);                                  /* request the HT clock for the download */
    start = timer_ticks();
    while (!read8(1, 0x1000E, &csr) && !(csr & 0x80) && timer_elapsed_ms(start) < 50) { }
    uart_printf("wifi: RAM %u KiB, clock CSR %02x; writing %u bytes of firmware...\n", ramsize / 1024, csr, WIFI_FW_LEN);
    start = timer_ticks();
    for (i = 0; i < WIFI_FW_LEN; i += 0x8000U) {
        uint32_t n = WIFI_FW_LEN - i < 0x8000U ? WIFI_FW_LEN - i : 0x8000U;
        if (bp_mem(1, i, (uint8_t *)(uintptr_t)(fw + i), n)) {
            uart_printf("wifi: firmware write at %05x failed (status %08x)\n", i, last_status);
            return -1;
        }
    }
    uart_printf("wifi: firmware written in %u ms\n", timer_elapsed_ms(start));
    if (bp_mem(0, 0, check, 64) || memcmp(check, fw, 64) ||
        bp_mem(0, WIFI_FW_LEN - 64, check, 64) || memcmp(check, fw + WIFI_FW_LEN - 64, 64)) {
        uart_puts("wifi: firmware read-back mismatch\n");
        return -1;
    }
    nvlen = nvram_build();
    nvaddr = ramsize - 4U - nvlen;
    token = nvlen / 4U;
    token = (~token << 16) | (token & 0xFFFFU);
    if (bp_mem(1, nvaddr, nvram_buf, nvlen) || bp_write32(ramsize - 4U, token)) {
        uart_printf("wifi: NVRAM write failed (status %08x)\n", last_status);
        return -1;
    }
    uart_printf("wifi: NVRAM %u bytes at %05x, token %08x; starting the chip's ARM core\n", nvlen, nvaddr, token);
    /* active: clear SDIO core interrupts, release the ARM (brcmf_chip_cm3_set_active) */
    bp_write32(sdiod->base + 0x20, 0xFFFFFFFFU);
    if (core_reset(arm, 0, 0, 0)) { uart_printf("wifi: ARM release failed (status %08x)\n", last_status); return -1; }
    /* as brcmfmac's firmware callback: force HT, announce SDPCM v4, enable F2 */
    if (!read8(1, 0x1000E, &csr)) write8(1, 0x1000E, (uint8_t)(csr | 0x02));
    bp_write32(sdiod->base + 0x48, 4U << 16);
    write8(0, 0x02, 0x06);
    start = timer_ticks();
    while (!read8(0, 0x03, &rdy) && !(rdy & 0x04) && timer_elapsed_ms(start) < 3000) timer_delay_ms(5);
    if (!(rdy & 0x04)) { uart_printf("wifi: firmware did not enable function 2 (I/O ready %02x)\n", rdy); return -1; }
    write8(0, 0x210, (uint8_t)(F2_BLOCK & 0xFF));              /* function 2 block size 512 */
    write8(0, 0x211, (uint8_t)(F2_BLOCK >> 8));
    write8(1, 0x10008, 8);                                     /* watermark */
    if (!bp_read32(sdiod->base + 0x4C, &v))
        uart_printf("wifi: chip firmware running after %u ms (mailbox %08x)\n", timer_elapsed_ms(start), v);
    tx_seq = 0;
    tx_max = 4;                                                /* until the first header tells */
    fw_running = 1;
    rc = wl_get_var("ver", ver, sizeof(ver));
    ver[sizeof(ver) - 1] = 0;
    for (i = 0; ver[i]; i++) if (ver[i] == '\n' || ver[i] == '\r') ver[i] = ' ';
    if (rc) { uart_printf("wifi: 'ver' request failed (%d)\n", rc); return -1; }
    uart_printf("wifi: firmware says: %s\n", (char *)ver);
    if (!wl_get_var("cur_etheraddr", ver, 16)) {
        char ms[18];
        memcpy(mac_addr, ver, 6);
        mac_str(mac_addr, ms);
        uart_printf("wifi: MAC address %s\n", ms);
    }
    wl_set_var_int("bus:txglom", 0);                           /* disable dongle SDIO TX glomming (superframes) */
    wl_set_var_int("bus:rxglom", 0);                           /* one frame per transfer, no superframes */
    wl_set_var_int("ampdu_hostreorder", 0);                    /* reorder AMPDUs in-dongle, not on host */
    wl_set_var_int("mchan", 0);
    wl_set_var_int("mpc", 0);                                  /* keep radio awake */
    wl_set_var_int("allmulti", 1);
    wl_set_var_int("pkt_filter_mode", 0);                      /* don't discard non-matching frames */
    wmode = WIFI_MODE_IDLE;
    return 0;
}

/* --- scan (HP's DHD uses the incremental scan "iscan") -------------------- */

#define SCAN_LEN 2048U
static uint8_t scan_buf[SCAN_LEN + 64];

static void put32(uint8_t *p, uint32_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24); }
static uint32_t get32(const uint8_t *p) { return p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24); }

static int iscan_request(uint16_t action)
{
    static const char name[] = "iscan";
    uint8_t *p = scan_buf, *sp;
    uint32_t i;
    for (i = 0; i < 6 + 72; i++) p[i] = 0;
    for (i = 0; i < sizeof(name); i++) p[i] = (uint8_t)name[i];
    sp = p + sizeof(name);
    put32(sp, 1);                                              /* ISCAN_REQ_VERSION */
    sp[4] = (uint8_t)action; sp[5] = (uint8_t)(action >> 8);   /* 1 start, 3 continue */
    /* wl_scan_params_t at sp + 8: empty SSID, broadcast BSSID, any BSS type */
    for (i = 0; i < 6; i++) sp[8 + 36 + i] = 0xFF;
    sp[8 + 42] = 2;                                            /* DOT11_BSSTYPE_ANY */
    sp[8 + 43] = 0;                                            /* active scan */
    put32(sp + 8 + 44, 0xFFFFFFFFU);                           /* nprobes, active, passive, home time: default */
    put32(sp + 8 + 48, 0xFFFFFFFFU);
    put32(sp + 8 + 52, 0xFFFFFFFFU);
    put32(sp + 8 + 56, 0xFFFFFFFFU);
    put32(sp + 8 + 60, 0);                                     /* all channels */
    return wl_ioctl(WLC_SET_VAR, 1, p, sizeof(name) + 72, 1000);
}

/* Incremental scan; verbose lists every network. count/best (optional) get
   the number of networks and the strongest signal in dBm (0 if none). */
static int scan_run(int verbose, uint32_t *count_out, int *best_out)
{
    uint8_t up[4] = { 0, 0, 0, 0 };
    uint32_t start, status = 2, count = 0, total = 0, off, i, round = 0;
    int rc, best = 0;
    if (!fw_running) { uart_puts("wifi: run 'wifi fw' first\n"); return -1; }
    rc = wl_ioctl(WLC_UP, 1, up, 4, 1000);
    if (rc) uart_printf("wifi: WLC_UP returned %d\n", rc);
    rc = iscan_request(1);
    if (rc) { uart_printf("wifi: iscan start failed (%d)\n", rc); return -1; }
    if (verbose) uart_puts("wifi: scanning...\n");
    start = timer_ticks();
    while (timer_elapsed_ms(start) < 12000) {
        timer_delay_ms(300);
        for (i = 0; i < sizeof(scan_buf); i++) scan_buf[i] = 0;
        for (i = 0; i < 13; i++) scan_buf[i] = (uint8_t)"iscanresults"[i];
        put32(scan_buf + 13 + 4, SCAN_LEN - 16);               /* results.buflen */
        rc = wl_ioctl(WLC_GET_VAR, 0, scan_buf, SCAN_LEN, 2000);
        if (rc) { uart_printf("wifi: iscanresults failed (%d)\n", rc); return -1; }
        status = get32(scan_buf);
        count = get32(scan_buf + 12);
        if (status == 2) continue;                             /* pending */
        for (off = 16, i = 0; i < count && off + 0x52 <= SCAN_LEN; i++) {
            const uint8_t *b = scan_buf + off;
            uint32_t blen = get32(b + 4), sl = b[0x12], k;
            int16_t rssi = (int16_t)(b[0x4E] | (b[0x4F] << 8));
            char ssid[33];
            if (blen < 0x52 || off + blen > SCAN_LEN) break;
            if (sl > 32) sl = 32;
            for (k = 0; k < sl; k++) ssid[k] = (b[0x13 + k] >= 0x20 && b[0x13 + k] < 0x7F) ? (char)b[0x13 + k] : '?';
            ssid[sl] = 0;
            if (verbose)                                       /* capability bit 4: privacy (WEP/WPA) */
                uart_printf("wifi:   %02x:%02x:%02x:%02x:%02x:%02x  ch %2u  %4d dBm  %s  [%s]\n",
                            b[8], b[9], b[10], b[11], b[12], b[13], b[0x48], rssi, sl ? ssid : "(hidden)",
                            (b[0x10] & 0x10U) ? "secured" : "open");
            if (rssi < 0 && (best == 0 || rssi > best)) best = rssi;
            total++;
            off += blen;
        }
        if (status == 1 && round++ < 8) { iscan_request(3); continue; }   /* partial: continue */
        break;
    }
    if (verbose)
        uart_printf("wifi: scan %s, %u networks listed (results status %u, %u event frames seen)\n",
                    status == 0 ? "complete" : "stopped", total, status, events_seen);
    if (count_out) *count_out = total;
    if (best_out) *best_out = best;
    return 0;
}

static int wifi_scan(void) { return scan_run(1, 0, 0); }

int wifi_scan_summary(uint32_t *count, int *best_rssi)
{
    if (wmode == WIFI_MODE_AP) return -1;               /* would disturb the stations */
    if (!fw_running && wifi_fw()) return -1;
    return scan_run(0, count, best_rssi);
}

static int wifi_probe(void)
{
    uint8_t cccr = 0, rdy = 0, csr = 0;
    uint16_t vendor = 0, device = 0;
    uint32_t chipid = 0, start;
    sdio_ready = 0;
    /* Always start from a powered-down module: a card left selected by an
       earlier probe ignores CMD5. */
    wifi_power(0);
    timer_delay_ms(100);
    wifi_power(1);
    uart_puts("wifi: module power cycled (pin 12 low 100 ms, then high), waiting 200 ms\n");
    timer_delay_ms(200);
    sd_block_enable();
    if (sdhc_init() || sdio_enumerate()) return -1;
    if (read8(0, 0x00, &cccr)) { uart_printf("wifi: CCCR read failed (status %08x)\n", last_status); return -1; }
    if (read_manfid(&vendor, &device)) uart_printf("wifi: no manufacturer tuple found in the CIS (status %08x)\n", last_status);
    uart_printf("wifi: CCCR/SDIO revision %02x, vendor %04x device %04x%s\n", cccr, vendor, device,
                vendor == SDIO_VENDOR_BCM ? " (Broadcom)" : "");
    if (write8(0, 0x02, 0x02)) { uart_puts("wifi: enabling function 1 failed\n"); return -1; }
    start = timer_ticks();
    while (!read8(0, 0x03, &rdy) && !(rdy & 0x02) && timer_elapsed_ms(start) < 200) { }
    if (!(rdy & 0x02)) { uart_puts("wifi: function 1 never became ready\n"); return -1; }
    write8(1, 0x1000E, 0x28);                          /* ALP clock request (force HW clock request off) */
    start = timer_ticks();
    while (!read8(1, 0x1000E, &csr) && !(csr & 0x40) && timer_elapsed_ms(start) < 200) { }
    uart_printf("wifi: function 1 ready, chip clock CSR %02x%s\n", csr, (csr & 0x40) ? " (ALP clock available)" : " (no ALP clock yet)");
    /* As brcmfmac does after ALPAvail: force ALP on (FORCE_HW_CLKREQ_OFF |
       FORCE_ALP), wait 65 us, disable the extra SDIO pull-ups (F1 0x1000F). */
    write8(1, 0x1000E, 0x21);
    timer_delay_us(65);
    write8(1, 0x1000F, 0x00);
    sdhc_set_clock_div(0x01);                          /* 48 MHz / 2 = 24 MHz, default speed */
    if (backplane_read32(0x18000000U, &chipid)) { uart_printf("wifi: chip ID read failed (status %08x)\n", last_status); return -1; }
    uart_printf("wifi: ChipCommon chip ID %08x: chip %u (0x%04x) rev %u, %u cores\n", chipid,
                chipid & 0xFFFF, chipid & 0xFFFF, (chipid >> 16) & 0xF, (chipid >> 24) & 0xF);
    sdio_ready = 1;
    return 0;
}

static void wifi_status(void)
{
    uart_printf("wifi: power pin %u: dir %u out %u; SD block reset reg %04x (%s), wrapper %04x\n",
                WIFI_POWER_PIN, GPIO_DIR(WIFI_POWER_PIN) & 1, GPIO_OUTPUT(WIFI_POWER_PIN) & 1,
                SD_RESET_REG, (SD_RESET_REG & SD_RESET_BIT) ? "held in reset" : "running", SDW_CTRL);
    uart_printf("wifi: SD host caps %08x version %08x present %08x\n", SDHC_CAPS, SDHC_VERSION, SDHC_PRESENT);
    uart_printf("wifi: host ctl %08x clock %08x int status %08x enable %08x signal %08x; card %s\n",
                SDHC_HOSTCTL, SDHC_CLOCK, SDHC_INTSTAT, SDHC_INTEN, SDHC_SIGEN,
                sdio_ready ? "identified" : "not identified");
}

/* --- join, access point, link upkeep ------------------------------------ */

int wifi_ensure_running(void) { return fw_running ? 0 : wifi_fw(); }
int wifi_mode(void) { return wmode; }
int wifi_link_up(void) { return wmode == WIFI_MODE_STA || wmode == WIFI_MODE_AP; }
const uint8_t *wifi_mac(void) { return mac_addr; }
const char *wifi_ssid(void) { return cur_ssid; }
int wifi_last_rssi(void) { return last_rssi; }
uint32_t wifi_ap_clients(void) { return ap_clients; }

static int read_rssi(int *dbm)
{
    uint8_t b[4] = { 0, 0, 0, 0 };
    int rc = wl_ioctl(WLC_GET_RSSI, 0, b, 4, 1000);
    if (rc) return rc;
    *dbm = (int)(int32_t)(b[0] | ((uint32_t)b[1] << 8) | ((uint32_t)b[2] << 16) | ((uint32_t)b[3] << 24));
    return 0;
}

/* Events this driver acts on (the mask is 16 bytes, one bit per event). */
static int set_event_mask(void)
{
    static const uint8_t wanted[] = { E_SET_SSID, E_AUTH, E_DEAUTH, E_DEAUTH_IND, E_ASSOC, E_ASSOC_IND,
                                      E_REASSOC_IND, E_DISASSOC, E_DISASSOC_IND, E_LINK, E_PSK_SUP };
    uint8_t mask[16];
    unsigned i;
    memset(mask, 0, sizeof(mask));
    for (i = 0; i < sizeof(wanted); i++) mask[wanted[i] / 8] |= (uint8_t)(1U << (wanted[i] % 8));
    return wl_set_var("event_msgs", mask, sizeof(mask));
}

/* WPA2-PSK (AES) with the passphrase handed to the chip, which runs the
   4-way handshake itself (its firmware has the in-dongle supplicant and
   authenticator), or an open network. As Cypress WHD prepares a join. */
static int set_security(const char *pass, int ap)
{
    int rc;
    if (pass && *pass) {
        uint8_t pmk[68];
        uint32_t n = (uint32_t)strlen(pass);
        if (n < 8 || n > 63) { uart_puts("wifi: a WPA2 passphrase has 8..63 characters\n"); return -1; }
        wl_set_int(WLC_SET_WSEC, 4);                           /* AES */
        rc = wl_set_bsscfg_int("sup_wpa", ap ? 0 : 1);
        if (rc && !ap) uart_printf("wifi: the chip refused its supplicant (sup_wpa: %d)\n", rc);
        if (!ap) {
            wl_set_bsscfg_int("sup_wpa2_eapver", 0xFFFFFFFFU);   /* follow the access point */
            wl_set_bsscfg_int("sup_wpa_tmo", 2500);
        }
        memset(pmk, 0, sizeof(pmk));                           /* wsec_pmk_t, little-endian */
        pmk[0] = (uint8_t)n;
        pmk[2] = 1;                                            /* WSEC_PASSPHRASE */
        memcpy(pmk + 4, pass, n);
        rc = wl_ioctl(WLC_SET_WSEC_PMK, 1, pmk, sizeof(pmk), 1000);
        memset(pmk, 0, sizeof(pmk));
        if (rc) { uart_printf("wifi: the chip refused the passphrase (%d)\n", rc); return -1; }
        wl_set_int(WLC_SET_WPA_AUTH, 0x80);                    /* WPA2_AUTH_PSK */
    } else {
        wl_set_int(WLC_SET_WSEC, 0);
        wl_set_bsscfg_int("sup_wpa", 0);
        wl_set_int(WLC_SET_WPA_AUTH, 0);
    }
    return 0;
}

static int set_ssid(const char *ssid)
{
    uint8_t b[36];
    uint32_t n = (uint32_t)strlen(ssid);
    memset(b, 0, sizeof(b));
    put32le(b, n);
    memcpy(b + 4, ssid, n);
    return wl_ioctl(WLC_SET_SSID, 1, b, sizeof(b), 1000);
}

static void leave_quiet(void)
{
    if (!fw_running) return;
    if (wmode == WIFI_MODE_STA || want_sta) {
        uint8_t scb[10];
        memset(scb, 0, sizeof(scb));
        put32le(scb, 3);                                       /* reason: station leaving */
        wl_ioctl(WLC_DISASSOC, 1, scb, sizeof(scb), 1000);
    }
    if (wmode == WIFI_MODE_AP) {
        wl_set_int(WLC_DOWN, 1);
        wl_set_int(WLC_SET_AP, 0);
        wl_set_int(WLC_UP, 1);
    }
    if (wmode == WIFI_MODE_STA || wmode == WIFI_MODE_AP) net_link_change(0, 0);
    wmode = WIFI_MODE_IDLE;
    want_sta = 0;
    rejoin_active = 0;
    join_in_progress = 0;
    link_flag = 0;
    ap_clients = 0;
    last_rssi = 0;
}

void wifi_leave(void)
{
    int was = wmode;
    leave_quiet();
    if (was == WIFI_MODE_STA) uart_puts("wifi: left the network\n");
    else if (was == WIFI_MODE_AP) uart_puts("wifi: access point stopped\n");
}

/* Wait for events until `done` reports success or a failure was noted. */
static int wait_link(uint32_t timeout_ms, int (*done)(void))
{
    uint32_t start = timer_ticks();
    while (timer_elapsed_ms(start) < timeout_ms) {
        pump_frames();
        if (join_failed) return -1;
        if (done()) return 0;
        watchdog_kick();
        timer_delay_ms(10);
    }
    return -2;
}

static int join_secure;
static int join_done(void) { return link_flag && (keyed || !join_secure); }
static int ap_done(void) { return ap_started; }

static void explain_failure(void)
{
    uart_printf("wifi: join failed (event %u, status %u, reason %u)", fail_event, fail_status, fail_reason);
    if (fail_event == E_PSK_SUP) uart_puts(": the WPA2 handshake failed, probably a wrong passphrase\n");
    else if (fail_event == E_SET_SSID && fail_status == 3) uart_puts(": network not found\n");
    else if (fail_event == E_AUTH) uart_puts(": the access point refused the authentication\n");
    else uart_puts("\n");
}

/* mDNS (224.0.0.251 = 01:00:5e:00:00:fb) has to pass the chip's multicast
   filter, which otherwise lets only broadcasts through. */
static void set_multicast_filter(void)
{
    static const uint8_t list[10] = { 1, 0, 0, 0, 0x01, 0x00, 0x5e, 0x00, 0x00, 0xfb };   /* count (LE), MAC */
    int rc = wl_set_var("mcast_list", list, sizeof(list));
    if (rc) uart_printf("wifi: the chip refused the multicast list (%d); mDNS may not answer\n", rc);
}

static uint32_t join_start_ticks;

/* Everything up to the join request; the chip's events finish it. */
static int join_begin(const char *ssid, const char *pass)
{
    int rc;
    join_secure = pass && *pass;
    uart_printf("wifi: joining \"%s\" (%s)\n", ssid, join_secure ? "WPA2-PSK, handshake by the chip" : "open");
    wl_set_int(WLC_UP, 1);
    set_event_mask();
    set_multicast_filter();
    wl_set_int(WLC_SET_PM, 0);                                 /* no power save: lower latency */
    wl_set_int(WLC_SET_INFRA, 1);
    wl_set_int(WLC_SET_AUTH, 0);                               /* open system authentication */
    if (set_security(pass, 0)) return -1;
    link_flag = keyed = ssid_ok = join_failed = 0;
    join_in_progress = 1;
    strlcpy(cur_ssid, ssid, sizeof(cur_ssid));
    join_start_ticks = timer_ticks();
    rc = set_ssid(ssid);
    if (rc) {
        join_in_progress = 0;
        uart_printf("wifi: the chip refused the join request (%d)\n", rc);
        return -1;
    }
    return 0;
}

/* Linked (and keyed): station mode, DHCP starts from net_poll(). */
static void join_finish(void)
{
    join_in_progress = 0;
    want_sta = 1;
    wmode = WIFI_MODE_STA;
    if (read_rssi(&last_rssi)) last_rssi = 0;
    rssi_timer = timer_ticks();
    uart_printf("wifi: connected to \"%s\" after %u ms, signal %d dBm\n", cur_ssid,
                timer_elapsed_ms(join_start_ticks), last_rssi);
    net_link_change(1, 0);
}

/* A failed or timed-out attempt: tell the chip to stop, keep wanting the network. */
static void join_cancel(void)
{
    uint8_t scb[10];
    join_in_progress = 0;
    memset(scb, 0, sizeof(scb));
    put32le(scb, 3);
    wl_ioctl(WLC_DISASSOC, 1, scb, sizeof(scb), 1000);
    link_flag = 0;
    wmode = WIFI_MODE_IDLE;
}

static void remember_network(const char *ssid, const char *pass)
{
    strlcpy(saved_ssid, ssid, sizeof(saved_ssid));
    strlcpy(saved_pass, pass ? pass : "", sizeof(saved_pass));
    have_saved = 1;
}

int wifi_join(const char *ssid, const char *pass)
{
    uint32_t n = (uint32_t)strlen(ssid), start;
    int rc;
    if (n == 0 || n > 32) { uart_puts("wifi: the network name must have 1..32 characters\n"); return -1; }
    if (!fw_running && wifi_fw()) return -1;
    leave_quiet();
    boot_join = 0;
    if (join_begin(ssid, pass)) return -1;
    rc = wait_link(REJOIN_TRY_MS, join_done);
    join_in_progress = 0;
    if (rc) {
        if (rc == -1) explain_failure();
        else uart_printf("wifi: no link to \"%s\" within 15 s (link %d, keyed %d)\n", ssid, link_flag, keyed);
        want_sta = 1;                                          /* make leave_quiet() cancel the attempt */
        leave_quiet();
        return -1;
    }
    remember_network(ssid, pass);
    join_finish();
    start = timer_ticks();
    while (!net_has_address() && timer_elapsed_ms(start) < 10000U) {
        wifi_poll();
        net_poll();
        watchdog_kick();
        timer_poll();
    }
    if (!net_has_address()) uart_puts("wifi: no address yet; DHCP keeps trying ('net' shows the state)\n");
    return 0;
}

int wifi_start_ap(const char *ssid, const char *pass, uint32_t channel)
{
    uint32_t n = (uint32_t)strlen(ssid);
    int rc;
    if (n == 0 || n > 32) { uart_puts("wifi: the network name must have 1..32 characters\n"); return -1; }
    if (channel < 1 || channel > 13) { uart_puts("wifi: channel 1..13\n"); return -1; }
    if (!fw_running && wifi_fw()) return -1;
    leave_quiet();
    uart_printf("wifi: starting access point \"%s\" on channel %u (%s)\n", ssid, channel,
                pass && *pass ? "WPA2-PSK" : "open");
    wl_set_int(WLC_DOWN, 1);
    wl_set_var_int("apsta", 0);
    rc = wl_set_int(WLC_SET_AP, 1);
    if (rc) { uart_printf("wifi: the chip refused access point mode (%d)\n", rc); wl_set_int(WLC_UP, 1); return -1; }
    wl_set_int(WLC_SET_INFRA, 1);
    wl_set_int(WLC_SET_CHANNEL, channel);
    if (set_security(pass, 1)) { wl_set_int(WLC_SET_AP, 0); wl_set_int(WLC_UP, 1); return -1; }
    set_event_mask();
    wl_set_int(WLC_UP, 1);
    ap_started = ssid_ok = join_failed = 0;
    ap_clients = 0;
    ap_pending = 1;
    strlcpy(cur_ssid, ssid, sizeof(cur_ssid));
    rc = set_ssid(ssid);
    if (rc) uart_printf("wifi: the chip refused the network name (%d)\n", rc);
    else rc = wait_link(5000U, ap_done);
    ap_pending = 0;
    if (rc && rc != -2) {
        if (rc == -1) explain_failure();
        wl_set_int(WLC_DOWN, 1);
        wl_set_int(WLC_SET_AP, 0);
        wl_set_int(WLC_UP, 1);
        return -1;
    }
    if (rc) uart_puts("wifi: the chip did not confirm the access point; carrying on\n");
    wmode = WIFI_MODE_AP;
    uart_printf("wifi: access point \"%s\" is up: join it and ping 192.168.4.1\n", ssid);
    net_link_change(1, 1);
    return 0;
}

static void backoff(void)
{
    rejoin_delay_ms = rejoin_delay_ms < REJOIN_FIRST_MS ? REJOIN_FIRST_MS :
                      rejoin_delay_ms * 2U > REJOIN_MAX_MS ? REJOIN_MAX_MS : rejoin_delay_ms * 2U;
}

/* Background (re)join, from wifi_poll(): start an attempt when it is due,
   then watch the chip's events until it links, fails or times out. */
static void rejoin_poll(void)
{
    if (rejoin_active) {
        if (join_done()) {
            rejoin_active = 0;
            rejoins_ok++;
            rejoin_delay_ms = REJOIN_FIRST_MS;
            join_finish();
        } else if (join_failed || timer_elapsed_ms(rejoin_timer) >= REJOIN_TRY_MS) {
            rejoin_active = 0;
            if (join_failed) explain_failure();
            else uart_printf("wifi: no link to \"%s\" within %u s\n", saved_ssid, REJOIN_TRY_MS / 1000U);
            join_cancel();
            rejoin_timer = timer_ticks();
            backoff();
            uart_printf("wifi: next try in %u s\n", rejoin_delay_ms / 1000U);
        }
        return;
    }
    if (!auto_rejoin || !have_saved || !want_sta || wmode != WIFI_MODE_IDLE || join_in_progress ||
        timer_elapsed_ms(rejoin_timer) < rejoin_delay_ms)
        return;
    rejoin_attempts++;
    uart_printf("wifi: rejoining in the background (attempt %u)\n", rejoin_attempts);
    rejoin_timer = timer_ticks();
    if (join_begin(saved_ssid, saved_pass) == 0) {
        rejoin_active = 1;
    } else {
        backoff();
    }
}

/* Printer builds: join the default network shortly after power-on. Loading
   the chip firmware blocks for a moment; the join itself runs in the background. */
static void boot_join_poll(void)
{
    if (!boot_join || timer_uptime_ms() < BOOT_JOIN_MS) return;
    boot_join = 0;
    if (wmode == WIFI_MODE_STA || wmode == WIFI_MODE_AP || join_in_progress) return;
    if (!DEFAULT_WIFI_SSID[0]) { uart_puts("wifi: no default network (fw/app/wifi_secrets.h), not joining\n"); return; }
    uart_printf("wifi: joining the default network \"%s\" ('wifi auto off' stops the retries)\n", DEFAULT_WIFI_SSID);
    if (!fw_running && wifi_fw()) return;
    remember_network(DEFAULT_WIFI_SSID, DEFAULT_WIFI_PASS);
    want_sta = 1;
    rejoin_delay_ms = 0;
    rejoin_timer = timer_ticks();
}

void wifi_poll(void)
{
    static uint32_t last_poll;
    boot_join_poll();
    if (!fw_running) return;
    rejoin_poll();
    if (link_back) {
        link_back = 0;
        if (wmode == WIFI_MODE_STA) net_link_change(1, 0);
    }
    rxq_deliver();
    if (timer_elapsed_us(last_poll) < 2000U) return;           /* look at the chip every 2 ms */
    last_poll = timer_ticks();
    {
        unsigned guard;
        for (guard = 0; guard < 16; guard++) {
            int fl = f2_read_frame(guard > 0);
            if (fl <= 0) break;
            if ((DMA_PTR[RX_OFF + 5] & 0x0FU) != 0) rx_dispatch((uint32_t)fl);
            rxq_deliver();
        }
    }
    if (wmode == WIFI_MODE_STA && timer_elapsed_ms(rssi_timer) >= 5000U) {
        int dbm;
        rssi_timer = timer_ticks();
        if (!read_rssi(&dbm)) last_rssi = dbm;
        rxq_deliver();
    }
}

void wifi_shutdown(void)
{
    if (wmode == WIFI_MODE_STA || wmode == WIFI_MODE_AP) net_link_change(0, 0);
    SDHC_CLOCK = 0;
    SDHC_HOSTCTL = 0;
    wifi_power(0);
    sdio_ready = 0;
    fw_running = 0;
    wmode = WIFI_MODE_OFF;
    want_sta = 0;
    link_back = 0;
    rejoin_active = 0;
    join_in_progress = 0;
    link_flag = 0;
    last_rssi = 0;
    ap_clients = 0;
}

static void wifi_print_auto(void)
{
    uart_printf("wifi: auto rejoin %s", auto_rejoin ? "on" : "off");
    if (have_saved) uart_printf(" for \"%s\"", saved_ssid);
    uart_printf("; links lost %u, background attempts %u, rejoined %u", links_lost, rejoin_attempts, rejoins_ok);
    if (rejoin_active) uart_puts("; attempt running");
    else if (auto_rejoin && have_saved && want_sta && wmode == WIFI_MODE_IDLE) {
        uint32_t el = timer_elapsed_ms(rejoin_timer);
        uart_printf("; next try in %u s", el >= rejoin_delay_ms ? 0U : (rejoin_delay_ms - el + 999U) / 1000U);
    }
    uart_puts("\n");
}

void wifi_auto_info(int *on, uint32_t *lost, uint32_t *rejoined)
{
    *on = auto_rejoin;
    *lost = links_lost;
    *rejoined = rejoins_ok;
}

int cmd_wifi(int argc, char **argv)
{
    static const char *const mode_names[] = { "off", "idle (chip firmware running)", "station", "access point" };
    if (argc == 2 && !strcmp(argv[1], "probe")) {
        if (wmode == WIFI_MODE_STA || wmode == WIFI_MODE_AP) net_link_change(0, 0);
        wmode = WIFI_MODE_OFF;
        fw_running = 0;
        return wifi_probe();
    }
    if (argc == 2 && !strcmp(argv[1], "fw")) return wifi_fw();
    if (argc == 2 && !strcmp(argv[1], "scan")) return wifi_scan();
    if (argc == 2 && !strcmp(argv[1], "join")) {
        if (!DEFAULT_WIFI_SSID[0]) { uart_puts("wifi: no default network, use: wifi join <ssid> [pass]\n"); return -1; }
        return wifi_join(DEFAULT_WIFI_SSID, DEFAULT_WIFI_PASS);
    }
    if (argc >= 3 && argc <= 4 && !strcmp(argv[1], "join")) return wifi_join(argv[2], argc == 4 ? argv[3] : 0);
    if (argc >= 2 && argc <= 5 && !strcmp(argv[1], "ap")) {
        char name[33];
        uint32_t ch = 6;
        const uint8_t *m = mac_addr;
        if (argc == 5 && (parse_u32(argv[4], &ch) || ch < 1 || ch > 13)) { uart_puts("usage: wifi ap [ssid] [pass|-] [channel]\n"); return -1; }
        if (argc >= 3) strlcpy(name, argv[2], sizeof(name));
        else {
            if (!fw_running && wifi_fw()) return -1;
            snprintf(name, sizeof(name), "HPDJ-%02X%02X", m[4], m[5]);
        }
        return wifi_start_ap(name, (argc >= 4 && strcmp(argv[3], "-")) ? argv[3] : 0, ch);
    }
    if (argc == 2 && !strcmp(argv[1], "leave")) { wifi_leave(); return 0; }
    if (argc >= 2 && argc <= 3 && !strcmp(argv[1], "auto")) {
        if (argc == 3 && !strcmp(argv[2], "on")) auto_rejoin = 1;
        else if (argc == 3 && !strcmp(argv[2], "off")) { auto_rejoin = 0; boot_join = 0; rejoin_active = 0; }
        else if (argc == 3) { uart_puts("usage: wifi auto [on|off]\n"); return -1; }
        wifi_print_auto();
        return 0;
    }
    if (argc == 2 && !strcmp(argv[1], "rssi")) {
        int dbm;
        if (wmode != WIFI_MODE_STA) { uart_puts("wifi: not connected to a network\n"); return -1; }
        if (read_rssi(&dbm)) { uart_puts("wifi: RSSI request failed\n"); return -1; }
        last_rssi = dbm;
        uart_printf("wifi: signal %d dBm from \"%s\"\n", dbm, cur_ssid);
        return 0;
    }
    if (argc == 2 && !strcmp(argv[1], "off")) {
        wifi_shutdown();
        uart_puts("wifi: SD clock and bus power off, module power off\n");
        return 0;
    }
    if (argc >= 3 && argc <= 4 && !strcmp(argv[1], "iovar")) {
        if (!fw_running) { uart_puts("wifi: chip firmware not running (wifi fw or wifi join first)\n"); return -1; }
        if (argc == 3) {
            uint8_t buf[64];
            uint32_t val = 0;
            int rc = wl_get_var(argv[2], buf, sizeof(buf));
            if (rc) { uart_printf("wifi: iovar \"%s\" get error %d\n", argv[2], rc); return -1; }
            val = (uint32_t)buf[0] | ((uint32_t)buf[1] << 8) | ((uint32_t)buf[2] << 16) | ((uint32_t)buf[3] << 24);
            uart_printf("wifi: iovar \"%s\" = %u (0x%x)\n", argv[2], val, val);
            return 0;
        } else {
            uint32_t val = 0;
            int rc;
            if (parse_u32(argv[3], &val)) { uart_puts("wifi: iovar value must be uint32\n"); return -1; }
            rc = wl_set_var_int(argv[2], val);
            if (rc) { uart_printf("wifi: iovar \"%s\" set error %d\n", argv[2], rc); return -1; }
            uart_printf("wifi: iovar \"%s\" set to %u (0x%x)\n", argv[2], val, val);
            return 0;
        }
    }
    if (argc == 1 || (argc == 2 && !strcmp(argv[1], "status"))) {
        wifi_status();
        uart_printf("wifi: mode %s", mode_names[wmode]);
        if (wmode == WIFI_MODE_STA) uart_printf(", network \"%s\", signal %d dBm", cur_ssid, last_rssi);
        if (wmode == WIFI_MODE_AP) uart_printf(", network \"%s\", %u stations", cur_ssid, ap_clients);
        uart_printf("; frames in %u out %u, dropped %u, send errors %u, events %u\n",
                    data_in, data_out, rxq_drops, data_errors, events_seen);
        wifi_print_auto();
        if (rx_glom_frames || rx_bad_frames)
            uart_printf("wifi: rx glom %u, bad hdr %u (last %08x), tx seq %u max %u\n",
                        rx_glom_frames, rx_bad_frames, last_rx_hdr, tx_seq, tx_max);
        return 0;
    }
    uart_puts("usage: wifi [status] | probe | fw | scan | join [ssid] [passphrase] | ap [ssid] [passphrase|-] [channel] | "
              "leave | auto [on|off] | rssi | iovar <name> [val] | off\n");
    return -1;
}
