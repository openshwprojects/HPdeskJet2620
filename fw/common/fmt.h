#ifndef FW_FMT_H
#define FW_FMT_H
#include <stdarg.h>
#include <stdint.h>
#include <stddef.h>

typedef void (*fmt_out_fn)(char c, void *ctx);

/* printf subset: %s %c %d %i %u %x %X %p %%, flags "-" and "0", width, and
   the "l" length modifier (ignored, 32-bit). */
void fmt_vformat(fmt_out_fn out, void *ctx, const char *fmt, va_list ap);
int  snprintf(char *buf, size_t size, const char *fmt, ...);
int  vsnprintf(char *buf, size_t size, const char *fmt, va_list ap);

/* Parse "0x..." hex or decimal; returns 0 on success. */
int parse_u32(const char *s, uint32_t *out);

#endif
