#ifndef LIBC_H
#define LIBC_H

#include <stddef.h>
#include <stdint.h>
#include <stdarg.h>

extern int errno;
int *__errno(void);

void  *memcpy(void *dst, const void *src, size_t n);
void  *memmove(void *dst, const void *src, size_t n);
void  *memset(void *dst, int c, size_t n);
int    memcmp(const void *a, const void *b, size_t n);
void  *memchr(const void *s, int c, size_t n);
size_t strlen(const char *s);
size_t strnlen(const char *s, size_t maxlen);
int    strcmp(const char *a, const char *b);
int    strncmp(const char *a, const char *b, size_t n);
int    strcasecmp(const char *a, const char *b);
int    strncasecmp(const char *a, const char *b, size_t n);
int    stricmp(const char *a, const char *b);
int    strnicmp(const char *a, const char *b, size_t n);
char  *strcpy(char *dst, const char *src);
char  *strncpy(char *dst, const char *src, size_t n);
char  *strcat(char *dst, const char *src);
char  *strncat(char *dst, const char *src, size_t n);
char  *strchr(const char *s, int c);
char  *strrchr(const char *s, int c);
char  *strstr(const char *haystack, const char *needle);
const char *strcasestr(const char *haystack, const char *needle);
size_t strspn(const char *s, const char *accept);
size_t strcspn(const char *s, const char *reject);
char  *strpbrk(const char *s, const char *accept);
char  *strtok_r(char *str, const char *delim, char **saveptr);
char  *strtok(char *str, const char *delim);

int    atoi(const char *s);
long   atol(const char *s);
long   strtol(const char *s, char **endp, int base);
unsigned long strtoul(const char *s, char **endp, int base);
long long strtoll(const char *s, char **endp, int base);
unsigned long long strtoull(const char *s, char **endp, int base);
double strtod(const char *s, char **endp);
float  strtof(const char *s, char **endp);
double atof(const char *s);

int    abs(int x);
long   labs(long x);
void   srand(unsigned int seed);
int    rand(void);
void   qsort(void *base, size_t nmemb, size_t size,
             int (*compar)(const void *, const void *));

int    vsscanf(const char *str, const char *fmt, va_list ap);
int    sscanf(const char *str, const char *fmt, ...);

uint32_t crc32_update(uint32_t crc, const void *data, size_t len);
uint32_t crc32(const void *data, size_t len);

#endif
