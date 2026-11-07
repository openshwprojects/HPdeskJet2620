/*
 * lwip_sockets.c - BSD / lwIP socket & DNS layer on top of OpenRTL8196C net.c/tcp.c.
 *
 * Important note for OpenBeken:
 *   new_http.c treats request->fd == 0 as local unit-test mode and skips send().
 *   All valid socket descriptors returned by lwip_socket() / lwip_accept() are
 *   therefore 1-based (fd = slot_index + 1, range 1..MAX_BSD_SOCKETS).
 */
#include "rtl8196c.h"
#include "net.h"
#include "tcp.h"
#include "timer.h"
#include "rtos.h"
#include "heap.h"
#include "libc.h"
#include "uart.h"

#include "obk_shim/lwip/sockets.h"
#include "obk_shim/lwip/netdb.h"
#include "obk_shim/lwip/dns.h"

#define MAX_BSD_SOCKETS  24
#define SOCK_RX_BUF_SIZE 32768
#define MAX_ACCEPT_QUEUE 12

enum bsd_sock_type {
    BSD_UNUSED = 0,
    BSD_TCP_LISTEN,
    BSD_TCP_CONN,
    BSD_UDP
};

struct bsd_sock {
    enum bsd_sock_type kind;
    int                type;          /* SOCK_STREAM or SOCK_DGRAM */
    int                nonblocking;
    uint32_t           rcv_timeout_ms;
    uint32_t           accept_ms;
    uint16_t           local_port;
    uint8_t            remote_ip[4];
    uint16_t           remote_port;
    tcp_conn_t        *tcp;
    int                peer_closed;
    uint8_t            first_recv_done;
    uint8_t           *rx_buf;
    uint32_t           rx_len;
    int                accept_q[MAX_ACCEPT_QUEUE];
    int                accept_cnt;
};

static struct bsd_sock socks[MAX_BSD_SOCKETS];
static uint16_t next_udp_ephemeral = 50000;

static struct bsd_sock *get_sock(int fd)
{
    int idx = fd - 1;
    if (idx < 0 || idx >= MAX_BSD_SOCKETS) {
        errno = EBADF;
        return 0;
    }
    if (socks[idx].kind == BSD_UNUSED) {
        errno = EBADF;
        return 0;
    }
    return &socks[idx];
}

static int alloc_sock_slot(void)
{
    for (int i = 0; i < MAX_BSD_SOCKETS; i++) {
        if (socks[i].kind == BSD_UNUSED) {
            memset(&socks[i], 0, sizeof(socks[i]));
            socks[i].rcv_timeout_ms = 5000;
            return i + 1; /* 1-based fd */
        }
    }
    errno = ENFILE;
    return -1;
}

static void free_sock_slot(struct bsd_sock *s)
{
    if (!s) return;
    if (s->kind == BSD_UDP && s->local_port != 0)
        net_udp_listen(s->local_port, 0);
    if (s->rx_buf) {
        free(s->rx_buf);
        s->rx_buf = 0;
    }
    memset(s, 0, sizeof(*s));
    s->kind = BSD_UNUSED;
}

int LWIP_GetMaxSockets(void)
{
    return MAX_BSD_SOCKETS;
}

int LWIP_GetActiveSockets(void)
{
    int cnt = 0;
    for (int i = 0; i < MAX_BSD_SOCKETS; i++) {
        if (socks[i].kind != BSD_UNUSED)
            cnt++;
    }
    return cnt;
}

/* ----------------------------------------------------------- TCP callbacks */
static void on_tcp_data(tcp_conn_t *c, const uint8_t *data, uint32_t len)
{
    struct bsd_sock *s = (struct bsd_sock *)tcp_user(c);
    if (!s || s->kind != BSD_TCP_CONN)
        return;
    if (!s->rx_buf) {
        s->rx_buf = (uint8_t *)malloc(SOCK_RX_BUF_SIZE);
        if (!s->rx_buf) return;
    }
    uint32_t space = SOCK_RX_BUF_SIZE - s->rx_len;
    uint32_t copy = (len > space) ? space : len;
    if (copy > 0) {
        memcpy(s->rx_buf + s->rx_len, data, copy);
        s->rx_len += copy;
    }
    if (s->rx_len >= SOCK_RX_BUF_SIZE - 1500u)
        tcp_set_rcv_wnd(c, 0xFFFFu);
    else if (s->rx_len >= 16384u)
        tcp_set_rcv_wnd(c, 0);
}

static void on_tcp_event(tcp_conn_t *c, int event)
{
    if (event == TCP_EV_ACCEPT) {
        /* Find listening socket for this local port */
        uint16_t lport = tcp_local_port(c);
        struct bsd_sock *ls = 0;
        for (int i = 0; i < MAX_BSD_SOCKETS; i++) {
            if (socks[i].kind == BSD_TCP_LISTEN && socks[i].local_port == lport) {
                ls = &socks[i];
                break;
            }
        }
        if (!ls || ls->accept_cnt >= MAX_ACCEPT_QUEUE) {
            tcp_abort(c);
            return;
        }
        int child_fd = alloc_sock_slot();
        if (child_fd < 0) {
            tcp_abort(c);
            return;
        }
        struct bsd_sock *cs = &socks[child_fd - 1];
        cs->kind       = BSD_TCP_CONN;
        cs->type       = SOCK_STREAM;
        cs->local_port = lport;
        cs->accept_ms  = timer_ms();
        memcpy(cs->remote_ip, tcp_remote_ip(c), 4);
        cs->remote_port = tcp_remote_port(c);
        cs->tcp         = c;
        cs->rx_buf      = (uint8_t *)malloc(SOCK_RX_BUF_SIZE);
        cs->rx_len      = 0;
        tcp_set_user(c, cs);
        ls->accept_q[ls->accept_cnt++] = child_fd;
        return;
    }

    struct bsd_sock *s = (struct bsd_sock *)tcp_user(c);
    if (!s)
        return;

    if (event == TCP_EV_PEER_FIN) {
        s->peer_closed = 1;
    } else if (event == TCP_EV_CLOSED) {
        s->peer_closed = 1;
        s->tcp = 0;
    }
}

/* ----------------------------------------------------------- UDP callbacks */
static void on_udp_packet(const uint8_t src_ip[4], uint16_t src_port, uint16_t dst_port,
                          const uint8_t *data, uint32_t len)
{
    if (len > 1472u)
        len = 1472u;
    for (int i = 0; i < MAX_BSD_SOCKETS; i++) {
        struct bsd_sock *s = &socks[i];
        if (s->kind == BSD_UDP && s->local_port == dst_port) {
            if (!s->rx_buf) {
                s->rx_buf = (uint8_t *)malloc(SOCK_RX_BUF_SIZE);
                if (!s->rx_buf) return;
            }
            if (s->rx_len + 8u + len > SOCK_RX_BUF_SIZE)
                continue;
            uint8_t *p = s->rx_buf + s->rx_len;
            memcpy(p, src_ip, 4);
            p[4] = (uint8_t)(src_port >> 8);
            p[5] = (uint8_t)(src_port & 0xFF);
            p[6] = (uint8_t)(len >> 8);
            p[7] = (uint8_t)(len & 0xFF);
            memcpy(p + 8, data, len);
            s->rx_len += 8u + len;
        }
    }
}

/* ----------------------------------------------------------- BSD Socket API */
int lwip_socket(int domain, int type, int protocol)
{
    (void)domain;
    (void)protocol;
    int fd = alloc_sock_slot();
    if (fd < 0)
        return -1;
    struct bsd_sock *s = &socks[fd - 1];
    s->type = type;
    if (type == SOCK_DGRAM) {
        s->kind = BSD_UDP;
        s->local_port = next_udp_ephemeral++;
        if (next_udp_ephemeral < 50000)
            next_udp_ephemeral = 50000;
        net_udp_listen(s->local_port, on_udp_packet);
    } else {
        s->kind = BSD_TCP_CONN;
    }
    return fd;
}

int lwip_bind(int fd, const struct sockaddr *name, socklen_t namelen)
{
    (void)namelen;
    struct bsd_sock *s = get_sock(fd);
    if (!s || !name) return -1;
    const struct sockaddr_in *sin = (const struct sockaddr_in *)name;
    uint16_t port = ntohs(sin->sin_port);
    if (port == 0)
        port = next_udp_ephemeral++;
    if (s->type == SOCK_DGRAM && s->local_port != 0 && s->local_port != port)
        net_udp_listen(s->local_port, 0);
    s->local_port = port;
    if (s->type == SOCK_DGRAM) {
        net_udp_listen(port, on_udp_packet);
    }
    return 0;
}

int lwip_listen(int fd, int backlog)
{
    (void)backlog;
    struct bsd_sock *s = get_sock(fd);
    if (!s) return -1;
    s->kind = BSD_TCP_LISTEN;
    if (tcp_listen(s->local_port, on_tcp_data, on_tcp_event) < 0) {
        errno = EADDRINUSE;
        return -1;
    }
    return 0;
}

int lwip_accept(int fd, struct sockaddr *addr, socklen_t *addrlen)
{
    struct bsd_sock *s = get_sock(fd);
    if (!s || s->kind != BSD_TCP_LISTEN) {
        errno = EBADF;
        return -1;
    }

    rtos_yield();
    if (s->accept_cnt == 0) {
        errno = EWOULDBLOCK;
        return -1;
    }

    uint32_t now = timer_ms();
    int pick_idx = -1;

    /* For HTTP server port 80, only hand off connections to worker threads once
     * the client has actually sent request bytes (or closed), so Chrome/Edge
     * speculative preconnect sockets never tie up worker threads. */
    for (int i = 0; i < s->accept_cnt; ) {
        struct bsd_sock *cs = get_sock(s->accept_q[i]);
        if (!cs) {
            for (int j = i + 1; j < s->accept_cnt; j++)
                s->accept_q[j - 1] = s->accept_q[j];
            s->accept_cnt--;
            continue;
        }
        if (s->local_port != 80 || cs->rx_len > 0 || cs->peer_closed) {
            pick_idx = i;
            break;
        }
        /* Reap idle preconnect sockets that sent 0 bytes for > 3 seconds */
        if ((uint32_t)(now - cs->accept_ms) > 3000u) {
            int stale_fd = s->accept_q[i];
            for (int j = i + 1; j < s->accept_cnt; j++)
                s->accept_q[j - 1] = s->accept_q[j];
            s->accept_cnt--;
            lwip_close(stale_fd);
            continue;
        }
        i++;
    }

    if (pick_idx < 0) {
        errno = EWOULDBLOCK;
        return -1;
    }

    int child_fd = s->accept_q[pick_idx];
    for (int i = pick_idx + 1; i < s->accept_cnt; i++)
        s->accept_q[i - 1] = s->accept_q[i];
    s->accept_cnt--;

    struct bsd_sock *cs = get_sock(child_fd);
    if (cs && addr && addrlen && *addrlen >= sizeof(struct sockaddr_in)) {
        struct sockaddr_in *sin = (struct sockaddr_in *)addr;
        memset(sin, 0, sizeof(*sin));
        sin->sin_family = AF_INET;
        sin->sin_port   = htons(cs->remote_port);
        memcpy(&sin->sin_addr.s_addr, cs->remote_ip, 4);
        *addrlen = sizeof(struct sockaddr_in);
    }
    return child_fd;
}

int lwip_connect(int fd, const struct sockaddr *name, socklen_t namelen)
{
    (void)namelen;
    struct bsd_sock *s = get_sock(fd);
    if (!s || !name) return -1;
    const struct sockaddr_in *sin = (const struct sockaddr_in *)name;
    uint8_t dst_ip[4];
    memcpy(dst_ip, &sin->sin_addr.s_addr, 4);
    uint16_t dst_port = ntohs(sin->sin_port);

    memcpy(s->remote_ip, dst_ip, 4);
    s->remote_port = dst_port;

    if (s->type == SOCK_DGRAM)
        return 0;

    s->rx_buf = (uint8_t *)malloc(SOCK_RX_BUF_SIZE);
    s->rx_len = 0;
    s->peer_closed = 0;
    tcp_conn_t *c = tcp_connect(dst_ip, dst_port, on_tcp_data, on_tcp_event);
    if (!c) {
        errno = ENOBUFS;
        return -1;
    }
    s->tcp = c;
    tcp_set_user(c, s);

    if (s->nonblocking) {
        errno = EINPROGRESS;
        return -1;
    }

    uint32_t start = xTaskGetTickCount();
    while (s->tcp && !tcp_is_established(s->tcp) && !s->peer_closed) {
        if ((uint32_t)(xTaskGetTickCount() - start) >= s->rcv_timeout_ms) {
            errno = ETIMEDOUT;
            return -1;
        }
        rtos_yield();
    }
    if (!s->tcp || !tcp_is_established(s->tcp)) {
        errno = ECONNREFUSED;
        return -1;
    }
    return 0;
}

int lwip_send(int fd, const void *data, size_t size, int flags)
{
    (void)flags;
    struct bsd_sock *s = get_sock(fd);
    if (!s || !data) return -1;
    if (size == 0) return 0;

    if (s->type == SOCK_DGRAM) {
        int r = net_udp_send(s->remote_ip, s->local_port, s->remote_port, data, (uint32_t)size);
        return (r == 0) ? (int)size : -1;
    }

    const uint8_t *p = (const uint8_t *)data;
    size_t sent = 0;
    uint32_t start = xTaskGetTickCount();
    while (sent < size) {
        net_poll();
        if (!s->tcp || !tcp_is_established(s->tcp)) {
            errno = ECONNRESET;
            return sent > 0 ? (int)sent : -1;
        }
        uint32_t space = tcp_send_space(s->tcp);
        if (space > 0) {
            uint32_t chunk = (uint32_t)(size - sent);
            if (chunk > space) chunk = space;
            int n = tcp_send(s->tcp, p + sent, chunk);
            if (n < 0) {
                errno = ECONNRESET;
                return sent > 0 ? (int)sent : -1;
            }
            sent += (size_t)n;
            start = xTaskGetTickCount();
        } else {
            if ((uint32_t)(xTaskGetTickCount() - start) > 3000) {
                errno = ETIMEDOUT;
                return sent > 0 ? (int)sent : -1;
            }
            rtos_yield();
        }
    }
    return (int)sent;
}

static uint16_t custom_ntp_port = 0;

int lwip_sendto(int fd, const void *data, size_t size, int flags,
                const struct sockaddr *to, socklen_t tolen)
{
    (void)flags;
    (void)tolen;
    struct bsd_sock *s = get_sock(fd);
    if (!s || !data) return -1;
    if (!to)
        return lwip_send(fd, data, size, flags);

    const struct sockaddr_in *sin = (const struct sockaddr_in *)to;
    uint8_t dst_ip[4];
    memcpy(dst_ip, &sin->sin_addr.s_addr, 4);
    uint16_t dst_port = ntohs(sin->sin_port);
    if (dst_port == 123 && custom_ntp_port != 0)
        dst_port = custom_ntp_port;
    if (s->local_port == 0) {
        s->local_port = next_udp_ephemeral++;
        if (next_udp_ephemeral < 50000) next_udp_ephemeral = 50000;
        net_udp_listen(s->local_port, on_udp_packet);
    }
    net_set_active_ip(net_select_src_ip(dst_ip));
    int r = net_udp_send(dst_ip, s->local_port, dst_port, data, (uint32_t)size);
    return (r == 0) ? (int)size : -1;
}

int lwip_recvfrom(int fd, void *mem, size_t len, int flags,
                  struct sockaddr *from, socklen_t *fromlen)
{
    struct bsd_sock *s = get_sock(fd);
    if (!s || !mem) return -1;
    if (len == 0) return 0;

    int nonblock = s->nonblocking || (flags & MSG_DONTWAIT);
    uint32_t start = xTaskGetTickCount();

    if (s->rx_len < 16384u)
        net_poll();
    while (s->rx_len == 0) {
        if (nonblock) {
            net_poll();
            if (s->rx_len > 0)
                break;
            if (s->peer_closed || (s->type == SOCK_STREAM && !s->tcp))
                return 0;
            errno = EWOULDBLOCK;
            return -1;
        }
        rtos_yield();
        if (s->rx_len > 0)
            break;
        if (s->peer_closed || (s->type == SOCK_STREAM && !s->tcp))
            return 0;
        if ((uint32_t)(xTaskGetTickCount() - start) >= s->rcv_timeout_ms) {
            errno = EWOULDBLOCK;
            return -1;
        }
    }

    uint32_t max_chunk = (uint32_t)len;
    if (s->type == SOCK_STREAM && s->local_port == 80 && !s->first_recv_done) {
        if (max_chunk > 1460u)
            max_chunk = 1460u;
        /* Coalesce initial HTTP header segments up to one MSS on the first recv() */
        if (s->rx_len < max_chunk && !s->peer_closed) {
            uint32_t cstart = xTaskGetTickCount();
            while ((uint32_t)(xTaskGetTickCount() - cstart) < 2 && s->rx_len < max_chunk && !s->peer_closed)
                rtos_yield();
        }
        if (!(flags & MSG_PEEK))
            s->first_recv_done = 1;
    }

    uint32_t copy;
    if (s->type == SOCK_DGRAM) {
        if (s->rx_len < 8u) {
            s->rx_len = 0;
            errno = EWOULDBLOCK;
            return -1;
        }
        memcpy(s->remote_ip, s->rx_buf, 4);
        s->remote_port = (uint16_t)(((uint16_t)s->rx_buf[4] << 8) | s->rx_buf[5]);
        uint32_t pkt_len = ((uint32_t)s->rx_buf[6] << 8) | s->rx_buf[7];
        if (8u + pkt_len > s->rx_len)
            pkt_len = s->rx_len - 8u;
        copy = (pkt_len < max_chunk) ? pkt_len : max_chunk;
        memcpy(mem, s->rx_buf + 8, copy);
        if (!(flags & MSG_PEEK)) {
            uint32_t consume = 8u + pkt_len;
            uint32_t rem = (s->rx_len > consume) ? (s->rx_len - consume) : 0;
            if (rem > 0)
                memmove(s->rx_buf, s->rx_buf + consume, rem);
            s->rx_len = rem;
        }
    } else {
        copy = (s->rx_len < max_chunk) ? s->rx_len : max_chunk;
        memcpy(mem, s->rx_buf, copy);
        if (!(flags & MSG_PEEK)) {
            uint32_t rem = s->rx_len - copy;
            if (rem > 0)
                memmove(s->rx_buf, s->rx_buf + copy, rem);
            s->rx_len = rem;
            if (s->tcp && rem < SOCK_RX_BUF_SIZE - 1500u)
                tcp_set_rcv_wnd(s->tcp, 0);
            net_poll();
            if (s->tcp && s->rx_len < 16384u)
                tcp_set_rcv_wnd(s->tcp, 16384);
        }
    }

    if (from && fromlen && *fromlen >= sizeof(struct sockaddr_in)) {
        struct sockaddr_in *sin = (struct sockaddr_in *)from;
        memset(sin, 0, sizeof(*sin));
        sin->sin_family = AF_INET;
        sin->sin_port   = htons(s->remote_port);
        memcpy(&sin->sin_addr.s_addr, s->remote_ip, 4);
        *fromlen = sizeof(struct sockaddr_in);
    }
    return (int)copy;
}

int lwip_recv(int fd, void *mem, size_t len, int flags)
{
    return lwip_recvfrom(fd, mem, len, flags, 0, 0);
}

int lwip_read(int fd, void *mem, size_t len)
{
    return lwip_recv(fd, mem, len, 0);
}

int lwip_write(int fd, const void *data, size_t size)
{
    return lwip_send(fd, data, size, 0);
}

int lwip_close(int fd)
{
    struct bsd_sock *s = get_sock(fd);
    if (!s) return -1;
    if (s->kind == BSD_TCP_CONN && s->tcp) {
        uint32_t t0 = timer_ms();
        while (s->tcp && tcp_is_established(s->tcp) &&
               tcp_send_space(s->tcp) < TCP_SNDBUF &&
               (uint32_t)(timer_ms() - t0) < 1000u) {
            net_poll();
            tcp_poll();
        }
        if (s->tcp) {
            tcp_set_user(s->tcp, 0);
            tcp_close(s->tcp);
            net_poll();
            s->tcp = 0;
        }
    } else if (s->kind == BSD_TCP_LISTEN) {
        for (int i = 0; i < s->accept_cnt; i++)
            lwip_close(s->accept_q[i]);
    }
    free_sock_slot(s);
    return 0;
}

int lwip_shutdown(int fd, int how)
{
    (void)how;
    struct bsd_sock *s = get_sock(fd);
    if (!s) return -1;
    if (s->kind == BSD_TCP_CONN && s->tcp)
        tcp_close(s->tcp);
    return 0;
}

int lwip_setsockopt(int fd, int level, int optname, const void *optval, socklen_t optlen)
{
    int saved_errno = errno;
    struct bsd_sock *s = get_sock(fd);
    if (!s) {
        errno = saved_errno;
        return -1;
    }
    if (level == SOL_SOCKET && optname == SO_RCVTIMEO && optval) {
        uint32_t ms = 5000;
        if (optlen >= sizeof(struct timeval)) {
            const struct timeval *tv = (const struct timeval *)optval;
            ms = (uint32_t)(tv->tv_sec * 1000 + tv->tv_usec / 1000);
        } else if (optlen >= sizeof(uint32_t)) {
            ms = *(const uint32_t *)optval;
        }
        if (s->local_port == 80 && ms > 2000u)
            ms = 2000u;
        s->rcv_timeout_ms = ms ? ms : 2000u;
    } else if (level == IPPROTO_IP && optname == IP_ADD_MEMBERSHIP && optval && optlen >= sizeof(struct ip_mreq)) {
        const struct ip_mreq *mreq = (const struct ip_mreq *)optval;
        net_igmp_join((const uint8_t *)&mreq->imr_multiaddr.s_addr);
    }
    return 0;
}

int lwip_getsockopt(int fd, int level, int optname, void *optval, socklen_t *optlen)
{
    (void)level;
    struct bsd_sock *s = get_sock(fd);
    if (!s) return -1;
    if (optname == SO_ERROR && optval && optlen && *optlen >= sizeof(int)) {
        *(int *)optval = 0;
    }
    return 0;
}

int lwip_getsockname(int fd, struct sockaddr *name, socklen_t *namelen)
{
    struct bsd_sock *s = get_sock(fd);
    if (!s || !name || !namelen || *namelen < sizeof(struct sockaddr_in))
        return -1;
    struct sockaddr_in *sin = (struct sockaddr_in *)name;
    memset(sin, 0, sizeof(*sin));
    sin->sin_family = AF_INET;
    sin->sin_port   = htons(s->local_port);
    memcpy(&sin->sin_addr.s_addr, net_ip(), 4);
    *namelen = sizeof(struct sockaddr_in);
    return 0;
}

int lwip_getpeername(int fd, struct sockaddr *name, socklen_t *namelen)
{
    struct bsd_sock *s = get_sock(fd);
    if (!s || !name || !namelen || *namelen < sizeof(struct sockaddr_in))
        return -1;
    struct sockaddr_in *sin = (struct sockaddr_in *)name;
    memset(sin, 0, sizeof(*sin));
    sin->sin_family = AF_INET;
    sin->sin_port   = htons(s->remote_port);
    memcpy(&sin->sin_addr.s_addr, s->remote_ip, 4);
    *namelen = sizeof(struct sockaddr_in);
    return 0;
}

int lwip_ioctl(int fd, long cmd, void *argp)
{
    struct bsd_sock *s = get_sock(fd);
    if (!s) return -1;
    if ((unsigned long)cmd == FIONBIO && argp) {
        s->nonblocking = (*(int *)argp != 0);
    }
    return 0;
}

int lwip_fcntl(int fd, int cmd, int val)
{
    struct bsd_sock *s = get_sock(fd);
    if (!s) return -1;
    if (cmd == F_GETFL)
        return s->nonblocking ? O_NONBLOCK : 0;
    if (cmd == F_SETFL)
        s->nonblocking = ((val & O_NONBLOCK) != 0);
    return 0;
}

int lwip_select(int maxfdp1, fd_set *readset, fd_set *writeset,
                fd_set *exceptset, struct timeval *timeout)
{
    uint32_t wait_ms = 0xFFFFFFFFu;
    if (timeout)
        wait_ms = (uint32_t)(timeout->tv_sec * 1000 + timeout->tv_usec / 1000);

    fd_set r_in = readset   ? *readset   : (fd_set){0};
    fd_set w_in = writeset  ? *writeset  : (fd_set){0};
    if (readset)   FD_ZERO(readset);
    if (writeset)  FD_ZERO(writeset);
    if (exceptset) FD_ZERO(exceptset);

    uint32_t start = xTaskGetTickCount();
    for (;;) {
        rtos_yield();
        int ready = 0;
        for (int fd = 1; fd < maxfdp1 && fd <= MAX_BSD_SOCKETS; fd++) {
            struct bsd_sock *s = &socks[fd - 1];
            if (s->kind == BSD_UNUSED)
                continue;
            if (readset && FD_ISSET(fd, &r_in)) {
                int r_ok = 0;
                if (s->kind == BSD_TCP_LISTEN && s->accept_cnt > 0) r_ok = 1;
                else if (s->rx_len > 0 || s->peer_closed) r_ok = 1;
                if (r_ok) { FD_SET(fd, readset); ready++; }
            }
            if (writeset && FD_ISSET(fd, &w_in)) {
                int w_ok = 1;
                if (s->kind == BSD_TCP_CONN && (!s->tcp || tcp_send_space(s->tcp) == 0))
                    w_ok = 0;
                if (w_ok) { FD_SET(fd, writeset); ready++; }
            }
        }
        if (ready > 0)
            return ready;
        if (wait_ms != 0xFFFFFFFFu && (uint32_t)(xTaskGetTickCount() - start) >= wait_ms)
            return 0;
    }
}

/* ----------------------------------------------------- IP / inet helpers */
int inet_aton(const char *cp, struct in_addr *addr)
{
    if (!cp) return 0;
    uint8_t b[4] = {0, 0, 0, 0};
    for (int i = 0; i < 4; i++) {
        if (*cp < '0' || *cp > '9')
            return 0;
        unsigned int v = 0;
        while (*cp >= '0' && *cp <= '9') {
            v = v * 10u + (unsigned int)(*cp - '0');
            if (v > 255u) return 0;
            cp++;
        }
        b[i] = (uint8_t)v;
        if (i < 3) {
            if (*cp != '.') return 0;
            cp++;
        }
    }
    if (*cp != 0 && *cp != ' ' && *cp != ':')
        return 0;
    if (addr)
        memcpy(&addr->s_addr, b, 4);
    return 1;
}

static int resolve_hostname_ipv4(const char *hostname, uint8_t out_ip[4]);

uint32_t inet_addr(const char *cp)
{
    if (!cp || !*cp)
        return INADDR_NONE;
    const char *colon = strchr(cp, ':');
    custom_ntp_port = colon ? (uint16_t)atoi(colon + 1) : 0;
    struct in_addr a;
    if (inet_aton(cp, &a))
        return a.s_addr;
    char host[64];
    size_t hlen = colon ? (size_t)(colon - cp) : strlen(cp);
    if (hlen == 0 || hlen >= sizeof(host))
        return INADDR_NONE;
    memcpy(host, cp, hlen);
    host[hlen] = 0;
    if (resolve_hostname_ipv4(host, (uint8_t *)&a.s_addr) == 0)
        return a.s_addr;
    return INADDR_NONE;
}

char *inet_ntoa_r(struct in_addr addr, char *buf, int buflen)
{
    if (!buf || buflen < 16) return 0;
    const uint8_t *b = (const uint8_t *)&addr.s_addr;
    snprintf(buf, (unsigned)buflen, "%u.%u.%u.%u", b[0], b[1], b[2], b[3]);
    return buf;
}

char *inet_ntoa(struct in_addr addr)
{
    static char buf[16];
    return inet_ntoa_r(addr, buf, sizeof(buf));
}

const char *inet_ntop(int af, const void *src, char *dst, uint32_t size)
{
    if (af != AF_INET || !src || !dst) return 0;
    struct in_addr a;
    memcpy(&a.s_addr, src, 4);
    return inet_ntoa_r(a, dst, (int)size);
}

int inet_pton(int af, const char *src, void *dst)
{
    if (af != AF_INET || !src || !dst) return 0;
    struct in_addr a;
    if (!inet_aton(src, &a)) return 0;
    memcpy(dst, &a.s_addr, 4);
    return 1;
}

int ipaddr_aton(const char *cp, ip_addr_t *addr)
{
    struct in_addr a;
    if (!inet_aton(cp, &a))
        return 0;
    if (addr)
        addr->addr = a.s_addr;
    return 1;
}

uint32_t ipaddr_addr(const char *cp)
{
    return inet_addr(cp);
}

char *ipaddr_ntoa_r(const ip_addr_t *addr, char *buf, int buflen)
{
    struct in_addr a = { addr ? addr->addr : 0 };
    return inet_ntoa_r(a, buf, buflen);
}

char *ipaddr_ntoa(const ip_addr_t *addr)
{
    struct in_addr a = { addr ? addr->addr : 0 };
    return inet_ntoa(a);
}

/* ----------------------------------------------------- DNS resolver */
static uint8_t dns_reply_ip[4];
static int     dns_got_reply = 0;

static void on_dns_udp(const uint8_t src_ip[4], uint16_t src_port, uint16_t dst_port,
                       const uint8_t *data, uint32_t len)
{
    (void)src_ip;
    (void)src_port;
    (void)dst_port;
    if (len < 12) return;
    uint16_t flags = rd16(data + 2);
    uint16_t qdcount = rd16(data + 4);
    uint16_t ancount = rd16(data + 6);
    if (!(flags & 0x8000u) || ancount == 0)
        return;

    uint32_t pos = 12;
    for (uint16_t q = 0; q < qdcount && pos < len; q++) {
        while (pos < len && data[pos] != 0) {
            if ((data[pos] & 0xC0u) == 0xC0u) { pos += 2; goto q_done; }
            pos += 1u + data[pos];
        }
        pos++;
    q_done:
        pos += 4;
    }

    for (uint16_t a = 0; a < ancount && pos + 10 <= len; a++) {
        while (pos < len && data[pos] != 0) {
            if ((data[pos] & 0xC0u) == 0xC0u) { pos += 2; goto name_done; }
            pos += 1u + data[pos];
        }
        pos++;
    name_done:
        if (pos + 10 > len) break;
        uint16_t rtype = rd16(data + pos);
        uint16_t rdlen = rd16(data + pos + 8);
        pos += 10;
        if (pos + rdlen > len) break;
        if (rtype == 1 && rdlen == 4) {
            memcpy(dns_reply_ip, data + pos, 4);
            dns_got_reply = 1;
            return;
        }
        pos += rdlen;
    }
}

static int resolve_hostname_ipv4(const char *hostname, uint8_t out_ip[4])
{
    if (!hostname || !*hostname)
        return -1;
    struct in_addr a;
    if (inet_aton(hostname, &a)) {
        memcpy(out_ip, &a.s_addr, 4);
        return 0;
    }

    uint8_t pkt[128];
    memset(pkt, 0, sizeof(pkt));
    wr16(pkt + 0, 0x4242); /* ID */
    wr16(pkt + 2, 0x0100); /* standard query, recursion desired */
    wr16(pkt + 4, 1);      /* QDCOUNT = 1 */

    uint32_t pos = 12;
    const char *p = hostname;
    while (*p) {
        const char *dot = strchr(p, '.');
        size_t lab_len = dot ? (size_t)(dot - p) : strlen(p);
        if (lab_len == 0 || lab_len > 63 || pos + 1 + lab_len + 5 > sizeof(pkt))
            return -1;
        pkt[pos++] = (uint8_t)lab_len;
        memcpy(pkt + pos, p, lab_len);
        pos += (uint32_t)lab_len;
        if (!dot) break;
        p = dot + 1;
    }
    pkt[pos++] = 0;
    wr16(pkt + pos, 1); pos += 2; /* QTYPE = A */
    wr16(pkt + pos, 1); pos += 2; /* QCLASS = IN */

    dns_got_reply = 0;
    const uint8_t *dns_ip = net_select_dns();
    net_set_active_ip(net_select_src_ip(dns_ip));
    net_udp_listen(53535, on_dns_udp);
    net_udp_send(dns_ip, 53535, 53, pkt, pos);

    uint32_t start = xTaskGetTickCount();
    while (!dns_got_reply && (uint32_t)(xTaskGetTickCount() - start) < 1200)
        rtos_yield();
    net_udp_listen(53535, 0);

    if (dns_got_reply) {
        memcpy(out_ip, dns_reply_ip, 4);
        return 0;
    }
    return -1;
}

struct hostent *lwip_gethostbyname(const char *name)
{
    static struct in_addr resolved_addr;
    static char *addr_list[2];
    static char host_buf[64];
    static struct hostent he;

    uint8_t ip[4];
    if (resolve_hostname_ipv4(name, ip) < 0)
        return 0;

    memcpy(&resolved_addr.s_addr, ip, 4);
    strncpy(host_buf, name ? name : "", sizeof(host_buf) - 1);
    addr_list[0] = (char *)&resolved_addr;
    addr_list[1] = 0;
    he.h_name      = host_buf;
    he.h_aliases   = 0;
    he.h_addrtype  = AF_INET;
    he.h_length    = 4;
    he.h_addr_list = addr_list;
    return &he;
}

err_t dns_gethostbyname_addrtype(const char *hostname, ip_addr_t *addr,
                                 dns_found_callback found, void *callback_arg,
                                 u8_t dns_addrtype)
{
    (void)dns_addrtype;
    uint8_t ip[4];
    if (resolve_hostname_ipv4(hostname, ip) == 0) {
        if (addr)
            memcpy(&addr->addr, ip, 4);
        if (found)
            found(hostname, addr, callback_arg);
        return ERR_OK;
    }
    return ERR_VAL;
}

err_t dns_gethostbyname(const char *hostname, ip_addr_t *addr,
                        dns_found_callback found, void *callback_arg)
{
    return dns_gethostbyname_addrtype(hostname, addr, found, callback_arg, 0);
}

int lwip_close_force(int fd)
{
    return lwip_close(fd);
}

int lwip_getaddrinfo(const char *nodename, const char *servname,
                     const struct addrinfo *hints, struct addrinfo **res)
{
    if (!res)
        return -1;
    *res = 0;

    uint8_t ip[4];
    if (nodename && *nodename) {
        if (resolve_hostname_ipv4(nodename, ip) < 0)
            return -1;
    } else {
        memset(ip, 0, 4);
    }

    uint16_t port = servname ? (uint16_t)atoi(servname) : 0;

    struct {
        struct addrinfo    ai;
        struct sockaddr_in sa;
    } *blk = malloc(sizeof(*blk));
    if (!blk)
        return -1;

    memset(blk, 0, sizeof(*blk));
    blk->sa.sin_len    = sizeof(struct sockaddr_in);
    blk->sa.sin_family = AF_INET;
    blk->sa.sin_port   = htons(port);
    memcpy(&blk->sa.sin_addr.s_addr, ip, 4);

    blk->ai.ai_family   = AF_INET;
    blk->ai.ai_socktype = hints ? hints->ai_socktype : SOCK_STREAM;
    blk->ai.ai_protocol = hints ? hints->ai_protocol : IPPROTO_TCP;
    blk->ai.ai_addrlen  = sizeof(struct sockaddr_in);
    blk->ai.ai_addr     = (struct sockaddr *)&blk->sa;
    blk->ai.ai_next     = 0;

    *res = &blk->ai;
    return 0;
}

void lwip_freeaddrinfo(struct addrinfo *ai)
{
    while (ai) {
        struct addrinfo *next = ai->ai_next;
        free(ai);
        ai = next;
    }
}

__attribute__((weak)) void usleep(int r)
{
    delay_us((uint32_t)(r > 0 ? r : 1));
}
