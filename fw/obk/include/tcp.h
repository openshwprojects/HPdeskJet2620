/*
 * tcp.h - socket-style TCP server core for a LAN
 *
 * Several listening ports, several simultaneous connections, a per
 * connection send buffer with peer-window flow control and go-back-N
 * retransmission, in-order receive delivered straight to a callback.
 * No TIME_WAIT, no congestion control, no options except MSS: enough for
 * HTTP, a console and the like on a local network.
 */
#ifndef TCP_H
#define TCP_H

#include <stdint.h>

#define TCP_MAX_LISTEN  8
#define TCP_MAX_CONN    16
#define TCP_SNDBUF      32768
#define TCP_MSS         1400
#define TCP_RTX_MS      300
#define TCP_MAX_RETRIES 8
#define TCP_IDLE_S      60

typedef struct tcp_conn tcp_conn_t;

enum {
    TCP_EV_ACCEPT,      /* handshake done, connection usable */
    TCP_EV_CONNECTED = TCP_EV_ACCEPT,
    TCP_EV_PEER_FIN,    /* peer sent FIN: you may still send, then tcp_close() */
    TCP_EV_CLOSED,      /* connection is gone (after this call the handle is invalid) */
};

typedef void (*tcp_data_cb_t)(tcp_conn_t *c, const uint8_t *data, uint32_t len);
typedef void (*tcp_event_cb_t)(tcp_conn_t *c, int event);

int         tcp_listen(uint16_t port, tcp_data_cb_t on_data, tcp_event_cb_t on_event);   /* 0 / -1 */
tcp_conn_t *tcp_connect(const uint8_t dst_ip[4], uint16_t dst_port,
                        tcp_data_cb_t on_data, tcp_event_cb_t on_event);                /* 0 if full */
int         tcp_is_established(tcp_conn_t *c);
int         tcp_send(tcp_conn_t *c, const void *data, uint32_t len);   /* bytes queued, -1 if closed */
uint32_t    tcp_send_space(tcp_conn_t *c);
void        tcp_close(tcp_conn_t *c);                 /* FIN after the queued data */
void        tcp_abort(tcp_conn_t *c);                 /* RST, immediate */
void        tcp_set_rcv_wnd(tcp_conn_t *c, uint16_t wnd);

void     tcp_set_user(tcp_conn_t *c, void *user);
void    *tcp_user(tcp_conn_t *c);
uint16_t tcp_local_port(tcp_conn_t *c);
uint16_t tcp_remote_port(tcp_conn_t *c);
const uint8_t *tcp_remote_ip(tcp_conn_t *c);
int      tcp_index(tcp_conn_t *c);                 /* 0..TCP_MAX_CONN-1, handy for per-connection state */
tcp_conn_t *tcp_conn_by_index(int i);              /* 0 if that slot is free */

struct tcp_stats {
    uint32_t syn, established, accepted, closed, retransmits, resets, timeouts, dropped, rx_bytes, tx_bytes;
};
extern struct tcp_stats tcp_stats;

/* called by net.c */
void tcp_input(const uint8_t src_ip[4], const uint8_t *seg, uint32_t len);
void tcp_poll(void);

#endif
