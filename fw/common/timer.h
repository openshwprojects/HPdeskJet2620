#ifndef FW_TIMER_H
#define FW_TIMER_H
#include <stdint.h>

/* Counter rate in use: compiled default until timer_calibrate_uart() or
   timer_set_rate() replaces it. */
extern uint32_t g_ticks_per_us;
extern uint32_t g_ticks_per_ms;

void     timer_init(void);
uint32_t timer_ticks(void);                       /* raw 32-bit free-running counter */
uint32_t timer_elapsed_ms(uint32_t start_ticks);  /* wrap-safe, < 2^32 ticks */
uint32_t timer_elapsed_us(uint32_t start_ticks);
void     timer_delay_ms(uint32_t ms);
void     timer_delay_us(uint32_t us);
void     timer_poll(void);                        /* call regularly: accumulates uptime */
uint32_t timer_uptime_ms(void);                   /* milliseconds since timer_init() */
uint32_t timer_uptime_s(void);

/* Measure the counter against UART0 at 115200 8N1 by timing TIMER_CAL_BYTES
   transmitted spaces (followed by a carriage return). Returns the measured
   rate in hundredths of a tick per microsecond, or 0 when the measurement is
   implausible; a plausible result is adopted for all timing above. */
uint32_t timer_calibrate_uart(void);
/* Raw numbers behind the last calibration, for the boot log. */
uint32_t timer_calibration_ticks(void);
uint32_t timer_calibration_fill(void);
/* Adopt a rate measured elsewhere (e.g. passed from the bootloader). */
void     timer_set_rate(uint32_t ticks_per_ms);
int      timer_rate_is_measured(void);

#endif
