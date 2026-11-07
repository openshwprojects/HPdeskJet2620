/*
 * dhcpd.h - tiny DHCPv4 server on net.c (UDP 67): hands out addresses from a
 * small pool, answers DISCOVER with OFFER and REQUEST with ACK (or NAK when
 * the client asks for an address that is not its lease).  Router and DNS are
 * the server itself.  Replies are broadcast, so they work before the client
 * has an address.
 */
#ifndef DHCPD_H
#define DHCPD_H

#include <stdint.h>

#define DHCPD_MAX_LEASES 8

struct dhcpd_lease { uint8_t mac[6]; uint8_t ip[4]; uint8_t valid; char hostname[32]; };
struct dhcpd_stats { uint32_t discover, request, offer, ack, nak, other; };
extern struct dhcpd_stats dhcpd_stats;

/* server_ip is also router and DNS; pool is server_ip[3] replaced by first_host .. first_host+count-1 */
int  dhcpd_start(const uint8_t server_ip[4], const uint8_t mask[4], uint8_t first_host, uint8_t count);
void dhcpd_stop(void);
int  dhcpd_leases(const struct dhcpd_lease **out);   /* number of entries */

#endif
