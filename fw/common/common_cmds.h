/* Commands available in both the bootloader and the application shell. */
#ifndef FW_COMMON_CMDS_H
#define FW_COMMON_CMDS_H
#include "cli.h"

/* Slot the running image was selected from (BL: best slot; app: bootinfo).
   -1 when unknown. Used by "update" to pick the inactive slot by default. */
extern int g_current_slot;

int cmd_md(int argc, char **argv);
int cmd_mw(int argc, char **argv);
int cmd_crc(int argc, char **argv);
int cmd_flash(int argc, char **argv);
int cmd_slots(int argc, char **argv);
int cmd_verify(int argc, char **argv);
int cmd_erase(int argc, char **argv);
int cmd_update(int argc, char **argv);
int cmd_uptime(int argc, char **argv);
int cmd_sysinfo(int argc, char **argv);
int cmd_reboot(int argc, char **argv);

#define COMMON_CMD_ENTRIES \
    { "slots",   "list application slots",                          cmd_slots }, \
    { "update",  "update [a|b]: receive app image over XMODEM into a slot", cmd_update }, \
    { "verify",  "verify [a|b]: re-check slot header and payload CRC", cmd_verify }, \
    { "erase",   "erase a|b: erase an application slot",            cmd_erase }, \
    { "flash",   "flash id|status|read <off> <len>|selftest|diag|mode [byte|wide]", cmd_flash }, \
    { "md",      "md <addr> [words]: dump memory",                  cmd_md }, \
    { "mw",      "mw <addr> <value>: write a 32-bit word",          cmd_mw }, \
    { "crc",     "crc <addr> <len>: CRC-32 of a memory range",      cmd_crc }, \
    { "sysinfo", "CPU/MPU/cache/flash information",                 cmd_sysinfo }, \
    { "uptime",  "time since boot",                                 cmd_uptime }, \
    { "reboot",  "reset the printer via the watchdog",              cmd_reboot }

/* Print the standard reboot marker and arm the watchdog. Returns -1 only if
   the SoC did not reset within REBOOT_WAIT_MS. */
int system_reboot(void);

#endif
