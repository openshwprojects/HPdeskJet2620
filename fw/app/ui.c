/*
 * Front-panel menu that works without a PC, polled from the shell's idle
 * loop every 150 ms (each frame exchange also samples the buttons):
 *
 *   Wireless            scan; the digit shows how many networks were found
 *                       (9 = nine or more) and the bars the strongest signal,
 *                       for 6 s. In access point mode: the number of stations.
 *   Wireless, hold 2 s  start or stop the printer's own open access point
 *                       "HPDJ-xxxx" (address 192.168.4.1, answers ping)
 *   Info                the IP address, one character at a time ('-' = dot)
 *   Cancel              Wi-Fi off
 *   Resume              display test: every element on for one second
 *   Power               soft off / on: display, LEDs and Wi-Fi off; the UART
 *                       shell keeps running
 *
 * The display shows the Wi-Fi state: antenna icon and signal bars while
 * connected to a network, "A" and the Wi-Fi Direct icon in access point
 * mode, and the "!" icon while HP's door sensor is active (door open). The
 * blue Wireless LED is on while there is a link. The lcd, panel and buttons
 * commands take the panel over; "ui on" hands it back.
 */
#include "ui.h"
#include "wifi.h"
#include "net.h"
#include "periph.h"
#include "periph_cmds.h"
#include "hw.h"
#include "uart.h"
#include "timer.h"
#include "fmt.h"
#include "libmini.h"

#define UI_POLL_MS        150U
#define LONG_PRESS_MS     2000U
#define CHAR_ON_MS        600U
#define CHAR_STEP_MS      800U
#define BTN_POWER         1
#define BTN_RESUME        2
#define BTN_WIRELESS      3
#define BTN_CANCEL        6
#define BTN_INFO          7
#define BIT_WIFI_DIRECT   20
#define BIT_EXCLAMATION   14
#define TEST_BITS         0x00000001FFFFFB00ULL   /* bits 8..32 except the unused bit 10 */

static int enabled = 1, held, soft_off;
static uint32_t last_poll, pressed_prev, wireless_since;
static uint32_t inject_mask, inject_since, inject_ms;     /* virtual presses (web page) */
static int wireless_long;
static char seq[20];                     /* characters shown one by one (Info) */
static unsigned seq_len;
static uint32_t seq_start;
static char flash_ch;                    /* a result shown for a while (scan) */
static int flash_bars;
static uint32_t flash_start, flash_ms;
static uint32_t test_start;
static int test_on, led_applied = -1;
static int door_state = -1;              /* debounced DOOR_SENSOR, -1 = not read yet */

static void led_wifi(int on) { GPIO_OUTPUT(GPIO_PIN_WIFI_LED) = on ? 0 : 1; GPIO_DIR(GPIO_PIN_WIFI_LED) = 1; }
static void led_power(int on) { GPIO_OUTPUT(GPIO_PIN_POWER_LED) = on ? 0 : 1; GPIO_DIR(GPIO_PIN_POWER_LED) = 1; }

static int rssi_bars(int dbm)
{
    if (dbm >= -55) return 5;
    if (dbm >= -62) return 4;
    if (dbm >= -70) return 3;
    if (dbm >= -78) return 2;
    return 1;
}

static void build_frame(uint32_t *lo, uint32_t *hi)
{
    int mode = wifi_mode(), bars = 0;
    char ch = ' ';
    uint64_t extra = 0;
    if (soft_off) { panel_encode(' ', 0, 0, 0, 0, lo, hi); return; }
    if (test_on) {
        if (timer_elapsed_ms(test_start) < 1000U) { panel_encode('8', 5, 3, 3, TEST_BITS, lo, hi); return; }
        test_on = 0;
    }
    if (mode == WIFI_MODE_STA) bars = wifi_last_rssi() ? rssi_bars(wifi_last_rssi()) : 1;
    if (mode == WIFI_MODE_AP) { ch = 'A'; extra |= 1ULL << BIT_WIFI_DIRECT; }
    if (door_state > 0) extra |= 1ULL << BIT_EXCLAMATION;
    if (seq_len) {
        uint32_t t = timer_elapsed_ms(seq_start), idx = t / CHAR_STEP_MS;
        if (idx >= seq_len) seq_len = 0;
        else ch = (t % CHAR_STEP_MS) < CHAR_ON_MS ? seq[idx] : ' ';
    } else if (flash_ms) {
        if (timer_elapsed_ms(flash_start) < flash_ms) {
            ch = flash_ch;
            if (flash_bars >= 0) bars = flash_bars;
        } else {
            flash_ms = 0;
        }
    }
    if (panel_encode(ch, bars, 0, 0, extra, lo, hi)) panel_encode(' ', bars, 0, 0, extra, lo, hi);
}

/* Draw at once (before something that blocks for a while). */
static void show_now(char ch, int bars, uint32_t ms)
{
    uint32_t lo, hi;
    flash_ch = ch;
    flash_bars = bars;
    flash_start = timer_ticks();
    flash_ms = ms;
    build_frame(&lo, &hi);
    panel_shift(lo, hi, 0);
}

static void action_scan(void)
{
    uint32_t count = 0;
    int best = 0;
    if (wifi_mode() == WIFI_MODE_AP) {
        uint32_t n = wifi_ap_clients();
        uart_printf("ui: wireless: %u station%s on the access point\n", n, n == 1 ? "" : "s");
        show_now((char)('0' + (n > 9 ? 9 : n)), -1, 4000);
        return;
    }
    uart_puts("ui: wireless: scanning\n");
    show_now('-', -1, 30000);
    if (wifi_scan_summary(&count, &best)) {
        uart_puts("ui: scan failed\n");
        show_now('E', 0, 3000);
        return;
    }
    uart_printf("ui: %u network%s, strongest %d dBm\n", count, count == 1 ? "" : "s", best);
    show_now((char)('0' + (count > 9 ? 9 : count)), count ? rssi_bars(best) : 0, 6000);
}

static void action_access_point(void)
{
    char name[12];
    const uint8_t *m;
    if (wifi_mode() == WIFI_MODE_AP) {
        uart_puts("ui: wireless held: stopping the access point\n");
        wifi_leave();
        return;
    }
    uart_puts("ui: wireless held: starting the access point\n");
    show_now('A', -1, 30000);
    if (wifi_ensure_running()) { show_now('E', 0, 3000); return; }
    m = wifi_mac();
    snprintf(name, sizeof(name), "HPDJ-%02X%02X", m[4], m[5]);
    if (wifi_start_ap(name, 0, 6)) show_now('E', 0, 3000);
    else flash_ms = 0;
}

static void action_info(void)
{
    char ip[16];
    unsigned i;
    if (!net_has_address()) {
        uart_puts("ui: info: no address\n");
        strlcpy(seq, "-", sizeof(seq));
    } else {
        net_format_ip(net_ip(), ip);
        uart_printf("ui: info: address %s\n", ip);
        strlcpy(seq, ip, sizeof(seq));
        for (i = 0; seq[i]; i++) if (seq[i] == '.') seq[i] = '-';
    }
    seq_len = (unsigned)strlen(seq);
    seq_start = timer_ticks();
}

static void power_off(void)
{
    uart_puts("ui: power: soft off (display, LEDs and Wi-Fi off; press power again)\n");
    wifi_shutdown();
    soft_off = 1;
    seq_len = 0;
    flash_ms = 0;
    led_power(0);
    led_wifi(0);
    led_applied = 0;
}

static void power_on(void)
{
    uart_puts("ui: power: on\n");
    soft_off = 0;
    led_power(1);
}

static void on_buttons(uint32_t pressed)
{
    uint32_t newly = pressed & ~pressed_prev, released = pressed_prev & ~pressed;
    int bit;
    pressed_prev = pressed;
    if (soft_off) {
        if (newly & (1U << BTN_POWER)) power_on();
        return;
    }
    if (newly & (1U << BTN_WIRELESS)) { wireless_since = timer_ticks(); wireless_long = 0; }
    if ((pressed & (1U << BTN_WIRELESS)) && !wireless_long && timer_elapsed_ms(wireless_since) >= LONG_PRESS_MS) {
        wireless_long = 1;
        action_access_point();
    }
    if ((released & (1U << BTN_WIRELESS)) && !wireless_long) action_scan();
    if (newly & (1U << BTN_INFO)) action_info();
    if (newly & (1U << BTN_CANCEL)) {
        uart_puts("ui: cancel: Wi-Fi off\n");
        wifi_shutdown();
        seq_len = 0;
        flash_ms = 0;
    }
    if (newly & (1U << BTN_RESUME)) {
        uart_puts("ui: resume: display test\n");
        test_on = 1;
        test_start = timer_ticks();
    }
    if (newly & (1U << BTN_POWER)) power_off();
    for (bit = 0; bit < 18; bit++) {
        const char *name;
        if (!(newly & (1U << bit)) || bit == BTN_POWER || bit == BTN_RESUME || bit == BTN_WIRELESS ||
            bit == BTN_CANCEL || bit == BTN_INFO)
            continue;
        name = panel_button_name(bit);
        uart_printf("ui: %s pressed (no action)\n", name ? name : "unnamed button");
    }
}

void ui_poll(void)
{
    static int door_last = -1;
    uint32_t lo, hi, raw;
    int ok = 0, want_led, door;
    if (!enabled || held) return;
    if (timer_elapsed_ms(last_poll) < UI_POLL_MS) return;
    if (uart_rx_ready()) return;                     /* typing first: the shell reads it */
    last_poll = timer_ticks();
    door = sensor_door_active();
    if (door == door_last && door != door_state) {     /* same on two polls in a row */
        if (door_state >= 0) uart_printf("ui: door %s\n", door ? "open" : "closed");
        door_state = door;
    }
    door_last = door;
    build_frame(&lo, &hi);
    raw = panel_shift(lo, hi, &ok);
    want_led = !soft_off && wifi_link_up();
    if (want_led != led_applied) {
        led_wifi(want_led);
        led_applied = want_led;
    }
    if (inject_mask && timer_elapsed_ms(inject_since) >= inject_ms) inject_mask = 0;
    if (ok) on_buttons(((~raw) & PANEL_BUTTON_MASK) | inject_mask);
}

/* Press panel buttons from software for `ms` (the menu sees them like real ones). */
int ui_inject(uint32_t mask, uint32_t ms)
{
    if (!enabled || held) return -1;
    inject_mask = mask & PANEL_BUTTON_MASK;
    inject_since = timer_ticks();
    inject_ms = ms < 200U ? 200U : ms > 5000U ? 5000U : ms;
    return 0;
}

int ui_state(void) { return !enabled ? 0 : held ? 2 : soft_off ? 3 : 1; }

void ui_hold(void)
{
    if (enabled && !held) {
        held = 1;
        uart_puts("ui: the panel menu is paused while you drive the panel ('ui on' resumes it)\n");
    }
}

int cmd_ui(int argc, char **argv)
{
    if (argc == 2 && !strcmp(argv[1], "on")) {
        enabled = 1;
        held = 0;
        pressed_prev = 0;
        uart_puts("ui: panel menu on\n");
        return 0;
    }
    if (argc == 2 && !strcmp(argv[1], "off")) {
        enabled = 0;
        uart_puts("ui: panel menu off\n");
        return 0;
    }
    if (argc == 1 || (argc == 2 && !strcmp(argv[1], "status"))) {
        uart_printf("ui: panel menu %s%s%s\n", enabled ? "on" : "off", held ? ", paused (ui on resumes)" : "",
                    soft_off ? ", soft off" : "");
        uart_puts("ui: Wireless = scan, hold 2 s = access point on/off, Info = IP address, Cancel = Wi-Fi off,\n"
                  "ui: Resume = display test, Power = soft off/on\n");
        return 0;
    }
    uart_puts("usage: ui [status] | ui on | ui off\n");
    return -1;
}
