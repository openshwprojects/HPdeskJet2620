/* Bare-metal freestanding sample C firmware for HP DeskJet 2620 (ARM946E-S Big-Endian) */
#include "hardware.h"

void sample_main(void);

__attribute__((section(".text.entry"), naked))
void _start(void) {
    /* Initialize stack pointer in DDR DRAM */
    __asm__ volatile (
        "ldr sp, =0x20100000\n\t"
        "b sample_main\n\t"
    );
}

static inline int str_eq(const char *s1, const char *s2) {
    while (*s1 && (*s1 == *s2)) {
        s1++;
        s2++;
    }
    return *(const unsigned char *)s1 - *(const unsigned char *)s2 == 0;
}

static inline int str_starts_with(const char *s, const char *prefix) {
    while (*prefix) {
        if (*prefix++ != *s++) return 0;
    }
    return 1;
}

static void handle_command(const char *cmd) {
    if (cmd[0] == '\0') return;

    if (str_eq(cmd, "help") || str_eq(cmd, "h") || str_eq(cmd, "?")) {
        uart_puts("\n[Sample-FW] Available Commands:\n");
        uart_puts("  help      - Show this command reference\n");
        uart_puts("  ping      - Check firmware responsiveness (returns pong)\n");
        uart_puts("  buttons   - Sample and display front panel button mask\n");
        uart_puts("  leds      - Toggle Power & WiFi LED states\n");
        uart_puts("  mailbox   - Inspect DRAM mailbox at 0x20000100\n");
        uart_puts("  timer     - Read hardware free-running timer (0xBD040814)\n");
        uart_puts("  spi       - Query W25Q128BV SPI flash JEDEC ID & reset word\n");
        uart_puts("  i2c       - Read factory EEPROM (0x50) Board ID & Undercroft (0x1F)\n");
        uart_puts("  ink       - Query MCASIC & CarriageAsic pen presence via MICCI\n");
        uart_puts("  echo <msg>- Echo string back over UART0\n");
        uart_puts("  reset     - Trigger hardware watchdog reset\n");
    } else if (str_eq(cmd, "ping") || str_eq(cmd, "p")) {
        uart_puts("\n[Sample-FW] pong\n");
    } else if (str_eq(cmd, "buttons") || str_eq(cmd, "b")) {
        uart_printf("\n[Sample-FW] Buttons: %X (signature=%X, raw=%X)\n",
                    MAILBOX_BUTTON_MASK, MAILBOX_SIGNATURE, MAILBOX_RAW_INPUT);
    } else if (str_eq(cmd, "mailbox") || str_eq(cmd, "m")) {
        uart_printf("\n[Sample-FW] DRAM Mailbox (0x20000100):\n");
        uart_printf("  Signature: %X\n", MAILBOX_SIGNATURE);
        uart_printf("  Buttons:   %X\n", MAILBOX_BUTTON_MASK);
        uart_printf("  Status:    %X\n", MAILBOX_STATUS);
    } else if (str_eq(cmd, "leds") || str_eq(cmd, "l")) {
        uint16_t cur_pwr = GPIO_OUTPUT(GPIO_PIN_POWER_LED);
        uint16_t cur_wifi = GPIO_OUTPUT(GPIO_PIN_WIFI_LED);
        GPIO_OUTPUT(GPIO_PIN_POWER_LED) = (cur_pwr == 0) ? 1 : 0;
        GPIO_OUTPUT(GPIO_PIN_WIFI_LED)  = (cur_wifi == 0) ? 1 : 0;
        uart_printf("\n[Sample-FW] Toggled LEDs: Power=%s, WiFi=%s\n",
                    (cur_pwr == 0) ? "OFF" : "ON", (cur_wifi == 0) ? "OFF" : "ON");
    } else if (str_eq(cmd, "timer") || str_eq(cmd, "t")) {
        uint32_t ticks = timer_ticks();
        uart_printf("\n[Sample-FW] Timer Ticks: %u (%X)\n", ticks, ticks);
    } else if (str_eq(cmd, "spi") || str_eq(cmd, "s")) {
        uint32_t jedec = spi_read_jedec_id();
        uint32_t word0 = spi_read_u32(0x000000U);
        uart_printf("\n[Sample-FW] SPI Flash JEDEC ID: %X, Word0: %X\n", jedec, word0);
    } else if (str_eq(cmd, "i2c")) {
        uint8_t nvm_ver = i2c_read_byte(0x50, 0x0000, 1);
        uint8_t bid_hi  = i2c_read_byte(0x50, 0x000C, 1);
        uint8_t bid_lo  = i2c_read_byte(0x50, 0x000D, 1);
        uint16_t board_id = (uint16_t)((bid_hi << 8) | bid_lo);
        uint8_t uc_stat = i2c_read_byte(0x1F, 0x0080, 0);
        uart_printf("\n[Sample-FW] I2C EEPROM(0x50): ver=%X board=%X, Undercroft(0x1F): status=%X\n",
                    (uint32_t)nvm_ver, (uint32_t)board_id, (uint32_t)uc_stat);
    } else if (str_eq(cmd, "ink") || str_eq(cmd, "micci")) {
        uint16_t mcasic_id   = micci_read_reg(MICCI0_BASE, 0, 6, 0x00);
        uint16_t carriage_id = micci_read_reg(MICCI1_BASE, 1, 4, 0x7D);
        uint16_t pen_status  = micci_read_reg(MICCI1_BASE, 1, 4, 0x09);
        uart_printf("\n[Sample-FW] MICCI MCASIC=%X, Carriage=%X, PenStatus=%X\n",
                    (uint32_t)mcasic_id, (uint32_t)carriage_id, (uint32_t)pen_status);
    } else if (str_starts_with(cmd, "echo ")) {
        uart_printf("\n[Sample-FW] Echo: %s\n", cmd + 5);
    } else if (str_eq(cmd, "reset")) {
        uart_puts("\n[Sample-FW] Forcing watchdog reset...\n");
        while (1) {
            __asm__ volatile ("nop");
        }
    } else {
        uart_printf("\n[Sample-FW] Unknown command: '%s'. Type 'help' for command list.\n", cmd);
    }
}

void sample_main(void) {
    /* Feed watchdog and initialize serial port */
    watchdog_kick();
    uart_init();

    /* Hello World announcement */
    uart_puts("\n======================================================\n");
    uart_puts("Hello World from Freestanding C Sample Firmware!\n");
    uart_puts("Target: ARMv5TE Big-Endian (ARM946E-S / HP DeskJet 2620)\n");
    uart_puts("======================================================\n");

    /* 1. Drive Power LED (GPIO 28, active-low: 0=ON) and Wireless LED (GPIO 0, 0=ON) */
    GPIO_OUTPUT(GPIO_PIN_POWER_LED) = 0;
    GPIO_DIR(GPIO_PIN_POWER_LED)    = 1;

    GPIO_OUTPUT(GPIO_PIN_WIFI_LED)  = 0;
    GPIO_DIR(GPIO_PIN_WIFI_LED)     = 1;

    /* 2. Arm front-panel shift register with Strobe pulse (GPIO 26: 0 -> 1) */
    PANEL_CTRL = 0x0009;
    GPIO_OUTPUT(GPIO_PIN_STROBE) = 0;
    GPIO_DIR(GPIO_PIN_STROBE)    = 1;
    watchdog_kick();
    GPIO_OUTPUT(GPIO_PIN_STROBE) = 1;

    /* 3. Shift 64 bits to panel while reading MISO */
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

    /* Extract signature (bits 18..25) and button mask (bits 0..17) */
    uint32_t signature = (raw_input >> 18) & 0xFF;
    uint32_t buttons = raw_input & 0x3FFFF;

    /* 4. Record status in DDR DRAM Mailbox */
    MAILBOX_RAW_INPUT   = raw_input;
    MAILBOX_SIGNATURE   = signature;
    MAILBOX_BUTTON_MASK = buttons;
    MAILBOX_STATUS      = MAILBOX_MAGIC_OK;

    /* 5. Transmit hardware diagnostic confirmation */
    uart_printf("[Sample-FW] Front Panel Shift Register: Sampled Signature %X\n", signature);
    uart_printf("[Sample-FW] DRAM Mailbox: Initialized at 0x20000100 with Magic %X\n", MAILBOX_STATUS);
    uart_puts("[Sample-FW] Execution status: OK\n");
    uart_puts("======================================================\n");

    /* 6. Interactive UART command shell */
    uart_puts("[Sample-FW] Interactive CLI ready. Type 'help' or 'ping'.\n");
    uart_puts("DJ2620> ");

    char line_buf[64];
    int line_len = 0;

    while (1) {
        watchdog_kick();
        if (uart_has_char()) {
            char c = uart_getc();
            if (c == '\r' || c == '\n') {
                if (line_len > 0) {
                    line_buf[line_len] = '\0';
                    handle_command(line_buf);
                    line_len = 0;
                    uart_puts("\nDJ2620> ");
                } else if (c == '\r') {
                    uart_puts("\nDJ2620> ");
                }
            } else if (c == '\b' || c == 0x7F) {
                if (line_len > 0) {
                    line_len--;
                    uart_puts("\b \b");
                }
            } else if (line_len < (int)(sizeof(line_buf) - 1)) {
                if (c >= ' ' && c <= '~') {
                    line_buf[line_len++] = c;
                    uart_putc(c);
                }
            }
        }
    }
}

