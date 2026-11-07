#ifndef HEAP_H
#define HEAP_H

#include <stddef.h>
#include <stdint.h>

#define HEAP_REGION_BASE 0x21000000u
#define HEAP_REGION_SIZE (4096u * 1024u) /* 4 MB: 0x21000000..0x21400000 (cached RW DRAM) */

void  heap_init(void);
void *malloc(size_t size);
void  free(void *ptr);
void *calloc(size_t nmemb, size_t size);
void *realloc(void *ptr, size_t size);
char *strdup(const char *s);
char *strndup(const char *s, size_t n);

void  *pvPortMalloc(size_t size);
void   vPortFree(void *ptr);
void  *pvPortRealloc(void *ptr, size_t size);
size_t xPortGetFreeHeapSize(void);

#endif
