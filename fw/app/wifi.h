#ifndef FW_WIFI_H
#define FW_WIFI_H
#include <stdint.h>

/* "wifi [status] | probe | fw | scan | join | ap | leave | rssi | off": power
   the BCM43143 module, enumerate it over SDIO, boot its firmware, scan, join
   a network or run an access point (see wifi.c). */
int cmd_wifi(int argc, char **argv);

#define WIFI_CMD_ENTRY \
    { "wifi", "wifi [status] | probe | fw | scan | join [ssid] [pass] | ap [ssid] [pass] | leave | auto [on|off] | rssi | off", cmd_wifi }

enum { WIFI_MODE_OFF, WIFI_MODE_IDLE, WIFI_MODE_STA, WIFI_MODE_AP };

/* For the network stack and the front-panel menu */
int            wifi_mode(void);           /* WIFI_MODE_* (IDLE: chip firmware running, no link) */
int            wifi_link_up(void);        /* station associated (and keyed) or access point started */
const uint8_t *wifi_mac(void);
const char    *wifi_ssid(void);
int            wifi_last_rssi(void);      /* dBm of the last station RSSI reading, 0 = unknown */
uint32_t       wifi_ap_clients(void);
int            wifi_ensure_running(void); /* load and boot the chip firmware unless it runs */
int            wifi_send(const uint8_t *eth, uint32_t len);    /* one Ethernet II frame, 0 = sent */
void           wifi_poll(void);           /* idle loop: receive frames, link upkeep */
int            wifi_join(const char *ssid, const char *pass);  /* pass NULL or "" = open network */
int            wifi_start_ap(const char *ssid, const char *pass, uint32_t channel);
void           wifi_leave(void);
void           wifi_shutdown(void);       /* module power off */
/* Scan quietly for the panel menu: number of networks and the strongest signal. */
int            wifi_scan_summary(uint32_t *count, int *best_rssi);
/* Background rejoin: switched on, links lost, rejoins that worked. */
void           wifi_auto_info(int *on, uint32_t *lost, uint32_t *rejoined);

#endif
