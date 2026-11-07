/*
 * printf.c - UART and buffer formatted output for RTL8196C (pure MIPS-I).
 *
 * Supports:
 *   - Specifiers: %d %i %u %x %X %o %p %c %s %f %F %g %G %e %E %%
 *   - Flags: '0', '-', '+', ' ', '#'
 *   - Width: integer or '*'
 *   - Precision: '.integer' or '.*' (for strings, integers, and floats)
 *   - Length modifiers: hh, h, l, ll, z, t
 *   - Entry points: printf, vprintf, snprintf, vsnprintf, sprintf, vsprintf,
 *                   puts, putchar
 */
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include "uart.h"

typedef void (*out_fn)(char c, void *ctx);

struct sbuf {
    char    *p;
    unsigned left;
};

static void out_uart(char c, void *ctx)
{
    (void)ctx;
    if (c == '\n')
        uart_putc('\r');
    uart_putc(c);
}

static void out_buf(char c, void *ctx)
{
    struct sbuf *b = ctx;
    if (b->left > 1) {
        *b->p++ = c;
        b->left--;
    }
}

static int emit_num64(out_fn out, void *ctx, uint64_t v, int base, int upper,
                      char sign_ch, int alt_prefix, int width, int prec,
                      int zero_pad, int left)
{
    char tmp[32];
    int n = 0, total = 0;
    const char *digits = upper ? "0123456789ABCDEF" : "0123456789abcdef";

    if (v == 0 && prec == 0) {
        /* Explicit %.0d with value 0 emits nothing for digits */
    } else {
        do {
            tmp[n++] = digits[v % (uint64_t)base];
            v /= (uint64_t)base;
        } while (v);
    }

    int zero_prec = (prec > n) ? (prec - n) : 0;
    int prefix_len = (sign_ch ? 1 : 0) + (alt_prefix ? 2 : 0);
    int content_len = prefix_len + zero_prec + n;
    int pad = (width > content_len) ? (width - content_len) : 0;

    if (prec >= 0 || left)
        zero_pad = 0;

    if (!left && !zero_pad) {
        while (pad-- > 0) { out(' ', ctx); total++; }
    }
    if (sign_ch) {
        out(sign_ch, ctx);
        total++;
    }
    if (alt_prefix) {
        out('0', ctx);
        out(upper ? 'X' : 'x', ctx);
        total += 2;
    }
    if (!left && zero_pad) {
        while (pad-- > 0) { out('0', ctx); total++; }
    }
    while (zero_prec-- > 0) {
        out('0', ctx);
        total++;
    }
    while (n > 0) {
        out(tmp[--n], ctx);
        total++;
    }
    if (left) {
        while (pad-- > 0) { out(' ', ctx); total++; }
    }
    return total;
}

static int emit_float(out_fn out, void *ctx, double val, int width, int prec,
                      char sign_flag, int zero_pad, int left, int strip_trailing)
{
    int total = 0;
    union { double d; uint64_t u; } u = { val };
    uint32_t exp = (uint32_t)((u.u >> 52) & 0x7FFu);
    uint64_t frac = u.u & 0x000FFFFFFFFFFFFFULL;
    int neg = (int)(u.u >> 63);

    if (exp == 0x7FFu) {
        const char *s = frac ? "nan" : (neg ? "-inf" : "inf");
        while (*s) { out(*s++, ctx); total++; }
        return total;
    }

    char sign_ch = 0;
    if (neg) {
        sign_ch = '-';
        val = -val;
    } else if (sign_flag) {
        sign_ch = sign_flag;
    }

    if (prec < 0)
        prec = 6;
    if (prec > 9)
        prec = 9;

    uint64_t scale = 1;
    for (int i = 0; i < prec; i++)
        scale *= 10;

    /* Round at target decimal place */
    double scaled = val * (double)scale + 0.5;
    uint64_t whole = (uint64_t)(scaled / (double)scale);
    uint64_t rem   = (uint64_t)(scaled - (double)whole * (double)scale);
    if (rem >= scale) {
        whole++;
        rem -= scale;
    }

    char int_buf[24];
    int int_n = 0;
    do {
        int_buf[int_n++] = (char)('0' + (whole % 10));
        whole /= 10;
    } while (whole);

    char frac_buf[12];
    int frac_n = prec;
    for (int i = prec - 1; i >= 0; i--) {
        frac_buf[i] = (char)('0' + (rem % 10));
        rem /= 10;
    }
    if (strip_trailing) {
        while (frac_n > 0 && frac_buf[frac_n - 1] == '0')
            frac_n--;
    }

    int content_len = (sign_ch ? 1 : 0) + int_n + (frac_n > 0 ? (1 + frac_n) : 0);
    int pad = (width > content_len) ? (width - content_len) : 0;

    if (!left && !zero_pad) {
        while (pad-- > 0) { out(' ', ctx); total++; }
    }
    if (sign_ch) {
        out(sign_ch, ctx);
        total++;
    }
    if (!left && zero_pad) {
        while (pad-- > 0) { out('0', ctx); total++; }
    }
    while (int_n > 0) {
        out(int_buf[--int_n], ctx);
        total++;
    }
    if (frac_n > 0) {
        out('.', ctx);
        total++;
        for (int i = 0; i < frac_n; i++) {
            out(frac_buf[i], ctx);
            total++;
        }
    }
    if (left) {
        while (pad-- > 0) { out(' ', ctx); total++; }
    }
    return total;
}

static int vformat(out_fn out, void *ctx, const char *fmt, va_list ap)
{
    int total = 0;

    for (; *fmt; fmt++) {
        if (*fmt != '%') {
            out(*fmt, ctx);
            total++;
            continue;
        }
        fmt++;

        int zero_pad = 0, left = 0, alt = 0;
        char sign_flag = 0;
        for (;; fmt++) {
            if (*fmt == '0') zero_pad = 1;
            else if (*fmt == '-') left = 1;
            else if (*fmt == '+') sign_flag = '+';
            else if (*fmt == ' ' && !sign_flag) sign_flag = ' ';
            else if (*fmt == '#') alt = 1;
            else break;
        }

        int width = 0;
        if (*fmt == '*') {
            width = va_arg(ap, int);
            if (width < 0) { left = 1; width = -width; }
            fmt++;
        } else {
            while (*fmt >= '0' && *fmt <= '9')
                width = width * 10 + (*fmt++ - '0');
        }

        int prec = -1;
        if (*fmt == '.') {
            fmt++;
            if (*fmt == '*') {
                prec = va_arg(ap, int);
                fmt++;
            } else {
                prec = 0;
                while (*fmt >= '0' && *fmt <= '9')
                    prec = prec * 10 + (*fmt++ - '0');
            }
        }

        int len_mod = 0; /* 0=int, 1=long, 2=long long, -1=short, -2=char */
        if (*fmt == 'l') {
            fmt++;
            if (*fmt == 'l') { len_mod = 2; fmt++; }
            else len_mod = 1;
        } else if (*fmt == 'h') {
            fmt++;
            if (*fmt == 'h') { len_mod = -2; fmt++; }
            else len_mod = -1;
        } else if (*fmt == 'z' || *fmt == 't') {
            len_mod = 1;
            fmt++;
        }

        switch (*fmt) {
        case 'd': case 'i': {
            int64_t v;
            if (len_mod == 2) v = va_arg(ap, long long);
            else if (len_mod == 1) v = va_arg(ap, long);
            else v = va_arg(ap, int);
            char sc = sign_flag;
            uint64_t u;
            if (v < 0) { sc = '-'; u = (uint64_t)(-v); }
            else u = (uint64_t)v;
            total += emit_num64(out, ctx, u, 10, 0, sc, 0, width, prec, zero_pad, left);
            break;
        }
        case 'u': {
            uint64_t v;
            if (len_mod == 2) v = va_arg(ap, unsigned long long);
            else if (len_mod == 1) v = va_arg(ap, unsigned long);
            else v = va_arg(ap, unsigned int);
            total += emit_num64(out, ctx, v, 10, 0, 0, 0, width, prec, zero_pad, left);
            break;
        }
        case 'x': case 'X': {
            uint64_t v;
            if (len_mod == 2) v = va_arg(ap, unsigned long long);
            else if (len_mod == 1) v = va_arg(ap, unsigned long);
            else v = va_arg(ap, unsigned int);
            total += emit_num64(out, ctx, v, 16, *fmt == 'X', 0, alt && v != 0,
                                width, prec, zero_pad, left);
            break;
        }
        case 'o': {
            uint64_t v;
            if (len_mod == 2) v = va_arg(ap, unsigned long long);
            else if (len_mod == 1) v = va_arg(ap, unsigned long);
            else v = va_arg(ap, unsigned int);
            total += emit_num64(out, ctx, v, 8, 0, 0, 0, width, prec, zero_pad, left);
            break;
        }
        case 'p': {
            uint64_t v = (uintptr_t)va_arg(ap, void *);
            out('0', ctx); out('x', ctx); total += 2;
            total += emit_num64(out, ctx, v, 16, 0, 0, 0, 8, -1, 1, 0);
            break;
        }
        case 'f': case 'F':
        case 'g': case 'G':
        case 'e': case 'E': {
            double dv = va_arg(ap, double);
            int strip = (*fmt == 'g' || *fmt == 'G');
            total += emit_float(out, ctx, dv, width, prec, sign_flag, zero_pad, left, strip);
            break;
        }
        case 'c': {
            int pad = width - 1;
            if (!left) while (pad-- > 0) { out(' ', ctx); total++; }
            out((char)va_arg(ap, int), ctx);
            total++;
            if (left) while (pad-- > 0) { out(' ', ctx); total++; }
            break;
        }
        case 's': {
            const char *s = va_arg(ap, const char *);
            if (!s) s = "(null)";
            int len = 0;
            for (const char *q = s; *q && (prec < 0 || len < prec); q++)
                len++;
            int pad = width - len;
            if (!left) while (pad-- > 0) { out(' ', ctx); total++; }
            for (int i = 0; i < len; i++) { out(s[i], ctx); total++; }
            if (left) while (pad-- > 0) { out(' ', ctx); total++; }
            break;
        }
        case '%':
            out('%', ctx);
            total++;
            break;
        case 0:
            return total;
        default:
            out('%', ctx); out(*fmt, ctx);
            total += 2;
            break;
        }
    }
    return total;
}

int vprintf(const char *fmt, va_list ap)
{
    return vformat(out_uart, 0, fmt, ap);
}

int printf(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    int n = vformat(out_uart, 0, fmt, ap);
    va_end(ap);
    return n;
}

int vsnprintf(char *buf, unsigned size, const char *fmt, va_list ap)
{
    struct sbuf b = { buf, size };
    int n = vformat(out_buf, &b, fmt, ap);
    if (size)
        *b.p = 0;
    return n;
}

int snprintf(char *buf, unsigned size, const char *fmt, ...)
{
    struct sbuf b = { buf, size };
    va_list ap;
    va_start(ap, fmt);
    int n = vformat(out_buf, &b, fmt, ap);
    va_end(ap);
    if (size)
        *b.p = 0;
    return n;
}

int vsprintf(char *buf, const char *fmt, va_list ap)
{
    return vsnprintf(buf, 0x7FFFFFFFu, fmt, ap);
}

int sprintf(char *buf, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf, 0x7FFFFFFFu, fmt, ap);
    va_end(ap);
    return n;
}

int putchar(int c)
{
    out_uart((char)c, 0);
    return c;
}

int puts(const char *s)
{
    if (s) {
        while (*s)
            out_uart(*s++, 0);
    }
    out_uart('\n', 0);
    return 0;
}

/* Compatibility with fw/common/fmt.h used by fw/common/uart.c and common_cmds.c */
void fmt_vformat(out_fn out, void *ctx, const char *fmt, va_list ap)
{
    vformat(out, ctx, fmt, ap);
}

int parse_u32(const char *s, uint32_t *out)
{
    uint32_t v = 0, base = 10;
    if (!s || !*s) return -1;
    if (s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) { base = 16; s += 2; if (!*s) return -1; }
    while (*s) {
        char c = *s++;
        uint32_t d;
        if (c >= '0' && c <= '9') d = (uint32_t)(c - '0');
        else if (base == 16 && c >= 'a' && c <= 'f') d = (uint32_t)(c - 'a' + 10);
        else if (base == 16 && c >= 'A' && c <= 'F') d = (uint32_t)(c - 'A' + 10);
        else return -1;
        v = v * base + d;
    }
    *out = v;
    return 0;
}
