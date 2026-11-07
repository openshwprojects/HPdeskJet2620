/*
 * W25Q128JV command-path driver on the AXON SPI controller.
 *
 * Lesson from the first boot on the real printer: reading RX after every byte
 * with control bit 5 always set returned the JEDEC id one bit off (de8030 for
 * ef4018), while HP's own stage-1, on the same board, reads ef4018 with its
 * identification pattern (routine 0xa000cf12 / helper 0xa000ceba):
 *     (cmd, r1=0, hold) (00, r1=0, hold) (00, r1=1, hold) (00, r1=0, release)
 * where bit 5 = !r1 and RX is read once, after the final transfer. Every read
 * here therefore uses exactly that data phase (three bytes per CS cycle), and
 * command/address bytes are plain bit-5-set held transfers like HP's opcode.
 * Transmit-only commands (WREN, WRDI, erase, program) never read RX at all.
 *
 * Writes are only allowed once calibration has (1) read the known JEDEC id,
 * (2) read back flash content through the command path that matches the
 * memory-mapped window at four addresses (proving opcode, address and data
 * phases), and only inside the application slots.
 */
#include "hw.h"
#include "config.h"
#include "layout.h"
#include "spi_flash.h"
#include "cache.h"
#include "timer.h"
#include "uart.h"
#include "libmini.h"

#define CMD_WRDI   0x04U
#define CMD_WREN   0x06U
#define CMD_RDSR1  0x05U
#define CMD_READ   0x03U
#define CMD_PP     0x02U
#define CMD_SE     0x20U
#define CMD_JEDEC  0x9FU
#define SR1_WIP    0x01U
#define SR1_WEL    0x02U

static int spi_mode = SPI_MODE_BYTE_FINAL;   /* framing of the final transfer (see spi_send) */
static int spi_mode_detected;     /* calibration passed: id + window-verified reads */
static int spi_mode_forced;       /* operator chose the framing with "flash mode" */
static uint32_t spi_wait_errors;
static uint32_t window_stale_sectors;

void flash_init(void) { spi_mode = SPI_MODE_BYTE_FINAL; spi_mode_detected = 0; spi_mode_forced = 0; spi_wait_errors = 0; }
void flash_set_mode(int mode) { spi_mode = mode ? SPI_MODE_WIDE_FINAL : SPI_MODE_BYTE_FINAL; spi_mode_forced = 1; }
int  flash_get_mode(void) { return spi_mode; }
int  flash_mode_was_detected(void) { return spi_mode_detected; }
const char *flash_mode_name(int mode) { return mode ? "wide-final" : "byte-final"; }
uint32_t flash_bus_errors(void) { return spi_wait_errors; }
uint32_t flash_window_stale_sectors(void) { return window_stale_sectors; }

/*
 * Wait for the controller state HP's own driver (0xa000ceba) waits for:
 * 13 after a transfer that keeps CS asserted, 0 after one that releases it.
 * Bounded so an unexpected status encoding degrades into an error count
 * instead of a hang.
 */
static void spi_wait_state(uint32_t expected)
{
    uint32_t spins = 0;
    while (SPI_STATUS_STATE(SPI_STATUS) != expected) {
        watchdog_kick();
        if (++spins > SPI_WAIT_SPINS) {
            spi_wait_errors++;
            return;
        }
    }
}

/* One transfer exactly like HP's helper: keep the boot code's upper control
   bits, select device 0, bit 5 clear only for a "receive" transfer (r1=1),
   CS held unless this transfer ends the command. RX is not read here. */
static void spi_xfer(uint8_t tx, int receive, int hold)
{
    uint32_t ctrl = SPI_CTRL & 0xFFFFU;
    ctrl &= ~(SPI_CTRL_HOLD_CS | SPI_CTRL_BYTE | SPI_CTRL_DEVICE(3));
    ctrl |= SPI_CTRL_DEVICE(0) | SPI_CTRL_CFG | (receive ? 0 : SPI_CTRL_BYTE) | (hold ? SPI_CTRL_HOLD_CS : 0);
    SPI_CTRL = ctrl;
    SPI_TX = (uint32_t)tx << 24;
    spi_wait_state(hold ? SPI_STATE_HELD : 0);
}

/* CS-only release, needed only under the "wide-final" serializer hypothesis. */
static void spi_release(void)
{
    uint32_t ctrl = SPI_CTRL & 0xFFFFU;
    ctrl &= ~(SPI_CTRL_HOLD_CS | SPI_CTRL_DEVICE(3));
    ctrl |= SPI_CTRL_DEVICE(0) | SPI_CTRL_CFG | SPI_CTRL_BYTE;
    SPI_CTRL = ctrl;
    SPI_TX = 0;
    spi_wait_state(0);
}

/* Transmit-only command: header bytes then data bytes, CS released after the
   last byte (byte-final: the last transfer releases; wide-final: an extra
   CS-only release). */
static void spi_send(const uint8_t *hdr, uint32_t hn, const uint8_t *data, uint32_t dn)
{
    uint32_t total = hn + dn, i;
    for (i = 0; i < total; i++) {
        uint8_t b = i < hn ? hdr[i] : data[i - hn];
        int last = (i + 1 == total);
        spi_xfer(b, 0, spi_mode == SPI_MODE_BYTE_FINAL ? !last : 1);
    }
    if (spi_mode == SPI_MODE_WIDE_FINAL) spi_release();
}

/* HP's receive pattern: prefix bytes, then three data transfers (bit 5
   set/clear/set, the last releasing CS); the 24-bit result is read from RX
   once, after the final transfer. Works under both serializer hypotheses and
   is the only receive sequence proven on the printer. */
static uint32_t spi_read3(const uint8_t *prefix, uint32_t n)
{
    uint32_t i;
    for (i = 0; i < n; i++) spi_xfer(prefix[i], 0, 1);
    spi_xfer(0, 0, 1);
    spi_xfer(0, 1, 1);
    spi_xfer(0, 0, 0);
    return SPI_RX & 0x00FFFFFFU;
}

uint32_t flash_read_jedec_id(void)
{
    uint8_t cmd = CMD_JEDEC;
    return spi_read3(&cmd, 1);
}

/* Status register 1. The chip repeats it for every clocked byte, so the three
   bytes must agree; a disagreement (e.g. WIP changing mid-read) reads as 0xFF,
   which callers treat as "busy / not usable". */
uint8_t flash_read_status1(void)
{
    uint8_t cmd = CMD_RDSR1;
    uint32_t v = spi_read3(&cmd, 1);
    uint8_t a = (uint8_t)(v >> 16), b = (uint8_t)(v >> 8), c = (uint8_t)v;
    return (a == b && b == c) ? c : 0xFF;
}

void flash_read(uint32_t addr, void *buf, uint32_t len)
{
    uint8_t *p = buf;
    while (len) {
        uint8_t pre[4] = { CMD_READ, (uint8_t)(addr >> 16), (uint8_t)(addr >> 8), (uint8_t)addr };
        uint32_t v = spi_read3(pre, 4), n = len < 3 ? len : 3, i;
        for (i = 0; i < n; i++) p[i] = (uint8_t)(v >> (16 - 8 * i));
        p += n; addr += n; len -= n;
    }
}

/* The previous driver's method (bit 5 always set, RX after every byte). Only
   used to detect whether a CS-releasing transfer clocks a byte: if it does,
   the last two bytes of the id differ (40 vs 18, whatever the bit alignment);
   if it does not, the last byte repeats. */
static uint32_t spi_jedec_bytewise(void)
{
    uint32_t r1, r2, r3;
    spi_xfer(CMD_JEDEC, 0, 1);
    spi_xfer(0, 0, 1); r1 = SPI_RX & 0xFFU;
    spi_xfer(0, 0, 1); r2 = SPI_RX & 0xFFU;
    spi_xfer(0, 0, 0); r3 = SPI_RX & 0xFFU;
    return (r1 << 16) | (r2 << 8) | r3;
}

/* Calibration (read-only). Returns the adopted framing, or -1 (writes stay disabled). */
int flash_autodetect_mode(void)
{
    static const uint32_t probes[] = { FLASH_BL_OFFSET, FLASH_BL_OFFSET + 0x0D, 0x7FFF01, 0xC94001 };
    uint8_t buf[4];
    uint32_t i, bw;
    spi_mode = SPI_MODE_BYTE_FINAL;
    spi_mode_forced = 0;
    spi_mode_detected = 0;
    if (flash_read_jedec_id() != FLASH_JEDEC_W25Q128) return -1;
    bw = spi_jedec_bytewise();
    if ((bw & 0xFFU) == ((bw >> 8) & 0xFFU)) spi_mode = SPI_MODE_WIDE_FINAL;
    for (i = 0; i < sizeof(probes) / sizeof(probes[0]); i++) {
        flash_read(probes[i], buf, 4);
        dcache_invalidate_range(probes[i], probes[i] + 4);
        if (memcmp(buf, flash_mmap(probes[i]), 4) != 0) return -1;
    }
    spi_mode_detected = 1;
    return spi_mode;
}

void flash_diag(void)
{
    uint32_t r[4];
    uint8_t pre[4] = { CMD_READ, (uint8_t)(FLASH_BL_OFFSET >> 16), (uint8_t)(FLASH_BL_OFFSET >> 8),
                       (uint8_t)FLASH_BL_OFFSET };
    const uint8_t *win = flash_mmap(FLASH_BL_OFFSET);
    uint32_t errors = spi_wait_errors;

    uart_printf("spi regs : ctrl %08x status %08x rx %08x windows %04x %04x %04x\n", SPI_CTRL, SPI_STATUS, SPI_RX,
                SPI_WINDOW(0), SPI_WINDOW(1), SPI_WINDOW(2));
    spi_xfer(CMD_JEDEC, 0, 1); spi_xfer(0, 0, 1); spi_xfer(0, 1, 1); spi_xfer(0, 0, 0);
    uart_printf("E1 9F hp pattern, rx at end    : %08x (expect ..ef4018)\n", SPI_RX);
    spi_xfer(CMD_JEDEC, 0, 1); r[0] = SPI_RX; spi_xfer(0, 0, 1); r[1] = SPI_RX;
    spi_xfer(0, 1, 1); r[2] = SPI_RX; spi_xfer(0, 0, 0); r[3] = SPI_RX;
    uart_printf("E2 9F hp pattern, rx each      : %08x %08x %08x %08x\n", r[0], r[1], r[2], r[3]);
    spi_xfer(CMD_JEDEC, 0, 1); spi_xfer(0, 0, 1); spi_xfer(0, 0, 1); spi_xfer(0, 0, 0);
    uart_printf("E3 9F bit5 always set, at end  : %08x\n", SPI_RX);
    spi_xfer(CMD_JEDEC, 0, 1); r[0] = SPI_RX; spi_xfer(0, 0, 1); r[1] = SPI_RX;
    spi_xfer(0, 0, 1); r[2] = SPI_RX; spi_xfer(0, 0, 0); r[3] = SPI_RX;
    uart_printf("E4 9F bit5 always set, rx each : %08x %08x %08x %08x (old driver)\n", r[0], r[1], r[2], r[3]);
    uart_printf("E5 03 @%06x hp pattern       : %06x (window %02x%02x%02x)\n", FLASH_BL_OFFSET, spi_read3(pre, 4),
                win[0], win[1], win[2]);
    spi_xfer(pre[0], 0, 1); spi_xfer(pre[1], 0, 1); spi_xfer(pre[2], 0, 1); spi_xfer(pre[3], 0, 1);
    spi_xfer(0, 1, 1); spi_xfer(0, 1, 1); spi_xfer(0, 1, 0);
    uart_printf("E6 03 bit5 always clear, at end: %08x (window %02x%02x%02x%02x)\n", SPI_RX, win[0], win[1], win[2],
                win[3]);
    spi_xfer(pre[0], 0, 1); spi_xfer(pre[1], 0, 1); spi_xfer(pre[2], 0, 1); spi_xfer(pre[3], 0, 1);
    spi_xfer(0, 0, 1); r[0] = SPI_RX; spi_xfer(0, 0, 1); r[1] = SPI_RX; spi_xfer(0, 0, 0); r[2] = SPI_RX;
    uart_printf("E7 03 bit5 set, rx each        : %08x %08x %08x\n", r[0], r[1], r[2]);
    uart_printf("E8 05 status hp pattern        : %06x\n", spi_read3((const uint8_t[]){ CMD_RDSR1 }, 1));
    uart_printf("bytewise id %06x, calibration %s, framing %s, bus errors during diag %u\n", spi_jedec_bytewise(),
                flash_autodetect_mode() >= 0 ? "passed" : "FAILED", flash_mode_name(spi_mode),
                spi_wait_errors - errors);
}

int flash_write_enable(void)
{
    uint32_t errors = spi_wait_errors;
    uint8_t sr;
    /* No write unless calibration proved the command path, or the operator
       forced a framing deliberately. */
    if (!spi_mode_detected && !spi_mode_forced) return FLASH_ERR_UNVERIFIED;
    sr = flash_read_status1();
    if (spi_wait_errors != errors) return FLASH_ERR_BUS;
    if (sr == 0xFF || (sr & SR1_WIP)) return FLASH_ERR_BUS;
    if (sr & SR1_WEL) {
        /* Left set by an earlier attempt: clearing it first also proves that
           single-byte commands are framed correctly. */
        uint8_t c = CMD_WRDI;
        spi_send(&c, 1, 0, 0);
        sr = flash_read_status1();
        if (sr == 0xFF || (sr & SR1_WEL)) return FLASH_ERR_WEL;
    }
    {
        uint8_t c = CMD_WREN;
        spi_send(&c, 1, 0, 0);
    }
    sr = flash_read_status1();
    if (spi_wait_errors != errors) return FLASH_ERR_BUS;
    return (sr != 0xFF && (sr & SR1_WEL)) ? FLASH_OK : FLASH_ERR_WEL;
}

int flash_wait_ready(uint32_t timeout_ms)
{
    uint32_t start = timer_ticks();
    uint32_t errors = spi_wait_errors;
    while (flash_read_status1() & SR1_WIP) {      /* 0xFF (inconsistent read) counts as busy */
        watchdog_kick();
        if (spi_wait_errors != errors) return FLASH_ERR_BUS;
        if (timer_elapsed_ms(start) >= timeout_ms) return FLASH_ERR_TIMEOUT;
    }
    return spi_wait_errors != errors ? FLASH_ERR_BUS : FLASH_OK;
}

/* Only the two application slots are ever written. Everything else (stage-1,
   this bootloader, HP's leftover regions the stage-1 loader still consults)
   is refused. */
static int check_writable(uint32_t addr, uint32_t len)
{
    uint32_t end = addr + len;
    if (end < addr) return FLASH_ERR_RANGE;
    if (addr < FLASH_PROTECT_END) return FLASH_ERR_PROTECTED;
    if (addr < FLASH_SLOT_A_OFFSET || end > FLASH_SLOT_B_OFFSET + FLASH_SLOT_SIZE) return FLASH_ERR_RANGE;
    return FLASH_OK;
}

/* Verify `len` bytes at `addr` against `expect` (or against 0xFF when expect
   is NULL). The memory-mapped window is checked first; if it disagrees, the
   bytes are re-read through the SPI read command. A window that lags behind
   the chip is counted and tolerated, a chip that holds wrong data is an error. */
static int verify_flash(uint32_t addr, const uint8_t *expect, uint32_t len)
{
    static uint8_t readback[FLASH_SECTOR_SIZE];
    const uint8_t *window = flash_mmap(addr);
    uint32_t i;
    int window_ok = 1;
    dcache_invalidate_range(addr, addr + len);
    if (expect) {
        window_ok = memcmp(window, expect, len) == 0;
    } else {
        for (i = 0; i < len; i++) if (window[i] != 0xFF) { window_ok = 0; break; }
    }
    if (window_ok) return FLASH_OK;
    flash_read(addr, readback, len);
    if (expect) {
        if (memcmp(readback, expect, len) != 0) return FLASH_ERR_VERIFY;
    } else {
        for (i = 0; i < len; i++) if (readback[i] != 0xFF) return FLASH_ERR_VERIFY;
    }
    window_stale_sectors++;
    return FLASH_OK;
}

int flash_erase_sector(uint32_t addr)
{
    int rc;
    uint8_t cmd[4];
    if (addr & (FLASH_SECTOR_SIZE - 1)) return FLASH_ERR_ALIGN;
    if ((rc = check_writable(addr, FLASH_SECTOR_SIZE)) != FLASH_OK) return rc;
    if ((rc = flash_write_enable()) != FLASH_OK) return rc;
    cmd[0] = CMD_SE; cmd[1] = (uint8_t)(addr >> 16); cmd[2] = (uint8_t)(addr >> 8); cmd[3] = (uint8_t)addr;
    spi_send(cmd, 4, 0, 0);
    if ((rc = flash_wait_ready(FLASH_ERASE_TIMEOUT_MS)) != FLASH_OK) return rc;
    return verify_flash(addr, 0, FLASH_SECTOR_SIZE);
}

int flash_program_page(uint32_t addr, const void *data, uint32_t len)
{
    int rc;
    uint8_t cmd[4];
    if (len == 0 || len > FLASH_PAGE_SIZE) return FLASH_ERR_RANGE;
    if ((addr & (FLASH_PAGE_SIZE - 1)) + len > FLASH_PAGE_SIZE) return FLASH_ERR_ALIGN;
    if ((rc = check_writable(addr, len)) != FLASH_OK) return rc;
    if ((rc = flash_write_enable()) != FLASH_OK) return rc;
    cmd[0] = CMD_PP; cmd[1] = (uint8_t)(addr >> 16); cmd[2] = (uint8_t)(addr >> 8); cmd[3] = (uint8_t)addr;
    spi_send(cmd, 4, data, len);
    return flash_wait_ready(FLASH_PROGRAM_TIMEOUT_MS);
}

int flash_write_region(uint32_t addr, const void *data, uint32_t len,
                       void (*progress)(uint32_t done, uint32_t total))
{
    const uint8_t *p = data;
    uint32_t done = 0;
    int rc;
    if (addr & (FLASH_SECTOR_SIZE - 1)) return FLASH_ERR_ALIGN;
    if ((rc = check_writable(addr, len)) != FLASH_OK) return rc;
    while (done < len) {
        uint32_t sector = addr + done;
        uint32_t chunk = len - done < FLASH_SECTOR_SIZE ? len - done : FLASH_SECTOR_SIZE;
        uint32_t off;
        if ((rc = flash_erase_sector(sector)) != FLASH_OK) return rc;
        for (off = 0; off < chunk; off += FLASH_PAGE_SIZE) {
            uint32_t n = chunk - off < FLASH_PAGE_SIZE ? chunk - off : FLASH_PAGE_SIZE;
            if ((rc = flash_program_page(sector + off, p + done + off, n)) != FLASH_OK) return rc;
        }
        if ((rc = verify_flash(sector, p + done, chunk)) != FLASH_OK) return rc;
        done += chunk;
        if (progress) progress(done, len);
    }
    return FLASH_OK;
}

const char *flash_err_str(int err)
{
    switch (err) {
    case FLASH_OK: return "ok";
    case FLASH_ERR_PROTECTED: return "address is write-protected (stage-1/bootloader region)";
    case FLASH_ERR_ALIGN: return "misaligned address";
    case FLASH_ERR_RANGE: return "outside the application slots (0x040000..0x13ffff)";
    case FLASH_ERR_UNVERIFIED: return "SPI command path not verified (see 'flash diag'); writes disabled";
    case FLASH_ERR_WEL: return "write-enable latch did not set (SPI write path unverified)";
    case FLASH_ERR_TIMEOUT: return "flash busy timeout";
    case FLASH_ERR_VERIFY: return "read-back verify failed (chip content differs)";
    case FLASH_ERR_BUS: return "SPI status not usable (controller state or wrong 'flash mode'); nothing written";
    default: return "unknown error";
    }
}
