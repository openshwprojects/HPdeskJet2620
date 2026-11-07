/*
 * Minimal IPv4 stack for the Wi-Fi link (see net.h). Frames are processed one
 * at a time from the shell's idle loop; outgoing frames are built in `frame`
 * and handed to wifi_send(). Everything is small and fixed: 8 ARP entries,
 * one packet waiting for an ARP answer, no IP fragments.
 *
 *   station:      DHCP client (host name hpdj-2620) or a static address
 *   access point: 192.168.4.1/24 and a DHCP server for .10 to .17
 *   both:         ICMP echo, so the printer answers ping and can ping;
 *                 mDNS, so "<name>.local" (default deskjet.local) finds it;
 *                 the HTTP control page and API on port 80 (motors, sensors,
 *                 panel, log, shell, application update)
 */
#include "net.h"
#include "wifi.h"
#include "uart.h"
#include "timer.h"
#include "fmt.h"
#include "libmini.h"
#include "hw.h"
#include "layout.h"
#include "slots.h"
#include "image.h"
#include "common_cmds.h"
#include "periph_cmds.h"
#include "spi_flash.h"
#include "ramrun.h"
#include "ui.h"
#include "periph.h"

#ifndef FW_VERSION_STR
#define FW_VERSION_STR "1.0.0"
#endif
#ifndef FW_SEQUENCE
#define FW_SEQUENCE 1U
#endif
/* Shared secret for the web shell, reboot and application update. Change it
   with "server token <t>" (RAM only) or build with -DWEB_TOKEN='"..."'. */
#ifndef WEB_TOKEN
#define WEB_TOKEN "hpdj2620"
#endif

#define ETH_HLEN        14U
#define IP_HLEN         20U
#define UDP_HLEN        8U
#define TCP_HLEN        20U
#define ETH_TYPE_IP     0x0800U
#define ETH_TYPE_ARP    0x0806U
#define PROTO_ICMP      1
#define PROTO_TCP       6
#define PROTO_UDP       17
#define FRAME_MAX       1536U
#define PAYLOAD_OFF     (ETH_HLEN + IP_HLEN)            /* ICMP / UDP / TCP header */

#define AP_ADDR         0xC0A80401U                     /* 192.168.4.1 */
#define AP_MASK         0xFFFFFF00U
#define AP_POOL_FIRST   10U
#define AP_POOL_SIZE    8U
#define DHCP_MAGIC      0x63825363U

static uint32_t my_ip, my_mask, my_gw, dhcp_server;
static uint8_t ip_ttl = 64;                             /* mDNS sends with 255 */
static uint32_t static_ip, static_mask, static_gw;
static int link_up, ap_mode, static_cfg;
static uint16_t ip_ident = 1;
static uint8_t frame[FRAME_MAX];                        /* outgoing frame under construction */

static struct {
    uint32_t rx, tx, bad, arp_miss, echo;
} st;

static const uint8_t mac_bcast[6] = { 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF };

/* --- byte order and checksums (network order is big-endian like the CPU,
       but everything is done byte by byte so alignment never matters) ----- */

static uint16_t rd16(const uint8_t *p) { return (uint16_t)((p[0] << 8) | p[1]); }
static uint32_t rd32(const uint8_t *p) { return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | p[3]; }
static void wr16(uint8_t *p, uint32_t v) { p[0] = (uint8_t)(v >> 8); p[1] = (uint8_t)v; }
static void wr32(uint8_t *p, uint32_t v) { p[0] = (uint8_t)(v >> 24); p[1] = (uint8_t)(v >> 16); p[2] = (uint8_t)(v >> 8); p[3] = (uint8_t)v; }

static uint32_t csum_add(uint32_t sum, const uint8_t *p, uint32_t len)
{
    while (len > 1) { sum += (uint32_t)((p[0] << 8) | p[1]); p += 2; len -= 2; }
    if (len) sum += (uint32_t)p[0] << 8;
    return sum;
}

static uint16_t csum_fold(uint32_t sum)
{
    while (sum >> 16) sum = (sum & 0xFFFFU) + (sum >> 16);
    return (uint16_t)~sum;
}

static uint32_t pseudo_sum(uint32_t src, uint32_t dst, uint32_t proto, uint32_t len)
{
    return (src >> 16) + (src & 0xFFFFU) + (dst >> 16) + (dst & 0xFFFFU) + proto + len;
}

void net_format_ip(uint32_t ip, char *buf)
{
    snprintf(buf, 16, "%u.%u.%u.%u", ip >> 24, (ip >> 16) & 0xFFU, (ip >> 8) & 0xFFU, ip & 0xFFU);
}

int net_parse_ip(const char *s, uint32_t *ip)
{
    uint32_t v = 0, part = 0;
    int parts = 0, digits = 0;
    for (;; s++) {
        if (*s >= '0' && *s <= '9') {
            part = part * 10U + (uint32_t)(*s - '0');
            if (part > 255 || ++digits > 3) return -1;
        } else if (*s == '.' || *s == 0) {
            if (!digits || parts == 4) return -1;
            v = (v << 8) | part;
            parts++;
            part = 0;
            digits = 0;
            if (*s == 0) break;
        } else {
            return -1;
        }
    }
    if (parts != 4) return -1;
    *ip = v;
    return 0;
}

static unsigned prefix_len(uint32_t mask)
{
    unsigned n = 0;
    while (mask & 0x80000000U) { n++; mask <<= 1; }
    return n;
}

uint32_t net_ip(void) { return my_ip; }
int net_has_address(void) { return link_up && my_ip != 0; }

/* --- ARP ------------------------------------------------------------------ */

#define ARP_SLOTS 8
static struct { uint32_t ip; uint8_t mac[6]; uint32_t used; } arp_tab[ARP_SLOTS];
static uint32_t arp_clock;
static uint8_t arp_wait_frame[FRAME_MAX];               /* one packet waiting for its next hop */
static uint32_t arp_wait_len, arp_wait_hop, arp_wait_since;

static const uint8_t *arp_find(uint32_t ip)
{
    unsigned i;
    if (!ip) return 0;
    for (i = 0; i < ARP_SLOTS; i++)
        if (arp_tab[i].ip == ip) { arp_tab[i].used = ++arp_clock; return arp_tab[i].mac; }
    return 0;
}

static void arp_learn(uint32_t ip, const uint8_t *mac)
{
    unsigned i, victim = 0;
    if (!ip || (mac[0] & 1U)) return;
    for (i = 0; i < ARP_SLOTS; i++) {
        if (arp_tab[i].ip == ip) { victim = i; break; }
        if (arp_tab[i].used < arp_tab[victim].used) victim = i;
    }
    arp_tab[victim].ip = ip;
    memcpy(arp_tab[victim].mac, mac, 6);
    arp_tab[victim].used = ++arp_clock;
    if (arp_wait_len && arp_wait_hop == ip) {           /* the waiting packet can go now */
        memcpy(arp_wait_frame, mac, 6);
        if (wifi_send(arp_wait_frame, arp_wait_len) == 0) st.tx++;
        arp_wait_len = 0;
    }
}

static void arp_send(uint32_t op, const uint8_t *tha, uint32_t tip)
{
    uint8_t f[60];
    memset(f, 0, sizeof(f));
    memcpy(f, op == 1 ? mac_bcast : tha, 6);
    memcpy(f + 6, wifi_mac(), 6);
    wr16(f + 12, ETH_TYPE_ARP);
    wr16(f + 14, 1);
    wr16(f + 16, ETH_TYPE_IP);
    f[18] = 6;
    f[19] = 4;
    wr16(f + 20, op);
    memcpy(f + 22, wifi_mac(), 6);
    wr32(f + 28, my_ip);
    if (op == 2) memcpy(f + 32, tha, 6);
    wr32(f + 38, tip);
    if (wifi_send(f, sizeof(f)) == 0) st.tx++;
}

static void arp_input(const uint8_t *e, uint32_t len)
{
    const uint8_t *a = e + ETH_HLEN;
    uint32_t spa, tpa;
    if (len < ETH_HLEN + 28 || rd16(a) != 1 || rd16(a + 2) != ETH_TYPE_IP || a[4] != 6 || a[5] != 4) return;
    spa = rd32(a + 14);
    tpa = rd32(a + 24);
    if (!my_ip) return;
    if (tpa == my_ip || arp_find(spa)) arp_learn(spa, a + 8);
    if (rd16(a + 6) == 1 && tpa == my_ip) arp_send(2, a + 8, spa);
}

/* --- IPv4 ------------------------------------------------------------------- */

/* Send the IPv4 packet whose payload (plen bytes) is already in place at
   frame + PAYLOAD_OFF. `mac` is the next hop's address, or NULL to look it up
   (broadcasts need none). A packet whose next hop is still unknown waits for
   one ARP answer; only the latest such packet is kept. */
static int ip_output(uint32_t dst, uint32_t proto, uint32_t plen, const uint8_t *mac)
{
    uint8_t *ip = frame + ETH_HLEN;
    uint32_t len = PAYLOAD_OFF + plen, hop = 0;
    if (len > FRAME_MAX) return -1;
    if (!mac) {
        if (dst == 0xFFFFFFFFU || (my_mask && dst == (my_ip | ~my_mask))) {
            mac = mac_bcast;
        } else {
            hop = (my_mask && ((dst ^ my_ip) & my_mask)) ? my_gw : dst;
            if (!hop) return -2;                        /* no route */
            mac = arp_find(hop);
        }
    }
    ip[0] = 0x45;
    ip[1] = 0;
    wr16(ip + 2, IP_HLEN + plen);
    wr16(ip + 4, ip_ident++);
    wr16(ip + 6, 0x4000);                               /* don't fragment */
    ip[8] = ip_ttl;
    ip[9] = (uint8_t)proto;
    wr16(ip + 10, 0);
    wr32(ip + 12, my_ip);
    wr32(ip + 16, dst);
    wr16(ip + 10, csum_fold(csum_add(0, ip, IP_HLEN)));
    memcpy(frame + 6, wifi_mac(), 6);
    wr16(frame + 12, ETH_TYPE_IP);
    if (len < 60) { memset(frame + len, 0, 60 - len); len = 60; }
    if (!mac) {
        memcpy(arp_wait_frame, frame, len);
        arp_wait_len = len;
        arp_wait_hop = hop;
        arp_wait_since = timer_ticks();
        st.arp_miss++;
        arp_send(1, 0, hop);
        return 1;
    }
    memcpy(frame, mac, 6);
    if (wifi_send(frame, len)) return -3;
    st.tx++;
    return 0;
}

#define MDNS_ADDR       0xE00000FBU                     /* 224.0.0.251 */
#define MDNS_PORT       5353U

static void icmp_input(uint32_t src, uint32_t dst, const uint8_t *p, uint32_t len, const uint8_t *src_mac);
static void udp_input(uint32_t src, uint32_t dst, const uint8_t *u, uint32_t len, const uint8_t *src_mac);
static void tcp_input(uint32_t src, uint32_t dst, const uint8_t *t, uint32_t len, const uint8_t *src_mac);

static void ip_input(const uint8_t *e, uint32_t len)
{
    const uint8_t *ip = e + ETH_HLEN;
    uint32_t ihl, tot, src, dst;
    int ours;
    if (len < ETH_HLEN + IP_HLEN || (ip[0] >> 4) != 4) return;
    ihl = (uint32_t)(ip[0] & 0x0FU) * 4U;
    tot = rd16(ip + 2);
    if (ihl < IP_HLEN || tot < ihl || ETH_HLEN + tot > len || csum_fold(csum_add(0, ip, ihl)) != 0) { st.bad++; return; }
    if (rd16(ip + 6) & 0x3FFFU) return;                 /* fragments are not reassembled */
    src = rd32(ip + 12);
    dst = rd32(ip + 16);
    ours = dst == my_ip || dst == 0xFFFFFFFFU || (my_ip && my_mask && dst == (my_ip | ~my_mask)) ||
           (!my_ip && ip[9] == PROTO_UDP) ||            /* DHCP answers before we have an address */
           (my_ip && dst == MDNS_ADDR && ip[9] == PROTO_UDP);
    if (!ours) return;
    if (my_ip && dst == my_ip && src && !((src ^ my_ip) & my_mask)) arp_learn(src, e + 6);
    if (ip[9] == PROTO_ICMP) icmp_input(src, dst, ip + ihl, tot - ihl, e + 6);
    else if (ip[9] == PROTO_UDP) udp_input(src, dst, ip + ihl, tot - ihl, e + 6);
    else if (ip[9] == PROTO_TCP) tcp_input(src, dst, ip + ihl, tot - ihl, e + 6);
}

/* --- ICMP echo and ping --------------------------------------------------- */

#define PING_IDENT 0x4850U                              /* "HP" */
static uint32_t ping_target, ping_seq_seen = 0xFFFFFFFFU, ping_sent_at;

static void icmp_input(uint32_t src, uint32_t dst, const uint8_t *p, uint32_t len, const uint8_t *src_mac)
{
    if (len < 8 || csum_fold(csum_add(0, p, len)) != 0) { st.bad++; return; }
    if (p[0] == 8 && dst == my_ip) {                    /* echo request: answer through the same MAC */
        uint8_t *r = frame + PAYLOAD_OFF;
        if (len > FRAME_MAX - PAYLOAD_OFF) return;
        memcpy(r, p, len);
        r[0] = 0;
        wr16(r + 2, 0);
        wr16(r + 2, csum_fold(csum_add(0, r, len)));
        st.echo++;
        ip_output(src, PROTO_ICMP, len, src_mac);
    } else if (p[0] == 0 && rd16(p + 4) == PING_IDENT && src == ping_target) {
        ping_seq_seen = rd16(p + 6);
    }
}

static void poll_network(void)
{
    wifi_poll();
    net_poll();
    watchdog_kick();
    timer_poll();
}

int net_ping(uint32_t ip, uint32_t count)
{
    uint32_t i, k, got = 0;
    char ipb[16];
    if (!net_has_address()) { uart_puts("ping: no address yet (wifi join or wifi ap first)\n"); return -1; }
    net_format_ip(ip, ipb);
    ping_target = ip;
    for (i = 0; i < count; i++) {
        uint8_t *p = frame + PAYLOAD_OFF;
        uint32_t start, us;
        memset(p, 0, 8);
        p[0] = 8;
        wr16(p + 4, PING_IDENT);
        wr16(p + 6, i);
        for (k = 0; k < 32; k++) p[8 + k] = (uint8_t)('a' + k % 23U);
        wr16(p + 2, csum_fold(csum_add(0, p, 40)));
        ping_seq_seen = 0xFFFFFFFFU;
        start = ping_sent_at = timer_ticks();
        ip_output(ip, PROTO_ICMP, 40, 0);
        while (timer_elapsed_ms(start) < 1000 && ping_seq_seen != i) poll_network();
        if (ping_seq_seen == i) {
            us = timer_elapsed_us(ping_sent_at);
            got++;
            uart_printf("reply from %s: seq %u time %u.%03u ms\n", ipb, i, us / 1000U, us % 1000U);
        } else {
            uart_printf("no reply from %s (seq %u)\n", ipb, i);
        }
        while (i + 1 < count && timer_elapsed_ms(start) < 1000) poll_network();
    }
    uart_printf("ping: %u of %u answered\n", got, count);
    return got ? 0 : -1;
}

/* --- UDP --------------------------------------------------------------------- */

static void dhcpc_input(const uint8_t *d, uint32_t len, uint32_t src, const uint8_t *src_mac);
static void dhcps_input(const uint8_t *d, uint32_t len);
static void mdns_input(uint32_t src, uint32_t sport, const uint8_t *d, uint32_t len, const uint8_t *src_mac);
static void mdns_announce_start(void);

/* The payload (dlen bytes) is already at frame + PAYLOAD_OFF + UDP_HLEN. */
static int udp_output(uint32_t dst, uint32_t sport, uint32_t dport, uint32_t dlen, const uint8_t *mac)
{
    uint8_t *u = frame + PAYLOAD_OFF;
    uint32_t ulen = UDP_HLEN + dlen;
    uint16_t c;
    wr16(u, sport);
    wr16(u + 2, dport);
    wr16(u + 4, ulen);
    wr16(u + 6, 0);
    c = csum_fold(csum_add(pseudo_sum(my_ip, dst, PROTO_UDP, ulen), u, ulen));
    wr16(u + 6, c ? c : 0xFFFFU);
    return ip_output(dst, PROTO_UDP, ulen, mac);
}

static void udp_input(uint32_t src, uint32_t dst, const uint8_t *u, uint32_t len, const uint8_t *src_mac)
{
    uint32_t ulen;
    if (len < UDP_HLEN) return;
    ulen = rd16(u + 4);
    if (ulen < UDP_HLEN || ulen > len) { st.bad++; return; }
    if (rd16(u + 6) && csum_fold(csum_add(pseudo_sum(src, dst, PROTO_UDP, ulen), u, ulen)) != 0) { st.bad++; return; }
    if (rd16(u + 2) == 68 && !ap_mode) dhcpc_input(u + UDP_HLEN, ulen - UDP_HLEN, src, src_mac);
    else if (rd16(u + 2) == 67 && ap_mode) dhcps_input(u + UDP_HLEN, ulen - UDP_HLEN);
    else if (rd16(u + 2) == MDNS_PORT && my_ip) mdns_input(src, rd16(u), u + UDP_HLEN, ulen - UDP_HLEN, src_mac);
}

/* --- DHCP (client in station mode, server in access point mode) ------------ */

static uint8_t *dhcp_start_msg(uint32_t op, uint32_t xid, uint32_t flags, uint32_t ciaddr, uint32_t yiaddr,
                               const uint8_t *chaddr)
{
    uint8_t *d = frame + PAYLOAD_OFF + UDP_HLEN;
    memset(d, 0, 300);
    d[0] = (uint8_t)op;
    d[1] = 1;
    d[2] = 6;
    wr32(d + 4, xid);
    wr16(d + 10, flags);
    wr32(d + 12, ciaddr);
    wr32(d + 16, yiaddr);
    memcpy(d + 28, chaddr, 6);
    wr32(d + 236, DHCP_MAGIC);
    return d + 240;
}

static uint8_t *opt32(uint8_t *o, uint8_t code, uint32_t v)
{
    o[0] = code;
    o[1] = 4;
    wr32(o + 2, v);
    return o + 6;
}

static uint32_t dhcp_finish_msg(uint8_t *o)
{
    uint32_t len;
    *o++ = 255;
    len = (uint32_t)(o - (frame + PAYLOAD_OFF + UDP_HLEN));
    return len < 300 ? 300 : len;
}

struct dhcp_opts { int type; uint32_t mask, router, lease, server, reqip; };

static int dhcp_parse(const uint8_t *d, uint32_t len, struct dhcp_opts *o)
{
    uint32_t i = 240;
    memset(o, 0, sizeof(*o));
    if (len < 240 || rd32(d + 236) != DHCP_MAGIC) return -1;
    while (i < len) {
        uint8_t code = d[i], l;
        const uint8_t *v;
        if (code == 255) break;
        if (code == 0) { i++; continue; }
        if (i + 1 >= len) break;
        l = d[i + 1];
        if (i + 2U + l > len) break;
        v = d + i + 2;
        if (code == 53 && l >= 1) o->type = v[0];
        else if (l >= 4) {
            switch (code) {
            case 1:  o->mask = rd32(v); break;
            case 3:  o->router = rd32(v); break;
            case 50: o->reqip = rd32(v); break;
            case 51: o->lease = rd32(v); break;
            case 54: o->server = rd32(v); break;
            default: break;
            }
        }
        i += 2U + l;
    }
    return 0;
}

enum { DHCPC_IDLE, DHCPC_SELECTING, DHCPC_REQUESTING, DHCPC_BOUND, DHCPC_RENEWING };
static int dhcpc_state;
static uint32_t dhcpc_xid, dhcpc_offer, dhcpc_tries, dhcpc_timer, dhcpc_lease, dhcpc_bound_ms;
static const char *const dhcpc_state_names[] = { "idle", "selecting", "requesting", "bound", "renewing" };

static void dhcpc_send(uint32_t type)
{
    static const char host[] = "hpdj-2620";
    const uint8_t *mac = wifi_mac();
    uint8_t *o = dhcp_start_msg(1, dhcpc_xid, 0, dhcpc_state == DHCPC_RENEWING ? my_ip : 0, 0, mac);
    *o++ = 53; *o++ = 1; *o++ = (uint8_t)type;
    *o++ = 61; *o++ = 7; *o++ = 1;
    memcpy(o, mac, 6);
    o += 6;
    if (type == 3 && dhcpc_state == DHCPC_REQUESTING) {
        o = opt32(o, 50, dhcpc_offer);
        o = opt32(o, 54, dhcp_server);
    }
    *o++ = 12; *o++ = (uint8_t)(sizeof(host) - 1);
    memcpy(o, host, sizeof(host) - 1);
    o += sizeof(host) - 1;
    *o++ = 55; *o++ = 3; *o++ = 1; *o++ = 3; *o++ = 51;
    udp_output(0xFFFFFFFFU, 68, 67, dhcp_finish_msg(o), mac_bcast);
    dhcpc_timer = timer_ticks();
}

static void dhcpc_start(void)
{
    const uint8_t *mac = wifi_mac();
    my_ip = 0;
    dhcpc_xid = (timer_ticks() * 2654435761U) ^ ((uint32_t)mac[3] << 16) ^ ((uint32_t)mac[4] << 8) ^ mac[5];
    dhcpc_state = DHCPC_SELECTING;
    dhcpc_tries = 0;
    dhcpc_send(1);
}

static void dhcpc_input(const uint8_t *d, uint32_t len, uint32_t src, const uint8_t *src_mac)
{
    struct dhcp_opts o;
    char a[16], g[16];
    if (len < 240 || d[0] != 2 || rd32(d + 4) != dhcpc_xid || memcmp(d + 28, wifi_mac(), 6) || dhcp_parse(d, len, &o))
        return;
    if (o.type == 2 && dhcpc_state == DHCPC_SELECTING) {              /* OFFER */
        dhcpc_offer = rd32(d + 16);
        dhcp_server = o.server ? o.server : src;
        net_format_ip(dhcpc_offer, a);
        net_format_ip(dhcp_server, g);
        uart_printf("net: DHCP offer %s from %s\n", a, g);
        dhcpc_state = DHCPC_REQUESTING;
        dhcpc_tries = 0;
        dhcpc_send(3);
    } else if (o.type == 5 && (dhcpc_state == DHCPC_REQUESTING || dhcpc_state == DHCPC_RENEWING)) {  /* ACK */
        int renewed = dhcpc_state == DHCPC_RENEWING;
        my_ip = rd32(d + 16);
        my_mask = o.mask ? o.mask : 0xFFFFFF00U;
        my_gw = o.router;
        if (o.server) dhcp_server = o.server;
        dhcpc_lease = o.lease ? o.lease : 3600U;
        if (dhcpc_lease > 604800U) dhcpc_lease = 604800U;
        dhcpc_state = DHCPC_BOUND;
        dhcpc_bound_ms = timer_uptime_ms();
        if (src && !((src ^ my_ip) & my_mask)) arp_learn(src, src_mac);
        if (!renewed) {
            net_format_ip(my_ip, a);
            net_format_ip(my_gw, g);
            uart_printf("net: address %s/%u, gateway %s, lease %u s (DHCP)\n", a, prefix_len(my_mask), g, dhcpc_lease);
            arp_send(1, 0, my_ip);                       /* announce the address */
            if (my_gw && !arp_find(my_gw)) arp_send(1, 0, my_gw);
            mdns_announce_start();
        }
    } else if (o.type == 6 && dhcpc_state != DHCPC_BOUND) {           /* NAK */
        uart_puts("net: the DHCP server refused the address, starting over\n");
        dhcpc_start();
    }
}

static void dhcpc_poll(void)
{
    uint32_t wait;
    switch (dhcpc_state) {
    case DHCPC_SELECTING:
    case DHCPC_REQUESTING:
        wait = dhcpc_tries < 3 ? 2000U << dhcpc_tries : 16000U;
        if (timer_elapsed_ms(dhcpc_timer) < wait) return;
        if (++dhcpc_tries > 4 && dhcpc_state == DHCPC_REQUESTING) {
            dhcpc_state = DHCPC_SELECTING;
            dhcpc_tries = 0;
        }
        dhcpc_send(dhcpc_state == DHCPC_SELECTING ? 1 : 3);
        break;
    case DHCPC_BOUND:
        if (timer_uptime_ms() - dhcpc_bound_ms >= dhcpc_lease * 500U) {   /* T1: renew */
            dhcpc_state = DHCPC_RENEWING;
            dhcpc_send(3);
        }
        break;
    case DHCPC_RENEWING:
        if (timer_uptime_ms() - dhcpc_bound_ms >= dhcpc_lease * 1000U) {
            uart_puts("net: DHCP lease expired, asking again\n");
            dhcpc_start();
        } else if (timer_elapsed_ms(dhcpc_timer) >= 10000U) {
            dhcpc_send(3);
        }
        break;
    default:
        break;
    }
}

static struct { uint8_t mac[6]; uint8_t used; } leases[AP_POOL_SIZE];

static uint32_t pool_ip(unsigned i) { return (AP_ADDR & AP_MASK) | (AP_POOL_FIRST + i); }

static int lease_index(const uint8_t *mac, int alloc)
{
    unsigned i;
    for (i = 0; i < AP_POOL_SIZE; i++)
        if (leases[i].used && !memcmp(leases[i].mac, mac, 6)) return (int)i;
    if (!alloc) return -1;
    for (i = 0; i < AP_POOL_SIZE; i++)
        if (!leases[i].used) { leases[i].used = 1; memcpy(leases[i].mac, mac, 6); return (int)i; }
    return -1;
}

static void dhcps_reply(const uint8_t *req, uint32_t type, uint32_t yiaddr)
{
    uint8_t *o = dhcp_start_msg(2, rd32(req + 4), rd16(req + 10), 0, yiaddr, req + 28);
    wr32(frame + PAYLOAD_OFF + UDP_HLEN + 20, my_ip);                  /* siaddr */
    *o++ = 53; *o++ = 1; *o++ = (uint8_t)type;
    o = opt32(o, 54, my_ip);
    if (type != 6) {
        o = opt32(o, 51, 3600);
        o = opt32(o, 1, AP_MASK);
    }
    udp_output(0xFFFFFFFFU, 67, 68, dhcp_finish_msg(o), mac_bcast);
}

static void dhcps_input(const uint8_t *d, uint32_t len)
{
    struct dhcp_opts o;
    const uint8_t *mac = d + 28;
    uint32_t req;
    char a[16];
    int idx;
    if (len < 240 || d[0] != 1 || d[1] != 1 || d[2] != 6 || dhcp_parse(d, len, &o)) return;
    if (o.type == 1) {                                                  /* DISCOVER */
        idx = lease_index(mac, 1);
        if (idx >= 0) dhcps_reply(d, 2, pool_ip((unsigned)idx));
    } else if (o.type == 3) {                                           /* REQUEST */
        if (o.server && o.server != my_ip) return;                      /* chose another server */
        req = o.reqip ? o.reqip : rd32(d + 12);
        idx = lease_index(mac, 1);
        if (idx >= 0 && req == pool_ip((unsigned)idx)) {
            dhcps_reply(d, 5, req);
            net_format_ip(req, a);
            uart_printf("net: %s leased to %02x:%02x:%02x:%02x:%02x:%02x\n", a, mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
        } else {
            dhcps_reply(d, 6, 0);
        }
    } else if (o.type == 7) {                                           /* RELEASE */
        idx = lease_index(mac, 0);
        if (idx >= 0) leases[idx].used = 0;
    }
}


/* --- mDNS responder: answers A queries for "<mdns_name>.local" ------------- */

static char mdns_name[32] = "deskjet";
static const uint8_t mac_mdns[6] = { 0x01, 0x00, 0x5E, 0x00, 0x00, 0xFB };
static uint32_t mdns_queries, mdns_answers, mdns_announce_left, mdns_announce_timer;

/* Name at msg[off] (labels, compression pointers) as lowercase "a.b.c".
   Returns the offset just after the name, 0 if malformed. */
static uint32_t dns_read_name(const uint8_t *msg, uint32_t len, uint32_t off, char *out, uint32_t out_max)
{
    uint32_t pos = off, end = 0, n = 0, i;
    int jumps = 0;
    for (;;) {
        uint32_t l;
        if (pos >= len) return 0;
        l = msg[pos];
        if (l == 0) { if (!end) end = pos + 1U; break; }
        if ((l & 0xC0U) == 0xC0U) {
            if (pos + 1U >= len || ++jumps > 8) return 0;
            if (!end) end = pos + 2U;
            pos = ((l & 0x3FU) << 8) | msg[pos + 1];
            continue;
        }
        if (l > 63U || pos + 1U + l > len) return 0;
        if (n && n + 1U < out_max) out[n++] = '.';
        for (i = 0; i < l; i++) {
            char c = (char)msg[pos + 1U + i];
            if (c >= 'A' && c <= 'Z') c = (char)(c + 32);
            if (n + 1U < out_max) out[n++] = c;
        }
        pos += 1U + l;
    }
    out[n] = 0;
    return end;
}

static uint8_t *dns_put_host(uint8_t *p)
{
    uint32_t n = (uint32_t)strlen(mdns_name);
    *p++ = (uint8_t)n;
    memcpy(p, mdns_name, n);
    p += n;
    *p++ = 5;
    memcpy(p, "local", 5);
    p += 5;
    *p++ = 0;
    return p;
}

/* One answer: multicast to 224.0.0.251 (cache-flush, TTL 120 s), unicast to
   a querier that asked for it, or a legacy (non-5353 port) reply that repeats
   the question and carries its ID. */
static void mdns_send(uint32_t dst, uint32_t dport, const uint8_t *mac, uint16_t id, int legacy)
{
    uint8_t *d = frame + PAYLOAD_OFF + UDP_HLEN, *p = d + 12;
    memset(d, 0, 12);
    wr16(d, legacy ? id : 0);
    wr16(d + 2, 0x8400);                                /* response, authoritative */
    wr16(d + 4, legacy ? 1 : 0);
    wr16(d + 6, 1);
    if (legacy) {
        p = dns_put_host(p);
        wr16(p, 1); wr16(p + 2, 1); p += 4;             /* A, IN */
    }
    p = dns_put_host(p);
    wr16(p, 1);                                         /* A */
    wr16(p + 2, legacy ? 0x0001U : 0x8001U);            /* IN, cache flush unless legacy */
    wr32(p + 4, legacy ? 10U : 120U);
    wr16(p + 8, 4);
    wr32(p + 10, my_ip);
    p += 14;
    ip_ttl = 255;
    if (udp_output(dst, MDNS_PORT, dport, (uint32_t)(p - d), mac) == 0) mdns_answers++;
    ip_ttl = 64;
}

static void mdns_input(uint32_t src, uint32_t sport, const uint8_t *d, uint32_t len, const uint8_t *src_mac)
{
    char want[48], name[64];
    uint32_t qd, off = 12, i;
    if (len < 12 || (d[2] & 0x80U) || !mdns_name[0]) return;   /* responses and malformed packets */
    snprintf(want, sizeof(want), "%s.local", mdns_name);
    qd = rd16(d + 4);
    for (i = 0; i < qd && i < 16; i++) {
        uint32_t type, cls;
        off = dns_read_name(d, len, off, name, sizeof(name));
        if (!off || off + 4U > len) return;
        type = rd16(d + off);
        cls = rd16(d + off + 2);
        off += 4;
        if (strcmp(name, want) || (cls & 0x7FFFU) != 1 || (type != 1 && type != 255)) continue;
        mdns_queries++;
        if (sport != MDNS_PORT) mdns_send(src, sport, src_mac, rd16(d), 1);
        else if (cls & 0x8000U) mdns_send(src, MDNS_PORT, src_mac, 0, 0);
        else mdns_send(MDNS_ADDR, MDNS_PORT, mac_mdns, 0, 0);
        return;
    }
}

/* Two unsolicited answers, a second apart, after the address changed. */
static void mdns_announce_start(void)
{
    mdns_announce_left = 2;
    mdns_announce_timer = timer_ticks() - 1000U * g_ticks_per_ms;
}

static void mdns_poll(void)
{
    if (!mdns_announce_left || !my_ip || timer_elapsed_ms(mdns_announce_timer) < 1000U) return;
    mdns_announce_left--;
    mdns_announce_timer = timer_ticks();
    mdns_send(MDNS_ADDR, MDNS_PORT, mac_mdns, 0, 0);
}

/* --- TCP + HTTP control server ----------------------------------------------
 *
 * One request per connection ("Connection: close"): the request has to
 * arrive in one segment, the answer goes out in MSS-sized segments with FIN.
 * The exception is POST /api/update, whose body (an application image) is
 * received segment by segment, in order, into the XMODEM staging buffer and
 * then written to the inactive slot. Commands from the web shell run in the
 * normal shell; their output is taken from the web log (a copy of everything
 * the UART prints). While a request is handled, new requests are dropped
 * (the client sends them again), because a command may poll the link.
 */

#define TCP_FIN 0x01U
#define TCP_SYN 0x02U
#define TCP_RST 0x04U
#define TCP_PSH 0x08U
#define TCP_ACK 0x10U
#define TCP_MSS 1360U
#define TCP_WINDOW 8192U
#define TN_PORT    23U                                  /* telnet shell, see tn_input() */
static void tn_input(uint32_t src, uint16_t sport, uint32_t seq, uint32_t ack, uint8_t flags,
                     const uint8_t *data, uint32_t dlen, const uint8_t *mac);

static int server_enabled = 1;
static uint16_t server_port = 80;
static uint32_t http_requests, http_motor_cmds, http_shell_cmds;
static char web_token[33] = WEB_TOKEN;
static int http_busy;
static int reboot_pending, ramrun_pending;
static uint32_t reboot_since;

static struct {
    uint32_t src;
    uint16_t sport;
    uint32_t req_seq;
    uint32_t resp_seq;
    uint32_t client_next;
    uint32_t resp_len;
    uint32_t tx_ticks;
} last_http;

static char http_buf[16384];
static char json_buf[12288];

/* Web log: the last WEBLOG_SIZE bytes the UART printed (without CR). */
#define WEBLOG_SIZE 8192U
static char weblog[WEBLOG_SIZE];
static uint32_t weblog_total;

static void weblog_tap(char c)
{
    if (c == '\r') return;
    weblog[weblog_total % WEBLOG_SIZE] = c;
    weblog_total++;
}

void net_log_init(void) { uart_set_tap(weblog_tap); }

/* Application image upload (POST /api/update). */
#define UPLOAD_IDLE_MS 10000U
static struct {
    int state;                  /* 0 idle, 1 receiving, 2 answered */
    uint32_t src;
    uint16_t sport, dport;
    uint8_t mac[6];
    uint32_t next_seq, our_seq;
    uint32_t expect, got;
    uint32_t last_ticks;
    int reboot;
    int ram;                    /* POST /api/run: run from RAM instead of writing a slot */
} up;
static uint32_t uploads_ok;

static int tcp_output(uint32_t dst, uint16_t sport, uint16_t dport, uint32_t seq, uint32_t ack, uint8_t flags,
                      const uint8_t *opt, uint32_t opt_len, const uint8_t *data, uint32_t dlen, const uint8_t *mac)
{
    uint8_t *t = frame + PAYLOAD_OFF;
    uint32_t hlen = TCP_HLEN + opt_len;
    uint32_t tlen = hlen + dlen;
    if (PAYLOAD_OFF + tlen > FRAME_MAX) return -1;
    wr16(t + 0, sport);
    wr16(t + 2, dport);
    wr32(t + 4, seq);
    wr32(t + 8, ack);
    t[12] = (uint8_t)((hlen / 4U) << 4);
    t[13] = flags;
    wr16(t + 14, TCP_WINDOW);
    wr16(t + 16, 0);
    wr16(t + 18, 0);
    if (opt_len) memcpy(t + TCP_HLEN, opt, opt_len);
    if (dlen) memcpy(t + hlen, data, dlen);
    wr16(t + 16, csum_fold(csum_add(pseudo_sum(my_ip, dst, PROTO_TCP, tlen), t, tlen)));
    return ip_output(dst, PROTO_TCP, tlen, mac);
}

/* Send http_buf[0..len) as the answer on a connection, ending with FIN, and
   remember it so lost segments can be sent again (see tcp_input). */
static void http_send(uint32_t src, uint16_t dport, uint16_t sport, uint32_t req_seq, uint32_t resp_seq,
                      uint32_t client_next, uint32_t len, const uint8_t *mac)
{
    uint32_t off = 0;
    if (len >= sizeof(http_buf)) len = sizeof(http_buf) - 1U;
    last_http.src = src;
    last_http.sport = sport;
    last_http.req_seq = req_seq;
    last_http.resp_seq = resp_seq;
    last_http.client_next = client_next;
    last_http.resp_len = len;
    last_http.tx_ticks = timer_ticks();
    while (off < len) {
        uint32_t chunk = len - off;
        uint8_t fl = TCP_PSH | TCP_ACK;
        if (chunk > TCP_MSS) chunk = TCP_MSS;
        if (off + chunk >= len) fl |= TCP_FIN;
        if (off > 0) timer_delay_ms(2);
        tcp_output(src, dport, sport, resp_seq + off, client_next, fl, 0, 0, (const uint8_t *)http_buf + off, chunk, mac);
        off += chunk;
    }
}

static int http_query_param(const char *q, const char *key, char *val, uint32_t val_max)
{
    uint32_t klen = (uint32_t)strlen(key);
    if (!q || !val_max) return -1;
    while (*q) {
        if (!strncmp(q, key, klen) && q[klen] == '=') {
            uint32_t i = 0;
            q += klen + 1U;
            while (*q && *q != '&' && *q != ' ' && *q != '\r' && *q != '\n' && i + 1U < val_max)
                val[i++] = *q++;
            val[i] = 0;
            return 0;
        }
        while (*q && *q != '&' && *q != ' ' && *q != '\r' && *q != '\n') q++;
        if (*q == '&') q++; else break;
    }
    val[0] = 0;
    return -1;
}

static int hexval(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static void url_decode(char *s)
{
    char *o = s;
    while (*s) {
        if (*s == '+') { *o++ = ' '; s++; }
        else if (*s == '%' && hexval(s[1]) >= 0 && hexval(s[2]) >= 0) {
            *o++ = (char)(hexval(s[1]) * 16 + hexval(s[2]));
            s += 3;
        } else {
            *o++ = *s++;
        }
    }
    *o = 0;
}

static int token_ok(const char *query)
{
    char t[40];
    if (http_query_param(query, "token", t, sizeof(t))) return 0;
    url_decode(t);
    return web_token[0] && !strcmp(t, web_token);
}

/* JSON-escaped copy of the web log from position `from` to the current end,
   as much as fits. *next = the position after the last byte copied. */
static uint32_t json_log_text(char *out, uint32_t max, uint32_t from, uint32_t *next)
{
    uint32_t n = 0, i = from;
    if (weblog_total - from > WEBLOG_SIZE - 1U) i = weblog_total - (WEBLOG_SIZE - 1U);
    for (; i < weblog_total && n + 8U < max; i++) {
        char c = weblog[i % WEBLOG_SIZE];
        if (c == '"' || c == '\\') { out[n++] = '\\'; out[n++] = c; }
        else if (c == '\n') { out[n++] = '\\'; out[n++] = 'n'; }
        else if (c == '\t') { out[n++] = '\\'; out[n++] = 't'; }
        else if ((unsigned char)c < 0x20 || (unsigned char)c >= 0x7F) out[n++] = '?';
        else out[n++] = c;
    }
    out[n] = 0;
    *next = i;
    return n;
}

static uint32_t http_reply(char *out, uint32_t out_max, int code, const char *type, const char *body)
{
    const char *reason = code == 200 ? "OK" : code == 400 ? "Bad Request" : code == 403 ? "Forbidden" :
                         code == 404 ? "Not Found" : code == 409 ? "Conflict" : code == 413 ? "Payload Too Large" :
                         "Internal Server Error";
    return (uint32_t)snprintf(out, out_max,
        "HTTP/1.1 %d %s\r\nContent-Type: %s\r\nAccess-Control-Allow-Origin: *\r\n"
        "Cache-Control: no-store\r\nConnection: close\r\nContent-Length: %u\r\n\r\n%s",
        code, reason, type, (uint32_t)strlen(body), body);
}

static uint32_t http_json(char *out, uint32_t out_max, int code, const char *body)
{
    return http_reply(out, out_max, code, "application/json", body);
}

static uint32_t http_build_status_json(char *buf, uint32_t max, int ok, const char *msg)
{
    struct motor_telemetry t;
    char ipb[16];
    int wauto, i, n;
    uint32_t lost, rejoined, len;
    motor_api_telemetry(&t);
    wifi_auto_info(&wauto, &lost, &rejoined);
    net_format_ip(my_ip, ipb);
    len = (uint32_t)snprintf(buf, max,
        "{\"ok\":%s,\"msg\":\"%s\",\"uptime_ms\":%u,\"rssi\":%d,"
        "\"inited\":%d,\"active_motor\":%d,\"active_mode\":%d,\"active_pwm\":%u,\"remaining_ms\":%u,"
        "\"door_open\":%d,\"sensors\":%u,\"http_reqs\":%u,\"motor_cmds\":%u,"
        "\"mcasic\":{\"id\":\"%04x\",\"st\":\"%04x\",\"r20\":\"%04x\",\"r30\":\"%04x\",\"r3b\":\"%04x\","
        "\"ctl\":[\"%04x\",\"%04x\",\"%04x\"],\"mst\":[\"%04x\",\"%04x\",\"%04x\"],"
        "\"enc\":[\"%04x\",\"%04x\",\"%04x\",\"%04x\"]},"
        "\"cnt\":[\"%08x\",\"%08x\",\"%08x\"],\"pos\":[%d,%d,%d],\"stall\":%d,"
        "\"ver\":\"%s\",\"seq\":%u,\"slot\":\"%c\",\"host\":\"%s.local\",\"ip\":\"%s\","
        "\"wauto\":%d,\"lost\":%u,\"rejoined\":%u,\"log\":%u,\"up\":[%u,%u,%u],\"sens\":{",
        ok ? "true" : "false", msg ? msg : "ok", timer_uptime_ms(), wifi_last_rssi(),
        t.inited, t.active_motor, t.active_mode, t.active_pwm, t.remaining_ms,
        t.door_open, t.sensor_bits, http_requests, http_motor_cmds,
        t.mcasic_id, t.mcasic_status, t.reg_20, t.reg_30, t.gpio_3b,
        t.motor_ctl[0], t.motor_ctl[1], t.motor_ctl[2],
        t.motor_st[0], t.motor_st[1], t.motor_st[2],
        t.enc[0], t.enc[1], t.enc[2], t.enc[3],
        t.pos_cnt[0], t.pos_cnt[1], t.pos_cnt[2], (int)t.pos[0], (int)t.pos[1], (int)t.pos[2], t.stall_motor,
        FW_VERSION_STR, (uint32_t)FW_SEQUENCE, g_current_slot >= 0 ? slot_letter(g_current_slot) : '?',
        mdns_name, ipb, wauto, lost, rejoined, weblog_total,
        up.state == 1 ? up.got : 0U, up.state == 1 ? up.expect : 0U, uploads_ok);
    n = sensor_api_count();
    for (i = 0; i < n && len + 40U < max; i++)
        len += (uint32_t)snprintf(buf + len, max - len, "%s\"%s\":%d", i ? "," : "", sensor_api_name(i),
                                  sensor_api_active(i));
    if (len + 4U < max) len += (uint32_t)snprintf(buf + len, max - len, "}}\n");
    return len;
}

static const char http_index_html[] =
    "<!doctype html><html><head><meta charset=utf-8>"
    "<meta name=viewport content='width=device-width,initial-scale=1'>"
    "<title>HP DeskJet 2620 Control</title><style>"
    "*{box-sizing:border-box;font-family:system-ui,sans-serif}"
    "body{margin:0;padding:12px;background:#0f172a;color:#f8fafc;max-width:760px;margin-inline:auto}"
    "h1{font-size:1.2rem;margin:0 0 8px;display:flex;justify-content:space-between;align-items:center;gap:8px}"
    ".b{background:#1e293b;border:1px solid #334155;border-radius:9px;padding:10px;margin-bottom:8px}"
    ".r{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:6px}"
    "button{background:#2563eb;color:#fff;border:0;border-radius:6px;padding:9px 12px;font-weight:600;cursor:pointer;user-select:none;touch-action:none}"
    "button:active{background:#1d4ed8}.stop{background:#dc2626;flex:1}.init{background:#059669}.alt{background:#475569;padding:5px 9px;font-size:.82rem}"
    ".m{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:6px}"
    "input{background:#020617;color:#f8fafc;border:1px solid #334155;border-radius:6px;padding:6px;min-width:0}"
    "code,pre{font-family:monospace;background:#020617;padding:3px 6px;border-radius:4px;font-size:.8rem}"
    "pre{white-space:pre-wrap;word-break:break-all;max-height:240px;overflow:auto;margin:6px 0 0}"
    ".on{color:#4ade80}.off{color:#64748b}"
    "</style></head><body>"
    "<h1><span>HP DeskJet 2620</span><code id=st>connecting...</code></h1>"
    "<div class=b><div class=r>"
    "<button class=init onclick=\"cmd('act=init')\">INIT MCASIC</button>"
    "<button class=stop onclick=stopM()>STOP ALL (Space)</button>"
    "<button class=alt onclick=poll()>Refresh</button>"
    "</div><div class=r>"
    "<label>PWM <b id=pv>350</b> <input id=pwm type=range min=100 max=600 step=10 value=350 oninput='pv.textContent=this.value'></label>"
    "<label>Pulse <b id=mv>150</b>ms <input id=ms type=range min=25 max=500 step=25 value=150 oninput='mv.textContent=this.value'></label>"
    "</div></div>"
    "<div id=mslot></div>"
    "<div class=b><b>Sensors</b> <span id=sens></span></div>"
    "<div class=b><b>Front panel</b> <span id=pst class=off></span>"
    "<div class=r><svg id=pnl width=320 height=96 viewBox='0 0 320 96'></svg></div><div class=r id=pbt></div></div>"
    "<div class=b><b>Panel LCD</b> <span id=lcdb></span></div>"
    "<div class=b><b>Shell</b><div class=r><input id=tk type=password placeholder=token size=9>"
    "<input id=cl placeholder='command, Enter to run' style='flex:1'></div><pre id=out></pre></div>"
    "<div class=b><b>Log</b><pre id=log></pre></div>"
    "<div class=b><b>Update application</b><div class=r><input id=fi type=file accept='.img'>"
    "<button class=alt onclick=upl()>Upload + reboot</button></div><pre id=upo>"
    "uses the token above; the image goes to the other slot</pre></div>"
    "<div class=b><code id=tel>waiting for telemetry...</code></div>"
    "<script>"
    "let ht=0,lf=-1;const G=id=>document.getElementById(id);"
    "try{G('tk').value=localStorage.getItem('tk')||''}catch(e){}"
    "function tok(){let t=G('tk').value;try{localStorage.setItem('tk',t)}catch(e){}return encodeURIComponent(t)}"
    "[['a','Motor A: Paper Feed (0x31)','FWD','REV'],['b','Motor B: Printhead Carriage (0x33)','LEFT (FWD)','RIGHT (REV)'],['c','Motor C: Scanner (0x35)','FWD','REV']].forEach(([m,t,f,r])=>{"
    "let x=m=='a'?'':`<div class=r>position <b id=p${m}>?</b><button class=alt onclick=\"cmd('act=home&m=${m}')\">Home</button>"
    "<button class=alt onclick=\"cmd('act=range&m=${m}')\">Range</button><input id=g${m} size=7 placeholder=position>"
    "<button class=alt onclick=\"cmd('act=goto&m=${m}&pos='+G('g${m}').value)\">Go</button></div>`;"
    "G('mslot').innerHTML+=`<div class=b><b>${t}</b><div class=m><button onpointerdown=\"hold('${m}','fwd')\" onpointerup=stopM() onpointerleave=stopM()>HOLD ${f}</button><button onpointerdown=\"hold('${m}','rev')\" onpointerup=stopM() onpointerleave=stopM()>HOLD ${r}</button><button class=alt onclick=\"pulse('${m}','fwd')\">Pulse FWD</button><button class=alt onclick=\"pulse('${m}','rev')\">Pulse REV</button></div>${x}</div>`});"
    "async function api(u,o){try{let r=await fetch(u,o);let j=await r.json();if(j.uptime_ms!==undefined)render(j);return j}catch(e){G('st').textContent='offline'}}"
    "function cmd(q){return api('/api/motor?'+q)}"
    "function pulse(m,d){cmd('act=move&m='+m+'&dir='+d+'&pwm='+G('pwm').value+'&ms='+G('ms').value)}"
    "function hold(m,d){clearInterval(ht);const f=()=>cmd('act=move&m='+m+'&dir='+d+'&pwm='+G('pwm').value+'&ms=280');f();ht=setInterval(f,130)}"
    "function stopM(){if(ht){clearInterval(ht);ht=0}cmd('act=stop')}"
    "function poll(){api('/api/status')}"
    "function render(j){let m=j.mcasic||{};"
    "G('st').textContent=(j.inited?'INIT OK':'UNINIT')+' | '+j.rssi+'dBm | '+(j.door_open?'DOOR OPEN':'DOOR CLOSED')+' | '+j.msg;"
    "G('pb').textContent=j.pos[1];G('pc').textContent=j.pos[2];"
    "G('sens').innerHTML=Object.entries(j.sens||{}).map(([k,v])=>`<span class=${v?'on':'off'}>${k}:${v?'on':'off'}</span>`).join(' ');"
    "if(j.up[1])G('upo').textContent='receiving '+j.up[0]+' / '+j.up[1];"
    "G('tel').textContent='v'+j.ver+' seq '+j.seq+' slot '+j.slot+' | '+j.host+' '+j.ip+' | rejoin '+(j.wauto?'on':'off')+' lost '+j.lost+' rejoined '+j.rejoined+"
    "' | mcasic 20='+m.r20+' 3b='+m.r3b+' ctl='+(m.ctl||[]).join('/')+' cnt='+(j.cnt||[]).join('/');"
    "if(j.log!==lf)pullLog()}"
    "async function pullLog(){try{let j=await (await fetch('/api/log?from='+(lf<0?0:lf))).json();lf=j.next;let L=G('log');"
    "L.textContent=(L.textContent+j.text).slice(-6000);L.scrollTop=L.scrollHeight}catch(e){}}"
    "G('cl').onkeydown=async e=>{if(e.key!='Enter')return;let v=G('cl').value;G('out').textContent='> '+v+' ...';"
    "try{let j=await (await fetch('/api/cmd?token='+tok()+'&c='+encodeURIComponent(v))).json();G('out').textContent='> '+v+'\\n'+(j.out!==undefined?j.out:j.msg)}"
    "catch(x){G('out').textContent='> '+v+'\\nno answer'}};"
    "async function upl(){let f=G('fi').files[0];if(!f)return;G('upo').textContent='uploading '+f.size+' bytes...';"
    "try{let j=await (await fetch('/api/update?reboot=1&token='+tok(),{method:'POST',body:f})).json();G('upo').textContent=JSON.stringify(j)}"
    "catch(x){G('upo').textContent='upload failed: '+x}}"
    "'0123456789AbCdEFPU-'.split('').forEach(c=>{let b=document.createElement('button');b.className='alt';b.style.margin='2px';b.textContent=c;b.onclick=()=>api('/api/lcd?ch='+c);G('lcdb').appendChild(b)});"
    "window.onkeydown=e=>{if(e.target.tagName=='INPUT')return;if(e.code==='Space'||e.code==='Escape'){e.preventDefault();stopM()}};"
    "function sg(x,y,w,h,on){return `<rect x=${x} y=${y} width=${w} height=${h} rx=2 fill=${on?'#fbbf24':'#334155'} />`}"
    "function tx(x,y,t,on){return `<text x=${x} y=${y} font-size=10 fill=${on?'#fbbf24':'#475569'}>${t}</text>`}"
    "function cl(x,y,on,c){return `<circle cx=${x} cy=${y} r=6 fill=${on?c:'#334155'} />`}"
    "function drawPanel(p){let lo=parseInt(p.lo,16)^0xF00,hi=parseInt(p.hi,16),b=n=>n<32?(lo>>>n)&1:(hi>>>(n-32))&1,s='';"
    "s+=sg(16,8,26,5,b(39))+sg(42,13,5,28,b(38))+sg(42,46,5,28,b(37))+sg(16,74,26,5,b(36))+sg(11,46,5,28,b(33))+sg(11,13,5,28,b(35))+sg(16,41,26,5,b(34));"
    "[24,28,29,30,31].forEach((n,i)=>s+=sg(64+i*9,70-i*12,6,9+i*12,b(n)));"
    "[[16,18,17],[27,22,26]].forEach((g,j)=>{g.forEach((n,i)=>s+=sg(120+j*22,58-i*20,14,16,b(n)));s+=`<rect x=${118+j*22} y=16 width=18 height=62 fill=none stroke=${b(j?23:19)?'#fbbf24':'#334155'} />`});"
    "[['Wi-Fi',12],['paper',13],['!',14],['E',15],['Direct',20],['ink',21],['drop',25],['w!',32]].forEach(([t,n],i)=>s+=tx(172+(i%2)*40,18+(i>>1)*16,t,b(n)));"
    "s+=cl(262,20,b(8),'#f97316')+tx(272,24,'ink',b(8))+cl(262,42,b(9),'#f97316')+tx(272,46,'resume',b(9))+cl(262,64,b(11),'#f97316')+tx(272,68,'info',b(11));"
    "s+=cl(16,90,p.power_led,'#22c55e')+tx(26,93,'power',p.power_led)+cl(72,90,p.wifi_led,'#3b82f6')+tx(82,93,'wireless',p.wifi_led);"
    "G('pnl').innerHTML=s;G('pst').textContent=['menu off','menu running','menu paused (ui on)','soft off'][p.ui]+(p.pressed?' | pressed '+p.pressed:'')}"
    "async function ppoll(){try{drawPanel(await (await fetch('/api/panel')).json())}catch(e){}}"
    "['power','wireless','info','cancel','resume','copy_black','copy_color'].forEach(n=>{let b=document.createElement('button');b.className='alt';b.textContent=n;"
    "b.onclick=async()=>{try{let j=await (await fetch('/api/panel?press='+n)).json();if(!j.ok)G('pst').textContent=j.msg;}catch(e){}};G('pbt').appendChild(b)});"
    "ppoll();setInterval(ppoll,600);"
    "poll();setInterval(()=>{if(!ht)poll()},1500);"
    "</script></body></html>";

/* Commands that wait for UART keys or run for long are refused from the web. */
static int web_cmd_refused(const char *line)
{
    static const char *const first[] = { "update", "bl", "run", "motorsAndButtonsTest", 0 };
    static const char *const any[] = { "watch", "walk", "wait", 0 };
    char w[24];
    const char *p = line;
    int i, word = 0;
    while (*p) {
        uint32_t n = 0;
        while (*p == ' ') p++;
        if (!*p) break;
        while (*p && *p != ' ' && n + 1U < sizeof(w)) w[n++] = *p++;
        while (*p && *p != ' ') p++;
        w[n] = 0;
        for (i = 0; word == 0 && first[i]; i++) if (!strcmp(w, first[i])) return 1;
        for (i = 0; any[i]; i++) if (!strcmp(w, any[i])) return 1;
        word++;
    }
    return 0;
}

static uint32_t http_handle_request(const char *req, uint32_t req_len, char *out, uint32_t out_max)
{
    char path[128], query[384];
    uint32_t i = 0, p = 0, q = 0, blen;
    while (i < req_len && req[i] != ' ') i++;
    if (i < req_len) i++;
    while (i < req_len && req[i] != ' ' && req[i] != '?' && req[i] != '\r' && req[i] != '\n' && p + 1U < sizeof(path))
        path[p++] = req[i++];
    path[p] = 0;
    if (i < req_len && req[i] == '?') {
        i++;
        while (i < req_len && req[i] != ' ' && req[i] != '\r' && req[i] != '\n' && q + 1U < sizeof(query))
            query[q++] = req[i++];
    }
    query[q] = 0;

    if (!strcmp(path, "/") || !strcmp(path, "/index.html")) {
        blen = (uint32_t)(sizeof(http_index_html) - 1U);
        return (uint32_t)snprintf(out, out_max,
            "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
            "Cache-Control: no-store\r\nConnection: close\r\nContent-Length: %u\r\n\r\n%s",
            blen, http_index_html);
    }
    if (!strcmp(path, "/favicon.ico"))
        return (uint32_t)snprintf(out, out_max, "HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n");
    if (!strcmp(path, "/api/status")) {
        http_build_status_json(json_buf, sizeof(json_buf), 1, "ok");
        return http_json(out, out_max, 200, json_buf);
    }
    if (!strcmp(path, "/api/motor")) {
        char act[16], mstr[8], dstr[8], pwm_s[16], ms_s[16], pos_s[16];
        int ok = 0;
        const char *msg = "invalid action";
        uint32_t pwm = 0, ms = 150U;
        int m_idx = -1, mode = 0, rc;
        http_query_param(query, "act", act, sizeof(act));
        http_query_param(query, "m", mstr, sizeof(mstr));
        http_query_param(query, "dir", dstr, sizeof(dstr));
        if (!http_query_param(query, "pwm", pwm_s, sizeof(pwm_s))) parse_u32(pwm_s, &pwm);
        if (!http_query_param(query, "ms", ms_s, sizeof(ms_s))) parse_u32(ms_s, &ms);
        if (mstr[0] == 'a' || mstr[0] == 'A' || mstr[0] == '0') m_idx = 0;
        else if (mstr[0] == 'b' || mstr[0] == 'B' || mstr[0] == '1') m_idx = 1;
        else if (mstr[0] == 'c' || mstr[0] == 'C' || mstr[0] == '2') m_idx = 2;
        if (!strcmp(dstr, "fwd") || !strcmp(dstr, "1")) mode = 1;
        else if (!strcmp(dstr, "rev") || !strcmp(dstr, "2")) mode = 2;
        if (!strcmp(act, "init")) {
            ok = (motor_api_init() == 0);
            msg = ok ? "mcasic initialized" : "mcasic init failed";
        } else if (!strcmp(act, "stop") || !strcmp(act, "off")) {
            motor_api_stop();
            ok = 1;
            msg = "motors stopped";
        } else if (!strcmp(act, "move") || !strcmp(act, "pulse")) {
            rc = motor_api_drive(m_idx, mode, pwm ? pwm : 350U, ms);
            ok = (rc == 0);
            msg = rc == 0 ? "driving" : rc == -2 ? "hold limit reached" :
                  rc == -3 ? "stalled at the end stop: drive the other way" : "drive failed";
        } else if (!strcmp(act, "home") || !strcmp(act, "range")) {
            if (!mode) mode = m_idx == 1 ? 1 : 2;
            if (!strcmp(act, "home")) {
                rc = motor_api_home(m_idx, mode, pwm);
                msg = rc == 0 ? "homed: position 0 at the end stop" : "homing failed (motor a has no encoder)";
            } else {
                int32_t span = 0;
                rc = motor_api_range(m_idx, mode, pwm, &span);
                msg = rc == 0 ? "travel measured, see the log" : "range failed";
            }
            ok = rc == 0;
        } else if (!strcmp(act, "goto")) {
            uint32_t mag = 0;
            int neg;
            http_query_param(query, "pos", pos_s, sizeof(pos_s));
            neg = pos_s[0] == '-';
            if (parse_u32(pos_s + neg, &mag)) {
                msg = "pos=<counts> expected";
            } else {
                rc = motor_api_goto(m_idx, neg ? -(int32_t)mag : (int32_t)mag, pwm);
                ok = rc == 0;
                msg = rc == 0 ? "at the position" : rc == -3 ? "blocked on the way" : "goto failed";
            }
        }
        http_motor_cmds++;
        http_build_status_json(json_buf, sizeof(json_buf), ok, msg);
        return http_json(out, out_max, 200, json_buf);
    }
    if (!strcmp(path, "/api/lcd")) {
        char ch_s[8], w_s[8], c_s[8], b_s[8];
        uint32_t w = 0, c = 0, b = 0;
        char ch = ' ';
        int ok;
        if (!http_query_param(query, "ch", ch_s, sizeof(ch_s)) && ch_s[0]) ch = ch_s[0];
        if (!http_query_param(query, "wifi", w_s, sizeof(w_s))) parse_u32(w_s, &w);
        if (!http_query_param(query, "color", c_s, sizeof(c_s))) parse_u32(c_s, &c);
        if (!http_query_param(query, "black", b_s, sizeof(b_s))) parse_u32(b_s, &b);
        ok = (panel_api_set(ch, (int)w, (int)c, (int)b, 0) == 0);
        http_build_status_json(json_buf, sizeof(json_buf), ok, ok ? "lcd updated" : "invalid lcd char");
        return http_json(out, out_max, 200, json_buf);
    }
    if (!strcmp(path, "/api/panel")) {
        char b_s[20], ms_s[12];
        uint32_t ms = 300, mask = 0, i;
        int rc = 0, st = ui_state();
        if (!http_query_param(query, "press", b_s, sizeof(b_s))) {
            for (i = 0; i < 18; i++) {
                const char *nm = panel_button_name((int)i);
                if (nm && !strcmp(nm, b_s)) mask = 1U << i;
            }
            if (!http_query_param(query, "ms", ms_s, sizeof(ms_s))) parse_u32(ms_s, &ms);
            rc = mask ? ui_inject(mask, ms) : -2;
        }
        snprintf(json_buf, sizeof(json_buf),
                 "{\"ok\":%s,\"lo\":\"%08x\",\"hi\":\"%08x\",\"pressed\":%u,\"frames\":%u,\"ui\":%d,"
                 "\"power_led\":%d,\"wifi_led\":%d%s}",
                 rc == 0 ? "true" : "false", g_panel_last_lo, g_panel_last_hi,
                 (~g_panel_last_raw) & PANEL_BUTTON_MASK, g_panel_frames, st,
                 (GPIO_DIR(GPIO_PIN_POWER_LED) & 1) && !(GPIO_OUTPUT(GPIO_PIN_POWER_LED) & 1),
                 (GPIO_DIR(GPIO_PIN_WIFI_LED) & 1) && !(GPIO_OUTPUT(GPIO_PIN_WIFI_LED) & 1),
                 rc == -1 ? ",\"msg\":\"panel menu not running ('ui on')\"" :
                 rc == -2 ? ",\"msg\":\"unknown button\"" : "");
        return http_json(out, out_max, 200, json_buf);
    }
    if (!strcmp(path, "/api/log")) {
        char f_s[16];
        uint32_t from = 0, next, n;
        if (!http_query_param(query, "from", f_s, sizeof(f_s))) parse_u32(f_s, &from);
        if (from > weblog_total) from = weblog_total;
        n = (uint32_t)snprintf(json_buf, sizeof(json_buf), "{\"next\":");
        {
            static char text[7000];
            json_log_text(text, sizeof(text), from, &next);
            snprintf(json_buf + n, sizeof(json_buf) - n, "%u,\"text\":\"%s\"}", next, text);
        }
        return http_json(out, out_max, 200, json_buf);
    }
    if (!strcmp(path, "/api/cmd")) {
        static char line[256], text[7000];
        uint32_t from, next;
        int rc;
        if (!token_ok(query)) return http_json(out, out_max, 403, "{\"ok\":false,\"msg\":\"wrong or missing token\"}");
        if (http_query_param(query, "c", line, sizeof(line)) || !line[0])
            return http_json(out, out_max, 400, "{\"ok\":false,\"msg\":\"c=<command> expected\"}");
        url_decode(line);
        if (web_cmd_refused(line))
            return http_json(out, out_max, 400, "{\"ok\":false,\"msg\":\"interactive or long command: use the UART\"}");
        http_shell_cmds++;
        uart_printf("web> %s\n", line);
        from = weblog_total;
        rc = app_shell_execute(line);
        json_log_text(text, sizeof(text), from, &next);
        snprintf(json_buf, sizeof(json_buf), "{\"ok\":%s,\"rc\":%d,\"out\":\"%s\"}", rc == 0 ? "true" : "false", rc, text);
        return http_json(out, out_max, 200, json_buf);
    }
    if (!strcmp(path, "/api/eeprom")) {
        /* Read-only: the settings EEPROM (M24512, 64 KiB) as hex, up to 2 KiB per
           request. Token required: it holds the serial number and network data. */
        static uint8_t eb[2048];
        char o_s[12], l_s[12];
        uint32_t off = 0, len = 2048, i, n;
        if (!token_ok(query)) return http_json(out, out_max, 403, "{\"ok\":false,\"msg\":\"wrong or missing token\"}");
        if (!http_query_param(query, "off", o_s, sizeof(o_s))) parse_u32(o_s, &off);
        if (!http_query_param(query, "len", l_s, sizeof(l_s))) parse_u32(l_s, &len);
        if (off >= 0x10000U || len == 0 || len > sizeof(eb) || off + len > 0x10000U)
            return http_json(out, out_max, 400, "{\"ok\":false,\"msg\":\"off + len within 64 KiB, len 1..2048\"}");
        for (i = 0; i < len; i += 256U) {
            uint32_t chunk = len - i < 256U ? len - i : 256U;
            if (i2c_read(I2C_ADDR_EEPROM, (uint16_t)(off + i), 1, eb + i, chunk) != I2C_OK)
                return http_json(out, out_max, 500, "{\"ok\":false,\"msg\":\"eeprom read failed\"}");
        }
        n = (uint32_t)snprintf(json_buf, sizeof(json_buf), "{\"ok\":true,\"off\":%u,\"len\":%u,\"hex\":\"", off, len);
        for (i = 0; i < len && n + 4U < sizeof(json_buf); i++)
            n += (uint32_t)snprintf(json_buf + n, sizeof(json_buf) - n, "%02x", eb[i]);
        snprintf(json_buf + n, sizeof(json_buf) - n, "\"}");
        return http_json(out, out_max, 200, json_buf);
    }
    if (!strcmp(path, "/api/chipid")) {
        /* The SoC ID words HP derives its NVM key from (analysis/nvm.md). */
        if (!token_ok(query)) return http_json(out, out_max, 403, "{\"ok\":false,\"msg\":\"wrong or missing token\"}");
        snprintf(json_buf, sizeof(json_buf), "{\"ok\":true,\"words\":[\"%08x\",\"%08x\",\"%08x\",\"%08x\"]}",
                 REG32(0xBD040010U), REG32(0xBD040014U), REG32(0xBD040018U), REG32(0xBD04001CU));
        return http_json(out, out_max, 200, json_buf);
    }
    if (!strcmp(path, "/api/reboot")) {
        if (!token_ok(query)) return http_json(out, out_max, 403, "{\"ok\":false,\"msg\":\"wrong or missing token\"}");
        reboot_pending = 1;
        reboot_since = timer_ticks();
        uart_puts("net: reboot requested over the web\n");
        return http_json(out, out_max, 200, "{\"ok\":true,\"msg\":\"rebooting in 1 s\"}");
    }
    if (!strcmp(path, "/api/update") || !strcmp(path, "/api/run"))
        return http_json(out, out_max, 400, "{\"ok\":false,\"msg\":\"POST the .img as the body, with token=\"}");
    return http_reply(out, out_max, 404, "text/plain", "not found\n");
}

/* --- application image upload ------------------------------------------------ */

static int lower(int c) { return (c >= 'A' && c <= 'Z') ? c + 32 : c; }

/* Decimal number at the start of s (a header value). */
static int parse_dec_prefix(const char *s, uint32_t *out)
{
    uint32_t v = 0;
    int digits = 0;
    while (*s >= '0' && *s <= '9' && digits < 10) { v = v * 10U + (uint32_t)(*s++ - '0'); digits++; }
    if (!digits) return -1;
    *out = v;
    return 0;
}

/* Value of header `name` (lowercase, with ':') in d[0..len), or NULL. */
static const char *header_value(const char *d, uint32_t len, const char *name)
{
    uint32_t n = (uint32_t)strlen(name), i, k;
    for (i = 0; i + n + 2U < len; i++) {
        if (d[i] != '\n') continue;
        for (k = 0; k < n && lower(d[i + 1U + k]) == name[k]; k++) { }
        if (k == n) {
            const char *v = d + i + 1U + n;
            while (*v == ' ') v++;
            return v;
        }
    }
    return 0;
}

static void upload_answer(int code, const char *body)
{
    uint32_t len = http_json(http_buf, sizeof(http_buf), code, body);
    up.state = 2;
    http_send(up.src, up.dport, up.sport, 0, up.our_seq, up.next_seq, len, up.mac);
}

static void upload_finish(void)
{
    const uint8_t *img = (const uint8_t *)STAGING_ADDR;
    const image_header_t *hdr = (const image_header_t *)img;
    char body[256], a[16];
    int st, slot, other, boots, rc;
    slot_info_t info;
    tcp_output(up.src, up.dport, up.sport, up.our_seq, up.next_seq, TCP_ACK, 0, 0, 0, 0, up.mac);
    net_format_ip(up.src, a);
    st = slot_validate_staged(img, up.got);
    if (st != IMG_OK) {
        uart_printf("net: the uploaded image is invalid: %s\n", image_status_str(st));
        snprintf(body, sizeof(body), "{\"ok\":false,\"msg\":\"invalid image: %s\"}", image_status_str(st));
        upload_answer(400, body);
        return;
    }
    if (up.ram) {                                       /* POST /api/run: no flash write */
        if (ramrun_check(up.got)) { upload_answer(400, "{\"ok\":false,\"msg\":\"image refused for RAM run\"}"); return; }
        snprintf(body, sizeof(body), "{\"ok\":true,\"msg\":\"running from RAM\",\"seq\":%u,\"bytes\":%u}",
                 hdr->sequence, up.got);
        upload_answer(200, body);
        ramrun_pending = 1;
        reboot_since = timer_ticks();
        return;
    }
    slot = g_current_slot == 0 ? 1 : 0;
    other = 1 - slot;
    slot_inspect(other, &info, 0);
    boots = info.status != IMG_OK || hdr->sequence > info.hdr->sequence;
    motor_api_stop();
    uart_printf("net: image from %s ok (seq %u, %u bytes), writing slot %c\n", a, hdr->sequence,
                hdr->payload_size, slot_letter(slot));
    rc = slot_write(slot, img, IMAGE_HEADER_SIZE + hdr->payload_size, 0);
    if (rc == FLASH_OK) {
        slot_inspect(slot, &info, 1);
        if (info.status != IMG_OK) rc = -100;
    }
    if (rc != FLASH_OK) {
        uart_printf("net: writing slot %c failed (%d)\n", slot_letter(slot), rc);
        snprintf(body, sizeof(body), "{\"ok\":false,\"msg\":\"flash write or verify failed (%d)\",\"slot\":\"%c\"}",
                 rc, slot_letter(slot));
        upload_answer(500, body);
        return;
    }
    uploads_ok++;
    uart_printf("net: slot %c updated and verified%s\n", slot_letter(slot),
                boots ? (up.reboot ? ", rebooting into it" : "; reboot to run it") :
                        "; NOT booted next: its sequence is not higher than the other slot's");
    snprintf(body, sizeof(body), "{\"ok\":true,\"slot\":\"%c\",\"seq\":%u,\"bytes\":%u,\"boots_next\":%s,\"reboot\":%s}",
             slot_letter(slot), hdr->sequence, up.got, boots ? "true" : "false", boots && up.reboot ? "true" : "false");
    upload_answer(200, body);
    if (boots && up.reboot) {
        reboot_pending = 1;
        reboot_since = timer_ticks();
    }
}

/* First segment of "POST /api/update?token=..[&reboot=1]": all headers in it. */
static void upload_begin(uint32_t src, uint16_t sport, uint16_t dport, uint32_t seq, uint32_t ack,
                         const char *d, uint32_t dlen, const uint8_t *mac)
{
    char query[128], r_s[8], a[16];
    const char *v;
    uint32_t i = 0, q = 0, hdr_end = 0, clen = 0, body;
    up.src = src;
    up.sport = sport;
    up.dport = dport;
    memcpy(up.mac, mac, 6);
    up.next_seq = seq + dlen;
    up.our_seq = ack;
    up.got = 0;
    up.last_ticks = timer_ticks();
    for (i = 0; i + 3U < dlen; i++)
        if (d[i] == '\r' && d[i + 1] == '\n' && d[i + 2] == '\r' && d[i + 3] == '\n') { hdr_end = i + 4U; break; }
    up.ram = dlen >= 13 && !memcmp(d, "POST /api/run", 13);
    for (i = 5; i < dlen && d[i] != '?' && d[i] != ' '; i++) { }
    if (i < dlen && d[i] == '?')
        for (i++; i < dlen && d[i] != ' ' && d[i] != '\r' && q + 1U < sizeof(query); i++) query[q++] = d[i];
    query[q] = 0;
    if (!hdr_end) { upload_answer(400, "{\"ok\":false,\"msg\":\"the request headers must come in one segment\"}"); return; }
    if (!token_ok(query)) { upload_answer(403, "{\"ok\":false,\"msg\":\"wrong or missing token\"}"); return; }
    v = header_value(d, hdr_end, "content-length:");
    if (!v || parse_dec_prefix(v, &clen) || clen < IMAGE_HEADER_SIZE || clen > STAGING_SIZE) {
        upload_answer(413, "{\"ok\":false,\"msg\":\"Content-Length missing or not 64 B .. 512 KiB\"}");
        return;
    }
    up.expect = clen;
    up.reboot = !http_query_param(query, "reboot", r_s, sizeof(r_s)) && r_s[0] == '1';
    up.state = 1;
    net_format_ip(src, a);
    uart_printf("net: receiving a %u byte application image from %s%s\n", clen, a, up.ram ? " (to run from RAM)" : "");
    v = header_value(d, hdr_end, "expect:");
    if (v && lower(v[0]) == '1') {                      /* "Expect: 100-continue" (curl) */
        static const char cont[] = "HTTP/1.1 100 Continue\r\n\r\n";
        tcp_output(src, dport, sport, up.our_seq, up.next_seq, TCP_PSH | TCP_ACK, 0, 0,
                   (const uint8_t *)cont, sizeof(cont) - 1U, mac);
        up.our_seq += sizeof(cont) - 1U;
    }
    body = dlen - hdr_end;
    if (body > clen) body = clen;
    memcpy((uint8_t *)STAGING_ADDR, d + hdr_end, body);
    up.got = body;
    if (up.got >= up.expect) upload_finish();
    else tcp_output(src, dport, sport, up.our_seq, up.next_seq, TCP_ACK, 0, 0, 0, 0, mac);
}

/* Further segments: in order only; anything else is answered with the
   sequence number still expected, which makes the client send it again. */
static void upload_segment(uint32_t seq, const uint8_t *data, uint32_t dlen)
{
    up.last_ticks = timer_ticks();
    if (dlen && seq == up.next_seq) {
        uint32_t take = up.expect - up.got;
        if (take > dlen) take = dlen;
        memcpy((uint8_t *)STAGING_ADDR + up.got, data, take);
        up.got += take;
        up.next_seq += dlen;
        if (up.got >= up.expect) { upload_finish(); return; }
    }
    tcp_output(up.src, up.dport, up.sport, up.our_seq, up.next_seq, TCP_ACK, 0, 0, 0, 0, up.mac);
}

static void tcp_input(uint32_t src, uint32_t dst, const uint8_t *t, uint32_t len, const uint8_t *src_mac)
{
    uint32_t hlen, dlen, seq, ack;
    uint16_t sport, dport;
    uint8_t flags;
    if (dst != my_ip || len < TCP_HLEN) return;
    hlen = (uint32_t)(t[12] >> 4) * 4U;
    if (hlen < TCP_HLEN || hlen > len) { st.bad++; return; }
    if (csum_fold(csum_add(pseudo_sum(src, dst, PROTO_TCP, len), t, len)) != 0) { st.bad++; return; }
    sport = rd16(t + 0);
    dport = rd16(t + 2);
    seq = rd32(t + 4);
    ack = rd32(t + 8);
    flags = t[13];
    dlen = len - hlen;

    if (flags & TCP_RST) {
        if (up.state == 1 && src == up.src && sport == up.sport) { up.state = 0; uart_puts("net: upload aborted by the sender\n"); }
        return;
    }

    if (dport == TN_PORT) { tn_input(src, sport, seq, ack, flags, t + hlen, dlen, src_mac); return; }

    if (!server_enabled || dport != server_port) {
        if (flags & TCP_ACK)
            tcp_output(src, dport, sport, ack, 0, TCP_RST, 0, 0, 0, 0, src_mac);
        else
            tcp_output(src, dport, sport, 0, seq + dlen + ((flags & (TCP_SYN | TCP_FIN)) ? 1U : 0U),
                       TCP_RST | TCP_ACK, 0, 0, 0, 0, src_mac);
        return;
    }

    if ((flags & TCP_SYN) && !(flags & TCP_ACK)) {
        static const uint8_t mss_opt[4] = { 2, 4, (uint8_t)(TCP_MSS >> 8), (uint8_t)(TCP_MSS & 0xFFU) };
        uint32_t isn = (timer_ticks() * 2654435761U) ^ ((uint32_t)sport << 16) ^ src;
        tcp_output(src, dport, sport, isn, seq + 1U, TCP_SYN | TCP_ACK, mss_opt, 4, 0, 0, src_mac);
        return;
    }

    if (up.state == 1 && src == up.src && sport == up.sport) {
        if (dlen) upload_segment(seq, t + hlen, dlen);
        return;
    }
    if (up.state == 2 && src == up.src && sport == up.sport && dlen > 0) {
        /* Body still coming after an early answer (refused upload): take it
           so the client finishes and reads the answer; lost answer segments
           are sent again by the ACK path below. */
        tcp_output(src, dport, sport, up.our_seq + last_http.resp_len + 1U, seq + dlen, TCP_ACK, 0, 0, 0, 0, src_mac);
        return;
    }

    if (dlen > 0) {
        if (http_busy) return;                          /* the client will send it again */
        if ((dlen >= 16 && !memcmp(t + hlen, "POST /api/update", 16)) ||
            (dlen >= 13 && !memcmp(t + hlen, "POST /api/run", 13))) {
            http_busy = 1;
            http_requests++;
            upload_begin(src, sport, dport, seq, ack, (const char *)(t + hlen), dlen, src_mac);
            http_busy = 0;
            return;
        }
        if (src == last_http.src && sport == last_http.sport && seq == last_http.req_seq && last_http.resp_len > 0) {
            http_send(src, dport, sport, seq, last_http.resp_seq, last_http.client_next, last_http.resp_len, src_mac);
            return;
        }
        {
            uint32_t n;
            http_busy = 1;
            http_requests++;
            n = http_handle_request((const char *)(t + hlen), dlen, http_buf, sizeof(http_buf));
            http_send(src, dport, sport, seq, ack, seq + dlen + ((flags & TCP_FIN) ? 1U : 0U), n, src_mac);
            http_busy = 0;
        }
        return;
    }

    if (flags & TCP_FIN) {
        tcp_output(src, dport, sport, ack, seq + 1U, TCP_ACK, 0, 0, 0, 0, src_mac);
        return;
    }

    if ((flags & TCP_ACK) && src == last_http.src && sport == last_http.sport &&
        ack > last_http.resp_seq && ack < last_http.resp_seq + last_http.resp_len &&
        timer_elapsed_ms(last_http.tx_ticks) > 150U) {
        uint32_t off = ack - last_http.resp_seq;
        last_http.tx_ticks = timer_ticks();
        while (off < last_http.resp_len) {
            uint32_t chunk = last_http.resp_len - off;
            uint8_t fl = TCP_PSH | TCP_ACK;
            if (chunk > TCP_MSS) chunk = TCP_MSS;
            if (off + chunk >= last_http.resp_len) fl |= TCP_FIN;
            tcp_output(src, dport, sport, last_http.resp_seq + off, last_http.client_next,
                       fl, 0, 0, (const uint8_t *)http_buf + off, chunk, src_mac);
            off += chunk;
        }
    }
}


/* --- telnet shell on port 23 ---------------------------------------------------
 *
 * One session at a time. The first line must be the web token; after that the
 * client is a console like the UART: its keys go into the shell through
 * uart_set_input2(), and it gets everything the UART prints, taken from the
 * web log (so output made while a command blocks is sent afterwards, as long
 * as it fits the 8 KiB log). Sent data is kept until acknowledged and sent
 * again after 300 ms without an acknowledgement.
 */
#define TN_TX        2048U
#define TN_RX        512U
#define TN_SEG       512U
#define TN_RETX_MS   300U
#define TN_IDLE_MS   600000U          /* drop a silent session after 10 minutes */

static struct {
    int state;                         /* 0 none, 1 SYN answered, 2 connected */
    int authed, fails, iac;            /* iac: telnet command bytes still to skip */
    uint32_t src;
    uint16_t sport;
    uint8_t mac[6];
    uint32_t rcv_nxt, snd_una, snd_nxt;
    uint32_t sent_at, heard_at;
    uint32_t log_pos;                  /* next web-log byte to forward */
    char line[40];
    uint32_t line_len;
    uint8_t tx[TN_TX];                 /* bytes from snd_una on: tx[0] = snd_una */
    uint32_t tx_len;
    char rx[TN_RX];
    uint32_t rx_head, rx_tail;
    int last_cr;
} tn;
static uint32_t tn_sessions;

static void tn_queue(const char *p, uint32_t n)
{
    while (n-- && tn.tx_len < TN_TX) tn.tx[tn.tx_len++] = (uint8_t)*p++;
}

static void tn_puts(const char *s) { tn_queue(s, (uint32_t)strlen(s)); }

static int tn_getc(void)
{
    int c;
    if (tn.rx_head == tn.rx_tail) return -1;
    c = (uint8_t)tn.rx[tn.rx_tail];
    tn.rx_tail = (tn.rx_tail + 1U) % TN_RX;
    return c;
}

static int tn_ready(void) { return tn.rx_head != tn.rx_tail; }

static void tn_close(int send_fin)
{
    if (send_fin && tn.state == 2)
        tcp_output(tn.src, TN_PORT, tn.sport, tn.snd_nxt, tn.rcv_nxt, TCP_FIN | TCP_ACK, 0, 0, 0, 0, tn.mac);
    if (tn.state == 2 && tn.authed) uart_puts("net: telnet session closed\n");
    tn.state = 0;
    tn.authed = 0;
    uart_set_input2(0, 0);
}

/* One received byte from the client. */
static void tn_byte(uint8_t c)
{
    if (tn.iac) {                                       /* inside IAC <cmd> [<option>] */
        if (tn.iac == 1 && c >= 251U && c <= 254U) { tn.iac = 2; return; }
        tn.iac = 0;
        return;
    }
    if (c == 255U) { tn.iac = 1; return; }
    if (c == 0 || (c == '\n' && tn.last_cr)) { tn.last_cr = 0; return; }   /* CR NUL / CR LF = Enter */
    tn.last_cr = c == '\r';
    if (!tn.authed) {                                   /* the first line is the token, not echoed */
        if (c == '\r' || c == '\n') {
            tn.line[tn.line_len] = 0;
            if (web_token[0] && !strcmp(tn.line, web_token)) {
                char a[16];
                tn.authed = 1;
                tn_sessions++;
                tn.log_pos = weblog_total;
                tn_puts("\r\nok: this is the printer's shell (same as the UART). Close the client to leave.\r\napp> ");
                uart_set_input2(tn_getc, tn_ready);
                net_format_ip(tn.src, a);
                uart_printf("net: telnet session from %s\n", a);
            } else if (++tn.fails >= 3) {
                tn_puts("\r\nwrong token, bye\r\n");
                tn.state = 3;                           /* close once this is sent */
            } else {
                tn_puts("\r\nwrong token\r\ntoken: ");
            }
            tn.line_len = 0;
        } else if (c >= 0x20U && c < 0x7FU && tn.line_len + 1U < sizeof(tn.line)) {
            tn.line[tn.line_len++] = (char)c;
        }
        return;
    }
    if (((tn.rx_head + 1U) % TN_RX) != tn.rx_tail) {
        tn.rx[tn.rx_head] = (char)c;
        tn.rx_head = (tn.rx_head + 1U) % TN_RX;
    }
}

static void tn_input(uint32_t src, uint16_t sport, uint32_t seq, uint32_t ack, uint8_t flags,
                     const uint8_t *data, uint32_t dlen, const uint8_t *mac)
{
    static const uint8_t mss_opt[4] = { 2, 4, (uint8_t)(TCP_MSS >> 8), (uint8_t)(TCP_MSS & 0xFFU) };
    static const char hello[] = "\xFF\xFB\x01\xFF\xFB\x03"  /* IAC WILL ECHO, IAC WILL SUPPRESS-GO-AHEAD */
                                "HP DeskJet 2620 custom firmware\r\ntoken: ";
    int ours = tn.state && src == tn.src && sport == tn.sport;
    if (flags & TCP_RST) { if (ours) tn_close(0); return; }
    if ((flags & TCP_SYN) && !(flags & TCP_ACK)) {      /* new session replaces an old one */
        if (tn.state) tn_close(1);
        memset(&tn, 0, sizeof(tn));
        tn.state = 1;
        tn.src = src;
        tn.sport = sport;
        memcpy(tn.mac, mac, 6);
        tn.rcv_nxt = seq + 1U;
        tn.snd_una = tn.snd_nxt = (timer_ticks() * 2654435761U) ^ 0x7E1E7E1EU;
        tn.heard_at = timer_ticks();
        tcp_output(src, TN_PORT, sport, tn.snd_nxt, tn.rcv_nxt, TCP_SYN | TCP_ACK, mss_opt, 4, 0, 0, mac);
        tn.snd_una = tn.snd_nxt = tn.snd_nxt + 1U;
        tn_queue(hello, sizeof(hello) - 1U);
        return;
    }
    if (!ours) {
        if (!(flags & TCP_ACK)) return;
        tcp_output(src, TN_PORT, sport, ack, 0, TCP_RST, 0, 0, 0, 0, mac);
        return;
    }
    tn.heard_at = timer_ticks();
    if (flags & TCP_ACK) {
        uint32_t acked = ack - tn.snd_una;
        if (tn.state == 1) tn.state = 2;
        if (acked && acked <= tn.snd_nxt - tn.snd_una) {
            memmove(tn.tx, tn.tx + acked, tn.tx_len - acked);
            tn.tx_len -= acked;
            tn.snd_una = ack;
            tn.sent_at = timer_ticks();
        }
    }
    if (dlen) {
        if (seq == tn.rcv_nxt) {
            uint32_t i;
            for (i = 0; i < dlen; i++) tn_byte(data[i]);
            tn.rcv_nxt += dlen;
        }
        tcp_output(src, TN_PORT, sport, tn.snd_nxt, tn.rcv_nxt, TCP_ACK, 0, 0, 0, 0, mac);
    }
    if (flags & TCP_FIN) {
        tn.rcv_nxt++;
        tcp_output(src, TN_PORT, sport, tn.snd_nxt, tn.rcv_nxt, TCP_ACK, 0, 0, 0, 0, mac);
        tn_close(1);
    }
}

/* From net_poll(): forward new shell output, send, resend, time out. */
static void tn_poll(void)
{
    uint32_t inflight, n;
    if (!tn.state) return;
    if (timer_elapsed_ms(tn.heard_at) >= TN_IDLE_MS) { tn_close(1); return; }
    if (tn.authed) {                                    /* new output, as CRLF */
        if (weblog_total - tn.log_pos > WEBLOG_SIZE - 1U) {
            tn.log_pos = weblog_total - (WEBLOG_SIZE - 1U);
            tn_puts("\r\n[... output lost ...]\r\n");
        }
        while (tn.log_pos != weblog_total && tn.tx_len + 2U <= TN_TX) {
            char c = weblog[tn.log_pos % WEBLOG_SIZE];
            if (c == '\n') tn.tx[tn.tx_len++] = '\r';
            tn.tx[tn.tx_len++] = (uint8_t)c;
            tn.log_pos++;
        }
    }
    if (tn.state == 1) return;                          /* handshake not finished */
    inflight = tn.snd_nxt - tn.snd_una;
    if (inflight && timer_elapsed_ms(tn.sent_at) >= TN_RETX_MS) {     /* resend everything unacknowledged */
        tn.snd_nxt = tn.snd_una;
        inflight = 0;
    }
    while (inflight < tn.tx_len && inflight < 4U * TN_SEG) {
        n = tn.tx_len - inflight;
        if (n > TN_SEG) n = TN_SEG;
        tcp_output(tn.src, TN_PORT, tn.sport, tn.snd_nxt, tn.rcv_nxt, TCP_PSH | TCP_ACK, 0, 0, tn.tx + inflight, n, tn.mac);
        tn.snd_nxt += n;
        inflight += n;
        tn.sent_at = timer_ticks();
    }
    if (tn.state == 3 && !tn.tx_len) tn_close(1);
}

/* Timers of the server: abandoned uploads, a requested reboot. */
static void http_poll(void)
{
    if (up.state == 1 && timer_elapsed_ms(up.last_ticks) >= UPLOAD_IDLE_MS) {
        up.state = 0;
        uart_printf("net: upload abandoned after %u of %u bytes\n", up.got, up.expect);
    }
    if (ramrun_pending && timer_elapsed_ms(reboot_since) >= 500U) {   /* after the answer went out */
        ramrun_pending = 0;
        ramrun_execute();
    }
    if (reboot_pending && timer_elapsed_ms(reboot_since) >= 1500U) {
        reboot_pending = 0;
        motor_api_stop();
        system_reboot();
    }
}

/* --- link and polling ------------------------------------------------------- */

void net_input(const uint8_t *eth, uint32_t len)
{
    uint32_t type;
    if (!link_up || len < ETH_HLEN) return;
    st.rx++;
    type = rd16(eth + 12);
    if (type == ETH_TYPE_ARP) arp_input(eth, len);
    else if (type == ETH_TYPE_IP) ip_input(eth, len);
}

void net_link_change(int up, int ap)
{
    char a[16], g[16];
    memset(arp_tab, 0, sizeof(arp_tab));
    memset(leases, 0, sizeof(leases));
    arp_wait_len = 0;
    dhcpc_state = DHCPC_IDLE;
    my_ip = my_mask = my_gw = 0;
    mdns_announce_left = 0;
    link_up = up;
    ap_mode = ap;
    if (!up) return;
    if (ap) {
        my_ip = AP_ADDR;
        my_mask = AP_MASK;
        uart_puts("net: access point address 192.168.4.1/24, DHCP for .10 to .17\n");
        mdns_announce_start();
    } else if (static_cfg) {
        my_ip = static_ip;
        my_mask = static_mask;
        my_gw = static_gw;
        net_format_ip(my_ip, a);
        net_format_ip(my_gw, g);
        uart_printf("net: static address %s/%u, gateway %s\n", a, prefix_len(my_mask), g);
        arp_send(1, 0, my_ip);
        mdns_announce_start();
    } else {
        uart_puts("net: asking for an address (DHCP)\n");
        dhcpc_start();
    }
}

void net_poll(void)
{
    http_poll();
    if (!link_up) return;
    if (!ap_mode && !static_cfg) dhcpc_poll();
    if (arp_wait_len && timer_elapsed_ms(arp_wait_since) > 3000U) arp_wait_len = 0;
    mdns_poll();
    tn_poll();
}

/* --- shell -------------------------------------------------------------------- */

static void net_status(void)
{
    char a[16], g[16];
    const uint8_t *m = wifi_mac();
    unsigned i;
    net_format_ip(my_ip, a);
    net_format_ip(my_gw, g);
    uart_printf("net: link %s%s, address %s/%u, gateway %s", link_up ? "up" : "down",
                link_up ? (ap_mode ? " (access point)" : " (station)") : "", a, prefix_len(my_mask), g);
    if (link_up && !ap_mode) {
        if (static_cfg) uart_puts(", static\n");
        else uart_printf(", DHCP %s, lease %u s\n", dhcpc_state_names[dhcpc_state], dhcpc_lease);
    } else {
        uart_puts("\n");
    }
    uart_printf("net: mac %02x:%02x:%02x:%02x:%02x:%02x, frames in %u out %u, bad %u, ARP waits %u, "
                "echo answered %u\n", m[0], m[1], m[2], m[3], m[4], m[5], st.rx, st.tx, st.bad, st.arp_miss, st.echo);
    uart_printf("net: mDNS name %s.local, %u queries answered, %u answers sent\n", mdns_name, mdns_queries, mdns_answers);
    for (i = 0; i < ARP_SLOTS; i++) {
        if (!arp_tab[i].ip) continue;
        net_format_ip(arp_tab[i].ip, a);
        uart_printf("net: arp %-15s %02x:%02x:%02x:%02x:%02x:%02x\n", a, arp_tab[i].mac[0], arp_tab[i].mac[1],
                    arp_tab[i].mac[2], arp_tab[i].mac[3], arp_tab[i].mac[4], arp_tab[i].mac[5]);
    }
    if (ap_mode)
        for (i = 0; i < AP_POOL_SIZE; i++)
            if (leases[i].used) {
                net_format_ip(pool_ip(i), a);
                uart_printf("net: lease %s %02x:%02x:%02x:%02x:%02x:%02x\n", a, leases[i].mac[0], leases[i].mac[1],
                            leases[i].mac[2], leases[i].mac[3], leases[i].mac[4], leases[i].mac[5]);
            }
}

int cmd_net(int argc, char **argv)
{
    if (argc == 1 || (argc == 2 && !strcmp(argv[1], "status"))) { net_status(); return 0; }
    if (argc >= 4 && !strcmp(argv[1], "ip")) {
        uint32_t ip, mask, gw = 0;
        if (net_parse_ip(argv[2], &ip) || net_parse_ip(argv[3], &mask) || (argc >= 5 && net_parse_ip(argv[4], &gw))) {
            uart_puts("usage: net ip <addr> <mask> [gateway]\n");
            return -1;
        }
        static_ip = ip; static_mask = mask; static_gw = gw; static_cfg = 1;
        if (link_up && !ap_mode) net_link_change(1, 0);
        else uart_puts("net: static address stored, used when the station link comes up\n");
        return 0;
    }
    if (argc == 2 && !strcmp(argv[1], "dhcp")) {
        static_cfg = 0;
        if (link_up && !ap_mode) net_link_change(1, 0);
        else uart_puts("net: DHCP will be used when the station link comes up\n");
        return 0;
    }
    if (argc == 3 && !strcmp(argv[1], "name")) {
        uint32_t i, n = (uint32_t)strlen(argv[2]);
        for (i = 0; i < n; i++) {
            char c = argv[2][i];
            if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || (c == '-' && i && i + 1U < n))) n = 0;
        }
        if (n == 0 || n >= sizeof(mdns_name)) { uart_puts("usage: net name <a-z, 0-9, inner '-', up to 31>\n"); return -1; }
        strlcpy(mdns_name, argv[2], sizeof(mdns_name));
        uart_printf("net: mDNS name %s.local\n", mdns_name);
        mdns_announce_start();
        return 0;
    }
    uart_puts("usage: net [status] | net ip <addr> <mask> [gw] | net dhcp | net name <host>\n");
    return -1;
}

int cmd_ping(int argc, char **argv)
{
    uint32_t ip, count = 4;
    if (argc < 2 || net_parse_ip(argv[1], &ip) ||
        (argc >= 3 && (parse_u32(argv[2], &count) || count == 0 || count > 100))) {
        uart_puts("usage: ping <a.b.c.d> [count 1..100]\n");
        return -1;
    }
    return net_ping(ip, count);
}

static void server_print_status(void)
{
    char a[16];
    struct motor_telemetry t;
    motor_api_telemetry(&t);
    net_format_ip(my_ip, a);
    if (net_has_address()) {
        if (server_port == 80)
            uart_printf("server: %s at http://%s/ (port %u, %u requests, %u motor cmds)\n",
                        server_enabled ? "listening" : "disabled", a, server_port, http_requests, http_motor_cmds);
        else
            uart_printf("server: %s at http://%s:%u/ (%u requests, %u motor cmds)\n",
                        server_enabled ? "listening" : "disabled", a, server_port, http_requests, http_motor_cmds);
    } else {
        uart_printf("server: %s on port %u (no IP address yet; run 'wifi join' or 'wifi ap')\n",
                    server_enabled ? "enabled" : "disabled", server_port);
    }
    uart_printf("server: telnet on port %u: %s, %u sessions so far\n", TN_PORT,
                tn.state == 2 && tn.authed ? "session open" : tn.state ? "logging in" : "idle", tn_sessions);
    uart_printf("server: %u shell commands, %u uploads written; token %s\n", http_shell_cmds, uploads_ok,
                web_token[0] ? "set ('server token <t>' changes it)" : "empty: shell, reboot and update are off");
    uart_printf("server: mcasic %s (00=%04x 20=%04x 30=%04x 3b=%04x), ctl=%04x/%04x/%04x, door %s\n",
                t.inited ? "initialized" : "not initialized",
                t.mcasic_id, t.reg_20, t.reg_30, t.gpio_3b,
                t.motor_ctl[0], t.motor_ctl[1], t.motor_ctl[2],
                t.door_open ? "open" : "closed");
}

int cmd_server(int argc, char **argv)
{
    if (argc == 1 || (argc == 2 && !strcmp(argv[1], "status"))) {
        server_print_status();
        return 0;
    }
    if (argc == 2 && !strcmp(argv[1], "on")) {
        server_enabled = 1;
        server_print_status();
        return 0;
    }
    if (argc == 2 && !strcmp(argv[1], "off")) {
        motor_api_stop();
        server_enabled = 0;
        server_print_status();
        return 0;
    }
    if (argc == 2 && !strcmp(argv[1], "init")) {
        int rc = motor_api_init();
        server_print_status();
        return rc;
    }
    if (argc == 2 && !strcmp(argv[1], "stop")) {
        motor_api_stop();
        server_print_status();
        return 0;
    }
    if (argc == 3 && !strcmp(argv[1], "port")) {
        uint32_t p = 0;
        if (parse_u32(argv[2], &p) || p == 0 || p > 65535U) {
            uart_puts("usage: server port <1..65535>\n");
            return -1;
        }
        server_port = (uint16_t)p;
        server_print_status();
        return 0;
    }
    if (argc == 3 && !strcmp(argv[1], "token")) {
        if (strlen(argv[2]) >= sizeof(web_token)) { uart_puts("server: token up to 32 characters\n"); return -1; }
        strlcpy(web_token, strcmp(argv[2], "-") ? argv[2] : "", sizeof(web_token));
        server_print_status();
        return 0;
    }
    uart_puts("usage: server [status] | on | off | port <1..65535> | init | stop | token <t> ('-' = none)\n");
    return -1;
}
