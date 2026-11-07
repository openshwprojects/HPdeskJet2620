/*
 * dnsd.c - captive DNS responder, see dnsd.h
 */
#include "dnsd.h"
#include "net.h"
#include "libc.h"

static uint8_t answer[4];
struct dnsd_stats dnsd_stats;

static void on_dns(const uint8_t src_ip[4], uint16_t sport, uint16_t dport, const uint8_t *q, uint32_t len)
{
    (void)dport;
    const uint8_t *wip = net_wifi_ip();
    if ((wip[0] | wip[1] | wip[2] | wip[3]) && net_rx_port() != NET_PORT_WIFI)
        return;
    if (len < 17 || (q[2] & 0x80) || rd16(q + 4) < 1) return;        /* a query with one question */
    dnsd_stats.queries++;
    uint32_t o = 12, k = 0;
    while (o < len && q[o]) {                                        /* copy the name for the status page */
        uint8_t l = q[o];
        if (o + 1 + l > len) return;
        for (uint8_t i = 0; i < l && k < sizeof dnsd_stats.last_name - 2; i++) dnsd_stats.last_name[k++] = (char)q[o + 1 + i];
        if (k < sizeof dnsd_stats.last_name - 2) dnsd_stats.last_name[k++] = '.';
        o += 1u + l;
    }
    if (k) dnsd_stats.last_name[k - 1] = 0;
    o++;
    if (o + 4 > len) return;
    uint16_t qtype = rd16(q + o);
    uint32_t qlen = o + 4;
    static uint8_t r[512];
    if (qlen + 16 > sizeof r) return;
    memcpy(r, q, qlen);
    r[2] = 0x81; r[3] = 0x80;                                        /* response, recursion available */
    wr16(r + 4, 1);
    wr16(r + 6, qtype == 1 ? 1 : 0);
    wr16(r + 8, 0); wr16(r + 10, 0);
    uint32_t n = qlen;
    if (qtype == 1) {
        r[n++] = 0xC0; r[n++] = 0x0C;                                /* name: pointer to the question */
        wr16(r + n, 1); n += 2;                                      /* type A */
        wr16(r + n, 1); n += 2;                                      /* class IN */
        wr32(r + n, 60); n += 4;                                     /* TTL */
        wr16(r + n, 4); n += 2;
        memcpy(r + n, answer, 4); n += 4;
    }
    if (net_rx_port() == NET_PORT_WIFI) net_set_tx_port(NET_PORT_WIFI);
    int rc = net_udp_send(src_ip, 53, sport, r, n);
    net_set_tx_port(-1);
    if (rc == 0) dnsd_stats.answered++;
}

int dnsd_start(const uint8_t answer_ip[4])
{
    memcpy(answer, answer_ip, 4);
    memset(&dnsd_stats, 0, sizeof dnsd_stats);
    return net_udp_listen(53, on_dns);
}

void dnsd_stop(void)
{
    net_udp_listen(53, 0);
}
