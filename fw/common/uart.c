#include "hw.h"
#include "uart.h"
#include "timer.h"
#include "fmt.h"

void uart_init(void)
{
    /* Stage-1 (or the previous stage) may still be shifting out its last
       byte; let it finish before touching the configuration. Bounded poll. */
    uint32_t spins = 0;
    while (!(UART0_LSR & UART0_LSR_THRE) && ++spins < 2000000U) {
        watchdog_kick();
    }
    PADMUX_UART0_TX = 0x0001;
    PADMUX_UART0_RX = 0x0001;
    UART0_CTRL = 0x1000;           /* 8N1 */
    UART0_MASK = 0x03FF;           /* all UART interrupts masked: polled I/O */
    UART0_FIFO_BAUD0 = UART0_BAUD_115200;
    UART0_FIFO_BAUD1 = UART0_BAUD_115200;
}

static void (*mirror_fn)(char c);
static void (*tap_fn)(char c);
static int (*input_fn)(void), (*input_ready_fn)(void);
static int (*input2_fn)(void), (*input2_ready_fn)(void);
static int hw_output = 1;

void uart_set_mirror(void (*fn)(char c)) { mirror_fn = fn; }
void uart_set_tap(void (*fn)(char c)) { tap_fn = fn; }
void uart_set_input(int (*getc_fn)(void), int (*ready_fn)(void)) { input_fn = getc_fn; input_ready_fn = ready_fn; }
void uart_set_input2(int (*getc_fn)(void), int (*ready_fn)(void)) { input2_fn = getc_fn; input2_ready_fn = ready_fn; }
void uart_set_hw_output(int on) { hw_output = on; }
int uart_hw_output(void) { return hw_output; }

void uart_putc(char c)
{
    if (mirror_fn) mirror_fn(c);
    if (tap_fn) tap_fn(c);
    if (!hw_output) return;
    while (!(UART0_LSR & UART0_LSR_THRE)) {
        watchdog_kick();
    }
    UART0_THR = (uint16_t)(uint8_t)c;
}

void uart_puts(const char *s)
{
    while (*s) {
        if (*s == '\n') uart_putc('\r');
        uart_putc(*s++);
    }
}

void uart_write(const void *buf, uint32_t len)
{
    const uint8_t *p = buf;
    while (len--) uart_putc((char)*p++);
}

/* Bytes taken out of the receive FIFO by uart_rx_service() while the CPU is
   busy elsewhere (e.g. a 45 ms front-panel exchange); read before the FIFO. */
static uint8_t rx_ring[256];
static unsigned rx_head, rx_tail;

void uart_rx_service(void)
{
    while (UART0_LSR & UART0_LSR_DR) {
        unsigned next = (rx_head + 1U) % sizeof(rx_ring);
        uint8_t c = (uint8_t)(UART0_RX & 0xFF);
        if (next == rx_tail) break;                    /* ring full: the byte is lost */
        rx_ring[rx_head] = c;
        rx_head = next;
    }
}

int uart_rx_ready(void)
{
    return rx_head != rx_tail || (UART0_LSR & UART0_LSR_DR) != 0 || (input_ready_fn && input_ready_fn()) ||
           (input2_ready_fn && input2_ready_fn());
}

int uart_getc_nonblock(void)
{
    if (rx_head != rx_tail) {
        int c = rx_ring[rx_tail];
        rx_tail = (rx_tail + 1U) % sizeof(rx_ring);
        return c;
    }
    if (!(UART0_LSR & UART0_LSR_DR)) {
        int c = input_fn ? input_fn() : -1;
        if (c < 0 && input2_fn) c = input2_fn();
        return c;
    }
    return (int)(UART0_RX & 0xFF);
}

int uart_getc_timeout(uint32_t timeout_ms)
{
    uint32_t start = timer_ticks();
    for (;;) {
        int c = uart_getc_nonblock();
        if (c >= 0) return c;
        watchdog_kick();
        if (timer_elapsed_ms(start) >= timeout_ms) return -1;
    }
}

void uart_flush(void)
{
    /* THRE only promises room for another byte: with a transmit FIFO of
       unknown depth up to a few dozen bytes may still be queued. Wait for
       THRE, then long enough for ~64 frames at 115200 8N1 (6 ms). */
    while (!(UART0_LSR & UART0_LSR_THRE)) watchdog_kick();
    timer_delay_us(6000);
}

static void uart_fmt_out(char c, void *ctx)
{
    (void)ctx;
    if (c == '\n') uart_putc('\r');
    uart_putc(c);
}

void uart_vprintf(const char *fmt, va_list ap)
{
    fmt_vformat(uart_fmt_out, 0, fmt, ap);
}

void uart_printf(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    uart_vprintf(fmt, ap);
    va_end(ap);
}

void uart_hexdump(uint32_t addr, const void *data, uint32_t len)
{
    const uint8_t *p = data;
    uint32_t i, j;
    for (i = 0; i < len; i += 16) {
        uart_printf("%08x: ", addr + i);
        for (j = 0; j < 16; j++) {
            if (i + j < len) uart_printf("%02x ", p[i + j]);
            else uart_puts("   ");
            if (j == 7) uart_putc(' ');
        }
        uart_puts(" |");
        for (j = 0; j < 16 && i + j < len; j++) {
            uint8_t c = p[i + j];
            uart_putc((c >= 0x20 && c < 0x7F) ? (char)c : '.');
        }
        uart_puts("|\n");
    }
}
