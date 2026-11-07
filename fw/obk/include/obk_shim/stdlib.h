#ifndef _SHIM_STDLIB_H
#define _SHIM_STDLIB_H

#include <stddef.h>

#define EXIT_SUCCESS 0
#define EXIT_FAILURE 1
#define RAND_MAX     0x7FFF

void *malloc(size_t size);
void  free(void *ptr);
void *calloc(size_t nmemb, size_t size);
void *realloc(void *ptr, size_t size);

int   atoi(const char *s);
long  atol(const char *s);
long  strtol(const char *s, char **endp, int base);
unsigned long strtoul(const char *s, char **endp, int base);
long long strtoll(const char *s, char **endp, int base);
unsigned long long strtoull(const char *s, char **endp, int base);
double strtod(const char *s, char **endp);
float  strtof(const char *s, char **endp);
double atof(const char *s);

int   abs(int x);
long  labs(long x);
void  srand(unsigned int seed);
int   rand(void);
void  qsort(void *base, size_t nmemb, size_t size,
            int (*compar)(const void *, const void *));
void  abort(void);
void  exit(int status);

#endif
