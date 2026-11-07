#ifndef _SHIM_LWIP_MDNS_H
#define _SHIM_LWIP_MDNS_H

#include "../err.h"
#include "../netif.h"

#define DNSSD_PROTO_UDP 0
#define DNSSD_PROTO_TCP 1

struct mdns_service;
typedef void (* service_get_txt_fn_t)(struct mdns_service *service, void *txt_userdata);

void mdns_resp_init(void);
err_t mdns_resp_add_netif(struct netif *netif, const char *hostname, u32_t dns_ttl);
err_t mdns_resp_remove_netif(struct netif *netif);
s8_t mdns_resp_add_service(struct netif *netif, const char *name, const char *service,
                           u16_t proto, u16_t port, u32_t dns_ttl,
                           service_get_txt_fn_t txt_fn, void *txt_userdata);
void mdns_resp_netif_settings_changed(struct netif *netif);
void mdns_resp_restart(struct netif *netif);
void mdns_resp_announce(struct netif *netif);

#endif
