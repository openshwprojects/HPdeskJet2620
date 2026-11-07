#ifndef FW_PERIPH_CMDS_H
#define FW_PERIPH_CMDS_H

#include <stdint.h>

int cmd_i2c(int argc, char **argv);
int cmd_eeprom(int argc, char **argv);
int cmd_micci(int argc, char **argv);
int cmd_asic(int argc, char **argv);
int cmd_adc(int argc, char **argv);
int cmd_lcd(int argc, char **argv);
int cmd_buttons(int argc, char **argv);
int cmd_memtest(int argc, char **argv);
int cmd_motor(int argc, char **argv);
int cmd_motors_and_buttons_test(int argc, char **argv);
int cmd_sensors(int argc, char **argv);
/* Cartridge door as HP's DOOR_SENSOR reads it (GPIO 8 low = active). */
int sensor_door_active(void);

struct motor_telemetry {
    int inited;
    int active_motor;       /* -1 = none, 0 = A, 1 = B, 2 = C */
    int active_mode;        /* 0 = off, 1 = fwd, 2 = rev */
    uint32_t active_pwm;
    uint32_t remaining_ms;
    uint16_t mcasic_id;
    uint16_t mcasic_status;
    uint16_t reg_20, reg_30;
    uint16_t motor_ctl[3];  /* 0x31, 0x33, 0x35 */
    uint16_t motor_st[3];   /* 0x32, 0x34, 0x36 */
    uint16_t gpio_3b;
    uint16_t enc[4];        /* 0x2A..0x2D */
    uint32_t pos_cnt[3];    /* 0xBD302010, 0xBD302410, 0xBD300038 */
    int32_t pos[3];         /* encoder position per motor (A: none, 0) */
    int stall_motor;        /* last motor stopped by its stall check, -1 = none */
    uint32_t sensor_bits;
    int door_open;
};

int  motor_api_init(void);
int  motor_api_drive(int motor_idx, int mode, uint32_t pwm, uint32_t ms);
void motor_api_stop(void);
void motor_api_poll(void);
void motor_api_telemetry(struct motor_telemetry *t);
/* Encoder-based (motors B and C): end stop = 0, travel, closed-loop move. */
int  motor_api_home(int m, int mode, uint32_t pwm);
int  motor_api_range(int m, int mode, uint32_t pwm, int32_t *span);
int  motor_api_goto(int m, int32_t target, uint32_t pwm);
int32_t motor_api_position(int m);
/* Sensors for the web page: count, name and state of sensor i. */
int  sensor_api_count(void);
const char *sensor_api_name(int i);
int  sensor_api_active(int i);
int  panel_api_set(char ch, int wifi, int color, int black, uint64_t extra);

#define PERIPH_CMD_ENTRIES \
    { "i2c",     "i2c scan | i2c read <addr7> <sub> [len] [16]",            cmd_i2c }, \
    { "eeprom",  "eeprom [offset] [len]: dump the 24512 NVM (read-only)",   cmd_eeprom }, \
    { "asic",    "identify MCASIC and carriage ASIC over MICCI",            cmd_asic }, \
    { "micci",   "micci read <ctrl> <bus> <dev> <reg> | write ... <val>",   cmd_micci }, \
    { "adc",     "adc <selector> [mode]: MCASIC ADC sample (15 ref, 0 supply)", cmd_adc }, \
    { "lcd",     "lcd <char> [wifi] [color] [black] [indicator...] | lcd walk [first last]", cmd_lcd }, \
    { "buttons", "buttons [wait [ms] | watch [s]]: read the front-panel buttons", cmd_buttons }, \
    { "memtest", "memtest [addr] [len]: DRAM pattern test",                 cmd_memtest }, \
    { "motor",   "motor [show] | off | init | pos | pulse <a|b|c> <fwd|rev> <pwm<=600> <ms<=250> | home|range <b|c> [fwd|rev] [pwm] | goto <b|c> <pos> [pwm] | zero <b|c>", cmd_motor }, \
    { "motorsAndButtonsTest", "motorsAndButtonsTest [pwm]: hold panel buttons to drive motors A/B/C (exit to quit)", cmd_motors_and_buttons_test }, \
    { "sensors", "sensors [watch [s]]: door and input-tray sensors (HP's pe_digital_sensor map)", cmd_sensors }

#endif
