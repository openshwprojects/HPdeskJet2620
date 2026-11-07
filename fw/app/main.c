/*
 * HPDJ application: UART logging, interactive shell and self-update.
 *
 * Launched by the bootloader with r0 = BOOTINFO_ADDR. Runs from DRAM at
 * APP_LOAD_ADDR with its data at APP_DATA_ADDR. "update" writes a new image
 * into the inactive slot (the bootloader then boots the highest sequence);
 * "bl" re-enters the bootloader shell without a reset.
 */
#include "hw.h"
#include "layout.h"
#include "config.h"
#include "uart.h"
#include "timer.h"
#include "log.h"
#include "cli.h"
#include "fmt.h"
#include "cache.h"
#include "crc.h"
#include "spi_flash.h"
#include "slots.h"
#include "image.h"
#include "bootinfo.h"
#include "common_cmds.h"
#include "periph_cmds.h"
#include "wifi.h"
#include "net.h"
#include "ui.h"
#include "usbcon.h"
#include "scan.h"
#include "irq.h"
#include "ramrun.h"
#include "periph.h"
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
#ifndef FW_SEQUENCE
#define FW_SEQUENCE 1U
#endif

extern void jump_to(uint32_t entry, uint32_t arg) __attribute__((noreturn));

static bootinfo_t g_bootinfo;
static int g_have_bootinfo;
static uint32_t g_led_state;
static uint32_t g_heartbeat_last;
static uint32_t g_heartbeat_period_ms = 10000;

static void led_set(int pin, int on)
{
    GPIO_OUTPUT(pin) = on ? 0 : 1;      /* active-low */
    GPIO_DIR(pin) = 1;
}

static int cmd_version(int argc, char **argv)
{
    (void)argc; (void)argv;
    uart_printf("HPDJ app v%s seq %u (%s)\n", FW_VERSION_STR, FW_SEQUENCE, FW_BUILD_STR);
    uart_puts(FW_CREDIT_LINE "\n");
    if (g_have_bootinfo) {
        if (g_bootinfo.active_slot == BOOTINFO_SLOT_RAM)
            uart_puts("running from RAM (uploaded with the bootloader's run command, not in flash) by bootloader v");
        else
            uart_printf("booted from slot %c by bootloader v", slot_letter((int)g_bootinfo.active_slot));
        print_version(g_bootinfo.bl_version);
        uart_printf(", boot #%u, reason %u\n", g_bootinfo.boot_count, g_bootinfo.boot_reason);
    } else {
        uart_puts("no bootinfo block (started without the bootloader)\n");
    }
    return 0;
}

static int cmd_log(int argc, char **argv)
{
    if (argc >= 3 && !strcmp(argv[1], "level")) {
        uint32_t lvl;
        if (parse_u32(argv[2], &lvl)) { uart_puts("usage: log level 0..3\n"); return -1; }
        log_set_level((int)lvl);
    } else if (argc >= 3 && !strcmp(argv[1], "heartbeat")) {
        uint32_t ms;
        if (parse_u32(argv[2], &ms)) { uart_puts("usage: log heartbeat <ms>\n"); return -1; }
        g_heartbeat_period_ms = ms;
    } else if (argc >= 2 && !strcmp(argv[1], "test")) {
        LOGE("error message");
        LOGW("warning message");
        LOGI("info message");
        LOGD("debug message");
    }
    uart_printf("log level %d (0 err, 1 warn, 2 info, 3 debug), heartbeat every %u ms (0 = off)\n",
                log_get_level(), g_heartbeat_period_ms);
    return 0;
}

static int cmd_led(int argc, char **argv)
{
    if (argc != 3) { uart_puts("usage: led power|wifi on|off\n"); return -1; }
    int pin = !strcmp(argv[1], "power") ? GPIO_PIN_POWER_LED : !strcmp(argv[1], "wifi") ? GPIO_PIN_WIFI_LED : -1;
    int on = !strcmp(argv[2], "on") ? 1 : !strcmp(argv[2], "off") ? 0 : -1;
    if (pin < 0 || on < 0) { uart_puts("usage: led power|wifi on|off\n"); return -1; }
    led_set(pin, on);
    if (on) g_led_state |= 1U << pin; else g_led_state &= ~(1U << pin);
    LOGI("led %s -> %s", argv[1], on ? "on" : "off");
    return 0;
}

static int cmd_gpio(int argc, char **argv)
{
    uint32_t pin, val;
    if (argc >= 2 && !strcmp(argv[1], "dump")) {
        for (pin = 0; pin < GPIO_PIN_COUNT; pin++)
            uart_printf("gpio %2u: dir %u out %u\n", pin, GPIO_DIR(pin) & 1, GPIO_OUTPUT(pin) & 1);
        return 0;
    }
    if (argc >= 3 && !strcmp(argv[1], "get") && !parse_u32(argv[2], &pin) && pin < GPIO_PIN_COUNT) {
        uart_printf("gpio %u in %u\n", pin, GPIO_INPUT(pin) & 1);
        return 0;
    }
    if (argc >= 4 && !strcmp(argv[1], "set") && !parse_u32(argv[2], &pin) && pin < GPIO_PIN_COUNT &&
        !parse_u32(argv[3], &val)) {
        GPIO_OUTPUT(pin) = (uint16_t)(val & 1);
        GPIO_DIR(pin) = 1;
        uart_printf("gpio %u out %u\n", pin, val & 1);
        return 0;
    }
    uart_puts("usage: gpio dump | gpio get <pin> | gpio set <pin> 0|1\n");
    return -1;
}

/* Shift a raw 64-bit frame (physical bits, no polarity mask applied) through
   the front panel and sample its inputs. */
static int cmd_panel(int argc, char **argv)
{
    uint32_t lo = 0x00000F00U, hi = 0, raw;
    int verified = 0;
    ui_hold();
    if (argc >= 3 && (parse_u32(argv[1], &lo) || parse_u32(argv[2], &hi))) {
        uart_puts("usage: panel [frame_lo frame_hi]   (raw physical frame, default: everything off)\n");
        return -1;
    }
    raw = panel_shift(lo, hi, &verified);
    uart_printf("panel frame %08x%08x %s; buttons raw %05x (active-low)\n", hi, lo,
                verified ? "acknowledged" : "NOT acknowledged", raw & PANEL_BUTTON_MASK);
    return verified ? 0 : -1;
}

static int cmd_echo(int argc, char **argv)
{
    int i;
    for (i = 1; i < argc; i++) uart_printf("%s%s", argv[i], i + 1 < argc ? " " : "");
    uart_puts("\n");
    return 0;
}

/* Re-enter the bootloader shell: reload BL from flash and jump with the stay magic. */
static int cmd_bl(int argc, char **argv)
{
    (void)argc; (void)argv;
    const image_header_t *hdr = (const image_header_t *)flash_mmap(FLASH_BL_OFFSET);
    dcache_invalidate_range(FLASH_BL_OFFSET, FLASH_BL_OFFSET + FLASH_BL_SIZE);
    int st = image_validate(hdr, IMAGE_TYPE_BL, BL_LOAD_ADDR, BL_CODE_MAX, 1);
    if (st != IMG_OK) {
        LOGE("bootloader image in flash is invalid (%s); use reboot instead", image_status_str(st));
        return -1;
    }
    LOGI("re-entering bootloader v%u.%u.%u shell", VERSION_MAJOR(hdr->version), VERSION_MINOR(hdr->version),
         VERSION_PATCH(hdr->version));
    uart_flush();
    uint32_t dst = RW_ALIAS(hdr->load_address);
    memcpy((void *)dst, flash_mmap(FLASH_BL_OFFSET + IMAGE_HEADER_SIZE), hdr->payload_size);
    cache_sync_code(dst, dst + hdr->payload_size, hdr->load_address, hdr->load_address + hdr->payload_size);
    /* Verify through the executable alias what the CPU will actually fetch. */
    if (crc32_compute((const void *)(uintptr_t)hdr->load_address, hdr->payload_size) != hdr->payload_crc32) {
        LOGE("bootloader code in DRAM failed its CRC after the copy; not jumping (cache coherency?)");
        return -1;
    }
    jump_to(hdr->entry_address, BL_STAY_MAGIC);
}

/* Hardware watchdog: with it on, a hang resets the printer by itself (the
   bootloader then starts the application again). Every wait loop feeds it. */
#ifndef WDT_BOOT_S
#define WDT_BOOT_S 0                   /* printer builds: 8 (fw/build.py) */
#endif
#define WDT_MAX_S 20U                  /* 32-bit count at 180 MHz */
static uint32_t g_wdt_seconds;

static int cmd_wdt(int argc, char **argv)
{
    uint32_t s = 8;
    if (argc >= 2 && !strcmp(argv[1], "on")) {
        if ((argc == 3 && (parse_u32(argv[2], &s) || s < 2U || s > WDT_MAX_S)) || argc > 3) {
            uart_printf("usage: wdt on [2..%u s]\n", WDT_MAX_S);
            return -1;
        }
        watchdog_enable(s);
        g_wdt_seconds = s;
    } else if (argc == 2 && !strcmp(argv[1], "off")) {
        watchdog_disable();
        g_wdt_seconds = 0;
    } else if (argc == 2 && !strcmp(argv[1], "test")) {
        uint32_t start = timer_ticks();
        if (!watchdog_enabled()) { uart_puts("wdt: off; 'wdt on' first\n"); return -1; }
        uart_printf("wdt: no longer feeding; the printer should reset in about %u s\n", g_wdt_seconds);
        uart_flush();
        for (;;) {
            if (timer_elapsed_ms(start) >= (g_wdt_seconds + 3U) * 1000U) break;
        }
        uart_puts("wdt: still running: the watchdog did not reset the SoC\n");
        return -1;
    } else if (argc != 1 && !(argc == 2 && !strcmp(argv[1], "status"))) {
        uart_puts("usage: wdt [status] | on [s] | off | test\n");
        return -1;
    }
    uart_printf("wdt: %s", watchdog_enabled() ? "on" : "off");
    if (watchdog_enabled()) uart_printf(", reset %u s after the last feed", g_wdt_seconds);
    uart_printf(" (ctl %04x)\n", WATCHDOG_CONTROL);
    return 0;
}

static int cmd_sleep(int argc, char **argv)
{
    uint32_t ms;
    if (argc != 2 || parse_u32(argv[1], &ms)) { uart_puts("usage: sleep <ms>\n"); return -1; }
    timer_delay_ms(ms);
    return 0;
}

static const cli_cmd_t app_cmds[] = {
    { "version", "application and bootloader versions", cmd_version },
    { "log",     "log level <0..3> | log heartbeat <ms> | log test", cmd_log },
    { "led",     "led power|wifi on|off", cmd_led },
    { "gpio",    "gpio dump | get <pin> | set <pin> 0|1", cmd_gpio },
    { "panel",   "panel [lo hi]: shift a frame to the front panel and read buttons", cmd_panel },
    { "echo",    "echo back the arguments", cmd_echo },
    { "sleep",   "sleep <ms>", cmd_sleep },
    { "wdt",     "wdt [status] | on [s] | off | test: hardware watchdog", cmd_wdt },
    { "bl",      "re-enter the bootloader recovery shell (no reset)", cmd_bl },
    PERIPH_CMD_ENTRIES,
    WIFI_CMD_ENTRY,
    NET_CMD_ENTRIES,
    UI_CMD_ENTRY,
    USB_CMD_ENTRY,
    SCAN_CMD_ENTRY,
    IRQ_CMD_ENTRY,
    RAMRUN_CMD_ENTRY,
    COMMON_CMD_ENTRIES,
};

static cli_t cli;

int app_shell_execute(char *line) { return cli_execute(&cli, line); }

static void app_idle(void)
{
    if (g_heartbeat_period_ms && timer_elapsed_ms(g_heartbeat_last) >= g_heartbeat_period_ms) {
        g_heartbeat_last = timer_ticks();
        LOGD("heartbeat, uptime %u s", timer_uptime_s());
    }
    wifi_poll();            /* Wi-Fi frames and events (nothing to do while the chip is off) */
    net_poll();             /* DHCP and ARP timers */
    motor_api_poll();       /* non-blocking motor lease safety cutoff */
    ui_poll();              /* front-panel menu, every 150 ms */
    usbcon_poll();          /* USB serial console, after "usb on"; its keys arrive through uart_getc_nonblock() */
}

void main(uint32_t arg)
{
    uart_init();
    net_log_init();         /* the web page's log starts with the boot messages */
    timer_init();
    log_init("app");
    flash_init();

    if (arg == BOOTINFO_ADDR && BOOTINFO->magic == BOOTINFO_MAGIC) {
        memcpy(&g_bootinfo, (const void *)BOOTINFO, sizeof(g_bootinfo));
        g_have_bootinfo = 1;
        if (g_bootinfo.active_slot == BOOTINFO_SLOT_RAM) {
            /* Running from RAM: "update" should still target the slot that
               is not the best one in flash. */
            slot_info_t infos[FLASH_SLOT_COUNT];
            int i;
            for (i = 0; i < FLASH_SLOT_COUNT; i++) slot_inspect(i, &infos[i], 0);
            g_current_slot = slot_select_best(infos);
        } else {
            g_current_slot = (int)g_bootinfo.active_slot;
        }
        if (g_bootinfo.ticks_per_ms) timer_set_rate(g_bootinfo.ticks_per_ms);
    }

    uart_puts("\n");
    uart_printf("==== HPDJ app v%s seq %u (%s) ====\n", FW_VERSION_STR, FW_SEQUENCE, FW_BUILD_STR);
    uart_puts(FW_CREDIT_LINE "\n");
    LOGI("running from 0x%08x, data at 0x%08x, %s %c", APP_LOAD_ADDR, APP_DATA_ADDR,
         (g_have_bootinfo && g_bootinfo.active_slot == BOOTINFO_SLOT_RAM) ? "RAM image, flash best slot" : "slot",
         g_current_slot >= 0 ? slot_letter(g_current_slot) : '?');
    flash_autodetect_mode();
    LOGI("timer %u ticks/ms (%s), flash id %06x, spi mode %s%s", g_ticks_per_ms,
         timer_rate_is_measured() ? "measured by bootloader" : "compiled default",
         flash_read_jedec_id(), flash_mode_name(flash_get_mode()),
         flash_mode_was_detected() ? "" : " (command path not verified: flash writes disabled, see 'flash diag')");
    led_set(GPIO_PIN_POWER_LED, 1);
    g_led_state = 1U << GPIO_PIN_POWER_LED;
    g_heartbeat_last = timer_ticks();
    boot_attempts_mark_alive();     /* tell the bootloader's crash-loop guard we came up */

#if WDT_BOOT_S
    watchdog_enable(WDT_BOOT_S);        /* printer builds: a hang resets the printer */
    g_wdt_seconds = WDT_BOOT_S;
#endif
#if USB_BOOT_ON
    usbcon_start();         /* printer builds: the USB port is a serial console from power-on */
#endif
    cli_init(&cli, app_cmds, (int)(sizeof(app_cmds) / sizeof(app_cmds[0])), "app> ");
    cli.idle = app_idle;
    cli_loop(&cli);
}
