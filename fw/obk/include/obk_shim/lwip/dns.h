#ifndef _SHIM_LWIP_DNS_H
#define _SHIM_LWIP_DNS_H

#include "err.h"
#include "ip_addr.h"

#define LWIP_DNS_ADDRTYPE_IPV4 0

typedef void (*dns_found_callback)(const char *name, ip_addr_t *ipaddr, void *callback_arg);

err_t dns_gethostbyname(const char *hostname, ip_addr_t *addr,
                        dns_found_callback found, void *callback_arg);
err_t dns_gethostbyname_addrtype(const char *hostname, ip_addr_t *addr,
                                 dns_found_callback found, void *callback_arg,
                                 u8_t dns_addrtype);

#endif
