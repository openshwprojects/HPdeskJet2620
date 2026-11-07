#ifndef FW_LIBMINI_H
#define FW_LIBMINI_H
#include <stddef.h>
#include <stdint.h>

void *memcpy(void *dst, const void *src, size_t n);
void *memmove(void *dst, const void *src, size_t n);
void *memset(void *dst, int c, size_t n);
int memcmp(const void *a, const void *b, size_t n);
size_t strlen(const char *s);
int strcmp(const char *a, const char *b);
int strncmp(const char *a, const char *b, size_t n);
char *strchr(const char *s, int c);
size_t strlcpy(char *dst, const char *src, size_t size);
uint32_t __aeabi_uidiv(uint32_t n, uint32_t d);
int32_t __aeabi_idiv(int32_t n, int32_t d);

#endif
