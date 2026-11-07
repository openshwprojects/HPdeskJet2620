#ifndef FW_RAMRUN_H
#define FW_RAMRUN_H
#include <stdint.h>

/* Run an application image from RAM without the bootloader and without
   writing flash (ramrun.c): "ramrun" receives it over XMODEM (UART or USB),
   POST /api/run takes it over HTTP. A power cycle returns to the flash slots. */

/* Validate the image staged at STAGING_ADDR (len bytes); 0 if it can run. */
int  ramrun_check(uint32_t len);
/* Hand over to the staged image; returns only if it was refused. */
int  ramrun_execute(void);
int  cmd_ramrun(int argc, char **argv);

#define RAMRUN_CMD_ENTRY \
    { "ramrun", "ramrun: receive an application over XMODEM (UART or USB) and run it from RAM", cmd_ramrun }

#endif
