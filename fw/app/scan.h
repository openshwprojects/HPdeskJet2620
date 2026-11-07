#ifndef FW_SCAN_H
#define FW_SCAN_H
#include <stdint.h>

/* Flatbed scanner experiments (scan.c, analysis/scanner.md): the scan front
   end ("Dasher", MICCI ctrl0 bus 1 dev 1), its LEDs and one scan line. */
void scan_regs_dump_ex(int with_soc);
int  scan_led_ex(int color, int ms, int soc);
int  scan_line_ex(uint16_t *out, uint32_t first, uint32_t max_px, int color);
void scan_regs_dump(void);
int  scan_led(int color, int ms);
int  scan_line(uint16_t *out, uint32_t max_px);
int  cmd_scan(int argc, char **argv);

#define SCAN_CMD_ENTRY \
    { "scan", "scan regs [soc] | led <r|g|b|off> [ms] [soc] | line [r|g|b|dark] [first] [count]", cmd_scan }

#endif
