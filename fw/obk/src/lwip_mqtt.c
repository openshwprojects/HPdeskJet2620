/*
 * lwip_mqtt.c - MQTT 3.1.1 client implementation on top of OpenRTL8196C tcp.c.
 */
#include "tcp.h"
#include "rtos.h"
#include "heap.h"
#include "libc.h"
#include "obk_shim/lwip/apps/mqtt.h"

struct mqtt_client_s {
    tcp_conn_t                 *tcp;
    u8_t                        connected;
    u8_t                        connecting;
    u16_t                       next_pkt_id;
    u16_t                       keep_alive_s;
    uint32_t                    last_tx_ms;
    mqtt_connection_cb_t        conn_cb;
    void                       *conn_arg;
    mqtt_incoming_publish_cb_t  pub_cb;
    mqtt_incoming_data_cb_t     data_cb;
    void                       *inpub_arg;
    struct mqtt_connect_client_info_t info;
    char                        client_id[64];
    char                        client_user[64];
    char                        client_pass[128];
    char                        will_topic[96];
    char                        will_msg[64];
    uint8_t                     rx_buf[2048];
    uint32_t                    rx_len;
};

static mqtt_client_t *active_mqtt = 0;

static uint32_t encode_rem_len(uint8_t *out, uint32_t len)
{
    uint32_t n = 0;
    do {
        uint8_t byte = (uint8_t)(len & 0x7Fu);
        len >>= 7;
        if (len > 0)
            byte |= 0x80u;
        out[n++] = byte;
    } while (len > 0);
    return n;
}

static uint8_t *put_mqtt_str(uint8_t *p, const char *s)
{
    uint16_t len = s ? (uint16_t)strlen(s) : 0;
    *p++ = (uint8_t)(len >> 8);
    *p++ = (uint8_t)(len & 0xFFu);
    if (len) {
        memcpy(p, s, len);
        p += len;
    }
    return p;
}

static void send_connect_packet(mqtt_client_t *c)
{
    uint8_t pkt[512];
    uint8_t *body = pkt + 5;
    uint8_t *p = body;

    /* Protocol Name "MQTT", Level 4 (3.1.1) */
    p = put_mqtt_str(p, "MQTT");
    *p++ = 0x04;

    uint8_t flags = 0x02; /* Clean Session */
    if (c->will_topic[0]) {
        flags |= 0x04 | (uint8_t)((c->info.will_qos & 0x03u) << 3);
        if (c->info.will_retain)
            flags |= 0x20;
    }
    if (c->client_user[0]) flags |= 0x80;
    if (c->client_pass[0]) flags |= 0x40;
    *p++ = flags;

    uint16_t ka = c->keep_alive_s ? c->keep_alive_s : 60;
    *p++ = (uint8_t)(ka >> 8);
    *p++ = (uint8_t)(ka & 0xFFu);

    p = put_mqtt_str(p, c->client_id);
    if (c->will_topic[0]) {
        p = put_mqtt_str(p, c->will_topic);
        p = put_mqtt_str(p, c->will_msg);
    }
    if (c->client_user[0])
        p = put_mqtt_str(p, c->client_user);
    if (c->client_pass[0])
        p = put_mqtt_str(p, c->client_pass);

    uint32_t rem_len = (uint32_t)(p - body);
    uint8_t hdr[5];
    hdr[0] = 0x10; /* CONNECT */
    uint32_t hlen = 1 + encode_rem_len(hdr + 1, rem_len);
    uint8_t *start = body - hlen;
    memcpy(start, hdr, hlen);
    tcp_send(c->tcp, start, hlen + rem_len);
    c->last_tx_ms = xTaskGetTickCount();
}

static void on_mqtt_tcp_data(tcp_conn_t *tc, const uint8_t *data, uint32_t len)
{
    mqtt_client_t *c = (mqtt_client_t *)tcp_user(tc);
    if (!c) return;

    uint32_t space = (uint32_t)sizeof(c->rx_buf) - c->rx_len;
    uint32_t copy = (len > space) ? space : len;
    if (copy > 0) {
        memcpy(c->rx_buf + c->rx_len, data, copy);
        c->rx_len += copy;
    }

    while (c->rx_len >= 2) {
        uint8_t type_flags = c->rx_buf[0];
        uint32_t mult = 1, rem_len = 0, pos = 1;
        while (pos < c->rx_len) {
            uint8_t b = c->rx_buf[pos++];
            rem_len += (uint32_t)(b & 0x7Fu) * mult;
            if (!(b & 0x80u))
                break;
            mult <<= 7;
            if (pos >= 5)
                break;
        }
        if (pos < c->rx_len && (c->rx_buf[pos - 1] & 0x80u))
            return; /* need more header bytes */
        if (c->rx_len < pos + rem_len)
            return; /* need more payload bytes */

        uint8_t *payload = c->rx_buf + pos;
        uint8_t pkt_type = (uint8_t)(type_flags >> 4);

        if (pkt_type == 2 && rem_len >= 2) { /* CONNACK */
            uint8_t rc = payload[1];
            c->connecting = 0;
            if (rc == 0) {
                c->connected = 1;
                if (c->conn_cb)
                    c->conn_cb(c, c->conn_arg, MQTT_CONNECT_ACCEPTED);
            } else {
                c->connected = 0;
                if (c->conn_cb)
                    c->conn_cb(c, c->conn_arg, (mqtt_connection_status_t)rc);
            }
        } else if (pkt_type == 3 && rem_len >= 2) { /* PUBLISH */
            uint16_t tlen = (uint16_t)((payload[0] << 8) | payload[1]);
            uint8_t qos = (uint8_t)((type_flags >> 1) & 0x03u);
            uint32_t hdr_off = 2u + tlen + (qos ? 2u : 0u);
            if (hdr_off <= rem_len) {
                char topic[128];
                uint16_t clen = (tlen < sizeof(topic) - 1) ? tlen : (sizeof(topic) - 1);
                memcpy(topic, payload + 2, clen);
                topic[clen] = 0;
                if (qos == 1 && c->tcp) {
                    uint8_t puback[4] = { 0x40, 0x02, payload[2 + tlen], payload[3 + tlen] };
                    tcp_send(c->tcp, puback, 4);
                }
                uint32_t dlen = rem_len - hdr_off;
                if (c->pub_cb)
                    c->pub_cb(c->inpub_arg, topic, dlen);
                if (c->data_cb)
                    c->data_cb(c->inpub_arg, payload + hdr_off, (u16_t)dlen, MQTT_DATA_FLAG_LAST);
            }
        }

        uint32_t consumed = pos + rem_len;
        uint32_t left = c->rx_len - consumed;
        if (left > 0)
            memmove(c->rx_buf, c->rx_buf + consumed, left);
        c->rx_len = left;
    }
}

static void on_mqtt_tcp_event(tcp_conn_t *tc, int event)
{
    mqtt_client_t *c = (mqtt_client_t *)tcp_user(tc);
    if (!c) return;

    if (event == TCP_EV_CONNECTED) {
        send_connect_packet(c);
    } else if (event == TCP_EV_PEER_FIN || event == TCP_EV_CLOSED) {
        if (event == TCP_EV_PEER_FIN && c->tcp)
            tcp_close(c->tcp);
        tcp_set_user(tc, 0);
        c->tcp = 0;
        int was_active = c->connected || c->connecting;
        c->connected = 0;
        c->connecting = 0;
        if (was_active && c->conn_cb)
            c->conn_cb(c, c->conn_arg, MQTT_CONNECT_DISCONNECTED);
    }
}

mqtt_client_t *mqtt_client_new(void)
{
    mqtt_client_t *c = (mqtt_client_t *)calloc(1, sizeof(mqtt_client_t));
    if (c) {
        c->next_pkt_id = 1;
        active_mqtt = c;
    }
    return c;
}

void mqtt_client_free(mqtt_client_t *client)
{
    if (!client) return;
    mqtt_disconnect(client);
    if (active_mqtt == client)
        active_mqtt = 0;
    free(client);
}

u8_t mqtt_client_is_connected(mqtt_client_t *client)
{
    return (client && client->connected && client->tcp) ? 1 : 0;
}

void mqtt_disconnect(mqtt_client_t *client)
{
    if (!client) return;
    if (client->tcp) {
        tcp_conn_t *tc = client->tcp;
        client->tcp = 0;
        if (tcp_user(tc) == client) {
            if (client->connected) {
                const uint8_t disc[2] = { 0xE0, 0x00 };
                tcp_send(tc, disc, 2);
            }
            tcp_set_user(tc, 0);
            tcp_close(tc);
        }
    }
    client->connected = 0;
    client->connecting = 0;
}

err_t mqtt_client_connect(mqtt_client_t *client, const ip_addr_t *ipaddr, u16_t port,
                          mqtt_connection_cb_t cb, void *arg,
                          const struct mqtt_connect_client_info_t *client_info)
{
    if (!client || !ipaddr || !client_info)
        return ERR_ARG;

    mqtt_disconnect(client);

    client->conn_cb      = cb;
    client->conn_arg     = arg;
    client->info         = *client_info;
    client->keep_alive_s = client_info->keep_alive ? client_info->keep_alive : 60;
    client->rx_len       = 0;

    strncpy(client->client_id, client_info->client_id ? client_info->client_id : "obk_rtl8196c",
            sizeof(client->client_id) - 1);
    strncpy(client->client_user, client_info->client_user ? client_info->client_user : "",
            sizeof(client->client_user) - 1);
    strncpy(client->client_pass, client_info->client_pass ? client_info->client_pass : "",
            sizeof(client->client_pass) - 1);
    strncpy(client->will_topic, client_info->will_topic ? client_info->will_topic : "",
            sizeof(client->will_topic) - 1);
    strncpy(client->will_msg, client_info->will_msg ? client_info->will_msg : "",
            sizeof(client->will_msg) - 1);

    uint8_t dst_ip[4];
    memcpy(dst_ip, &ipaddr->addr, 4);
    tcp_conn_t *tc = tcp_connect(dst_ip, port ? port : 1883, on_mqtt_tcp_data, on_mqtt_tcp_event);
    if (!tc)
        return ERR_MEM;

    client->tcp = tc;
    client->connecting = 1;
    tcp_set_user(tc, client);
    return ERR_OK;
}

void mqtt_set_inpub_callback(mqtt_client_t *client,
                             mqtt_incoming_publish_cb_t pub_cb,
                             mqtt_incoming_data_cb_t data_cb,
                             void *arg)
{
    if (!client) return;
    client->pub_cb    = pub_cb;
    client->data_cb   = data_cb;
    client->inpub_arg = arg;
}

err_t mqtt_sub_unsub(mqtt_client_t *client, const char *topic, u8_t qos,
                     mqtt_request_cb_t cb, void *arg, u8_t sub)
{
    if (!client || !client->connected || !client->tcp || !topic)
        return ERR_CONN;

    uint8_t pkt[256];
    uint8_t *body = pkt + 5;
    uint8_t *p = body;
    uint16_t pid = client->next_pkt_id++;
    if (client->next_pkt_id == 0) client->next_pkt_id = 1;

    *p++ = (uint8_t)(pid >> 8);
    *p++ = (uint8_t)(pid & 0xFFu);
    p = put_mqtt_str(p, topic);
    if (sub)
        *p++ = (uint8_t)(qos & 0x03u);

    uint32_t rem_len = (uint32_t)(p - body);
    uint8_t hdr[5];
    hdr[0] = sub ? 0x82 : 0xA2;
    uint32_t hlen = 1 + encode_rem_len(hdr + 1, rem_len);
    uint8_t *start = body - hlen;
    memcpy(start, hdr, hlen);

    if (tcp_send(client->tcp, start, hlen + rem_len) < 0)
        return ERR_MEM;
    client->last_tx_ms = xTaskGetTickCount();
    if (cb)
        cb(arg, ERR_OK);
    return ERR_OK;
}

err_t mqtt_publish(mqtt_client_t *client, const char *topic,
                   const void *payload, u16_t payload_length,
                   u8_t qos, u8_t retain,
                   mqtt_request_cb_t cb, void *arg)
{
    (void)qos; /* Send as QoS 0 for low-overhead LAN operation */
    if (!client || !client->connected || !client->tcp || !topic)
        return ERR_CONN;

    uint16_t tlen = (uint16_t)strlen(topic);
    uint32_t rem_len = 2u + tlen + payload_length;
    if (tcp_send_space(client->tcp) < rem_len + 5u)
        return ERR_MEM;

    uint8_t hdr[5];
    hdr[0] = (uint8_t)(0x30u | (retain ? 0x01u : 0x00u));
    uint32_t hlen = 1 + encode_rem_len(hdr + 1, rem_len);
    uint8_t thdr[2] = { (uint8_t)(tlen >> 8), (uint8_t)(tlen & 0xFFu) };

    tcp_send(client->tcp, hdr, hlen);
    tcp_send(client->tcp, thdr, 2);
    tcp_send(client->tcp, topic, tlen);
    if (payload && payload_length > 0)
        tcp_send(client->tcp, payload, payload_length);

    client->last_tx_ms = xTaskGetTickCount();
    if (cb)
        cb(arg, ERR_OK);
    return ERR_OK;
}

void mqtt_poll(void)
{
    mqtt_client_t *c = active_mqtt;
    if (!c || !c->connected || !c->tcp)
        return;
    uint32_t now = xTaskGetTickCount();
    uint32_t ka_ms = (uint32_t)c->keep_alive_s * 500u; /* ping at half keep-alive */
    if (ka_ms < 15000u) ka_ms = 15000u;
    if ((uint32_t)(now - c->last_tx_ms) >= ka_ms) {
        const uint8_t pingreq[2] = { 0xC0, 0x00 };
        tcp_send(c->tcp, pingreq, 2);
        c->last_tx_ms = now;
    }
}
