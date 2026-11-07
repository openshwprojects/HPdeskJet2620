/*
 * dhcpd.c - tiny DHCPv4 server, see dhcpd.h (RFC 2131/2132 subset)
 */
#include "dhcpd.h"
#include "net.h"
#include "libc.h"

static uint8_t srv_ip[4], srv_mask[4];
static uint8_t pool_first, pool_count;
static struct dhcpd_lease leases[DHCPD_MAX_LEASES];
struct dhcpd_stats dhcpd_stats;

static struct dhcpd_lease *lease_for(const uint8_t *mac)
{
    for (int i = 0; i < DHCPD_MAX_LEASES; i++)
        if (leases[i].valid && !memcmp(leases[i].mac, mac, 6)) return &leases[i];
    for (int i = 0; i < DHCPD_MAX_LEASES && i < pool_count; i++)
        if (!leases[i].valid) {
            memset(&leases[i], 0, sizeof leases[i]);
            memcpy(leases[i].mac, mac, 6);
            memcpy(leases[i].ip, srv_ip, 4);
            leases[i].ip[3] = (uint8_t)(pool_first + i);
            leases[i].valid = 1;
            return &leases[i];
        }
    return 0;
}

static const uint8_t *find_opt(const uint8_t *o, const uint8_t *end, uint8_t code, uint8_t *len)
{
    while (o + 2 <= end && *o != 255) {
        if (*o == 0) { o++; continue; }
        if (o[0] == code) { *len = o[1]; return o + 2 + o[1] <= end ? o + 2 : 0; }
        o += 2u + o[1];
    }
    return 0;
}

static void on_dhcp(const uint8_t src_ip[4], uint16_t sport, uint16_t dport, const uint8_t *m, uint32_t len)
{
    (void)src_ip; (void)sport; (void)dport;
    const uint8_t *wip = net_wifi_ip();
    if ((wip[0] | wip[1] | wip[2] | wip[3]) && net_rx_port() != NET_PORT_WIFI)
        return; /* never answer DHCP on wired Ethernet when running AP on Wi-Fi */
    if (len < 240 || m[0] != 1 || m[1] != 1 || m[2] != 6) return;          /* BOOTREQUEST, Ethernet */
    if (!(m[236] == 0x63 && m[237] == 0x82 && m[238] == 0x53 && m[239] == 0x63)) return;
    const uint8_t *opts = m + 240, *end = m + len;
    uint8_t l, type = 0;
    const uint8_t *p = find_opt(opts, end, 53, &l);
    if (!p || l < 1) return;
    type = p[0];
    const uint8_t *mac = m + 28;
    struct dhcpd_lease *le = lease_for(mac);
    if (!le) return;
    p = find_opt(opts, end, 12, &l);
    if (p && l < sizeof le->hostname) { memcpy(le->hostname, p, l); le->hostname[l] = 0; }

    uint8_t reply_type;
    if (type == 1) { dhcpd_stats.discover++; reply_type = 2; }                /* DISCOVER -> OFFER */
    else if (type == 3) {                                                     /* REQUEST -> ACK / NAK */
        dhcpd_stats.request++;
        const uint8_t *req = find_opt(opts, end, 50, &l);
        const uint8_t *want = (req && l == 4) ? req : (m[12] | m[13] | m[14] | m[15]) ? m + 12 : 0;
        reply_type = (!want || !memcmp(want, le->ip, 4)) ? 5 : 6;
    } else { dhcpd_stats.other++; return; }

    static uint8_t r[320];
    memset(r, 0, sizeof r);
    r[0] = 2; r[1] = 1; r[2] = 6;                                             /* BOOTREPLY */
    memcpy(r + 4, m + 4, 4);                                                  /* xid */
    r[10] = 0x80;                                                             /* broadcast flag */
    if (reply_type != 6) { memcpy(r + 16, le->ip, 4); memcpy(r + 20, srv_ip, 4); }
    memcpy(r + 28, mac, 6);
    r[236] = 0x63; r[237] = 0x82; r[238] = 0x53; r[239] = 0x63;
    uint32_t n = 240;
    r[n++] = 53; r[n++] = 1; r[n++] = reply_type;
    r[n++] = 54; r[n++] = 4; memcpy(r + n, srv_ip, 4); n += 4;
    if (reply_type != 6) {
        r[n++] = 51; r[n++] = 4; r[n++] = 0; r[n++] = 0x01; r[n++] = 0x51; r[n++] = 0x80;   /* 86400 s */
        r[n++] = 1;  r[n++] = 4; memcpy(r + n, srv_mask, 4); n += 4;
        r[n++] = 3;  r[n++] = 4; memcpy(r + n, srv_ip, 4); n += 4;
        r[n++] = 6;  r[n++] = 4; memcpy(r + n, srv_ip, 4); n += 4;
    }
    r[n++] = 255;
    if (n < 300) n = 300;
    static const uint8_t bcast[4] = { 255, 255, 255, 255 };
    if (net_rx_port() == NET_PORT_WIFI) net_set_tx_port(NET_PORT_WIFI);
    int rc = net_udp_send(bcast, 67, 68, r, n);
    net_set_tx_port(-1);
    if (rc == 0) {
        if (reply_type == 2) dhcpd_stats.offer++;
        else if (reply_type == 5) dhcpd_stats.ack++;
        else dhcpd_stats.nak++;
    }
}

int dhcpd_start(const uint8_t server_ip[4], const uint8_t mask[4], uint8_t first_host, uint8_t count)
{
    memcpy(srv_ip, server_ip, 4);
    memcpy(srv_mask, mask, 4);
    pool_first = first_host;
    pool_count = count > DHCPD_MAX_LEASES ? DHCPD_MAX_LEASES : count;
    memset(leases, 0, sizeof leases);
    memset(&dhcpd_stats, 0, sizeof dhcpd_stats);
    return net_udp_listen(67, on_dhcp);
}

void dhcpd_stop(void)
{
    net_udp_listen(67, 0);
}

int dhcpd_leases(const struct dhcpd_lease **out) { *out = leases; return DHCPD_MAX_LEASES; }
