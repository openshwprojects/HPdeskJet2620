/* Bare-metal hardware register definitions for HP DeskJet 2620 (ARM946E-S Big-Endian) */
#ifndef HARDWARE_H
#define HARDWARE_H

#include <stdint.h>
#include <stdarg.h>

#define REG32(addr) (*(volatile uint32_t *)(addr))
#define REG16(addr) (*(volatile uint16_t *)(addr))
#define REG8(addr)  (*(volatile uint8_t *)(addr))

/* --- Pad Multiplexing Controller (0xBD060800) --- */
#define PADMUX_BASE         0xBD060800
#define PADMUX_UART0_TX     REG16(PADMUX_BASE + 0x0E)
#define PADMUX_UART0_RX     REG16(PADMUX_BASE + 0x10)

/* --- UART0 Serial Interface (0xBD0B3C00) --- */
#define UART0_BASE          0xBD0B3C00
#define UART0_CTRL          REG16(UART0_BASE + 0x00)
#define UART0_MASK          REG16(UART0_BASE + 0x04)
#define UART0_THR           REG16(UART0_BASE + 0x06)
#define UART0_RBR           REG16(UART0_BASE + 0x08)
#define UART0_RX            REG16(UART0_BASE + 0x0C)
#define UART0_LSR           REG16(UART0_BASE + 0x0E)
#define UART0_THRE          0x0100
#define UART0_DR            0x0004

/* --- UART0 Baud / FIFO Controller (0xBD0B4C00) --- */
#define UART0_FIFO_BASE     0xBD0B4C00
#define UART0_FIFO_BAUD0    REG16(UART0_FIFO_BASE + 0x00)
#define UART0_FIFO_BAUD1    REG16(UART0_FIFO_BASE + 0x02)

/* --- GPIO Controller (0xBD0B2000) --- */
#define GPIO_BASE           0xBD0B2000
#define GPIO_INPUT(pin)     REG16(GPIO_BASE + (pin) * 8 + 0)
#define GPIO_OUTPUT(pin)    REG16(GPIO_BASE + (pin) * 8 + 2)
#define GPIO_DIR(pin)       REG16(GPIO_BASE + (pin) * 8 + 4)

#define GPIO_PIN_POWER_LED  28
#define GPIO_PIN_WIFI_LED   0
#define GPIO_PIN_STROBE     26

/* --- 64-bit Shift-Register Front Panel (0xBD0B1400) --- */
#define PANEL_MISO          REG16(0xBD0B1410)
#define PANEL_CTRL          REG16(0xBD0B1418)

/* --- Hardware Timer & Watchdog (0xBD040000) --- */
#define TIMER_TICKS         REG32(0xBD040814)
#define WATCHDOG_KICK       REG16(0xBD040400)
#define WATCHDOG_MAGIC      0x000C

/* --- SPI Flash Controller (0xBD063800) --- */
#define SPI_BASE            0xBD063800
#define SPI_STATUS          REG32(SPI_BASE + 0x00)
#define SPI_RX_SHIFT        REG32(SPI_BASE + 0x04)
#define SPI_CTRL            REG32(SPI_BASE + 0x08)
#define SPI_TX_SHIFT        REG32(SPI_BASE + 0x14)
#define SPI_CTRL_CS_HOLD    0x00000023U
#define SPI_CTRL_CS_REL     0x00000022U

/* --- I2C Master Controller (0xBD065800) --- */
#define I2C_BASE            0xBD065800
#define I2C_CTRL            REG16(I2C_BASE + 0x00)
#define I2C_ADDR            REG16(I2C_BASE + 0x02)
#define I2C_SUBADDR_H       REG16(I2C_BASE + 0x04)
#define I2C_SUBADDR_L       REG16(I2C_BASE + 0x06)
#define I2C_DATA            REG16(I2C_BASE + 0x08)
#define I2C_CTRL_EN_8BIT    0x0001
#define I2C_CTRL_EN_16BIT   0x0201
#define I2C_CTRL_BUSY       0x0080

/* --- MICCI Serial Bus Controllers (0xBD300800 / 0xBD300C00) --- */
#define MICCI0_BASE         0xBD300800U
#define MICCI1_BASE         0xBD300C00U

/* --- DRAM Mailbox & Communication Buffers (0x20000100) --- */
#define MAILBOX_RAW_INPUT   REG32(0x20000100)
#define MAILBOX_SIGNATURE   REG32(0x20000104)
#define MAILBOX_BUTTON_MASK REG32(0x20000108)
#define MAILBOX_STATUS      REG32(0x2000010C)
#define MAILBOX_MAGIC_OK    0xC0DE0035

static inline void watchdog_kick(void) {
    WATCHDOG_KICK = WATCHDOG_MAGIC;
}

static inline uint32_t timer_ticks(void) {
    return TIMER_TICKS;
}

static inline void spi_wait_ready(void) {
    while (((SPI_STATUS >> 11) & 0x1FU) == 1U) {
        watchdog_kick();
    }
}

static inline uint32_t spi_read_jedec_id(void) {
    SPI_CTRL = SPI_CTRL_CS_HOLD;
    SPI_TX_SHIFT = 0x9F000000U;
    spi_wait_ready();
    SPI_TX_SHIFT = 0x00000000U;
    spi_wait_ready();
    SPI_TX_SHIFT = 0x00000000U;
    spi_wait_ready();
    SPI_CTRL = SPI_CTRL_CS_REL;
    SPI_TX_SHIFT = 0x00000000U;
    spi_wait_ready();
    return SPI_RX_SHIFT & 0x00FFFFFFU;
}

static inline uint32_t spi_read_u32(uint32_t flash_addr) {
    SPI_CTRL = SPI_CTRL_CS_HOLD;
    SPI_TX_SHIFT = 0x03000000U;
    spi_wait_ready();
    SPI_TX_SHIFT = (flash_addr & 0x00FF0000U) << 8;
    spi_wait_ready();
    SPI_TX_SHIFT = (flash_addr & 0x0000FF00U) << 16;
    spi_wait_ready();
    SPI_TX_SHIFT = (flash_addr & 0x000000FFU) << 24;
    spi_wait_ready();
    for (int i = 0; i < 3; i++) {
        SPI_TX_SHIFT = 0x00000000U;
        spi_wait_ready();
    }
    SPI_CTRL = SPI_CTRL_CS_REL;
    SPI_TX_SHIFT = 0x00000000U;
    spi_wait_ready();
    return SPI_RX_SHIFT;
}

static inline uint8_t i2c_read_byte(uint8_t slave7, uint16_t subaddr, int word_subaddr) {
    if (word_subaddr) {
        I2C_CTRL = I2C_CTRL_EN_16BIT;
        I2C_SUBADDR_H = (uint16_t)((subaddr >> 8) & 0xFFU);
        I2C_SUBADDR_L = (uint16_t)(subaddr & 0xFFU);
    } else {
        I2C_CTRL = I2C_CTRL_EN_8BIT;
        I2C_SUBADDR_L = (uint16_t)(subaddr & 0xFFU);
    }
    I2C_ADDR = (uint16_t)(((slave7 & 0x7FU) << 1) | 1U);
    while (I2C_CTRL & I2C_CTRL_BUSY) {
        watchdog_kick();
    }
    return (uint8_t)(I2C_DATA & 0xFFU);
}

static inline uint16_t micci_read_reg(uint32_t base, uint8_t bus, uint8_t dev, uint8_t reg) {
    if (REG16(base + 0x00) == 0) {
        REG16(base + 0x00) = 0x4001;
    }
    REG16(base + 0xAC) = (uint16_t)(((dev & 7U) << 13) | ((reg & 0x7FU) << 6) | 0x20U | ((bus & 1U) << 4) | 0x0FU);
    while (REG32(base + 0xB0) & 0x80000000U) {
        watchdog_kick();
    }
    return (uint16_t)(REG32(base + 0xB0) & 0xFFFFU);
}

static inline void uart_init(void) {
    /* Set UART0 pad multiplexing */
    PADMUX_UART0_TX = 0x0001;
    PADMUX_UART0_RX = 0x0001;

    /* 115200 baud, 8N1 framing, mask interrupts */
    UART0_CTRL = 0x1000;
    UART0_MASK = 0x03FF;
    UART0_FIFO_BAUD0 = 0x2111;
    UART0_FIFO_BAUD1 = 0x2111;
}

static inline void uart_putc(char c) {
    while (!(UART0_LSR & UART0_THRE)) {
        watchdog_kick();
    }
    UART0_THR = (uint16_t)(uint8_t)c;
}

static inline void uart_puts(const char *s) {
    while (*s) {
        if (*s == '\n') {
            uart_putc('\r');
        }
        uart_putc(*s++);
    }
}

static inline void uart_puthex(uint32_t val) {
    const char hex_chars[] = "0123456789ABCDEF";
    uart_puts("0x");
    for (int i = 28; i >= 0; i -= 4) {
        uart_putc(hex_chars[(val >> i) & 0xF]);
    }
}

static inline void uart_putdec(uint32_t val) {
    char buf[12];
    int i = 0;
    if (val == 0) {
        uart_putc('0');
        return;
    }
    while (val > 0) {
        buf[i++] = (char)('0' + (val % 10));
        val /= 10;
    }
    while (i > 0) {
        uart_putc(buf[--i]);
    }
}

static inline int uart_has_char(void) {
    return (UART0_LSR & UART0_DR) != 0;
}

static inline char uart_getc(void) {
    while (!uart_has_char()) {
        watchdog_kick();
    }
    return (char)(uint8_t)(UART0_RX & 0xFF);
}

static inline void uart_printf(const char *fmt, ...) {
    va_list args;
    va_start(args, fmt);
    while (*fmt) {
        if (*fmt == '%') {
            fmt++;
            if (*fmt == 's') {
                const char *s = va_arg(args, const char *);
                uart_puts(s ? s : "(null)");
            } else if (*fmt == 'd' || *fmt == 'i') {
                int32_t v = va_arg(args, int32_t);
                if (v < 0) {
                    uart_putc('-');
                    uart_putdec((uint32_t)(-v));
                } else {
                    uart_putdec((uint32_t)v);
                }
            } else if (*fmt == 'u') {
                uint32_t v = va_arg(args, uint32_t);
                uart_putdec(v);
            } else if (*fmt == 'x' || *fmt == 'X') {
                uint32_t v = va_arg(args, uint32_t);
                uart_puthex(v);
            } else if (*fmt == 'c') {
                char c = (char)va_arg(args, int);
                uart_putc(c);
            } else if (*fmt == '%') {
                uart_putc('%');
            } else {
                uart_putc('%');
                uart_putc(*fmt);
            }
        } else {
            if (*fmt == '\n') {
                uart_putc('\r');
            }
            uart_putc(*fmt);
        }
        fmt++;
    }
    va_end(args);
}

#endif /* HARDWARE_H */
