#ifndef _SHIM_LWIP_IP_ADDR_H
#define _SHIM_LWIP_IP_ADDR_H

#include "err.h"

typedef struct ip4_addr {
    uint32_t addr;
} ip4_addr_t;

typedef ip4_addr_t ip_addr_t;

#define IPADDR_ANY       0x00000000UL
#define IPADDR_LOOPBACK  0x7F000001UL
#define IPADDR_NONE      0xFFFFFFFFUL
#define IPADDR_BROADCAST 0xFFFFFFFFUL

#define ip4_addr1(ipaddr) (((const uint8_t *)(&(ipaddr)->addr))[0])
#define ip4_addr2(ipaddr) (((const uint8_t *)(&(ipaddr)->addr))[1])
#define ip4_addr3(ipaddr) (((const uint8_t *)(&(ipaddr)->addr))[2])
#define ip4_addr4(ipaddr) (((const uint8_t *)(&(ipaddr)->addr))[3])

#define IP4_ADDR(ipaddr, a,b,c,d) \
    (ipaddr)->addr = (((uint32_t)((a) & 0xff) << 24) | \
                      ((uint32_t)((b) & 0xff) << 16) | \
                      ((uint32_t)((c) & 0xff) << 8)  | \
                       (uint32_t)((d) & 0xff))

int       ipaddr_aton(const char *cp, ip_addr_t *addr);
uint32_t  ipaddr_addr(const char *cp);
char     *ipaddr_ntoa(const ip_addr_t *addr);
char     *ipaddr_ntoa_r(const ip_addr_t *addr, char *buf, int buflen);

#define ip4addr_aton   ipaddr_aton
#define ip4addr_ntoa   ipaddr_ntoa
#define ip4addr_ntoa_r ipaddr_ntoa_r

#endif
