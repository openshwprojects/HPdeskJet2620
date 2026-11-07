#include <stdarg.h>
#include "log.h"
#include "uart.h"
#include "timer.h"

static const char *log_tag = "fw";
static int log_level = LOG_INFO;
static const char level_char[4] = { 'E', 'W', 'I', 'D' };

void log_init(const char *tag) { log_tag = tag; }
void log_set_level(int level) { log_level = level < 0 ? 0 : level > LOG_DBG ? LOG_DBG : level; }
int  log_get_level(void) { return log_level; }

void log_printf(int level, const char *fmt, ...)
{
    if (level > log_level) return;
    uint32_t ms = timer_uptime_ms();
    uart_printf("[%5u.%03u] %c %s: ", ms / 1000U, ms % 1000U, level_char[level & 3], log_tag);
    va_list ap;
    va_start(ap, fmt);
    uart_vprintf(fmt, ap);
    va_end(ap);
    uart_puts("\n");
}
