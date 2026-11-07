/*
 * heap.c - 8-byte aligned coalescing free-list allocator for RTL8196C.
 *
 * Uses cached KSEG0 SDRAM region 0x80700000..0x80880000 (1.5 MB), above the
 * firmware image (0x80500000) and main stack (0x806F0000).
 */
#include "heap.h"
#include "libc.h"

#define BLOCK_MAGIC 0x48454150u /* "HEAP" */

struct heap_block {
    uint32_t           magic;
    uint32_t           size;      /* usable payload bytes (multiple of 8) */
    struct heap_block *next;
    uint32_t           used;      /* 1 = allocated, 0 = free */
}; /* 16 bytes (multiple of 8) */

static struct heap_block *heap_head = 0;
static size_t heap_free_bytes = 0;

static size_t align8(size_t n)
{
    return (n + 7u) & ~(size_t)7u;
}

void heap_init(void)
{
    heap_head = (struct heap_block *)(uintptr_t)HEAP_REGION_BASE;
    heap_head->magic = BLOCK_MAGIC;
    heap_head->size  = HEAP_REGION_SIZE - sizeof(struct heap_block);
    heap_head->next  = 0;
    heap_head->used  = 0;
    heap_free_bytes  = heap_head->size;
}

void *malloc(size_t size)
{
    if (!heap_head)
        heap_init();
    if (size == 0)
        size = 8;
    size = align8(size);

    for (struct heap_block *b = heap_head; b; b = b->next) {
        if (!b->used && b->size >= size) {
            if (b->size >= size + sizeof(struct heap_block) + 16u) {
                struct heap_block *split = (struct heap_block *)((uint8_t *)(b + 1) + size);
                split->magic = BLOCK_MAGIC;
                split->size  = b->size - size - sizeof(struct heap_block);
                split->next  = b->next;
                split->used  = 0;
                b->size = (uint32_t)size;
                b->next = split;
                heap_free_bytes -= sizeof(struct heap_block);
            }
            b->used = 1;
            heap_free_bytes -= b->size;
            return (void *)(b + 1);
        }
    }
    return 0;
}

void free(void *ptr)
{
    if (!ptr || !heap_head)
        return;
    uintptr_t addr = (uintptr_t)ptr;
    if (addr < HEAP_REGION_BASE + sizeof(struct heap_block) ||
        addr >= HEAP_REGION_BASE + HEAP_REGION_SIZE)
        return;

    struct heap_block *b = ((struct heap_block *)ptr) - 1;
    if (b->magic != BLOCK_MAGIC || !b->used)
        return;

    b->used = 0;
    heap_free_bytes += b->size;

    /* Coalesce adjacent free blocks across the list */
    for (struct heap_block *cur = heap_head; cur && cur->next; ) {
        if (!cur->used && !cur->next->used) {
            struct heap_block *nxt = cur->next;
            cur->size += (uint32_t)sizeof(struct heap_block) + nxt->size;
            cur->next  = nxt->next;
            heap_free_bytes += sizeof(struct heap_block);
        } else {
            cur = cur->next;
        }
    }
}

void *calloc(size_t nmemb, size_t size)
{
    size_t total = nmemb * size;
    void *p = malloc(total);
    if (p)
        memset(p, 0, total);
    return p;
}

void *realloc(void *ptr, size_t size)
{
    if (!ptr)
        return malloc(size);
    if (size == 0) {
        free(ptr);
        return 0;
    }
    struct heap_block *b = ((struct heap_block *)ptr) - 1;
    if (b->magic != BLOCK_MAGIC)
        return 0;
    size_t aligned = align8(size);
    if (b->size >= aligned)
        return ptr;

    /* Check if next block is free and large enough to expand in-place */
    if (b->next && !b->next->used &&
        b->size + sizeof(struct heap_block) + b->next->size >= aligned) {
        struct heap_block *nxt = b->next;
        heap_free_bytes -= nxt->size;
        b->size += (uint32_t)sizeof(struct heap_block) + nxt->size;
        b->next  = nxt->next;
        return ptr;
    }

    void *np = malloc(size);
    if (!np)
        return 0;
    memcpy(np, ptr, b->size < size ? b->size : size);
    free(ptr);
    return np;
}

__attribute__((weak)) char *strdup(const char *s)
{
    if (!s)
        return 0;
    size_t len = strlen(s) + 1;
    char *copy = (char *)malloc(len);
    if (copy)
        memcpy(copy, s, len);
    return copy;
}

__attribute__((weak)) char *strndup(const char *s, size_t n)
{
    if (!s)
        return 0;
    size_t len = strnlen(s, n);
    char *copy = (char *)malloc(len + 1);
    if (copy) {
        memcpy(copy, s, len);
        copy[len] = 0;
    }
    return copy;
}

void *os_malloc(size_t size)
{
    return malloc(size);
}

void os_free(void *ptr)
{
    free(ptr);
}

void *pvPortMalloc(size_t size)
{
    return malloc(size);
}

void vPortFree(void *ptr)
{
    free(ptr);
}

void *pvPortRealloc(void *ptr, size_t size)
{
    return realloc(ptr, size);
}

size_t xPortGetFreeHeapSize(void)
{
    if (!heap_head)
        heap_init();
    return heap_free_bytes;
}
