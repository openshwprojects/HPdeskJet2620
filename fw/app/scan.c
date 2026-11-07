/*
 * Flatbed scanner (CIS) experiments for the
 * application shell: register dump, one LED colour on for a moment, and a
 * single scan line read through the scan block's light-monitor windows.
 *
 * Everything here comes from HP's firmware (dump.bin, Stage-22 snapshot);
 * see analysis/scanner.md for the addresses that prove each value. Short
 * version:
 *
 *  - The CIS module has its own front-end ASIC, which HP calls "Dasher"
 *    (TI or Wolfson; the newer variant is "Comet"). It sits on MICCI
 *    controller 0 (0xBD300800), sub-bus 1, device 1 (HP handle 0x11,
 *    set at 0xa03a110a; handle -> bus/device in 0xa0171420). HP expects
 *    REG_CHIP_ID = 0x800C (table 0xa05fa4c8[1]; select is hard-wired to 1
 *    by 0xa03a1586). The Dasher holds the LED current DACs, the sensor
 *    timing generator, the ADC and an LVDS serialiser.
 *  - The SoC scan block (0xBD066C00..0xBD06B4xx) receives the LVDS stream
 *    (SCIF), runs the line timing (TG) and the datapath, and drives the
 *    LED PWM timing (ScanIllum*).
 *  - HP never reaches DMA from the scan driver directly; the scan-to-DRAM
 *    path is not known yet. This draft therefore reads pixels through the
 *    two light-monitor windows (ScanDPLm0/1 + ScanLmw0/1*DataRd), the
 *    mechanism HP uses for its LED/illumination checks: one or two pixels
 *    per line, so one 2624-pixel line takes ~1300 line periods.
 *
 * Every write whose effect is not proven is marked UNCERTAIN. Order of use
 * on the printer: "scan regs" (MICCI only, then SoC reads), "scan led g 500",
 * "scan led g 500 soc", then "scan line g".
 */
#include "hw.h"
#include "uart.h"
#include "fmt.h"
#include "timer.h"
#include "periph.h"
#include "libmini.h"
#include "scan.h"

/* ---------------------------------------------------------------------------
 * Dasher (scan front end) on MICCI
 * ------------------------------------------------------------------------- */
#define DASHER_CTRL           MICCI_CTRL0
#define DASHER_BUS            1
#define DASHER_DEV            1
#define DASHER_CHIP_ID        0x800CU   /* 0xa05fa4c8[1] */

/* Register numbers from HP's field table 0xa05f67ac (Dasher, select 1). */
#define DR_CHIP_ID            0x00
#define DR_SUPER_STATUS       0x01      /* b9 FIFO_OVR_FLW, b2 WARM, b0 OTS_LTH */
#define DR_MICCI_STATUS       0x02      /* b15 EN_VEND_REGS, b2:0 LAST_MICCI_ERR */
#define DR_FORCE_RESET        0x03      /* HP writes 0x279A when the chip ID is wrong (0xa03a161a) */
#define DR_SLEEP_MODE         0x06      /* b8 LED_FW_EN, b0 SLEEPMODE */
#define DR_DITHER             0x08
#define DR_OSC_TRAINING       0x09      /* b15 TRAIN_EN, b8:4 TRAIN_FREQ, b3:0 TRAIN_DIV */
#define DR_PT_LOCKOUT         0x0F      /* b15 SM_LOCKOUT, b7 PT_RESET, b3 N_XG_LOCKOUT */
#define DR_PIXEL_CLK_PRESCALE 0x10
#define DR_DUMMY_COUNT        0x11
#define DR_PIXEL_COUNT        0x12
#define DR_TGEN_INIT          0x13
#define DR_XG_HOLD            0x14
#define DR_PHI_EDGE           0x15
#define DR_PHI_XG_TOGGLE1     0x1D
#define DR_PHI_XG_TOGGLE2     0x1E
#define DR_PHI_XG_TOGGLE3     0x1F
#define DR_CCD_RS_EDGE        0x20
#define DR_VSMP_EDGE          0x21
#define DR_BSMP_EDGE          0x22
#define DR_A2D_CLK_EDGE       0x23
#define DR_VLED_CP            0x2F      /* b0 CP_VLED_EN (LED supply charge pump) */
#define DR_ILLUM_STATUS       0x30      /* b12 LED_MONO, b8 LED_RESTART, b5:4 next, b1:0 current state */
#define DR_LED_STATE0         0x31      /* b14:12 MUX, b10:8 COARSE_IDAC, b5:0 FINE_IDAC */
#define DR_LED_STATE1         0x32
#define DR_LED_STATE2         0x33
#define DR_LED_STATE_SEQ      0x34      /* next state of states 3..0 in b13:12, b9:8, b5:4, b1:0 */
#define DR_GPIO_CONFIG        0x50
#define DR_ANALOG_CONFIG      0x60      /* b13:12 BIT_DEPTH, b9 RLC_CLAMP_EN, b8 RLC_CCD, b4 CCD_MODE, b0 VIDEO_EN */
#define DR_PGA_DAC            0x61      /* b13:8 VID_DAC, b2:0 VID_PGA */
#define DR_SZ_N_LOCKOUT       0x70      /* b15 SZ_N_LOCK (HP sets it around LVDS changes) */
#define DR_LVDS_CONFIG        0x71
#define DR_LVDS_PACKETIZER    0x72      /* b15 FULL_SCAN_LINE, b14:0 PACKET_SIZE */
#define DR_LVDS_TEST_PATTERN  0x73
#define DR_IC_REVISION        0x7D      /* b15:8 manufacturer, b7:0 revision */

/* LED MUX codes: HP pairs colour 0/1/2 (Red/Grn/Blu PWM registers, written
   in the same function) with MUX 2/1/4 at 0xa03b3df0 / 0xa03b3e3c / 0xa03b3e86. */
#define DASHER_MUX_RED        2
#define DASHER_MUX_GREEN      1
#define DASHER_MUX_BLUE       4
/* UNCERTAIN: HP derives the IDAC values from its LED calibration (0xa03b3644);
   the scale is unknown. Start low; raise only if nothing is visible. */
#define SCAN_LED_COARSE       1
#define SCAN_LED_FINE         0x10

/* ---------------------------------------------------------------------------
 * SoC scan block (HP's register table 0xa05ff6c4; values = HP snapshot)
 * ------------------------------------------------------------------------- */
#define SCIF_MERGE_DATA_SEL   0xBD066C00U   /* 16 */
#define SCIF_MERGE_NEWPIXID   0xBD066C02U   /* 16 */
#define SCIF_NEW_START        0xBD066C04U   /* 16, HP writes 1 to start (0xa03b5696) */
#define SCIF_MERGE_LINECNT    0xBD066C08U   /* 32, read-only line counter */
#define SCIF_MERGE_PAD_CTRL   0xBD066C0EU   /* 16, b1 clk pad, b0 data pad (decoder 0xa03b8e36) */
#define SCIF_MERGE_INT_STAT   0xBD066C10U   /* 16 */
#define SCIF_MERGE_INT_MASK   0xBD066C12U   /* 16 */
#define SCIF0_CONFIG          0xBD067000U   /* 32: b25 clock_extraction, b24 test_mode, b23 native,
                                               b22:21 pixel_data_width, b19:0 packet_size */
#define SCIF0_PACKETS_PER_LINE 0xBD067004U  /* 16 */
#define SCIF0_SENSOR_ID_MAP   0xBD067006U   /* 16 */
#define SCIF0_BYTE_SYNC       0xBD06700AU   /* 16 */
#define SCIF0_PKT_ERR_CTRL    0xBD06700CU   /* 16 */
#define SCIF0_ERR_FIFO_DATA   0xBD067010U   /* 32 */
#define SCIF0_ERR_FIFO_MASK   0xBD067014U   /* 32 */
#define SCIF0_ERR_FIFO_FULL   0xBD067018U   /* 16 */
#define SCIF0_TP_EXPECTED     0xBD06701AU   /* 16 */
#define SCIF0_TP_CAPTURED     0xBD06701CU   /* 16 */
#define SCIF0_TP_MISMATCH     0xBD06701EU   /* 16 */
#define SCIF0_CLK_FILT_DELAYS 0xBD067020U   /* 32 */
#define SCIF0_DAT_FILT_DELAYS 0xBD067024U   /* 32 */
#define SCAN_SCIF_REF_SELECT  0xBD041010U   /* 16, system block! HP writes 8 (0xa03b5574) */

#define SCAN_AFE_BASE         0xBD067400U   /* 16-bit registers, see analysis/scanner.md */
#define SCAN_TARGET_EXP       0xBD067400U
#define SCAN_STARTUP_LAT      0xBD06741EU
#define SCAN_AFE_TEST_CTRL    0xBD067480U   /* 32 */
#define SCAN_AFE_TEST_PIXSEL  0xBD067484U   /* 32: b31:29 ch_sel, b15:0 pixel_sel */
#define SCAN_AFE_TEST_DATA1   0xBD067488U   /* 16 (x3) */

#define ILLUM_BULB_PWM        0xBD067800U   /* 32 */
#define ILLUM_LED_CALIB       0xBD067804U   /* 32 */
#define ILLUM_CONTROL         0xBD067808U   /* 32, bit layout from decoder 0xa03b7cca */
#define ILLUM_RED_PWM         0xBD06780CU   /* 32: b31:16 on time, b15:0 off time */
#define ILLUM_GRN_PWM         0xBD067810U
#define ILLUM_BLU_PWM         0xBD067814U
#define ILLUM_LED_CONTROL     0xBD067818U   /* 16: b13:7 mask_led, b6:0 ledon_stall */
#define ILLUM_LED_TIMER_DELAY 0xBD06781AU   /* 16 */
#define ILLUM_AUX1_WINDOW     0xBD06781CU   /* 32 */
#define ILLUM_SM_STATE        0xBD067820U   /* 16, read-only */

#define ILLUM_CTL_LEDOPERATENOW (1U << 20)
#define ILLUM_CTL_PWM2_ON       (1U << 14)
#define ILLUM_CTL_PWM1_ON       (1U << 12)
#define ILLUM_CTL_PWM0_ON       (1U << 10)
#define ILLUM_LEDCTL_MASK_ALL   0x3F80U     /* HP's "all LEDs masked" (0xa03b67f0(1)) */

#define DP_LM0_START          0xBD068000U   /* 16 */
#define DP_LM0_TOTAL          0xBD068002U   /* 16, pixels - 1 (0xa03b68c6) */
#define DP_LM1_START          0xBD068004U
#define DP_LM1_TOTAL          0xBD068006U
#define DP_IMAGE_START        0xBD068008U
#define DP_IMAGE_TOTAL        0xBD06800AU
#define DP_MAX_ARRAYS         0xBD068010U
#define DP_CH_COUNTER         0xBD068012U
#define DP_CH_OFFSET          0xBD068014U   /* 32 */
#define DP_STATUS2            0xBD06801AU
#define DP_CONTROL            0xBD06801EU

#define TG_SETUP              0xBD068400U   /* 16-bit registers from here to 0x842C */
#define TG_ASSERT             0xBD068402U
#define TG_HOLD               0xBD068404U
#define TG_RESSEL0_SETUP      0xBD068406U
#define TG_RESSEL0_ASSERT     0xBD068408U
#define TG_RESSEL_CONFIG      0xBD06840CU   /* 32 */
#define TG_MIN_EXP            0xBD068410U
#define TG_MAX_EXP            0xBD068412U
#define TG_CONTROL            0xBD068414U   /* b0 scanner_enable, b3 scan_init_tgs (decoder 0xa03b8106) */
#define TG_A2D_START_EDGE     0xBD068416U
#define TG_PIXEL_TIME         0xBD068418U
#define TG_MASK_EDGES         0xBD06841AU
#define TG_PIXEL_DATA_EN_MASK 0xBD06841CU
#define TG_DATAPATH_CH_EN     0xBD06841EU
#define TG_INT_CONTROL        0xBD068420U
#define TG_STATUS             0xBD068422U   /* b4 tg_control_sm_idle */
#define TG_TRIGGER_WR         0xBD068428U   /* HP writes 1 after starting (0xa03b56a2) */
#define TG_EXP_WINDOW_MODE    0xBD06842AU
#define TG_CTL_SCANNER_ENABLE 0x0001U
#define TG_CTL_SCAN_INIT_TGS  0x0008U

#define PHI0_BASE             0xBD068800U   /* Edge1..12, Init (+0x18), Ctrl (+0x1A) */
#define PHI1_BASE             0xBD068C00U
#define RS_BASE               0xBD069800U   /* Edge1..4, Init (+0x08) */
#define CLAMP_CONTROL         0xBD06B014U
#define CLAMP_CH_SELECT       0xBD06B016U
#define LMW0_RED              0xBD06B018U   /* 32 each: LMW0 R/G/B, LMW1 R/G/B */
#define AVG_CONTROL           0xBD06B400U

#define SCAN_MAX_PIXELS       2816U         /* >= 22 dummy + 2624 image pixels */
#define SCAN_LED_MAX_MS       3000U
#define SCAN_LINE_TIMEOUT_MS  200U          /* per expected line-counter step */
#define SCAN_LINE_TOTAL_MS    30000U

static uint16_t g_scan_line[SCAN_MAX_PIXELS];   /* the captured line (DRAM, .bss) */

static int scan_usage(void)
{
    uart_puts("usage: scan regs | scan led <r|g|b|off> [ms<=3000] [soc] | scan line [r|g|b|dark] [first] [count]\n");
    return -1;
}

/* --- Dasher access -------------------------------------------------------- */
static int dasher_rd(int reg, uint16_t *v)
{
    return micci_read(DASHER_CTRL, DASHER_BUS, DASHER_DEV, reg, v);
}

static int dasher_wr(int reg, uint16_t v)
{
    uint16_t back = 0;
    int rc = micci_write(DASHER_CTRL, DASHER_BUS, DASHER_DEV, reg, v);
    if (rc == MICCI_OK) dasher_rd(reg, &back);
    uart_printf("dasher %02x <- %04x %s, reads %04x\n", reg, v, rc == MICCI_OK ? "ok" : "FAILED", back);
    return rc;
}

/* Returns 0 when the scan front end answers with HP's expected chip ID. */
static int dasher_present(void)
{
    uint16_t id = 0, rev = 0;
    if (dasher_rd(DR_CHIP_ID, &id) != MICCI_OK) {
        uart_puts("dasher: no answer on MICCI ctrl0 bus 1 dev 1\n");
        return -1;
    }
    dasher_rd(DR_IC_REVISION, &rev);
    uart_printf("dasher: chip id %04x (HP expects %04x), manufacturer %02x rev %02x\n",
                id, DASHER_CHIP_ID, rev >> 8, rev & 0xFF);
    return id == DASHER_CHIP_ID ? 0 : -1;
}

/* --- SoC access: print the address and flush before each read, so a bus
       hang on an unclocked block shows which register caused it. ------- */
static uint32_t soc_rd(uint32_t addr, int size)
{
    uart_printf("  %08x ", addr);
    uart_flush();
    return size == 4 ? REG32(addr) : REG16(addr);
}

static void soc_wr16(uint32_t addr, uint16_t v) { REG16(addr) = v; }
static void soc_wr32(uint32_t addr, uint32_t v) { REG32(addr) = v; }

/* --- scan regs ------------------------------------------------------------ */
struct scan_reg { uint32_t addr; uint8_t size; const char *name; };

static const struct scan_reg scan_soc_regs[] = {
    { SCAN_SCIF_REF_SELECT,  2, "ScanScifRefSelect (HP: 0008)" },
    { SCIF_MERGE_DATA_SEL,   2, "ScanScifMergeDataSelect (0001)" },
    { SCIF_NEW_START,        2, "ScanScifNewStart (0001)" },
    { SCIF_MERGE_LINECNT,    4, "ScanScifMergeLineCountRd" },
    { SCIF_MERGE_PAD_CTRL,   2, "ScanScifMergePadControl (0001)" },
    { SCIF_MERGE_INT_STAT,   2, "ScanScifMergeIntStatusRd" },
    { SCIF0_CONFIG,          4, "ScanScif0Config (026015ca)" },
    { SCIF0_PACKETS_PER_LINE,2, "ScanScif0PacketsPerLine (0001)" },
    { SCIF0_SENSOR_ID_MAP,   2, "ScanScif0SensorIdMap (4000)" },
    { SCIF0_BYTE_SYNC,       2, "ScanScif0ByteSync (0e62)" },
    { SCIF0_ERR_FIFO_DATA,   4, "ScanScif0ErrorFifoDataRd" },
    { SCIF0_ERR_FIFO_FULL,   2, "ScanScif0ErrorFifoFullRd" },
    { SCIF0_TP_CAPTURED,     2, "ScanScif0CapturedTestPatternRd" },
    { SCAN_TARGET_EXP,       2, "ScanTargetExp (0c24)" },
    { SCAN_STARTUP_LAT,      2, "ScanStartupLat (0006)" },
    { ILLUM_BULB_PWM,        4, "ScanIllumBulbPwmPeriodOnTime (ffff0000)" },
    { ILLUM_LED_CALIB,       4, "ScanIllumLedCalib (000000ff)" },
    { ILLUM_CONTROL,         4, "ScanIllumControl (00110000)" },
    { ILLUM_RED_PWM,         4, "ScanIllumRedLedDcOrPwm (00500050)" },
    { ILLUM_GRN_PWM,         4, "ScanIllumGrnLedDcOrPwm (00500050)" },
    { ILLUM_BLU_PWM,         4, "ScanIllumBluLedDcOrPwm (00500050)" },
    { ILLUM_LED_CONTROL,     2, "ScanIllumLedControl (3f80)" },
    { ILLUM_SM_STATE,        2, "ScanIllumLedSmCurrentStateRd" },
    { DP_IMAGE_START,        2, "ScanDPImageStartColumn (0016)" },
    { DP_IMAGE_TOTAL,        2, "ScanDPImageTotalPixels (0a40)" },
    { DP_STATUS2,            2, "ScanDPStatus2Rd" },
    { TG_CONTROL,            2, "ScanTGControl (3501)" },
    { TG_PIXEL_TIME,         2, "ScanPixelTime (0058)" },
    { TG_STATUS,             2, "ScanTGStatusRd" },
    { LMW0_RED,              4, "ScanLmw0RedDataRd" },
    { LMW0_RED + 4,          4, "ScanLmw0GrnDataRd" },
    { LMW0_RED + 8,          4, "ScanLmw0BluDataRd" },
    { AVG_CONTROL,           2, "ScanAvgControl (0088)" },
};

static const uint8_t dasher_dump_regs[] = {
    DR_CHIP_ID, DR_SUPER_STATUS, DR_MICCI_STATUS, DR_SLEEP_MODE, DR_DITHER, DR_OSC_TRAINING,
    DR_PT_LOCKOUT, DR_PIXEL_CLK_PRESCALE, DR_DUMMY_COUNT, DR_PIXEL_COUNT, DR_TGEN_INIT,
    DR_VLED_CP, DR_ILLUM_STATUS, DR_LED_STATE0, DR_LED_STATE1, DR_LED_STATE2, DR_LED_STATE_SEQ,
    DR_GPIO_CONFIG, DR_ANALOG_CONFIG, DR_PGA_DAC, DR_SZ_N_LOCKOUT, DR_LVDS_CONFIG,
    DR_LVDS_PACKETIZER, DR_LVDS_TEST_PATTERN, DR_IC_REVISION,
};

void scan_regs_dump_ex(int with_soc)
{
    uint32_t i;
    uart_puts("dasher (MICCI ctrl0 bus 1 dev 1):");
    for (i = 0; i < sizeof dasher_dump_regs; i++) {
        uint16_t v;
        if ((i & 7) == 0) uart_puts("\n ");
        if (dasher_rd(dasher_dump_regs[i], &v) == MICCI_OK) uart_printf(" %02x=%04x", dasher_dump_regs[i], v);
        else uart_printf(" %02x=????", dasher_dump_regs[i]);
        watchdog_kick();
    }
    uart_puts("\n");
    if (!with_soc) {
        uart_puts("(SoC scan block not read; use \"scan regs soc\" once \"md 0xbd067000 1\" returned)\n");
        return;
    }
    uart_puts("SoC scan block (HP's value after boot in brackets):\n");
    for (i = 0; i < sizeof scan_soc_regs / sizeof scan_soc_regs[0]; i++) {
        uint32_t v = soc_rd(scan_soc_regs[i].addr, scan_soc_regs[i].size);
        if (scan_soc_regs[i].size == 4) uart_printf("%08x %s\n", v, scan_soc_regs[i].name);
        else uart_printf("%04x     %s\n", v, scan_soc_regs[i].name);
        watchdog_kick();
    }
}

/* --- LEDs --------------------------------------------------------------- */
static uint16_t led_state_word(int color)
{
    uint16_t mux = color == 'r' ? DASHER_MUX_RED : color == 'g' ? DASHER_MUX_GREEN : DASHER_MUX_BLUE;
    return (uint16_t)((mux << 12) | (SCAN_LED_COARSE << 8) | SCAN_LED_FINE);
}

/* Everything off again: Dasher LED states and supply, SoC LED outputs masked
   (HP's idle value 0x3f80) and the forced-on bits cleared. Safe to call twice. */
static void scan_led_all_off(int touch_soc)
{
    dasher_wr(DR_LED_STATE0, 0);
    dasher_wr(DR_LED_STATE1, 0);
    dasher_wr(DR_LED_STATE2, 0);
    dasher_wr(DR_SLEEP_MODE, 0x0000);       /* LED_FW_EN off, not asleep */
    dasher_wr(DR_VLED_CP, 0x0000);          /* UNCERTAIN: HP leaves it 1 (snapshot shadow) */
    if (touch_soc) {
        soc_wr32(ILLUM_CONTROL, REG32(ILLUM_CONTROL) &
                 ~(ILLUM_CTL_PWM0_ON | ILLUM_CTL_PWM1_ON | ILLUM_CTL_PWM2_ON));
        soc_wr16(ILLUM_LED_CONTROL, ILLUM_LEDCTL_MASK_ALL);
    }
}

/* color: 'r' 'g' 'b', or 0 = off. soc: also force the SoC LED PWM outputs on. */
int scan_led_ex(int color, int ms, int soc)
{
    uint16_t st = 0;
    uint32_t start;
    if (dasher_present() != 0) return -1;
    if (!color) { scan_led_all_off(soc); return 0; }

    dasher_rd(DR_ILLUM_STATUS, &st);
    uart_printf("dasher illum status %04x (current state %u; HP requires 0 before reprogramming)\n", st, st & 3);

    /* HP order (0xa03b3dc8..0xa03b3ed0): states, then VLED, then LED_FW_EN. */
    dasher_wr(DR_SLEEP_MODE, 0x0000);                 /* wake, LED_FW_EN still 0 */
    dasher_wr(DR_LED_STATE_SEQ, 0x0000);              /* all states -> state 0 (HP mono, 0xa03b42b4) */
    dasher_wr(DR_LED_STATE1, 0);
    dasher_wr(DR_LED_STATE2, 0);
    dasher_wr(DR_LED_STATE0, led_state_word(color));  /* UNCERTAIN current */
    dasher_wr(DR_VLED_CP, 0x0001);                    /* LED charge pump on (0xa03b3eba) */
    timer_delay_ms(10);                               /* UNCERTAIN: soft-start time not known */
    dasher_wr(DR_SLEEP_MODE, 0x0100);                 /* LED_FW_EN (0xa03b3ed0) */

    if (soc) {
        /* UNCERTAIN: whether the SoC PWM pins gate the Dasher LED driver, and
           which PWM channel (0/1/2) is used. Force all three on, unmask. */
        uint32_t ctl = soc_rd(ILLUM_CONTROL, 4);
        uart_printf("= %08x (ScanIllumControl before)\n", ctl);
        soc_wr32(ILLUM_CONTROL, (ctl & ~((1U << 11) | (1U << 13) | (1U << 15))) |
                 ILLUM_CTL_PWM0_ON | ILLUM_CTL_PWM1_ON | ILLUM_CTL_PWM2_ON | ILLUM_CTL_LEDOPERATENOW);
        soc_wr16(ILLUM_LED_CONTROL, 0x0000);          /* unmask (HP's manual LED path, 0xa03b7088) */
    }

    uart_printf("scan led %c: on for %u ms\n", color, ms);
    uart_flush();
    start = timer_ticks();
    while (timer_elapsed_ms(start) < (uint32_t)ms) watchdog_kick();
    scan_led_all_off(soc);
    uart_printf("scan led %c: off after %u ms\n", color, timer_elapsed_ms(start));
    return 0;
}

/* --- One line ----------------------------------------------------------- */
/* Dasher configuration HP leaves after boot (shadow array 0x20da0b9c in the
   Stage-22 snapshot), in the order HP's init writes the groups:
   LVDS (0xa03b543c..0xa03b5564), analog (0xa03b51bc..0xa03b5414), timing
   (0xa03b48a0..0xa03b4e8c). OSC_TRAINING and GPIO_CONFIG are left out
   (UNCERTAIN, see analysis/scanner.md). */
static const uint8_t  dasher_cfg_reg[] = {
    DR_SZ_N_LOCKOUT, DR_LVDS_TEST_PATTERN, DR_LVDS_CONFIG, DR_LVDS_PACKETIZER, DR_SZ_N_LOCKOUT,
    DR_ANALOG_CONFIG, DR_PGA_DAC, DR_DITHER,
    DR_TGEN_INIT, DR_PIXEL_CLK_PRESCALE, DR_DUMMY_COUNT, DR_PIXEL_COUNT, DR_XG_HOLD, DR_PHI_EDGE,
    DR_PHI_XG_TOGGLE1, DR_PHI_XG_TOGGLE2, DR_PHI_XG_TOGGLE3, DR_CCD_RS_EDGE, DR_VSMP_EDGE,
    DR_BSMP_EDGE, DR_A2D_CLK_EDGE, DR_PT_LOCKOUT,
};
static const uint16_t dasher_cfg_val[] = {
    0x8000, 0xF0F0, 0x4C02, 0x8000, 0x0000,
    0x3311, 0x3F01, 0x0001,
    0x0011, 0x0027, 0x0016, 0x0399, 0x0005, 0x2002,
    0x403B, 0x4A45, 0xFF00, 0x0901, 0x3B38,
    0x1D1A, 0x1900, 0x0008,
};

/* SoC scan block values HP had written after boot (snapshot), in the order
   of HP's init 0xa03b1de2 / timing / SCIF functions. 16-bit unless noted. */
struct scan_wr { uint32_t addr; uint8_t size; uint32_t val; };
static const struct scan_wr scan_soc_cfg[] = {
    { TG_CONTROL, 2, 0x3500 },                 /* scanner_enable off while configuring */
    { ILLUM_RED_PWM, 4, 0x00500050 }, { ILLUM_GRN_PWM, 4, 0x00500050 }, { ILLUM_BLU_PWM, 4, 0x00500050 },
    { ILLUM_LED_CONTROL, 2, ILLUM_LEDCTL_MASK_ALL },
    { ILLUM_CONTROL, 4, 0x00110000 }, { ILLUM_LED_TIMER_DELAY, 2, 0x0002 },
    { ILLUM_BULB_PWM, 4, 0xFFFF0000 }, { ILLUM_LED_CALIB, 4, 0x000000FF }, { ILLUM_AUX1_WINDOW, 4, 0x00500AF4 },
    { 0xBD067458, 2, 0x0003 },                 /* ScanAfeIoSpeed */
    { DP_CH_COUNTER, 2, 0x0000 }, { DP_CH_OFFSET, 4, 0x00020100 },
    { TG_INT_CONTROL, 2, 0x0000 },
    { SCAN_STARTUP_LAT, 2, 0x0006 }, { 0xBD067420, 2, 0x0000 },        /* Startup/ShutdownLat */
    { SCAN_TARGET_EXP, 2, 0x0C24 }, { 0xBD067402, 2, 0x0024 }, { 0xBD067404, 2, 0x0040 },
    { 0xBD067406, 2, 0x0002 }, { 0xBD067408, 2, 0x0010 }, { 0xBD06740A, 2, 0x001A },
    { 0xBD06740C, 2, 0x0026 }, { 0xBD06740E, 2, 0x002E }, { 0xBD067410, 2, 0x003A },
    { 0xBD067422, 2, 0x0015 }, { 0xBD067424, 2, 0x001E }, { 0xBD067426, 2, 0x0002 }, { 0xBD067428, 2, 0x000C },
    { 0xBD06742A, 2, 0x0015 }, { 0xBD06742C, 2, 0x001E }, { 0xBD06742E, 2, 0x0002 }, { 0xBD067430, 2, 0x000C },
    { 0xBD067432, 2, 0x0015 }, { 0xBD067434, 2, 0x001E }, { 0xBD067436, 2, 0x0002 }, { 0xBD067438, 2, 0x000C },
    { 0xBD06743A, 2, 0x0024 },                 /* ScanNewPixelEdge */
    { 0xBD067442, 2, 0x0001 }, { 0xBD067444, 2, 0x0001 }, { 0xBD067446, 2, 0x0001 },  /* Red/Grn/BluGain */
    { 0xBD067456, 2, 0x008C },                 /* ScanAfeControl */
    { DP_IMAGE_START, 2, 0x0016 }, { DP_IMAGE_TOTAL, 2, 0x0A40 }, { DP_MAX_ARRAYS, 2, 0x0001 },
    { TG_SETUP, 2, 0x021C }, { TG_ASSERT, 2, 0x1C20 }, { TG_HOLD, 2, 0x021C },
    { TG_RESSEL0_SETUP, 2, 0x03E8 }, { TG_RESSEL0_ASSERT, 2, 0x03E8 },
    { TG_MIN_EXP, 2, 0x0C24 }, { TG_MAX_EXP, 2, 0x0C24 },
    { TG_A2D_START_EDGE, 2, 0x0001 }, { TG_PIXEL_TIME, 2, 0x0058 }, { TG_EXP_WINDOW_MODE, 2, 0x0001 },
    { PHI0_BASE + 0x00, 2, 0x0002 }, { PHI0_BASE + 0x02, 2, 0x0025 }, { PHI0_BASE + 0x18, 2, 0x0001 },
    { RS_BASE + 0x00, 2, 0x0004 }, { RS_BASE + 0x02, 2, 0x000E }, { RS_BASE + 0x08, 2, 0x0001 },
    { CLAMP_CONTROL, 2, 0x0001 }, { CLAMP_CH_SELECT, 2, 0x0FFF },
    { AVG_CONTROL, 2, 0x0088 },
    { SCIF_MERGE_DATA_SEL, 2, 0x0001 },
    { SCAN_SCIF_REF_SELECT, 2, 0x0008 },       /* UNCERTAIN: system block 0xBD041000 */
    { SCIF0_CONFIG, 4, 0x026015CA },
    { SCIF0_PACKETS_PER_LINE, 2, 0x0001 }, { SCIF0_SENSOR_ID_MAP, 2, 0x4000 }, { SCIF0_BYTE_SYNC, 2, 0x0E62 },
    { SCIF0_CLK_FILT_DELAYS, 4, 0x40000000 }, { SCIF0_DAT_FILT_DELAYS, 4, 0x40000000 },
    { SCIF_MERGE_PAD_CTRL, 2, 0x0001 }, { SCIF_MERGE_INT_MASK, 2, 0x0001 },
};

static void scan_stop(void)
{
    soc_wr16(TG_CONTROL, REG16(TG_CONTROL) & ~TG_CTL_SCANNER_ENABLE);
    scan_led_all_off(1);
}

/* Waits until the SCIF line counter has advanced by `lines`; 0 on success. */
static int scan_wait_lines(uint32_t lines, uint32_t timeout_ms)
{
    uint32_t c0 = REG32(SCIF_MERGE_LINECNT), start = timer_ticks();
    while (REG32(SCIF_MERGE_LINECNT) - c0 < lines) {
        watchdog_kick();
        if (timer_elapsed_ms(start) >= timeout_ms) return -1;
    }
    return 0;
}

/* Reads `max_px` pixels starting at column `first` of the datapath, two per
   line through the light-monitor windows (1-pixel windows). The window sum
   of the lit colour channel is stored. Returns the number of pixels read. */
int scan_line_ex(uint16_t *out, uint32_t first, uint32_t max_px, int color)
{
    uint32_t i, n = 0, t0, line_ms, lc0, lc1;
    uint32_t lmw_off = color == 'g' ? 4 : color == 'b' ? 8 : 0;   /* UNCERTAIN: channel per colour */

    if (dasher_present() != 0) return -1;
    if (max_px > SCAN_MAX_PIXELS) max_px = SCAN_MAX_PIXELS;

    /* 1. Dasher: HP's post-boot configuration. */
    dasher_wr(DR_SLEEP_MODE, 0x0000);
    for (i = 0; i < sizeof dasher_cfg_reg; i++) {
        if (dasher_wr(dasher_cfg_reg[i], dasher_cfg_val[i]) != MICCI_OK) { scan_led_all_off(0); return -1; }
        watchdog_kick();
    }
    /* 2. SoC: HP's values (prints each address first; a hang shows where). */
    for (i = 0; i < sizeof scan_soc_cfg / sizeof scan_soc_cfg[0]; i++) {
        uart_printf("  %08x <- %08x\n", scan_soc_cfg[i].addr, scan_soc_cfg[i].val);
        uart_flush();
        if (scan_soc_cfg[i].size == 4) soc_wr32(scan_soc_cfg[i].addr, scan_soc_cfg[i].val);
        else soc_wr16(scan_soc_cfg[i].addr, (uint16_t)scan_soc_cfg[i].val);
        watchdog_kick();
    }
    /* 3. Start the line timing the way HP does (0xa03b560e..0xa03b56a8):
          scan_init_tgs pulse, scanner_enable, SCIF new start, TG trigger. */
    soc_wr16(TG_CONTROL, 0x3500 | TG_CTL_SCAN_INIT_TGS);
    soc_wr16(TG_CONTROL, 0x3500);
    soc_wr16(TG_CONTROL, 0x3500 | TG_CTL_SCANNER_ENABLE);
    soc_wr16(SCIF_NEW_START, 0x0001);
    soc_wr16(TG_TRIGGER_WR, 0x0001);

    /* 4. Lines arriving? Measure the line period over 100 ms. */
    lc0 = REG32(SCIF_MERGE_LINECNT);
    t0 = timer_ticks();
    while (timer_elapsed_ms(t0) < 100) watchdog_kick();
    lc1 = REG32(SCIF_MERGE_LINECNT);
    uart_printf("line counter %08x -> %08x in 100 ms; TG status %04x, SCIF err fifo full %04x data %08x\n",
                lc0, lc1, REG16(TG_STATUS), REG16(SCIF0_ERR_FIFO_FULL), REG32(SCIF0_ERR_FIFO_DATA));
    if (lc1 == lc0) {
        uart_puts("scan line: no lines from the SCIF (link or TG not running), stopping\n");
        scan_stop();
        return -1;
    }
    line_ms = 100 / (lc1 - lc0) + 1;

    /* 5. Light (or not, for a dark line). */
    if (color == 'r' || color == 'g' || color == 'b') {
        dasher_wr(DR_LED_STATE_SEQ, 0x0000);
        dasher_wr(DR_LED_STATE0, led_state_word(color));
        dasher_wr(DR_VLED_CP, 0x0001);
        timer_delay_ms(10);
        dasher_wr(DR_SLEEP_MODE, 0x0100);
        /* HP's LED timing for state 0: on at 0x40, off at 2500 (init 0xa03b1de2). */
        soc_wr32(color == 'r' ? ILLUM_RED_PWM : color == 'g' ? ILLUM_GRN_PWM : ILLUM_BLU_PWM, 0x004009C4);
        soc_wr32(ILLUM_CONTROL, REG32(ILLUM_CONTROL) | ILLUM_CTL_LEDOPERATENOW);
        soc_wr16(ILLUM_LED_CONTROL, 0x0000);
        scan_wait_lines(4, SCAN_LINE_TIMEOUT_MS * 4);  /* let the exposure settle */
    }

    /* 5b. Second, independent probe: HP's UDW "AFE test pixel" path
           (0xa03b9616): ScanAfeTestChPixelSel = (ch << 29) | pixel, wait,
           read the three ScanAfeTestPixelData registers. */
    soc_wr32(SCAN_AFE_TEST_PIXSEL, first + 100);
    scan_wait_lines(2, SCAN_LINE_TIMEOUT_MS + 2 * line_ms);
    uart_printf("afe test pixel %u: %04x %04x %04x\n", first + 100,
                REG16(SCAN_AFE_TEST_DATA1), REG16(SCAN_AFE_TEST_DATA1 + 2), REG16(SCAN_AFE_TEST_DATA1 + 4));

    /* 6. Pixels, two per line: window 0 at x, window 1 at x+1, 1 pixel each. */
    soc_wr16(DP_LM0_TOTAL, 0);
    soc_wr16(DP_LM1_TOTAL, 0);
    t0 = timer_ticks();
    for (i = 0; n < max_px; i += 2) {
        soc_wr16(DP_LM0_START, (uint16_t)(first + i));
        soc_wr16(DP_LM1_START, (uint16_t)(first + i + 1));
        /* one line to take the new window, one line to measure */
        if (scan_wait_lines(2, SCAN_LINE_TIMEOUT_MS + 2 * line_ms) != 0) {
            uart_printf("scan line: line counter stopped at pixel %u\n", n);
            break;
        }
        out[n++] = (uint16_t)REG32(LMW0_RED + lmw_off);
        if (n < max_px) out[n++] = (uint16_t)REG32(LMW0_RED + 0x0C + lmw_off);
        if (timer_elapsed_ms(t0) > SCAN_LINE_TOTAL_MS) { uart_puts("scan line: total time limit\n"); break; }
    }

    /* 7. Everything off. */
    scan_stop();
    return (int)n;
}

/* Signatures as requested: MICCI + SoC dump, LED with the SoC outputs forced
   on as well, and a green-lit line from column 0. */
void scan_regs_dump(void) { scan_regs_dump_ex(1); }
int  scan_led(int color, int ms) { return scan_led_ex(color, ms, 1); }
int  scan_line(uint16_t *out, uint32_t max_px) { return scan_line_ex(out, 0, max_px, 'g'); }

static void scan_print_line(const uint16_t *px, uint32_t n, uint32_t first)
{
    uint32_t i, mn = 0xFFFF, mx = 0, sum = 0;
    for (i = 0; i < n; i++) {
        if ((i & 15) == 0) uart_printf("\n%4u:", first + i);
        uart_printf(" %04x", px[i]);
        if (px[i] < mn) mn = px[i];
        if (px[i] > mx) mx = px[i];
        sum += px[i];
        if ((i & 255) == 255) watchdog_kick();
    }
    uart_printf("\n%u pixels from column %u: min %u max %u avg %u\n", n, first, mn, mx, n ? sum / n : 0);
}

int cmd_scan(int argc, char **argv)
{
    uint32_t ms = 1000, first = 0, count = 2624 + 22;
    int color;
    if (argc >= 2 && !strcmp(argv[1], "regs")) {
        scan_regs_dump_ex(argc >= 3 && !strcmp(argv[2], "soc"));
        return 0;
    }
    if (argc >= 3 && !strcmp(argv[1], "led")) {
        color = !strcmp(argv[2], "r") ? 'r' : !strcmp(argv[2], "g") ? 'g' :
                !strcmp(argv[2], "b") ? 'b' : !strcmp(argv[2], "off") ? 0 : -1;
        if (color < 0 || (argc >= 4 && (parse_u32(argv[3], &ms) || ms == 0 || ms > SCAN_LED_MAX_MS)))
            return scan_usage();
        return scan_led_ex(color, (int)ms, argc >= 5 && !strcmp(argv[4], "soc"));
    }
    if (argc >= 2 && !strcmp(argv[1], "line")) {
        int n;
        color = argc >= 3 ? argv[2][0] : 'g';
        if (color != 'r' && color != 'g' && color != 'b' && color != 'd') return scan_usage();
        if ((argc >= 4 && parse_u32(argv[3], &first)) || (argc >= 5 && parse_u32(argv[4], &count)) ||
            count == 0 || first + count > SCAN_MAX_PIXELS)
            return scan_usage();
        n = scan_line_ex(g_scan_line, first, count, color);
        if (n <= 0) return -1;
        scan_print_line(g_scan_line, (uint32_t)n, first);
        return 0;
    }
    return scan_usage();
}
