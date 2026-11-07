/* XMODEM-CRC / XMODEM-1K receiver over UART0. */
#ifndef FW_XMODEM_H
#define FW_XMODEM_H
#include <stdint.h>

enum xmodem_err {
    XMODEM_ERR_TIMEOUT = -1,
    XMODEM_ERR_CANCELLED = -2,
    XMODEM_ERR_TOO_LARGE = -3,
    XMODEM_ERR_RETRIES = -4,
    XMODEM_ERR_SYNC = -5,
    XMODEM_ERR_NOISE = -6,
};

/* Receive into `buf` (capacity `max_len`). Returns the number of bytes stored
   (including the sender's padding of the final block) or a negative error. */
int32_t xmodem_receive(uint8_t *buf, uint32_t max_len);
const char *xmodem_err_str(int32_t err);

#endif
