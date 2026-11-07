#ifndef FW_CRC_H
#define FW_CRC_H
#include <stdint.h>
#include <stddef.h>

/* CRC-32/IEEE (reflected, poly 0xEDB88320), same as zlib.crc32. */
uint32_t crc32_update(uint32_t crc, const void *data, size_t len);
static inline uint32_t crc32_compute(const void *data, size_t len) { return crc32_update(0, data, len); }

/* CRC-16/XMODEM (poly 0x1021, init 0, not reflected). */
uint16_t crc16_xmodem(const void *data, size_t len);

#endif
