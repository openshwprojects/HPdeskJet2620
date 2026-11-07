#include "fmt.h"
#include "libmini.h"

static void emit_padded(fmt_out_fn out, void *ctx, const char *s, int len,
                        int width, int left, char pad)
{
    int i;
    if (!left) for (i = len; i < width; i++) out(pad, ctx);
    for (i = 0; i < len; i++) out(s[i], ctx);
    if (left) for (i = len; i < width; i++) out(' ', ctx);
}

static int utoa_base(char *buf, uint32_t v, unsigned base, int upper)
{
    const char *digits = upper ? "0123456789ABCDEF" : "0123456789abcdef";
    char tmp[12];
    int n = 0, i;
    if (v == 0) tmp[n++] = '0';
    while (v) { tmp[n++] = digits[v % base]; v /= base; }
    for (i = 0; i < n; i++) buf[i] = tmp[n - 1 - i];
    return n;
}

void fmt_vformat(fmt_out_fn out, void *ctx, const char *fmt, va_list ap)
{
    while (*fmt) {
        if (*fmt != '%') { out(*fmt++, ctx); continue; }
        fmt++;
        if (*fmt == '%') { out('%', ctx); fmt++; continue; }
        int left = 0, width = 0;
        char pad = ' ';
        while (*fmt == '-' || *fmt == '0') {
            if (*fmt == '-') left = 1; else pad = '0';
            fmt++;
        }
        while (*fmt >= '0' && *fmt <= '9') width = width * 10 + (*fmt++ - '0');
        while (*fmt == 'l' || *fmt == 'h' || *fmt == 'z') fmt++;
        char buf[12];
        int n;
        switch (*fmt) {
        case 's': {
            const char *s = va_arg(ap, const char *);
            if (!s) s = "(null)";
            emit_padded(out, ctx, s, (int)strlen(s), width, left, ' ');
            break;
        }
        case 'c': {
            char c = (char)va_arg(ap, int);
            emit_padded(out, ctx, &c, 1, width, left, ' ');
            break;
        }
        case 'd': case 'i': {
            int32_t v = va_arg(ap, int32_t);
            int neg = v < 0;
            uint32_t u = neg ? (uint32_t)(-v) : (uint32_t)v;
            n = utoa_base(buf + 1, u, 10, 0);
            if (neg) { buf[0] = '-'; emit_padded(out, ctx, buf, n + 1, width, left, pad); }
            else emit_padded(out, ctx, buf + 1, n, width, left, pad);
            break;
        }
        case 'u':
            n = utoa_base(buf, va_arg(ap, uint32_t), 10, 0);
            emit_padded(out, ctx, buf, n, width, left, pad);
            break;
        case 'x': case 'X':
            n = utoa_base(buf, va_arg(ap, uint32_t), 16, *fmt == 'X');
            emit_padded(out, ctx, buf, n, width, left, pad);
            break;
        case 'p':
            buf[0] = '0'; buf[1] = 'x';
            n = utoa_base(buf + 2, (uint32_t)(uintptr_t)va_arg(ap, void *), 16, 0);
            emit_padded(out, ctx, buf, n + 2, width, left, ' ');
            break;
        case 0:
            return;
        default:
            out('%', ctx); out(*fmt, ctx);
            break;
        }
        fmt++;
    }
}

struct snctx { char *buf; size_t size; size_t pos; };

static void sn_out(char c, void *vctx)
{
    struct snctx *s = vctx;
    if (s->pos + 1 < s->size) s->buf[s->pos] = c;
    s->pos++;
}

int vsnprintf(char *buf, size_t size, const char *fmt, va_list ap)
{
    struct snctx s = { buf, size, 0 };
    fmt_vformat(sn_out, &s, fmt, ap);
    if (size) buf[s.pos < size ? s.pos : size - 1] = 0;
    return (int)s.pos;
}

int snprintf(char *buf, size_t size, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf, size, fmt, ap);
    va_end(ap);
    return n;
}

int parse_u32(const char *s, uint32_t *out)
{
    uint32_t v = 0;
    if (!s || !*s) return -1;
    if (s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) {
        s += 2;
        if (!*s) return -1;
        for (; *s; s++) {
            uint32_t d;
            if (*s >= '0' && *s <= '9') d = (uint32_t)(*s - '0');
            else if (*s >= 'a' && *s <= 'f') d = (uint32_t)(*s - 'a' + 10);
            else if (*s >= 'A' && *s <= 'F') d = (uint32_t)(*s - 'A' + 10);
            else return -1;
            if (v >> 28) return -1;             /* more than eight hex digits: overflow */
            v = (v << 4) | d;
        }
    } else {
        for (; *s; s++) {
            uint32_t d;
            if (*s < '0' || *s > '9') return -1;
            d = (uint32_t)(*s - '0');
            if (v > (0xFFFFFFFFU - d) / 10U) return -1;   /* decimal overflow */
            v = v * 10U + d;
        }
    }
    *out = v;
    return 0;
}
