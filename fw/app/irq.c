/*
 * First hardware interrupt for the custom firmware: a periodic timer tick on
 * timer channel IRQ_CH, routed through timer group 0 = INTC source 18.
 * Pairs with irq.S. Style follows fw/app/periph_cmds.c.
 *
 * Safety model (owner requirement: nothing may risk bricking):
 *   - RAM only. No flash, EEPROM, MCASIC or pad-mux writes. Everything touched
 *     (ITCM literal 0x38, CP15 c5 for two instructions, INTC, timer channel)
 *     is volatile and reset by a power cycle.
 *   - Off by default: nothing runs unless the shell command "irq tick" is typed.
 *   - Bounded: the tick runs for at most IRQ_MAX_SECONDS, 'q' aborts, and the
 *     handler masks itself if it sees more interrupts than possible (storm).
 *   - irq_tick_stop() is idempotent and is called on every exit/error path:
 *     CPSR I+F masked, channel stopped and acked, all INTC masks = 1,
 *     saved INTC priority/threshold restored, original 0x38 literal restored.
 *   - Worst case of any wrong assumption: CPU stuck in a self-loop vector
 *     (HP stage-1 leaves every vector looping on itself) -> power cycle.
 *
 * Evidence (addresses in HP's dump, see the report):
 *   vectors   .reset @0: 8 x E59FF018 "ldr pc,[pc,#0x18]", literals at 0x20..0x3C
 *   install   A000FCC0: MPU off (A000FA6C), str handler,[0x20+4*idx], MPU on (A000FBB4)
 *   handoff   A000FC80 (called from A000C320/A000C710): IRQ+FIQ off (0x2B0),
 *             literals <- table A0012970 = {0:0x14,1:4,...,6:0x18,7:0x1C}
 *             => at our entry, literal[0x38] = 0x18 (self-loop)       [high]
 *   MPU       table 0x31F0 (profile 3) -> region1 = 0x0..0x1FFF, data AP 6 [high]
 *   INTC      A0005B58 init, A0005C4C source config, ITCM 0x16C dispatch
 *   timer     prescaler BD04080E = 0xB4 (180) at 0x2828 -> 1 us channel tick
 *             A02F6704 kernel tick: reload = 1e6/(60*n) - 1 -> BD04081C+4*ch
 *             A01776BA enable  = BD040804+2*group |= 1<<ch
 *             A01776DE disable = mask &= ~bit; BD040802 = bit; BD040812 = bit
 *             A02F66F0 tick ISR ack = BD040802 = 1<<ch (write-1-to-clear)
 *             A01777A0 group ISR: pending = BD040800 & BD040804[group]
 *             A01777D8 registers group ISR at INTC source 18+group, prio 1
 */
#include "hw.h"
#include "uart.h"
#include "fmt.h"
#include "timer.h"
#include "cli.h"
#include "libmini.h"
#include "cache.h"
#include "irq.h"
#include "periph_cmds.h"

/* ---------- registers ---------- */
#define INTC_BASE            0xBD040C00U
#define INTC_PRIO2(bank)     REG32(INTC_BASE + 0x0CU - 4U * (bank))   /* priority bit 2 (1 -> FIQ class) */
#define INTC_MASK(bank)      REG32(INTC_BASE + 0x4CU - 4U * (bank))   /* 1 = masked */
#define INTC_PRIO(idx)       REG32(INTC_BASE + 0xFCU - 4U * (idx))    /* 2 bits per source */
#define INTC_CTRL_B0         REG16(INTC_BASE + 0xB0U)                 /* = 2 at init, meaning unknown: never written */
#define INTC_IRQ_CLAIM       REG16(INTC_BASE + 0xB8U)                 /* hi byte prio, lo byte source */
#define INTC_THRESHOLD       REG16(INTC_BASE + 0xC8U)

#define TMR_PENDING          REG16(0xBD040800U)                       /* read: pending channel bits */
#define TMR_ACK              REG16(0xBD040802U)                       /* write 1 to clear */
#define TMR_GROUP_MASK(g)    REG16(0xBD040804U + 2U * (g))            /* g = 0..4 -> INTC 18..22 */
#define TMR_PRESCALE         REG16(0xBD04080EU)                       /* 0xB4 written by HP reset code */
#define TMR_STOP             REG16(0xBD040812U)                       /* write 1<<ch: stop */
#define TMR_RELOAD(ch)       REG16(0xBD04081CU + 4U * (ch))           /* periodic, period = value+1 us */
#define TMR_COUNTDOWN(ch)    REG16(0xBD04081EU + 4U * (ch))           /* one-shot / remaining */

#define IRQ_SRC              18U       /* timer group 0 */
#define IRQ_GROUP            0U
#define IRQ_CH               7U        /* any idle channel; HP never runs here */
#define IRQ_PRIO             1U        /* HP's priority for the timer group */
#define IRQ_MIN_HZ           16U       /* 16-bit reload: 1e6/16 - 1 = 62499 */
#define IRQ_MAX_HZ           10000U    /* 100 us: keeps the CPU mostly free */
#define IRQ_MAX_SECONDS      30U
#define VEC_INSN_EXPECTED    0xE59FF018U   /* ldr pc,[pc,#0x18] at 0x18 */
#define VEC_LITERAL_HANDOFF  0x00000018U   /* stage-1 handoff value (self-loop), predicted from A000FC80 */
#define VEC_LITERAL_PRINTER  0x00000168U   /* what the printer and the emulator actually have: HP's dispatcher */
#define MPU_R1_DATA_AP_RO    6U

/* irq.S */
uint32_t irq_vec_swap(uint32_t new_literal, uint32_t *readback);
void irq_set_stack(uint32_t top);
uint32_t irq_cpsr_disable(void);
void irq_cpsr_enable_irq_only(void);
void irq_entry(void);
void irq_dispatch_c(void);

/* ---------- state ---------- */
static uint64_t irq_stack[128] __attribute__((aligned(8)));   /* 1 KiB SP_irq */

static struct {
    int installed;                 /* literal 0x38 currently points at irq_entry */
    uint32_t saved_literal;
    uint32_t saved_cpsr;
    uint32_t saved_prio2[4], saved_mask[4], saved_prio[8];
    uint16_t saved_threshold, saved_group_mask[5];
    int intc_saved;
} irq_save;

/* Called from the interrupt on every tick (IRQ mode, I set): keep it short. */
static void (*volatile tick_hook)(uint32_t ticks);

static volatile struct {
    uint32_t ticks;                /* timer interrupts handled */
    uint32_t spurious;             /* claim prio 0/>=4, or our pending bit not set */
    uint32_t foreign;              /* claim for another source (should be 0) */
    uint32_t limit;                /* storm guard: handler masks itself above this */
    uint32_t last_stamp, min_dt, max_dt;
    uint32_t last_claim;
    int fault;                     /* set by the handler when it masked everything */
} irq_stat;

/* ---------- helpers ---------- */
static int usage(const char *text)
{
    uart_printf("usage: %s\n", text);
    return -1;
}

static void dsb(void) { __asm__ volatile ("mcr p15, 0, %0, c7, c10, 4" :: "r"(0) : "memory"); }

static uint32_t mpu_r1_data_ap(void)
{
    uint32_t v;
    __asm__ volatile ("mrc p15, 0, %0, c5, c0, 2" : "=r"(v));
    return (v >> 4) & 15U;
}

static void intc_mask_all(void)
{
    uint32_t b;
    for (b = 0; b < 4; b++) INTC_MASK(b) = 0xFFFFFFFFU;     /* same as HP init A0005BB8 */
    dsb();
}

static void timer_channel_off(uint32_t ch)
{
    uint32_t bit = 1U << ch, g;
    for (g = 0; g < 5; g++) TMR_GROUP_MASK(g) = (uint16_t)(TMR_GROUP_MASK(g) & ~bit);
    TMR_RELOAD(ch) = 0;            /* A02F6744 */
    TMR_COUNTDOWN(ch) = 0;         /* A02F6744 */
    TMR_STOP = (uint16_t)bit;      /* A01776DE; UNCERTAIN whether 0x812 is "stop" or "clear count" */
    TMR_ACK = (uint16_t)bit;       /* A01776DE / A02F66F0 */
    dsb();
}

/* ---------- interrupt handler (IRQ mode, I set, non-nested) ---------- */
void irq_dispatch_c(void)
{
    uint16_t claim = INTC_IRQ_CLAIM;            /* HP 0x178: ldrh [CB8] */
    uint32_t prio = claim >> 8, src = claim & 0xFFU, old;
    irq_stat.last_claim = claim;
    if (prio == 0 || prio >= 4) { irq_stat.spurious++; goto guard; }   /* HP 0x17C..0x188 */

    /* HP raises the threshold to the claimed priority while the handler runs
       (0x18C -> 0x130) and restores it afterwards (0x1D4). Mimic it, since
       the controller may track "in service" through it (no EOI exists). */
    old = INTC_THRESHOLD;
    INTC_THRESHOLD = (uint16_t)prio;
    dsb();

    if (src == IRQ_SRC) {
        uint16_t pend = (uint16_t)(TMR_PENDING & TMR_GROUP_MASK(IRQ_GROUP));
        if (pend & (1U << IRQ_CH)) {
            uint32_t now = TIMER_TICKS, dt;
            TMR_ACK = (uint16_t)(1U << IRQ_CH);  /* device ack, W1C (A02F66F0) */
            dsb();
            if (irq_stat.ticks) {
                dt = now - irq_stat.last_stamp;
                if (dt < irq_stat.min_dt) irq_stat.min_dt = dt;
                if (dt > irq_stat.max_dt) irq_stat.max_dt = dt;
            }
            irq_stat.last_stamp = now;
            irq_stat.ticks++;
            if (tick_hook) tick_hook(irq_stat.ticks);
        } else {
            irq_stat.spurious++;
        }
    } else {
        /* Every other source was masked before enabling: this means our model
           of the controller is wrong. Mask everything and let the shell stop. */
        irq_stat.foreign++;
        intc_mask_all();
        irq_stat.fault = 1;
    }

    INTC_THRESHOLD = (uint16_t)old;
    dsb();
guard:
    if (irq_stat.ticks + irq_stat.spurious > irq_stat.limit) {
        /* Storm guard: more interrupts than the configured rate allows. */
        INTC_MASK(IRQ_SRC >> 5) |= 1U << (IRQ_SRC & 31U);
        timer_channel_off(IRQ_CH);
        irq_stat.fault = 2;
    }
}

/* ---------- start / stop ---------- */
void irq_tick_stop(void)
{
    uint32_t b, rb = 0;
    (void)irq_cpsr_disable();                              /* 1. CPU: I+F masked */
    timer_channel_off(IRQ_CH);                             /* 2. device quiet */
    intc_mask_all();                                       /* 3. controller: everything masked */
    if (irq_save.intc_saved) {                             /* 4. restore priorities/threshold */
        for (b = 0; b < 4; b++) INTC_PRIO2(b) = irq_save.saved_prio2[b];
        for (b = 0; b < 8; b++) INTC_PRIO(b) = irq_save.saved_prio[b];
        for (b = 0; b < 5; b++) TMR_GROUP_MASK(b) = irq_save.saved_group_mask[b];
        INTC_THRESHOLD = irq_save.saved_threshold;
        dsb();
        irq_save.intc_saved = 0;
        /* Masks are deliberately NOT restored: the owner wants everything
           masked afterwards (this firmware never uses interrupts). */
    }
    if (irq_save.installed) {                              /* 5. original vector literal */
        (void)irq_vec_swap(irq_save.saved_literal, &rb);
        if (rb != irq_save.saved_literal)
            uart_printf("irq: WARNING literal 0x38 read back %08x, expected %08x\n", rb, irq_save.saved_literal);
        irq_save.installed = 0;
    }
    tick_hook = 0;
    /* CPSR stays I+F masked: start.S runs the firmware that way. */
}

/* Returns 0 when the tick is running; on any failure everything is undone. */
int irq_tick_start(uint32_t hz, uint32_t seconds)
{
    uint32_t vec = REG32(0x18U), lit = REG32(0x38U), sctlr = cp15_read_sctlr();
    uint32_t period_us, b, rb = 0, bit = 1U << IRQ_CH;

    if (irq_save.installed || irq_save.intc_saved) { uart_puts("irq: already active\n"); return -1; }
    if (hz < IRQ_MIN_HZ || hz > IRQ_MAX_HZ) return usage("irq tick <16..10000 Hz> [1..30 s]");
    period_us = 1000000U / hz;

    /* ---- preconditions (read-only): refuse anything unexpected ---- */
    if (vec != VEC_INSN_EXPECTED) { uart_printf("irq: word at 0x18 is %08x, not ldr pc,[pc,#0x18]: refused\n", vec); return -1; }
    if (lit != VEC_LITERAL_HANDOFF && lit != VEC_LITERAL_PRINTER)
        uart_printf("irq: note: literal 0x38 is %08x (seen before: 0x168 or 0x18); it will be restored as is\n", lit);
    if (sctlr & (1U << 13)) { uart_puts("irq: SCTLR.V set (high vectors): refused\n"); return -1; }
    if (!(sctlr & 1U)) { uart_puts("irq: MPU is off (unexpected): refused\n"); return -1; }
    if (mpu_r1_data_ap() != MPU_R1_DATA_AP_RO) { uart_printf("irq: MPU region 1 data AP %u, expected 6: refused\n", mpu_r1_data_ap()); return -1; }
    if (!timer_rate_is_measured() || g_ticks_per_us != 1U)
        uart_puts("irq: note: system counter not confirmed at 1 MHz; channel period assumes the 180 prescaler\n");
    if (TMR_PRESCALE != 0xB4U) uart_printf("irq: note: prescaler BD04080E = %04x (HP writes 00b4)\n", TMR_PRESCALE);
    if (TMR_RELOAD(IRQ_CH) || TMR_COUNTDOWN(IRQ_CH) || (TMR_PENDING & bit)) {
        uart_printf("irq: channel %u not idle (reload %04x count %04x pending %04x): refused\n",
                    IRQ_CH, TMR_RELOAD(IRQ_CH), TMR_COUNTDOWN(IRQ_CH), TMR_PENDING);
        return -1;
    }

    memset((void *)&irq_stat, 0, sizeof irq_stat);
    irq_stat.min_dt = 0xFFFFFFFFU;
    irq_stat.limit = hz * seconds + hz / 4U + 64U;          /* storm guard */

    /* ---- 1. CPU masked, controller saved and fully masked ---- */
    irq_save.saved_cpsr = irq_cpsr_disable();
    for (b = 0; b < 4; b++) { irq_save.saved_prio2[b] = INTC_PRIO2(b); irq_save.saved_mask[b] = INTC_MASK(b); }
    for (b = 0; b < 8; b++) irq_save.saved_prio[b] = INTC_PRIO(b);
    for (b = 0; b < 5; b++) irq_save.saved_group_mask[b] = TMR_GROUP_MASK(b);
    irq_save.saved_threshold = INTC_THRESHOLD;
    irq_save.intc_saved = 1;
    intc_mask_all();
    timer_channel_off(IRQ_CH);                              /* clean channel, nothing pending */

    /* ---- 2. IRQ-mode stack ---- */
    irq_set_stack((uint32_t)(uintptr_t)&irq_stack[sizeof irq_stack / sizeof irq_stack[0]]);

    /* ---- 3. vector: only the literal word at 0x38 changes ---- */
    irq_save.saved_literal = irq_vec_swap((uint32_t)(uintptr_t)irq_entry, &rb);
    irq_save.installed = 1;
    if (rb != (uint32_t)(uintptr_t)irq_entry) {
        uart_printf("irq: literal write did not stick (read %08x): undone\n", rb);
        irq_tick_stop();
        return -1;
    }

    /* ---- 4. controller: source 18 -> IRQ class, priority 1, unmasked ---- */
    /* Same bit operations as HP A0005C4C for source 18 / priority 1. */
    INTC_PRIO2(IRQ_SRC >> 5) &= ~(1U << (IRQ_SRC & 31U));                /* C0C bit 18 = 0 (not FIQ) */
    INTC_PRIO(IRQ_SRC >> 4) = (INTC_PRIO(IRQ_SRC >> 4) & ~(3U << (2U * (IRQ_SRC & 15U))))
                              | (IRQ_PRIO << (2U * (IRQ_SRC & 15U)));      /* CF8 bits 5:4 = 1 */
    INTC_THRESHOLD = 0;            /* claim needs prio > threshold (HP boot value 0, A0005BD8) */
    INTC_MASK(IRQ_SRC >> 5) &= ~(1U << (IRQ_SRC & 31U));                 /* C4C bit 18 = 0 */
    dsb();

    /* ---- 5. timer: route channel to group 0 and start periodic reload ---- */
    TMR_RELOAD(IRQ_CH) = (uint16_t)(period_us > 1U ? period_us - 1U : period_us);   /* A02F6728..6738 */
    TMR_GROUP_MASK(IRQ_GROUP) = (uint16_t)(TMR_GROUP_MASK(IRQ_GROUP) | bit);       /* A01776BA */
    dsb();

    /* ---- 6. CPU: clear I only (F stays set: FIQ vector is a self-loop) ---- */
    irq_cpsr_enable_irq_only();
    return 0;
}

/* ---------- shell ---------- */
static void irq_show(void)
{
    uint32_t b;
    uart_printf("vector 0x18 %08x literal 0x38 %08x (irq_entry %08x)  sctlr %08x  mpu r1 data AP %u  cpsr %08x\n",
                REG32(0x18U), REG32(0x38U), (uint32_t)(uintptr_t)irq_entry, cp15_read_sctlr(), mpu_r1_data_ap(),
                cpsr_read());
    uart_puts("intc mask c40..c4c:");
    for (b = 4; b-- > 0;) uart_printf(" %08x", INTC_MASK(b));
    uart_puts("  prio2 c00..c0c:");
    for (b = 4; b-- > 0;) uart_printf(" %08x", INTC_PRIO2(b));
    uart_printf("\nintc prio cf8 %08x cfc %08x  threshold %04x  cb0 %04x\n",
                INTC_PRIO(1), INTC_PRIO(0), INTC_THRESHOLD, INTC_CTRL_B0);
    /* CB8 (claim) is read only inside the handler: HP never reads it elsewhere. */
    uart_printf("timer pending %04x groups %04x %04x %04x %04x %04x prescale %04x  ch%u reload %04x count %04x\n",
                TMR_PENDING, TMR_GROUP_MASK(0), TMR_GROUP_MASK(1), TMR_GROUP_MASK(2), TMR_GROUP_MASK(3),
                TMR_GROUP_MASK(4), TMR_PRESCALE, IRQ_CH, TMR_RELOAD(IRQ_CH), TMR_COUNTDOWN(IRQ_CH));
    uart_printf("last run: ticks %u spurious %u foreign %u fault %d  dt min %u max %u us  last claim %04x\n",
                irq_stat.ticks, irq_stat.spurious, irq_stat.foreign, irq_stat.fault,
                irq_stat.ticks > 1 ? irq_stat.min_dt : 0, irq_stat.max_dt, irq_stat.last_claim);
}

/* ---------- demo: LEDs blinked by the interrupt, a stopwatch in the foreground ---------- */

#define DEMO_HZ 1000U

/* Active-low LEDs: power at 1 Hz, blue Wi-Fi at 4 Hz, straight from the tick. */
static void demo_tick(uint32_t ticks)
{
    uint32_t ms = ticks % 1000U;
    GPIO_OUTPUT(GPIO_PIN_POWER_LED) = ms < 500U ? 0 : 1;
    GPIO_OUTPUT(GPIO_PIN_WIFI_LED) = (ms % 250U) < 125U ? 0 : 1;
}

/* "irq demo [s]": the interrupt keeps the LEDs going while the foreground
   counts seconds on the panel digit, and in the middle the foreground stops
   polling for 2 s on purpose: the LEDs and the tick count do not notice. */
static int irq_demo(uint32_t seconds)
{
    uint16_t power_out = GPIO_OUTPUT(GPIO_PIN_POWER_LED), wifi_out = GPIO_OUTPUT(GPIO_PIN_WIFI_LED);
    uint16_t power_dir = GPIO_DIR(GPIO_PIN_POWER_LED), wifi_dir = GPIO_DIR(GPIO_PIN_WIFI_LED);
    uint32_t start, sec = 0, busy_at = seconds / 2U, ms, expect;
    int c, stopped = 0;

    uart_printf("irq demo: %u s. The timer interrupt (%u Hz) blinks the power LED at 1 Hz and the blue\n"
                "Wi-Fi LED at 4 Hz; the panel digit counts seconds. At %u s the main loop stops\n"
                "polling for 2 s on purpose: watch the LEDs keep going. 'q' stops.\n", seconds, DEMO_HZ, busy_at);
    uart_flush();
    GPIO_DIR(GPIO_PIN_POWER_LED) = 1;
    GPIO_DIR(GPIO_PIN_WIFI_LED) = 1;
    tick_hook = demo_tick;
    if (irq_tick_start(DEMO_HZ, seconds + 3U)) { irq_tick_stop(); return -1; }
    start = timer_ticks();
    while (!stopped && sec < seconds) {
        uint32_t t = irq_stat.ticks;                     /* time as the interrupt sees it */
        if (t / 1000U != sec) {
            sec = t / 1000U;
            panel_api_set((char)('0' + sec % 10U), 0, 0, 0, 0);   /* ~45 ms, interrupts keep running */
            uart_printf("  %2u s  ticks %5u  (counter says %5u ms)\n", sec, t, timer_elapsed_ms(start));
            if (sec == busy_at && sec) {
                uint32_t t0 = timer_ticks();
                uart_puts("  main loop busy for 2 s, not polling anything ...\n");
                uart_flush();
                while (timer_elapsed_ms(t0) < 2000U) watchdog_kick();     /* deliberately blind */
                uart_printf("  ... back: %u ticks happened meanwhile\n", irq_stat.ticks - t);
            }
        }
        if (irq_stat.fault) break;
        watchdog_kick();
        c = uart_getc_nonblock();
        if (c == 'q' || c == 3) stopped = 1;
    }
    ms = timer_elapsed_ms(start);
    irq_tick_stop();                                     /* also clears the hook */
    GPIO_OUTPUT(GPIO_PIN_POWER_LED) = power_out;
    GPIO_OUTPUT(GPIO_PIN_WIFI_LED) = wifi_out;
    GPIO_DIR(GPIO_PIN_POWER_LED) = power_dir;
    GPIO_DIR(GPIO_PIN_WIFI_LED) = wifi_dir;
    expect = DEMO_HZ * ms / 1000U;
    uart_printf("irq demo: %u ticks in %u ms (expected ~%u), spurious %u, foreign %u, period %u..%u us%s; "
                "'ui on' gives the panel back to the menu\n", irq_stat.ticks, ms, expect, irq_stat.spurious,
                irq_stat.foreign, irq_stat.ticks > 1 ? irq_stat.min_dt : 0, irq_stat.max_dt,
                irq_stat.fault ? " -- FAULT" : "");
    return irq_stat.fault ? -1 : 0;
}

int cmd_irq(int argc, char **argv)
{
    uint32_t hz, seconds = 2, start, ms, expect;
    int c;
    if (argc == 1 || (argc == 2 && !strcmp(argv[1], "status"))) { irq_show(); return 0; }
    if (argc == 2 && !strcmp(argv[1], "off")) { irq_tick_stop(); irq_show(); return 0; }
    if (argc >= 2 && argc <= 3 && !strcmp(argv[1], "demo")) {
        uint32_t s = 10;
        if (argc == 3 && (parse_u32(argv[2], &s) || s < 3U || s > 27U)) return usage("irq demo [3..27 s]");
        return irq_demo(s);
    }
    if (argc < 3 || argc > 4 || strcmp(argv[1], "tick") || parse_u32(argv[2], &hz) ||
        (argc == 4 && parse_u32(argv[3], &seconds)) || seconds == 0 || seconds > IRQ_MAX_SECONDS ||
        hz < IRQ_MIN_HZ || hz > IRQ_MAX_HZ)
        return usage("irq [status] | irq tick <16..10000 Hz> [1..30 s] | irq demo [3..27 s] | irq off");

    uart_printf("irq: tick %u Hz on timer channel %u (INTC source %u) for %u s, 'q' stops\n", hz, IRQ_CH, IRQ_SRC, seconds);
    uart_flush();
    if (irq_tick_start(hz, seconds)) { irq_tick_stop(); return -1; }

    /* Bounded foreground run: no other command can run meanwhile, so nothing
       else in this firmware ever sees interrupts enabled. */
    start = timer_ticks();
    ms = seconds * 1000U;
    while (timer_elapsed_ms(start) < ms) {
        watchdog_kick();
        if (irq_stat.fault) break;
        c = uart_getc_nonblock();
        if (c == 'q' || c == 3) break;
    }
    ms = timer_elapsed_ms(start);
    irq_tick_stop();

    expect = hz * ms / 1000U;      /* <= 10000 * ~30000: fits 32 bits, no 64-bit divide helper */
    uart_printf("irq: %u ticks in %u ms (expected ~%u), spurious %u, foreign %u, period min %u max %u us%s\n",
                irq_stat.ticks, ms, expect, irq_stat.spurious, irq_stat.foreign,
                irq_stat.ticks > 1 ? irq_stat.min_dt : 0, irq_stat.max_dt,
                irq_stat.fault == 1 ? " -- FAULT: foreign source, everything masked" :
                irq_stat.fault == 2 ? " -- FAULT: storm guard tripped" : "");
    irq_show();
    return irq_stat.fault ? -1 : 0;
}
