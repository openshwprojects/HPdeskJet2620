#ifndef _SHIM_LWIP_NETIF_H
#define _SHIM_LWIP_NETIF_H

struct netif {
    int dummy;
};

void *net_get_sta_handle(void);

#endif
