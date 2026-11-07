/*
 * Run an application image from RAM, from the running application: no
 * bootloader, no flash write, no CH341 or serial adapter needed (the image can
 * come over USB or Wi-Fi). Like the bootloader's "run": the image is checked,
 * copied to its load address and started with a bootinfo block that says
 * "running from RAM"; a power cycle (or the watchdog) returns to the flash
 * slots.
 *
 * The copy overwrites the running application, so the last steps run in a
 * small trampoline (ramrun.S) copied to free DRAM first. Before that, while
 * this code is still intact: motors off, USB off (its DMA writes into DRAM),
 * the watchdog off, the staged image, the bootinfo block and this
 * application's data and stack cleaned out of the D-cache.
 */
#include "ramrun.h"
#include "hw.h"
#include "layout.h"
#include "image.h"
#include "slots.h"
#include "bootinfo.h"
#include "cache.h"
#include "uart.h"
#include "fmt.h"
#include "timer.h"
#include "xmodem.h"
#include "libmini.h"
#include "periph_cmds.h"
#include "usbcon.h"

#define TRAMP_RW        0x20490000U         /* free DRAM after the staging buffer */
#define TRAMP_EXEC      0xA0490000U
#define UNCACHED(a)     (((uint32_t)(a) & 0x03FFFFFFU) | 0x60000000U)
#define CLEAN_FROM      0x20000000U         /* bootinfo, data, stack: everything below 2 MiB */
#define CLEAN_TO        0x20200000U

extern const uint8_t ramrun_tramp_start[], ramrun_tramp_end[];
typedef void (*tramp_fn)(uint32_t src, uint32_t dst, uint32_t len, uint32_t entry);

static uint32_t staged_len;

int ramrun_check(uint32_t len)
{
    const image_header_t *hdr = (const image_header_t *)STAGING_ADDR;
    int st = slot_validate_staged((const uint8_t *)STAGING_ADDR, len);
    if (st != IMG_OK) {
        uart_printf("ramrun: image refused: %s\n", image_status_str(st));
        return -1;
    }
    if (hdr->load_address != APP_LOAD_ADDR || hdr->entry_address < APP_LOAD_ADDR ||
        hdr->entry_address >= APP_LOAD_ADDR + hdr->payload_size) {
        uart_puts("ramrun: image refused: load or entry address outside the application area\n");
        return -1;
    }
    staged_len = len;
    return 0;
}

int ramrun_execute(void)
{
    const image_header_t *hdr = (const image_header_t *)STAGING_ADDR;
    uint32_t n = (uint32_t)(ramrun_tramp_end - ramrun_tramp_start), payload, entry;
    bootinfo_t bi;
    if (!staged_len || ramrun_check(staged_len)) return -1;
    payload = (hdr->payload_size + 3U) & ~3U;
    entry = hdr->entry_address;
    uart_printf("ramrun: starting v%u.%u.%u seq %u from RAM (%u bytes, entry %08x); "
                "a power cycle returns to the flash slots\n", VERSION_MAJOR(hdr->version),
                VERSION_MINOR(hdr->version), VERSION_PATCH(hdr->version), hdr->sequence, hdr->payload_size, entry);
    uart_flush();

    motor_api_stop();
    usbcon_stop();
    watchdog_disable();
    __asm__ volatile ("msr cpsr_c, #0xD3" ::: "memory");

    /* The new application reads this block (r0) like after the bootloader. */
    memcpy(&bi, (const void *)BOOTINFO, sizeof(bi));
    if (bi.magic != BOOTINFO_MAGIC) { memset(&bi, 0, sizeof(bi)); bi.magic = BOOTINFO_MAGIC; }
    bi.active_slot = BOOTINFO_SLOT_RAM;
    bi.app_version = hdr->version;
    bi.app_sequence = hdr->sequence;
    bi.boot_count++;
    bi.boot_reason = BOOT_REASON_RAM;
    memcpy((void *)BOOTINFO, &bi, sizeof(bi));

    memcpy((void *)TRAMP_RW, ramrun_tramp_start, n);
    cache_sync_code(TRAMP_RW, TRAMP_RW + n, TRAMP_EXEC, TRAMP_EXEC + n);
    dcache_clean_range(STAGING_ADDR, STAGING_ADDR + IMAGE_HEADER_SIZE + payload);
    dcache_clean_range(CLEAN_FROM, CLEAN_TO);
    ((tramp_fn)TRAMP_EXEC)(UNCACHED(STAGING_ADDR + IMAGE_HEADER_SIZE), UNCACHED(APP_LOAD_ADDR), payload, entry);
    return -1;                                       /* not reached */
}

int cmd_ramrun(int argc, char **argv)
{
    int32_t n;
    (void)argv;
    if (argc != 1) { uart_puts("usage: ramrun   (then send the .img with XMODEM-1K)\n"); return -1; }
    uart_puts("ramrun: send the application image (XMODEM-CRC/1K) now ...\n");
    n = xmodem_receive((uint8_t *)STAGING_ADDR, STAGING_SIZE);
    uart_puts("\n");
    if (n < 0) { uart_printf("ramrun: xmodem failed: %s\n", xmodem_err_str(n)); return -1; }
    if (ramrun_check((uint32_t)n)) return -1;
    return ramrun_execute();
}
