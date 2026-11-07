/*
 * lwip_mdns.c - lightweight RFC 6762 / RFC 6763 mDNS & DNS-SD responder
 * for OpenRTL8196C (answers <hostname>.local A and _http._tcp.local PTR/SRV/TXT
 * on both Ethernet and Wi-Fi interfaces via UDP port 5353).
 */
#include "rtl8196c.h"
#include "net.h"
#include "libc.h"
#include "obk_shim/stdio.h"
#include "obk_shim/lwip/apps/mdns.h"

#define MDNS_PORT 5353

static struct netif g_dummy_netif = { 1 };
static int g_mdns_enabled = 0;
static char g_mdns_host[64] = "openbeken";
static uint16_t g_mdns_http_port = 80;
static uint32_t g_mdns_ttl = 120;

static const uint8_t g_mdns_mcast_ip[4] = { 224, 0, 0, 251 };

void *net_get_sta_handle(void)
{
    return &g_dummy_netif;
}

static int ascii_tolower(int c)
{
    return (c >= 'A' && c <= 'Z') ? (c - 'A' + 'a') : c;
}

static int str_ieq(const char *a, const char *b)
{
    while (*a && *b) {
        if (ascii_tolower((unsigned char)*a) != ascii_tolower((unsigned char)*b))
            return 0;
        a++;
        b++;
    }
    return *a == 0 && *b == 0;
}

/* Decode a DNS name at offset `pos` into dotted string `out` (max `out_max`).
 * Returns offset immediately after the name in packet, or -1 on error. */
static int dns_decode_name(const uint8_t *pkt, uint32_t len, uint32_t pos,
                           char *out, uint32_t out_max)
{
    uint32_t o = 0;
    uint32_t cur = pos;
    int jumped = 0;
    uint32_t next_pos = 0;
    int hops = 0;

    while (cur < len && hops++ < 16) {
        uint8_t l = pkt[cur];
        if (l == 0) {
            if (!jumped)
                next_pos = cur + 1;
            break;
        }
        if ((l & 0xC0) == 0xC0) {
            if (cur + 1 >= len)
                return -1;
            if (!jumped)
                next_pos = cur + 2;
            jumped = 1;
            cur = (uint32_t)(((l & 0x3F) << 8) | pkt[cur + 1]);
            continue;
        }
        if (l > 63 || cur + 1 + l > len)
            return -1;
        if (o > 0 && o + 1 < out_max)
            out[o++] = '.';
        for (uint8_t i = 0; i < l; i++) {
            if (o + 1 < out_max)
                out[o++] = (char)pkt[cur + 1 + i];
        }
        cur += 1u + l;
    }
    out[o] = 0;
    return jumped ? (int)next_pos : (int)(cur + 1);
}

/* Encode a dotted name like "myhost.local" into DNS labels at `buf` */
static uint32_t dns_encode_name(uint8_t *buf, const char *name)
{
    uint32_t o = 0;
    const char *p = name;
    while (*p) {
        const char *dot = p;
        while (*dot && *dot != '.')
            dot++;
        uint32_t l = (uint32_t)(dot - p);
        if (l > 63) l = 63;
        buf[o++] = (uint8_t)l;
        memcpy(buf + o, p, l);
        o += l;
        p = (*dot == '.') ? (dot + 1) : dot;
    }
    buf[o++] = 0;
    return o;
}

static uint32_t put_a_record(uint8_t *b, const char *fqdn, uint32_t ttl, const uint8_t ip[4])
{
    uint32_t n = dns_encode_name(b, fqdn);
    wr16(b + n, 1);          /* TYPE A */
    wr16(b + n + 2, 0x8001); /* CLASS IN + cache-flush */
    wr32(b + n + 4, ttl);
    wr16(b + n + 8, 4);
    memcpy(b + n + 10, ip, 4);
    return n + 14;
}

static uint32_t put_ptr_record(uint8_t *b, const char *service_fqdn, const char *instance_fqdn, uint32_t ttl)
{
    uint32_t n = dns_encode_name(b, service_fqdn);
    wr16(b + n, 12);         /* TYPE PTR */
    wr16(b + n + 2, 0x0001); /* CLASS IN (shared) */
    wr32(b + n + 4, ttl);
    uint32_t rlen = dns_encode_name(b + n + 10, instance_fqdn);
    wr16(b + n + 8, (uint16_t)rlen);
    return n + 10 + rlen;
}

static uint32_t put_srv_record(uint8_t *b, const char *instance_fqdn, const char *host_fqdn,
                               uint16_t port, uint32_t ttl)
{
    uint32_t n = dns_encode_name(b, instance_fqdn);
    wr16(b + n, 33);         /* TYPE SRV */
    wr16(b + n + 2, 0x8001); /* CLASS IN + cache-flush */
    wr32(b + n + 4, ttl);
    wr16(b + n + 10, 0);     /* priority */
    wr16(b + n + 12, 0);     /* weight */
    wr16(b + n + 14, port);  /* port */
    uint32_t tlen = dns_encode_name(b + n + 16, host_fqdn);
    wr16(b + n + 8, (uint16_t)(6 + tlen));
    return n + 16 + tlen;
}

static uint32_t put_txt_record(uint8_t *b, const char *instance_fqdn, uint32_t ttl)
{
    uint32_t n = dns_encode_name(b, instance_fqdn);
    wr16(b + n, 16);         /* TYPE TXT */
    wr16(b + n + 2, 0x8001); /* CLASS IN + cache-flush */
    wr32(b + n + 4, ttl);
    wr16(b + n + 8, 1);
    b[n + 10] = 0;           /* single empty string */
    return n + 11;
}

static void mdns_udp_rx(const uint8_t src_ip[4], uint16_t src_port, uint16_t dst_port,
                        const uint8_t *data, uint32_t len)
{
    (void)dst_port;
    if (!g_mdns_enabled || len < 12)
        return;

    uint16_t id      = rd16(data);
    uint16_t flags   = rd16(data + 2);
    uint16_t qdcount = rd16(data + 4);
    if ((flags & 0x8000) != 0 || qdcount == 0)
        return;

    char host_fqdn[80];
    char inst_fqdn[96];
    snprintf(host_fqdn, sizeof(host_fqdn), "%s.local", g_mdns_host);
    snprintf(inst_fqdn, sizeof(inst_fqdn), "%s._http._tcp.local", g_mdns_host);

    int want_a = 0;
    int want_ptr = 0;
    int want_srv_txt = 0;
    int want_unicast = (src_port != MDNS_PORT);

    uint32_t pos = 12;
    for (uint16_t q = 0; q < qdcount && pos < len; q++) {
        char qname[128];
        int next = dns_decode_name(data, len, pos, qname, sizeof(qname));
        if (next < 0 || (uint32_t)next + 4 > len)
            break;
        uint16_t qtype  = rd16(data + next);
        uint16_t qclass = rd16(data + next + 2);
        pos = (uint32_t)next + 4;

        if (qclass & 0x8000)
            want_unicast = 1;

        if (str_ieq(qname, host_fqdn) && (qtype == 1 || qtype == 255)) {
            want_a = 1;
        } else if (str_ieq(qname, "_http._tcp.local") && (qtype == 12 || qtype == 255)) {
            want_ptr = 1;
        } else if (str_ieq(qname, inst_fqdn) && (qtype == 33 || qtype == 16 || qtype == 255)) {
            want_srv_txt = 1;
        }
    }

    if (!want_a && !want_ptr && !want_srv_txt)
        return;

    static uint8_t resp[512];
    memset(resp, 0, 12);
    wr16(resp, (src_port != MDNS_PORT) ? id : 0);
    wr16(resp + 2, 0x8400); /* QR=1, AA=1 */

    uint16_t ancount = 0;
    uint32_t n = 12;
    const uint8_t *my_ip = net_active_ip();

    if (want_ptr) {
        n += put_ptr_record(resp + n, "_http._tcp.local", inst_fqdn, g_mdns_ttl);
        ancount++;
        want_srv_txt = 1;
        want_a = 1;
    }
    if (want_srv_txt) {
        n += put_srv_record(resp + n, inst_fqdn, host_fqdn, g_mdns_http_port, g_mdns_ttl);
        ancount++;
        n += put_txt_record(resp + n, inst_fqdn, g_mdns_ttl);
        ancount++;
        want_a = 1;
    }
    if (want_a) {
        n += put_a_record(resp + n, host_fqdn, g_mdns_ttl, my_ip);
        ancount++;
    }

    wr16(resp + 6, ancount);

    if (want_unicast)
        net_udp_send(src_ip, MDNS_PORT, src_port, resp, n);
    else
        net_udp_send(g_mdns_mcast_ip, MDNS_PORT, MDNS_PORT, resp, n);
}

void mdns_resp_init(void)
{
    net_udp_listen(MDNS_PORT, mdns_udp_rx);
}

err_t mdns_resp_add_netif(struct netif *netif, const char *hostname, u32_t dns_ttl)
{
    (void)netif;
    if (hostname && hostname[0]) {
        strncpy(g_mdns_host, hostname, sizeof(g_mdns_host) - 1);
        g_mdns_host[sizeof(g_mdns_host) - 1] = 0;
    }
    if (dns_ttl)
        g_mdns_ttl = dns_ttl;
    g_mdns_enabled = 1;
    net_udp_listen(MDNS_PORT, mdns_udp_rx);
    return ERR_OK;
}

err_t mdns_resp_remove_netif(struct netif *netif)
{
    (void)netif;
    g_mdns_enabled = 0;
    return ERR_OK;
}

s8_t mdns_resp_add_service(struct netif *netif, const char *name, const char *service,
                           u16_t proto, u16_t port, u32_t dns_ttl,
                           service_get_txt_fn_t txt_fn, void *txt_userdata)
{
    (void)netif; (void)name; (void)service; (void)proto; (void)txt_fn; (void)txt_userdata;
    if (port)
        g_mdns_http_port = port;
    if (dns_ttl)
        g_mdns_ttl = dns_ttl;
    return 0;
}

void mdns_resp_announce(struct netif *netif)
{
    (void)netif;
    if (!g_mdns_enabled)
        return;
    char host_fqdn[80];
    snprintf(host_fqdn, sizeof(host_fqdn), "%s.local", g_mdns_host);
    static uint8_t pkt[256];
    memset(pkt, 0, 12);
    wr16(pkt + 2, 0x8400);
    wr16(pkt + 6, 1);
    uint32_t n = 12 + put_a_record(pkt + 12, host_fqdn, g_mdns_ttl, net_ip());
    net_udp_send(g_mdns_mcast_ip, MDNS_PORT, MDNS_PORT, pkt, n);
}

void mdns_resp_restart(struct netif *netif)
{
    (void)netif;
}

void mdns_resp_netif_settings_changed(struct netif *netif)
{
    mdns_resp_announce(netif);
}
