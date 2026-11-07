/*
 * dhcp.h - tiny non-blocking DHCPv4 client on top of net.c (UDP 68 -> 67)
 */
#ifndef DHCP_H
#define DHCP_H

#include <stdint.h>

enum {
    DHCP_OFF = 0,
    DHCP_DISCOVERING,
    DHCP_REQUESTING,
    DHCP_BOUND,
};

struct dhcp_lease {
    uint8_t  state;
    uint8_t  ip[4];
    uint8_t  mask[4];
    uint8_t  gw[4];
    uint8_t  dns[4];
    uint8_t  server[4];
    uint32_t lease_seconds;
    uint32_t xid;
};

void dhcp_start(const char *hostname);
void dhcp_poll(void);
int  dhcp_bound(void);
const struct dhcp_lease *dhcp_info(void);

/* Secondary DHCP client on NET_PORT_WIFI for Wi-Fi station mode */
void dhcp_wifi_start(const char *hostname);
void dhcp_wifi_stop(void);
void dhcp_wifi_poll(void);
int  dhcp_wifi_bound(void);
const struct dhcp_lease *dhcp_wifi_info(void);

#endif
