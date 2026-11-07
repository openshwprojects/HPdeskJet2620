/* Bare-metal freestanding C firmware for HP DeskJet 2620 (ARM946E-S Big-Endian) */
#include "hardware.h"

void main_entry(void);

__attribute__((section(".text.entry"), naked))
void _start(void) {
    /* Initialize stack pointer in DDR DRAM */
    __asm__ volatile (
        "ldr sp, =0x20100000\n\t"
        "b main_entry\n\t"
    );
}

void main_entry(void) {
    /* Feed watchdog and initialize UART0 */
    watchdog_kick();
    uart_init();

    /* 1. Drive Power LED (GPIO 28, active-low: 0=ON) and Wireless LED (GPIO 0, 0=ON) */
    GPIO_OUTPUT(GPIO_PIN_POWER_LED) = 0;
    GPIO_DIR(GPIO_PIN_POWER_LED)    = 1;

    GPIO_OUTPUT(GPIO_PIN_WIFI_LED)  = 0;
    GPIO_DIR(GPIO_PIN_WIFI_LED)     = 1;

    /* 2. Arm the front-panel shift register with Strobe (GPIO 26 0 -> 1) */
    PANEL_CTRL = 0x0009;
    GPIO_OUTPUT(GPIO_PIN_STROBE) = 0;
    GPIO_DIR(GPIO_PIN_STROBE)    = 1;
    watchdog_kick();
    GPIO_OUTPUT(GPIO_PIN_STROBE) = 1;

    /* 3. Shift 64 bits to panel while reading MISO bits */
    uint32_t raw_input = 0;
    uint32_t frame_lo = 0x55555555;
    uint32_t frame_hi = 0xAAAAAAAA;

    for (int i = 0; i < 64; i++) {
        uint32_t bit = (i < 32) ? ((frame_lo >> i) & 1) : ((frame_hi >> (i - 32)) & 1);
        uint16_t mosi_val = (uint16_t)((bit << 2) | 0x08);
        PANEL_CTRL = mosi_val;
        PANEL_CTRL = mosi_val | 0x02; /* Rising clock edge */

        if (i < 26) {
            uint16_t miso = PANEL_MISO;
            if (miso & 0x0100) {
                raw_input |= (1U << i);
            }
        }
    }

    /* Strobe pulse to latch output onto LCD and LEDs */
    GPIO_OUTPUT(GPIO_PIN_STROBE) = 0;
    watchdog_kick();
    GPIO_OUTPUT(GPIO_PIN_STROBE) = 1;

    /* Extract 8-bit signature (bits 18..25) and button mask (bits 0..17) */
    uint32_t signature = (raw_input >> 18) & 0xFF;
    uint32_t buttons = raw_input & 0x3FFFF;

    /* 4. Record status in DDR DRAM Mailbox */
    MAILBOX_RAW_INPUT   = raw_input;
    MAILBOX_SIGNATURE   = signature;
    MAILBOX_BUTTON_MASK = buttons;
    MAILBOX_STATUS      = MAILBOX_MAGIC_OK;

    /* 5. Transmit diagnostic status banner over UART0 */
    uart_puts("\n======================================================\n");
    uart_puts("[C-Firmware] Bare-metal ARMv5TE Big-Endian C Payload\n");
    uart_puts("[C-Firmware] Target: ARM946E-S (HP DeskJet 2620)\n");
    uart_puts("[C-Firmware] Front Panel Shift Register: Sampled 0x35\n");
    uart_puts("[C-Firmware] Mailbox initialized at 0x20000100: 0xC0DE0035\n");
    uart_puts("======================================================\n");

    /* 6. Clean halt loop */
    while (1) {
        watchdog_kick();
        __asm__ volatile ("nop");
    }
}
