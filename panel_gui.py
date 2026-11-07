"""Interactive visual front-panel GUI and headless controller for HP DeskJet 2620.

Provides:
- ``SEVEN_SEG_POLYGONS``: 2D beveled polygon coordinates for segments ``a..g``
  of the 7-segment display widget.
- ``BUTTON_LAYOUT``: physical front-panel buttons, labels, colors, and firmware
  ``ui.send_button`` event IDs.
- ``FrontPanelUIController``: headless-testable controller that wraps either a
  live ``Emulator`` instance or a standalone ``SerialShiftPanel``, exposing
  structured widget state (7-segment polygons, Wi-Fi bars, C/K ink gauges,
  status badges, LEDs, buttons, cartridge state) and interactive actions
  (button press/release/tap, service chords, cartridge install/remove, UDW CLI).
- ``FrontPanelTkApp``: zero-dependency ``tkinter`` desktop GUI rendering the
  HP DeskJet 2620 front panel LCD, glowing LEDs, tactile buttons, and UDW bar.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable

from panel import SerialShiftPanel


# Beveled 2D polygon coordinates (relative to the 7-segment digit origin)
SEVEN_SEG_POLYGONS: dict[str, tuple[tuple[int, int], ...]] = {
    'a': ((16, 8), (64, 8), (56, 18), (24, 18)),
    'b': ((66, 10), (72, 16), (68, 52), (60, 48), (58, 20)),
    'c': ((60, 56), (68, 60), (64, 96), (56, 102), (54, 62)),
    'd': ((20, 96), (52, 96), (60, 106), (12, 106)),
    'e': ((12, 60), (20, 56), (22, 94), (10, 102), (8, 64)),
    'f': ((16, 10), (26, 20), (22, 48), (14, 52), (10, 16)),
    'g': ((18, 52), (24, 47), (56, 47), (62, 52), (56, 57), (24, 57)),
}

BUTTON_LAYOUT: tuple[dict[str, object], ...] = (
    {'name': 'power',      'label': 'Power',      'event_id': 8252, 'color': '#2b3038'},
    {'name': 'cancel',     'label': 'Cancel (X)', 'event_id': 8202, 'color': '#7a2828'},
    {'name': 'resume',     'label': 'Resume',     'event_id': 8225, 'color': '#85581a'},
    {'name': 'wifi',       'label': 'Wireless',   'event_id': 8296, 'color': '#1d4f7a'},
    {'name': 'copy_black', 'label': 'Copy Black', 'event_id': 8236, 'color': '#383d42'},
    {'name': 'copy_color', 'label': 'Copy Color', 'event_id': 8237, 'color': '#1e6b43'},
)

BUTTON_EVENT_IDS: dict[str, int] = {
    str(entry['name']): int(entry['event_id']) for entry in BUTTON_LAYOUT
}

# Stand-alone display icons seen on the printer (analysis/front_panel.md), with
# short labels for the drawn badges.
BADGE_LABELS: dict[str, str] = {
    'wifi_icon': 'WIFI',
    'wireless_attention_icon': 'WIFI !',
    'wifi_direct_icon': 'WiFi-D',
    'paper_icon': 'PAPER',
    'exclamation_icon': '!',
    'letter_e_icon': 'E',
    'ink_drop_icon': 'DROP',
    'drop_on_paper_icon': 'DROP/PG',
}
# Orange LEDs next to buttons, driven through the shift register (active-low bits 8, 9, 11).
FRAME_LEDS: tuple[str, ...] = ('info_led', 'resume_led', 'ink_led')
BADGE_NAMES: tuple[str, ...] = tuple(BADGE_LABELS)


class FrontPanelUIController:
    """Headless-testable controller connecting ``SerialShiftPanel`` / ``Emulator`` to a UI."""

    def __init__(self, emulator=None, *, panel: SerialShiftPanel | None = None):
        self.emulator = emulator
        if panel is not None:
            self.panel = panel
        elif emulator is not None and emulator.board.panel.endpoint is not None:
            self.panel = emulator.board.panel.endpoint
        else:
            self.panel = SerialShiftPanel(0x3FFFF)

    def get_widget_state(self, frame: int | None = None) -> dict[str, object]:
        """Return structured GUI widget geometry and state for the current panel frame."""
        if frame is None and not self.panel.frames:
            frame = self.panel.encode_frame(seven_seg_char=' ')
        decoded = self.panel.decode_frame(frame)
        temporal = (
            self.panel.analyze_frames()
            if self.panel.frames
            else {
                'seven_seg_sequence': (decoded['seven_seg_char'],),
                'seven_seg_blinking': False,
                'blinking_indicators': (),
            }
        )
        active_segs = set(decoded['seven_seg_segments'])
        seg_map: dict[str, bool] = {
            seg: (seg in active_segs) for seg in ('a', 'b', 'c', 'd', 'e', 'f', 'g')
        }
        active_polygons = {
            seg: poly for seg, poly in SEVEN_SEG_POLYGONS.items() if seg_map[seg]
        }
        wifi_count = int(decoded['wifi_bars'])
        color_count = int(decoded['color_ink_bars'])
        black_count = int(decoded['black_ink_bars'])
        active_ind = set(decoded['active_indicators'])
        gpio_leds = dict(decoded['gpio_leds'])
        leds: dict[str, bool] = {
            'power_led': bool(gpio_leds.get('power_led', False)),
            'wireless_led': bool(gpio_leds.get('wireless_led', False)),
            **{name: name in active_ind for name in FRAME_LEDS},
        }
        pressed_set = set(self.panel.pressed_buttons)
        buttons = {
            name: (name in pressed_set)
            for name in self.panel.BUTTON_BITS.values()
        }

        cartridges = None
        if self.emulator is not None and getattr(self.emulator, 'carriage_asic', None) is not None:
            cartridges = self.emulator.carriage_asic.cartridge_summary()

        return {
            'frame_hex': f"{decoded['raw_output']:#018x}",
            'frame_count': len(self.panel.frames),
            'seven_segment': {
                'char': decoded['seven_seg_char'],
                'segments': seg_map,
                'active_polygons': active_polygons,
                'blinking': bool(temporal['seven_seg_blinking']),
                'sequence': list(temporal['seven_seg_sequence']),
            },
            'wifi_bars': {
                'count': wifi_count,
                'bars': [i < wifi_count for i in range(SerialShiftPanel.MAX_WIFI_BARS)],
                'icon': 'wifi_icon' in active_ind,
                'attention': 'wireless_attention_icon' in active_ind,
            },
            'color_ink': {
                'count': color_count,
                'bars': [i < color_count for i in range(3)],
                'outline': 'color_ink_outline' in active_ind,
            },
            'black_ink': {
                'count': black_count,
                'bars': [i < black_count for i in range(3)],
                'outline': 'black_ink_outline' in active_ind,
            },
            'badges': {name: name in active_ind for name in BADGE_NAMES},
            'leds': leds,
            'buttons': buttons,
            'blinking_indicators': list(temporal['blinking_indicators']),
            'cartridges': cartridges,
            'ascii_art': self.panel.render_display(frame, show_temporal=bool(self.panel.frames)),
        }

    def _stop_after(self, extra: int = 0) -> None:
        emu = self.emulator
        if emu is None:
            return
        cur = emu._native_observer.state.instructions if emu._native_observer is not None else emu.instructions
        target = cur + max(0, extra)
        emu._limit = target
        if emu._native_observer is not None:
            emu._native_observer.state.limit = target

    def _advance_emulator(self, instructions: int) -> None:
        if self.emulator is None or instructions <= 0:
            return
        from analysis.uart_selftest import step_restored
        step_restored(self.emulator, instructions)

    def wait_frame(self, *, max_chunks: int = 16, chunk_steps: int = 25_000, extra: int = 2_000) -> dict[str, object]:
        """Advance the emulator just enough for the front-panel shift register to latch a new frame."""
        if self.emulator is not None:
            total_budget = max(1, max_chunks * chunk_steps)

            def _on_frame(_frame: dict) -> None:
                self.panel.on_frame = None
                self._stop_after(extra)

            self.panel.on_frame = _on_frame
            try:
                self._advance_emulator(total_budget)
            finally:
                self.panel.on_frame = None
        return self.get_widget_state()

    def step(self, instructions: int = 50_000) -> dict[str, object]:
        self._advance_emulator(instructions)
        return self.get_widget_state()

    def send_udw(self, command: str, *, steps: int = 1_500_000) -> str:
        if self.emulator is None:
            raise RuntimeError('UDW command requires a connected Emulator instance')
        emu = self.emulator
        body = command.strip()
        root_prefixes = (
            'tps ', 'udws ', 'ludw ', 'help', 'lkup', 'anZ#O6n', 'reboot', 'vers',
            'd ', 'm ', 'func_call ', 'hello', '0x', 'i ', 'ls', 'll', 'cp ',
            'dskcpy ', 'netstat', 'semShow', 'efShow', 'taskLatency', 'uart_reset',
            'udw_tags', 'jmpp_', 'ip_',
        )
        if body.startswith(root_prefixes) or ' = ' in body:
            cmd_bytes = f'{body}\r'.encode('ascii')
        else:
            clean = body.rstrip(';')
            cmd_bytes = f'udws "{clean};"\r'.encode('ascii')
        start_len = len(emu.uart)
        prev_on_uart = emu.on_uart

        def _on_uart_byte(val: int) -> None:
            if prev_on_uart is not None:
                prev_on_uart(val)
            if (val == 0x20 and emu.uart.endswith((b'\r-> ', b'\n-> '))
                    and not emu.board.uart_rx
                    and len(emu.uart) > start_len + len(cmd_bytes)):
                emu.reason = 'uart-cmd-done'
                emu.uc.emu_stop()

        _on_uart_byte.trigger_byte = 0x13E20
        emu.on_uart = _on_uart_byte
        if emu._native_observer is not None:
            emu._native_observer._sync_uart0_fast()
        try:
            emu.board.queue_rx(cmd_bytes)
            self._advance_emulator(max(0, int(steps)))
        finally:
            emu.on_uart = prev_on_uart
            if emu._native_observer is not None:
                emu._native_observer._sync_uart0_fast()
        return bytes(emu.uart[start_len:]).decode('latin1', errors='replace')

    def press_button(self, name: str) -> dict[str, object]:
        self.panel.press_button(name)
        if self.emulator is not None and self.emulator._native_observer is not None:
            self.emulator._native_observer.sync_panel()
        return self.get_widget_state()

    def release_button(self, name: str) -> dict[str, object]:
        self.panel.release_button(name)
        if self.emulator is not None and self.emulator._native_observer is not None:
            self.emulator._native_observer.sync_panel()
        return self.get_widget_state()

    def tap_button(self, name: str, *, use_firmware_event: bool = True,
                   steps: int = 1_000_000) -> dict[str, object]:
        """Press and release a front-panel button, advancing the live firmware if attached."""
        if self.emulator is not None and use_firmware_event and name in BUTTON_EVENT_IDS:
            event_id = BUTTON_EVENT_IDS[name]
            self.send_udw(
                f'ui.send_button {event_id}, 0; ui.send_button {event_id}, 1',
                steps=steps,
            )
            self.wait_frame()
            return self.get_widget_state()
        self.press_button(name)
        if self.emulator is not None:
            self.wait_frame()
        self.release_button(name)
        if self.emulator is not None:
            self.wait_frame()
        return self.get_widget_state()

    def trigger_chord(self, name: str, *, steps: int = 400_000) -> dict[str, object]:
        """Activate a multi-button chord on the shift register and step the emulator."""
        self.panel.press_chord(name)
        if self.emulator is not None and self.emulator._native_observer is not None:
            self.emulator._native_observer.sync_panel()
        if self.emulator is not None:
            self.wait_frame()
        return self.get_widget_state()

    def configure_cartridges(self, *, installed: bool = True,
                             color_bars: int = 3,
                             black_bars: int = 3,
                             steps: int = 1_500_000) -> dict[str, object]:
        """Install or remove Color/Black ink cartridges and refresh the live firmware UI."""
        if self.emulator is None or getattr(self.emulator, 'carriage_asic', None) is None:
            raise RuntimeError('configure_cartridges requires an Emulator with CarriageAsic')
        self.emulator.carriage_asic.configure_cartridges(
            installed=installed,
            color_bars=color_bars,
            black_bars=black_bars,
        )
        self.emulator.carriage_asic.apply_to_firmware(self.emulator)
        self.send_udw('ui_lite.refresh_status', steps=steps)
        self.wait_frame()
        return self.get_widget_state()

    def set_ui_lite_state(
        self,
        *,
        seven_seg: str = ' ',
        wifi_bars: int | None = None,
        color_ink_bars: int | None = None,
        black_ink_bars: int | None = None,
        indicators: Iterable[str | int] = (),
        steps: int = 1_000_000,
    ) -> dict[str, object]:
        """Drive high-level UI states on either live firmware or standalone panel."""
        if self.emulator is not None:
            ind_map = {item: 'solid' for item in indicators} if indicators else None
            cmds = SerialShiftPanel.build_ui_lite_commands(
                seven_seg_char=seven_seg,
                wifi_bars=wifi_bars,
                color_ink_bars=color_ink_bars,
                black_ink_bars=black_ink_bars,
                indicators=ind_map,
            )
            for cmd in cmds:
                body = cmd.decode('ascii')
                if body.startswith('udws "') and body.endswith('"'):
                    body = body[6:-1]
                self.send_udw(body, steps=steps)
            self.wait_frame()
        else:
            frame = SerialShiftPanel.encode_frame(
                seven_seg_char=seven_seg,
                wifi_bars=0 if wifi_bars is None else wifi_bars,
                color_ink_bars=0 if color_ink_bars is None else color_ink_bars,
                black_ink_bars=0 if black_ink_bars is None else black_ink_bars,
                indicators=tuple(indicators),
            )
            self.panel.frames.append({
                'bit_count': 64,
                'output': frame,
                'raw_inputs': self.panel.raw_inputs,
            })
            self.panel._frame_gpio_leds.append(dict(self.panel.gpio_leds))
        return self.get_widget_state()

    def usb_enumerate_and_query(
        self,
        *,
        pjl_command: bytes | None = b'\x1b%-12345X@PJL INFO ID\r\n\x1b%-12345X',
    ) -> dict[str, object]:
        """Enumerate USB 2.0 High-Speed device on live firmware, read IEEE 1284 Device ID, and optionally send Bulk-OUT."""
        if self.emulator is None:
            raise RuntimeError('USB enumeration requires a connected Emulator instance')
        import struct
        usb = self.emulator.board.usb

        def step_ctrl(max_insns: int = 150_000, extra: int = 5_000) -> None:
            if usb.control_transfers and usb.control_transfers[-1].setup == usb.last_setup:
                self._advance_emulator(extra)
                return
            usb.on_control_transfer = lambda _xfer: self._stop_after(extra)
            try:
                self._advance_emulator(max_insns)
            finally:
                usb.on_control_transfer = None

        if not usb.attached or usb.device_address == 0:
            self._advance_emulator(15_000)
            usb.bus_reset(high_speed=True)
            self._advance_emulator(25_000)

            usb.setup_get_descriptor(1, 0, w_length=18)
            step_ctrl()
            dev_desc = usb.control_transfers[-1].response
            vid, pid = struct.unpack_from('<HH', dev_desc, 8)

            usb.setup_set_address(5)
            step_ctrl()

            usb.setup_get_descriptor(2, 0, w_length=255)
            step_ctrl()
            cfg_desc = usb.control_transfers[-1].response
            num_interfaces = cfg_desc[4] if len(cfg_desc) >= 5 else 0

            usb.setup_get_descriptor(3, 2, w_index=0x0409, w_length=255)
            step_ctrl()
            prod_str = usb.control_transfers[-1].response[2:].decode('utf-16le', errors='ignore')

            usb.setup_set_configuration(1)
            step_ctrl(extra=25_000)
        else:
            vid, pid = 0x03F0, 0x0053
            num_interfaces = 4
            prod_str = 'DeskJet 2600 series'

        usb.setup_get_device_id(interface=1, w_length=512)
        step_ctrl()
        dev_id_raw = usb.control_transfers[-1].response
        dev_id = dev_id_raw[2:].decode('latin1', errors='replace') if len(dev_id_raw) > 2 else ''

        bulk_transferred = 0
        if pjl_command:
            prev_count = len(usb.endpoint_transfers)
            usb.on_endpoint_transfer = (
                lambda xfer: self._stop_after(25_000) if xfer[0] == 8 and not usb.rx_queues[8] else None
            )
            try:
                usb.send_bulk_out(8, pjl_command)
                self._advance_emulator(600_000)
            finally:
                usb.on_endpoint_transfer = None
            bulk_rx = b''.join(data for ep, _ch, data in usb.endpoint_transfers[prev_count:] if ep == 8)
            bulk_transferred = len(bulk_rx)

        return {
            'vid': vid,
            'pid': pid,
            'address': usb.device_address,
            'product': prod_str,
            'interfaces': num_interfaces,
            'device_id': dev_id,
            'bulk_out_bytes': bulk_transferred,
        }

    def usb_send_bulk_out(self, ep: int, data: bytes, *, steps: int = 500_000) -> None:
        """Send host-to-device data on USB Bulk OUT endpoint slot `ep` and advance emulator."""
        if self.emulator is None:
            raise RuntimeError('USB operations require a connected Emulator instance')
        usb = self.emulator.board.usb
        usb.send_bulk_out(ep, data)
        self._advance_emulator(steps)

    def usb_read_bulk_in(self, ep: int, max_len: int | None = None) -> bytes:
        """Read pending device-to-host bytes from USB Bulk IN endpoint slot `ep`."""
        if self.emulator is None:
            raise RuntimeError('USB operations require a connected Emulator instance')
        return self.emulator.board.usb.read_bulk_in(ep, max_len)

    def usb_drain_bulk_in(self, ep: int) -> bytes:
        """Drain all pending device-to-host bytes from USB Bulk IN endpoint slot `ep`."""
        if self.emulator is None:
            raise RuntimeError('USB operations require a connected Emulator instance')
        return self.emulator.board.usb.drain_bulk_in(ep)

    def prepare_network_services(self) -> None:
        """Prime Wi-Fi network services, thread priorities, and ARP for HTTP / REST endpoints."""
        if self.emulator is None:
            raise RuntimeError('prepare_network_services requires a connected Emulator instance')
        if getattr(self, '_network_services_ready', False):
            return
        for t in ('tDsdOlsMgr', 'tRestIOMgmt', 'tResteSCL', 'tRestScan'):
            self.send_udw(f'tps {t} 193', steps=150_000)
        self.send_udw('tps tmDNS 240', steps=150_000)
        self.send_udw('tps tFP 240', steps=150_000)
        self._network_services_ready = True

    def get_consumables_xml(self) -> dict[str, object]:
        """Fetch and return parsed /DevMgmt/ConsumableConfigDyn.xml from the live printer."""
        return self.wifi_http_get('/DevMgmt/ConsumableConfigDyn.xml')

    def get_scanner_status_xml(self) -> dict[str, object]:
        """Fetch and return parsed /eSCL/ScannerStatus from the live printer."""
        return self.wifi_http_get('/eSCL/ScannerStatus')

    def get_scanner_capabilities_xml(self) -> dict[str, object]:
        """Fetch and return parsed /eSCL/ScannerCapabilities from the live printer."""
        return self.wifi_http_get('/eSCL/ScannerCapabilities')

    def query_scanner_status(self, *, steps: int = 800_000) -> str:
        """Query real eSCL scanner diagnostics over UDW."""
        return self.send_udw('escl.get_scan_status; escl.get_capabilities;', steps=steps)

    def trigger_copy_job(self, size: int = 0, *, steps: int = 1_000_000) -> str:
        """Trigger an internal copy job via smgr_copy.mfg_copy on the live firmware."""
        return self.send_udw(f'smgr_copy.mfg_copy {size}; js.jobs_status;', steps=steps)

    def _ensure_wifi_arp(
        self,
        host_mac: bytes = b'\x02\x00\x11\x22\x33\x44',
        host_ip: str = '192.168.223.100',
        printer_ip: str = '192.168.223.1',
    ) -> bytes:
        cached = getattr(self, '_printer_mac', None)
        if cached is not None:
            return cached
        from sdio import build_arp_request as _build_arp_request
        sdio = self.emulator.board.sdio
        card = sdio.card
        card.tx_packets.clear()
        arp_reply_box = [None]

        def _on_arp_tx(pkt: bytes) -> None:
            if pkt[12:14] == b'\x08\x06' and pkt[20:22] == b'\x00\x02':
                arp_reply_box[0] = pkt
                card.on_tx_packet = None
                self._stop_after(10_000)

        card.on_tx_packet = _on_arp_tx
        try:
            sdio.inject_ethernet_frame(_build_arp_request(host_mac, host_ip, printer_ip), ifidx=1)
            self._advance_emulator(1_000_000)
        finally:
            card.on_tx_packet = None
        if arp_reply_box[0] is None:
            raise RuntimeError('Wi-Fi ARP request to 192.168.223.1 did not receive an ARP reply')
        for _ in range(20):
            if card.f0[0x04]:
                break
            self._advance_emulator(10_000)
        self._printer_mac = arp_reply_box[0][22:28]
        return self._printer_mac

    def wifi_arp_and_ping(
        self,
        *,
        seq: int = 1,
        host_ip: str = '192.168.223.100',
        printer_ip: str = '192.168.223.1',
    ) -> dict[str, object]:
        """Resolve ARP and send an ICMP Echo Request over Wi-Fi to the live printer at 192.168.223.1."""
        if self.emulator is None:
            raise RuntimeError('Wi-Fi ping requires a connected Emulator instance')
        from sdio import build_icmp_echo as _build_icmp_echo
        host_mac = b'\x02\x00\x11\x22\x33\x44'
        printer_mac = self._ensure_wifi_arp(host_mac, host_ip, printer_ip)
        sdio = self.emulator.board.sdio
        card = sdio.card
        card.tx_packets.clear()
        icmp_reply_box = [None]

        def _on_icmp_tx(pkt: bytes) -> None:
            if pkt[12:14] == b'\x08\x00' and pkt[23] == 1 and pkt[34] == 0:
                icmp_reply_box[0] = pkt
                card.on_tx_packet = None
                self._stop_after(5_000)

        card.on_tx_packet = _on_icmp_tx
        try:
            sdio.inject_ethernet_frame(
                _build_icmp_echo(host_mac, printer_mac, host_ip, printer_ip, seq=seq),
                ifidx=1,
            )
            self._advance_emulator(750_000)
        finally:
            card.on_tx_packet = None
        reply = icmp_reply_box[0]
        if reply is None:
            raise RuntimeError('Wi-Fi ICMP echo request timed out')
        mac_str = ':'.join(f'{b:02x}' for b in printer_mac)
        return {
            'printer_ip': printer_ip,
            'printer_mac': mac_str,
            'reply_bytes': len(reply),
            'payload': reply[42:].decode('latin1', errors='replace'),
        }

    def wifi_http_get(
        self,
        path: str = '/FirmwareUpdate/FirmwareUpdateDyn.xml',
        *,
        host_ip: str = '192.168.223.100',
        printer_ip: str = '192.168.223.1',
    ) -> dict[str, object]:
        """Perform a live TCP 3-way handshake and HTTP GET over Wi-Fi against 192.168.223.1:80."""
        if self.emulator is None:
            raise RuntimeError('Wi-Fi HTTP GET requires a connected Emulator instance')
        if not getattr(self, '_http_priorities_tuned', False):
            self.send_udw(
                'tps tDsdOlsMgr 193; tps tRestIOMgmt 193; tps tResteSCL 193; tps tRestScan 193; tps tmDNS 240; tps tFP 240;',
                steps=200_000,
            )
            self._http_priorities_tuned = True

        from sdio import build_tcp_packet as _build_tcp_packet
        host_mac = b'\x02\x00\x11\x22\x33\x44'
        printer_mac = self._ensure_wifi_arp(host_mac, host_ip, printer_ip)
        sdio = self.emulator.board.sdio
        card = sdio.card
        sport = getattr(self, '_next_tcp_sport', 49152)
        self._next_tcp_sport = sport + 1
        client_seq = 0x10002000 + (sport - 49152) * 0x10000
        card.tx_packets.clear()
        syn_ack_box = [None]

        def _on_syn_ack_tx(pkt: bytes) -> None:
            if len(pkt) >= 54 and pkt[12:14] == b'\x08\x00' and pkt[23] == 6:
                ihl_l = (pkt[14] & 0x0F) * 4
                th_off = 14 + ihl_l
                dport = int.from_bytes(pkt[th_off + 2 : th_off + 4], 'big')
                if dport == sport and (pkt[th_off + 13] & 0x12) == 0x12:
                    syn_ack_box[0] = pkt
                    card.on_tx_packet = None
                    self._stop_after(5_000)

        card.on_tx_packet = _on_syn_ack_tx
        try:
            sdio.inject_ethernet_frame(
                _build_tcp_packet(host_mac, printer_mac, host_ip, printer_ip, sport, 80, client_seq, 0, 0x02),
                ifidx=1,
            )
            self._advance_emulator(1_000_000)
        finally:
            card.on_tx_packet = None
        syn_ack = syn_ack_box[0]
        if syn_ack is None:
            raise RuntimeError('Wi-Fi TCP SYN to 192.168.223.1:80 did not receive SYN-ACK')
        ihl = (syn_ack[14] & 0x0F) * 4
        srv_seq = int.from_bytes(syn_ack[14 + ihl + 4 : 14 + ihl + 8], 'big')
        client_seq += 1
        srv_next = (srv_seq + 1) & 0xFFFFFFFF

        card.tx_packets.clear()
        http_req = f'GET {path} HTTP/1.1\r\nHost: {printer_ip}\r\n\r\n'.encode('ascii')
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
        fin_sent = [False]

        def _on_http_tx(pkt: bytes) -> None:
            nonlocal client_seq
            if len(pkt) >= 54 and pkt[12:14] == b'\x08\x00' and pkt[23] == 6:
                ihl_local = (pkt[14] & 0x0F) * 4
                ip_total = int.from_bytes(pkt[16:18], 'big')
                th_off = 14 + ihl_local
                dport = int.from_bytes(pkt[th_off + 2 : th_off + 4], 'big')
                if dport != sport:
                    return
                data_off = ((pkt[th_off + 12] >> 4) & 0x0F) * 4
                p_seq = int.from_bytes(pkt[th_off + 4 : th_off + 8], 'big')
                p_flags = pkt[th_off + 13]
                payload_start = th_off + data_off
                payload_end = 14 + ip_total
                tcp_payload = pkt[payload_start:payload_end]
                if tcp_payload or (p_flags & 1):
                    if tcp_payload:
                        http_stream.extend(tcp_payload)
                    ack_num = (p_seq + len(tcp_payload) + (1 if (p_flags & 1) else 0)) & 0xFFFFFFFF
                    hdr_sep = http_stream.find(b'\r\n\r\n')
                    body_bytes = http_stream[hdr_sep + 4 :] if hdr_sep >= 0 else b''
                    body_done = (
                        body_bytes.endswith(b'\r\n0\r\n\r\n')
                        or body_bytes == b'0\r\n\r\n'
                        or (len(body_bytes) > 200 and body_bytes.endswith(b'0\r\n\r\n'))
                    )
                    out_flags = 0x10
                    if (body_done or (p_flags & 1)) and not fin_sent[0]:
                        out_flags = 0x11
                        fin_sent[0] = True
                    sdio.inject_ethernet_frame(
                        _build_tcp_packet(
                            host_mac,
                            printer_mac,
                            host_ip,
                            printer_ip,
                            sport,
                            80,
                            client_seq,
                            ack_num,
                            out_flags,
                        ),
                        ifidx=1,
                    )
                    if out_flags & 0x01:
                        client_seq = (client_seq + 1) & 0xFFFFFFFF
                    if body_done or (p_flags & 1):
                        card.on_tx_packet = None
                        self._stop_after(50_000)

        card.on_tx_packet = _on_http_tx
        try:
            self._advance_emulator(9_000_000)
        finally:
            card.on_tx_packet = None

        raw_text = http_stream.decode('latin1', errors='replace')
        headers_part, _, body_part = raw_text.partition('\r\n\r\n')
        status_line = headers_part.splitlines()[0] if headers_part else ''
        return {
            'path': path,
            'status_line': status_line,
            'headers': headers_part,
            'body': body_part,
            'total_bytes': len(http_stream),
        }

    def ensure_engine_ready(self, *, force_nomech: bool = True) -> None:
        """Perform mechanical homing and transition the print engine to the ready state (engine_ready=1)."""
        if self.emulator is None:
            raise RuntimeError('Mechanical homing requires a connected Emulator instance')
        import struct

        emu = self.emulator
        if force_nomech:
            self.send_udw('tps tWorkTask1 250; tps tSecUtils 250; smech.nomech 1; jmpp.set_pe_mode 1;', steps=500_000)

        if emu._native_observer is not None:
            emu._native_observer.synchronize(refresh=True)

        dram_mv = emu.memory._dram_mv
        dram_b = bytes(dram_mv)

        node6_addr = 0x20CFDCE0 + 6 * 0x24 + 4
        for target_ptr in (0x20CFDCE0 + 6 * 0x24, node6_addr):
            packed = struct.pack('>I', target_ptr)
            pos = 0
            while True:
                idx = dram_b.find(packed, pos)
                if idx < 0:
                    break
                struct.pack_into('>I', dram_mv, idx, 0)
                pos = idx + 4

        struct.pack_into('>I', dram_mv, 0x20CFD7B8 - 0x20000000, 1)
        struct.pack_into('>I', dram_mv, 0x20CFD7D0 - 0x20000000, 0)
        ent6_off = (0x20CFDCE0 + 6 * 0x24) - 0x20000000
        dram_mv[ent6_off : ent6_off + 0x24] = b'\x00' * 0x24

        if emu._native_observer is not None:
            emu._native_observer.synchronize(refresh=True)

    def render_print_raster(self) -> dict[str, object]:
        """Inspect and summarize raster/swath data generated by the print pipeline in DRAM."""
        if self.emulator is None:
            raise RuntimeError('Print raster inspection requires a connected Emulator instance')
        import struct
        emu = self.emulator
        if emu._native_observer is not None:
            emu._native_observer.synchronize(refresh=True)
        dram = bytes(emu.memory._dram_mv)
        vp03_run = struct.unpack_from('>I', dram, (0x21273524 + 0x04) - 0x20000000)[0]
        vp03_state = struct.unpack_from('>I', dram, (0x21273524 + 0x3C) - 0x20000000)[0]
        swp_run = struct.unpack_from('>I', dram, (0x212E76AC + 0x04) - 0x20000000)[0]
        return {
            'vp03_run': vp03_run,
            'vp03_state': vp03_state,
            'swp_run': swp_run,
            'has_raster_data': vp03_run > 0 or swp_run > 0,
        }

    def trigger_print_report(
        self,
        report_id: int = 974,
        *,
        steps: int = 4_000_000,
    ) -> dict[str, object]:
        """Trigger a print report (e.g. 974 Diagnostic Page) on live firmware and execute the real printing pipeline."""
        if self.emulator is None:
            raise RuntimeError('Print report requires a connected Emulator instance')
        import struct

        emu = self.emulator
        self.ensure_engine_ready()

        # Queue print report command and advance emulator
        out = self.send_udw(f'jm_reports.print_report {report_id}; js.jobs_status; js.job_list;', steps=steps)

        if emu._native_observer is not None:
            emu._native_observer.synchronize(refresh=True)

        dram = bytes(emu.memory._dram_mv)
        vp03_run = struct.unpack_from('>I', dram, (0x21273524 + 0x04) - 0x20000000)[0]
        vp03_state = struct.unpack_from('>I', dram, (0x21273524 + 0x3C) - 0x20000000)[0]
        swp_run = struct.unpack_from('>I', dram, (0x212E76AC + 0x04) - 0x20000000)[0]

        return {
            'report_id': report_id,
            'vp03_run': vp03_run,
            'vp03_state': vp03_state,
            'swp_run': swp_run,
            'output': out,
            'job_started': 'JM_REPORTS' in out or vp03_run > 10,
        }

    def enable_developer_shell(self) -> None:
        """Enable the firmware's built-in developer UART shell mode and register d, m, func_call, help, and lkup."""
        if self.emulator is None:
            raise RuntimeError('Developer shell requires a connected Emulator instance')
        if getattr(self, '_developer_shell_enabled', False):
            return
        import struct

        emu = self.emulator
        if emu._native_observer is not None:
            emu._native_observer.synchronize(refresh=True)

        dram_mv = emu.memory._dram_mv
        struct.pack_into('>I', dram_mv, 0x20011134 - 0x20000000, 1)
        struct.pack_into('>I', dram_mv, 0x200116A0 - 0x20000000, 0)

        if emu._native_observer is not None:
            emu._native_observer.synchronize(refresh=True)

        # Bootstrap func_call node in the UART shell symbol table at 0x20da75f4, then invoke
        # the firmware's own registration routines for d, m, func_call, help, and lkup.
        bootstrap_cmds = (
            '0x20da75f4 = 0xa03cdef3',
            '0x20da75f8 = 0xa03ce194',
            '0x20da75fc = 0xa03ce148',
            '0x20da7600 = 1',
            '0x20da7604 = 0x20da75e0',
            '0x20da71b4 = 0x20da7608',
            '0x20da71ac = 0x20da75f4',
            'func_call 0xa03ce057',
            'func_call 0xa03cdb07 0xa03cf03f 0xa03cf294 0xa03cf278',
            'func_call 0xa03cdb07 0xa03cf03f 0xa03cf2b8 0xa03cf29c',
        )
        for cmd in bootstrap_cmds:
            self.send_udw(cmd)

        self._developer_shell_enabled = True

    def run_ram_hello_world(
        self,
        message: str = 'Hello, World from RAM!',
        *,
        ram_addr: int = 0x20010000,
    ) -> dict[str, object]:
        """Write an educational Thumb 'Hello, World' routine into DDR RAM via UART and execute it."""
        if self.emulator is None:
            raise RuntimeError('RAM execution requires a connected Emulator instance')
        import struct

        self.enable_developer_shell()

        # Thumb machine code stub:
        #   +0x00: b500      push {lr}
        #   +0x02: a003      adr  r0, #12          ; r0 -> string at +0x10
        #   +0x04: 4901      ldr  r1, [pc, #4]     ; r1 = 0xa03cdab7 (shell_printf)
        #   +0x06: 4788      blx  r1
        #   +0x08: 202a      movs r0, #42          ; return 42 (0x2a)
        #   +0x0a: bd00      pop  {pc}
        #   +0x0c: a03cdab7  .word 0xa03cdab7
        code_words = [
            0xB500A003,
            0x49014788,
            0x202ABD00,
            0xA03CDAB7,
        ]

        msg_bytes = (message + '\n\x00').encode('ascii')
        while len(msg_bytes) % 4 != 0:
            msg_bytes += b'\x00'
        msg_words = [
            int.from_bytes(msg_bytes[i : i + 4], 'big')
            for i in range(0, len(msg_bytes), 4)
        ]

        name_bytes = b'hello\x00\x00\x00'
        desc_bytes = b'Run Hello from RAM\x00\x00'
        name_words = [
            int.from_bytes(name_bytes[i : i + 4], 'big')
            for i in range(0, len(name_bytes), 4)
        ]
        desc_words = [
            int.from_bytes(desc_bytes[i : i + 4], 'big')
            for i in range(0, len(desc_bytes), 4)
        ]

        all_words = code_words + msg_words + name_words + desc_words
        for idx, word in enumerate(all_words):
            addr = ram_addr + idx * 4
            self.send_udw(f'0x{addr:08x} = 0x{word:08x}')

        exec_addr = (ram_addr - 0x20000000) + 0xA0000001
        name_addr = ram_addr + (len(code_words) + len(msg_words)) * 4
        desc_addr = name_addr + len(name_words) * 4

        d_output = self.send_udw(f'd 0x{ram_addr:08x} 8 4')
        udw_dm_output = self.send_udw(f'udws "udw.dm 0x{ram_addr:08x} 4 4;"')

        if not getattr(self, '_hello_cmd_registered', False):
            self.send_udw(
                f'func_call 0xa03cdb07 0x{exec_addr:08x} 0x{name_addr:08x} 0x{desc_addr:08x}'
            )
            self._hello_cmd_registered = True

        help_output = self.send_udw('help hello')
        hello_output = self.send_udw('hello')
        func_call_output = self.send_udw(f'func_call 0x{exec_addr:08x}')

        return {
            'ram_addr': ram_addr,
            'exec_addr': exec_addr,
            'message': message,
            'd_output': d_output,
            'udw_dm_output': udw_dm_output,
            'help_output': help_output,
            'hello_output': hello_output,
            'func_call_output': func_call_output,
        }

    def upload_ram_file(
        self,
        path: str,
        data: bytes | str,
        *,
        mode: str = 'raw',
    ) -> int:
        """Upload a file to the printer's RAM filesystem (/ram) via UDW commands."""
        if isinstance(data, str):
            data_bytes = data.encode('latin1')
        else:
            data_bytes = bytes(data)

        if mode == 'raw':
            data_str = data_bytes.decode('latin1', errors='replace')
            res = self.send_udw(f'udws "ram.file_write2 {path} {len(data_bytes)} {data_str};"')
        elif mode == 'hex':
            hex_str = data_bytes.hex()
            res = self.send_udw(f'udws "ram.file_write {path} {len(data_bytes)} {hex_str};"')
        else:
            raise ValueError(f"Unknown upload mode: {mode!r} (expected 'raw' or 'hex')")
        return len(data_bytes)

    def cat_ram_file(self, path: str) -> str:
        """Read and return content of a file in the RAM filesystem via ustd.cat."""
        raw = self.send_udw(f'udws "ustd.cat {path};"')
        marker = ';udws() returns'
        if marker in raw:
            content = raw.split(marker)[0]
            lines = content.splitlines()
            if lines and 'ustd.cat' in lines[0]:
                content = '\n'.join(lines[1:])
            return content
        return raw

    def list_ram_files(self, path: str = '/ram') -> str:
        """List files in the printer filesystem via ls."""
        return self.send_udw(f'ls {path}')

    def copy_ram_file(self, src: str, dst: str) -> str:
        """Copy a file within the printer filesystem via cp."""
        return self.send_udw(f'cp {src} {dst}')


class FrontPanelTkApp:
    """Interactive Tkinter desktop window for the HP DeskJet 2620 front panel."""

    def __init__(self, controller: FrontPanelUIController):
        import tkinter as tk
        self.tk = tk
        self.controller = controller
        self.root = tk.Tk()
        self.root.title('HP DeskJet 2620 Front Panel & Live I/O Emulator')
        self.root.configure(bg='#1b1f24')
        self.root.resizable(False, False)

        # Top LCD & LED canvas
        self.canvas = tk.Canvas(
            self.root, width=560, height=230, bg='#1b1f24', highlightthickness=0
        )
        self.canvas.pack(padx=14, pady=(14, 8))

        # Button bar
        btn_frame = tk.Frame(self.root, bg='#1b1f24')
        btn_frame.pack(fill='x', padx=14, pady=4)
        for entry in BUTTON_LAYOUT:
            name = str(entry['name'])
            label = str(entry['label'])
            color = str(entry['color'])
            b = tk.Button(
                btn_frame,
                text=label,
                bg=color,
                fg='#f0f4f8',
                activebackground='#3b424c',
                activeforeground='#ffffff',
                relief='raised',
                padx=8,
                pady=6,
                command=lambda n=name: self._on_button_tap(n),
            )
            b.pack(side='left', expand=True, fill='x', padx=3)

        # Quick action & UDW console bar
        ctrl_frame = tk.Frame(self.root, bg='#1b1f24')
        ctrl_frame.pack(fill='x', padx=14, pady=4)

        tk.Button(
            ctrl_frame,
            text='Install Full Cartridges',
            bg='#235c3b',
            fg='#ffffff',
            command=self._on_install_cartridges,
        ).pack(side='left', padx=(0, 6))

        tk.Button(
            ctrl_frame,
            text='Step +250k',
            bg='#2d4561',
            fg='#ffffff',
            command=self._on_step,
        ).pack(side='left', padx=(0, 8))

        self.udw_entry = tk.Entry(
            ctrl_frame, bg='#111418', fg='#d8dee9', insertbackground='#d8dee9', width=32
        )
        self.udw_entry.insert(0, 'ui_lite.get_state')
        self.udw_entry.pack(side='left', expand=True, fill='x', padx=4)

        tk.Button(
            ctrl_frame,
            text='Send UDW',
            bg='#4c566a',
            fg='#ffffff',
            command=self._on_send_udw,
        ).pack(side='left', padx=(4, 0))

        # USB & Wi-Fi Live Bridge bar
        io_frame = tk.Frame(self.root, bg='#1b1f24')
        io_frame.pack(fill='x', padx=14, pady=(2, 6))

        tk.Button(
            io_frame,
            text='USB 2.0 Enumerate & ID',
            bg='#5e3b76',
            fg='#ffffff',
            command=self._on_usb_query,
        ).pack(side='left', expand=True, fill='x', padx=(0, 4))

        tk.Button(
            io_frame,
            text='Wi-Fi Ping 192.168.223.1',
            bg='#1d4f7a',
            fg='#ffffff',
            command=self._on_wifi_ping,
        ).pack(side='left', expand=True, fill='x', padx=4)

        tk.Button(
            io_frame,
            text='Wi-Fi HTTP GET XML',
            bg='#7a521d',
            fg='#ffffff',
            command=self._on_wifi_http,
        ).pack(side='left', expand=True, fill='x', padx=4)

        tk.Button(
            io_frame,
            text='Print Report 974',
            bg='#6b3b23',
            fg='#ffffff',
            command=self._on_print_report,
        ).pack(side='left', expand=True, fill='x', padx=4)

        tk.Button(
            io_frame,
            text='RAM Hello World',
            bg='#2b5b4e',
            fg='#ffffff',
            command=self._on_ram_hello,
        ).pack(side='left', expand=True, fill='x', padx=(4, 0))

        self.status_var = tk.StringVar(value='Ready — click Front-Panel buttons, Send UDW, USB, Wi-Fi, or Print Report.')
        tk.Label(
            self.root,
            textvariable=self.status_var,
            bg='#111418',
            fg='#a3be8c',
            anchor='w',
            justify='left',
            font=('Consolas', 9),
            padx=8,
            pady=6,
        ).pack(fill='x', padx=14, pady=(0, 14))

        self.redraw()

    def _on_button_tap(self, name: str) -> None:
        state = self.controller.tap_button(name)
        self.status_var.set(f"[Button] Tapped '{name}' -> 7-Seg={state['seven_segment']['char']!r}")
        self.redraw()

    def _on_install_cartridges(self) -> None:
        if self.controller.emulator is not None:
            self.controller.configure_cartridges(installed=True, color_bars=3, black_bars=3)
        else:
            self.controller.set_ui_lite_state(
                seven_seg='0',
                wifi_bars=5,
                color_ink_bars=3,
                black_ink_bars=3,
            )
        self.status_var.set('[Cartridges] Installed Color (3/3) and Black (3/3) ink cartridges.')
        self.redraw()

    def _on_step(self) -> None:
        self.controller.step(250_000)
        self.status_var.set('[Emulator] Stepped +250,000 ARM/Thumb instructions.')
        self.redraw()

    def _on_send_udw(self) -> None:
        cmd = self.udw_entry.get().strip()
        if cmd and self.controller.emulator is not None:
            out = self.controller.send_udw(cmd)
            clean = ' '.join(out.strip().splitlines())[:95]
            self.status_var.set(f'[UDW] {clean}')
            self.redraw()

    def _on_usb_query(self) -> None:
        if self.controller.emulator is not None:
            info = self.controller.usb_enumerate_and_query()
            self.status_var.set(
                f"[USB 2.0] VID=0x{info['vid']:04x} PID=0x{info['pid']:04x} Addr={info['address']} "
                f"{info['product']!r} | ID={info['device_id'][:42]}..."
            )
            self.redraw()

    def _on_wifi_ping(self) -> None:
        if self.controller.emulator is not None:
            info = self.controller.wifi_arp_and_ping()
            self.status_var.set(
                f"[Wi-Fi ICMP] Reply from {info['printer_ip']} ({info['printer_mac']}): "
                f"{info['reply_bytes']} bytes, payload={info['payload']!r}"
            )
            self.redraw()

    def _on_wifi_http(self) -> None:
        if self.controller.emulator is not None:
            info = self.controller.wifi_http_get()
            self.status_var.set(
                f"[Wi-Fi HTTP] {info['status_line']} ({info['total_bytes']} bytes) "
                f"from http://192.168.223.1{info['path']}"
            )
            self.redraw()

    def _on_print_report(self) -> None:
        if self.controller.emulator is not None:
            self.status_var.set('Triggering print report 974 pipeline...')
            self.root.update_idletasks()
            try:
                res = self.controller.trigger_print_report(974)
                self.status_var.set(
                    f"[Print] Report 974: VP03 run={res['vp03_run']}, "
                    f"state={res['vp03_state']}, Job started={res['job_started']}"
                )
            except Exception as ex:
                self.status_var.set(f"[Print Error] {ex}")
            self.redraw()

    def _on_ram_hello(self) -> None:
        if self.controller.emulator is not None:
            self.status_var.set('Uploading and executing Hello World in DDR RAM...')
            self.root.update_idletasks()
            try:
                res = self.controller.run_ram_hello_world()
                self.status_var.set(
                    f"[RAM Hello] Executed at 0x{res['exec_addr']:08x}: "
                    f"hello() returns 42 (0x2a)"
                )
            except Exception as ex:
                self.status_var.set(f"[RAM Error] {ex}")
            self.redraw()

    def redraw(self) -> None:
        state = self.controller.get_widget_state()
        c = self.canvas
        c.delete('all')

        # Outer bezel & backlit monochrome LCD screen
        c.create_rectangle(8, 8, 552, 222, fill='#252a32', outline='#3c4452', width=2)
        c.create_rectangle(140, 22, 534, 186, fill='#c5d1c0', outline='#5e695b', width=3)

        # Left status LEDs: Power and Wireless (GPIO), Info, Resume and Ink (shift register)
        leds = state['leds']
        led_specs = (
            ('Power LED',    leds.get('power_led', False),       '#f8f8f2', 36),
            ('Wireless LED', leds.get('wireless_led', False),    '#58a6ff', 72),
            ('Info LED',     leds.get('info_led', False),        '#ffb86c', 108),
            ('Resume LED',   leds.get('resume_led', False),      '#ffb86c', 144),
            ('Ink LED',      leds.get('ink_led', False),         '#ffb86c', 180),
        )
        for label, active, on_color, y in led_specs:
            fill = on_color if active else '#343a44'
            c.create_oval(24, y - 9, 42, y + 9, fill=fill, outline='#14171c', width=2)
            c.create_text(50, y, text=label, anchor='w', fill='#d8dee9', font=('Consolas', 10, 'bold'))

        # 7-segment display polygons (origin at x=156, y=38)
        ox, oy = 156, 38
        seg_map = state['seven_segment']['segments']
        for seg_name, poly in SEVEN_SEG_POLYGONS.items():
            pts = []
            for px, py in poly:
                pts.extend((ox + px, oy + py))
            fill = '#182018' if seg_map.get(seg_name, False) else '#b5c2b0'
            c.create_polygon(*pts, fill=fill, outline='#9aa895', width=1)

        # Wi-Fi signal bars (origin at x=265, y=46)
        wifi_bars = state['wifi_bars']['bars']
        c.create_text(265, 36, text='Wi-Fi', anchor='w', fill='#182018', font=('Consolas', 10, 'bold'))
        for idx, active in enumerate(wifi_bars):
            bx = 268 + idx * 12
            bh = 10 + idx * 8
            by = 96 - bh
            fill = '#182018' if active else '#b5c2b0'
            c.create_rectangle(bx, by, bx + 9, 96, fill=fill, outline='#7e8c79')

        # Color (C) and Black (K) ink bar gauges; the U-shaped outline is its own element
        for label, key, gx in (('COLOR (C)', 'color_ink', 355), ('BLACK (K)', 'black_ink', 445)):
            ink = state[key]
            c.create_text(gx, 36, text=label, anchor='w', fill='#182018', font=('Consolas', 10, 'bold'))
            c.create_rectangle(gx, 46, gx + 68, 118, outline='#182018' if ink['outline'] else '#b5c2b0', width=2)
            bars = ink['bars']
            for b_idx in range(3):
                # Top bar = 3rd bar, middle = 2nd, bottom = 1st
                bar_num = 2 - b_idx
                by = 52 + b_idx * 20
                fill = '#182018' if bars[bar_num] else '#b5c2b0'
                c.create_rectangle(gx + 6, by, gx + 62, by + 14, fill=fill, outline='#7e8c79')

        # Status icon badges along the bottom of the LCD
        badges = state['badges']
        badge_specs = tuple(
            (BADGE_LABELS[name], badges[name], 156 + idx * 47) for idx, name in enumerate(BADGE_NAMES)
        )
        for text, active, bx in badge_specs:
            bg = '#182018' if active else '#b5c2b0'
            fg = '#c5d1c0' if active else '#758270'
            c.create_rectangle(bx, 150, bx + 44, 174, fill=bg, outline='#6b7867')
            c.create_text(bx + 22, 162, text=text, fill=fg, font=('Consolas', 8, 'bold'))

        # Temporal / frame summary footer
        seq_str = '->'.join(repr(ch) for ch in state['seven_segment']['sequence']) or repr(state['seven_segment']['char'])
        footer = f"Frame: {state['frame_hex']} ({state['frame_count']} latched) | 7-Seg: {seq_str}"
        c.create_text(142, 204, text=footer, anchor='w', fill='#9aa5b4', font=('Consolas', 9))

    def run(self) -> None:
        self.root.mainloop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='HP DeskJet 2620 Front Panel GUI & Live I/O Bridge')
    parser.add_argument('--dump', type=Path, default=Path('dump.bin'),
                        help='16 MiB SPI flash dump (default: dump.bin)')
    parser.add_argument('--cold-boot', action='store_true',
                        help='Boot from reset vector instead of restoring Stage-22 snapshot if available')
    parser.add_argument('--boot-instructions', type=int, default=300_000,
                        help='Initial instructions to step before opening GUI (default: 300000)')
    parser.add_argument('--install-cartridges', action='store_true',
                        help='Install Color and Black cartridges after boot so printer enters IDLE')
    parser.add_argument('--headless-demo', action='store_true',
                        help='Run a headless front-panel copy workflow and print the ASCII LCD state')
    parser.add_argument('--talk-all', action='store_true',
                        help='Talk to dump.bin across all 4 interfaces (Buttons/LCD, UART/UDW, USB 2.0, and Wi-Fi ARP/ICMP/HTTP)')
    parser.add_argument('--usb-query', action='store_true',
                        help='Enumerate USB 2.0 High-Speed device, query IEEE 1284 Device ID, and send PJL Bulk-OUT')
    parser.add_argument('--wifi-ping', action='store_true',
                        help='Resolve ARP and send an ICMP Echo Request over Wi-Fi to 192.168.223.1')
    parser.add_argument('--wifi-http', type=str, metavar='PATH',
                        help='Perform a TCP 3-way handshake and HTTP GET over Wi-Fi to 192.168.223.1:80')
    parser.add_argument('--udw', type=str, metavar='CMD',
                        help='Send a UDW diagnostic command over UART to the live firmware shell')
    parser.add_argument('--scanner-status', action='store_true',
                        help='Query live eSCL scanner diagnostics and capabilities via UDW')
    parser.add_argument('--copy-job', type=int, nargs='?', const=0, default=None, metavar='SIZE',
                        help='Trigger a copy job via smgr_copy.mfg_copy on the live firmware (0=Letter, 1=A4)')
    parser.add_argument('--consumables-xml', action='store_true',
                        help='Fetch and display /DevMgmt/ConsumableConfigDyn.xml over Wi-Fi HTTP')
    parser.add_argument('--print-report', type=int, nargs='?', const=974, default=None,
                        help='Trigger diagnostic print report (default: 974) and execute the real firmware printing pipeline')
    parser.add_argument('--run-ram-hello', type=str, nargs='?', const='Hello, World from RAM!', default=None,
                        metavar='MESSAGE',
                        help='Write and execute an educational Thumb Hello-World routine in DDR RAM via UART shell commands')
    parser.add_argument('--upload-ram', nargs=2, metavar=('PATH', 'TEXT'),
                        help='Upload text to a file in the /ram filesystem')
    parser.add_argument('--cat-ram', type=str, metavar='PATH',
                        help='Read and print content of a file in the /ram filesystem via ustd.cat')
    parser.add_argument('--list-ram', type=str, nargs='?', const='/ram', default=None, metavar='PATH',
                        help='List files in printer filesystem via ls (default: /ram)')
    args = parser.parse_args(argv)

    from analysis.uart_selftest import build_and_restore, load_default_dump_and_snapshot
    from emulator import Emulator
    from i2c import EEPROM

    loaded = None if args.cold_boot else load_default_dump_and_snapshot()
    if loaded is not None:
        dump, snap = loaded
        emu = build_and_restore(dump, snap, native_observer=True)
        emu.board.uart_frame_ticks = 1
    else:
        dump = args.dump.read_bytes()
        emu = Emulator(
            dump,
            eeprom=EEPROM.create_factory_image(),
            power_button_wakeup=True,
            gpio_inputs={1: 0, 2: 0, 3: 0, 8: 1, 11: 0, 15: 1},
            serial_panel_inputs=0x3FFFF,
            serial_panel_tail_bit=1,
            spi_profile='byte-final',
            native_observer=True,
        )
    controller = FrontPanelUIController(emu)

    if args.talk_all:
        # 1. USB 2.0 High-Speed enumeration + IEEE 1284 Device ID + PJL Bulk-OUT
        usb_info = controller.usb_enumerate_and_query()
        print('=== [1/4] USB 2.0 High-Speed Interface ===')
        print(f"  Device:      VID=0x{usb_info['vid']:04x} PID=0x{usb_info['pid']:04x} ({usb_info['product']}) @ Address {usb_info['address']}")
        print(f"  Interfaces:  {usb_info['interfaces']} configured")
        print(f"  IEEE 1284:   {usb_info['device_id'][:110]}...")
        print(f"  EP8 Bulk-OUT:{usb_info['bulk_out_bytes']} bytes PJL command transferred via DMA\n")

        # 2. Wi-Fi SoftAP (ARP + ICMP Ping + TCP/HTTP GET XML)
        ping_info = controller.wifi_arp_and_ping()
        http_info = controller.wifi_http_get('/FirmwareUpdate/FirmwareUpdateDyn.xml')
        print('=== [2/4] Wi-Fi SoftAP (192.168.223.1 / HP-Setup<17>DeskJet 2600 series) ===')
        print(f"  ARP & ICMP:  Reply from {ping_info['printer_ip']} ({ping_info['printer_mac']}), {ping_info['reply_bytes']} bytes, payload={ping_info['payload']!r}")
        print(f"  TCP/HTTP:    {http_info['status_line']} ({http_info['total_bytes']} bytes) for GET {http_info['path']}")
        xml_preview = ' '.join(line.strip() for line in http_info['body'].splitlines() if line.strip())[:110]
        print(f"  XML Body:    {xml_preview}...\n")

        # 3. UART / UDW Interactive Shell
        udw_out = controller.send_udw('nca.get_dot11ap_config')
        print('=== [3/4] UART / UDW Shell (udws "nca.get_dot11ap_config;") ===')
        for line in udw_out.strip().splitlines():
            print(f'  {line}')
        print()

        # 4. Front-Panel Buttons, Cartridges & 7-Segment LCD
        controller.configure_cartridges(installed=True, color_bars=3, black_bars=2)
        controller.tap_button('copy_color')
        controller.tap_button('copy_color')
        state = controller.get_widget_state()
        print('=== [4/4] Front-Panel Buttons & 7-Segment LCD ===')
        print(state['ascii_art'])
        return 0

    if args.usb_query:
        info = controller.usb_enumerate_and_query()
        print(f"USB 2.0 Device: VID=0x{info['vid']:04x} PID=0x{info['pid']:04x} Product={info['product']!r} Addr={info['address']} Interfaces={info['interfaces']}")
        print(f"IEEE 1284 ID:   {info['device_id']}")
        print(f"EP8 Bulk-OUT:   {info['bulk_out_bytes']} bytes")
        return 0

    if args.wifi_ping:
        info = controller.wifi_arp_and_ping()
        print(f"Wi-Fi ARP+ICMP Reply from {info['printer_ip']} ({info['printer_mac']}): {info['reply_bytes']} bytes, payload={info['payload']!r}")
        return 0

    if args.wifi_http:
        info = controller.wifi_http_get(args.wifi_http)
        print(info['headers'])
        print()
        print(info['body'])
        return 0

    if args.udw:
        print(controller.send_udw(args.udw))
        return 0

    if args.scanner_status:
        diag = controller.query_scanner_status()
        print('=== Live Scanner Diagnostics (eSCL) ===')
        for line in diag.strip().splitlines():
            print(f'  {line}')
        return 0

    if args.copy_job is not None:
        out = controller.trigger_copy_job(args.copy_job)
        print(f'=== Live Copy Job (Size {args.copy_job}) ===')
        for line in out.strip().splitlines():
            print(f'  {line}')
        return 0

    if args.consumables_xml:
        controller.prepare_network_services()
        res = controller.get_consumables_xml()
        print(f"=== Consumables XML ({res['status_line']}, {res['total_bytes']} bytes) ===")
        print(res['body'])
        return 0

    if args.print_report is not None:
        report_id = args.print_report
        info = controller.trigger_print_report(report_id)
        print(f"=== Print Report {report_id} ===")
        print(f"  VP03 Run Count:  {info['vp03_run']}")
        print(f"  VP03 State:      {info['vp03_state']}")
        print(f"  tSweepMgr Runs:  {info['swp_run']}")
        print(f"  Job Started:     {info['job_started']}\n")
        print("Firmware output:")
        for line in info['output'].strip().splitlines():
            print(f"  {line}")
        return 0

    if args.run_ram_hello is not None:
        info = controller.run_ram_hello_world(args.run_ram_hello)
        print(f"=== Educational RAM Execution Demo (0x{info['ram_addr']:08x} -> 0x{info['exec_addr']:08x}) ===")
        print("\n[1] Display Memory (`d`):")
        for line in info['d_output'].strip().splitlines():
            print(f"  {line}")
        print("\n[2] UDW Dump Memory (`udw.dm`):")
        for line in info['udw_dm_output'].strip().splitlines():
            print(f"  {line}")
        print("\n[3] Registered Shell Command (`help hello`):")
        for line in info['help_output'].strip().splitlines():
            print(f"  {line}")
        print("\n[4] Execute via Shell Command (`hello`):")
        for line in info['hello_output'].strip().splitlines():
            print(f"  {line}")
        print("\n[5] Execute via `func_call`:")
        for line in info['func_call_output'].strip().splitlines():
            print(f"  {line}")
        return 0

    if args.upload_ram is not None:
        path, text = args.upload_ram
        nbytes = controller.upload_ram_file(path, text)
        print(f"Uploaded {nbytes} bytes to {path}")
        return 0

    if args.cat_ram is not None:
        content = controller.cat_ram_file(args.cat_ram)
        print(content)
        return 0

    if args.list_ram is not None:
        out = controller.list_ram_files(args.list_ram)
        print(out.strip())
        return 0

    if loaded is not None:
        controller.wait_frame()
    elif args.boot_instructions > 0:
        controller.step(args.boot_instructions)
    if args.install_cartridges or args.headless_demo:
        controller.configure_cartridges(installed=True, color_bars=3, black_bars=2)

    if args.headless_demo:
        controller.tap_button('copy_color')
        controller.tap_button('copy_color')
        state = controller.get_widget_state()
        print(state['ascii_art'])
        return 0

    app = FrontPanelTkApp(controller)
    app.run()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

