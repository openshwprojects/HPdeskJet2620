/*
 * HPDJ bootloader (BL): second stage after HP stage-1.
 *
 * Stage-1 copies this image from flash 0x22040 to BL_LOAD_ADDR and jumps to it.
 * The BL selects the newest valid application slot, waits briefly for a key,
 * then copies the application into DRAM and runs it. Any key during the wait,
 * a missing/invalid application, or a direct call from the application with
 * r0 == BL_STAY_MAGIC drops into the recovery shell, which can receive a new
 * application over XMODEM and write it to a slot ("update") or run it from
 * RAM without touching flash ("run").
 *
 * Order of operations at power-on is deliberate: everything needed to select
 * and start the application (slot scan, copy) uses only the memory-mapped
 * flash window; SPI command traffic (JEDEC probe, mode detection) comes after
 * the slot scan so a surprise in the command path cannot hide a good slot.
 */
#include "hw.h"
#include "layout.h"
#include "config.h"
#include "uart.h"
#include "timer.h"
#include "log.h"
#include "cli.h"
#include "fmt.h"
#include "crc.h"
#include "cache.h"
#include "spi_flash.h"
#include "slots.h"
#include "image.h"
#include "xmodem.h"
#include "bootinfo.h"
#include "common_cmds.h"
#include "branding.h"
#include "libmini.h"

#ifndef FW_VERSION_U32
#define FW_VERSION_U32 0x01000000U
#endif
#ifndef FW_VERSION_STR
#define FW_VERSION_STR "1.0.0"
#endif
#ifndef FW_BUILD_STR
#define FW_BUILD_STR "unknown"
#endif

extern void jump_to(uint32_t entry, uint32_t arg) __attribute__((noreturn));

static int autoboot_allowed(void);

static slot_info_t g_slots[FLASH_SLOT_COUNT];
static int g_best = -1;
static uint32_t g_boot_count;
static cli_t cli;
static int g_shell_rearm;             /* shell entered by a key press: resume autoboot when idle */
static uint32_t g_shell_idle_start;
static int g_shell_last_len;

static void scan_slots(void)
{
    int i;
    for (i = 0; i < FLASH_SLOT_COUNT; i++) slot_inspect(i, &g_slots[i], 1);
    g_best = slot_select_best(g_slots);
    g_current_slot = g_best;
}

/* Copy a validated image payload to its load address and start it.
   `slot_code` is 0/1 for flash slots or BOOTINFO_SLOT_RAM. Returns only on failure. */
static void launch_image(const image_header_t *hdr, const uint8_t *payload, uint32_t slot_code, uint32_t reason)
{
    uint32_t dst_rw = RW_ALIAS(hdr->load_address);
    uint32_t size = hdr->payload_size;
    uint32_t crc;

    if (slot_code == BOOTINFO_SLOT_RAM)
        LOGI("loading RAM image: %u bytes -> 0x%08x, entry 0x%08x", size, hdr->load_address, hdr->entry_address);
    else
        LOGI("loading slot %c: %u bytes flash 0x%06x -> 0x%08x, entry 0x%08x", slot_letter((int)slot_code), size,
             (uint32_t)((const uint8_t *)payload - flash_mmap(0)), hdr->load_address, hdr->entry_address);
    memcpy((void *)dst_rw, payload, size);
    /* Clean the written lines to DRAM and drop any stale lines/translations
       for the executable alias, then verify the code the CPU will fetch
       through that alias (D-cache lines there were just invalidated, so this
       reads DRAM, not the cache). */
    cache_sync_code(dst_rw, dst_rw + size, hdr->load_address, hdr->load_address + size);
    crc = crc32_compute((const void *)(uintptr_t)hdr->load_address, size);
    if (crc != hdr->payload_crc32) {
        LOGE("code in DRAM failed its CRC after the copy (%08x != %08x); staying in the shell", crc,
             hdr->payload_crc32);
        return;
    }

    volatile bootinfo_t *bi = BOOTINFO;
    bi->magic = BOOTINFO_MAGIC;
    bi->bl_version = FW_VERSION_U32;
    bi->active_slot = slot_code;
    bi->app_version = hdr->version;
    bi->app_sequence = hdr->sequence;
    bi->boot_count = ++g_boot_count;
    bi->boot_reason = reason;
    bi->ticks_per_ms = timer_rate_is_measured() ? g_ticks_per_ms : 0;
    dcache_clean_range(BOOTINFO_ADDR, BOOTINFO_ADDR + sizeof(bootinfo_t));

    if (slot_code == BOOTINFO_SLOT_RAM)
        uart_puts("[BL] starting application from RAM v");
    else
        uart_printf("[BL] starting application %c v", slot_letter((int)slot_code));
    print_version(hdr->version);
    uart_printf(" seq %u at 0x%08x\n", hdr->sequence, hdr->entry_address);
    uart_flush();
    jump_to(hdr->entry_address, BOOTINFO_ADDR);
}

static void launch_slot(int slot, uint32_t reason)
{
    const image_header_t *hdr = g_slots[slot].hdr;
    uint32_t src = g_slots[slot].offset + IMAGE_HEADER_SIZE;
    dcache_invalidate_range(src, src + hdr->payload_size);
    launch_image(hdr, flash_mmap(src), (uint32_t)slot, reason);
}

static int cmd_boot(int argc, char **argv)
{
    int slot;
    scan_slots();
    if (argc >= 2) {
        slot = slot_from_arg(argv[1]);
        if (slot < 0) { uart_puts("usage: boot [a|b]\n"); return -1; }
        if (g_slots[slot].status != IMG_OK) {
            uart_printf("slot %c is not bootable: %s\n", slot_letter(slot), image_status_str(g_slots[slot].status));
            return -1;
        }
    } else {
        slot = g_best;
        if (slot < 0) { uart_puts("no valid application slot\n"); return -1; }
    }
    launch_slot(slot, BOOT_REASON_COMMAND);
    return -1;
}

/* Receive an application image and execute it from RAM without writing flash:
   the fastest development cycle, and nothing persists across a power cycle. */
static int cmd_run(int argc, char **argv)
{
    uint8_t *staging = (uint8_t *)STAGING_ADDR;
    (void)argc; (void)argv;
    uart_puts("send the application image (XMODEM-CRC/1K) now, it will run from RAM only ...\n");
    int32_t n = xmodem_receive(staging, STAGING_SIZE);
    uart_puts("\n");
    if (n < 0) {
        uart_printf("xmodem failed: %s\n", xmodem_err_str(n));
        return -1;
    }
    int st = slot_validate_staged(staging, (uint32_t)n);
    if (st != IMG_OK) {
        uart_printf("received %d bytes but the image is invalid: %s\n", (int)n, image_status_str(st));
        return -2;
    }
    const image_header_t *hdr = (const image_header_t *)staging;
    uart_printf("received %d bytes: app version ", (int)n);
    print_version(hdr->version);
    uart_printf(" seq %u, %u payload bytes, crc ok (not written to flash)\n", hdr->sequence, hdr->payload_size);
    scan_slots();
    launch_image(hdr, staging + IMAGE_HEADER_SIZE, BOOTINFO_SLOT_RAM, BOOT_REASON_RAM);
    return -3;
}

static int cmd_info(int argc, char **argv)
{
    (void)argc; (void)argv;
    const image_header_t *self = (const image_header_t *)flash_mmap(FLASH_BL_OFFSET);
    uart_printf("HPDJ bootloader v%s (%s)\n", FW_VERSION_STR, FW_BUILD_STR);
    uart_puts(FW_CREDIT_LINE "\n");
    uart_printf("bl image in flash: ");
    if (self->magic == IMAGE_MAGIC) {
        int st = image_validate(self, IMAGE_TYPE_BL, BL_LOAD_ADDR, BL_CODE_MAX, 1);
        uart_printf("%s, v", image_status_str(st));
        print_version(self->version);
        uart_printf(" seq %u, %u bytes\n", self->sequence, self->payload_size);
    } else {
        uart_puts("no header\n");
    }
    uart_printf("autoboot delay %u ms (idle shell resumes after %u ms), protect < 0x%06x, "
                "slots at 0x%06x/0x%06x (%u KiB each)\n",
                BL_AUTOBOOT_DELAY_MS, BL_SHELL_IDLE_AUTOBOOT_MS, FLASH_PROTECT_END, FLASH_SLOT_A_OFFSET,
                FLASH_SLOT_B_OFFSET, FLASH_SLOT_SIZE / 1024U);
    uart_printf("spi mode %s%s, bus errors %u, stale window sectors %u\n", flash_mode_name(flash_get_mode()),
                flash_mode_was_detected() ? " (auto-detected)" : " (JEDEC id not recognised)", flash_bus_errors(),
                flash_window_stale_sectors());
    scan_slots();
    slot_print_table(g_slots, g_best);
    return 0;
}

static const cli_cmd_t bl_cmds[] = {
    { "info",  "bootloader version and slot summary", cmd_info },
    { "boot",  "boot [a|b]: start the best (or given) application slot", cmd_boot },
    { "run",   "receive an app image over XMODEM and run it from RAM (no flash write)", cmd_run },
    COMMON_CMD_ENTRIES,
};

/* Idle hook: a shell entered by a (possibly spurious) key press resumes the
   autoboot when the operator neither types nor runs anything. */
static void shell_idle(void)
{
    if (!g_shell_rearm || BL_SHELL_IDLE_AUTOBOOT_MS == 0) return;
    if (cli.executed) { g_shell_rearm = 0; return; }       /* a real operator: stay */
    if (cli.len != g_shell_last_len) {                    /* keystrokes restart the timer */
        g_shell_last_len = cli.len;
        g_shell_idle_start = timer_ticks();
        return;
    }
    if (timer_elapsed_ms(g_shell_idle_start) < BL_SHELL_IDLE_AUTOBOOT_MS) return;
    g_shell_rearm = 0;
    uart_printf("\n[BL] no input for %u ms after the key press (line noise?): resuming autoboot\n",
                BL_SHELL_IDLE_AUTOBOOT_MS);
    scan_slots();
    if (g_best >= 0 && autoboot_allowed()) launch_slot(g_best, BOOT_REASON_AUTOBOOT);
    uart_puts("[BL] staying in the shell\n");
    cli_prompt(&cli);
}

static void shell(const char *why, int rearm_autoboot)
{
    uart_printf("[BL] entering recovery shell (%s); type help\n", why);
    cli_init(&cli, bl_cmds, (int)(sizeof(bl_cmds) / sizeof(bl_cmds[0])), "BL> ");
    g_shell_rearm = rearm_autoboot;
    g_shell_idle_start = timer_ticks();
    g_shell_last_len = 0;
    cli.idle = shell_idle;
    cli_loop(&cli);
}

static void probe_flash_controller(void)
{
    /* Raw controller state before our first command-mode transaction: status
       bit 0 is the SPI boot strap, control bits 15:12 are HP's protocol bits. */
    uint32_t ctrl = SPI_CTRL, status = SPI_STATUS, win0 = SPI_WINDOW(0);
    LOGI("spi controller before probe: ctrl %08x status %08x window0 %04x", ctrl, status, win0);
    int mode = flash_autodetect_mode();
    uint32_t id = flash_read_jedec_id();
    LOGI("flash id %06x (spi mode %s%s, bus errors %u)", id, flash_mode_name(flash_get_mode()),
         mode < 0 ? ", id not recognised in either mode" : (mode == SPI_MODE_WIDE_FINAL ? ", auto-detected" : ""),
         flash_bus_errors());
}

/* Crash-loop guard: count automatic launches that never reported "alive". */
static int autoboot_allowed(void)
{
    uint32_t attempts = boot_attempts_read();
    if (attempts >= BL_MAX_BOOT_ATTEMPTS) {
        LOGW("the application did not come up after %u consecutive automatic starts (crash loop?); "
             "not autobooting. 'boot' starts it anyway, 'erase'/'update' replace it.", attempts);
        boot_attempts_write(0);
        return 0;
    }
    boot_attempts_write(attempts + 1);
    return 1;
}

void main(uint32_t arg)
{
    int reentered = (arg == BL_STAY_MAGIC);
    uart_init();
    timer_init();
    log_init("bl");
    flash_init();

    uart_puts("\n\n");
    uart_printf("==== HPDJ bootloader v%s (%s) ====\n", FW_VERSION_STR, FW_BUILD_STR);
    uart_puts(FW_CREDIT_LINE "\n");
    LOGI("cpsr %08x sctlr %08x", cpsr_read(), cp15_read_sctlr());
    if (reentered && BOOTINFO->magic == BOOTINFO_MAGIC) {
        /* Re-entered from the application: keep the power-on measurements. */
        g_boot_count = BOOTINFO->boot_count;
        if (BOOTINFO->ticks_per_ms) {
            timer_set_rate(BOOTINFO->ticks_per_ms);
            LOGI("timer: %u ticks/ms (measured at power-on)", g_ticks_per_ms);
        }
    }
    if (!timer_rate_is_measured()) {
        /* The counter rate is the one timing unknown; measure it against the
           UART bit clock before anything depends on milliseconds. */
        uint32_t rate = timer_calibrate_uart();
        if (rate)
            LOGI("timer: %u.%02u ticks/us measured against UART (compiled default %u; %u ticks for %u bytes, fifo fill %u)",
                 rate / 100U, rate % 100U, TIMER_TICKS_PER_US, timer_calibration_ticks(), TIMER_CAL_BYTES,
                 timer_calibration_fill());
        else
            LOGW("timer: UART calibration implausible (%u ticks for %u bytes, fifo fill %u), using compiled %u ticks/us",
                 timer_calibration_ticks(), TIMER_CAL_BYTES, timer_calibration_fill(), TIMER_TICKS_PER_US);
    }

    /* Slot selection uses only the memory-mapped window. */
    scan_slots();
    slot_print_table(g_slots, g_best);
    probe_flash_controller();

    if (reentered) {
        shell("requested by application", 0);
    }
    if (g_best < 0) {
        shell("no valid application", 0);
    }
    if (!autoboot_allowed()) {
        shell("repeated application failures", 0);
    }

    uart_printf("[BL] autoboot slot %c in %u ms, press any key to stop... ", slot_letter(g_best), BL_AUTOBOOT_DELAY_MS);
    {
        uint32_t start = timer_ticks();
        while (timer_elapsed_ms(start) < BL_AUTOBOOT_DELAY_MS) {
            int c;
            watchdog_kick();
            timer_poll();
            c = uart_getc_nonblock();
            if (c >= 0 && c != 0x00 && c != 0xFF) {       /* 0x00/0xFF are the usual line glitches */
                uart_puts("stopped\n");
                shell("key pressed", 1);
            }
        }
    }
    uart_puts("\n");
    launch_slot(g_best, BOOT_REASON_AUTOBOOT);
    shell("application launch failed", 0);
}
