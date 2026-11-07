#ifndef _OBK_SHIM_STDIO_H
#define _OBK_SHIM_STDIO_H

#include <stddef.h>
#include <stdarg.h>

#ifndef EOF
#define EOF (-1)
#endif

typedef struct _FILE FILE;
extern FILE *stdin;
extern FILE *stdout;
extern FILE *stderr;

int  printf(const char *fmt, ...);
int  vprintf(const char *fmt, va_list ap);
int  snprintf(char *buf, size_t size, const char *fmt, ...);
int  vsnprintf(char *buf, size_t size, const char *fmt, va_list ap);
int  sprintf(char *buf, const char *fmt, ...);
int  vsprintf(char *buf, const char *fmt, va_list ap);
int  putchar(int c);
int  puts(const char *s);

static inline int fflush(FILE *stream) { (void)stream; return 0; }

#endif
