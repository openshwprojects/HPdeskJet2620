#ifndef FW_UART_H
#define FW_UART_H
#include <stdint.h>
#include <stdarg.h>

void uart_init(void);
void uart_putc(char c);           /* raw byte, no CR insertion */
void uart_puts(const char *s);    /* "\n" is sent as "\r\n" */
void uart_write(const void *buf, uint32_t len);
int  uart_rx_ready(void);
int  uart_getc_nonblock(void);    /* -1 when nothing is pending */
void uart_rx_service(void);       /* move waiting bytes from the FIFO into a RAM ring (call during long waits) */
int  uart_getc_timeout(uint32_t timeout_ms);  /* -1 on timeout */
void uart_flush(void);            /* wait until the transmitter is idle */
void uart_printf(const char *fmt, ...);
void uart_vprintf(const char *fmt, va_list ap);
void uart_hexdump(uint32_t addr, const void *data, uint32_t len);
/* Every byte sent is also handed to `fn` (NULL = none): the application's
   USB serial console shows the same output as the UART. */
void uart_set_mirror(void (*fn)(char c));
/* A second copy of every output byte (the application's web log). */
void uart_set_tap(void (*fn)(char c));
/* A second input (the application's USB console): uart_getc_nonblock() and
   everything built on it (shell, XMODEM, interactive commands) also returns
   its characters; ready_fn tells uart_rx_ready() that some are waiting. */
void uart_set_input(int (*getc_fn)(void), int (*ready_fn)(void));
/* The same for a second source (the application's telnet shell). */
void uart_set_input2(int (*getc_fn)(void), int (*ready_fn)(void));
/* 0: output goes only to the mirror/tap (USB, web log), not the UART pins. */
void uart_set_hw_output(int on);
int  uart_hw_output(void);

#endif
