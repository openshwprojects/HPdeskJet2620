#ifndef _SHIM_LWIP_INET_H
#define _SHIM_LWIP_INET_H

#include "ip_addr.h"

typedef uint32_t in_addr_t;
typedef uint16_t in_port_t;

struct in_addr {
    in_addr_t s_addr;
};

#define INADDR_ANY       0x00000000UL
#define INADDR_LOOPBACK  0x7F000001UL
#define INADDR_BROADCAST 0xFFFFFFFFUL
#define INADDR_NONE      0xFFFFFFFFUL

/* Big-endian MIPS: network byte order == host byte order */
#ifndef htons
#define htons(x) ((uint16_t)(x))
#define ntohs(x) ((uint16_t)(x))
#define htonl(x) ((uint32_t)(x))
#define ntohl(x) ((uint32_t)(x))
#endif

#define lwip_htons(x) htons(x)
#define lwip_ntohs(x) ntohs(x)
#define lwip_htonl(x) htonl(x)
#define lwip_ntohl(x) ntohl(x)

uint32_t inet_addr(const char *cp);
int      inet_aton(const char *cp, struct in_addr *addr);
char    *inet_ntoa(struct in_addr addr);
char    *inet_ntoa_r(struct in_addr addr, char *buf, int buflen);
const char *inet_ntop(int af, const void *src, char *dst, uint32_t size);
int      inet_pton(int af, const char *src, void *dst);

#endif
