/*
 * Peripheral drivers for test commands: I2C master (EEPROM, undercroft),
 * MICCI serial bus (MCASIC, carriage ASIC), MCASIC ADC, front-panel shift
 * register (LCD segments/icons and buttons), and a DRAM test. Register
 * semantics follow analysis/boot_hardware.md and the emulator models; the
 * same sequences are what HP's own drivers issue.
 */
#ifndef FW_PERIPH_H
#define FW_PERIPH_H
#include <stdint.h>

/* --- I2C (0xBD065800) ------------------------------------------------------ */
enum { I2C_OK = 0, I2C_ERR_TIMEOUT = -1, I2C_ERR_BUSY = -2 };
#define I2C_ADDR_EEPROM     0x50   /* ST M24512 64 KiB NVM, 16-bit word addresses */
#define I2C_ADDR_UNDERCROFT 0x1F
/* Read `len` bytes starting at `sub` (8- or 16-bit sub-address). On a missing
   device the controller stays BUSY; the driver times out and resets it. */
int  i2c_read(uint8_t addr7, uint16_t sub, int sub16, uint8_t *buf, uint32_t len);
int  i2c_probe(uint8_t addr7);           /* I2C_OK when something acknowledges */

/* --- MICCI (controllers 0xBD300800 and 0xBD300C00) -------------------------- */
#define MICCI_CTRL0 0xBD300800U
#define MICCI_CTRL1 0xBD300C00U
enum { MICCI_OK = 0, MICCI_ERR_TIMEOUT = -1, MICCI_ERR_BUSY = -2 };
int  micci_read(uint32_t base, int bus, int dev, int reg, uint16_t *value);
int  micci_write(uint32_t base, int bus, int dev, int reg, uint16_t value);
/* Board devices from the dump's descriptor */
#define MCASIC_CTRL  MICCI_CTRL0
#define MCASIC_BUS   0
#define MCASIC_DEV   6
#define CARRIAGE_CTRL MICCI_CTRL1
#define CARRIAGE_BUS 1
#define CARRIAGE_DEV 4

/* MCASIC ADC: command register 0x10 (bit15 start, bits 14:8 mode, 7:0 selector),
   result register 0x11 (bit15 valid, 9:0 sample). Selector 15 is the internal
   half-scale reference, selector 0 the 24 V supply (28.05 V full scale). */
int  mcasic_adc(uint8_t selector, uint8_t mode, uint16_t *sample);
#define MCASIC_ADC_FULL_SCALE_MV 28050U

/* MCASIC DC-motor bridges, from HP's servo code (lib_servo PWM path
   0xa02bdbf0 -> 0xa016edfa) and the MCASIC board descriptor at 0x200014d4:
   control word = (mode << 12) | pwm, pwm 0..1023, mode 1/2 = the two
   directions, 0 = off. HP's servo 0 uses motor A, servo 1 motor B. HP's
   health check reads the registers after each one (0x32, 0x34, 0x37). */
#define MCASIC_REG_MOTOR_A      0x31
#define MCASIC_REG_MOTOR_B      0x33
#define MCASIC_REG_MOTOR_C      0x35
#define MCASIC_MOTOR_MODE_SHIFT 12
#define MCASIC_MOTOR_PWM_MASK   0x3FFU

/* --- Front panel 64-bit shift register ------------------------------------- */
/* Build the logical frame for a 7-segment character, bar graphs and extra
   indicator bits (bit numbers from panel.py / FP_HWDEFS). Returns 0 and sets
   the low and high words, or -1 for an unknown character or bar count. */
int  panel_encode(char seven_seg, int wifi_bars, int color_bars, int black_bars,
                  uint64_t indicator_bits, uint32_t *lo, uint32_t *hi);
int  panel_indicator_bit(const char *name);    /* -1 when unknown */
const char *panel_indicator_name(int bit);     /* NULL when unnamed */
/* Shift a 64-bit frame out (LSB first) with HP stage-1's waveform, sampling
   the 18 input bits. *verified (optional) is set when the panel echoed the
   0x35 signature and the first output byte, i.e. the frame really arrived. */
uint32_t panel_shift(uint32_t lo, uint32_t hi, int *verified);
/* Last frame sent (physical bits), raw button bits read back, frame count. */
extern volatile uint32_t g_panel_last_lo, g_panel_last_hi, g_panel_last_raw, g_panel_frames;
extern volatile int g_panel_last_ok;
/* Button mask (active-low raw bits 0..17) and names. */
#define PANEL_BUTTON_MASK 0x3FFFFU
const char *panel_button_name(int bit);        /* NULL when unnamed */
void panel_print_buttons(uint32_t raw);

/* --- DRAM test -------------------------------------------------------------- */
/* Writes address-derived and inverted patterns over [addr, addr+len) via the
   given alias and verifies them; returns the number of mismatching words. */
uint32_t memtest(uint32_t addr, uint32_t len, uint32_t *first_bad);

#endif
