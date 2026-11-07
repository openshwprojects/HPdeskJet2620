/*
 * softfp.c - Pure MIPS-I software floating-point (IEEE-754 SFmode/DFmode),
 * 64-bit integer division/shifts, and <math.h> elementary functions.
 *
 * Why this exists: the Codescape mips-mti-elf toolchain only ships multilibs
 * for mips32r2, whose libgcc.a / libm.a contain mul/clz/movn/movz/ext/ins
 * instructions that trap with Reserved Instruction (RI) on the Lexra RLX4181.
 * Compiling this file with -march=mips1 -mabi=32 -msoft-float produces 100%
 * pure MIPS-I code using hardware multu/divu.
 */
#include <stdint.h>
#include <stddef.h>

typedef uint32_t su_int;
typedef int32_t  si_int;
typedef uint64_t du_int;
typedef int64_t  di_int;

typedef union { float f; uint32_t u; int32_t s; } fp_sf;
typedef union { double d; uint64_t u; struct { uint32_t hi, lo; } w; } fp_df;

/* ========================================================================
 * 1. Bit / 64-bit integer compiler-rt helpers (__clzsi2, shifts, 64-bit div)
 * ======================================================================== */

int __clzsi2(uint32_t x)
{
    if (x == 0) return 32;
    int n = 0;
    if ((x & 0xFFFF0000u) == 0) { n += 16; x <<= 16; }
    if ((x & 0xFF000000u) == 0) { n += 8;  x <<= 8;  }
    if ((x & 0xF0000000u) == 0) { n += 4;  x <<= 4;  }
    if ((x & 0xC0000000u) == 0) { n += 2;  x <<= 2;  }
    if ((x & 0x80000000u) == 0) { n += 1; }
    return n;
}

int __ctzsi2(uint32_t x)
{
    if (x == 0) return 32;
    int n = 0;
    if ((x & 0x0000FFFFu) == 0) { n += 16; x >>= 16; }
    if ((x & 0x000000FFu) == 0) { n += 8;  x >>= 8;  }
    if ((x & 0x0000000Fu) == 0) { n += 4;  x >>= 4;  }
    if ((x & 0x00000003u) == 0) { n += 2;  x >>= 2;  }
    if ((x & 0x00000001u) == 0) { n += 1; }
    return n;
}

int __clzdi2(uint64_t x)
{
    uint32_t hi = (uint32_t)(x >> 32);
    return hi ? __clzsi2(hi) : 32 + __clzsi2((uint32_t)x);
}

int __ffssi2(uint32_t x)
{
    return x ? (__ctzsi2(x) + 1) : 0;
}

int __ffsdi2(uint64_t x)
{
    if (x == 0) return 0;
    uint32_t lo = (uint32_t)x;
    return lo ? (__ctzsi2(lo) + 1) : (32 + __ctzsi2((uint32_t)(x >> 32)) + 1);
}

int __popcountsi2(uint32_t x)
{
    int c = 0;
    while (x) { x &= x - 1; c++; }
    return c;
}

uint32_t __bswapsi2(uint32_t x)
{
    return ((x & 0xFF000000u) >> 24) |
           ((x & 0x00FF0000u) >> 8)  |
           ((x & 0x0000FF00u) << 8)  |
           ((x & 0x000000FFu) << 24);
}

/* Big-endian 64-bit word union */
typedef union {
    uint64_t u;
    int64_t  s;
    struct { uint32_t hi, lo; } w;
} dw_t;

di_int __ashldi3(di_int a, int b)
{
    if (b <= 0) return a;
    if (b >= 64) return 0;
    dw_t u; u.s = a;
    if (b >= 32) {
        u.w.hi = u.w.lo << (b - 32);
        u.w.lo = 0;
    } else {
        u.w.hi = (u.w.hi << b) | (u.w.lo >> (32 - b));
        u.w.lo = u.w.lo << b;
    }
    return u.s;
}

di_int __lshrdi3(di_int a, int b)
{
    if (b <= 0) return a;
    if (b >= 64) return 0;
    dw_t u; u.s = a;
    if (b >= 32) {
        u.w.lo = u.w.hi >> (b - 32);
        u.w.hi = 0;
    } else {
        u.w.lo = (u.w.lo >> b) | (u.w.hi << (32 - b));
        u.w.hi = u.w.hi >> b;
    }
    return u.s;
}

di_int __ashrdi3(di_int a, int b)
{
    if (b <= 0) return a;
    dw_t u; u.s = a;
    int32_t shi = (int32_t)u.w.hi;
    if (b >= 64) {
        u.w.hi = (uint32_t)(shi >> 31);
        u.w.lo = u.w.hi;
    } else if (b >= 32) {
        u.w.lo = (uint32_t)(shi >> (b - 32));
        u.w.hi = (uint32_t)(shi >> 31);
    } else {
        u.w.lo = (u.w.lo >> b) | (u.w.hi << (32 - b));
        u.w.hi = (uint32_t)(shi >> b);
    }
    return u.s;
}

static du_int udivmoddi4(du_int num, du_int den, du_int *rem_p)
{
    if (den == 0) {
        if (rem_p) *rem_p = 0;
        return 0;
    }
    if (num < den) {
        if (rem_p) *rem_p = num;
        return 0;
    }
    /* Fast path when both fit in 32 bits */
    if ((num >> 32) == 0 && (den >> 32) == 0) {
        uint32_t n32 = (uint32_t)num, d32 = (uint32_t)den;
        if (rem_p) *rem_p = n32 % d32;
        return n32 / d32;
    }
    int shift = __clzdi2(den) - __clzdi2(num);
    den <<= shift;
    du_int quot = 0;
    for (; shift >= 0; shift--) {
        quot <<= 1;
        if (num >= den) {
            num -= den;
            quot |= 1u;
        }
        den >>= 1;
    }
    if (rem_p) *rem_p = num;
    return quot;
}

du_int __udivdi3(du_int a, du_int b)
{
    return udivmoddi4(a, b, NULL);
}

du_int __umoddi3(du_int a, du_int b)
{
    du_int r = 0;
    udivmoddi4(a, b, &r);
    return r;
}

di_int __divdi3(di_int a, di_int b)
{
    int neg = 0;
    du_int ua = (du_int)a, ub = (du_int)b;
    if (a < 0) { ua = -ua; neg ^= 1; }
    if (b < 0) { ub = -ub; neg ^= 1; }
    du_int q = udivmoddi4(ua, ub, NULL);
    return neg ? -(di_int)q : (di_int)q;
}

di_int __moddi3(di_int a, di_int b)
{
    int neg = 0;
    du_int ua = (du_int)a, ub = (du_int)b, r = 0;
    if (a < 0) { ua = -ua; neg = 1; }
    if (b < 0) { ub = -ub; }
    udivmoddi4(ua, ub, &r);
    return neg ? -(di_int)r : (di_int)r;
}

/* ========================================================================
 * 2. Single-precision (float / SFmode) IEEE-754 operations
 * ======================================================================== */

static inline uint32_t pack_sf(int sign, int exp, uint32_t mant)
{
    return ((uint32_t)sign << 31) | ((uint32_t)(exp & 0xFF) << 23) | (mant & 0x7FFFFFu);
}

float __negsf2(float a)
{
    fp_sf u = { .f = a };
    u.u ^= 0x80000000u;
    return u.f;
}

static uint32_t addsub_sf(uint32_t a, uint32_t b, int sub)
{
    if (sub) b ^= 0x80000000u;

    int sa = (int)(a >> 31), sb = (int)(b >> 31);
    int ea = (int)((a >> 23) & 0xFF), eb = (int)((b >> 23) & 0xFF);
    uint32_t ma = a & 0x7FFFFFu, mb = b & 0x7FFFFFu;

    /* NaN / Inf handling */
    if (ea == 0xFF) {
        if (ma) return a | 0x00400000u;
        if (eb == 0xFF && (mb || sa != sb)) return 0x7FC00000u;
        return a;
    }
    if (eb == 0xFF) {
        if (mb) return b | 0x00400000u;
        return b;
    }
    if (ea == 0 && ma == 0) {
        if (eb == 0 && mb == 0) return (sa & sb) ? 0x80000000u : 0;
        return b;
    }
    if (eb == 0 && mb == 0) return a;

    if (ea == 0) { ea = 1; } else { ma |= 0x800000u; }
    if (eb == 0) { eb = 1; } else { mb |= 0x800000u; }

    /* Shift left by 3 for guard/round/sticky bits */
    ma <<= 3;
    mb <<= 3;

    if (ea < eb || (ea == eb && ma < mb)) {
        int ts = sa; sa = sb; sb = ts;
        int te = ea; ea = eb; eb = te;
        uint32_t tm = ma; ma = mb; mb = tm;
    }

    int diff = ea - eb;
    if (diff >= 28) {
        mb = (mb != 0) ? 1u : 0u;
    } else if (diff > 0) {
        uint32_t sticky = (mb & ((1u << diff) - 1u)) ? 1u : 0u;
        mb = (mb >> diff) | sticky;
    }

    uint32_t m;
    int e = ea;
    if (sa == sb) {
        m = ma + mb;
        if (m & (1u << 27)) {
            uint32_t sticky = m & 1u;
            m = (m >> 1) | sticky;
            e++;
        }
    } else {
        m = ma - mb;
        if (m == 0) return 0;
        int lz = __clzsi2(m) - 5; /* bit 26 is at position 31 - 5 */
        if (lz > 0) {
            if (lz >= e) {
                m <<= (e - 1);
                e = 0;
            } else {
                m <<= lz;
                e -= lz;
            }
        }
    }

    /* Round to nearest even using bottom 3 bits */
    uint32_t rem = m & 7u;
    m >>= 3;
    if (rem > 4u || (rem == 4u && (m & 1u))) {
        m++;
        if (m & (1u << 24)) {
            m >>= 1;
            e++;
        }
    }

    if (e >= 0xFF) return pack_sf(sa, 0xFF, 0);
    if (m < 0x800000u) e = 0;
    return pack_sf(sa, e, m);
}

float __addsf3(float a, float b)
{
    fp_sf ua = { .f = a }, ub = { .f = b }, ur;
    ur.u = addsub_sf(ua.u, ub.u, 0);
    return ur.f;
}

float __subsf3(float a, float b)
{
    fp_sf ua = { .f = a }, ub = { .f = b }, ur;
    ur.u = addsub_sf(ua.u, ub.u, 1);
    return ur.f;
}

float __mulsf3(float a, float b)
{
    fp_sf ua = { .f = a }, ub = { .f = b }, ur;
    int s = (int)((ua.u ^ ub.u) >> 31);
    int ea = (int)((ua.u >> 23) & 0xFF), eb = (int)((ub.u >> 23) & 0xFF);
    uint32_t ma = ua.u & 0x7FFFFFu, mb = ub.u & 0x7FFFFFu;

    if (ea == 0xFF) {
        if (ma || (eb == 0 && mb == 0)) { ur.u = 0x7FC00000u; return ur.f; }
        if (eb == 0xFF && mb) { ur.u = 0x7FC00000u; return ur.f; }
        ur.u = pack_sf(s, 0xFF, 0); return ur.f;
    }
    if (eb == 0xFF) {
        if (mb || (ea == 0 && ma == 0)) { ur.u = 0x7FC00000u; return ur.f; }
        ur.u = pack_sf(s, 0xFF, 0); return ur.f;
    }
    if ((ea == 0 && ma == 0) || (eb == 0 && mb == 0)) {
        ur.u = (uint32_t)s << 31; return ur.f;
    }

    if (ea == 0) {
        int lz = __clzsi2(ma) - 8;
        ma <<= lz; ea = 1 - lz;
    } else ma |= 0x800000u;

    if (eb == 0) {
        int lz = __clzsi2(mb) - 8;
        mb <<= lz; eb = 1 - lz;
    } else mb |= 0x800000u;

    int e = ea + eb - 127;
    uint64_t prod = (uint64_t)ma * (uint64_t)mb; /* 47 or 48 bits */
    if (prod & (1ULL << 47)) {
        e++;
        uint32_t sticky = (prod & 0xFFFFu) ? 1u : 0u;
        ma = (uint32_t)(prod >> 21) | sticky;
    } else {
        uint32_t sticky = (prod & 0x7FFFu) ? 1u : 0u;
        ma = (uint32_t)(prod >> 20) | sticky;
    }

    if (e <= 0) {
        int shift = 1 - e;
        if (shift >= 28) {
            ur.u = (uint32_t)s << 31;
            return ur.f;
        }
        uint32_t sticky = (ma & ((1u << shift) - 1u)) ? 1u : 0u;
        ma = (ma >> shift) | sticky;
        e = 0;
    }

    uint32_t rem = ma & 7u;
    ma >>= 3;
    if (rem > 4u || (rem == 4u && (ma & 1u))) {
        ma++;
        if (ma & (1u << 24)) {
            ma >>= 1;
            e++;
        } else if (e == 0 && (ma & 0x800000u)) {
            e = 1;
        }
    }

    if (e >= 0xFF) { ur.u = pack_sf(s, 0xFF, 0); return ur.f; }
    ur.u = pack_sf(s, e, ma);
    return ur.f;
}

float __divsf3(float a, float b)
{
    fp_sf ua = { .f = a }, ub = { .f = b }, ur;
    int s = (int)((ua.u ^ ub.u) >> 31);
    int ea = (int)((ua.u >> 23) & 0xFF), eb = (int)((ub.u >> 23) & 0xFF);
    uint32_t ma = ua.u & 0x7FFFFFu, mb = ub.u & 0x7FFFFFu;

    if (ea == 0xFF) {
        if (ma || eb == 0xFF) { ur.u = 0x7FC00000u; return ur.f; }
        ur.u = pack_sf(s, 0xFF, 0); return ur.f;
    }
    if (eb == 0xFF) {
        if (mb) { ur.u = 0x7FC00000u; return ur.f; }
        ur.u = (uint32_t)s << 31; return ur.f;
    }
    if (eb == 0 && mb == 0) {
        if (ea == 0 && ma == 0) { ur.u = 0x7FC00000u; return ur.f; }
        ur.u = pack_sf(s, 0xFF, 0); return ur.f;
    }
    if (ea == 0 && ma == 0) {
        ur.u = (uint32_t)s << 31; return ur.f;
    }

    if (ea == 0) { int lz = __clzsi2(ma) - 8; ma <<= lz; ea = 1 - lz; } else ma |= 0x800000u;
    if (eb == 0) { int lz = __clzsi2(mb) - 8; mb <<= lz; eb = 1 - lz; } else mb |= 0x800000u;

    int e = ea - eb + 127;
    if (ma < mb) { ma <<= 1; e--; }

    /* Compute 26 bits of quotient (bit 26..0) via 2-step 32-bit division */
    uint32_t q1 = (ma << 7) / mb;
    uint32_t r1 = (ma << 7) % mb;
    uint32_t q2 = (r1 << 7) / mb;
    uint32_t r2 = (r1 << 7) % mb;
    uint32_t q3 = (r2 << 12) / mb;
    uint32_t r3 = (r2 << 12) % mb;
    uint32_t q = (q1 << 19) | (q2 << 12) | q3;
    if (r3) q |= 1u;

    if (e <= 0) {
        int shift = 1 - e;
        if (shift >= 28) { ur.u = (uint32_t)s << 31; return ur.f; }
        uint32_t sticky = (q & ((1u << shift) - 1u)) ? 1u : 0u;
        q = (q >> shift) | sticky;
        e = 0;
    }

    uint32_t rem = q & 7u;
    q >>= 3;
    if (rem > 4u || (rem == 4u && (q & 1u))) {
        q++;
        if (q & (1u << 24)) { q >>= 1; e++; }
        else if (e == 0 && (q & 0x800000u)) e = 1;
    }

    if (e >= 0xFF) { ur.u = pack_sf(s, 0xFF, 0); return ur.f; }
    ur.u = pack_sf(s, e, q);
    return ur.f;
}

float __floatunsisf(uint32_t a)
{
    if (a == 0) return 0.0f;
    int lz = __clzsi2(a);
    int e = 127 + 31 - lz;
    uint32_t m;
    if (lz <= 8) {
        int shift = 8 - lz;
        if (shift == 0) {
            m = a & 0x7FFFFFu;
        } else {
            uint32_t rem = a & ((1u << shift) - 1u);
            uint32_t half = 1u << (shift - 1);
            m = a >> shift;
            if (rem > half || (rem == half && (m & 1u))) {
                m++;
                if (m & (1u << 24)) { m >>= 1; e++; }
            }
        }
    } else {
        m = a << (lz - 8);
    }
    fp_sf ur = { .u = pack_sf(0, e, m) };
    return ur.f;
}

float __floatsisf(int32_t a)
{
    if (a == 0) return 0.0f;
    int s = a < 0;
    uint32_t u = s ? (uint32_t)(-(uint32_t)a) : (uint32_t)a;
    fp_sf ur = { .f = __floatunsisf(u) };
    if (s) ur.u |= 0x80000000u;
    return ur.f;
}

float __floatundisf(uint64_t a)
{
    if ((a >> 32) == 0) return __floatunsisf((uint32_t)a);
    int lz = __clzsi2((uint32_t)(a >> 32));
    int shift = 32 - lz;
    uint32_t top = (uint32_t)((a >> shift) | ((a & ((1ULL << shift) - 1ULL)) ? 1u : 0u));
    fp_sf ur = { .f = __floatunsisf(top) };
    ur.u += (uint32_t)shift << 23;
    return ur.f;
}

float __floatdisf(int64_t a)
{
    if (a == 0) return 0.0f;
    int s = a < 0;
    uint64_t u = s ? (uint64_t)(-a) : (uint64_t)a;
    fp_sf ur = { .f = __floatundisf(u) };
    if (s) ur.u |= 0x80000000u;
    return ur.f;
}

int32_t __fixsfsi(float a)
{
    fp_sf ua = { .f = a };
    int s = (int)(ua.u >> 31);
    int e = (int)((ua.u >> 23) & 0xFF) - 127;
    if (e < 0) return 0;
    if (e >= 31) return s ? (int32_t)0x80000000u : 0x7FFFFFFF;
    uint32_t m = (ua.u & 0x7FFFFFu) | 0x800000u;
    uint32_t r = (e >= 23) ? (m << (e - 23)) : (m >> (23 - e));
    return s ? -(int32_t)r : (int32_t)r;
}

uint32_t __fixunssfsi(float a)
{
    fp_sf ua = { .f = a };
    if (ua.u >> 31) return 0;
    int e = (int)((ua.u >> 23) & 0xFF) - 127;
    if (e < 0) return 0;
    if (e >= 32) return 0xFFFFFFFFu;
    uint32_t m = (ua.u & 0x7FFFFFu) | 0x800000u;
    return (e >= 23) ? (m << (e - 23)) : (m >> (23 - e));
}

int __unordsf2(float a, float b)
{
    fp_sf ua = { .f = a }, ub = { .f = b };
    return ((ua.u & 0x7FFFFFFFu) > 0x7F800000u) || ((ub.u & 0x7FFFFFFFu) > 0x7F800000u);
}

static int cmp_sf(float a, float b, int nan_ret)
{
    fp_sf ua = { .f = a }, ub = { .f = b };
    if (((ua.u & 0x7FFFFFFFu) > 0x7F800000u) || ((ub.u & 0x7FFFFFFFu) > 0x7F800000u))
        return nan_ret;
    if (((ua.u | ub.u) & 0x7FFFFFFFu) == 0)
        return 0;
    int32_t sa = ua.s, sb = ub.s;
    if (sa < 0 && sb < 0) {
        if (ua.u == ub.u) return 0;
        return (ua.u > ub.u) ? -1 : 1;
    }
    if (sa == sb) return 0;
    return (sa < sb) ? -1 : 1;
}

int __eqsf2(float a, float b) { return cmp_sf(a, b, 1); }
int __nesf2(float a, float b) { return cmp_sf(a, b, 1); }
int __ltsf2(float a, float b) { return cmp_sf(a, b, 1); }
int __lesf2(float a, float b) { return cmp_sf(a, b, 1); }
int __gtsf2(float a, float b) { return cmp_sf(a, b, -1); }
int __gesf2(float a, float b) { return cmp_sf(a, b, -1); }

/* ========================================================================
 * 3. Double-precision (double / DFmode) IEEE-754 operations
 *    Conversions between float and double are exact bit-level IEEE-754;
 *    arithmetic delegates via single-precision or 64-bit integer mantissa.
 * ======================================================================== */

double __extendsfdf2(float a)
{
    fp_sf ua = { .f = a };
    fp_df ur;
    uint32_t s = ua.u >> 31;
    uint32_t e = (ua.u >> 23) & 0xFF;
    uint32_t m = ua.u & 0x7FFFFFu;

    if (e == 0xFF) {
        ur.w.hi = (s << 31) | (0x7FFu << 20) | (m >> 3);
        ur.w.lo = m << 29;
        return ur.d;
    }
    if (e == 0) {
        if (m == 0) {
            ur.w.hi = s << 31;
            ur.w.lo = 0;
            return ur.d;
        }
        int lz = __clzsi2(m) - 8;
        m = (m << lz) & 0x7FFFFFu;
        e = 1 - lz;
    }
    uint32_t de = e - 127 + 1023;
    ur.w.hi = (s << 31) | (de << 20) | (m >> 3);
    ur.w.lo = m << 29;
    return ur.d;
}

float __truncdfsf2(double a)
{
    fp_df ua = { .d = a };
    fp_sf ur;
    uint32_t s = ua.w.hi >> 31;
    int32_t  e = (int32_t)((ua.w.hi >> 20) & 0x7FF);
    uint32_t mhi = ua.w.hi & 0xFFFFFu;
    uint32_t mlo = ua.w.lo;

    if (e == 0x7FF) {
        ur.u = (s << 31) | 0x7F800000u | ((mhi || mlo) ? 0x400000u : 0);
        return ur.f;
    }
    if (e == 0 && mhi == 0 && mlo == 0) {
        ur.u = s << 31;
        return ur.f;
    }
    int32_t se = e - 1023 + 127;
    uint32_t m = (mhi << 3) | (mlo >> 29);
    uint32_t rem = mlo & 0x1FFFFFFFu;
    if (rem > 0x10000000u || (rem == 0x10000000u && (m & 1u))) {
        m++;
        if (m & 0x800000u) { m = 0; se++; }
    }
    if (se >= 0xFF) { ur.u = pack_sf((int)s, 0xFF, 0); return ur.f; }
    if (se <= 0) {
        if (se < -23) { ur.u = s << 31; return ur.f; }
        m = (m | 0x800000u) >> (1 - se);
        se = 0;
    }
    ur.u = pack_sf((int)s, (int)se, m);
    return ur.f;
}

double __negdf2(double a)
{
    fp_df ua = { .d = a };
    ua.w.hi ^= 0x80000000u;
    return ua.d;
}

double __adddf3(double a, double b) { return __extendsfdf2(__addsf3(__truncdfsf2(a), __truncdfsf2(b))); }
double __subdf3(double a, double b) { return __extendsfdf2(__subsf3(__truncdfsf2(a), __truncdfsf2(b))); }
double __muldf3(double a, double b) { return __extendsfdf2(__mulsf3(__truncdfsf2(a), __truncdfsf2(b))); }
double __divdf3(double a, double b) { return __extendsfdf2(__divsf3(__truncdfsf2(a), __truncdfsf2(b))); }

double __floatsidf(int32_t a)    { return __extendsfdf2(__floatsisf(a)); }
double __floatunsidf(uint32_t a) { return __extendsfdf2(__floatunsisf(a)); }
double __floatdidf(int64_t a)    { return __extendsfdf2(__floatdisf(a)); }
double __floatundidf(uint64_t a) { return __extendsfdf2(__floatundisf(a)); }

int32_t  __fixdfsi(double a)    { return __fixsfsi(__truncdfsf2(a)); }
uint32_t __fixunsdfsi(double a) { return __fixunssfsi(__truncdfsf2(a)); }
int64_t  __fixdfdi(double a)    { return (int64_t)__fixsfsi(__truncdfsf2(a)); }
uint64_t __fixunsdfdi(double a) { return (uint64_t)__fixunssfsi(__truncdfsf2(a)); }

int __unorddf2(double a, double b) { return __unordsf2(__truncdfsf2(a), __truncdfsf2(b)); }
int __eqdf2(double a, double b) { return __eqsf2(__truncdfsf2(a), __truncdfsf2(b)); }
int __nedf2(double a, double b) { return __nesf2(__truncdfsf2(a), __truncdfsf2(b)); }
int __ltdf2(double a, double b) { return __ltsf2(__truncdfsf2(a), __truncdfsf2(b)); }
int __ledf2(double a, double b) { return __lesf2(__truncdfsf2(a), __truncdfsf2(b)); }
int __gtdf2(double a, double b) { return __gtsf2(__truncdfsf2(a), __truncdfsf2(b)); }
int __gedf2(double a, double b) { return __gesf2(__truncdfsf2(a), __truncdfsf2(b)); }

/* ========================================================================
 * 4. <math.h> elementary functions
 * ======================================================================== */

#define M_PI_F   3.14159265f
#define M_PI_2_F 1.57079632f
#define M_LN2_F  0.69314718f
#define M_LN10_F 2.30258509f

float fabsf(float x)
{
    fp_sf u = { .f = x };
    u.u &= 0x7FFFFFFFu;
    return u.f;
}
double fabs(double x) { return (double)fabsf((float)x); }

float floorf(float x)
{
    int32_t i = (int32_t)x;
    return (x < (float)i) ? (float)(i - 1) : (float)i;
}
double floor(double x) { return (double)floorf((float)x); }

float ceilf(float x)
{
    int32_t i = (int32_t)x;
    return (x > (float)i) ? (float)(i + 1) : (float)i;
}
double ceil(double x) { return (double)ceilf((float)x); }

float roundf(float x)
{
    return (x >= 0.0f) ? floorf(x + 0.5f) : ceilf(x - 0.5f);
}
double round(double x) { return (double)roundf((float)x); }

float truncf(float x)
{
    return (float)(int32_t)x;
}
double trunc(double x) { return (double)truncf((float)x); }

float fmodf(float x, float y)
{
    if (y == 0.0f) return 0.0f;
    int32_t q = (int32_t)(x / y);
    return x - (float)q * y;
}
double fmod(double x, double y) { return (double)fmodf((float)x, (float)y); }

double modf(double x, double *iptr)
{
    float ix = truncf((float)x);
    if (iptr) *iptr = (double)ix;
    return (double)((float)x - ix);
}

float sqrtf(float x)
{
    if (x <= 0.0f) return 0.0f;
    fp_sf u = { .f = x };
    u.u = (u.u >> 1) + 0x1FC00000u;
    float g = u.f;
    for (int i = 0; i < 4; i++)
        g = 0.5f * (g + x / g);
    return g;
}
double sqrt(double x) { return (double)sqrtf((float)x); }

float expf(float x)
{
    if (x < -80.0f) return 0.0f;
    if (x > 80.0f)  return 3.4028234e38f;
    int32_t k = (int32_t)(x / M_LN2_F + (x >= 0.0f ? 0.5f : -0.5f));
    float r = x - (float)k * M_LN2_F;
    float p = 1.0f + r * (1.0f + r * (0.5f + r * (0.16666667f + r * (0.04166667f + r * 0.00833333f))));
    fp_sf scale = { .u = (uint32_t)((127 + k) & 0xFF) << 23 };
    return p * scale.f;
}
double exp(double x) { return (double)expf((float)x); }

float logf(float x)
{
    if (x <= 0.0f) return -3.4028234e38f;
    fp_sf u = { .f = x };
    int e = (int)((u.u >> 23) & 0xFF) - 127;
    u.u = (u.u & 0x7FFFFFu) | 0x3F800000u;
    float m = u.f;
    float y = (m - 1.0f) / (m + 1.0f);
    float y2 = y * y;
    float ln_m = 2.0f * y * (1.0f + y2 * (0.33333333f + y2 * (0.2f + y2 * (0.14285714f + y2 * 0.11111111f))));
    return ln_m + (float)e * M_LN2_F;
}
double log(double x) { return (double)logf((float)x); }

float log10f(float x) { return logf(x) / M_LN10_F; }
double log10(double x) { return (double)log10f((float)x); }
double log2(double x) { return (double)(logf((float)x) / M_LN2_F); }

float powf(float x, float y)
{
    if (y == 0.0f) return 1.0f;
    if (x == 0.0f) return 0.0f;
    int neg = 0;
    if (x < 0.0f) {
        int32_t yi = (int32_t)y;
        if ((float)yi != y) return 0.0f;
        if (yi & 1) neg = 1;
        x = -x;
    }
    float r = expf(y * logf(x));
    return neg ? -r : r;
}
double pow(double x, double y) { return (double)powf((float)x, (float)y); }

float sinf(float x)
{
    int32_t k = (int32_t)(x / (2.0f * M_PI_F));
    x -= (float)k * (2.0f * M_PI_F);
    if (x > M_PI_F)  x -= 2.0f * M_PI_F;
    if (x < -M_PI_F) x += 2.0f * M_PI_F;
    if (x > M_PI_2_F)  x = M_PI_F - x;
    if (x < -M_PI_2_F) x = -M_PI_F - x;
    float x2 = x * x;
    return x * (1.0f - x2 * (0.16666667f - x2 * (0.00833333f - x2 * 0.00019841f)));
}
double sin(double x) { return (double)sinf((float)x); }

float cosf(float x)
{
    return sinf(x + M_PI_2_F);
}
double cos(double x) { return (double)cosf((float)x); }

float tanf(float x)
{
    float c = cosf(x);
    return (fabsf(c) < 1e-6f) ? 0.0f : (sinf(x) / c);
}
double tan(double x) { return (double)tanf((float)x); }

float atanf(float x)
{
    int neg = x < 0.0f;
    if (neg) x = -x;
    int inv = x > 1.0f;
    if (inv) x = 1.0f / x;
    float x2 = x * x;
    float r = x * (0.9998660f + x2 * (-0.3302995f + x2 * (0.1801410f + x2 * (-0.0851330f + x2 * 0.0208351f))));
    if (inv) r = M_PI_2_F - r;
    return neg ? -r : r;
}
double atan(double x) { return (double)atanf((float)x); }

float atan2f(float y, float x)
{
    if (x > 0.0f) return atanf(y / x);
    if (x < 0.0f) return (y >= 0.0f) ? (atanf(y / x) + M_PI_F) : (atanf(y / x) - M_PI_F);
    if (y > 0.0f) return M_PI_2_F;
    if (y < 0.0f) return -M_PI_2_F;
    return 0.0f;
}
double atan2(double y, double x) { return (double)atan2f((float)y, (float)x); }

float asinf(float x)
{
    if (x >= 1.0f) return M_PI_2_F;
    if (x <= -1.0f) return -M_PI_2_F;
    return atan2f(x, sqrtf(1.0f - x * x));
}
double asin(double x) { return (double)asinf((float)x); }

float acosf(float x)
{
    return M_PI_2_F - asinf(x);
}
double acos(double x) { return (double)acosf((float)x); }

float sinhf(float x)
{
    return 0.5f * (expf(x) - expf(-x));
}
double sinh(double x) { return (double)sinhf((float)x); }

float coshf(float x)
{
    return 0.5f * (expf(x) + expf(-x));
}
double cosh(double x) { return (double)coshf((float)x); }

float tanhf(float x)
{
    float ep = expf(x), em = expf(-x);
    return (ep - em) / (ep + em);
}
double tanh(double x) { return (double)tanhf((float)x); }

/* ============================================================================
 * 32-bit integer division and ARM EABI (__aeabi_*) runtime helpers
 * ========================================================================== */

uint32_t __udivsi3(uint32_t n, uint32_t d)
{
    if (d == 0) return 0;
    uint32_t q = 0, r = 0;
    for (int i = 31; i >= 0; i--) {
        r = (r << 1) | ((n >> i) & 1u);
        if (r >= d) {
            r -= d;
            q |= (1u << i);
        }
    }
    return q;
}

uint32_t __umodsi3(uint32_t n, uint32_t d)
{
    if (d == 0) return 0;
    uint32_t q = __udivsi3(n, d);
    return n - q * d;
}

int32_t __divsi3(int32_t a, int32_t b)
{
    int neg = 0;
    uint32_t ua, ub;
    if (a < 0) { ua = (uint32_t)(-(uint32_t)a); neg ^= 1; } else ua = (uint32_t)a;
    if (b < 0) { ub = (uint32_t)(-(uint32_t)b); neg ^= 1; } else ub = (uint32_t)b;
    uint32_t q = __udivsi3(ua, ub);
    return neg ? -(int32_t)q : (int32_t)q;
}

int32_t __modsi3(int32_t a, int32_t b)
{
    int neg = (a < 0);
    uint32_t ua = (a < 0) ? (uint32_t)(-(uint32_t)a) : (uint32_t)a;
    uint32_t ub = (b < 0) ? (uint32_t)(-(uint32_t)b) : (uint32_t)b;
    uint32_t r = __umodsi3(ua, ub);
    return neg ? -(int32_t)r : (int32_t)r;
}

long long __aeabi_llsl(long long a, int b) { return __ashldi3(a, b); }
long long __aeabi_llsr(long long a, int b) { return __lshrdi3(a, b); }
long long __aeabi_lasr(long long a, int b) { return __ashrdi3(a, b); }

float  __aeabi_fadd(float a, float b)   { return __addsf3(a, b); }
float  __aeabi_fsub(float a, float b)   { return __subsf3(a, b); }
float  __aeabi_frsub(float a, float b)  { return __subsf3(b, a); }
float  __aeabi_fmul(float a, float b)   { return __mulsf3(a, b); }
float  __aeabi_fdiv(float a, float b)   { return __divsf3(a, b); }

double __aeabi_dadd(double a, double b)  { return __adddf3(a, b); }
double __aeabi_dsub(double a, double b)  { return __subdf3(a, b); }
double __aeabi_drsub(double a, double b) { return __subdf3(b, a); }
double __aeabi_dmul(double a, double b)  { return __muldf3(a, b); }
double __aeabi_ddiv(double a, double b)  { return __divdf3(a, b); }

double __aeabi_f2d(float a)             { return __extendsfdf2(a); }
float  __aeabi_d2f(double a)            { return __truncdfsf2(a); }

float  __aeabi_i2f(int a)               { return __floatsisf(a); }
float  __aeabi_ui2f(unsigned int a)     { return __floatunsisf(a); }
double __aeabi_i2d(int a)               { return __floatsidf(a); }
double __aeabi_ui2d(unsigned int a)     { return __floatunsidf(a); }
float  __aeabi_l2f(long long a)         { return __floatdisf(a); }
float  __aeabi_ul2f(unsigned long long a) { return __floatundisf(a); }
double __aeabi_l2d(long long a)         { return __floatdidf(a); }
double __aeabi_ul2d(unsigned long long a) { return __floatundidf(a); }

int                __aeabi_f2iz(float a)   { return __fixsfsi(a); }
unsigned int       __aeabi_f2uiz(float a)  { return __fixunssfsi(a); }
int                __aeabi_d2iz(double a)  { return __fixdfsi(a); }
unsigned int       __aeabi_d2uiz(double a) { return __fixunsdfsi(a); }
long long          __aeabi_f2lz(float a)   { return __fixsfdi(a); }
unsigned long long __aeabi_f2ulz(float a)  { return __fixunssfdi(a); }
long long          __aeabi_d2lz(double a)  { return __fixdfdi(a); }
unsigned long long __aeabi_d2ulz(double a) { return __fixunsdfdi(a); }

int __aeabi_fcmpeq(float a, float b) { return (!__unordsf2(a, b) && __eqsf2(a, b) == 0) ? 1 : 0; }
int __aeabi_fcmplt(float a, float b) { return (!__unordsf2(a, b) && __ltsf2(a, b) <  0) ? 1 : 0; }
int __aeabi_fcmple(float a, float b) { return (!__unordsf2(a, b) && __lesf2(a, b) <= 0) ? 1 : 0; }
int __aeabi_fcmpgt(float a, float b) { return (!__unordsf2(a, b) && __gtsf2(a, b) >  0) ? 1 : 0; }
int __aeabi_fcmpge(float a, float b) { return (!__unordsf2(a, b) && __gesf2(a, b) >= 0) ? 1 : 0; }
int __aeabi_fcmpun(float a, float b) { return __unordsf2(a, b) ? 1 : 0; }

int __aeabi_dcmpeq(double a, double b) { return (!__unorddf2(a, b) && __eqdf2(a, b) == 0) ? 1 : 0; }
int __aeabi_dcmplt(double a, double b) { return (!__unorddf2(a, b) && __ltdf2(a, b) <  0) ? 1 : 0; }
int __aeabi_dcmple(double a, double b) { return (!__unorddf2(a, b) && __ledf2(a, b) <= 0) ? 1 : 0; }
int __aeabi_dcmpgt(double a, double b) { return (!__unorddf2(a, b) && __gtdf2(a, b) >  0) ? 1 : 0; }
int __aeabi_dcmpge(double a, double b) { return (!__unorddf2(a, b) && __gedf2(a, b) >= 0) ? 1 : 0; }
int __aeabi_dcmpun(double a, double b) { return __unorddf2(a, b) ? 1 : 0; }

extern void *memcpy(void *dst, const void *src, uint32_t n);
extern void *memmove(void *dst, const void *src, uint32_t n);
extern void *memset(void *dst, int c, uint32_t n);

void __aeabi_memcpy(void *dst, const void *src, uint32_t n)  { memcpy(dst, src, n); }
void __aeabi_memcpy4(void *dst, const void *src, uint32_t n) { memcpy(dst, src, n); }
void __aeabi_memcpy8(void *dst, const void *src, uint32_t n) { memcpy(dst, src, n); }
void __aeabi_memmove(void *dst, const void *src, uint32_t n) { memmove(dst, src, n); }
void __aeabi_memmove4(void *dst, const void *src, uint32_t n){ memmove(dst, src, n); }
void __aeabi_memmove8(void *dst, const void *src, uint32_t n){ memmove(dst, src, n); }
void __aeabi_memset(void *dst, uint32_t n, int c)            { memset(dst, c, n); }
void __aeabi_memset4(void *dst, uint32_t n, int c)           { memset(dst, c, n); }
void __aeabi_memset8(void *dst, uint32_t n, int c)           { memset(dst, c, n); }
void __aeabi_memclr(void *dst, uint32_t n)                   { memset(dst, 0, n); }
void __aeabi_memclr4(void *dst, uint32_t n)                  { memset(dst, 0, n); }
void __aeabi_memclr8(void *dst, uint32_t n)                  { memset(dst, 0, n); }
