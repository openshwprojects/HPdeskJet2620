/*
 * libc.c - Pure MIPS-I standard C runtime for RTL8196C.
 * Byte-wise memory operations: the Lexra RLX4181 core lacks unaligned load/store.
 */
#include <stddef.h>
#include <stdint.h>
#include <stdarg.h>

int errno = 0;

int *__errno(void)
{
    return &errno;
}

/* Word copies when source and destination share their alignment (only
 * aligned lw/sw, nothing the Lexra core lacks); bytes otherwise.  Measured
 * 1.2 MB/s byte-wise from uncached memory. */
void *memcpy(void *dst, const void *src, size_t n)
{
    unsigned char *d = dst;
    const unsigned char *s = src;
    if (n >= 8 && (((uintptr_t)d ^ (uintptr_t)s) & 3u) == 0) {
        while ((uintptr_t)d & 3u) { *d++ = *s++; n--; }
        uint32_t *dw = (uint32_t *)d;
        const uint32_t *sw = (const uint32_t *)s;
        while (n >= 32) {
            uint32_t a0 = sw[0], a1 = sw[1], a2 = sw[2], a3 = sw[3];
            uint32_t a4 = sw[4], a5 = sw[5], a6 = sw[6], a7 = sw[7];
            dw[0] = a0; dw[1] = a1; dw[2] = a2; dw[3] = a3;
            dw[4] = a4; dw[5] = a5; dw[6] = a6; dw[7] = a7;
            dw += 8; sw += 8; n -= 32;
        }
        while (n >= 4) { *dw++ = *sw++; n -= 4; }
        d = (unsigned char *)dw;
        s = (const unsigned char *)sw;
    }
    while (n--)
        *d++ = *s++;
    return dst;
}

void *memmove(void *dst, const void *src, size_t n)
{
    unsigned char *d = dst;
    const unsigned char *s = src;
    if (d <= s)
        return memcpy(dst, src, n);
    d += n; s += n;
    while (n--)
        *--d = *--s;
    return dst;
}

void *memset(void *dst, int c, size_t n)
{
    unsigned char *d = dst;
    if (n >= 8) {
        while ((uintptr_t)d & 3u) { *d++ = (unsigned char)c; n--; }
        uint32_t w = (unsigned char)c;
        w |= w << 8;
        w |= w << 16;
        uint32_t *dw = (uint32_t *)d;
        while (n >= 16) { dw[0] = w; dw[1] = w; dw[2] = w; dw[3] = w; dw += 4; n -= 16; }
        while (n >= 4) { *dw++ = w; n -= 4; }
        d = (unsigned char *)dw;
    }
    while (n--)
        *d++ = (unsigned char)c;
    return dst;
}

int memcmp(const void *a, const void *b, size_t n)
{
    const unsigned char *p = a, *q = b;
    for (; n; n--, p++, q++)
        if (*p != *q)
            return *p - *q;
    return 0;
}

void *memchr(const void *s, int c, size_t n)
{
    const unsigned char *p = s;
    unsigned char ch = (unsigned char)c;
    for (; n; n--, p++)
        if (*p == ch)
            return (void *)p;
    return 0;
}

size_t strlen(const char *s)
{
    size_t n = 0;
    while (s[n])
        n++;
    return n;
}

size_t strnlen(const char *s, size_t maxlen)
{
    size_t n = 0;
    while (n < maxlen && s[n])
        n++;
    return n;
}

int strcmp(const char *a, const char *b)
{
    while (*a && *a == *b) { a++; b++; }
    return (unsigned char)*a - (unsigned char)*b;
}

int strncmp(const char *a, const char *b, size_t n)
{
    for (; n; n--, a++, b++) {
        if (*a != *b) return (unsigned char)*a - (unsigned char)*b;
        if (!*a) return 0;
    }
    return 0;
}

static unsigned char lower_ch(unsigned char c)
{
    return (c >= 'A' && c <= 'Z') ? (unsigned char)(c + ('a' - 'A')) : c;
}

int strcasecmp(const char *a, const char *b)
{
    while (*a && lower_ch((unsigned char)*a) == lower_ch((unsigned char)*b)) { a++; b++; }
    return (int)lower_ch((unsigned char)*a) - (int)lower_ch((unsigned char)*b);
}

int strncasecmp(const char *a, const char *b, size_t n)
{
    for (; n; n--, a++, b++) {
        unsigned char ca = lower_ch((unsigned char)*a), cb = lower_ch((unsigned char)*b);
        if (ca != cb) return (int)ca - (int)cb;
        if (!ca) return 0;
    }
    return 0;
}

int stricmp(const char *a, const char *b)
{
    return strcasecmp(a, b);
}

int strnicmp(const char *a, const char *b, size_t n)
{
    return strncasecmp(a, b, n);
}

char *strcpy(char *dst, const char *src)
{
    char *d = dst;
    while ((*d++ = *src++) != 0)
        ;
    return dst;
}

char *strncpy(char *dst, const char *src, size_t n)
{
    char *d = dst;
    for (; n && *src; n--)
        *d++ = *src++;
    for (; n; n--)
        *d++ = 0;
    return dst;
}

char *strcat(char *dst, const char *src)
{
    char *d = dst;
    while (*d) d++;
    while ((*d++ = *src++) != 0)
        ;
    return dst;
}

char *strncat(char *dst, const char *src, size_t n)
{
    char *d = dst;
    while (*d) d++;
    for (; n && *src; n--)
        *d++ = *src++;
    *d = 0;
    return dst;
}

char *strchr(const char *s, int c)
{
    char ch = (char)c;
    for (; *s; s++)
        if (*s == ch) return (char *)s;
    return ch == 0 ? (char *)s : 0;
}

char *strrchr(const char *s, int c)
{
    const char *last = 0;
    char ch = (char)c;
    for (; *s; s++)
        if (*s == ch) last = s;
    return ch == 0 ? (char *)s : (char *)last;
}

char *strstr(const char *haystack, const char *needle)
{
    size_t nl = strlen(needle);
    if (!nl) return (char *)haystack;
    for (; *haystack; haystack++)
        if (strncmp(haystack, needle, nl) == 0)
            return (char *)haystack;
    return 0;
}

const char *strcasestr(const char *haystack, const char *needle)
{
    size_t nl = strlen(needle);
    if (!nl) return (char *)haystack;
    for (; *haystack; haystack++)
        if (strncasecmp(haystack, needle, nl) == 0)
            return (char *)haystack;
    return 0;
}

size_t strspn(const char *s, const char *accept)
{
    size_t n = 0;
    for (; *s; s++, n++) {
        const char *a = accept;
        while (*a && *a != *s) a++;
        if (!*a) break;
    }
    return n;
}

size_t strcspn(const char *s, const char *reject)
{
    size_t n = 0;
    for (; *s; s++, n++) {
        const char *r = reject;
        while (*r) {
            if (*r == *s) return n;
            r++;
        }
    }
    return n;
}

char *strpbrk(const char *s, const char *accept)
{
    for (; *s; s++) {
        for (const char *a = accept; *a; a++)
            if (*s == *a)
                return (char *)s;
    }
    return 0;
}

char *strtok_r(char *str, const char *delim, char **saveptr)
{
    char *s = str ? str : *saveptr;
    if (!s) return 0;
    s += strspn(s, delim);
    if (!*s) {
        *saveptr = s;
        return 0;
    }
    char *tok = s;
    s += strcspn(s, delim);
    if (*s) {
        *s = 0;
        *saveptr = s + 1;
    } else {
        *saveptr = s;
    }
    return tok;
}

char *strtok(char *str, const char *delim)
{
    static char *save;
    return strtok_r(str, delim, &save);
}

unsigned long long strtoull(const char *s, char **endp, int base)
{
    while (*s == ' ' || *s == '\t' || *s == '\r' || *s == '\n') s++;
    if (*s == '+') s++;
    if ((base == 0 || base == 16) && s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) {
        s += 2;
        base = 16;
    } else if (base == 0) {
        base = (s[0] == '0') ? 8 : 10;
    }
    unsigned long long val = 0;
    for (; *s; s++) {
        int d;
        if (*s >= '0' && *s <= '9') d = *s - '0';
        else if (*s >= 'a' && *s <= 'z') d = *s - 'a' + 10;
        else if (*s >= 'A' && *s <= 'Z') d = *s - 'A' + 10;
        else break;
        if (d >= base) break;
        val = val * (unsigned long long)base + (unsigned long long)d;
    }
    if (endp) *endp = (char *)s;
    return val;
}

long long strtoll(const char *s, char **endp, int base)
{
    while (*s == ' ' || *s == '\t' || *s == '\r' || *s == '\n') s++;
    int neg = 0;
    if (*s == '-') { neg = 1; s++; }
    else if (*s == '+') s++;
    unsigned long long u = strtoull(s, endp, base);
    return neg ? -(long long)u : (long long)u;
}

unsigned long strtoul(const char *s, char **endp, int base)
{
    return (unsigned long)strtoull(s, endp, base);
}

long strtol(const char *s, char **endp, int base)
{
    return (long)strtoll(s, endp, base);
}

int atoi(const char *s)
{
    return (int)strtol(s, 0, 10);
}

long atol(const char *s)
{
    return strtol(s, 0, 10);
}

double strtod(const char *s, char **endp)
{
    const char *p = s;
    while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') p++;

    int neg = 0;
    if (*p == '-') { neg = 1; p++; }
    else if (*p == '+') p++;

    double val = 0.0;
    int any = 0;
    while (*p >= '0' && *p <= '9') {
        val = val * 10.0 + (double)(*p - '0');
        p++;
        any = 1;
    }
    if (*p == '.') {
        p++;
        double place = 0.1;
        while (*p >= '0' && *p <= '9') {
            val += (double)(*p - '0') * place;
            place *= 0.1;
            p++;
            any = 1;
        }
    }
    if (!any) {
        if (endp) *endp = (char *)s;
        return 0.0;
    }
    if (*p == 'e' || *p == 'E') {
        const char *ep = p + 1;
        int eneg = 0;
        if (*ep == '-') { eneg = 1; ep++; }
        else if (*ep == '+') ep++;
        if (*ep >= '0' && *ep <= '9') {
            int exp = 0;
            while (*ep >= '0' && *ep <= '9') {
                exp = exp * 10 + (*ep - '0');
                ep++;
            }
            p = ep;
            double factor = 1.0;
            for (int i = 0; i < exp && i < 308; i++)
                factor *= 10.0;
            if (eneg) val /= factor;
            else val *= factor;
        }
    }
    if (endp) *endp = (char *)p;
    return neg ? -val : val;
}

float strtof(const char *s, char **endp)
{
    return (float)strtod(s, endp);
}

double atof(const char *s)
{
    return strtod(s, 0);
}

int abs(int x)
{
    return x < 0 ? -x : x;
}

long labs(long x)
{
    return x < 0 ? -x : x;
}

static uint32_t rand_state = 1;

void srand(unsigned int seed)
{
    rand_state = seed ? seed : 1;
}

int rand(void)
{
    rand_state = rand_state * 1103515245u + 12345u;
    return (int)((rand_state >> 16) & 0x7FFFu);
}

void qsort(void *base, size_t nmemb, size_t size,
           int (*compar)(const void *, const void *))
{
    unsigned char *arr = (unsigned char *)base;
    if (nmemb < 2 || size == 0)
        return;
    for (size_t i = 1; i < nmemb; i++) {
        size_t j = i;
        while (j > 0 && compar(arr + (j - 1) * size, arr + j * size) > 0) {
            unsigned char *a = arr + (j - 1) * size;
            unsigned char *b = arr + j * size;
            for (size_t k = 0; k < size; k++) {
                unsigned char tmp = a[k];
                a[k] = b[k];
                b[k] = tmp;
            }
            j--;
        }
    }
}

/*
 * Minimal vsscanf / sscanf supporting %d, %i, %u, %x, %X, %f, %lf, %s, %c,
 * width, '*', 'h', 'hh', 'l', 'll', and literal matching.
 */
static int is_space(char c)
{
    return c == ' ' || c == '\t' || c == '\r' || c == '\n' || c == '\v' || c == '\f';
}

int vsscanf(const char *str, const char *fmt, va_list ap)
{
    int assigned = 0;
    const char *s = str;

    while (*fmt) {
        if (is_space(*fmt)) {
            while (is_space(*fmt)) fmt++;
            while (is_space(*s)) s++;
            continue;
        }
        if (*fmt != '%') {
            if (*s != *fmt)
                return assigned;
            s++;
            fmt++;
            continue;
        }
        fmt++;
        if (*fmt == '%') {
            if (*s != '%')
                return assigned;
            s++;
            fmt++;
            continue;
        }

        int suppress = 0;
        if (*fmt == '*') { suppress = 1; fmt++; }

        int width = 0;
        while (*fmt >= '0' && *fmt <= '9')
            width = width * 10 + (*fmt++ - '0');

        int len_mod = 0; /* 0=int, 1=long, 2=long long, -1=short, -2=char */
        if (*fmt == 'l') {
            fmt++;
            if (*fmt == 'l') { len_mod = 2; fmt++; }
            else len_mod = 1;
        } else if (*fmt == 'h') {
            fmt++;
            if (*fmt == 'h') { len_mod = -2; fmt++; }
            else len_mod = -1;
        }

        char spec = *fmt++;
        if (spec != 'c' && spec != '[') {
            while (is_space(*s)) s++;
            if (!*s && spec != 'n')
                return assigned;
        }

        /* Copy bounded token if width is given */
        char tok_buf[128];
        const char *parse_p = s;
        if (width > 0 && width < (int)sizeof(tok_buf) - 1 && spec != 'c' && spec != 's') {
            int k = 0;
            while (k < width && s[k] && !is_space(s[k])) {
                tok_buf[k] = s[k];
                k++;
            }
            tok_buf[k] = 0;
            parse_p = tok_buf;
        }

        switch (spec) {
        case 'd':
        case 'i': {
            char *endp;
            int base = (spec == 'd') ? 10 : 0;
            long long v = strtoll(parse_p, &endp, base);
            if (endp == parse_p) return assigned;
            s += (endp - parse_p);
            if (!suppress) {
                if (len_mod == 2) *va_arg(ap, long long *) = v;
                else if (len_mod == 1) *va_arg(ap, long *) = (long)v;
                else if (len_mod == -1) *va_arg(ap, short *) = (short)v;
                else if (len_mod == -2) *va_arg(ap, signed char *) = (signed char)v;
                else *va_arg(ap, int *) = (int)v;
                assigned++;
            }
            break;
        }
        case 'u':
        case 'x':
        case 'X':
        case 'o': {
            char *endp;
            int base = (spec == 'u') ? 10 : ((spec == 'o') ? 8 : 16);
            unsigned long long v = strtoull(parse_p, &endp, base);
            if (endp == parse_p) return assigned;
            s += (endp - parse_p);
            if (!suppress) {
                if (len_mod == 2) *va_arg(ap, unsigned long long *) = v;
                else if (len_mod == 1) *va_arg(ap, unsigned long *) = (unsigned long)v;
                else if (len_mod == -1) *va_arg(ap, unsigned short *) = (unsigned short)v;
                else if (len_mod == -2) *va_arg(ap, unsigned char *) = (unsigned char)v;
                else *va_arg(ap, unsigned int *) = (unsigned int)v;
                assigned++;
            }
            break;
        }
        case 'f':
        case 'g':
        case 'e': {
            char *endp;
            double v = strtod(parse_p, &endp);
            if (endp == parse_p) return assigned;
            s += (endp - parse_p);
            if (!suppress) {
                if (len_mod >= 1) *va_arg(ap, double *) = v;
                else *va_arg(ap, float *) = (float)v;
                assigned++;
            }
            break;
        }
        case 's': {
            char *dst = suppress ? 0 : va_arg(ap, char *);
            int count = 0;
            while (*s && !is_space(*s) && (width == 0 || count < width)) {
                if (dst) *dst++ = *s;
                s++;
                count++;
            }
            if (count == 0) return assigned;
            if (dst) { *dst = 0; assigned++; }
            break;
        }
        case 'c': {
            if (!*s) return assigned;
            int n = width ? width : 1;
            char *dst = suppress ? 0 : va_arg(ap, char *);
            for (int i = 0; i < n && *s; i++) {
                if (dst) *dst++ = *s;
                s++;
            }
            if (!suppress) assigned++;
            break;
        }
        case 'n': {
            if (!suppress)
                *va_arg(ap, int *) = (int)(s - str);
            break;
        }
        default:
            return assigned;
        }
    }
    return assigned;
}

int sscanf(const char *str, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    int n = vsscanf(str, fmt, ap);
    va_end(ap);
    return n;
}

/*
 * <time.h> support for OpenBeken NTP / timekeeping.
 */
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

static int is_leap(int y)
{
    return (y % 4 == 0 && (y % 100 != 0 || y % 400 == 0));
}

static const int mdays[12] = { 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31 };

struct tm *gmtime_r(const uint32_t *timep, struct tm *r)
{
    uint32_t t = timep ? *timep : 0;
    r->tm_sec  = (int)(t % 60); t /= 60;
    r->tm_min  = (int)(t % 60); t /= 60;
    r->tm_hour = (int)(t % 24); t /= 24;

    r->tm_wday = (int)((t + 4) % 7); /* 1970-01-01 was Thursday (4) */

    int year = 1970;
    for (;;) {
        int dy = is_leap(year) ? 366 : 365;
        if ((int)t < dy) break;
        t -= (uint32_t)dy;
        year++;
    }
    r->tm_year = year - 1900;
    r->tm_yday = (int)t;

    int mon = 0;
    for (; mon < 12; mon++) {
        int dm = mdays[mon] + (mon == 1 && is_leap(year) ? 1 : 0);
        if ((int)t < dm) break;
        t -= (uint32_t)dm;
    }
    r->tm_mon  = mon;
    r->tm_mday = (int)t + 1;
    r->tm_isdst = 0;
    return r;
}

struct tm *gmtime(const uint32_t *timep)
{
    static struct tm tmbuf;
    return gmtime_r(timep, &tmbuf);
}

struct tm *localtime(const uint32_t *timep)
{
    return gmtime(timep);
}

uint32_t mktime(struct tm *tm)
{
    if (!tm) return 0;
    int year = tm->tm_year + 1900;
    uint32_t days = 0;
    for (int y = 1970; y < year; y++)
        days += is_leap(y) ? 366u : 365u;
    for (int m = 0; m < tm->tm_mon && m < 12; m++)
        days += (uint32_t)(mdays[m] + (m == 1 && is_leap(year) ? 1 : 0));
    days += (uint32_t)(tm->tm_mday - 1);
    return ((days * 24u + (uint32_t)tm->tm_hour) * 60u + (uint32_t)tm->tm_min) * 60u +
           (uint32_t)tm->tm_sec;
}

extern int snprintf(char *buf, unsigned size, const char *fmt, ...);

size_t strftime(char *s, size_t max, const char *format, const struct tm *tm)
{
    (void)format;
    if (!s || max == 0 || !tm) return 0;
    int n = snprintf(s, (unsigned)max, "%04d-%02d-%02d %02d:%02d:%02d",
                     tm->tm_year + 1900, tm->tm_mon + 1, tm->tm_mday,
                     tm->tm_hour, tm->tm_min, tm->tm_sec);
    return (n > 0 && (size_t)n < max) ? (size_t)n : 0;
}

size_t strlcpy(char *dst, const char *src, size_t size)
{
    size_t srclen = strlen(src);
    if (size > 0) {
        size_t n = (srclen >= size) ? (size - 1) : srclen;
        memcpy(dst, src, n);
        dst[n] = '\0';
    }
    return srclen;
}

extern uint32_t crc32_update(uint32_t crc, const void *data, size_t len);

uint32_t crc32(const void *data, size_t len)
{
    return crc32_update(0, data, len);
}

void abort(void)
{
    while (1) { }
}

void exit(int status)
{
    (void)status;
    while (1) { }
}
