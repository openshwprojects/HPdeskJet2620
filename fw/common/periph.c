#include "hw.h"
#include "config.h"
#include "periph.h"
#include "timer.h"
#include "uart.h"
#include "libmini.h"

/* ---------------------------------------------------------------------------
 * I2C master: +0 control (bit0 enable, bit1 continue-read, bit2 continue-write,
 * bit7 BUSY read-only, bit8 reset, bit9 16-bit word address), +2 wire address
 * (addr<<1 | read), +4/+6 word address high/low, +8 data. Writing +2 starts a
 * transfer; reading +8 consumes a byte and, when continuation was latched for
 * it, starts the next one. Dividers (+0xc/+0xe) were set by HP stage-1.
 * ------------------------------------------------------------------------- */
#define I2C_CTRL     REG16(0xBD065800U)
#define I2C_ADDR     REG16(0xBD065802U)
#define I2C_SUB_H    REG16(0xBD065804U)
#define I2C_SUB_L    REG16(0xBD065806U)
#define I2C_DATA     REG16(0xBD065808U)
#define I2C_BUSY     0x0080U
#define I2C_RESET    0x0100U
#define I2C_EN       0x0001U
#define I2C_CONT_RD  0x0002U
#define I2C_WORD16   0x0200U
#define I2C_BYTE_TIMEOUT_MS 5U

static int i2c_wait_idle(void)
{
    uint32_t start = timer_ticks();
    while (I2C_CTRL & I2C_BUSY) {
        watchdog_kick();
        if (timer_elapsed_ms(start) >= I2C_BYTE_TIMEOUT_MS) {
            /* No acknowledge: the controller stays BUSY until reset (HP's own
               recovery sequence: reset, then re-enable). */
            I2C_CTRL = I2C_RESET;
            I2C_CTRL = I2C_EN;
            return I2C_ERR_TIMEOUT;
        }
    }
    return I2C_OK;
}

int i2c_read(uint8_t addr7, uint16_t sub, int sub16, uint8_t *buf, uint32_t len)
{
    uint32_t i;
    uint16_t ctrl;
    if (len == 0) return I2C_OK;
    if (I2C_CTRL & I2C_BUSY) {
        I2C_CTRL = I2C_RESET;
        I2C_CTRL = I2C_EN;
    }
    ctrl = I2C_EN | (sub16 ? I2C_WORD16 : 0) | (len > 1 ? I2C_CONT_RD : 0);
    I2C_CTRL = ctrl;
    if (sub16) I2C_SUB_H = (uint16_t)(sub >> 8);
    I2C_SUB_L = (uint16_t)(sub & 0xFF);
    I2C_ADDR = (uint16_t)(((addr7 & 0x7F) << 1) | 1);
    for (i = 0; i < len; i++) {
        if (i2c_wait_idle() != I2C_OK) return I2C_ERR_TIMEOUT;
        if (i + 2 == len) {
            /* Two bytes remain: clear continuation before consuming the
               penultimate byte so the final byte starts without it. */
            ctrl &= (uint16_t)~I2C_CONT_RD;
            I2C_CTRL = ctrl;
        }
        buf[i] = (uint8_t)(I2C_DATA & 0xFF);
    }
    return I2C_OK;
}

int i2c_probe(uint8_t addr7)
{
    uint8_t b;
    return i2c_read(addr7, 0, 0, &b, 1);
}

/* ---------------------------------------------------------------------------
 * MICCI: +0 config (nonzero after HP init), +0xa6 status (bit1 error),
 * +0xac command, +0xb0 data (bit31 busy, 15:0 payload).
 * command = dev<<13 | reg<<6 | read<<5 | bus<<4 | 0xf
 * ------------------------------------------------------------------------- */
#define MICCI_TIMEOUT_MS 5U

static int micci_wait(uint32_t base)
{
    uint32_t start = timer_ticks();
    while (REG32(base + 0xB0) & 0x80000000U) {
        watchdog_kick();
        if (timer_elapsed_ms(start) >= MICCI_TIMEOUT_MS) return MICCI_ERR_TIMEOUT;
    }
    return MICCI_OK;
}

static uint16_t micci_command(int bus, int dev, int reg, int read)
{
    return (uint16_t)(((dev & 7) << 13) | ((reg & 0x7F) << 6) | (read ? 0x20 : 0) | ((bus & 1) << 4) | 0x0F);
}

int micci_read(uint32_t base, int bus, int dev, int reg, uint16_t *value)
{
    if (REG16(base + 0x00) == 0) REG16(base + 0x00) = 0x4001;   /* controller not configured */
    if (REG32(base + 0xB0) & 0x80000000U) return MICCI_ERR_BUSY;
    REG32(base + 0xB0) = 0;
    REG16(base + 0xAC) = micci_command(bus, dev, reg, 1);
    if (micci_wait(base) != MICCI_OK) return MICCI_ERR_TIMEOUT;
    *value = (uint16_t)(REG32(base + 0xB0) & 0xFFFF);
    return MICCI_OK;
}

int micci_write(uint32_t base, int bus, int dev, int reg, uint16_t value)
{
    if (REG16(base + 0x00) == 0) REG16(base + 0x00) = 0x4001;
    if (REG32(base + 0xB0) & 0x80000000U) return MICCI_ERR_BUSY;
    REG32(base + 0xB0) = value;
    REG16(base + 0xAC) = micci_command(bus, dev, reg, 0);
    return micci_wait(base);
}

int mcasic_adc(uint8_t selector, uint8_t mode, uint16_t *sample)
{
    uint16_t cmd = (uint16_t)(0x8000U | ((uint16_t)(mode & 0x7F) << 8) | selector);
    uint16_t result = 0;
    uint32_t start;
    int rc = micci_write(MCASIC_CTRL, MCASIC_BUS, MCASIC_DEV, 0x10, cmd);
    if (rc != MICCI_OK) return rc;
    start = timer_ticks();
    for (;;) {
        rc = micci_read(MCASIC_CTRL, MCASIC_BUS, MCASIC_DEV, 0x11, &result);
        if (rc != MICCI_OK) return rc;
        if (result & 0x8000) break;
        if (timer_elapsed_ms(start) >= 50U) return MICCI_ERR_TIMEOUT;
        watchdog_kick();
    }
    *sample = result & 0x3FF;
    return MICCI_OK;
}

/* ---------------------------------------------------------------------------
 * Front panel shift register (bit numbers from panel.py)
 * ------------------------------------------------------------------------- */
#define SEG_A (1ULL << 39)
#define SEG_B (1ULL << 38)
#define SEG_C (1ULL << 37)
#define SEG_D (1ULL << 36)
#define SEG_E (1ULL << 33)
#define SEG_F (1ULL << 35)
#define SEG_G (1ULL << 34)
#define PANEL_POLARITY_MASK 0x0F00ULL

static const struct { char ch; uint64_t segs; } seven_seg_table[] = {
    { ' ', 0 },
    { '0', SEG_A | SEG_B | SEG_C | SEG_D | SEG_E | SEG_F },
    { '1', SEG_B | SEG_C },
    { '2', SEG_A | SEG_B | SEG_D | SEG_E | SEG_G },
    { '3', SEG_A | SEG_B | SEG_C | SEG_D | SEG_G },
    { '4', SEG_B | SEG_C | SEG_F | SEG_G },
    { '5', SEG_A | SEG_C | SEG_D | SEG_F | SEG_G },
    { '6', SEG_A | SEG_C | SEG_D | SEG_E | SEG_F | SEG_G },
    { '7', SEG_A | SEG_B | SEG_C },
    { '8', SEG_A | SEG_B | SEG_C | SEG_D | SEG_E | SEG_F | SEG_G },
    { '9', SEG_A | SEG_B | SEG_C | SEG_D | SEG_F | SEG_G },
    { 'A', SEG_A | SEG_B | SEG_C | SEG_E | SEG_F | SEG_G },
    { 'b', SEG_C | SEG_D | SEG_E | SEG_F | SEG_G },
    { 'C', SEG_A | SEG_D | SEG_E | SEG_F },
    { 'd', SEG_B | SEG_C | SEG_D | SEG_E | SEG_G },
    { 'E', SEG_A | SEG_D | SEG_E | SEG_F | SEG_G },
    { 'F', SEG_A | SEG_E | SEG_F | SEG_G },
    { 'J', SEG_B | SEG_C | SEG_D },
    { 'P', SEG_A | SEG_B | SEG_E | SEG_F | SEG_G },
    { 'U', SEG_B | SEG_C | SEG_D | SEG_E | SEG_F },
    { 'n', SEG_A | SEG_B | SEG_C | SEG_E | SEG_F },
    { '-', SEG_G },
};

/* Elements as seen on the owner's printer with "lcd walk" (analysis/front_panel.md).
   Bits 8, 9 and 11 are the orange LEDs at the Ink (Cancel), Resume and Info
   buttons; LEDs are active-low, hence HP's 0x0F00 XOR. Ink bars are numbered
   from the bottom, Wi-Fi bars from the smallest. Bits 10 and 45 light nothing. */
static const struct { const char *name; int bit; } indicator_table[] = {
    { "ink_led", 8 }, { "resume_led", 9 }, { "info_led", 11 }, { "wifi_icon", 12 },
    { "paper_icon", 13 }, { "exclamation_icon", 14 }, { "letter_e_icon", 15 },
    { "color_ink_bar_1", 16 }, { "color_ink_bar_3", 17 }, { "color_ink_bar_2", 18 },
    { "color_ink_outline", 19 }, { "wifi_direct_icon", 20 }, { "ink_drop_icon", 21 },
    { "black_ink_bar_2", 22 }, { "black_ink_outline", 23 }, { "wifi_bar_1", 24 },
    { "drop_on_paper_icon", 25 }, { "black_ink_bar_3", 26 }, { "black_ink_bar_1", 27 },
    { "wifi_bar_2", 28 }, { "wifi_bar_3", 29 }, { "wifi_bar_4", 30 }, { "wifi_bar_5", 31 },
    { "wireless_attention_icon", 32 }, { "unused_bit45", 45 },
};

static char lower_or_same(char c) { return (c >= 'A' && c <= 'Z') ? (char)(c + 32) : c; }

int panel_indicator_bit(const char *name)
{
    unsigned i;
    for (i = 0; i < sizeof(indicator_table) / sizeof(indicator_table[0]); i++)
        if (!strcmp(indicator_table[i].name, name)) return indicator_table[i].bit;
    return -1;
}

const char *panel_indicator_name(int bit)
{
    unsigned i;
    for (i = 0; i < sizeof(indicator_table) / sizeof(indicator_table[0]); i++)
        if (indicator_table[i].bit == bit) return indicator_table[i].name;
    return 0;
}

int panel_encode(char seven_seg, int wifi_bars, int color_bars, int black_bars,
                 uint64_t indicator_bits, uint32_t *lo, uint32_t *hi)
{
    static const int wifi_bits[5] = { 24, 28, 29, 30, 31 };    /* smallest to biggest */
    static const int color_bits[3] = { 16, 18, 17 };           /* bottom, middle, top */
    static const int black_bits[3] = { 27, 22, 26 };
    uint64_t logical = indicator_bits;
    unsigned i;
    int found = 0;
    for (i = 0; i < sizeof(seven_seg_table) / sizeof(seven_seg_table[0]); i++) {
        char ch = seven_seg_table[i].ch;
        if (ch == seven_seg || lower_or_same(ch) == lower_or_same(seven_seg)) {
            logical |= seven_seg_table[i].segs;
            found = 1;
            break;
        }
    }
    if (!found) return -1;
    if (wifi_bars < 0 || wifi_bars > 5 || color_bars < 0 || color_bars > 3 || black_bars < 0 || black_bars > 3)
        return -1;
    if (wifi_bars > 0) {
        logical |= 1ULL << 12;                                  /* Wi-Fi antenna icon */
        for (i = 0; i < (unsigned)wifi_bars; i++) logical |= 1ULL << wifi_bits[i];
    }
    if (color_bars > 0) {
        logical |= 1ULL << 19;                                  /* gauge outline */
        for (i = 0; i < (unsigned)color_bars; i++) logical |= 1ULL << color_bits[i];
    }
    if (black_bars > 0) {
        logical |= 1ULL << 23;
        for (i = 0; i < (unsigned)black_bars; i++) logical |= 1ULL << black_bits[i];
    }
    logical ^= PANEL_POLARITY_MASK;
    *lo = (uint32_t)logical;
    *hi = (uint32_t)(logical >> 32);
    return 0;
}

/*
 * Frame exchange exactly as HP stage-1 drives the panel (0xa000e0f6, probe
 * 0xa000e3dc). On the printer stage-1 leaves its driver state at 0x60001040
 * ("md 0x60001040 10"): delay unit 3 us, clock-low template 0x0009,
 * clock-high template 0x000b, polarity 0, detection state 1 (serial panel).
 * Output register bits: 0 must stay set, 1 clock, 2 data, 3 low = reset.
 * Every edge is followed by 75 units; the input (bit 8) is sampled while the
 * clock is high. 18 active-low inputs come first, then 16 verification bits
 * (0x35, then the low eight output bits).
 */
#define PANEL_TPL_LOW     0x0009U
#define PANEL_TPL_HIGH    0x000BU
#define PANEL_RESET_BIT   0x0008U
#define PANEL_DATA_BIT    0x0004U
#define PANEL_UNIT_US     3U
#define PANEL_INPUT_BITS  18U
#define PANEL_VERIFY_BITS 16U
#define PANEL_FRAME_BITS  64U

static int panel_synced;

/* The last frame shifted out and what came back (for the web page). */
volatile uint32_t g_panel_last_lo, g_panel_last_hi, g_panel_last_raw, g_panel_frames;
volatile int g_panel_last_ok;

uint32_t panel_shift(uint32_t lo, uint32_t hi, int *verified)
{
    uint32_t raw = 0, verify = ((lo & 0xFFU) << 8) | 0x35U, i;
    int ok = 1;

    if (!panel_synced) {
        GPIO_DIR(GPIO_PIN_PANEL_STROBE) = 1;
        PANEL_CTRL = (uint16_t)(PANEL_TPL_LOW ^ PANEL_RESET_BIT);   /* bit 3 low: reset pulse */
        timer_delay_us(20U * PANEL_UNIT_US);
        PANEL_CTRL = PANEL_TPL_LOW;
        timer_delay_us(125U * PANEL_UNIT_US);
        timer_delay_us(66000U);                                     /* 0x101d0 in HP's code */
        panel_synced = 1;
    }
    timer_delay_us(75U * PANEL_UNIT_US);
    GPIO_OUTPUT(GPIO_PIN_PANEL_STROBE) = 0;
    timer_delay_us(120U * PANEL_UNIT_US);
    GPIO_OUTPUT(GPIO_PIN_PANEL_STROBE) = 1;
    timer_delay_us(95U * PANEL_UNIT_US);
    for (i = 0; i < PANEL_FRAME_BITS; i++) {
        uint32_t bit = i < 32 ? (lo >> i) & 1U : (hi >> (i - 32)) & 1U;
        uint16_t data = bit ? (uint16_t)PANEL_DATA_BIT : 0;
        PANEL_CTRL = (uint16_t)(PANEL_TPL_LOW | data);
        uart_rx_service();              /* a frame takes ~45 ms: keep typed bytes */
        timer_delay_us(75U * PANEL_UNIT_US);
        PANEL_CTRL = (uint16_t)(PANEL_TPL_HIGH | data);
        uart_rx_service();
        timer_delay_us(75U * PANEL_UNIT_US);
        /* Like HP, the input is only read for the 34 positions it defines. */
        if (i < PANEL_INPUT_BITS) {
            raw |= ((PANEL_MISO >> 8) & 1U) << i;
        } else if (i < PANEL_INPUT_BITS + PANEL_VERIFY_BITS) {
            if (((PANEL_MISO >> 8) & 1U) != (verify & 1U)) ok = 0;
            verify >>= 1;
        }
        PANEL_CTRL = (uint16_t)(PANEL_TPL_LOW | data);
        uart_rx_service();
        timer_delay_us(75U * PANEL_UNIT_US);
    }
    GPIO_OUTPUT(GPIO_PIN_PANEL_STROBE) = 0;
    timer_delay_us(120U * PANEL_UNIT_US);
    GPIO_OUTPUT(GPIO_PIN_PANEL_STROBE) = 1;
    timer_delay_us(30U * PANEL_UNIT_US);
    if (!ok) panel_synced = 0;          /* like HP: the next frame re-synchronises */
    if (verified) *verified = ok;
    g_panel_last_lo = lo;
    g_panel_last_hi = hi;
    g_panel_last_raw = raw;
    g_panel_last_ok = ok;
    g_panel_frames++;
    return raw;
}

const char *panel_button_name(int bit)
{
    switch (bit) {
    case 1: return "power";
    case 2: return "resume";
    case 3: return "wireless";
    case 6: return "cancel";
    case 7: return "info";
    case 8: return "copy_color";
    case 9: return "copy_black";
    default: return 0;
    }
}

void panel_print_buttons(uint32_t raw)
{
    uint32_t pressed = (~raw) & PANEL_BUTTON_MASK;   /* active-low */
    int bit, any = 0;
    uart_printf("buttons raw %05x pressed:", raw & PANEL_BUTTON_MASK);
    for (bit = 0; bit < 18; bit++) {
        if (pressed & (1U << bit)) {
            const char *name = panel_button_name(bit);
            if (name) uart_printf(" %s", name); else uart_printf(" bit%d", bit);
            any = 1;
        }
    }
    uart_puts(any ? "\n" : " none\n");
}

/* --------------------------------------------------------------------------- */
uint32_t memtest(uint32_t addr, uint32_t len, uint32_t *first_bad)
{
    volatile uint32_t *p = (volatile uint32_t *)(uintptr_t)addr;
    uint32_t words = len / 4, i, bad = 0, pass;
    static const uint32_t patterns[2] = { 0x00000000U, 0xFFFFFFFFU };
    if (first_bad) *first_bad = 0;
    for (pass = 0; pass < 2; pass++) {
        uint32_t x = patterns[pass];
        for (i = 0; i < words; i++) p[i] = (addr + i * 4) ^ x;
        for (i = 0; i < words; i++) {
            if (p[i] != ((addr + i * 4) ^ x)) {
                if (!bad && first_bad) *first_bad = addr + i * 4;
                bad++;
            }
        }
        watchdog_kick();
    }
    return bad;
}
