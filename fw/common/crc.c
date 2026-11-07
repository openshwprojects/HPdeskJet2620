#include "crc.h"

static uint32_t crc32_table[256];
static int crc32_ready;

static void crc32_init(void)
{
    uint32_t i, j;
    for (i = 0; i < 256; i++) {
        uint32_t c = i;
        for (j = 0; j < 8; j++) c = (c & 1) ? (0xEDB88320U ^ (c >> 1)) : (c >> 1);
        crc32_table[i] = c;
    }
    crc32_ready = 1;
}

uint32_t crc32_update(uint32_t crc, const void *data, size_t len)
{
    const uint8_t *p = data;
    if (!crc32_ready) crc32_init();
    crc = ~crc;
    while (len--) crc = crc32_table[(crc ^ *p++) & 0xFF] ^ (crc >> 8);
    return ~crc;
}

uint16_t crc16_xmodem(const void *data, size_t len)
{
    const uint8_t *p = data;
    uint16_t crc = 0;
    while (len--) {
        int i;
        crc ^= (uint16_t)(*p++) << 8;
        for (i = 0; i < 8; i++) crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
    }
    return crc;
}
