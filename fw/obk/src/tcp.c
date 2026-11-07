/*
 * tcp.c - see tcp.h
 */
#include "rtl8196c.h"
#include "tcp.h"
#include "net.h"
#include "timer.h"
#include "libc.h"

#define F_FIN 0x01
#define F_SYN 0x02
#define F_RST 0x04
#define F_PSH 0x08
#define F_ACK 0x10

enum { FREE, SYN_RCVD, SYN_SENT, ESTABLISHED, CLOSE_WAIT, FIN_WAIT_1, FIN_WAIT_2, LAST_ACK };

struct listener { uint16_t port; tcp_data_cb_t on_data; tcp_event_cb_t on_event; };

struct tcp_conn {
    uint8_t  state;
    uint8_t  fin_queued;      /* app called tcp_close() */
    uint8_t  fin_sent;        /* FIN is in flight or acked */
    uint8_t  fin_acked;
    uint8_t  peer_fin;        /* the peer's FIN has been received (in order) */
    uint8_t  rx_full;         /* socket rx_buf has no room for another segment */
    uint8_t  retries;
    uint8_t  rip[4];
    uint8_t  lip[4];
    uint16_t rport, lport;
    struct listener *l;
    tcp_data_cb_t  on_data;
    tcp_event_cb_t on_event;
    uint32_t snd_una;         /* seq of sndbuf[0] */
    uint32_t snd_nxt;         /* next seq to send */
    uint32_t rcv_nxt;
    uint32_t snd_wnd;         /* peer window */
    uint16_t rcv_wnd;         /* our advertised receive window */
    uint32_t snd_len;         /* bytes in sndbuf */
    uint32_t t_last, t_rtx;
    void    *user;
    uint8_t  sndbuf[TCP_SNDBUF];
};

static struct listener listeners[TCP_MAX_LISTEN];
static struct tcp_conn conns[TCP_MAX_CONN];
static uint8_t seg[24 + TCP_MSS];
static uint16_t next_ephemeral = 49152;
struct tcp_stats tcp_stats;

/* ------------------------------------------------------------- helpers */
static int32_t seq_diff(uint32_t a, uint32_t b) { return (int32_t)(a - b); }

static void send_raw(const uint8_t rip[4], uint16_t lport, uint16_t rport, uint32_t seqn, uint32_t ack,
                     uint8_t flags, uint16_t rcv_wnd, const uint8_t *data, uint32_t len, int with_mss)
{
    uint32_t hlen = with_mss ? 24 : 20;
    wr16(seg, lport); wr16(seg + 2, rport);
    wr32(seg + 4, seqn); wr32(seg + 8, ack);
    seg[12] = (uint8_t)((hlen / 4) << 4); seg[13] = flags;
    wr16(seg + 14, rcv_wnd);
    wr16(seg + 16, 0); wr16(seg + 18, 0);
    if (with_mss) { seg[20] = 2; seg[21] = 4; wr16(seg + 22, 1460); }
    if (len) memcpy(seg + hlen, data, len);
    wr16(seg + 16, net_l4_checksum(net_active_ip(), rip, 6, seg, hlen + len));
    net_ip_send(rip, 6, seg, hlen + len);
    tcp_stats.tx_bytes += len;
}

static void conn_send(struct tcp_conn *c, uint32_t seqn, uint8_t flags, const uint8_t *d, uint32_t n)
{
    net_set_active_ip(c->lip);
    send_raw(c->rip, c->lport, c->rport, seqn, c->rcv_nxt, flags, c->rcv_wnd, d, n, 0);
}

static void conn_free(struct tcp_conn *c, int notify)
{
    if (c->state == FREE) return;
    c->state = FREE;
    tcp_stats.closed++;
    if (notify && c->on_event) c->on_event(c, TCP_EV_CLOSED);
}

/* send whatever is allowed: unsent data within the peer window, then FIN */
static int conn_flush(struct tcp_conn *c)
{
    int sent = 0;
    uint32_t end = c->snd_una + c->snd_len;
    while (seq_diff(end, c->snd_nxt) > 0) {
        uint32_t inflight = c->snd_nxt - c->snd_una;
        uint32_t wnd = c->snd_wnd;
        if (wnd > 4 * TCP_MSS) wnd = 4 * TCP_MSS;
        if (inflight >= wnd) break;
        uint32_t n = end - c->snd_nxt;
        if (n > TCP_MSS) n = TCP_MSS;
        if (n > wnd - inflight) n = wnd - inflight;
        uint32_t off = c->snd_nxt - c->snd_una;
        uint8_t flags = F_ACK | F_PSH;
        int last = (c->snd_nxt + n == end);
        if (last && c->fin_queued) { flags |= F_FIN; }
        conn_send(c, c->snd_nxt, flags, c->sndbuf + off, n);
        c->snd_nxt += n;
        if (last && c->fin_queued) { c->snd_nxt++; c->fin_sent = 1; }
        sent++;
        if (!sent) break;
    }
    if (c->fin_queued && !c->fin_sent && c->snd_nxt == end) {
        conn_send(c, c->snd_nxt, F_ACK | F_FIN, 0, 0);
        c->snd_nxt++;
        c->fin_sent = 1;
        sent++;
    }
    if (sent) c->t_rtx = micros();
    return sent;
}

static void conn_ack_update(struct tcp_conn *c, uint32_t ack, uint16_t wnd)
{
    c->snd_wnd = wnd;
    if (seq_diff(ack, c->snd_una) <= 0 || seq_diff(ack, c->snd_nxt) > 0)
        return;
    uint32_t acked = ack - c->snd_una;
    uint32_t data_acked = acked > c->snd_len ? c->snd_len : acked;
    if (data_acked) {
        if (c->snd_len > data_acked)
            memmove(c->sndbuf, c->sndbuf + data_acked, c->snd_len - data_acked);
        c->snd_len -= data_acked;
    }
    if (c->fin_sent && acked > data_acked)
        c->fin_acked = 1;
    c->snd_una = ack;
    c->retries = 0;
    c->t_rtx = micros();
}

/* ---------------------------------------------------------------- API */
int tcp_listen(uint16_t port, tcp_data_cb_t on_data, tcp_event_cb_t on_event)
{
    for (int i = 0; i < TCP_MAX_LISTEN; i++)
        if (!listeners[i].port || listeners[i].port == port) {
            listeners[i].port = port; listeners[i].on_data = on_data; listeners[i].on_event = on_event;
            return 0;
        }
    return -1;
}

tcp_conn_t *tcp_connect(const uint8_t dst_ip[4], uint16_t dst_port,
                        tcp_data_cb_t on_data, tcp_event_cb_t on_event)
{
    struct tcp_conn *c = 0;
    for (int i = 0; i < TCP_MAX_CONN; i++)
        if (conns[i].state == FREE) { c = &conns[i]; break; }
    if (!c) return 0;
    uint32_t now = micros();
    memset(c, 0, sizeof *c);
    memcpy(c->rip, dst_ip, 4);
    memcpy(c->lip, net_select_src_ip(dst_ip), 4);
    c->rport = dst_port;
    c->lport = next_ephemeral++;
    if (next_ephemeral < 49152) next_ephemeral = 49152;
    c->on_data = on_data;
    c->on_event = on_event;
    c->snd_una = (now << 4) ^ 0xA5A50000u ^ ((uint32_t)(c - conns) << 24);
    c->snd_nxt = c->snd_una + 1;
    c->snd_wnd = 1460;
    c->rcv_wnd = 16384;
    c->state = SYN_SENT;
    c->t_last = c->t_rtx = now;
    net_set_active_ip(c->lip);
    send_raw(c->rip, c->lport, c->rport, c->snd_una, 0, F_SYN, c->rcv_wnd, 0, 0, 1);
    return (c->state != FREE) ? c : 0;
}

int tcp_is_established(tcp_conn_t *c)
{
    return c && (c->state == ESTABLISHED || c->state == CLOSE_WAIT);
}

int tcp_send(tcp_conn_t *c, const void *data, uint32_t len)
{
    if (!c) return -1;
    if (c->state != ESTABLISHED && c->state != CLOSE_WAIT) return -1;
    if (c->fin_queued) return -1;
    uint32_t space = TCP_SNDBUF - c->snd_len;
    if (len > space) len = space;
    memcpy(c->sndbuf + c->snd_len, data, len);
    c->snd_len += len;
    c->t_last = c->t_rtx = micros();
    conn_flush(c);
    return (int)len;
}

uint32_t tcp_send_space(tcp_conn_t *c)
{
    return (c && (c->state == ESTABLISHED || c->state == CLOSE_WAIT)) ? TCP_SNDBUF - c->snd_len : 0;
}

void tcp_close(tcp_conn_t *c)
{
    if (!c || c->state == FREE || c->fin_queued) return;
    c->fin_queued = 1;
    c->t_last = c->t_rtx = micros();
    if (c->state == ESTABLISHED) c->state = FIN_WAIT_1;
    else if (c->state == CLOSE_WAIT) c->state = LAST_ACK;
    else if (c->state == SYN_RCVD || c->state == SYN_SENT) {
        conn_send(c, c->snd_nxt, F_RST | F_ACK, 0, 0);
        conn_free(c, 1);
        return;
    }
    conn_flush(c);
}

void tcp_abort(tcp_conn_t *c)
{
    if (!c || c->state == FREE) return;
    conn_send(c, c->snd_nxt, F_RST | F_ACK, 0, 0);
    tcp_stats.resets++;
    conn_free(c, 1);
}

void tcp_set_rcv_wnd(tcp_conn_t *c, uint16_t wnd)
{
    if (!c) return;
    if (wnd == 0xFFFFu) {
        c->rcv_wnd = 0;
        c->rx_full = 1;
        return;
    }
    uint16_t old = c->rcv_wnd;
    c->rx_full = 0;
    c->rcv_wnd = wnd;
    if (c->state == ESTABLISHED && old == 0 && wnd > 0)
        conn_send(c, c->snd_nxt, F_ACK, 0, 0);
}

void tcp_set_user(tcp_conn_t *c, void *user) { if (c) c->user = user; }
void *tcp_user(tcp_conn_t *c) { return c ? c->user : 0; }
uint16_t tcp_local_port(tcp_conn_t *c) { return c->lport; }
uint16_t tcp_remote_port(tcp_conn_t *c) { return c->rport; }
const uint8_t *tcp_remote_ip(tcp_conn_t *c) { return c->rip; }
int tcp_index(tcp_conn_t *c) { return (int)(c - conns); }
tcp_conn_t *tcp_conn_by_index(int i) { return (i >= 0 && i < TCP_MAX_CONN && conns[i].state != FREE) ? &conns[i] : 0; }

/* -------------------------------------------------------------- input */
static struct tcp_conn *find(const uint8_t rip[4], uint16_t rport, uint16_t lport)
{
    for (int i = 0; i < TCP_MAX_CONN; i++)
        if (conns[i].state != FREE && conns[i].rport == rport && conns[i].lport == lport &&
            memcmp(conns[i].rip, rip, 4) == 0)
            return &conns[i];
    return 0;
}

static struct listener *find_listener(uint16_t port)
{
    for (int i = 0; i < TCP_MAX_LISTEN; i++)
        if (listeners[i].port == port) return &listeners[i];
    return 0;
}

void tcp_input(const uint8_t src_ip[4], const uint8_t *s, uint32_t len)
{
    if (len < 20) return;
    uint16_t sport = rd16(s), dport = rd16(s + 2);
    uint32_t seqn = rd32(s + 4), ack = rd32(s + 8);
    uint32_t hlen = (s[12] >> 4) * 4;
    uint8_t flags = s[13];
    uint16_t wnd = rd16(s + 14);
    if (hlen < 20 || hlen > len) return;
    const uint8_t *data = s + hlen;
    uint32_t dlen = len - hlen;
    uint32_t now = micros();

    struct tcp_conn *c = find(src_ip, sport, dport);

    if (!c) {
        struct listener *l = find_listener(dport);
        if (flags & F_RST) return;
        if (!l || !(flags & F_SYN) || (flags & F_ACK)) {
            /* not for us: answer with RST so the peer gives up quickly */
            if (flags & F_ACK) send_raw(src_ip, dport, sport, ack, 0, F_RST, 0, 0, 0, 0);
            else send_raw(src_ip, dport, sport, 0, seqn + dlen + 1, F_RST | F_ACK, 0, 0, 0, 0);
            return;
        }
        for (int i = 0; i < TCP_MAX_CONN; i++)
            if (conns[i].state == FREE) { c = &conns[i]; break; }
        if (!c) { tcp_stats.dropped++; return; }
        memset(c, 0, sizeof *c);
        memcpy(c->rip, src_ip, 4);
        memcpy(c->lip, net_active_ip(), 4);
        c->rport = sport; c->lport = dport; c->l = l;
        c->on_data = l->on_data; c->on_event = l->on_event;
        c->rcv_nxt = seqn + 1;
        c->snd_una = (now << 4) ^ 0x5A5A0000u ^ ((uint32_t)(c - conns) << 24);
        c->snd_nxt = c->snd_una + 1;
        c->snd_wnd = wnd ? wnd : 1460;
        c->rcv_wnd = 16384;
        c->state = SYN_RCVD;
        c->t_last = c->t_rtx = now;
        tcp_stats.syn++;
        send_raw(c->rip, c->lport, c->rport, c->snd_una, c->rcv_nxt, F_SYN | F_ACK, c->rcv_wnd, 0, 0, 1);
        return;
    }

    c->t_last = now;

    if (flags & F_RST) {
        tcp_stats.resets++;
        conn_free(c, 1);
        return;
    }

    if (c->state == SYN_SENT) {
        if ((flags & (F_SYN | F_ACK)) == (F_SYN | F_ACK) && ack == c->snd_nxt) {
            c->rcv_nxt = seqn + 1;
            c->snd_una = c->snd_nxt;
            c->snd_wnd = wnd ? wnd : 1460;
            c->state = ESTABLISHED;
            tcp_stats.established++;
            conn_send(c, c->snd_nxt, F_ACK, 0, 0);
            if (c->on_event) c->on_event(c, TCP_EV_CONNECTED);
        }
        return;
    }

    if (c->state == SYN_RCVD) {
        if (flags & F_SYN) {                                    /* SYN retransmitted */
            net_set_active_ip(c->lip);
            send_raw(c->rip, c->lport, c->rport, c->snd_una, c->rcv_nxt, F_SYN | F_ACK, c->rcv_wnd, 0, 0, 1);
            return;
        }
        if (!(flags & F_ACK) || ack != c->snd_nxt) return;
        c->snd_una = c->snd_nxt;
        c->snd_wnd = wnd;
        c->state = ESTABLISHED;
        tcp_stats.established++;
        tcp_stats.accepted++;
        if (c->on_event) c->on_event(c, TCP_EV_ACCEPT);
        if (c->state == FREE) return;
        /* fall through: this ACK may carry data */
    }

    if (flags & F_ACK)
        conn_ack_update(c, ack, wnd);

    if (seqn != c->rcv_nxt) {                                   /* out of order / duplicate */
        if (dlen || (flags & F_FIN))
            conn_send(c, c->snd_nxt, F_ACK, 0, 0);
        goto progress;
    }

    if (dlen) {
        if (c->rx_full) {
            conn_send(c, c->snd_nxt, F_ACK, 0, 0);
            goto progress;
        }
        tcp_stats.rx_bytes += dlen;
        c->rcv_nxt += dlen;
        if (c->state == ESTABLISHED || c->state == FIN_WAIT_1 || c->state == FIN_WAIT_2) {
            if (c->on_data) c->on_data(c, data, dlen);
            if (c->state == FREE) return;
            c->t_last = micros();
        }
    }
    if (flags & F_FIN) {
        c->rcv_nxt++;
        c->peer_fin = 1;
        if (c->state == ESTABLISHED) {
            c->state = CLOSE_WAIT;
            if (c->on_event) c->on_event(c, TCP_EV_PEER_FIN);
            if (c->state == FREE) return;
        } else if (c->state == FIN_WAIT_1 || c->state == FIN_WAIT_2) {
            /* peer closed too; done once our FIN is acked */
        }
        if (!conn_flush(c))
            conn_send(c, c->snd_nxt, F_ACK, 0, 0);
    } else if (dlen) {
        if (!conn_flush(c))
            conn_send(c, c->snd_nxt, F_ACK, 0, 0);
    } else {
        conn_flush(c);                                          /* window may have opened */
    }

progress:
    if (c->state == FIN_WAIT_1 && c->fin_acked) c->state = FIN_WAIT_2;
    /* Both FINs done, in whichever order they happened.  The peer's FIN may
     * have arrived before the ACK of ours (a client that closes as soon as it
     * has Content-Length bytes, e.g. Python urllib): requiring the FIN flag
     * on the current segment left such connections in FIN_WAIT_2 until the
     * 10 s idle timeout, and 16 of them exhausted the connection table. */
    if ((c->state == FIN_WAIT_2 || c->state == FIN_WAIT_1) && c->fin_acked && c->peer_fin)
        conn_free(c, 1);
    else if (c->state == LAST_ACK && c->fin_acked)
        conn_free(c, 1);
}

static int in_tcp_poll = 0;

void tcp_poll(void)
{
    if (in_tcp_poll) return;
    in_tcp_poll = 1;
    uint32_t now = micros();
    for (int i = 0; i < TCP_MAX_CONN; i++) {
        struct tcp_conn *c = &conns[i];
        if (c->state == FREE) continue;
        uint32_t idle_limit = (c->state == ESTABLISHED || c->state == CLOSE_WAIT) ? TCP_IDLE_S * 1000000u : 10000000u;
        if (((now - c->t_last) & TC_DATA_MAX) > idle_limit) {
            tcp_stats.timeouts++;
            conn_send(c, c->snd_nxt, F_RST | F_ACK, 0, 0);
            conn_free(c, 1);
            continue;
        }
        int unacked = seq_diff(c->snd_nxt, c->snd_una) > 0;
        if (c->state == SYN_RCVD || c->state == SYN_SENT) {
            if (((now - c->t_rtx) & TC_DATA_MAX) > TCP_RTX_MS * 1000u) {
                if (++c->retries > TCP_MAX_RETRIES) { conn_free(c, 1); tcp_stats.timeouts++; continue; }
                c->t_rtx = now;
                tcp_stats.retransmits++;
                net_set_active_ip(c->lip);
                if (c->state == SYN_RCVD)
                    send_raw(c->rip, c->lport, c->rport, c->snd_una, c->rcv_nxt, F_SYN | F_ACK, c->rcv_wnd, 0, 0, 1);
                else
                    send_raw(c->rip, c->lport, c->rport, c->snd_una, 0, F_SYN, c->rcv_wnd, 0, 0, 1);
            }
        } else if (unacked && ((now - c->t_rtx) & TC_DATA_MAX) > TCP_RTX_MS * 1000u) {
            if (++c->retries > TCP_MAX_RETRIES) {
                tcp_stats.timeouts++;
                conn_send(c, c->snd_nxt, F_RST | F_ACK, 0, 0);
                conn_free(c, 1);
                continue;
            }
            c->t_rtx = now;
            tcp_stats.retransmits++;
            c->snd_nxt = c->snd_una;                            /* go back N */
            c->fin_sent = 0;
            conn_flush(c);
        }
    }
    in_tcp_poll = 0;
}
