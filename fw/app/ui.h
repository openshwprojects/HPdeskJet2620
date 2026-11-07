#ifndef FW_UI_H
#define FW_UI_H
#include <stdint.h>

/* Front-panel menu that works without a PC (see ui.c). */
void ui_poll(void);       /* idle loop: draw the Wi-Fi state, read and act on buttons */
void ui_hold(void);
/* Virtual button press for `ms` (200..5000) while the menu runs; -1 if it does not. */
int  ui_inject(uint32_t mask, uint32_t ms);
/* 0 menu off, 1 running, 2 paused by a panel command, 3 soft off. */
int  ui_state(void);       /* lcd/panel/buttons commands own the panel until "ui on" */
int  cmd_ui(int argc, char **argv);

#define UI_CMD_ENTRY \
    { "ui", "ui [status] | on | off: front-panel menu (Wireless, Info, Cancel, Resume, Power)", cmd_ui }

#endif
