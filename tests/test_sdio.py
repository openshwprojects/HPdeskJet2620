from pathlib import Path
import pickle
import socket
import struct
import unittest

from sdio import (
    Bcm43143SdioCard,
    SDIO_BASE,
    SDIOError,
    SdioHostController,
    _build_arp_request,
    _build_icmp_echo,
    _build_tcp_packet,
    _ip_checksum,
    build_arp_request,
    build_icmp_echo,
    build_tcp_packet,
    ip_checksum,
)


class SdioControllerUnitTests(unittest.TestCase):
    def test_f2_rx_queue_and_ethernet_injection(self):
        irqs: list[tuple[int, bool]] = []
        ram = bytearray(0x2000)

        sdio = SdioHostController(
            set_irq=lambda src, lvl: irqs.append((src, lvl)),
            dma_read=lambda addr, size: bytes(ram[addr : addr + size]),
            dma_write=lambda addr, data: ram.__setitem__(slice(addr, addr + len(data)), data),
        )
        # Enable card interrupt in CCCR and SDHCI host controller
        sdio.card.write_byte(0, 0x04, 0x07)
        sdio.write(SDIO_BASE + 0x34, 4, 0x01FF)
        sdio.write(SDIO_BASE + 0x38, 4, 0x01FF)

        eth1 = b"\xff" * 6 + b"\x00\x11\x22\x33\x44\x55\x08\x06" + b"\xaa" * 28
        eth2 = b"\xff" * 6 + b"\x00\x11\x22\x33\x44\x55\x08\x00" + b"\xbb" * 46
        sdio.inject_ethernet_frame(eth1, ifidx=1)
        sdio.inject_ethernet_frame(eth2, ifidx=1)

        self.assertTrue(sdio.card.card_irq_pending)
        self.assertEqual(irqs[-1], (63, True))

        # Read first queued frame on Function 2
        frame1 = sdio.card.read_extended(2, 0, 128, True)
        flen1, cksum1 = struct.unpack_from("<HH", frame1, 0)
        self.assertEqual(flen1, 12 + 4 + 60)
        self.assertEqual(cksum1, (~flen1) & 0xFFFF)
        self.assertEqual(frame1[12:16], b"\x20\x00\x01\x00")
        self.assertTrue(frame1[16:76].startswith(eth1))

        # Second frame automatically activates after first frame is drained
        self.assertTrue(sdio.card.card_irq_pending)
        frame2 = sdio.card.read_extended(2, 0, 128, True)
        flen2, _cksum2 = struct.unpack_from("<HH", frame2, 0)
        self.assertEqual(flen2, 12 + 4 + 60)
        self.assertEqual(frame2[16:76], eth2)
        self.assertFalse(sdio.card.card_irq_pending)
        self.assertEqual(sdio.card.sdiod_intstatus, 0)
        self.assertEqual(irqs[-1], (63, False))

        # Per-function interrupt enable bits in CCCR 0x04 gate card_irq_pending
        sdio.card.write_byte(0, 0x04, 0x01)  # IENM only, no Fn1/Fn2 enable bits
        sdio.card.queue_ethernet_frame(eth1, ifidx=1)
        self.assertFalse(sdio.card.card_irq_pending)
        self.assertEqual(irqs[-1], (63, False))
        sdio.card.write_byte(0, 0x04, 0x05)  # Enable IENM + IEN2
        self.assertTrue(sdio.card.card_irq_pending)
        self.assertEqual(irqs[-1], (63, True))
        sdio.card.write_byte(0, 0x06, 0x02)  # I/O Abort clears pending RX and IRQ
        self.assertFalse(sdio.card.card_irq_pending)
        self.assertEqual(sdio.card.sdiod_intstatus, 0)
        self.assertEqual(irqs[-1], (63, False))

        with self.assertRaises(SDIOError):
            sdio.read(SDIO_BASE + 0x100, 4)

    def test_subword_writes_and_reset(self):
        from hardware import BootHardware

        board = BootHardware()
        sdio = board.sdio
        # Writing only the 16-bit Transfer Mode halfword (SDIO_BASE + 0x0E) does not execute a command
        board.write(SDIO_BASE + 0x0E, 2, 0x0013)
        self.assertEqual(sdio.transfer_mode, 0x0013)
        self.assertEqual(sdio.cmd_count, 0)
        # Writing the 16-bit Command halfword (SDIO_BASE + 0x0C) executes the command
        board.write(SDIO_BASE + 0x08, 4, 0x1AA)
        board.write(SDIO_BASE + 0x0C, 2, (8 << 8) | 0x1A)
        self.assertEqual(sdio.cmd_count, 1)
        self.assertEqual(board.read(SDIO_BASE + 0x10, 4), 0x1AA)

        # Trigger CMD55 timeout error (0x00010000) while Command Complete (0x00000001) is also set
        board.write(SDIO_BASE + 0x0C, 2, (55 << 8) | 0x1A)
        self.assertEqual(board.read(SDIO_BASE + 0x30, 4), 0x00010001)
        # 16-bit W1C write to lower halfword (SDIO_BASE + 0x32) clears 0x0001 without clearing 0x00010000
        board.write(SDIO_BASE + 0x32, 2, 0x0001)
        self.assertEqual(board.read(SDIO_BASE + 0x30, 4), 0x00010000)
        # 16-bit W1C write to upper halfword (SDIO_BASE + 0x30) clears 0x00010000
        board.write(SDIO_BASE + 0x30, 2, 0x0001)
        self.assertEqual(board.read(SDIO_BASE + 0x30, 4), 0x00000000)

        sdio.card.reset()
        sdio.reset()
        self.assertEqual(sdio.cmd_count, 0)
        self.assertEqual(sdio.int_status, 0)

    def test_wlan_scan_ioctls_and_brcm_events(self):
        sdio = SdioHostController(set_irq=lambda _src, _lvl: None)
        card = sdio.card

        def send_ioctl(cmd: int, payload: bytes = b"", *, is_set: bool = False, ifidx: int = 0, ioc_len: int = 512) -> bytes:
            flags = (1 << 16) | ((ifidx & 0x0F) << 12) | (0x02 if is_set else 0x00)
            cdc = struct.pack("<IIII", cmd, max(ioc_len, len(payload)), flags, 0) + payload
            sdpcm = struct.pack("<HHBBBBBBBB", 12 + len(cdc), (~(12 + len(cdc))) & 0xFFFF, 1, 0, 0, 12, 0, 64, 0, 0) + cdc
            card.write_extended(2, 0, sdpcm, True)
            raw = card.read_extended(2, 0, 1024, True)
            flen = int.from_bytes(raw[0:2], "little")
            return raw[12 + 16 : flen]

        # 1. Radio / STA status ioctls (WLC_GET_RATE=12, WLC_GET_CHANNEL=29, WLC_GET_RSSI=127, WLC_GET_PHY_NOISE=135)
        self.assertEqual(struct.unpack("<i", send_ioctl(12, ioc_len=4))[0], 144)
        self.assertEqual(struct.unpack("<III", send_ioctl(29, ioc_len=12)), (6, 6, 0))
        self.assertEqual(struct.unpack("<i", send_ioctl(127, ioc_len=4))[0], -42)
        self.assertEqual(struct.unpack("<i", send_ioctl(135, ioc_len=4))[0], -90)

        # 2. Unassociated STA (ifidx=0) vs SoftAP (ifidx=1) BSSID & SSID
        self.assertEqual(send_ioctl(23, ifidx=0, ioc_len=6), b"\x00" * 6)
        self.assertEqual(send_ioctl(23, ifidx=1, ioc_len=6), bytes(card.mac_address))
        uap_ssid_resp = send_ioctl(25, ifidx=1, ioc_len=36)
        uap_len = int.from_bytes(uap_ssid_resp[0:4], "little")
        self.assertEqual(uap_ssid_resp[4 : 4 + uap_len], b"HP-Setup<17>DeskJet 2600 series")

        # 3. Triggering `iscan` queues ioctl response followed by WLC_E_SCAN_COMPLETE (0x886C event 26)
        sdio.clear_scan_aps()
        sdio.add_scan_ap("HP-Print-Lab-2.4G", b"\x00\x11\x22\x33\x44\x55", channel=6, rssi=-42, security="wpa2")
        sdio.add_scan_ap("Office_Guest_WiFi", b"\x66\x77\x88\x99\xaa\xbb", channel=11, rssi=-65, security="open")
        send_ioctl(263, b"iscan\x00\x01\x00\x00\x00", is_set=True, ioc_len=64)
        # Drain the queued WLC_E_SCAN_COMPLETE event frame
        ev_frame = card.read_extended(2, 0, 256, True)
        ev_len = int.from_bytes(ev_frame[0:2], "little")
        eth_pkt = ev_frame[16:ev_len]
        self.assertEqual(eth_pkt[12:14], b"\x88\x6c")
        self.assertEqual(int.from_bytes(eth_pkt[28:32], "big"), card.WLC_E_SCAN_COMPLETE)

        # 4. Fetch `iscanresults` via WLC_GET_VAR (262)
        iscan_res = send_ioctl(262, b"iscanresults\x00" + b"\x00" * 512, is_set=False, ioc_len=512)
        status, buflen, version, count = struct.unpack_from("<IIII", iscan_res, 0)
        self.assertEqual((status, version, count), (0, 109, 2))
        self.assertGreater(buflen, 256)
        self.assertIn(b"HP-Print-Lab-2.4G", iscan_res)
        self.assertIn(b"Office_Guest_WiFi", iscan_res)

        # 5. Associate STA via connect_ap / WLC_SET_SSID (26) and disassociate via WLC_DISASSOC (52)
        sdio.connect_ap("HP-Print-Lab-2.4G", b"\x00\x11\x22\x33\x44\x55", channel=6, rssi=-38)
        set_ssid_ev = card.read_extended(2, 0, 256, True)
        self.assertEqual(int.from_bytes(set_ssid_ev[16 + 28 : 16 + 32], "big"), card.WLC_E_SET_SSID)
        self.assertTrue(card.associated)
        self.assertEqual(send_ioctl(23, ifidx=0, ioc_len=6), b"\x00\x11\x22\x33\x44\x55")
        bss_resp = send_ioctl(136, ifidx=0, ioc_len=256)
        self.assertEqual(int.from_bytes(bss_resp[0:4], "little"), 109)
        self.assertIn(b"HP-Print-Lab-2.4G", bss_resp)

        sdio.disconnect_ap(reason=3)
        deauth_ev = card.read_extended(2, 0, 256, True)
        self.assertEqual(int.from_bytes(deauth_ev[16 + 28 : 16 + 32], "big"), card.WLC_E_DEAUTH_IND)
        self.assertFalse(card.associated)

    def test_packet_builders_and_ip_checksum(self):
        src_mac = b"\x02\x00\x11\x22\x33\x44"
        dst_mac = b"\x12\x93\x34\x03\x31\x2b"
        arp = build_arp_request(src_mac, "192.168.223.100", "192.168.223.1")
        self.assertEqual(len(arp), 42)
        self.assertEqual(arp[0:6], b"\xff\xff\xff\xff\xff\xff")
        self.assertEqual(arp[12:14], b"\x08\x06")
        self.assertEqual(arp[20:22], b"\x00\x01")

        icmp = build_icmp_echo(src_mac, dst_mac, "192.168.223.100", "192.168.223.1", seq=7, payload=b"OddLen!")
        self.assertEqual(icmp[12:14], b"\x08\x00")
        self.assertEqual(ip_checksum(icmp[14:34]), 0)
        self.assertEqual(ip_checksum(icmp[34:]), 0)

        tcp = build_tcp_packet(
            src_mac, dst_mac, "192.168.223.100", "192.168.223.1",
            sport=49152, dport=80, seq=0x100, ack=0x200, flags=0x18, payload=b"GET / HTTP/1.1\r\n\r\n"
        )
        self.assertEqual(tcp[12:14], b"\x08\x00")
        self.assertEqual(ip_checksum(tcp[14:34]), 0)
        pseudo = tcp[26:34] + struct.pack("!BBH", 0, 6, len(tcp) - 34)
        self.assertEqual(ip_checksum(pseudo + tcp[34:]), 0)


class SdioLiveWifiNetworkStackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from analysis.uart_selftest import load_default_dump_and_snapshot

        loaded = load_default_dump_and_snapshot()
        if loaded is None:
            raise unittest.SkipTest("dump.bin or Stage-22 snapshot not available")
        cls.dump, cls.snap = loaded

    def test_live_wifi_arp_icmp_tcp_and_http_ledm_xml(self):
        from analysis.uart_selftest import build_and_restore, step_restored

        emu = build_and_restore(self.dump, self.snap)
        sdio = emu.board.sdio
        card = sdio.card

        def step(insns: int) -> None:
            step_restored(emu, insns)

        def _stop_after(extra: int = 5_000) -> None:
            cur = emu._native_observer.state.instructions if emu._native_observer is not None else emu.instructions
            target = cur + max(0, extra)
            emu._limit = target
            if emu._native_observer is not None:
                emu._native_observer.state.limit = target

        host_mac = b"\x02\x00\x11\x22\x33\x44"
        host_ip = "192.168.223.100"
        printer_ip = "192.168.223.1"

        # 1. ARP Request -> ARP Reply (also flushes initial boot broadcast queue)
        card.tx_packets.clear()
        arp_reply_box = [None]

        def _on_arp_tx(pkt: bytes) -> None:
            if pkt[12:14] == b"\x08\x06" and pkt[20:22] == b"\x00\x02":
                arp_reply_box[0] = pkt
                card.on_tx_packet = None
                _stop_after(10_000)

        card.on_tx_packet = _on_arp_tx
        sdio.inject_ethernet_frame(_build_arp_request(host_mac, host_ip, printer_ip), ifidx=1)
        step(1_000_000)
        card.on_tx_packet = None
        arp_reply = arp_reply_box[0]
        self.assertIsNotNone(arp_reply)
        for _ in range(20):
            if card.f0[0x04]:
                break
            step(10_000)
        printer_mac = arp_reply[22:28]

        # 2. ICMP Echo Request -> ICMP Echo Reply
        card.tx_packets.clear()
        icmp_reply_box = [None]

        def _on_icmp_tx(pkt: bytes) -> None:
            if pkt[12:14] == b"\x08\x00" and pkt[23] == 1 and pkt[34] == 0:
                icmp_reply_box[0] = pkt
                card.on_tx_packet = None
                _stop_after(5_000)

        card.on_tx_packet = _on_icmp_tx
        sdio.inject_ethernet_frame(
            _build_icmp_echo(host_mac, printer_mac, host_ip, printer_ip, seq=1),
            ifidx=1,
        )
        step(750_000)
        card.on_tx_packet = None
        icmp_reply = icmp_reply_box[0]
        self.assertIsNotNone(icmp_reply)
        self.assertIn(b"HPDeskJet2620Ping!", icmp_reply)

        # 3. TCP 3-Way Handshake + HTTP GET /FirmwareUpdate/FirmwareUpdateDyn.xml on Port 80
        card.tx_packets.clear()
        sport = 49152
        client_seq = 0x10002000
        syn_ack_box = [None]

        def _on_syn_ack_tx(pkt: bytes) -> None:
            if len(pkt) >= 54 and pkt[12:14] == b"\x08\x00" and pkt[23] == 6:
                ihl = (pkt[14] & 0x0F) * 4
                if (pkt[14 + ihl + 13] & 0x12) == 0x12:
                    syn_ack_box[0] = pkt
                    card.on_tx_packet = None
                    _stop_after(5_000)

        card.on_tx_packet = _on_syn_ack_tx
        sdio.inject_ethernet_frame(
            _build_tcp_packet(host_mac, printer_mac, host_ip, printer_ip, sport, 80, client_seq, 0, 0x02),
            ifidx=1,
        )
        step(1_000_000)
        card.on_tx_packet = None
        syn_ack = syn_ack_box[0]
        self.assertIsNotNone(syn_ack)
        ihl = (syn_ack[14] & 0x0F) * 4
        srv_seq = int.from_bytes(syn_ack[14 + ihl + 4 : 14 + ihl + 8], "big")
        client_seq += 1
        srv_next = (srv_seq + 1) & 0xFFFFFFFF

        card.tx_packets.clear()
        http_req = (
            b"GET /FirmwareUpdate/FirmwareUpdateDyn.xml HTTP/1.1\r\n"
            b"Host: 192.168.223.1\r\n\r\n"
        )
        sdio.inject_ethernet_frame(
            _build_tcp_packet(host_mac, printer_mac, host_ip, printer_ip, sport, 80, client_seq, srv_next, 0x10),
            ifidx=1,
        )
        sdio.inject_ethernet_frame(
            _build_tcp_packet(
                host_mac, printer_mac, host_ip, printer_ip, sport, 80, client_seq, srv_next, 0x18, http_req
            ),
            ifidx=1,
        )
        client_seq += len(http_req)

        http_stream = bytearray()

        def _on_http_tx(pkt: bytes) -> None:
            if len(pkt) >= 54 and pkt[12:14] == b"\x08\x00" and pkt[23] == 6:
                ihl_local = (pkt[14] & 0x0F) * 4
                ip_total = int.from_bytes(pkt[16:18], "big")
                th_off = 14 + ihl_local
                data_off = ((pkt[th_off + 12] >> 4) & 0x0F) * 4
                p_seq = int.from_bytes(pkt[th_off + 4 : th_off + 8], "big")
                p_flags = pkt[th_off + 13]
                payload_start = th_off + data_off
                payload_end = 14 + ip_total
                tcp_payload = pkt[payload_start:payload_end]
                if tcp_payload:
                    http_stream.extend(tcp_payload)
                    ack_num = (p_seq + len(tcp_payload) + (1 if (p_flags & 1) else 0)) & 0xFFFFFFFF
                    card.queue_ethernet_frame(
                        _build_tcp_packet(
                            host_mac, printer_mac, host_ip, printer_ip, sport, 80, client_seq, ack_num, 0x10
                        ),
                        ifidx=1,
                    )
                    if b"</fwudyn:FirmwareUpdateDyn>" in http_stream:
                        card.on_tx_packet = None
                        _stop_after(0)

        card.on_tx_packet = _on_http_tx
        step(3_000_000)
        card.on_tx_packet = None

        self.assertIn(b"HTTP/1.1 200 OK\r\n", http_stream)
        self.assertIn(b"Server: HP HTTP Server; HP DeskJet 2600 series", http_stream)
        self.assertIn(b"<fwudyn:FirmwareUpdateDyn", http_stream)
        self.assertIn(b"</fwudyn:FirmwareUpdateDyn>", http_stream)

    def test_live_wifi_scan_dot11ap_config_and_sta_association(self):
        from analysis.uart_selftest import build_and_restore, step_restored

        prompt_reached = [False]

        def on_uart_byte(val: int) -> None:
            if val == 0x20 and emu.uart.endswith(b"-> ") and not emu.board.uart_rx and b"returns 0 (0x0)" in emu.uart[start_len:]:
                prompt_reached[0] = True
                emu.reason = "udws-done"
                emu.uc.emu_stop()

        on_uart_byte.trigger_byte = 0x13E20
        emu = build_and_restore(self.dump, self.snap, on_uart=on_uart_byte)
        emu.board.uart_frame_ticks = 1
        sdio = emu.board.sdio
        card = sdio.card

        # 1. Add a custom AP and verify live 802.11 wireless scan, radio FW/MAC, and SoftAP config in one pass
        sdio.add_scan_ap("HP-Fast-Lab-AP", b"\xaa\xbb\xcc\xdd\xee\xff", channel=1, rssi=-35, security="wpa2")
        start_len = len(emu.uart)
        emu.board.queue_rx(
            b'udws "nca.get_wireless_scan; nca.get_radio_fw_version; nca.get_permanent_radio_mac 1; nca.get_dot11ap_config;"\r'
        )
        step_restored(emu, 7_000_000)
        self.assertTrue(prompt_reached[0])
        out = bytes(emu.uart[start_len:]).decode("latin1", errors="replace")
        self.assertIn("00:11:22:33:44:55 HP-Print-Lab-2.4G", out)
        self.assertIn("WPA  -042 006", out)
        self.assertIn("66:77:88:99:AA:BB Office_Guest_WiFi", out)
        self.assertIn("None -065 011", out)
        self.assertIn("AA:BB:CC:DD:EE:FF HP-Fast-Lab-AP", out)
        self.assertIn("WPA  -035 001", out)
        self.assertIn("06.10.00.00", out)
        self.assertIn("30:E1:71:26:20:01", out)
        self.assertIn("State: connected", out)
        self.assertIn("ssid: HP-Setup<17>DeskJet 2600 series", out)
        self.assertIn("Channel:  6", out)

        # 2. Associate STA (ifidx=0) with discovered AP and verify full update_status ioctl sequence
        start_ioctls = card.ioctl_count

        def _on_ioctl(_cmd: int) -> None:
            if card.ioctl_count >= start_ioctls + 10:
                card.on_ioctl = None
                cur = emu._native_observer.state.instructions if emu._native_observer is not None else emu.instructions
                emu._limit = cur
                if emu._native_observer is not None:
                    emu._native_observer.state.limit = cur

        card.on_ioctl = _on_ioctl
        sdio.connect_ap("HP-Print-Lab-2.4G", b"\x00\x11\x22\x33\x44\x55", channel=6, rssi=-42)
        step_restored(emu, 1_100_000)
        card.on_ioctl = None
        self.assertGreaterEqual(card.ioctl_count, start_ioctls + 10)
        self.assertTrue(card.associated)

    def test_differential_python_vs_native_and_multiframe_rx(self):
        from analysis.uart_selftest import (
            REGISTERS,
            UC_ARM_REG_CPSR,
            build_and_restore,
            capture_snapshot,
            step_restored,
        )

        host_mac = b"\x02\x00\x11\x22\x33\x44"
        printer_mac = b"\x12\x93\x34\x03\x31\x2b"
        host_ip = "192.168.223.100"
        printer_ip = "192.168.223.1"

        # Drain the 12 initial Stage-22 boot broadcast packets once on native_observer=True
        # so the differential run measures back-to-back multiframe RX + ARP/ICMP TX parity directly.
        warm_emu = build_and_restore(self.dump, self.snap, native_observer=True)
        warm_emu.board.sdio.inject_ethernet_frame(
            _build_arp_request(host_mac, host_ip, printer_ip), ifidx=1
        )
        step_restored(warm_emu, 565_000)
        warm_snap = capture_snapshot(warm_emu)
        del warm_emu

        def run_trace(native: bool):
            emu = build_and_restore(self.dump, warm_snap, native_observer=native)
            emu.board.sdio.card.tx_packets.clear()
            # Queue two Ethernet frames back-to-back before stepping CPU
            emu.board.sdio.inject_ethernet_frame(
                _build_arp_request(host_mac, host_ip, printer_ip), ifidx=1
            )
            emu.board.sdio.inject_ethernet_frame(
                _build_icmp_echo(host_mac, printer_mac, host_ip, printer_ip, seq=7), ifidx=1
            )
            step_restored(emu, 90_000)
            res = (
                emu.instructions,
                emu.board.ticks,
                tuple(emu.uc.reg_read(r) for r in REGISTERS),
                emu.uc.reg_read(UC_ARM_REG_CPSR),
                list(emu.board.sdio.card.tx_packets),
            )
            del emu
            return res

        py_res = run_trace(False)
        nat_res = run_trace(True)
        self.assertEqual(py_res, nat_res)
        tx_pkts = py_res[4]
        self.assertTrue(any(p[12:14] == b"\x08\x06" and p[20:22] == b"\x00\x02" for p in tx_pkts))
        self.assertTrue(any(p[12:14] == b"\x08\x00" and p[23] == 1 and p[34] == 0 for p in tx_pkts))

    def test_live_http_network_services_and_xml(self) -> None:
        """Verify live HTTP GET requests over Wi-Fi for multiple EWS XML services."""
        from analysis.uart_selftest import build_and_restore
        from panel_gui import FrontPanelUIController

        emu = build_and_restore(self.dump, self.snap, native_observer=True)
        emu.board.uart_frame_ticks = 1
        ctrl = FrontPanelUIController(emu)
        ctrl.prepare_network_services()

        # 1. Product configuration
        res_prod = ctrl.wifi_http_get("/DevMgmt/ProductConfigDyn.xml")
        self.assertEqual(res_prod["status_line"], "HTTP/1.1 200 OK")
        self.assertGreater(res_prod["total_bytes"], 1000)
        self.assertIn("DeskJet 2600", res_prod["body"])

        # 2. Firmware update status
        res_fwd = ctrl.wifi_http_get("/FirmwareUpdate/FirmwareUpdateDyn.xml")
        self.assertEqual(res_fwd["status_line"], "HTTP/1.1 200 OK")
        self.assertGreater(res_fwd["total_bytes"], 1000)
        self.assertIn("FirmwareUpdateDyn", res_fwd["body"])


if __name__ == "__main__":
    unittest.main()

