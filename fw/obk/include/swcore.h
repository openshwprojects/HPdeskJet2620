#ifndef SWCORE_COMPAT_H
#define SWCORE_COMPAT_H

#include <stdint.h>

#define NUM_PHY_PORTS 5
#define ALL_PORT_MASK 0x1Fu

static inline void eth_init(const uint8_t mac[6]) { (void)mac; }
static inline int  eth_link_up(int port) { (void)port; return 0; }
static inline int  eth_send(const void *frame, uint32_t len, uint8_t port_mask)
{
    (void)frame; (void)len; (void)port_mask;
    return -1;
}
static inline int  eth_recv(uint8_t **frame_out, uint32_t *len_out, int *port_out)
{
    (void)frame_out; (void)len_out; (void)port_out;
    return 0;
}
static inline void eth_recv_done(void) {}
static inline void eth_tx_reclaim(void) {}

#endif
