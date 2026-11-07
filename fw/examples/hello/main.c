/*
 * Minimal DIY application for the HPDJ bootloader.
 *
 *   python -m fw.build --no-flash --app-src fw/examples/hello --app-version 0.1.0 --out build/hello
 *   python -m fw.tool update --port COMx build/hello/app.img --run
 *
 * Everything in fw/common is available: uart_printf/LOGI, timer_*, the GPIO
 * register macros in hw.h, the SPI flash driver, the CLI, and the shared
 * commands (slots/update/flash/md/...). Read-only data stays in the image;
 * variables go to .data/.bss, which the linker script places in the writable
 * DRAM alias for you. There is no heap; use static buffers.
 */
#include "hw.h"
#include "layout.h"
#include "uart.h"
#include "timer.h"
#include "log.h"
#include "cli.h"
#include "fmt.h"
#include "cache.h"
#include "crc.h"
#include "image.h"
#include "spi_flash.h"
#include "bootinfo.h"
#include "common_cmds.h"
#include "branding.h"
#include "libmini.h"

#ifndef FW_VERSION_STR
#define FW_VERSION_STR "0.0.0"
#endif
#ifndef FW_SEQUENCE
#define FW_SEQUENCE 0U
#endif

extern void jump_to(uint32_t entry, uint32_t arg) __attribute__((noreturn));

static uint32_t g_hello_count;            /* lives in .bss (zeroed at start) */
static const char *g_greeting = "Hello";  /* lives in .data (copied at start) */

static int cmd_hello(int argc, char **argv)
{
    const char *who = argc > 1 ? argv[1] : "DeskJet";
    g_hello_count++;
    uart_printf("%s, %s! (greeting #%u, uptime %u ms)\n", g_greeting, who, g_hello_count, timer_uptime_ms());
    return 0;
}

static int cmd_blink(int argc, char **argv)
{
    uint32_t times = 3, i;
    if (argc > 1 && parse_u32(argv[1], &times)) { uart_puts("usage: blink [count]\n"); return -1; }
    GPIO_DIR(GPIO_PIN_POWER_LED) = 1;
    for (i = 0; i < times; i++) {
        GPIO_OUTPUT(GPIO_PIN_POWER_LED) = 1;   /* active-low: off */
        timer_delay_ms(200);
        GPIO_OUTPUT(GPIO_PIN_POWER_LED) = 0;   /* on */
        timer_delay_ms(200);
    }
    LOGI("blinked %u times", times);
    return 0;
}

/* Re-enter the bootloader so the next "update" can be started from there. */
static int cmd_bl(int argc, char **argv)
{
    (void)argc; (void)argv;
    const image_header_t *hdr = (const image_header_t *)flash_mmap(FLASH_BL_OFFSET);
    if (image_validate(hdr, IMAGE_TYPE_BL, BL_LOAD_ADDR, BL_CODE_MAX, 1) != IMG_OK) return -1;
    uart_flush();
    uint32_t dst = RW_ALIAS(hdr->load_address);
    memcpy((void *)dst, flash_mmap(FLASH_BL_OFFSET + IMAGE_HEADER_SIZE), hdr->payload_size);
    cache_sync_code(dst, dst + hdr->payload_size, hdr->load_address, hdr->load_address + hdr->payload_size);
    if (crc32_compute((const void *)(uintptr_t)hdr->load_address, hdr->payload_size) != hdr->payload_crc32) {
        uart_puts("bootloader code in DRAM failed its CRC after the copy; not jumping\n");
        return -1;
    }
    jump_to(hdr->entry_address, BL_STAY_MAGIC);
}

static const cli_cmd_t cmds[] = {
    { "hello", "hello [name]: say hello", cmd_hello },
    { "blink", "blink [count]: blink the power LED", cmd_blink },
    { "bl",    "re-enter the bootloader shell", cmd_bl },
    COMMON_CMD_ENTRIES,
};

static cli_t cli;

void main(uint32_t arg)
{
    uart_init();
    timer_init();
    log_init("hello");
    flash_init();
    if (arg == BOOTINFO_ADDR && BOOTINFO->magic == BOOTINFO_MAGIC) {
        /* active_slot is 0/1 for a flash slot or BOOTINFO_SLOT_RAM after "run" */
        g_current_slot = BOOTINFO->active_slot < 2 ? (int)BOOTINFO->active_slot : -1;
        if (BOOTINFO->ticks_per_ms) timer_set_rate(BOOTINFO->ticks_per_ms);
    }
    flash_autodetect_mode();
    boot_attempts_mark_alive();     /* the bootloader's crash-loop guard: we are up */

    uart_printf("\n==== hello example v%s seq %u ====\n", FW_VERSION_STR, FW_SEQUENCE);
    uart_puts(FW_CREDIT_LINE "\n");
    uart_puts("Hello World from the HP DeskJet 2620!\n");
    LOGI("type help; this image runs from %s", g_current_slot == 0 ? "slot A" : g_current_slot == 1 ? "slot B" : "RAM");
    cli_init(&cli, cmds, (int)(sizeof(cmds) / sizeof(cmds[0])), "hello> ");
    cli_loop(&cli);
}
