#ifndef _SHIM_MATH_H
#define _SHIM_MATH_H

#define M_PI   3.14159265358979323846
#define M_PI_2 1.57079632679489661923
#define M_E    2.71828182845904523536
#define NAN    (__builtin_nanf(""))
#define INFINITY (__builtin_inff())
#define HUGE_VAL (__builtin_inf())

#define isnan(x) ((x) != (x))
#define isinf(x) (((x) == INFINITY) || ((x) == -INFINITY))
#define isfinite(x) (!isnan(x) && !isinf(x))

double fabs(double x);
float  fabsf(float x);
double floor(double x);
float  floorf(float x);
double ceil(double x);
float  ceilf(float x);
double round(double x);
float  roundf(float x);
double trunc(double x);
float  truncf(float x);
double fmod(double x, double y);
float  fmodf(float x, float y);
double modf(double x, double *iptr);
double sqrt(double x);
float  sqrtf(float x);
double sin(double x);
float  sinf(float x);
double cos(double x);
float  cosf(float x);
double tan(double x);
float  tanf(float x);
double atan(double x);
float  atanf(float x);
double atan2(double y, double x);
float  atan2f(float y, float x);
double asin(double x);
float  asinf(float x);
double acos(double x);
float  acosf(float x);
double exp(double x);
float  expf(float x);
double log(double x);
float  logf(float x);
double log10(double x);
float  log10f(float x);
double pow(double x, double y);
float  powf(float x, float y);
double ldexp(double x, int exp);
double sinh(double x);
float  sinhf(float x);
double cosh(double x);
float  coshf(float x);
double tanh(double x);
float  tanhf(float x);

#endif
