/*
 * dhcp.c - non-blocking DHCPv4 client (DISCOVER -> OFFER -> REQUEST -> ACK)
 */
#include "dhcp.h"
#include "net.h"
#include "timer.h"
#include "libc.h"

struct dhcp_client {
    struct dhcp_lease lease;
    uint8_t  offered_ip[4];
    uint8_t  server_id[4];
    uint32_t t_last_tx;
    int      is_wifi;
};

static struct dhcp_client eth_c  = { .is_wifi = 0 };
static struct dhcp_client wifi_c = { .is_wifi = 1 };
static char host_name[24] = "OpenRTL8196C";

static const uint8_t *client_mac(const struct dhcp_client *c)
{
    return c->is_wifi ? net_wifi_mac() : net_mac();
}

static void send_dhcp(struct dhcp_client *c, uint8_t msg_type)
{
    const uint8_t *mac = client_mac(c);
    uint8_t pkt[300];
    memset(pkt, 0, sizeof pkt);
    pkt[0] = 1;                /* BOOTREQUEST */
    pkt[1] = 1;                /* Ethernet */
    pkt[2] = 6;                /* MAC len */
    wr32(pkt + 4, c->lease.xid);
    wr16(pkt + 10, 0);         /* unicast flag (matches fw/app/net.c) */
    memcpy(pkt + 28, mac, 6);

    /* magic cookie 99.130.83.99 */
    pkt[236] = 99; pkt[237] = 130; pkt[238] = 83; pkt[239] = 99;
    uint8_t *o = pkt + 240;

    /* option 53: DHCP message type */
    *o++ = 53; *o++ = 1; *o++ = msg_type;

    /* option 61: client identifier */
    *o++ = 61; *o++ = 7; *o++ = 1;
    memcpy(o, mac, 6); o += 6;

    /* option 12: hostname */
    uint8_t hlen = (uint8_t)strlen(host_name);
    if (hlen) {
        *o++ = 12; *o++ = hlen;
        memcpy(o, host_name, hlen); o += hlen;
    }

    if (msg_type == 3) {       /* DHCPREQUEST */
        *o++ = 50; *o++ = 4; memcpy(o, c->offered_ip, 4); o += 4;
        if (c->server_id[0] | c->server_id[1] | c->server_id[2] | c->server_id[3]) {
            *o++ = 54; *o++ = 4; memcpy(o, c->server_id, 4); o += 4;
        }
    }

    /* option 55: parameter request list (subnet mask, router, DNS, lease time) */
    *o++ = 55; *o++ = 4; *o++ = 1; *o++ = 3; *o++ = 6; *o++ = 51;
    *o++ = 255;                /* end */

    static const uint8_t bcast[4] = { 255, 255, 255, 255 };
    net_set_tx_port(c->is_wifi ? NET_PORT_WIFI : 0);
    net_udp_send(bcast, 68, 67, pkt, sizeof(pkt));
    net_set_tx_port(-1);
    c->t_last_tx = micros();
}

static void handle_client_reply(struct dhcp_client *c, const uint8_t src_ip[4],
                                const uint8_t *d, uint32_t len)
{
    uint8_t msg_type = 0;
    uint8_t mask[4] = { 255, 255, 255, 0 };
    uint8_t gw[4]   = { 0, 0, 0, 0 };
    uint8_t dns[4]  = { 0, 0, 0, 0 };
    uint8_t srv[4]  = { 0, 0, 0, 0 };
    uint32_t ltime  = 3600;

    uint32_t i = 240;
    while (i < len) {
        uint8_t opt = d[i++];
        if (opt == 0) continue;
        if (opt == 255 || i >= len) break;
        uint8_t olen = d[i++];
        if (i + olen > len) break;
        const uint8_t *v = d + i;
        if (opt == 53 && olen >= 1) msg_type = v[0];
        else if (opt == 1 && olen >= 4) memcpy(mask, v, 4);
        else if (opt == 3 && olen >= 4) memcpy(gw, v, 4);
        else if (opt == 6 && olen >= 4) memcpy(dns, v, 4);
        else if (opt == 54 && olen >= 4) memcpy(srv, v, 4);
        else if (opt == 51 && olen >= 4) ltime = rd32(v);
        i += olen;
    }

    if (!(srv[0] | srv[1] | srv[2] | srv[3]))
        memcpy(srv, src_ip, 4);

    if (msg_type == 2 && c->lease.state == DHCP_DISCOVERING) {   /* DHCPOFFER */
        memcpy(c->offered_ip, d + 16, 4);
        memcpy(c->server_id, srv, 4);
        c->lease.state = DHCP_REQUESTING;
        send_dhcp(c, 3);
    } else if (msg_type == 5 && (c->lease.state == DHCP_REQUESTING || c->lease.state == DHCP_DISCOVERING)) { /* DHCPACK */
        memcpy(c->lease.ip, d + 16, 4);
        memcpy(c->lease.mask, mask, 4);
        memcpy(c->lease.gw, gw, 4);
        memcpy(c->lease.dns, (dns[0] | dns[1] | dns[2] | dns[3]) ? dns : gw, 4);
        memcpy(c->lease.server, srv, 4);
        c->lease.lease_seconds = ltime;
        c->lease.state = DHCP_BOUND;
        if (c->is_wifi) {
            net_set_wifi_ip(c->lease.ip);
            net_set_wifi_netmask(c->lease.mask);
            if (gw[0] | gw[1] | gw[2] | gw[3]) net_set_wifi_gateway(c->lease.gw);
            if (c->lease.dns[0] | c->lease.dns[1] | c->lease.dns[2] | c->lease.dns[3]) net_set_wifi_dns(c->lease.dns);
        } else {
            net_set_ip(c->lease.ip);
            net_set_netmask(c->lease.mask);
            if (gw[0] | gw[1] | gw[2] | gw[3]) net_set_gateway(c->lease.gw);
            if (c->lease.dns[0] | c->lease.dns[1] | c->lease.dns[2] | c->lease.dns[3]) net_set_dns(c->lease.dns);
        }
    }
}

static void on_dhcp_udp(const uint8_t src_ip[4], uint16_t sport, uint16_t dport,
                        const uint8_t *d, uint32_t len)
{
    (void)sport; (void)dport;
    if (len < 240 || d[0] != 2)
        return;
    if (d[236] != 99 || d[237] != 130 || d[238] != 83 || d[239] != 99)
        return;
    uint32_t xid = rd32(d + 4);
    if (eth_c.lease.state != DHCP_OFF && xid == eth_c.lease.xid && memcmp(d + 28, net_mac(), 6) == 0) {
        handle_client_reply(&eth_c, src_ip, d, len);
    } else if (wifi_c.lease.state != DHCP_OFF && xid == wifi_c.lease.xid && memcmp(d + 28, net_wifi_mac(), 6) == 0) {
        handle_client_reply(&wifi_c, src_ip, d, len);
    }
}

static void client_start(struct dhcp_client *c, const char *hostname)
{
    if (hostname && hostname[0]) {
        strncpy(host_name, hostname, sizeof(host_name) - 1);
        host_name[sizeof(host_name) - 1] = 0;
    }
    const uint8_t *m = client_mac(c);
    c->lease.xid = (c->is_wifi ? 0x92CE0000u : 0x8196C000u) ^ ((uint32_t)m[4] << 8) ^ m[5] ^ micros();
    c->lease.state = DHCP_DISCOVERING;
    net_udp_listen(68, on_dhcp_udp);
    send_dhcp(c, 1);
}

static void client_poll(struct dhcp_client *c)
{
    if (c->lease.state == DHCP_DISCOVERING || c->lease.state == DHCP_REQUESTING) {
        if (elapsed_us(c->t_last_tx) > 1500000u) {
            c->lease.state = DHCP_DISCOVERING;
            send_dhcp(c, 1);
        }
    }
}

void dhcp_start(const char *hostname)      { client_start(&eth_c, hostname); }
void dhcp_poll(void)                       { client_poll(&eth_c); }
int  dhcp_bound(void)                      { return eth_c.lease.state == DHCP_BOUND; }
const struct dhcp_lease *dhcp_info(void)   { return &eth_c.lease; }

void dhcp_wifi_start(const char *hostname)
{
    static const uint8_t zero_ip[4] = { 0, 0, 0, 0 };
    memset(&wifi_c.lease, 0, sizeof(wifi_c.lease));
    net_set_wifi_ip(zero_ip);
    client_start(&wifi_c, hostname);
}
void dhcp_wifi_stop(void)
{
    memset(&wifi_c.lease, 0, sizeof(wifi_c.lease));
    wifi_c.lease.state = DHCP_OFF;
}
void dhcp_wifi_poll(void)                    { client_poll(&wifi_c); }
int  dhcp_wifi_bound(void)                   { return wifi_c.lease.state == DHCP_BOUND; }
const struct dhcp_lease *dhcp_wifi_info(void) { return &wifi_c.lease; }
