/*
 * dnsd.h - captive DNS responder on net.c (UDP 53): answers every A query
 * with one address.  Phones joining an open network probe a well-known host
 * (captive.apple.com, connectivitycheck.gstatic.com, ...); pointing that at
 * our web server makes them open our page.
 */
#ifndef DNSD_H
#define DNSD_H

#include <stdint.h>

struct dnsd_stats { uint32_t queries, answered; char last_name[64]; };
extern struct dnsd_stats dnsd_stats;

int  dnsd_start(const uint8_t answer_ip[4]);
void dnsd_stop(void);

#endif
