#ifndef FW_IRQ_H
#define FW_IRQ_H
#include <stdint.h>

/* First hardware interrupt (irq.c, irq.S): a periodic timer tick through HP's
   interrupt controller, for a bounded time from the shell only. RAM only; the
   vector literal at 0x38 and the controller are restored afterwards. */
int  irq_tick_start(uint32_t hz, uint32_t seconds);
void irq_tick_stop(void);
int  cmd_irq(int argc, char **argv);

#define IRQ_CMD_ENTRY \
    { "irq", "irq [status] | tick <16..10000 Hz> [1..30 s] | demo [s] | off: timer interrupt test and LED demo", cmd_irq }

#endif
