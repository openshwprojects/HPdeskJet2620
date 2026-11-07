/*
 * Memory-mapped peripheral registers of the AXON ASIC (HP DeskJet 2620,
 * ARM946E-S, big-endian). Addresses follow analysis/boot_hardware.md and
 * hardware.py; only registers this firmware uses are listed.
 */
#ifndef FW_HW_H
#define FW_HW_H

#include <stdint.h>

#define REG32(a) (*(volatile uint32_t *)(a))
#define REG16(a) (*(volatile uint16_t *)(a))
#define REG8(a)  (*(volatile uint8_t *)(a))

/* Pad multiplexing */
#define PADMUX_BASE          0xBD060800U
#define PADMUX_UART0_TX      REG16(PADMUX_BASE + 0x0E)
#define PADMUX_UART0_RX      REG16(PADMUX_BASE + 0x10)

/* UART0 */
#define UART0_BASE           0xBD0B3C00U
#define UART0_CTRL           REG16(UART0_BASE + 0x00)
#define UART0_MASK           REG16(UART0_BASE + 0x04)
#define UART0_THR            REG16(UART0_BASE + 0x06)
#define UART0_RX             REG16(UART0_BASE + 0x0C)
#define UART0_LSR            REG16(UART0_BASE + 0x0E)
#define UART0_LSR_THRE       0x0100U
#define UART0_LSR_DR         0x0004U
#define UART0_FIFO_BASE      0xBD0B4C00U
#define UART0_FIFO_BAUD0     REG16(UART0_FIFO_BASE + 0x00)
#define UART0_FIFO_BAUD1     REG16(UART0_FIFO_BASE + 0x02)
#define UART0_BAUD_115200    0x2111U

/* GPIO: base + pin*8 (+0 input, +2 output latch, +4 direction) */
#define GPIO_BASE            0xBD0B2000U
#define GPIO_INPUT(pin)      REG16(GPIO_BASE + (pin) * 8 + 0)
#define GPIO_OUTPUT(pin)     REG16(GPIO_BASE + (pin) * 8 + 2)
#define GPIO_DIR(pin)        REG16(GPIO_BASE + (pin) * 8 + 4)
#define GPIO_PIN_POWER_LED   28
#define GPIO_PIN_WIFI_LED    0
#define GPIO_PIN_PANEL_STROBE 26
#define GPIO_PIN_COUNT       32

/* Front panel 64-bit shift register */
#define PANEL_MISO           REG16(0xBD0B1410U)
#define PANEL_CTRL           REG16(0xBD0B1418U)

/* System counter and watchdog */
#define TIMER_TICKS          REG32(0xBD040814U)
#define WATCHDOG_CONTROL     REG16(0xBD040400U)
#define WATCHDOG_COUNT       REG32(0xBD040404U)
#define WATCHDOG_RELOAD      REG32(0xBD040408U)
#define WATCHDOG_ROUTING     REG16(0xBD060002U)
#define WATCHDOG_KICK_VALUE  0x000CU
#define RESET_RETENTION      REG16(0xBD04083CU)
#define ASIC_ID              REG32(0xBD040000U)

/* AXON SPI flash controller */
#define SPI_BASE             0xBD063800U
#define SPI_STATUS           REG32(SPI_BASE + 0x00)
#define SPI_RX               REG32(SPI_BASE + 0x04)
#define SPI_CTRL             REG32(SPI_BASE + 0x08)
#define SPI_TX               REG32(SPI_BASE + 0x14)
#define SPI_WINDOW(dev)      REG16(SPI_BASE + 0x18 + (dev) * 2)
#define SPI_STATUS_STATE(s)  (((s) >> 11) & 0x1FU)
#define SPI_STATE_BUSY       1U
#define SPI_STATE_HELD       13U
#define SPI_CTRL_HOLD_CS     0x01U   /* keep CS asserted after this transfer */
#define SPI_CTRL_CFG         0x02U   /* observed configuration bit, semantics unknown */
#define SPI_CTRL_BYTE        0x20U   /* 8-bit transfer in the wide-final hypothesis */
#define SPI_CTRL_DEVICE(d)   (((d) & 3U) << 10)

/* Feed the watchdog the way HP's feeding routine does: remember the enable
   bits, write 0xC (stop/clear, reloads the count), then enable again. While the
   watchdog is off (the normal state since boot) this is just the 0xC write. */
static inline void watchdog_kick(void)
{
    uint16_t on = (uint16_t)(WATCHDOG_CONTROL & 0x3U);
    WATCHDOG_CONTROL = WATCHDOG_KICK_VALUE;
    if (on) WATCHDOG_CONTROL = on;
}

/* Arm the watchdog: the SoC resets about `seconds` + 67 ms after the last feed
   (counts at the 180 MHz reference; the second stage is HP's 12,000,000). */
static inline void watchdog_enable(uint32_t seconds)
{
    WATCHDOG_CONTROL = 0x000CU;
    WATCHDOG_COUNT = 12000000U + 180000000U * seconds;
    WATCHDOG_RELOAD = 12000000U;
    WATCHDOG_CONTROL = 0x0003U;
}

static inline void watchdog_disable(void) { WATCHDOG_CONTROL = 0x000CU; }
static inline int watchdog_enabled(void) { return (WATCHDOG_CONTROL & 0x3U) != 0; }

#endif /* FW_HW_H */
