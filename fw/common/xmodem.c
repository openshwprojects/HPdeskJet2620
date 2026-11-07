#include "xmodem.h"
#include "uart.h"
#include "timer.h"
#include "crc.h"
#include "config.h"
#include "hw.h"

#define SOH 0x01
#define STX 0x02
#define EOT 0x04
#define ACK 0x06
#define NAK 0x15
#define CAN 0x18
#define CRC_REQ 'C'

/* Discard whatever the sender is still streaming: wait for 20 ms of silence,
   but never longer than half a second in total (continuous garbage must not
   keep the shell hostage). */
static void drain_rx(void)
{
    uint32_t start = timer_ticks(), quiet = start;
    while (timer_elapsed_ms(quiet) < 20U && timer_elapsed_ms(start) < 500U) {
        if (uart_getc_nonblock() >= 0) quiet = timer_ticks();
        watchdog_kick();
    }
}

int32_t xmodem_receive(uint8_t *buf, uint32_t max_len)
{
    uint32_t received = 0;
    uint8_t expected = 1;
    int retries = 0;
    int started = 0;
    uint32_t idle_start = timer_ticks();

    int pending = -1;       /* a byte already read that must be dispatched */
    uint32_t noise = 0;

    uart_putc(CRC_REQ);
    for (;;) {
        int c;
        if (pending >= 0) {
            c = pending;
            pending = -1;
        } else {
            c = uart_getc_timeout(started ? XMODEM_BYTE_TIMEOUT_MS : XMODEM_POLL_INTERVAL_MS);
        }
        if (c < 0) {
            if (!started) {
                if (timer_elapsed_ms(idle_start) >= XMODEM_START_TIMEOUT_MS) return XMODEM_ERR_TIMEOUT;
                uart_putc(CRC_REQ);
                continue;
            }
            if (++retries > XMODEM_MAX_RETRIES) { drain_rx(); return XMODEM_ERR_RETRIES; }
            uart_putc(NAK);
            continue;
        }
        if (c == EOT) {
            uart_putc(ACK);
            return (int32_t)received;
        }
        if (c == CAN) {
            int c2 = uart_getc_timeout(XMODEM_BYTE_TIMEOUT_MS);
            if (c2 == CAN || c2 < 0) { drain_rx(); return XMODEM_ERR_CANCELLED; }
            pending = c2;       /* a lone CAN is noise: the next byte still counts */
            continue;
        }
        if (c == 0x03) {        /* Ctrl-C from the operator's terminal */
            drain_rx();
            return XMODEM_ERR_CANCELLED;
        }
        if (c != SOH && c != STX) {
            /* Noise where a header byte is expected. Keep honouring the start
               deadline, and give up on a sender that only produces garbage. */
            if (!started && timer_elapsed_ms(idle_start) >= XMODEM_START_TIMEOUT_MS) return XMODEM_ERR_TIMEOUT;
            if (++noise > XMODEM_MAX_NOISE_BYTES) { drain_rx(); return XMODEM_ERR_NOISE; }
            continue;
        }
        uint32_t blen = (c == STX) ? 1024U : 128U;
        int blk = uart_getc_timeout(XMODEM_BYTE_TIMEOUT_MS);
        int nblk = uart_getc_timeout(XMODEM_BYTE_TIMEOUT_MS);
        if (blk < 0 || nblk < 0) {
            drain_rx();
            if (++retries > XMODEM_MAX_RETRIES) return XMODEM_ERR_RETRIES;
            uart_putc(NAK);
            continue;
        }

        uint8_t *dst = buf + received;
        int fits = received + blen <= max_len;
        uint8_t scratch[1024];
        uint8_t *target = fits ? dst : scratch;
        uint32_t i;
        int bad = 0;
        for (i = 0; i < blen; i++) {
            int d = uart_getc_timeout(XMODEM_BYTE_TIMEOUT_MS);
            if (d < 0) { bad = 1; break; }
            target[i] = (uint8_t)d;
        }
        int crc_hi = bad ? -1 : uart_getc_timeout(XMODEM_BYTE_TIMEOUT_MS);
        int crc_lo = bad ? -1 : uart_getc_timeout(XMODEM_BYTE_TIMEOUT_MS);
        if (bad || crc_hi < 0 || crc_lo < 0) {
            drain_rx();
            if (++retries > XMODEM_MAX_RETRIES) return XMODEM_ERR_RETRIES;
            uart_putc(NAK);
            continue;
        }
        if (((blk + nblk) & 0xFF) != 0xFF) {
            drain_rx();
            if (++retries > XMODEM_MAX_RETRIES) return XMODEM_ERR_RETRIES;
            uart_putc(NAK);
            continue;
        }
        uint16_t crc = crc16_xmodem(target, blen);
        if (crc != (uint16_t)((crc_hi << 8) | crc_lo)) {
            drain_rx();
            if (++retries > XMODEM_MAX_RETRIES) return XMODEM_ERR_RETRIES;
            uart_putc(NAK);
            continue;
        }
        started = 1;
        if ((uint8_t)blk == (uint8_t)(expected - 1)) {
            uart_putc(ACK);     /* duplicate of the previous block: already stored */
            continue;
        }
        if ((uint8_t)blk != expected) {
            uart_putc(CAN); uart_putc(CAN);
            drain_rx();         /* the sender may still be streaming a frame */
            return XMODEM_ERR_SYNC;
        }
        if (!fits) {
            uart_putc(CAN); uart_putc(CAN);
            drain_rx();
            return XMODEM_ERR_TOO_LARGE;
        }
        received += blen;
        expected++;
        retries = 0;
        uart_putc(ACK);
    }
}

const char *xmodem_err_str(int32_t err)
{
    switch (err) {
    case XMODEM_ERR_TIMEOUT: return "timeout waiting for sender";
    case XMODEM_ERR_CANCELLED: return "cancelled (CAN CAN or Ctrl-C)";
    case XMODEM_ERR_TOO_LARGE: return "image larger than the buffer";
    case XMODEM_ERR_RETRIES: return "too many retries";
    case XMODEM_ERR_SYNC: return "block sequence error";
    case XMODEM_ERR_NOISE: return "too much non-XMODEM data on the line";
    default: return "unknown error";
    }
}
