#include "hw.h"
#include "config.h"
#include "timer.h"

uint32_t g_ticks_per_us = TIMER_TICKS_PER_US;
uint32_t g_ticks_per_ms = TIMER_TICKS_PER_MS;

static uint32_t t_last;
static uint32_t t_carry;      /* ticks not yet converted to milliseconds */
static uint32_t t_uptime_ms;
static int t_measured;
static uint32_t t_cal_ticks, t_cal_fill;

uint32_t timer_calibration_ticks(void) { return t_cal_ticks; }
uint32_t timer_calibration_fill(void) { return t_cal_fill; }

void timer_init(void)
{
    t_last = TIMER_TICKS;
    t_carry = 0;
    t_uptime_ms = 0;
}

uint32_t timer_ticks(void) { return TIMER_TICKS; }

uint32_t timer_elapsed_us(uint32_t start) { return (TIMER_TICKS - start) / g_ticks_per_us; }
uint32_t timer_elapsed_ms(uint32_t start) { return (TIMER_TICKS - start) / g_ticks_per_ms; }

void timer_delay_us(uint32_t us)
{
    uint32_t start = TIMER_TICKS;
    while (timer_elapsed_us(start) < us) watchdog_kick();
}

void timer_delay_ms(uint32_t ms)
{
    uint32_t start = TIMER_TICKS;
    while (timer_elapsed_ms(start) < ms) {
        watchdog_kick();
        timer_poll();
    }
}

void timer_poll(void)
{
    uint32_t now = TIMER_TICKS;
    t_carry += now - t_last;
    t_last = now;
    if (t_carry >= g_ticks_per_ms) {
        t_uptime_ms += t_carry / g_ticks_per_ms;
        t_carry %= g_ticks_per_ms;
    }
}

uint32_t timer_uptime_ms(void) { timer_poll(); return t_uptime_ms; }
uint32_t timer_uptime_s(void) { return timer_uptime_ms() / 1000U; }

void timer_set_rate(uint32_t ticks_per_ms)
{
    uint32_t mhz;
    if (ticks_per_ms < TIMER_CAL_MIN_X100 * 10U || ticks_per_ms > TIMER_CAL_MAX_X100 * 10U) return;
    /* The UART measurement carries ~1% systematic error (on the printer it
       reads 990 ticks/ms for a counter that is exactly 1 MHz, checked against
       a PC clock). Counters run at whole-MHz rates, so a measurement within 3%
       of one is rounded to it. */
    mhz = (ticks_per_ms + 500U) / 1000U;
    if (mhz >= 1U) {
        uint32_t exact = mhz * 1000U;
        uint32_t diff = ticks_per_ms > exact ? ticks_per_ms - exact : exact - ticks_per_ms;
        if (diff * 100U <= exact * 3U) ticks_per_ms = exact;
    }
    timer_poll();
    g_ticks_per_ms = ticks_per_ms;
    g_ticks_per_us = (ticks_per_ms + 500U) / 1000U;
    if (g_ticks_per_us == 0) g_ticks_per_us = 1;
    t_measured = 1;
}

int timer_rate_is_measured(void) { return t_measured; }

uint32_t timer_calibrate_uart(void)
{
    uint32_t fill = 0, i, start, end, ticks, rate_x100;

    /* Fill the transmitter until it stops accepting bytes (FIFO full, or one
       byte without a FIFO). From then on each accepted byte corresponds to
       exactly one byte leaving at the baud rate, whatever the FIFO depth. */
    while ((UART0_LSR & UART0_LSR_THRE) && fill < 4096U) {
        UART0_THR = ' ';
        fill++;
    }
    t_cal_fill = fill;
    t_cal_ticks = 0;
    if (fill >= 4096U) return 0;          /* THRE never cleared: cannot measure */

    start = TIMER_TICKS;
    for (i = 0; i < TIMER_CAL_BYTES; i++) {
        uint32_t spins = 0;
        while (!(UART0_LSR & UART0_LSR_THRE)) {
            watchdog_kick();
            if (++spins > 10000000U) return 0;   /* transmitter stalled */
        }
        UART0_THR = ' ';
    }
    end = TIMER_TICKS;
    UART0_THR = '\r';

    /* 8N1 at 115200: one byte takes 86.8056 us; TIMER_CAL_BYTES bytes take
       TIMER_CAL_BYTES * 86.8056 us = 22222 us for 256 bytes. */
    ticks = end - start;
    t_cal_ticks = ticks;
    if (ticks > 40000000U) return 0;      /* > ~1.8 GHz, or the counter is not free-running */
    rate_x100 = (ticks * 100U) / ((TIMER_CAL_BYTES * 868056U) / 10000U);
    if (rate_x100 < TIMER_CAL_MIN_X100 || rate_x100 > TIMER_CAL_MAX_X100) return 0;
    timer_set_rate(rate_x100 * 10U);
    return rate_x100;
}
