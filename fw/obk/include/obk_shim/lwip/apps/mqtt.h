#ifndef _SHIM_LWIP_APPS_MQTT_H
#define _SHIM_LWIP_APPS_MQTT_H

#include "../err.h"
#include "../ip_addr.h"
#include "../mqtt_opts.h"

typedef struct mqtt_client_s mqtt_client_t;

typedef enum {
    MQTT_CONNECT_ACCEPTED                 = 0,
    MQTT_CONNECT_REFUSED_PROTOCOL_VERSION = 1,
    MQTT_CONNECT_REFUSED_IDENTIFIER       = 2,
    MQTT_CONNECT_REFUSED_SERVER           = 3,
    MQTT_CONNECT_REFUSED_USERNAME_PASS    = 4,
    MQTT_CONNECT_REFUSED_NOT_AUTHORIZED_  = 5,
    MQTT_CONNECT_DISCONNECTED             = 256,
    MQTT_CONNECT_TIMEOUT                  = 257
} mqtt_connection_status_t;

enum {
    MQTT_DATA_FLAG_LAST = 1
};

struct mqtt_connect_client_info_t {
    const char *client_id;
    const char *client_user;
    const char *client_pass;
    u16_t       keep_alive;
    const char *will_topic;
    const char *will_msg;
    u8_t        will_qos;
    u8_t        will_retain;
};

typedef void (*mqtt_connection_cb_t)(mqtt_client_t *client, void *arg, mqtt_connection_status_t status);
typedef void (*mqtt_request_cb_t)(void *arg, err_t err);
typedef void (*mqtt_incoming_publish_cb_t)(void *arg, const char *topic, u32_t tot_len);
typedef void (*mqtt_incoming_data_cb_t)(void *arg, const u8_t *data, u16_t len, u8_t flags);

mqtt_client_t *mqtt_client_new(void);
void           mqtt_client_free(mqtt_client_t *client);
u8_t           mqtt_client_is_connected(mqtt_client_t *client);
void           mqtt_disconnect(mqtt_client_t *client);
err_t          mqtt_client_connect(mqtt_client_t *client, const ip_addr_t *ipaddr, u16_t port,
                                   mqtt_connection_cb_t cb, void *arg,
                                   const struct mqtt_connect_client_info_t *client_info);
void           mqtt_set_inpub_callback(mqtt_client_t *client,
                                       mqtt_incoming_publish_cb_t pub_cb,
                                       mqtt_incoming_data_cb_t data_cb,
                                       void *arg);
err_t          mqtt_sub_unsub(mqtt_client_t *client, const char *topic, u8_t qos,
                              mqtt_request_cb_t cb, void *arg, u8_t sub);
err_t          mqtt_publish(mqtt_client_t *client, const char *topic,
                            const void *payload, u16_t payload_length,
                            u8_t qos, u8_t retain,
                            mqtt_request_cb_t cb, void *arg);

void           mqtt_poll(void);

#define mqtt_subscribe(client, topic, qos, cb, arg) \
    mqtt_sub_unsub(client, topic, qos, cb, arg, 1)
#define mqtt_unsubscribe(client, topic, cb, arg) \
    mqtt_sub_unsub(client, topic, 0, cb, arg, 0)

#endif
