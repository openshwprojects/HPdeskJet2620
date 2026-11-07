"""Virtual Wi-Fi network for the BCM43143 model (sdio.Bcm43143SdioCard), opt-in.

``VirtualWifiNetwork(card)`` attaches to the card and plays the far side of
the custom firmware's Wi-Fi link (fw/app/wifi.c, fw/app/net.c):

* Station mode: ``add_ap()`` describes access points. WLC_SET_SSID is answered
  the way the chip does it, with brcmfmac-numbered events: LINK up, PSK_SUP
  "keyed" when the WPA2 passphrase matches (PSK_SUP with a handshake timeout
  and LINK down when it does not), SET_SSID success, or SET_SSID "no
  networks" for an unknown name. Events the firmware did not enable in
  "event_msgs" are not delivered, like on the chip. Behind every access point
  is a router, 192.168.50.1/24, with a DHCP server; it answers ARP and ping
  and can ping the printer.
* Access point mode: once the firmware has started its access point,
  ``associate_client()`` makes a station join (ASSOC_IND), ask for an address
  over DHCP and answer ARP and ping; ``ping_printer()`` sends echo requests.

Frames from the firmware arrive in ``on_frame``; answers are queued on the
card at once and reach the firmware at its next poll. ``log`` keeps a short
description of what happened, for test assertions.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from sdio import ip_checksum

# Broadcom event codes (brcmfmac fweh.h) and the ioctls the firmware uses.
E_SET_SSID, E_AUTH, E_DEAUTH_IND, E_ASSOC, E_ASSOC_IND = 0, 3, 6, 7, 8
E_DISASSOC, E_DISASSOC_IND, E_LINK, E_PSK_SUP = 11, 12, 16, 46
WLC_UP, WLC_DOWN, WLC_SET_INFRA, WLC_SET_AUTH, WLC_SET_SSID = 2, 3, 20, 22, 26
WLC_SET_CHANNEL, WLC_DISASSOC, WLC_SET_PM, WLC_SET_AP, WLC_GET_RSSI = 30, 52, 86, 118, 127
WLC_SET_WSEC, WLC_SET_WPA_AUTH, WLC_SET_VAR, WLC_SET_WSEC_PMK = 134, 165, 263, 268
SUP_KEYED, SUP_WAIT_M3, SUP_REASON_PSK_TIMEOUT = 6, 8, 12
STATUS_SUCCESS, STATUS_FAIL, STATUS_NO_NETWORKS = 0, 1, 3

ETH_IP, ETH_ARP = 0x0800, 0x0806
BCAST_MAC = b"\xff" * 6
ZERO_IP = b"\x00" * 4
BCAST_IP = b"\xff" * 4
PING_IDENT = 0x7777


def ip4(text: str) -> bytes:
    return bytes(int(part) for part in text.split("."))


def ip_str(addr: bytes) -> str:
    return ".".join(str(b) for b in addr)


def eth_header(dst: bytes, src: bytes, ethertype: int) -> bytes:
    return dst + src + struct.pack("!H", ethertype)


def ipv4_packet(src: bytes, dst: bytes, proto: int, payload: bytes, ident: int = 0x4242) -> bytes:
    hdr = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(payload), ident, 0, 64, proto, 0, src, dst)
    return hdr[:10] + struct.pack("!H", ip_checksum(hdr)) + hdr[12:] + payload


def udp_frame(src_mac: bytes, dst_mac: bytes, src_ip: bytes, dst_ip: bytes, sport: int, dport: int,
              payload: bytes) -> bytes:
    length = 8 + len(payload)
    pseudo = src_ip + dst_ip + struct.pack("!BBH", 0, 17, length)
    csum = ip_checksum(pseudo + struct.pack("!HHHH", sport, dport, length, 0) + payload) or 0xFFFF
    udp = struct.pack("!HHHH", sport, dport, length, csum) + payload
    return eth_header(dst_mac, src_mac, ETH_IP) + ipv4_packet(src_ip, dst_ip, 17, udp)


def tcp_frame(src_mac: bytes, dst_mac: bytes, src_ip: bytes, dst_ip: bytes, sport: int, dport: int,
              seq: int, ack: int, flags: int, payload: bytes = b"") -> bytes:
    hdr = struct.pack("!HHIIBBHHH", sport, dport, seq, ack, 5 << 4, flags, 8192, 0, 0)
    length = len(hdr) + len(payload)
    pseudo = src_ip + dst_ip + struct.pack("!BBH", 0, 6, length)
    csum = ip_checksum(pseudo + hdr + payload)
    tcp = hdr[:16] + struct.pack("!H", csum) + hdr[18:] + payload
    return eth_header(dst_mac, src_mac, ETH_IP) + ipv4_packet(src_ip, dst_ip, 6, tcp)


def icmp_echo(kind: int, ident: int, seq: int, data: bytes) -> bytes:
    body = struct.pack("!BBHHH", kind, 0, 0, ident, seq) + data
    return body[:2] + struct.pack("!H", ip_checksum(body)) + body[4:]


def arp_frame(op: int, sha: bytes, spa: bytes, tha: bytes, tpa: bytes, dst_mac: bytes) -> bytes:
    return eth_header(dst_mac, sha, ETH_ARP) + struct.pack("!HHBBH6s4s6s4s", 1, ETH_IP, 6, 4, op, sha, spa, tha, tpa)


def dhcp_message(op: int, xid: int, chaddr: bytes, *, yiaddr: bytes = ZERO_IP, siaddr: bytes = ZERO_IP,
                 flags: int = 0, options: bytes = b"") -> bytes:
    msg = struct.pack("!BBBBIHH4s4s4s4s16s64s128sI", op, 1, 6, 0, xid, 0, flags, ZERO_IP, yiaddr, siaddr, ZERO_IP,
                      chaddr.ljust(16, b"\x00"), b"", b"", 0x63825363) + options + b"\xff"
    return msg.ljust(300, b"\x00")


def dhcp_options(raw: bytes) -> dict[int, bytes]:
    opts: dict[int, bytes] = {}
    i = 0
    while i < len(raw):
        code = raw[i]
        if code == 255:
            break
        if code == 0:
            i += 1
            continue
        if i + 1 >= len(raw):
            break
        length = raw[i + 1]
        opts[code] = raw[i + 2:i + 2 + length]
        i += 2 + length
    return opts


@dataclass
class VirtualAp:
    ssid: bytes
    passphrase: str | None
    bssid: bytes
    channel: int
    rssi: int


class VirtualWifiNetwork:
    ROUTER_IP = ip4("192.168.50.1")
    NETMASK = ip4("255.255.255.0")
    POOL_FIRST = 100
    ROUTER_MAC = bytes.fromhex("0200c0a83201")
    CLIENT_MAC = bytes.fromhex("0200c0a80442")
    PRINTER_AP_IP = ip4("192.168.4.1")

    def __init__(self, card) -> None:
        self.card = card
        card.network = self
        self.aps: dict[bytes, VirtualAp] = {}
        self.log: list[str] = []
        self.up = False
        self.ap_mode = False
        self.wsec = 0
        self.wpa_auth = 0
        self.sup_wpa = 0
        self.passphrase: str | None = None
        self.channel = 0
        self.event_mask = bytes(16)
        self.joined: VirtualAp | None = None
        self.ap_running = False
        self.ap_ssid = b""
        self.printer_ip: bytes | None = None
        self.leases: dict[bytes, bytes] = {}
        self.echo_replies: list[int] = []          # our pings the printer answered
        self.echo_requests: list[int] = []         # the printer's pings we answered
        self.client_ip: bytes | None = None
        self.client_xid = 0x5A5A0001
        self.http_sport = 49152
        self.http_pending_req: bytes | None = None
        self.http_dport: int = 80
        self.http_seq: int = 1000
        self.http_response: bytearray = bytearray()
        self.http_done: bool = False
        self.http_body = b""                       # request body still to send (POST), windowed
        self.http_body_base = 0                    # sequence number of the body's first byte
        self.http_body_sent = 0
        self.http_window = 0
        self.http_server_next = 0
        self.mdns_answers: list[tuple[str, bytes, int]] = []   # (name, address, destination port)
        # raw TCP client (telnet test): one connection at a time
        self.raw_sport = 0
        self.raw_dport = 0
        self.raw_seq = 0
        self.raw_server_next = 0
        self.raw_rx = bytearray()
        self.raw_connected = False
        self.raw_closed = False

    @property
    def printer_mac(self) -> bytes:
        return bytes(self.card.mac_address[:6])

    def add_ap(self, ssid: str | bytes, passphrase: str | None = None, *, channel: int = 6, rssi: int = -48,
               bssid: bytes | None = None) -> VirtualAp:
        """An access point the firmware can join (and find in a scan)."""
        ssid_b = ssid.encode() if isinstance(ssid, str) else bytes(ssid)
        bssid = bssid or bytes([0x02, 0x00, 0x00, 0x50, 0x00, len(self.aps) + 1])
        self.aps[ssid_b] = VirtualAp(ssid_b, passphrase, bssid, channel, rssi)
        self.card.add_scan_ap(ssid_b, bssid, channel=channel, rssi=rssi, security="wpa2" if passphrase else "open")
        return self.aps[ssid_b]

    # -- events and frames towards the firmware ------------------------------------
    def _event(self, etype: int, *, status: int = 0, reason: int = 0, flags: int = 0,
               addr: bytes | None = None) -> None:
        if not self.event_mask[etype // 8] & (1 << (etype % 8)):
            self.log.append(f"event {etype} not enabled by the firmware, dropped")
            return
        if addr is None:
            addr = self.joined.bssid if self.joined else self.printer_mac
        pkt = self.card.build_brcm_event(etype, status=status, reason=reason, flags=flags, bssid=addr, ifidx=0)
        self.card.queue_ethernet_frame(pkt, ifidx=0)

    def _send(self, frame: bytes) -> None:
        self.card.queue_ethernet_frame(frame, ifidx=0)

    # -- control requests (called by sdio.Bcm43143SdioCard) ----------------------
    def handle_ioctl(self, cmd: int, is_set: bool, ifidx: int, payload: bytes, ioc_len: int):
        """Return (answer payload, status, actions to run after the answer) or None for the default model."""
        def ok(data: bytes | None = None, after=()):
            return (payload[:ioc_len] if data is None else data), STATUS_SUCCESS, list(after)

        value = int.from_bytes(payload[:4], "little") if len(payload) >= 4 else 0
        if cmd == WLC_UP:
            self.up = True
            return ok()
        if cmd == WLC_DOWN:
            self.up = False
            self.ap_running = False
            return ok()
        if cmd == WLC_SET_AP:
            self.ap_mode = bool(value)
            return ok()
        if cmd == WLC_SET_WSEC:
            self.wsec = value
            return ok()
        if cmd == WLC_SET_WPA_AUTH:
            self.wpa_auth = value
            return ok()
        if cmd == WLC_SET_CHANNEL:
            self.channel = value
            return ok()
        if cmd in (WLC_SET_INFRA, WLC_SET_AUTH, WLC_SET_PM):
            return ok()
        if cmd == WLC_SET_WSEC_PMK:
            key_len = int.from_bytes(payload[0:2], "little")
            flags = int.from_bytes(payload[2:4], "little")
            self.passphrase = payload[4:4 + key_len].decode("latin1") if flags & 1 else None
            return ok()
        if cmd == WLC_SET_VAR:
            nul = payload.find(0)
            name = payload[:nul] if nul >= 0 else payload
            val = payload[nul + 1:] if nul >= 0 else b""
            if name == b"bsscfg:sup_wpa" and len(val) >= 8:
                self.sup_wpa = int.from_bytes(val[4:8], "little")
            elif name == b"event_msgs":
                self.event_mask = bytes(val[:16]).ljust(16, b"\x00")
            return None                                  # the card keeps storing iovars as before
        if cmd == WLC_GET_RSSI:
            return struct.pack("<i", self.joined.rssi if self.joined else 0), STATUS_SUCCESS, []
        if cmd == WLC_DISASSOC:
            if self.joined is None:
                return ok()
            ap = self.joined
            self.joined = None
            self.log.append(f"left {ap.ssid.decode()}")
            return ok(after=[lambda: self._event(E_DISASSOC, addr=ap.bssid),
                             lambda: self._event(E_LINK, flags=0, addr=ap.bssid)])
        if cmd == WLC_SET_SSID:
            n = min(int.from_bytes(payload[0:4], "little"), 32)
            ssid = bytes(payload[4:4 + n])
            if self.ap_mode:
                return ok(after=[lambda: self._start_ap(ssid)])
            return ok(after=[lambda: self._join(ssid)])
        return None

    def _join(self, ssid: bytes) -> None:
        ap = self.aps.get(ssid)
        if ap is None or not self.up:
            self.log.append(f"join {ssid.decode('latin1')}: no such network")
            self._event(E_SET_SSID, status=STATUS_NO_NETWORKS, addr=ZERO_IP + b"\x00\x00")
            return
        if ap.passphrase is None:
            if self.wsec:
                self.log.append(f"join {ap.ssid.decode()}: security configured for an open network")
                self._event(E_SET_SSID, status=STATUS_FAIL, addr=ap.bssid)
                return
            self.joined = ap
            self.log.append(f"joined {ap.ssid.decode()} (open)")
            self._event(E_LINK, flags=1, addr=ap.bssid)
            self._event(E_SET_SSID, addr=ap.bssid)
            return
        if not (self.wsec & 4 and self.wpa_auth == 0x80 and self.sup_wpa == 1):
            self.log.append(f"join {ap.ssid.decode()}: WPA2 supplicant not configured")
            self._event(E_SET_SSID, status=STATUS_FAIL, addr=ap.bssid)
            return
        self._event(E_LINK, flags=1, addr=ap.bssid)    # associated; the 4-way handshake follows
        if self.passphrase != ap.passphrase:
            self.log.append(f"join {ap.ssid.decode()}: wrong passphrase")
            self._event(E_PSK_SUP, status=SUP_WAIT_M3, reason=SUP_REASON_PSK_TIMEOUT, addr=ap.bssid)
            self._event(E_LINK, flags=0, addr=ap.bssid)
            return
        self.joined = ap
        self.log.append(f"joined {ap.ssid.decode()} (WPA2)")
        self._event(E_PSK_SUP, status=SUP_KEYED, addr=ap.bssid)
        self._event(E_SET_SSID, addr=ap.bssid)

    def _start_ap(self, ssid: bytes) -> None:
        if not self.up:
            self._event(E_SET_SSID, status=STATUS_FAIL)
            return
        self.ap_running = True
        self.ap_ssid = ssid
        self.printer_ip = self.PRINTER_AP_IP
        security = "WPA2" if (self.wsec & 4 and self.wpa_auth == 0x80) else "open"
        self.log.append(f"access point {ssid.decode('latin1')} up on channel {self.channel} ({security})")
        self._event(E_SET_SSID, addr=self.printer_mac)
        self._event(E_LINK, flags=1, addr=self.printer_mac)

    # -- frames from the firmware --------------------------------------------------
    def _peer(self) -> tuple[bytes | None, bytes | None]:
        """The machine on the other side: the router (station mode) or the client (AP mode)."""
        if self.joined is not None:
            return self.ROUTER_MAC, self.ROUTER_IP
        if self.ap_running and self.client_ip is not None:
            return self.CLIENT_MAC, self.client_ip
        return None, None

    def on_frame(self, pkt: bytes) -> None:
        if len(pkt) < 14:
            return
        if pkt[6:12] != self.printer_mac:
            self.log.append("frame from an unexpected source address")
            return
        ethertype = int.from_bytes(pkt[12:14], "big")
        if ethertype == ETH_ARP:
            self._arp(pkt[14:])
        elif ethertype == ETH_IP:
            self._ip(pkt[14:])

    def _arp(self, a: bytes) -> None:
        if len(a) < 28:
            return
        op = int.from_bytes(a[6:8], "big")
        sha, spa, tpa = a[8:14], a[14:18], a[24:28]
        if spa != ZERO_IP:
            self.printer_ip = spa
        mac, ip = self._peer()
        if op == 1 and mac is not None and tpa == ip:
            self._send(arp_frame(2, mac, ip, sha, spa, sha))
            self.log.append(f"arp: told the printer where {ip_str(ip)} is")

    def _ip(self, p: bytes) -> None:
        if len(p) < 20:
            return
        ihl = (p[0] & 15) * 4
        total = int.from_bytes(p[2:4], "big")
        if ip_checksum(p[:ihl]) != 0:
            self.log.append("bad IP header checksum")
            return
        src, dst = p[12:16], p[16:20]
        if src != ZERO_IP:
            self.printer_ip = src
        body = p[ihl:total]
        if p[9] == 1:
            self._icmp(src, dst, body)
        elif p[9] == 6:
            self._tcp(src, dst, body)
        elif p[9] == 17:
            self._udp(src, dst, body)

    def _icmp(self, src: bytes, dst: bytes, body: bytes) -> None:
        if len(body) < 8 or ip_checksum(body) != 0:
            self.log.append("bad ICMP checksum")
            return
        kind = body[0]
        ident, seq = struct.unpack("!HH", body[4:8])
        mac, ip = self._peer()
        if kind == 8 and mac is not None and dst == ip:
            self.echo_requests.append(seq)
            reply = icmp_echo(0, ident, seq, body[8:])
            self._send(eth_header(self.printer_mac, mac, ETH_IP) + ipv4_packet(ip, src, 1, reply))
        elif kind == 0 and ident == PING_IDENT:
            self.echo_replies.append(seq)

    def _udp(self, src: bytes, dst: bytes, body: bytes) -> None:
        if len(body) < 8:
            return
        sport, dport, length, csum = struct.unpack("!HHHH", body[:8])
        if csum and ip_checksum(src + dst + struct.pack("!BBH", 0, 17, length) + body[:length]) != 0:
            self.log.append("bad UDP checksum")
            return
        data = body[8:length]
        if sport == 5353:
            self._mdns(dport, data)
        elif dport == 67 and self.joined is not None:
            self._dhcp_server(data)
        elif dport == 68 and self.ap_running:
            self._dhcp_client(data)

    def _dhcp_server(self, d: bytes) -> None:
        """The router's DHCP server (station mode)."""
        if len(d) < 240 or d[0] != 1:
            return
        xid = struct.unpack("!I", d[4:8])[0]
        chaddr = d[28:34]
        opts = dhcp_options(d[240:])
        kind = opts.get(53, b"\x00")[0]
        if chaddr not in self.leases:
            self.leases[chaddr] = bytes([192, 168, 50, self.POOL_FIRST + len(self.leases)])
        ip = self.leases[chaddr]
        if kind == 1:
            answer = 2
        elif kind == 3:
            answer = 5 if (opts.get(50) or d[12:16]) == ip else 6
        else:
            return
        self.log.append(f"dhcp server: {'discover' if kind == 1 else 'request'} -> "
                        f"{ {2: 'offer', 5: 'ack', 6: 'nak'}[answer] } {ip_str(ip)}")
        options = bytes([53, 1, answer, 54, 4]) + self.ROUTER_IP
        if answer != 6:
            options += bytes([51, 4]) + (3600).to_bytes(4, "big") + bytes([1, 4]) + self.NETMASK
            options += bytes([3, 4]) + self.ROUTER_IP
        msg = dhcp_message(2, xid, chaddr, yiaddr=ip if answer != 6 else ZERO_IP, siaddr=self.ROUTER_IP,
                           flags=0x8000, options=options)
        self._send(udp_frame(self.ROUTER_MAC, BCAST_MAC, self.ROUTER_IP, BCAST_IP, 67, 68, msg))

    # -- the client station in access point mode -------------------------------------
    def associate_client(self) -> None:
        """A station joins the printer's access point and asks for an address."""
        if not self.ap_running:
            raise RuntimeError("the printer has no access point running")
        self.log.append("client associated")
        self._event(E_ASSOC_IND, addr=self.CLIENT_MAC)
        self.client_xid += 1
        discover = dhcp_message(1, self.client_xid, self.CLIENT_MAC, flags=0x8000, options=bytes([53, 1, 1]))
        self._send(udp_frame(self.CLIENT_MAC, BCAST_MAC, ZERO_IP, BCAST_IP, 68, 67, discover))

    def _dhcp_client(self, d: bytes) -> None:
        if len(d) < 240 or d[0] != 2 or struct.unpack("!I", d[4:8])[0] != self.client_xid or d[28:34] != self.CLIENT_MAC:
            return
        opts = dhcp_options(d[240:])
        kind = opts.get(53, b"\x00")[0]
        yiaddr = d[16:20]
        if kind == 2:
            self.log.append(f"dhcp client: offered {ip_str(yiaddr)}")
            request = dhcp_message(1, self.client_xid, self.CLIENT_MAC, flags=0x8000,
                                   options=bytes([53, 1, 3, 50, 4]) + yiaddr + bytes([54, 4]) + opts.get(54, ZERO_IP))
            self._send(udp_frame(self.CLIENT_MAC, BCAST_MAC, ZERO_IP, BCAST_IP, 68, 67, request))
        elif kind == 5:
            self.client_ip = yiaddr
            self.log.append(f"dhcp client: bound to {ip_str(yiaddr)}")
        elif kind == 6:
            self.log.append("dhcp client: refused")

    def ping_printer(self, count: int = 1, first_seq: int = 0) -> None:
        """Echo requests to the printer from the router (station mode) or the client (AP mode)."""
        mac, ip = self._peer()
        if mac is None or self.printer_ip is None:
            raise RuntimeError("nothing on the link has an address yet")
        for seq in range(first_seq, first_seq + count):
            request = icmp_echo(8, PING_IDENT, seq, b"virtual-network-ping")
            self._send(eth_header(self.printer_mac, mac, ETH_IP) + ipv4_packet(ip, self.printer_ip, 1, request))

    def start_http_get(self, path: str, port: int = 80) -> None:
        """Initiate a TCP handshake and HTTP GET request to the printer."""
        self.start_http_request("GET", path, port=port)

    def start_http_request(self, method: str, path: str, body: bytes = b"", *, port: int = 80) -> None:
        """TCP handshake, then the request; a body (POST) follows in 1360-byte
        segments, as many as the printer's advertised window allows."""
        mac, ip = self._peer()
        if mac is None or self.printer_ip is None:
            raise RuntimeError("nothing on the link has an address yet")
        self.http_sport += 1
        self.http_dport = port
        self.http_seq = 10000 + self.http_sport * 100
        head = f"{method} {path} HTTP/1.1\r\nHost: {ip_str(self.printer_ip)}\r\n"
        if body:
            head += f"Content-Type: application/octet-stream\r\nContent-Length: {len(body)}\r\n"
        self.http_pending_req = (head + "\r\n").encode("ascii")
        self.http_body = bytes(body)
        self.http_body_sent = 0
        self.http_response.clear()
        self.http_done = False
        self._send(tcp_frame(mac, self.printer_mac, ip, self.printer_ip, self.http_sport, port,
                             self.http_seq, 0, 0x02))  # SYN

    def _send_body(self, mac: bytes, ip: bytes, dst: bytes, dport: int, acked: int = 0) -> None:
        while (self.http_body_sent < len(self.http_body)
               and self.http_body_sent - acked + 1360 <= max(self.http_window, 1360)):
            chunk = self.http_body[self.http_body_sent:self.http_body_sent + 1360]
            self._send(tcp_frame(mac, self.printer_mac, ip, dst, self.http_sport, dport,
                                 (self.http_body_base + self.http_body_sent) & 0xFFFFFFFF,
                                 self.http_server_next, 0x18, chunk))
            self.http_body_sent += len(chunk)
        if self.http_body and self.http_body_sent >= len(self.http_body):
            self.http_seq = (self.http_body_base + len(self.http_body)) & 0xFFFFFFFF

    def raw_connect(self, port: int) -> None:
        """Open a plain TCP connection to the printer (SYN); data arrives in raw_rx."""
        mac, ip = self._peer()
        if mac is None or self.printer_ip is None:
            raise RuntimeError("nothing on the link has an address yet")
        self.raw_sport = 52000 + (self.raw_sport % 100) + 1
        self.raw_dport = port
        self.raw_seq = 77000
        self.raw_rx = bytearray()
        self.raw_connected = self.raw_closed = False
        self._send(tcp_frame(mac, self.printer_mac, ip, self.printer_ip, self.raw_sport, port, self.raw_seq, 0, 0x02))

    def raw_send(self, data: bytes) -> None:
        mac, ip = self._peer()
        self._send(tcp_frame(mac, self.printer_mac, ip, self.printer_ip, self.raw_sport, self.raw_dport,
                             self.raw_seq, self.raw_server_next, 0x18, data))
        self.raw_seq = (self.raw_seq + len(data)) & 0xFFFFFFFF

    def _raw_tcp(self, src: bytes, sport: int, seq: int, flags: int, data: bytes) -> None:
        mac, ip = self._peer()
        if flags & 0x04:
            self.raw_closed = True
            return
        if (flags & 0x12) == 0x12 and not self.raw_connected:          # SYN+ACK
            self.raw_seq += 1
            self.raw_server_next = (seq + 1) & 0xFFFFFFFF
            self.raw_connected = True
            self._send(tcp_frame(mac, self.printer_mac, ip, src, self.raw_sport, sport,
                                 self.raw_seq, self.raw_server_next, 0x10))
            return
        if data and seq == self.raw_server_next:
            self.raw_rx.extend(data)
            self.raw_server_next = (seq + len(data)) & 0xFFFFFFFF
        if flags & 0x01:
            self.raw_server_next = (self.raw_server_next + 1) & 0xFFFFFFFF
            self.raw_closed = True
        if data or flags & 0x01:
            self._send(tcp_frame(mac, self.printer_mac, ip, src, self.raw_sport, sport,
                                 self.raw_seq, self.raw_server_next, 0x10))

    def mdns_query(self, name: str, *, legacy: bool = False) -> None:
        """An mDNS question for an A record, from the router (station mode):
        multicast from port 5353, or a legacy one-shot query from port 40000."""
        mac, ip = self._peer()
        if mac is None:
            raise RuntimeError("no link")
        qname = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"
        msg = struct.pack("!HHHHHH", 0x1234 if legacy else 0, 0, 1, 0, 0, 0) + qname + struct.pack("!HH", 1, 1)
        self._send(udp_frame(mac, bytes.fromhex("01005e0000fb"), ip, ip4("224.0.0.251"),
                             40000 if legacy else 5353, 5353, msg))

    def _mdns(self, dport: int, data: bytes) -> None:
        if len(data) < 12 or not data[2] & 0x80:
            return
        qd, an = struct.unpack("!HH", data[4:8])
        off = 12

        def name_at(o: int) -> tuple[str, int]:
            parts = []
            while data[o]:
                parts.append(data[o + 1:o + 1 + data[o]].decode())
                o += 1 + data[o]
            return ".".join(parts), o + 1
        for _ in range(qd):
            _, off = name_at(off)
            off += 4
        for _ in range(an):
            nm, off = name_at(off)
            rtype, _cls, _ttl, rdlen = struct.unpack("!HHIH", data[off:off + 10])
            if rtype == 1:
                self.mdns_answers.append((nm, data[off + 10:off + 14], dport))
            off += 10 + rdlen

    def _tcp(self, src: bytes, dst: bytes, body: bytes) -> None:
        if len(body) < 20:
            return
        sport, dport, seq, ack, off_Byte, flags = struct.unpack("!HHIIBB", body[:14])
        hlen = (off_Byte >> 4) * 4
        if ip_checksum(src + dst + struct.pack("!BBH", 0, 6, len(body)) + body) != 0:
            self.log.append("bad TCP checksum")
            return
        if self.raw_sport and dport == self.raw_sport:
            self._raw_tcp(src, sport, seq, flags, body[hlen:])
            return
        if dport != self.http_sport:
            return
        mac, ip = self._peer()
        if mac is None or ip is None:
            return
        data = body[hlen:]
        if flags & 0x04:  # RST
            self.http_done = True
            return
        window = struct.unpack("!H", body[14:16])[0]
        if (flags & 0x12) == 0x12 and self.http_pending_req is not None:  # SYN+ACK
            self.http_seq += 1
            req = self.http_pending_req
            self.http_pending_req = None
            self.http_server_next = (seq + 1) & 0xFFFFFFFF
            self._send(tcp_frame(mac, self.printer_mac, ip, src, self.http_sport, sport,
                                 self.http_seq, self.http_server_next, 0x18, req))  # PSH+ACK
            self.http_seq += len(req)
            self.http_body_base = self.http_seq
            self.http_window = window
            self._send_body(mac, ip, src, sport)
            return
        if self.http_body and flags & 0x10:
            acked = (ack - self.http_body_base) & 0xFFFFFFFF
            if acked <= len(self.http_body):
                self.http_window = window
                self._send_body(mac, ip, src, sport, acked)
        if data:
            self.http_response.extend(data)
        if flags & 0x01:  # FIN
            self._send(tcp_frame(mac, self.printer_mac, ip, src, self.http_sport, sport,
                                 self.http_seq, (seq + len(data) + 1) & 0xFFFFFFFF, 0x10))  # ACK
            self.http_done = True
        elif data:
            self._send(tcp_frame(mac, self.printer_mac, ip, src, self.http_sport, sport,
                                 self.http_seq, (seq + len(data)) & 0xFFFFFFFF, 0x10))  # ACK
