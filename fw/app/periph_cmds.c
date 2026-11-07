/* Peripheral test commands for the application shell. */
#include "hw.h"
#include "layout.h"
#include "uart.h"
#include "fmt.h"
#include "timer.h"
#include "log.h"
#include "cli.h"
#include "periph.h"
#include "libmini.h"
#include "periph_cmds.h"
#include "ui.h"

static int usage(const char *text)
{
    uart_printf("usage: %s\n", text);
    return -1;
}

int cmd_i2c(int argc, char **argv)
{
    if (argc >= 2 && !strcmp(argv[1], "scan")) {
        uint32_t a, found = 0;
        uart_puts("i2c scan:");
        for (a = 0x08; a < 0x78; a++) {
            if (i2c_probe((uint8_t)a) == I2C_OK) {
                uart_printf(" 0x%02x%s", a, a == I2C_ADDR_EEPROM ? "(eeprom)" : a == I2C_ADDR_UNDERCROFT ? "(undercroft)" : "");
                found++;
            }
        }
        uart_printf("%s (%u device%s)\n", found ? "" : " none", found, found == 1 ? "" : "s");
        return 0;
    }
    if (argc >= 4 && !strcmp(argv[1], "read")) {
        uint32_t addr, sub, len = 1;
        static uint8_t buf[256];
        int sub16 = argc >= 6 && !strcmp(argv[5], "16");
        if (parse_u32(argv[2], &addr) || parse_u32(argv[3], &sub) || (argc >= 5 && parse_u32(argv[4], &len)) ||
            addr > 0x7F || len == 0 || len > sizeof(buf) || sub > 0xFFFF)
            return usage("i2c read <addr7> <sub> [len] [16]");
        int rc = i2c_read((uint8_t)addr, (uint16_t)sub, sub16 || sub > 0xFF, buf, len);
        if (rc != I2C_OK) { uart_printf("i2c 0x%02x: no acknowledge (controller reset)\n", addr); return -1; }
        uart_hexdump(sub, buf, len);
        return 0;
    }
    return usage("i2c scan | i2c read <addr7> <sub> [len] [16]");
}

int cmd_eeprom(int argc, char **argv)
{
    uint32_t off = 0, len = 64;
    static uint8_t buf[256];
    if ((argc >= 2 && parse_u32(argv[1], &off)) || (argc >= 3 && parse_u32(argv[2], &len)) || off > 0xFFFF ||
        len == 0 || len > sizeof(buf))
        return usage("eeprom [offset] [len<=256]  (read-only dump of the 24512 NVM)");
    int rc = i2c_read(I2C_ADDR_EEPROM, (uint16_t)off, 1, buf, len);
    if (rc != I2C_OK) { uart_puts("eeprom: no acknowledge from 0x50\n"); return -1; }
    uart_hexdump(off, buf, len);
    return 0;
}

static int micci_ctrl_arg(const char *s, uint32_t *base)
{
    if (!strcmp(s, "0")) { *base = MICCI_CTRL0; return 0; }
    if (!strcmp(s, "1")) { *base = MICCI_CTRL1; return 0; }
    return -1;
}

int cmd_micci(int argc, char **argv)
{
    uint32_t base, bus, dev, reg, val;
    uint16_t value;
    if (argc >= 6 && !strcmp(argv[1], "read") && !micci_ctrl_arg(argv[2], &base) && !parse_u32(argv[3], &bus) &&
        !parse_u32(argv[4], &dev) && !parse_u32(argv[5], &reg)) {
        int rc = micci_read(base, (int)bus, (int)dev, (int)reg, &value);
        if (rc != MICCI_OK) { uart_printf("micci: %s\n", rc == MICCI_ERR_BUSY ? "busy" : "timeout"); return -1; }
        uart_printf("micci ctrl%s bus %u dev %u reg 0x%02x = 0x%04x\n", argv[2], bus, dev, reg, value);
        return 0;
    }
    if (argc >= 7 && !strcmp(argv[1], "write") && !micci_ctrl_arg(argv[2], &base) && !parse_u32(argv[3], &bus) &&
        !parse_u32(argv[4], &dev) && !parse_u32(argv[5], &reg) && !parse_u32(argv[6], &val)) {
        int rc = micci_write(base, (int)bus, (int)dev, (int)reg, (uint16_t)val);
        if (rc != MICCI_OK) { uart_printf("micci: %s\n", rc == MICCI_ERR_BUSY ? "busy" : "timeout"); return -1; }
        uart_printf("micci ctrl%s bus %u dev %u reg 0x%02x <- 0x%04x\n", argv[2], bus, dev, reg, val & 0xFFFF);
        return 0;
    }
    return usage("micci read <ctrl 0|1> <bus> <dev> <reg> | micci write <ctrl> <bus> <dev> <reg> <val> (careful)");
}

/*
 * "motor": read the MCASIC motor registers, or give one motor a single short,
 * weak pulse. Limits are deliberately low: at most MOTOR_MAX_PWM of 1023 for
 * at most MOTOR_MAX_MS, and every motor is switched off afterwards, even when
 * a bus transfer failed.
 */
#define MOTOR_MAX_PWM 600U
#define MOTOR_MAX_MS  250U

static const uint8_t motor_regs[3] = { MCASIC_REG_MOTOR_A, MCASIC_REG_MOTOR_B, MCASIC_REG_MOTOR_C };

/* Position counters HP's servo code reads (0xa0177440): channels 7 and 8
   (16-bit, in the low half) and channel 10. Reading them is harmless. */
static const uint32_t motor_counters[3] = { 0xBD302010U, 0xBD302410U, 0xBD300038U };

static void motor_read_counters(uint32_t *out)
{
    unsigned i;
    for (i = 0; i < 3; i++) out[i] = REG32(motor_counters[i]);
}

static int motor_write(uint8_t reg, uint16_t value)
{
    return micci_write(MCASIC_CTRL, MCASIC_BUS, MCASIC_DEV, reg, value);
}

static void motor_all_off(void)
{
    unsigned i;
    for (i = 0; i < sizeof(motor_regs); i++)
        if (motor_write(motor_regs[i], 0) != MICCI_OK)
            uart_printf("motor: switching register 0x%02x off FAILED\n", motor_regs[i]);
}

static void motor_show(void)
{
    static const uint8_t regs[] = { 0x08, 0x09, 0x0a, 0x30, 0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37 };
    unsigned i;
    uart_puts("mcasic:");
    for (i = 0; i < sizeof(regs); i++) {
        uint16_t v = 0;
        if (micci_read(MCASIC_CTRL, MCASIC_BUS, MCASIC_DEV, regs[i], &v) == MICCI_OK)
            uart_printf(" %02x=%04x", regs[i], v);
        else
            uart_printf(" %02x=????", regs[i]);
    }
    uart_puts("\n");
}

/* MCASIC registers that HP's running firmware had set (Stage-22 emulator
   snapshot) but this firmware never wrote before its motor pulses; see
   analysis/motors.md. Order as HP's MCASIC wake/restore routine
   (0xa0170ef6, registers from the board descriptor at +0x10e): 0x20 &=
   ~0x8000 (mask 0xffff7fff), 0x21, 0x22, 0x23, 0x20 = full value (bit 15 set
   again), then its register-block loop, which covers 0x30 and the MCASIC
   GPIO registers (0x3a bits 15/14 = pick and scan encoder power). Values from
   the snapshot. Nothing moves. */
static const uint8_t hp_state_regs[] = { 0x20, 0x21, 0x22, 0x23, 0x20, 0x30, 0x38, 0x39, 0x3a };
static const uint16_t hp_state_vals[] = { 0x0000, 0x0101, 0x0000, 0x0000, 0xC070, 0x8000, 0x0030, 0x0030, 0xC000 };

static int g_motor_inited;
static int g_active_motor = -1;
static int g_active_mode;
static uint32_t g_active_pwm;
static uint32_t g_active_start_ticks;
static uint32_t g_active_lease_ms;
static uint32_t g_hold_begin_ticks;

#define MOTOR_WEB_MAX_MS  1000U
#define MOTOR_HOLD_MAX_MS 8000U

/*
 * Encoders, measured on the printer (2026-11-05, 150 ms pulses at PWM 350):
 * Motor B (carriage) counts on channel 10, about -5970 forward (left) and
 * +5930 reverse; Motor C (scanner) on channel 7, +3060 forward and -3100
 * reverse. Motor A's encoder is not on channels 7, 8 or 10 (channel 8 stays 0
 * even with its enable bit 0x80 in 0xBD303C00 set), so A has no stall
 * detection and runs on time limits only. The low 16 bits of each counter
 * are extended in software, as HP's 0xa01773d8 does.
 */
static const int motor_encoder[3] = { -1, 2, 0 };          /* index into motor_counters, -1 = none */
static const int motor_fwd_sign[3] = { 0, -1, 1 };          /* how the count moves when driving forward */
static uint16_t enc_last[3];
static int32_t enc_pos[3];
static int enc_started;

#define STALL_GRACE_MS   150U    /* spin-up before the first check */
#define STALL_WINDOW_MS  60U
#define STALL_MIN_COUNTS 12      /* less movement than this per window = blocked */
#define HOME_PWM         200U    /* gentle: end stops are hit at this speed */
#define HOME_MAX_MS      10000U

static int32_t stall_ref_pos;
static uint32_t stall_ref_ticks;
static int g_stall_motor = -1, g_stall_mode;               /* last stall, refused again until cleared */
static int32_t g_stall_pos;

static void enc_update(void)
{
    unsigned m;
    for (m = 0; m < 3; m++) {
        uint16_t raw;
        if (motor_encoder[m] < 0) continue;
        raw = (uint16_t)REG32(motor_counters[motor_encoder[m]]);
        if (enc_started) enc_pos[m] += (int16_t)(uint16_t)(raw - enc_last[m]);
        enc_last[m] = raw;
    }
    enc_started = 1;
}

static int32_t iabs32(int32_t v) { return v < 0 ? -v : v; }

static int motor_init_hp_state(void)
{
    unsigned i;
    int rc = 0;
    uint16_t r20 = 0;
    motor_all_off();
    g_active_motor = -1;
    g_active_mode = 0;
    g_active_pwm = 0;
    g_active_lease_ms = 0;
    if (micci_read(MCASIC_CTRL, MCASIC_BUS, MCASIC_DEV, 0x20, &r20) == MICCI_OK) {
        /* HP clears only the enable bit first */
        motor_write(0x20, (uint16_t)(r20 & 0x7FFFU));
        uart_printf("mcasic 20 <- %04x (enable bit cleared)\n", r20 & 0x7FFFU);
    }
    for (i = 1; i < sizeof(hp_state_regs); i++) {
        uint16_t back = 0;
        int w = motor_write(hp_state_regs[i], hp_state_vals[i]);
        int r = micci_read(MCASIC_CTRL, MCASIC_BUS, MCASIC_DEV, hp_state_regs[i], &back);
        uart_printf("mcasic %02x <- %04x %s, reads %04x\n", hp_state_regs[i], hp_state_vals[i],
                    w == MICCI_OK ? "ok" : "FAILED", r == MICCI_OK ? back : 0xFFFFU);
        if (w != MICCI_OK) rc = -1;
    }
    if (rc == 0) g_motor_inited = 1;
    uart_puts("motor: HP's register state applied (motors off); try a pulse next\n");
    return rc;
}

int motor_api_init(void)
{
    return motor_init_hp_state();
}

static void motor_stop_internal(void)
{
    motor_all_off();
    g_active_motor = -1;
    g_active_mode = 0;
    g_active_pwm = 0;
    g_active_lease_ms = 0;
}

/* Explicit stop (web STOP, button released): also forgets the last stall. */
void motor_api_stop(void)
{
    motor_stop_internal();
    g_stall_motor = -1;
}

int motor_api_drive(int motor_idx, int mode, uint32_t pwm, uint32_t ms)
{
    uint16_t word;
    if (motor_idx < 0 || motor_idx > 2 || (mode != 1 && mode != 2) || pwm == 0)
        return -1;
    /* Do not push into the end stop that just stopped this motor; another
       direction, another motor or an explicit stop clears it. */
    if (motor_idx == g_stall_motor && mode == g_stall_mode) return -3;
    g_stall_motor = -1;
    if (pwm > MOTOR_MAX_PWM) pwm = MOTOR_MAX_PWM;
    if (ms == 0) ms = 150U;
    if (ms > MOTOR_WEB_MAX_MS) ms = MOTOR_WEB_MAX_MS;
    if (!g_motor_inited) {
        if (motor_init_hp_state() != 0) return -1;
    }
    if (g_active_motor >= 0 && g_active_motor != motor_idx) {
        motor_all_off();
        g_hold_begin_ticks = timer_ticks();
    } else if (g_active_motor < 0 || g_active_mode != mode) {
        g_hold_begin_ticks = timer_ticks();
    }
    if (timer_elapsed_ms(g_hold_begin_ticks) >= MOTOR_HOLD_MAX_MS) {
        motor_stop_internal();
        return -2;
    }
    if (g_active_motor != motor_idx || g_active_mode != mode) {
        enc_update();
        stall_ref_pos = enc_pos[motor_idx];
        stall_ref_ticks = timer_ticks();
    }
    word = (uint16_t)(((uint32_t)mode << MCASIC_MOTOR_MODE_SHIFT) | (pwm & MCASIC_MOTOR_PWM_MASK));
    if (motor_write(motor_regs[motor_idx], word) != MICCI_OK) {
        motor_stop_internal();
        return -1;
    }
    g_active_motor = motor_idx;
    g_active_mode = mode;
    g_active_pwm = pwm;
    g_active_start_ticks = timer_ticks();
    g_active_lease_ms = ms;
    return 0;
}

/* Called while a motor runs: 1 when its encoder shows it is blocked. */
static int stall_check(int m)
{
    int32_t moved;
    if (motor_encoder[m] < 0 || timer_elapsed_ms(stall_ref_ticks) < STALL_WINDOW_MS) return 0;
    moved = iabs32(enc_pos[m] - stall_ref_pos);
    stall_ref_pos = enc_pos[m];
    stall_ref_ticks = timer_ticks();
    return timer_elapsed_ms(g_hold_begin_ticks) >= STALL_GRACE_MS && moved < STALL_MIN_COUNTS;
}

void motor_api_poll(void)
{
    enc_update();
    if (g_active_motor < 0) return;
    if (stall_check(g_active_motor)) {
        int m = g_active_motor, mode = g_active_mode;
        motor_stop_internal();
        g_stall_motor = m;
        g_stall_mode = mode;
        g_stall_pos = enc_pos[m];
        uart_printf("motor %c: stalled at position %d (end stop or blocked), switched off\n", 'a' + m,
                    (int)enc_pos[m]);
        return;
    }
    if (timer_elapsed_ms(g_active_start_ticks) >= g_active_lease_ms ||
        timer_elapsed_ms(g_hold_begin_ticks) >= MOTOR_HOLD_MAX_MS) {
        motor_stop_internal();
    }
}

static int motor_has_encoder(int m) { return m >= 0 && m < 3 && motor_encoder[m] >= 0; }

/* Run motor m in `mode` until its encoder stops (end stop) or max_ms.
   Returns 0 on a stall, -2 on timeout, -1 on a bus error. */
static int drive_until_stall(int m, int mode, uint32_t pwm, uint32_t max_ms)
{
    uint32_t start = timer_ticks();
    uint16_t word = (uint16_t)(((uint32_t)mode << MCASIC_MOTOR_MODE_SHIFT) | (pwm & MCASIC_MOTOR_PWM_MASK));
    int rc = -2;
    motor_stop_internal();
    enc_update();
    stall_ref_pos = enc_pos[m];
    stall_ref_ticks = g_hold_begin_ticks = timer_ticks();
    if (motor_write(motor_regs[m], word) != MICCI_OK) { motor_all_off(); return -1; }
    while (timer_elapsed_ms(start) < max_ms) {
        enc_update();
        if (stall_check(m)) { rc = 0; break; }
        watchdog_kick();
        timer_delay_ms(2);
    }
    motor_all_off();
    timer_delay_ms(150);                    /* let it settle */
    enc_update();
    return rc;
}

static int motor_ensure_init(void)
{
    return g_motor_inited ? 0 : motor_init_hp_state();
}

/* Drive towards an end stop and call that position 0. */
int motor_api_home(int m, int mode, uint32_t pwm)
{
    int rc;
    if (!motor_has_encoder(m) || (mode != 1 && mode != 2)) return -1;
    if (pwm == 0) pwm = HOME_PWM;
    if (pwm > MOTOR_MAX_PWM) pwm = MOTOR_MAX_PWM;
    if (motor_ensure_init()) return -1;
    g_stall_motor = -1;
    rc = drive_until_stall(m, mode, pwm, HOME_MAX_MS);
    if (rc == 0) {
        enc_pos[m] = 0;
        uart_printf("motor %c: home (end stop, %s) is position 0\n", 'a' + m, mode == 1 ? "fwd" : "rev");
    } else {
        uart_printf("motor %c: no end stop within %u ms (rc %d)\n", 'a' + m, HOME_MAX_MS, rc);
    }
    return rc;
}

/* Home at the `mode` end, then run to the other end: the travel in counts. */
int motor_api_range(int m, int mode, uint32_t pwm, int32_t *span)
{
    int rc = motor_api_home(m, mode, pwm);
    if (rc) return rc;
    rc = drive_until_stall(m, mode == 1 ? 2 : 1, pwm ? pwm : HOME_PWM, HOME_MAX_MS);
    *span = enc_pos[m];
    uart_printf("motor %c: travel %d counts between the end stops (now at the %s end)\n", 'a' + m, (int)*span,
                mode == 1 ? "rev" : "fwd");
    return rc;
}

/* Closed loop on the encoder. These DC motors coast a long way (the carriage
   overshot by ~190 counts after switching off at PWM 260), so: run at `pwm`
   until GOTO_NEAR counts before the target, then short pulses whose length
   follows how far the previous pulse moved (counts per ms of drive). */
#define GOTO_TOL      30
#define GOTO_NEAR     1200
#define GOTO_PULSE_PWM 260U
static int goto_pulse(int m, int mode, uint32_t p, uint32_t ms)
{
    uint32_t t = timer_ticks();
    uint16_t word = (uint16_t)(((uint32_t)mode << MCASIC_MOTOR_MODE_SHIFT) | (p & MCASIC_MOTOR_PWM_MASK));
    if (motor_write(motor_regs[m], word) != MICCI_OK) { motor_all_off(); return -1; }
    while (timer_elapsed_ms(t) < ms) { enc_update(); watchdog_kick(); }
    motor_all_off();
    timer_delay_ms(100);                    /* coast to a stop */
    enc_update();
    return 0;
}

int motor_api_goto(int m, int32_t target, uint32_t pwm)
{
    uint32_t start = timer_ticks(), gain = 20U;    /* counts per ms of pulse, learnt below */
    int32_t err;
    int tries;
    if (!motor_has_encoder(m)) return -1;
    if (pwm == 0) pwm = 350U;
    if (pwm > MOTOR_MAX_PWM) pwm = MOTOR_MAX_PWM;
    if (motor_ensure_init()) return -1;
    motor_stop_internal();
    g_stall_motor = -1;
    enc_update();
    err = target - enc_pos[m];
    if (iabs32(err) > GOTO_NEAR) {                  /* the long run */
        int dir = err > 0 ? 1 : -1, mode = dir == motor_fwd_sign[m] ? 1 : 2;
        uint16_t word = (uint16_t)(((uint32_t)mode << MCASIC_MOTOR_MODE_SHIFT) | (pwm & MCASIC_MOTOR_PWM_MASK));
        stall_ref_pos = enc_pos[m];
        stall_ref_ticks = g_hold_begin_ticks = timer_ticks();
        if (motor_write(motor_regs[m], word) != MICCI_OK) { motor_all_off(); return -1; }
        while (timer_elapsed_ms(start) < HOME_MAX_MS) {
            enc_update();
            err = target - enc_pos[m];
            if ((dir > 0 && err <= GOTO_NEAR) || (dir < 0 && err >= -GOTO_NEAR)) break;
            if (stall_check(m)) {
                motor_all_off();
                uart_printf("motor %c: blocked at %d on the way to %d\n", 'a' + m, (int)enc_pos[m], (int)target);
                return -3;
            }
            watchdog_kick();
        }
        motor_all_off();
        timer_delay_ms(150);
        enc_update();
    }
    for (tries = 0; tries < 12 && timer_elapsed_ms(start) < HOME_MAX_MS; tries++) {
        int32_t before = enc_pos[m], moved;
        uint32_t ms;
        int mode;
        err = target - enc_pos[m];
        if (iabs32(err) <= GOTO_TOL) break;
        mode = (err > 0 ? 1 : -1) == motor_fwd_sign[m] ? 1 : 2;
        ms = (uint32_t)iabs32(err) / gain;
        if (ms < 2U) ms = 2U;
        if (ms > 60U) ms = 60U;
        if (goto_pulse(m, mode, GOTO_PULSE_PWM, ms)) return -1;
        moved = iabs32(enc_pos[m] - before);
        if (moved < 3) gain = gain > 4U ? gain / 2U : 2U;        /* did not break free: longer next time */
        else {
            gain = (uint32_t)moved / ms;
            if (gain < 2U) gain = 2U;
            if (gain > 200U) gain = 200U;
        }
    }
    enc_update();
    uart_printf("motor %c: at %d (target %d, %u ms, %d correction pulses)\n", 'a' + m, (int)enc_pos[m],
                (int)target, timer_elapsed_ms(start), tries);
    return iabs32(target - enc_pos[m]) <= GOTO_TOL * 3 ? 0 : -2;
}

int32_t motor_api_position(int m) { enc_update(); return (m >= 0 && m < 3) ? enc_pos[m] : 0; }

int cmd_motor(int argc, char **argv)
{
    uint32_t pwm, ms, start, cnt_before[3], cnt_after[3];
    int index, mode, rc;
    uint16_t word;
    if (argc == 1 || (argc == 2 && !strcmp(argv[1], "show"))) { motor_show(); return 0; }
    if (argc == 2 && !strcmp(argv[1], "off")) { motor_all_off(); motor_show(); return 0; }
    if (argc == 2 && !strcmp(argv[1], "init")) return motor_init_hp_state();
    if (argc == 2 && !strcmp(argv[1], "pos")) {
        enc_update();
        uart_printf("motor positions (encoder counts): b %d, c %d; a has no encoder\n", (int)enc_pos[1], (int)enc_pos[2]);
        if (g_stall_motor >= 0)
            uart_printf("last stall: motor %c %s at %d\n", 'a' + g_stall_motor, g_stall_mode == 1 ? "fwd" : "rev",
                        (int)g_stall_pos);
        return 0;
    }
    if (argc >= 3 && (!strcmp(argv[1], "home") || !strcmp(argv[1], "range") || !strcmp(argv[1], "goto") ||
                      !strcmp(argv[1], "zero"))) {
        int m = !strcmp(argv[2], "b") ? 1 : !strcmp(argv[2], "c") ? 2 : -1;
        uint32_t p = 0;
        if (m < 0) return usage("motor home|range|goto|zero <b|c> ... (motor a has no encoder)");
        if (!strcmp(argv[1], "zero")) { enc_update(); enc_pos[m] = 0; uart_printf("motor %c: position 0\n", 'a' + m); return 0; }
        if (!strcmp(argv[1], "goto")) {
            uint32_t mag;
            int neg = argc >= 4 && argv[3][0] == '-';
            if (argc < 4 || parse_u32(argv[3] + neg, &mag) || (argc >= 5 && parse_u32(argv[4], &p)))
                return usage("motor goto <b|c> <position> [pwm]");
            return motor_api_goto(m, neg ? -(int32_t)mag : (int32_t)mag, p);
        }
        {
            /* home/range: default end = fwd for the carriage (left, count 0 side), rev for the scanner */
            int mode = m == 1 ? 1 : 2;
            if (argc >= 4) mode = !strcmp(argv[3], "fwd") ? 1 : !strcmp(argv[3], "rev") ? 2 : 0;
            if (!mode || (argc >= 5 && parse_u32(argv[4], &p)))
                return usage("motor home|range <b|c> [fwd|rev] [pwm]");
            if (!strcmp(argv[1], "home")) return motor_api_home(m, mode, p);
            {
                int32_t span = 0;
                return motor_api_range(m, mode, p, &span);
            }
        }
    }
    if (argc != 6 || strcmp(argv[1], "pulse"))
        return usage("motor [show] | motor off | motor init | motor pulse <a|b|c> <fwd|rev> <pwm 1..600> <ms 1..250>");
    index = !strcmp(argv[2], "a") ? 0 : !strcmp(argv[2], "b") ? 1 : !strcmp(argv[2], "c") ? 2 : -1;
    mode = !strcmp(argv[3], "fwd") ? 1 : !strcmp(argv[3], "rev") ? 2 : 0;
    if (index < 0 || !mode || parse_u32(argv[4], &pwm) || parse_u32(argv[5], &ms) ||
        pwm == 0 || pwm > MOTOR_MAX_PWM || ms == 0 || ms > MOTOR_MAX_MS)
        return usage("motor pulse <a|b|c> <fwd|rev> <pwm 1..600> <ms 1..250>");
    word = (uint16_t)(((uint32_t)mode << MCASIC_MOTOR_MODE_SHIFT) | (pwm & MCASIC_MOTOR_PWM_MASK));
    motor_show();
    motor_read_counters(cnt_before);
    uart_printf("motor %s: register 0x%02x <- 0x%04x for %u ms\n", argv[2], motor_regs[index], word, ms);
    uart_flush();
    start = timer_ticks();
    rc = motor_write(motor_regs[index], word);
    if (rc == MICCI_OK) {
        /* Read the control word and the register after it (HP's health check
           reads that one) while the motor is powered: early and at the end. */
        uint16_t ctl1 = 0, st1 = 0, ctl2 = 0, st2 = 0;
        micci_read(MCASIC_CTRL, MCASIC_BUS, MCASIC_DEV, motor_regs[index], &ctl1);
        micci_read(MCASIC_CTRL, MCASIC_BUS, MCASIC_DEV, motor_regs[index] + 1, &st1);
        while (timer_elapsed_ms(start) < ms) { }
        micci_read(MCASIC_CTRL, MCASIC_BUS, MCASIC_DEV, motor_regs[index], &ctl2);
        micci_read(MCASIC_CTRL, MCASIC_BUS, MCASIC_DEV, motor_regs[index] + 1, &st2);
        motor_all_off();
        uart_printf("motor %s: while on: %02x=%04x %02x=%04x, at the end: %02x=%04x %02x=%04x\n", argv[2],
                    motor_regs[index], ctl1, motor_regs[index] + 1, st1,
                    motor_regs[index], ctl2, motor_regs[index] + 1, st2);
    } else {
        motor_all_off();
    }
    uart_printf("motor %s: %s, off again after %u ms\n", argv[2],
                rc == MICCI_OK ? "pulse sent" : "start write FAILED", timer_elapsed_ms(start));
    timer_delay_ms(150);                    /* let the mechanism coast to a stop */
    motor_read_counters(cnt_after);
    uart_printf("counters before/after: %08x/%08x %08x/%08x %08x/%08x\n",
                cnt_before[0], cnt_after[0], cnt_before[1], cnt_after[1], cnt_before[2], cnt_after[2]);
    motor_show();
    return rc == MICCI_OK ? 0 : -1;
}

/* ---------------------------------------------------------------------------
 * "motorsAndButtonsTest": interactive sub-shell linking front-panel button
 * holds directly to Motor A/B/C forward/reverse so the operator can test
 * mechanisms hands-on at the printer and exit from the UART console.
 * ------------------------------------------------------------------------- */
static uint32_t g_frame_lo, g_frame_hi;     /* last frame sent, reused by "buttons" */
static int g_frame_valid;

/* Debounce: a new button state is accepted only after two consecutive frames
   agree (one ~65 ms frame apart). Removes single-frame glitches. */
struct debounce { uint32_t stable, candidate; int seen; };

static int debounce_update(struct debounce *d, uint32_t pressed)
{
    if (pressed == d->stable) { d->candidate = pressed; d->seen = 0; return 0; }
    if (pressed != d->candidate) { d->candidate = pressed; d->seen = 1; return 0; }
    if (++d->seen < 2) return 0;
    d->stable = pressed;
    return 1;
}

#define MBT_DEFAULT_PWM      350U
#define MBT_DEFAULT_LIMIT_MS 8000U
#define MBT_MAX_LIMIT_MS     15000U
/* Shift-register input bits that are NOT wired to any button on the 2620 panel
   (bits 0, 4, 5, 10..17). On a clean frame all of these stay pulled high (1);
   frames where any of them read low (e.g. 0x2eeef) are shift-register glitches. */
#define MBT_UNUSED_HIGH_MASK 0x3FC31U

struct mbt_binding {
    int bit;
    const char *btn_name;
    int motor_idx;          /* 0 = A, 1 = B, 2 = C */
    char motor_letter;
    int mode;               /* 1 = fwd, 2 = rev */
    const char *dir_name;
    char lcd_ch;
    uint64_t lcd_ind;
};

static const struct mbt_binding mbt_bindings[] = {
    { 6, "cancel",     0, 'A', 1, "fwd", 'A', 1ULL << 8  }, /* ink_led */
    { 2, "resume",     0, 'A', 2, "rev", 'A', 1ULL << 9  }, /* resume_led */
    { 3, "wireless",   1, 'B', 1, "fwd", 'b', 1ULL << 12 }, /* wifi_icon */
    { 7, "info",       1, 'B', 2, "rev", 'b', 1ULL << 11 }, /* info_led */
    { 9, "copy_black", 2, 'C', 1, "fwd", 'C', 1ULL << 23 }, /* black_ink_outline */
    { 8, "copy_color", 2, 'C', 2, "rev", 'C', 1ULL << 19 }, /* color_ink_outline */
};
#define MBT_BINDING_COUNT (sizeof(mbt_bindings) / sizeof(mbt_bindings[0]))

static void mbt_print_help(uint32_t pwm, uint32_t limit_ms)
{
    uart_printf("motorsAndButtonsTest: hold a front-panel button to drive a motor (release = stop)\n"
                "  cancel     (bit 6) -> motor A fwd (reg 0x31, LCD 'A' + ink LED)\n"
                "  resume     (bit 2) -> motor A rev (reg 0x31, LCD 'A' + resume LED)\n"
                "  wireless   (bit 3) -> motor B fwd (reg 0x33, LCD 'b' + wifi icon)\n"
                "  info       (bit 7) -> motor B rev (reg 0x33, LCD 'b' + info LED)\n"
                "  copy_black (bit 9) -> motor C fwd (reg 0x35, LCD 'C' + black ink icon)\n"
                "  copy_color (bit 8) -> motor C rev (reg 0x35, LCD 'C' + color ink icon)\n"
                "  power      (bit 1) -> cycle PWM preset (250 / 350 / 450 / 550)\n"
                "current: pwm %u (1..%u), max hold %u ms\n"
                "sub-commands: pwm <1..%u> | limit <100..%u> | a|b|c fwd|rev [ms] | init | status | help | exit\n",
                pwm, MOTOR_MAX_PWM, limit_ms, MOTOR_MAX_PWM, MBT_MAX_LIMIT_MS);
}

static void mbt_print_Telemetry(uint8_t status_reg)
{
    uint16_t st = 0, gpio3b = 0, e2a = 0, e2b = 0, e2c = 0, e2d = 0;
    micci_read(MCASIC_CTRL, MCASIC_BUS, MCASIC_DEV, status_reg, &st);
    micci_read(MCASIC_CTRL, MCASIC_BUS, MCASIC_DEV, 0x3B, &gpio3b);
    micci_read(MCASIC_CTRL, MCASIC_BUS, MCASIC_DEV, 0x2A, &e2a);
    micci_read(MCASIC_CTRL, MCASIC_BUS, MCASIC_DEV, 0x2B, &e2b);
    micci_read(MCASIC_CTRL, MCASIC_BUS, MCASIC_DEV, 0x2C, &e2c);
    micci_read(MCASIC_CTRL, MCASIC_BUS, MCASIC_DEV, 0x2D, &e2d);
    uart_printf("status %02x=%04x gpio3b=%04x enc=%04x/%04x/%04x/%04x\n",
                status_reg, st, gpio3b, e2a, e2b, e2c, e2d);
}

static int mbt_idle_bars(uint32_t pwm, char *ch_out)
{
    if (pwm <= 175) { *ch_out = '1'; return 1; }
    if (pwm <= 275) { *ch_out = '2'; return 2; }
    if (pwm <= 375) { *ch_out = '3'; return 3; }
    *ch_out = '4';
    return 4;
}

static void mbt_reprompt(const char *line, int len)
{
    int i;
    uart_puts("mbt> ");
    for (i = 0; i < len; i++) uart_putc(line[i]);
}

/* Returns 1 to exit the sub-shell, 0 to stay. */
static int mbt_exec_line(char *line, uint32_t *pwm, uint32_t *limit_ms)
{
    char *argv[8];
    int argc = 0;
    char *p = line;
    while (*p) {
        while (*p == ' ' || *p == '\t') *p++ = 0;
        if (!*p) break;
        if (argc < 8) argv[argc++] = p;
        while (*p && *p != ' ' && *p != '\t') p++;
    }
    if (argc == 0) return 0;
    if (!strcmp(argv[0], "exit") || !strcmp(argv[0], "quit") || !strcmp(argv[0], "q"))
        return 1;
    if (!strcmp(argv[0], "help") || !strcmp(argv[0], "?")) {
        mbt_print_help(*pwm, *limit_ms);
        return 0;
    }
    if (!strcmp(argv[0], "init")) {
        motor_init_hp_state();
        return 0;
    }
    if (!strcmp(argv[0], "status") || !strcmp(argv[0], "show")) {
        uart_printf("pwm %u, hold limit %u ms; ", *pwm, *limit_ms);
        mbt_print_Telemetry(0x32);
        motor_show();
        return 0;
    }
    if (!strcmp(argv[0], "pwm")) {
        uint32_t v = 0;
        if (argc != 2 || parse_u32(argv[1], &v) || v == 0 || v > MOTOR_MAX_PWM) {
            uart_printf("usage: pwm <1..%u>\n", MOTOR_MAX_PWM);
            return 0;
        }
        *pwm = v;
        uart_printf("[mbt] pwm set to %u\n", *pwm);
        return 0;
    }
    if (!strcmp(argv[0], "limit")) {
        uint32_t v = 0;
        if (argc != 2 || parse_u32(argv[1], &v) || v < 100 || v > MBT_MAX_LIMIT_MS) {
            uart_printf("usage: limit <100..%u>\n", MBT_MAX_LIMIT_MS);
            return 0;
        }
        *limit_ms = v;
        uart_printf("[mbt] hold limit set to %u ms\n", *limit_ms);
        return 0;
    }
    if ((!strcmp(argv[0], "a") || !strcmp(argv[0], "b") || !strcmp(argv[0], "c")) && argc >= 2) {
        int idx = argv[0][0] - 'a';
        int mode = !strcmp(argv[1], "fwd") ? 1 : !strcmp(argv[1], "rev") ? 2 : 0;
        uint32_t ms = 100, start;
        uint16_t word;
        if (!mode || (argc >= 3 && (parse_u32(argv[2], &ms) || ms == 0 || ms > *limit_ms))) {
            uart_printf("usage: %s <fwd|rev> [ms 1..%u]\n", argv[0], *limit_ms);
            return 0;
        }
        word = (uint16_t)(((uint32_t)mode << MCASIC_MOTOR_MODE_SHIFT) | (*pwm & MCASIC_MOTOR_PWM_MASK));
        uart_printf("[mbt] pulse motor %c %s: reg 0x%02x <- 0x%04x (pwm %u) for %u ms\n",
                    'A' + idx, argv[1], motor_regs[idx], word, *pwm, ms);
        start = timer_ticks();
        motor_write(motor_regs[idx], word);
        while (timer_elapsed_ms(start) < ms) watchdog_kick();
        motor_all_off();
        uart_printf("[mbt] motor %c off after %u ms; ", 'A' + idx, timer_elapsed_ms(start));
        mbt_print_Telemetry((uint8_t)(motor_regs[idx] + 1));
        return 0;
    }
    uart_printf("unknown sub-command: %s (type help or exit)\n", argv[0]);
    return 0;
}

int cmd_motors_and_buttons_test(int argc, char **argv)
{
    uint32_t pwm = MBT_DEFAULT_PWM, limit_ms = MBT_DEFAULT_LIMIT_MS;
    uint32_t hold_start = 0, last_pressed = 0;
    struct debounce db = { 0, 0, 0 };
    int active_binding = -1, cutoff_latched = 0, line_len = 0, exit_req = 0;
    char line[CLI_LINE_MAX + 1];

    if (argc >= 2 && (parse_u32(argv[1], &pwm) || pwm == 0 || pwm > MOTOR_MAX_PWM))
        return usage("motorsAndButtonsTest [pwm 1..600]");

    ui_hold();
    motor_init_hp_state();
    mbt_print_help(pwm, limit_ms);
    mbt_reprompt(line, 0);

    while (!exit_req) {
        char lcd_ch = ' ';
        int bars = mbt_idle_bars(pwm, &lcd_ch), verified = 0;
        uint64_t lcd_ind = 0;
        uint32_t lo = 0, hi = 0, raw, pressed;
        int match = -1, match_count = 0;
        unsigned i;

        if (active_binding >= 0) {
            lcd_ch = mbt_bindings[active_binding].lcd_ch;
            lcd_ind = mbt_bindings[active_binding].lcd_ind;
        }
        panel_encode(lcd_ch, bars, 0, 0, lcd_ind, &lo, &hi);
        raw = panel_shift(lo, hi, &verified);
        /* Ignore single-frame shift-register glitches where unused pull-up bits drop low. */
        if (verified && ((raw & MBT_UNUSED_HIGH_MASK) == MBT_UNUSED_HIGH_MASK)) {
            debounce_update(&db, (~raw) & PANEL_BUTTON_MASK);
        } else if (!verified) {
            db.stable = 0; db.candidate = 0; db.seen = 0;
        }
        pressed = db.stable;

        /* Count how many motor-mapped buttons are held right now. */
        for (i = 0; i < MBT_BINDING_COUNT; i++) {
            if (pressed & (1U << mbt_bindings[i].bit)) {
                match = (int)i;
                match_count++;
            }
        }

        /* Power button (bit 1) tap cycles PWM presets when no motor button is held. */
        if (match_count == 0 && (pressed & (1U << 1)) && !(last_pressed & (1U << 1))) {
            static const uint32_t presets[4] = { 250, 350, 450, 550 };
            unsigned p = 0;
            for (p = 0; p < 4; p++) {
                if (pwm <= presets[p]) { pwm = presets[(p + 1) % 4]; break; }
            }
            if (p == 4) pwm = presets[0];
            uart_printf("\r\n[mbt] power button: pwm -> %u\n", pwm);
            mbt_reprompt(line, line_len);
        }

        /* Report any newly pressed unmapped button bits so unknown buttons are visible. */
        if (pressed != last_pressed) {
            uint32_t newly = pressed & ~last_pressed;
            uint32_t known_mask = (1U << 1);
            int bit;
            for (i = 0; i < MBT_BINDING_COUNT; i++) known_mask |= (1U << mbt_bindings[i].bit);
            for (bit = 0; bit < 18; bit++) {
                if ((newly & (1U << bit)) && !(known_mask & (1U << bit))) {
                    uart_printf("\r\n[mbt] unmapped button bit %d pressed (raw 0x%05x)\n",
                                bit, raw & PANEL_BUTTON_MASK);
                    mbt_reprompt(line, line_len);
                }
            }
        }

        if (match_count == 0) {
            if (active_binding >= 0) {
                const struct mbt_binding *b = &mbt_bindings[active_binding];
                uint32_t held_ms = timer_elapsed_ms(hold_start);
                motor_all_off();
                uart_printf("\r\n[mbt] RELEASE %s: motor %c %s off after %u ms; ",
                            b->btn_name, b->motor_letter, b->dir_name, held_ms);
                mbt_print_Telemetry((uint8_t)(motor_regs[b->motor_idx] + 1));
                active_binding = -1;
                mbt_reprompt(line, line_len);
            }
            cutoff_latched = 0;
        } else if (match_count > 1) {
            if (active_binding >= 0 || !cutoff_latched) {
                motor_all_off();
                active_binding = -1;
                cutoff_latched = 1;
                uart_puts("\r\n[mbt] multiple buttons held: motors stopped\n");
                mbt_reprompt(line, line_len);
            }
        } else if (!cutoff_latched) {
            const struct mbt_binding *b = &mbt_bindings[match];
            uint16_t word = (uint16_t)(((uint32_t)b->mode << MCASIC_MOTOR_MODE_SHIFT) |
                                       (pwm & MCASIC_MOTOR_PWM_MASK));
            if (active_binding != match) {
                if (active_binding >= 0) motor_all_off();
                active_binding = match;
                hold_start = timer_ticks();
                motor_write(motor_regs[b->motor_idx], word);
                uart_printf("\r\n[mbt] HOLD %s (bit %d): motor %c %s (reg 0x%02x <- 0x%04x, pwm %u)\n",
                            b->btn_name, b->bit, b->motor_letter, b->dir_name,
                            motor_regs[b->motor_idx], word, pwm);
                mbt_reprompt(line, line_len);
            } else if (timer_elapsed_ms(hold_start) >= limit_ms) {
                motor_all_off();
                active_binding = -1;
                cutoff_latched = 1;
                uart_printf("\r\n[mbt] hold limit (%u ms) reached: motor %c stopped (release button to re-arm)\n",
                            limit_ms, b->motor_letter);
                mbt_reprompt(line, line_len);
            } else {
                /* Refresh the motor control word while held. */
                motor_write(motor_regs[b->motor_idx], word);
            }
        }
        last_pressed = pressed;

        /* Process UART input for the sub-command line. */
        for (;;) {
            int c = uart_getc_nonblock();
            if (c < 0) break;
            if (c == 0x03) {            /* Ctrl-C exits immediately */
                uart_puts("^C\n");
                exit_req = 1;
                break;
            }
            if (c == '\r' || c == '\n') {
                if (c == '\n' && line_len == 0) continue;
                uart_puts("\n");
                line[line_len] = 0;
                line_len = 0;
                if (mbt_exec_line(line, &pwm, &limit_ms)) {
                    exit_req = 1;
                    break;
                }
                mbt_reprompt(line, 0);
                continue;
            }
            if (c == 0x08 || c == 0x7F) {
                if (line_len > 0) { line_len--; uart_puts("\b \b"); }
                continue;
            }
            if (c >= 0x20 && c < 0x7F && line_len < CLI_LINE_MAX) {
                line[line_len++] = (char)c;
                uart_putc((char)c);
            }
        }
        watchdog_kick();
        timer_poll();
    }

    motor_all_off();
    panel_encode(' ', 0, 0, 0, 0, &g_frame_lo, &g_frame_hi);
    g_frame_valid = 1;
    panel_shift(g_frame_lo, g_frame_hi, 0);
    uart_puts("motorsAndButtonsTest: all motors off, exiting\n");
    return 0;
}

int cmd_asic(int argc, char **argv)
{
    uint16_t id = 0, status = 0, cid = 0, neo = 0, pen = 0;
    (void)argc; (void)argv;
    if (micci_read(MCASIC_CTRL, MCASIC_BUS, MCASIC_DEV, 0x00, &id) != MICCI_OK) { uart_puts("mcasic: no response\n"); return -1; }
    micci_read(MCASIC_CTRL, MCASIC_BUS, MCASIC_DEV, 0x01, &status);
    uart_printf("mcasic   id 0x%04x (expect 0x8014) status 0x%04x\n", id, status);
    if (micci_read(CARRIAGE_CTRL, CARRIAGE_BUS, CARRIAGE_DEV, 0x7D, &cid) != MICCI_OK) { uart_puts("carriage asic: no response\n"); return -1; }
    micci_read(CARRIAGE_CTRL, CARRIAGE_BUS, CARRIAGE_DEV, 0x00, &neo);
    micci_read(CARRIAGE_CTRL, CARRIAGE_BUS, CARRIAGE_DEV, 0x09, &pen);
    uart_printf("carriage id 0x%04x (expect 0x0801) status 0x%04x pen 0x%04x\n", cid, neo, pen);
    return 0;
}

int cmd_adc(int argc, char **argv)
{
    uint32_t sel, mode = 3;
    uint16_t sample;
    if (argc < 2 || parse_u32(argv[1], &sel) || sel > 0xFF || (argc >= 3 && parse_u32(argv[2], &mode)) || mode > 0x7F)
        return usage("adc <selector> [mode]   (15 = internal reference ~512, 0 = 24V supply)");
    int rc = mcasic_adc((uint8_t)sel, (uint8_t)mode, &sample);
    if (rc != MICCI_OK) { uart_printf("adc: %s\n", rc == MICCI_ERR_BUSY ? "bus busy" : "timeout"); return -1; }
    uart_printf("adc selector %u mode %u: %u/1023", sel, mode, sample);
    if (sel == 0) uart_printf(" = %u mV supply (28.05 V full scale)", (sample * MCASIC_ADC_FULL_SCALE_MV) / 1023U);
    if (sel == 15) uart_printf(" (reference, expect 465..543)");
    uart_puts("\n");
    return 0;
}

/* The frame "buttons" re-sends: the last lcd frame, or a blank display. */
static void current_frame(uint32_t *lo, uint32_t *hi)
{
    if (!g_frame_valid) {
        panel_encode(' ', 0, 0, 0, 0, &g_frame_lo, &g_frame_hi);
        g_frame_valid = 1;
    }
    *lo = g_frame_lo;
    *hi = g_frame_hi;
}

static uint32_t exchange(uint32_t lo, uint32_t hi, int report)
{
    int verified = 0;
    uint32_t raw = panel_shift(lo, hi, &verified);
    if (report || !verified)
        uart_printf("panel frame %08x%08x %s\n", hi, lo,
                    verified ? "acknowledged (signature 0x35 verified)" : "NOT acknowledged by the panel");
    return raw;
}

static int lcd_walk(int argc, char **argv);

int cmd_lcd(int argc, char **argv)
{
    uint32_t wifi = 0, color = 0, black = 0, lo, hi;
    uint64_t extra = 0;
    int i;
    ui_hold();                                  /* the panel menu stays off the display */
    if (argc >= 2 && !strcmp(argv[1], "walk"))
        return lcd_walk(argc, argv);
    if (argc < 2 || strlen(argv[1]) != 1 || (argc >= 3 && parse_u32(argv[2], &wifi)) ||
        (argc >= 4 && parse_u32(argv[3], &color)) || (argc >= 5 && parse_u32(argv[4], &black)))
        return usage("lcd <char> [wifi 0-5] [color 0-3] [black 0-3] [indicator ...]  (chars: 0-9 A b C d E F J P U n - space)");
    for (i = 5; i < argc; i++) {
        int bit = panel_indicator_bit(argv[i]);
        if (bit < 0) { uart_printf("unknown indicator %s\n", argv[i]); return -1; }
        extra |= 1ULL << bit;
    }
    if (panel_encode(argv[1][0], (int)wifi, (int)color, (int)black, extra, &lo, &hi) != 0)
        return usage("lcd <char> [wifi 0-5] [color 0-3] [black 0-3] [indicator ...]");
    g_frame_lo = lo; g_frame_hi = hi; g_frame_valid = 1;
    panel_print_buttons(exchange(lo, hi, 1));
    return 0;
}

/*
 * "lcd walk": light one logical panel bit at a time so the owner can see
 * which element each bit drives. While a bit is shown, the digit blinks its
 * number (tens, short gap, units, long gap). The seven digit segments come
 * last, alone and steady, in the order a..g. Any panel button steps forward
 * and cancel steps back; over the UART n or space = next, b = back and
 * q, Esc or Ctrl-C = stop.
 */
#define WALK_TENS_END_MS   700U
#define WALK_UNITS_START_MS 950U
#define WALK_UNITS_END_MS  1650U
#define WALK_CYCLE_MS      2650U
#define WALK_IDLE_MS       (20U * 60U * 1000U)
#define WALK_BUTTON_CANCEL 6

static const uint8_t walk_segment_bits[7] = { 39, 38, 37, 36, 33, 35, 34 };   /* a..g (SEG_A..SEG_G) */
static const char *const walk_segment_names[7] = {
    "top", "top-right", "bottom-right", "bottom", "bottom-left", "top-left", "middle"
};

static int walk_segment_index(int bit)
{
    int i;
    for (i = 0; i < 7; i++)
        if (walk_segment_bits[i] == bit) return i;
    return -1;
}

static char walk_digit(int bit, uint32_t phase_ms)
{
    if (phase_ms < WALK_TENS_END_MS) return (char)('0' + bit / 10);
    if (phase_ms >= WALK_UNITS_START_MS && phase_ms < WALK_UNITS_END_MS) return (char)('0' + bit % 10);
    return ' ';
}

static void walk_announce(int step, int steps, int bit)
{
    int seg = walk_segment_index(bit);
    const char *name = panel_indicator_name(bit);
    if (seg >= 0)
        uart_printf("step %d/%d: bit %d = digit segment %c (%s), lit alone\n",
                    step + 1, steps, bit, 'a' + seg, walk_segment_names[seg]);
    else
        uart_printf("step %d/%d: bit %d (%s), the digit blinks %d %d\n",
                    step + 1, steps, bit, name ? name : "unnamed", bit / 10, bit % 10);
}

static int lcd_walk(int argc, char **argv)
{
    uint8_t steps[64];
    uint32_t first = 8, last = 47, step_start, idle_start, lo, hi, held = 0;
    struct debounce db = { 0, 0, 0 };
    int count = 0, step = 0, bit, i;

    if ((argc != 2 && argc != 4) ||
        (argc == 4 && (parse_u32(argv[2], &first) || parse_u32(argv[3], &last) || first > last || last > 63)))
        return usage("lcd walk [first last]  (bits 0..63, default 8 47; digit segments are shown last)");
    for (bit = (int)first; bit <= (int)last; bit++)
        if (walk_segment_index(bit) < 0) steps[count++] = (uint8_t)bit;
    for (i = 0; i < 7; i++)
        if (walk_segment_bits[i] >= first && walk_segment_bits[i] <= last) steps[count++] = walk_segment_bits[i];
    uart_printf("lcd walk: %d steps, one panel bit at a time; the digit blinks the bit number\n"
                "next: any panel button (or n / space here), back: cancel (or b), stop: q, Esc or Ctrl-C\n", count);
    step_start = idle_start = timer_ticks();
    walk_announce(step, count, steps[step]);
    for (;;) {
        int c, move = 0, seg;
        uint32_t raw;
        bit = steps[step];
        seg = walk_segment_index(bit);
        panel_encode(seg >= 0 ? ' ' : walk_digit(bit, timer_elapsed_ms(step_start) % WALK_CYCLE_MS),
                     0, 0, 0, 1ULL << bit, &lo, &hi);
        raw = exchange(lo, hi, 0);
        if (debounce_update(&db, (~raw) & PANEL_BUTTON_MASK)) {
            /* A step happens when a button goes down after all were released. */
            if (db.stable && !held)
                move = (db.stable & (1U << WALK_BUTTON_CANCEL)) ? -1 : 1;
            held = db.stable;
        }
        c = uart_getc_nonblock();
        if (c == 'q' || c == 'Q' || c == 27 || c == 3) { uart_puts("walk stopped\n"); break; }
        if (c == 'n' || c == 'N' || c == ' ') move = 1;
        if (c == 'b' || c == 'B') move = -1;
        if (move) {
            idle_start = timer_ticks();
            if (move < 0 && step == 0) { uart_puts("already at the first step\n"); continue; }
            step += move;
            if (step >= count) { uart_puts("walk done\n"); break; }
            step_start = timer_ticks();
            walk_announce(step, count, steps[step]);
        } else if (timer_elapsed_ms(idle_start) >= WALK_IDLE_MS) {
            uart_puts("walk stopped: no button for 20 minutes\n");
            break;
        }
    }
    panel_encode(' ', 0, 0, 0, 0, &g_frame_lo, &g_frame_hi);
    g_frame_valid = 1;
    exchange(g_frame_lo, g_frame_hi, 0);
    return 0;
}

/* Report every button change for `seconds`; the display digit counts down. */
static int buttons_watch(uint32_t seconds)
{
    uint32_t start = timer_ticks(), last_pressed = 0, presses = 0;
    struct debounce db = { 0, 0, 0 };
    int shown = -1;
    uart_printf("watching the buttons for %u s; the panel digit shows the seconds left\n", seconds);
    for (;;) {
        uint32_t elapsed = timer_elapsed_ms(start), lo, hi, raw, pressed;
        int left;
        if (elapsed >= seconds * 1000U) break;
        left = (int)(seconds - elapsed / 1000U);
        if (left != shown) {
            panel_encode((char)('0' + left % 10), 0, 0, 0, 0, &g_frame_lo, &g_frame_hi);
            g_frame_valid = 1;
            shown = left;
        }
        lo = g_frame_lo; hi = g_frame_hi;
        raw = exchange(lo, hi, 0);
        if (!debounce_update(&db, (~raw) & PANEL_BUTTON_MASK)) {
            timer_delay_ms(20);
            continue;
        }
        pressed = db.stable;
        if (pressed != last_pressed) {
            uint32_t changed = pressed ^ last_pressed;
            int bit;
            for (bit = 0; bit < 18; bit++) {
                if (!(changed & (1U << bit))) continue;
                const char *name = panel_button_name(bit);
                uart_printf("[%3u.%03u] %-10s %s\n", elapsed / 1000U, elapsed % 1000U,
                            name ? name : "unnamed", (pressed >> bit) & 1U ? "pressed" : "released");
                if ((pressed >> bit) & 1U) presses++;
            }
            last_pressed = pressed;
        }
        timer_delay_ms(20);
    }
    uart_printf("done, %u press%s seen\n", presses, presses == 1 ? "" : "es");
    return 0;
}

int cmd_buttons(int argc, char **argv)
{
    uint32_t lo, hi;
    ui_hold();                                  /* the panel menu would act on the presses */
    current_frame(&lo, &hi);
    if (argc >= 2 && !strcmp(argv[1], "watch")) {
        uint32_t seconds = 30;
        if (argc >= 3 && (parse_u32(argv[2], &seconds) || seconds == 0 || seconds > 600))
            return usage("buttons watch [seconds 1..600]");
        return buttons_watch(seconds);
    }
    if (argc >= 2 && !strcmp(argv[1], "wait")) {
        uint32_t timeout = 10000, start = timer_ticks();
        if (argc >= 3 && parse_u32(argv[2], &timeout)) return usage("buttons wait [timeout_ms]");
        struct debounce db = { 0, 0, 0 };
        uart_puts("waiting for a button...\n");
        for (;;) {
            uint32_t raw = exchange(lo, hi, 0);
            if (debounce_update(&db, (~raw) & PANEL_BUTTON_MASK) && db.stable) {
                panel_print_buttons(raw);
                return 0;
            }
            if (timer_elapsed_ms(start) >= timeout) { uart_puts("timeout, nothing pressed\n"); return -1; }
            timer_delay_ms(20);
        }
    }
    panel_print_buttons(exchange(lo, hi, 1));
    return 0;
}

int cmd_memtest(int argc, char **argv)
{
    uint32_t addr = 0x20800000U, len = 0x00100000U, bad, first = 0, start;
    if ((argc >= 2 && parse_u32(argv[1], &addr)) || (argc >= 3 && parse_u32(argv[2], &len)) || (addr & 3) || (len & 3))
        return usage("memtest [addr] [len]   (default 1 MiB at 0x20800000; destroys the range)");
    if (((addr & 0xFC000000U) != 0x20000000U && (addr & 0xFC000000U) != 0x60000000U) ||
        (addr & 0x03FFFFFFU) < DRAM_FREE_OFFSET || (addr & 0x03FFFFFFU) + len > DRAM_ALIAS_SIZE ||
        (addr & 0x03FFFFFFU) + len < (addr & 0x03FFFFFFU)) {
        uart_printf("refusing: test only the writable DRAM aliases 0x20... / 0x60... above offset 0x%06x "
                    "(below it: HP's DTCM/data, bootloader, application and staging buffers)\n", DRAM_FREE_OFFSET);
        return -1;
    }
    start = timer_ticks();
    bad = memtest(addr, len, &first);
    uart_printf("memtest %08x..%08x: %u bad words", addr, addr + len, bad);
    if (bad) uart_printf(" (first at %08x)", first);
    uart_printf(", %u ms\n", timer_elapsed_ms(start));
    return bad ? -1 : 0;
}

/* --------------------------------------------------------------------------- */
/* Digital sensors. HP's lib_digital_sensor names them (table at 0x2056d190:
   DOOR_SENSOR, TRAY_SENSOR, TRAY_IR_SENSOR_S1..S3, TOF_OOPS = IDs 0..5). The
   pins below were found by flipping each GPIO input in the emulator and
   asking HP's own firmware "pe_digital_sensor.get <id>" (analysis/sensors.md).
   TRAY_SENSOR follows none of these pins; TOF_OOPS answers -1 (not fitted).
   GPIO 11 and 15 are further inputs HP reads; their meaning is unknown. */

static const struct { const char *name; int pin; int active_low; const char *hp; } sensor_pins[] = {
    { "door",       8, 1, "DOOR_SENSOR" },
    { "tray_ir_s1", 1, 0, "TRAY_IR_SENSOR_S1 (HP: small)" },
    { "tray_ir_s2", 2, 0, "TRAY_IR_SENSOR_S2 (HP: medium)" },
    { "tray_ir_s3", 3, 0, "TRAY_IR_SENSOR_S3 (HP: large)" },
    { "gpio11",    11, 0, "input, meaning unknown" },
    { "gpio15",    15, 0, "input, meaning unknown" },
};
#define SENSOR_COUNT (sizeof(sensor_pins) / sizeof(sensor_pins[0]))

static int sensor_active(unsigned i)
{
    int level = GPIO_INPUT(sensor_pins[i].pin) & 1;
    return sensor_pins[i].active_low ? !level : level;
}

int sensor_door_active(void) { return sensor_active(0); }
int sensor_api_count(void) { return (int)SENSOR_COUNT; }
const char *sensor_api_name(int i) { return (i >= 0 && i < (int)SENSOR_COUNT) ? sensor_pins[i].name : ""; }
int sensor_api_active(int i) { return (i >= 0 && i < (int)SENSOR_COUNT) ? sensor_active((unsigned)i) : 0; }

static uint32_t sensor_snapshot(void)
{
    uint32_t bits = 0;
    unsigned i;
    for (i = 0; i < SENSOR_COUNT; i++) if (sensor_active(i)) bits |= 1U << i;
    return bits;
}

int cmd_sensors(int argc, char **argv)
{
    unsigned i;
    uint16_t v = 0;
    if (argc >= 2 && !strcmp(argv[1], "watch")) {
        uint32_t seconds = 30, start = timer_ticks(), last = sensor_snapshot(), now, changes = 0;
        if (argc >= 3 && (parse_u32(argv[2], &seconds) || seconds == 0 || seconds > 600))
            return usage("sensors watch [seconds 1..600]");
        uart_printf("watching the sensors for %u s (open the door, slide paper in); press a key to stop\n", seconds);
        while (timer_elapsed_ms(start) < seconds * 1000U) {
            if (uart_getc_nonblock() >= 0) break;
            now = sensor_snapshot();
            if (now != last) {
                uint32_t t = timer_elapsed_ms(start);
                for (i = 0; i < SENSOR_COUNT; i++) {
                    if (!((now ^ last) & (1U << i))) continue;
                    uart_printf("[%3u.%03u] %-10s %s (gpio %d = %d)\n", t / 1000U, t % 1000U, sensor_pins[i].name,
                                (now >> i) & 1U ? "active" : "inactive", sensor_pins[i].pin,
                                GPIO_INPUT(sensor_pins[i].pin) & 1);
                    changes++;
                }
                last = now;
            }
            watchdog_kick();
            timer_delay_ms(20);
        }
        uart_printf("done, %u change%s\n", changes, changes == 1 ? "" : "s");
        return 0;
    }
    if (argc != 1) return usage("sensors [watch [seconds]]");
    for (i = 0; i < SENSOR_COUNT; i++)
        uart_printf("%-10s gpio %2d = %d -> %-8s  %s\n", sensor_pins[i].name, sensor_pins[i].pin,
                    GPIO_INPUT(sensor_pins[i].pin) & 1, sensor_active(i) ? "active" : "inactive", sensor_pins[i].hp);
    uart_printf("door: %s (inferred: HP's DOOR_SENSOR is active when the pin is low)\n",
                sensor_door_active() ? "open" : "closed");
    if (micci_read(MCASIC_CTRL, MCASIC_BUS, MCASIC_DEV, 0x3B, &v) == MICCI_OK)
        uart_printf("mcasic gpio inputs (reg 0x3b) = 0x%04x\n", v);
    return 0;
}

void motor_api_telemetry(struct motor_telemetry *t)
{
    unsigned i;
    uint32_t elapsed;
    memset(t, 0, sizeof(*t));
    motor_api_poll();
    t->inited = g_motor_inited;
    t->active_motor = g_active_motor;
    t->active_mode = g_active_mode;
    t->active_pwm = g_active_pwm;
    if (g_active_motor >= 0) {
        elapsed = timer_elapsed_ms(g_active_start_ticks);
        t->remaining_ms = elapsed < g_active_lease_ms ? (g_active_lease_ms - elapsed) : 0;
    }
    micci_read(MCASIC_CTRL, MCASIC_BUS, MCASIC_DEV, 0x00, &t->mcasic_id);
    micci_read(MCASIC_CTRL, MCASIC_BUS, MCASIC_DEV, 0x01, &t->mcasic_status);
    micci_read(MCASIC_CTRL, MCASIC_BUS, MCASIC_DEV, 0x20, &t->reg_20);
    micci_read(MCASIC_CTRL, MCASIC_BUS, MCASIC_DEV, 0x30, &t->reg_30);
    for (i = 0; i < 3; i++) {
        micci_read(MCASIC_CTRL, MCASIC_BUS, MCASIC_DEV, motor_regs[i], &t->motor_ctl[i]);
        micci_read(MCASIC_CTRL, MCASIC_BUS, MCASIC_DEV, motor_regs[i] + 1, &t->motor_st[i]);
    }
    micci_read(MCASIC_CTRL, MCASIC_BUS, MCASIC_DEV, 0x3B, &t->gpio_3b);
    for (i = 0; i < 4; i++)
        micci_read(MCASIC_CTRL, MCASIC_BUS, MCASIC_DEV, (int)(0x2AU + i), &t->enc[i]);
    motor_read_counters(t->pos_cnt);
    for (i = 0; i < 3; i++) t->pos[i] = enc_pos[i];
    t->stall_motor = g_stall_motor;
    t->sensor_bits = sensor_snapshot();
    t->door_open = sensor_door_active();
}

int panel_api_set(char ch, int wifi, int color, int black, uint64_t extra)
{
    uint32_t lo, hi;
    ui_hold();
    if (panel_encode(ch, wifi, color, black, extra, &lo, &hi) != 0)
        return -1;
    g_frame_lo = lo;
    g_frame_hi = hi;
    g_frame_valid = 1;
    exchange(lo, hi, 0);
    return 0;
}
