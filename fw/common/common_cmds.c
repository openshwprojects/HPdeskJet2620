#include "common_cmds.h"
#include "hw.h"
#include "layout.h"
#include "config.h"
#include "uart.h"
#include "fmt.h"
#include "timer.h"
#include "crc.h"
#include "cache.h"
#include "spi_flash.h"
#include "slots.h"
#include "libmini.h"

int g_current_slot = -1;

static int usage(const char *text)
{
    uart_printf("usage: %s\n", text);
    return -1;
}

/* Addresses the CPU can touch without a data abort: the flash window, the
   three DRAM aliases and the peripheral block HP's drivers use. There are no
   exception handlers of our own, so a stray access would land in HP's stage-1
   vectors and hang until a power cycle. */
static int address_ok(uint32_t addr, uint32_t len)
{
    uint32_t end = addr + len;
    if (end < addr) return 0;
    if (end <= FLASH_TOTAL_SIZE) return 1;
    if ((addr & 0xFC000000U) == 0x20000000U || (addr & 0xFC000000U) == 0x60000000U ||
        (addr & 0xFC000000U) == 0xA0000000U)
        return ((end - 1) & 0xFC000000U) == (addr & 0xFC000000U);
    if ((addr & 0xFF000000U) == 0xBD000000U) return ((end - 1) & 0xFF000000U) == 0xBD000000U;
    return 0;
}

static int refuse_address(uint32_t addr)
{
    uart_printf("address 0x%08x is outside flash (0..0x00ffffff), DRAM (0x20/0x60/0xa0 + 64 MiB) or "
                "peripherals (0xbd......): refused\n", addr);
    return -1;
}

int cmd_md(int argc, char **argv)
{
    uint32_t addr, count = 16, i;
    if (argc < 2 || parse_u32(argv[1], &addr) || (argc > 2 && parse_u32(argv[2], &count)))
        return usage("md <addr> [words]");
    addr &= ~3U;
    if (count > 4096) count = 4096;
    if (!address_ok(addr, count * 4)) return refuse_address(addr);
    for (i = 0; i < count; i++) {
        if ((i & 3) == 0) uart_printf("%08x:", addr + i * 4);
        uart_printf(" %08x", REG32(addr + i * 4));
        if ((i & 3) == 3 || i + 1 == count) uart_puts("\n");
    }
    return 0;
}

int cmd_mw(int argc, char **argv)
{
    uint32_t addr, value;
    if (argc != 3 || parse_u32(argv[1], &addr) || parse_u32(argv[2], &value))
        return usage("mw <addr> <value>");
    if (!address_ok(addr & ~3U, 4) || addr < FLASH_TOTAL_SIZE || (addr & 0xFC000000U) == 0xA0000000U) {
        if (address_ok(addr & ~3U, 4)) uart_puts("flash window and the 0xa0 alias are read-only\n");
        else refuse_address(addr);
        return -1;
    }
    if ((addr & 0xFC000000U) != 0xBC000000U && (addr & 0x03FFFFFFU) < FLASH_BL_OFFSET) {
        /* HP stage-1's exception handlers, their stacks and the relocated DTCM
           live in the first 128 KiB of DRAM; a crash would run through them. */
        uart_puts("DRAM below offset 0x20000 holds HP stage-1 state (exception handlers, DTCM): refused\n");
        return -1;
    }
    REG32(addr & ~3U) = value;
    uart_printf("%08x = %08x\n", addr & ~3U, REG32(addr & ~3U));
    return 0;
}

int cmd_crc(int argc, char **argv)
{
    uint32_t addr, len;
    if (argc != 3 || parse_u32(argv[1], &addr) || parse_u32(argv[2], &len))
        return usage("crc <addr> <len>");
    if (!address_ok(addr, len)) return refuse_address(addr);
    uart_printf("crc32(%08x, %u) = %08x\n", addr, len, crc32_compute((const void *)(uintptr_t)addr, len));
    return 0;
}

static int flash_selftest(void)
{
    /* Compare SPI-command reads against the memory-mapped window. Agreement on
       real hardware confirms the serializer hypothesis for the read path. The
       first 8 KiB of the window are shadowed by the ITCM, so probes start
       above it. */
    static const uint32_t probes[] = { 0x010000, 0x020030, FLASH_BL_OFFSET, FLASH_SLOT_A_OFFSET, 0xFFF000 };
    uint8_t buf[64];
    uint32_t i, fails = 0;
    uint32_t id = flash_read_jedec_id();
    uart_printf("jedec id %06x (%s)\n", id, id == 0xEF4018 ? "W25Q128JV, expected" : "UNEXPECTED");
    if (id != 0xEF4018) fails++;
    for (i = 0; i < sizeof(probes) / sizeof(probes[0]); i++) {
        flash_read(probes[i], buf, sizeof(buf));
        dcache_invalidate_range(probes[i], probes[i] + sizeof(buf));
        int same = memcmp(buf, flash_mmap(probes[i]), sizeof(buf)) == 0;
        uart_printf("  spi read @%06x vs memory map: %s\n", probes[i], same ? "match" : "MISMATCH");
        if (!same) fails++;
    }
    uart_printf("status1 = %02x (WIP must be clear; BP bits 4:2 must be 0 for the slots to be writable)\n",
                flash_read_status1());
    if (flash_bus_errors()) {
        uart_printf("controller status waits that gave up: %u (unexpected AXON state encoding)\n", flash_bus_errors());
        fails++;
    }
    uart_printf("command path %s for writes\n", flash_mode_was_detected() ? "verified" : "NOT verified");
    uart_printf("flash selftest: %s (mode %s)\n", fails ? "FAILED" : "passed", flash_mode_name(flash_get_mode()));
    return fails ? -1 : 0;
}

#define FLASH_USAGE "flash id|status|read <off> <len>|selftest|diag|mode [byte|wide]"

int cmd_flash(int argc, char **argv)
{
    if (argc < 2) return usage(FLASH_USAGE);
    if (!strcmp(argv[1], "id")) {
        uart_printf("jedec id: %06x\n", flash_read_jedec_id());
        return 0;
    }
    if (!strcmp(argv[1], "status")) {
        uint8_t sr = flash_read_status1();
        uart_printf("status1: %02x (WIP %u, WEL %u, BP %u, TB %u, SEC %u, SRP %u)%s\n", sr, sr & 1, (sr >> 1) & 1,
                    (sr >> 2) & 7, (sr >> 5) & 1, (sr >> 6) & 1, (sr >> 7) & 1,
                    sr == 0xFF ? " - inconsistent read" : "");
        return 0;
    }
    if (!strcmp(argv[1], "diag")) {
        flash_diag();
        return 0;
    }
    if (!strcmp(argv[1], "read")) {
        uint32_t off, len;
        static uint8_t buf[256];
        if (argc != 4 || parse_u32(argv[2], &off) || parse_u32(argv[3], &len)) return usage("flash read <off> <len>");
        if (len > sizeof(buf)) len = sizeof(buf);
        flash_read(off, buf, len);
        uart_hexdump(off, buf, len);
        return 0;
    }
    if (!strcmp(argv[1], "selftest")) return flash_selftest();
    if (!strcmp(argv[1], "mode")) {
        if (argc >= 3) {
            if (!strcmp(argv[2], "byte")) flash_set_mode(SPI_MODE_BYTE_FINAL);
            else if (!strcmp(argv[2], "wide")) flash_set_mode(SPI_MODE_WIDE_FINAL);
            else return usage("flash mode [byte|wide]");
        }
        uart_printf("spi transfer mode: %s\n", flash_mode_name(flash_get_mode()));
        return 0;
    }
    return usage(FLASH_USAGE);
}

int cmd_slots(int argc, char **argv)
{
    slot_info_t infos[FLASH_SLOT_COUNT];
    int i;
    (void)argc; (void)argv;
    for (i = 0; i < FLASH_SLOT_COUNT; i++) slot_inspect(i, &infos[i], 1);
    slot_print_table(infos, slot_select_best(infos));
    return 0;
}

int cmd_verify(int argc, char **argv)
{
    slot_info_t info;
    int i, from = 0, to = FLASH_SLOT_COUNT - 1;
    if (argc >= 2) {
        from = to = slot_from_arg(argv[1]);
        if (from < 0) return usage("verify [a|b]");
    }
    for (i = from; i <= to; i++) {
        slot_inspect(i, &info, 1);
        uart_printf("slot %c: %s\n", slot_letter(i), image_status_str(info.status));
    }
    return 0;
}

static void erase_progress(uint32_t done, uint32_t total)
{
    (void)total;
    if ((done & 0xFFFF) == 0) uart_putc('.');
}

int cmd_erase(int argc, char **argv)
{
    int slot = argc >= 2 ? slot_from_arg(argv[1]) : -1;
    if (slot < 0) return usage("erase a|b");
    if (slot == g_current_slot) uart_puts("note: erasing the slot the running image came from\n");
    uart_printf("erasing slot %c ", slot_letter(slot));
    int rc = slot_erase(slot, erase_progress);
    uart_printf("\n%s\n", flash_err_str(rc));
    return rc;
}

int cmd_update(int argc, char **argv)
{
    int slot = -1;
    if (argc >= 2) {
        slot = slot_from_arg(argv[1]);
        if (slot < 0) return usage("update [a|b]");
    }
    return slot_update_over_xmodem(slot, g_current_slot);
}

int cmd_uptime(int argc, char **argv)
{
    (void)argc; (void)argv;
    uint32_t ms = timer_uptime_ms();
    uart_printf("uptime %u.%03u s (timer ticks %08x, %u ticks/ms %s)\n",
                ms / 1000U, ms % 1000U, timer_ticks(), g_ticks_per_ms,
                timer_rate_is_measured() ? "measured against UART" : "compiled default");
    return 0;
}

int cmd_sysinfo(int argc, char **argv)
{
    uint32_t v;
    (void)argc; (void)argv;
    __asm__ volatile ("mrc p15, 0, %0, c0, c0, 0" : "=r"(v));
    uart_printf("cpu id      %08x\n", v);
    uart_printf("cache type  %08x\n", cp15_read_cache_type());
    uart_printf("sctlr       %08x (mpu %u, dcache %u, icache %u, big-endian %u)\n",
                cp15_read_sctlr(), cp15_read_sctlr() & 1, (cp15_read_sctlr() >> 2) & 1,
                (cp15_read_sctlr() >> 12) & 1, (cp15_read_sctlr() >> 7) & 1);
    __asm__ volatile ("mrc p15, 0, %0, c5, c0, 2" : "=r"(v));
    uart_printf("mpu dperm   %08x\n", v);
    __asm__ volatile ("mrc p15, 0, %0, c5, c0, 3" : "=r"(v));
    uart_printf("mpu iperm   %08x\n", v);
    uart_printf("cpsr        %08x\n", cpsr_read());
    uart_printf("asic id     %08x\n", ASIC_ID);
    uart_printf("flash id    %06x, spi mode %s%s, bus errors %u, stale window sectors %u\n", flash_read_jedec_id(),
                flash_mode_name(flash_get_mode()), flash_mode_was_detected() ? " (auto-detected)" : "",
                flash_bus_errors(), flash_window_stale_sectors());
    uart_printf("spi regs    ctrl %08x status %08x windows %04x %04x %04x\n", SPI_CTRL, SPI_STATUS,
                SPI_WINDOW(0), SPI_WINDOW(1), SPI_WINDOW(2));
    uart_printf("timer       %u ticks/ms (%s)\n", g_ticks_per_ms,
                timer_rate_is_measured() ? "measured against UART" : "compiled default");
    uart_printf("protect     flash < 0x%06x is read-only for this firmware\n", FLASH_PROTECT_END);
    uart_printf("watchdog    ctl %04x routing %04x\n", WATCHDOG_CONTROL, WATCHDOG_ROUTING);
    return 0;
}

int system_reboot(void)
{
    uint32_t start;
    uart_printf("[reboot] arming watchdog, resetting... (ctl %04x routing %04x)\n", WATCHDOG_CONTROL, WATCHDOG_ROUTING);
    uart_flush();
    /* HP's own sequence (analysis/boot_hardware.md): stop/clear both stages,
       load the counts, then enable both stages. ~0.1 s at the 180 MHz
       reference count. In the emulator the expiry only raises an IRQ (masked),
       so the harness treats the marker above as the power cycle. */
    WATCHDOG_CONTROL = 0x000C;
    WATCHDOG_COUNT = 12000000U + 180U * 50000U;
    WATCHDOG_RELOAD = 12000000U;
    WATCHDOG_CONTROL = 0x0003;
    start = timer_ticks();
    while (timer_elapsed_ms(start) < REBOOT_WAIT_MS) {
        __asm__ volatile ("nop");
    }
    /* Still running: the watchdog did not reset the SoC (its reset effect is
       unverified on this hardware). Stop it again and give the shell back. */
    WATCHDOG_CONTROL = 0x000C;
    uart_printf("[reboot] watchdog did not reset the SoC within %u ms (ctl now %04x); power-cycle the printer, "
                "or use 'bl' and 'boot' to restart the application without a reset\n", REBOOT_WAIT_MS, WATCHDOG_CONTROL);
    return -1;
}

int cmd_reboot(int argc, char **argv)
{
    (void)argc; (void)argv;
    return system_reboot();
}
