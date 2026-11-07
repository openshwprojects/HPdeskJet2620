#ifndef _SHIM_TIME_H
#define _SHIM_TIME_H

#include <stddef.h>
#include <stdint.h>

typedef uint32_t time_t;
typedef uint32_t clock_t;

struct tm {
    int tm_sec;
    int tm_min;
    int tm_hour;
    int tm_mday;
    int tm_mon;
    int tm_year;
    int tm_wday;
    int tm_yday;
    int tm_isdst;
};

struct tm *gmtime(const time_t *timep);
struct tm *gmtime_r(const time_t *timep, struct tm *result);
struct tm *localtime(const time_t *timep);
time_t     mktime(struct tm *tm);
size_t     strftime(char *s, size_t max, const char *format, const struct tm *tm);

#endif
