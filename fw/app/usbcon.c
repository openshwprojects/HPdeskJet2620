/*
 * Serial console on the printer's USB device port (CDC-ACM) - an experiment.
 *
 * Written against the emulator's model of the AXON USB 2.0 device controller
 * (usb.py, reverse-engineered from HP's udw20_drv.c and the boot ROM's USB
 * recovery stack) and the endpoint set-up words HP's own firmware leaves in
 * the controller (Stage-22 snapshot, analysis/usb_console.md). Enumeration
 * works on the printer (2026-11-05, Windows: COM port, high speed). The bulk
 * endpoints follow HP's own sequence (boot ROM USB stack in .load_text and
 * udw20_drv.c): endpoint enable bit 7 and the packet-size code are set at
 * SET_CONFIGURATION, 0xBD09FC08 holds the configuration value (the hardware
 * answers SET_ADDRESS by itself), every transfer is one DMA on channel 0.
 *
 *   EP0 control:      SETUP packet at 0xBD09FC10/14, answers through the EP0
 *                     DMA channel, 0xBD09FC1A = 1 acknowledges, 3 stalls
 *   EP1 bulk OUT:     characters typed on the host go to the shell
 *   EP2 bulk IN:      a copy of everything the shell prints
 *   EP3 interrupt IN: CDC notifications (none are sent)
 *
 * Polled from the idle loop after "usb on"; USB IDs 1209:0001 (pid.codes
 * test ID). DMA buffers live in the uncached DRAM alias like the SDIO ones.
 */
#include "usbcon.h"
#include "hw.h"
#include "uart.h"
#include "timer.h"
#include "fmt.h"
#include "libmini.h"

#define USB_CTRL         REG32(0xBD09FC00U)   /* bit 19 soft reset */
#define USB_STATUS       REG32(0xBD09FC04U)   /* bit 0 attached, bits 8..9 speed (0 high, 1 full) */
#define USB_CONFIG       REG16(0xBD09FC08U)   /* configuration value (the address is handled by the hardware) */
#define USB_PULLUP       REG16(0xBD09FC0EU)   /* bit 0 D+ pull-up */
#define USB_SETUP0       REG32(0xBD09FC10U)
#define USB_SETUP1       REG32(0xBD09FC14U)   /* reading it releases the SETUP latch */
#define USB_EP0_DONE     REG16(0xBD09FC1AU)   /* 1 = acknowledge, 3 = stall */
#define USB_RX_READY     REG16(0xBD09FC1CU)   /* per endpoint, write 1 to clear */
#define USB_RX_ENABLE    REG16(0xBD09FC1EU)
#define USB_DMA_DONE     REG16(0xBD09FC24U)   /* per endpoint DMA completion (status) */
#define USB_DMA_ENABLE   REG16(0xBD09FC26U)   /* per endpoint DMA completion enable */
#define USB_EVENTS       REG16(0xBD09FC28U)   /* write 1 to clear: 1 suspend, 2 resume, 3 bus reset, 8 setup */
#define EP_BASE(ep)      (0xBD09BC00U + 0x400U * (uint32_t)(ep))
#define EP_WORD(ep)      REG32(EP_BASE(ep) + 0x00)
#define EP_MAP(ep)       REG16(EP_BASE(ep) + 0x04)   /* [3:0] ep no. (HW preset), [7:4] alt setting, [11:8] interface */
#define EP_FIFO(ep)      REG16(EP_BASE(ep) + 0x06)   /* offset of the EP buffer in FIFO RAM 0xBD0A0000 */
#define EP_STAT(ep)      REG16(EP_BASE(ep) + 0x08)   /* bit4 = current OUT buffer, bit8 = IN FIFO still full [?] */
#define EP_CNT0(ep)      REG16(EP_BASE(ep) + 0x10)   /* OUT bytes in buffer 0, & 0x7FF (NOT <<2 as in usb.py) */
#define EP_CNT1(ep)      REG16(EP_BASE(ep) + 0x14)   /* OUT bytes in buffer 1, & 0x7FF */
#define EP_TOGGLE(ep)    REG32(EP_BASE(ep) + 0x18)   /* write 0 = reset data toggle [?] */
#define EP_FLUSH(ep)     REG16(EP_BASE(ep) + 0x1C)   /* 1 = drop OUT buffer, 2 = IN, 3 = both */
#define EP_DMA_ACK(ep)   REG16(EP_BASE(ep) + 0x20)   /* write 1: acknowledge channel-0 completion */
#define EP_DMA_ABORT(ep) REG16(EP_BASE(ep) + 0x22)   /* write 1: abort channel 0 */
#define EP_DMA_ADDR(ep)  REG32(EP_BASE(ep) + 0x24)
#define EP_DMA_LEN(ep)   REG32(EP_BASE(ep) + 0x28)   /* bytes still to move; residual after completion */
#define EP_DMA_DONE(ep)  REG16(EP_BASE(ep) + 0x2C)   /* read-only: != 0 once channel 0 finished */
#define EP_DMA_GO(ep)    REG16(EP_BASE(ep) + 0x2E)   /* write 1 = start; reads 1 while busy */

#define EV_BUS_RESET     0x0008U
#define EV_SETUP         0x0100U
#define EPW_MPS_MASK     0x0000001FU                  /* 4 = 64 bytes, 7 = 512 bytes */
#define EPW_ENABLE       0x00000080U                  /* set at SET_CONFIGURATION, cleared at reset/deconfig */
#define EPW_DMA          0x00100000U                  /* set before every DMA, with ENABLE at SET_CONFIGURATION */
#define EPW_B22          0x00400000U
#define EPW_B23          0x00800000U                  /* never written as 1 except the bus-reset pulse */
#define EPW_KEEP         0x83100000U                  /* bits HP preserves when (re)programming the type */

#define RX_LEN           512U
#define TX_MAX           511U                         /* always end with a short packet: no ZLP needed */

#define CTRL_RUN         0x0060130FU          /* HP's value */
#define CTRL_SOFT_RESET  0x00080000U

#define EP_OUT           1
#define EP_IN            2
#define EP_NOTIFY        3

/* uncached DRAM after the SDIO buffer (0x60800000, 64 KiB) */
#define BUF_EP0          0x60810000U
#define BUF_RX           0x60810400U
#define BUF_TX           0x60810800U
#define BUF(a)           ((volatile uint8_t *)(uintptr_t)(a))
#define MAX_PACKET_HS    512U
#define MAX_PACKET_FS    64U

static int enabled, configured, tx_busy;
static uint32_t resets, setups, stalls, rx_bytes, tx_bytes, tx_dropped;
static uint8_t line_coding[7] = { 0x00, 0xC2, 0x01, 0x00, 0, 0, 8 };   /* 115200 8N1 */

#define RING 8192U
static char txq[RING];
static unsigned txq_head, txq_tail;
static char rxq[2048];                           /* a whole XMODEM-1K block fits */
static unsigned rxq_head, rxq_tail;

static const uint8_t dev_desc[18] = {
    18, 1, 0x00, 0x02, 0x02, 0x00, 0x00, 64,          /* USB 2.0, CDC, EP0 64 bytes */
    0x09, 0x12, 0x01, 0x00, 0x00, 0x01,               /* 1209:0001, release 1.00 (little-endian) */
    1, 2, 0, 1,
};

static uint8_t cfg_desc[67] = {
    9, 2, 67, 0, 2, 1, 0, 0xC0, 1,                    /* 2 interfaces, self-powered, 2 mA */
    9, 4, 0, 0, 1, 0x02, 0x02, 0x01, 0,               /* interface 0: CDC ACM */
    5, 0x24, 0x00, 0x10, 0x01,                        /* header, CDC 1.10 */
    5, 0x24, 0x01, 0x00, 0x01,                        /* call management: data interface 1 */
    4, 0x24, 0x02, 0x02,                              /* ACM: line coding supported */
    5, 0x24, 0x06, 0x00, 0x01,                        /* union: 0 controls 1 */
    7, 5, 0x80 | EP_NOTIFY, 0x03, 8, 0, 16,           /* interrupt IN, 8 bytes */
    9, 4, 1, 0, 2, 0x0A, 0x00, 0x00, 0,               /* interface 1: CDC data */
    7, 5, EP_OUT, 0x02, 0x00, 0x02, 0,                /* bulk OUT, packet size patched */
    7, 5, 0x80 | EP_IN, 0x02, 0x00, 0x02, 0,          /* bulk IN */
};

static const char *const strings[] = { "", "HPDJ hobby firmware", "DeskJet 2620 serial console" };

static void console_mirror(char c)
{
    unsigned next = (txq_head + 1U) % RING;
    if (!enabled || !configured) return;               /* nobody to read it yet */
    if (next == txq_tail) { tx_dropped++; return; }
    txq[txq_head] = c;
    txq_head = next;
}

int usbcon_getc(void)
{
    int c;
    if (rxq_tail == rxq_head) return -1;
    c = (uint8_t)rxq[rxq_tail];
    rxq_tail = (rxq_tail + 1U) % sizeof(rxq);
    return c;
}

/* Answer the data stage of an IN control request through the EP0 DMA channel. */
static void ep0_send(const uint8_t *data, uint32_t len, uint32_t wlength)
{
    uint32_t i;
    if (len > wlength) len = wlength;
    if (len > 256) len = 256;
    for (i = 0; i < len; i++) BUF(BUF_EP0)[i] = data[i];
    EP_DMA_ADDR(0) = BUF_EP0;
    EP_DMA_LEN(0) = len;
    if (len) EP_DMA_GO(0) = 1;
    USB_EP0_DONE = 1;
}

static void ep0_ack(void) { USB_EP0_DONE = 1; }

static void ep0_stall(void)
{
    stalls++;
    USB_EP0_DONE = 3;
}

static int rx_armed;
static uint32_t rx_seen;                        /* bytes of the armed OUT buffer already taken */

/* udw20 0xA01986D8: pulse bit 23, clear 23/22, flush both FIFO halves */
static void ep_reset_pulse(int ep)
{
    EP_WORD(ep) = EP_WORD(ep) | EPW_B23;
    EP_WORD(ep) = EP_WORD(ep) & ~(EPW_B23 | EPW_B22);
    EP_FLUSH(ep) = 3;
}

/* udw20 0xA0197E74: type/direction word, FIFO offset, interface number */
static void ep_setup(int ep, uint32_t type_bits, uint16_t fifo, unsigned iface)
{
    EP_FIFO(ep) = fifo;
    EP_WORD(ep) = (EP_WORD(ep) & EPW_KEEP) | type_bits;
    EP_MAP(ep) = (uint16_t)((EP_MAP(ep) & 0xF0FFU) | (iface << 8));
}

/* udw20 0xA019879E + 0xA0198E02 / ROM 0xA000E962: same order */
static void dma_start(int ep, uint32_t addr, uint32_t len)
{
    EP_WORD(ep) = (EP_WORD(ep) & ~EPW_B23) | EPW_DMA;
    USB_DMA_ENABLE = (uint16_t)(USB_DMA_ENABLE | (1U << ep));   /* FC26; IRQs are masked in this fw */
    /* The boot ROM's IN send (0xA000F324) sets the endpoint's FC1E bit first;
       without it the data stays in the endpoint FIFO (seen on the printer). */
    if (ep == EP_IN) USB_RX_ENABLE = (uint16_t)(USB_RX_ENABLE | (1U << ep));
    EP_DMA_ADDR(ep) = addr;
    EP_DMA_LEN(ep) = len;
    EP_DMA_GO(ep) = 1;
}

/* udw20 0xA0198890 / ROM 0xA000EA54: residual byte count, or -1 if still running */
static int dma_finish(int ep)
{
    uint32_t rem;
    if (!EP_DMA_DONE(ep) && !(USB_DMA_DONE & (1U << ep))) return -1;
    rem = EP_DMA_LEN(ep) & 0xFFFFU;
    USB_DMA_ENABLE = (uint16_t)(USB_DMA_ENABLE & ~(1U << ep));
    EP_DMA_ACK(ep) = 1;                                 /* clears +0x2C and the FC24 bit */
    return (int)rem;
}

static void ep_disable_all(void)                        /* bus reset / config 0 (ROM 0xA000EDD0, udw20 0xA0196CE8) */
{
    int ep;
    if (rx_armed) {                                      /* udw20 0xA0198990 + wait loop at 0xA0196064 */
        unsigned i;
        EP_DMA_ABORT(EP_OUT) = 1;
        for (i = 0; i < 1000 && !EP_DMA_DONE(EP_OUT); i++) ;
        EP_DMA_ACK(EP_OUT) = 1;
        rx_armed = 0;
    }
    for (ep = 1; ep <= 3; ep++) {
        EP_WORD(ep) = EP_WORD(ep) & 0xFF7FFFFFU & 0xFFEFFF7FU;
        EP_FLUSH(ep) = ep == EP_OUT ? 1 : 2;
        EP_TOGGLE(ep) = 0;
    }
    USB_CONFIG = 0;
    tx_busy = 0;
    USB_DMA_ENABLE = 0;
}

static void rx_arm(void)                                 /* ROM-style: arm OUT DMA before data arrives */
{
    dma_start(EP_OUT, BUF_RX, RX_LEN);
    rx_armed = 1;
    rx_seen = 0;
}

static void configure_endpoints(void)
{
    int hs = ((USB_STATUS >> 8) & 3U) == 0;              /* ROM 0xA000EE50: FC04 & 0x300 == 0 -> high speed */
    uint32_t code = hs ? 7U : 4U;
    int ep;

    for (ep = 1; ep <= 3; ep++) ep_reset_pulse(ep);
    ep_setup(EP_OUT,    0x01100044U, 0x0080, 1);         /* bulk OUT  (udw20 0xA0197F58) */
    EP_STAT(EP_OUT) = (uint16_t)(EP_STAT(EP_OUT) & 0xFF0FU);
    ep_setup(EP_IN,     0x05100544U, 0x0280, 1);         /* bulk IN   (0x05100144 | 0x400, 0xA0197FA2) */
    ep_setup(EP_NOTIFY, 0x05100564U, 0x0480, 0);         /* interrupt IN (0xA019803C) */

    /* SET_CONFIGURATION: ROM 0xA000EE0C..EE84, udw20 0xA0196DF6..0xA0196E66 */
    EP_WORD(EP_OUT)    = (EP_WORD(EP_OUT)    & 0xFF7FFFE0U) | EPW_DMA | EPW_ENABLE | code;   /* 0x011000C7 @HS */
    EP_WORD(EP_IN)     = (EP_WORD(EP_IN)     & 0xFF7FFFE0U) | EPW_DMA | EPW_ENABLE | code;   /* 0x051005C7 @HS */
    EP_WORD(EP_NOTIFY) = (EP_WORD(EP_NOTIFY) & 0xFF7FFFE0U) | EPW_DMA | EPW_ENABLE | 4U;     /* 0x051005E4 */
    for (ep = 1; ep <= 3; ep++) EP_TOGGLE(ep) = 0;
    USB_CONFIG = 1;                                      /* ROM 0xA000EE34 (reads back 1) */
    tx_busy = 0;
    rx_arm();
}

static void string_desc(unsigned index, uint32_t wlength)
{
    uint8_t d[64];
    uint32_t n = 2;
    if (index == 0) {
        d[2] = 0x09; d[3] = 0x04;                      /* English (US) */
        n = 4;
    } else if (index < sizeof(strings) / sizeof(strings[0])) {
        const char *s = strings[index];
        while (*s && n + 2 <= sizeof(d)) { d[n++] = (uint8_t)*s++; d[n++] = 0; }   /* UTF-16LE */
    } else {
        ep0_stall();
        return;
    }
    d[0] = (uint8_t)n;
    d[1] = 3;
    ep0_send(d, n, wlength);
}

static void handle_setup(void)
{
    uint32_t s0 = USB_SETUP0, s1 = USB_SETUP1;
    uint8_t type = (uint8_t)(s0 >> 24), req = (uint8_t)(s0 >> 16);
    uint16_t value = (uint16_t)(((s0 >> 8) & 0xFFU) | ((s0 & 0xFFU) << 8));
    uint16_t index = (uint16_t)(((s1 >> 24) & 0xFFU) | (((s1 >> 16) & 0xFFU) << 8));
    uint16_t length = (uint16_t)(((s1 >> 8) & 0xFFU) | ((s1 & 0xFFU) << 8));
    (void)index;
    setups++;
    if (type == 0x80 && req == 6) {                    /* GET_DESCRIPTOR */
        unsigned kind = value >> 8;
        if (kind == 1) { ep0_send(dev_desc, sizeof(dev_desc), length); return; }
        if (kind == 2) {
            uint16_t mps = ((USB_STATUS >> 8) & 3U) == 1U ? MAX_PACKET_FS : MAX_PACKET_HS;
            cfg_desc[57] = (uint8_t)mps; cfg_desc[58] = (uint8_t)(mps >> 8);   /* bulk OUT */
            cfg_desc[64] = (uint8_t)mps; cfg_desc[65] = (uint8_t)(mps >> 8);   /* bulk IN */
            ep0_send(cfg_desc, sizeof(cfg_desc), length);
            return;
        }
        if (kind == 3) { string_desc(value & 0xFFU, length); return; }
        ep0_stall();                                   /* device qualifier etc. */
        return;
    }
    if (type == 0x00 && req == 5) {                    /* SET_ADDRESS: the hardware latches it (HP never handles it) */
        ep0_ack();
        return;
    }
    if (type == 0x00 && req == 9) {                    /* SET_CONFIGURATION */
        configured = (value & 0xFFU) != 0;
        if (configured) configure_endpoints();
        else ep_disable_all();
        ep0_ack();
        return;
    }
    if (type == 0x80 && req == 8) {                    /* GET_CONFIGURATION */
        uint8_t c = (uint8_t)configured;
        ep0_send(&c, 1, length);
        return;
    }
    if (type == 0x80 && req == 0) {                    /* GET_STATUS: self-powered */
        static const uint8_t st[2] = { 1, 0 };
        ep0_send(st, 2, length);
        return;
    }
    if (type == 0x21 && (req == 0x20 || req == 0x22)) { ep0_ack(); return; }   /* line coding / line state */
    if (type == 0xA1 && req == 0x21) { ep0_send(line_coding, sizeof(line_coding), length); return; }
    if ((type & 0x60) == 0 && (req == 1 || req == 3 || req == 11)) { ep0_ack(); return; }  /* features, interface */
    ep0_stall();
}

/* The OUT DMA does not end on a short packet (seen on the printer: 12 typed
   bytes landed, the length register went 512 -> 500, no completion), so the
   bytes are taken as the length register counts down; the DMA completes, and
   is re-armed, once the 512-byte buffer is full. Nothing is dropped: bytes the
   queue cannot take stay in the buffer, and while the buffer is full the
   controller makes the host wait (XMODEM over USB relies on this). */
static void receive(void)
{
    uint32_t got;
    if (!rx_armed) { rx_arm(); return; }
    got = RX_LEN - (EP_DMA_LEN(EP_OUT) & 0xFFFFU);
    if (got > RX_LEN) got = RX_LEN;
    while (rx_seen < got) {
        unsigned next = (rxq_head + 1U) % sizeof(rxq);
        if (next == rxq_tail) return;                   /* queue full: take the rest later */
        rxq[rxq_head] = (char)BUF(BUF_RX)[rx_seen++];
        rxq_head = next;
        rx_bytes++;
    }
    if (rx_seen < RX_LEN || dma_finish(EP_OUT) < 0) return;
    rx_armed = 0;
    rx_arm();                                            /* re-arm so the next OUT packet is ACKed */
}

static void transmit(void)
{
    uint32_t n = 0;
    if (tx_busy) {
        if (dma_finish(EP_IN) < 0) return;
        tx_busy = 0;
    }
    while (txq_tail != txq_head && n < TX_MAX) {
        BUF(BUF_TX)[n++] = (uint8_t)txq[txq_tail];
        txq_tail = (txq_tail + 1U) % RING;
    }
    if (!n) return;
    dma_start(EP_IN, BUF_TX, n);
    tx_bytes += n;
    tx_busy = 1;
}

void usbcon_poll(void)
{
    uint16_t ev;
    if (!enabled) return;
    ev = USB_EVENTS;
    if (ev & EV_BUS_RESET) {
        USB_EVENTS = EV_BUS_RESET;
        resets++;
        ep_disable_all();
        configured = 0;
    }
    if (ev & EV_SETUP) handle_setup();
    if (ev & 0x0006U) USB_EVENTS = (uint16_t)(ev & 0x0006U);   /* suspend / resume */
    if (configured) {
        receive();
        transmit();
    }
}

/* Input hook for uart_getc_nonblock(): the shell, XMODEM and the interactive
   commands read USB keys too, and the USB side keeps running while they wait. */
static int usb_input(void)
{
    usbcon_poll();
    return usbcon_getc();
}

static int usb_input_ready(void)
{
    usbcon_poll();
    return rxq_head != rxq_tail;
}

static void usb_start(void)
{
    USB_PULLUP = 0;
    USB_CTRL = CTRL_RUN | CTRL_SOFT_RESET;
    timer_delay_us(100);
    USB_CTRL = CTRL_RUN;
    USB_EVENTS = 0xFFFF;
    USB_RX_READY = 0xFFFF;
    USB_DMA_DONE = 0xFFFF;
    USB_CONFIG = 0;
    configured = tx_busy = rx_armed = 0;
    txq_head = txq_tail = rxq_head = rxq_tail = 0;
    enabled = 1;
    uart_set_mirror(console_mirror);
    uart_set_input(usb_input, usb_input_ready);
    USB_PULLUP = 1;                                    /* tell the host we are here */
}

void usbcon_start(void) { usb_start(); }

static void usb_stop(void)
{
    uart_set_mirror(0);
    uart_set_input(0, 0);
    uart_set_hw_output(1);
    if (configured) ep_disable_all();
    USB_PULLUP = 0;
    enabled = configured = 0;
}

void usbcon_stop(void) { if (enabled) usb_stop(); }

int cmd_usb(int argc, char **argv)
{
    if (argc == 2 && !strcmp(argv[1], "on")) {
        usb_start();
        uart_puts("usb: pull-up on; a host should now see a serial port (1209:0001)\n");
        return 0;
    }
    if (argc == 2 && !strcmp(argv[1], "off")) {
        usb_stop();
        uart_puts("usb: off\n");
        return 0;
    }
    if (argc == 3 && !strcmp(argv[1], "uart") && (!strcmp(argv[2], "on") || !strcmp(argv[2], "off"))) {
        /* With the UART copy off, output runs at USB speed instead of 115200
           baud; the UART still takes input. Back on with "usb uart on" or
           "usb off". */
        if (!strcmp(argv[2], "on")) uart_set_hw_output(1);
        uart_printf("usb: output %s\n", strcmp(argv[2], "on") ? "to USB only (UART silent until 'usb uart on')"
                                                                : "to USB and the UART");
        if (strcmp(argv[2], "on")) {
            if (!enabled) { uart_set_hw_output(1); uart_puts("usb: not on\n"); return -1; }
            uart_set_hw_output(0);
        }
        return 0;
    }
    if (argc == 1 || (argc == 2 && !strcmp(argv[1], "status"))) {
        uint32_t st = USB_STATUS;
        uart_printf("usb: %s, link status %08x (%s, %s speed), %s (config register %u)\n", enabled ? "on" : "off",
                    st, st & 1U ? "attached" : "no host", ((st >> 8) & 3U) == 1U ? "full" : "high",
                    configured ? "configured" : "not configured", USB_CONFIG);
        uart_printf("usb: bus resets %u, setups %u, stalls %u, bytes in %u out %u, dropped %u; UART copy %s\n",
                    resets, setups, stalls, rx_bytes, tx_bytes, tx_dropped, uart_hw_output() ? "on" : "off");
        return 0;
    }
    uart_puts("usage: usb [status] | usb on | usb off | usb uart on|off\n");
    return -1;
}
