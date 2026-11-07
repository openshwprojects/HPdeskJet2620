#ifndef FW_NET_H
#define FW_NET_H
#include <stdint.h>

/*
 * Minimal IPv4 stack on top of the Wi-Fi data path (see net.c): ARP, IPv4,
 * ICMP echo (ping both ways), UDP, a DHCP client in station mode and a DHCP
 * server in access point mode. Everything is polled.
 */

/* Wi-Fi driver -> stack */
void net_input(const uint8_t *eth, uint32_t len);   /* one received Ethernet II frame */
void net_link_change(int up, int ap_mode);          /* link up (DHCP / AP address) or down */
void net_poll(void);                                 /* timers: DHCP, ARP */

/* Address state (host order: 192.168.1.2 = 0xC0A80102) */
uint32_t net_ip(void);
int      net_has_address(void);
void     net_format_ip(uint32_t ip, char *buf);      /* buf >= 16 bytes */
int      net_parse_ip(const char *s, uint32_t *ip);

/* Web log: copy everything the UART prints into the buffer behind /api/log. */
void net_log_init(void);
/* Provided by main.c: run one shell line (the web shell's /api/cmd). */
int  app_shell_execute(char *line);

/* Ping: send `count` echo requests (1 s apart) and print the answers. */
int net_ping(uint32_t ip, uint32_t count);

/* Shell commands */
int cmd_net(int argc, char **argv);
int cmd_ping(int argc, char **argv);
int cmd_server(int argc, char **argv);

#define NET_CMD_ENTRIES \
    { "net",    "net [status] | ip <addr> <mask> [gw] | dhcp | name <host>: Wi-Fi network settings, mDNS name", cmd_net }, \
    { "ping",   "ping <ip> [count]: ICMP echo over Wi-Fi", cmd_ping }, \
    { "server", "server [status] | on | off | port <p> | init | stop | token <t>: HTTP control server", cmd_server }

#endif
