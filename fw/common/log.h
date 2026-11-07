#ifndef FW_LOG_H
#define FW_LOG_H
#include <stdint.h>

enum { LOG_ERR = 0, LOG_WARN = 1, LOG_INFO = 2, LOG_DBG = 3 };

void log_init(const char *tag);
void log_set_level(int level);
int  log_get_level(void);
void log_printf(int level, const char *fmt, ...);

#define LOGE(...) log_printf(LOG_ERR, __VA_ARGS__)
#define LOGW(...) log_printf(LOG_WARN, __VA_ARGS__)
#define LOGI(...) log_printf(LOG_INFO, __VA_ARGS__)
#define LOGD(...) log_printf(LOG_DBG, __VA_ARGS__)

#endif
