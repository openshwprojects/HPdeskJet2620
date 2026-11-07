"""AXON SDHCI Host Controller (0xBD0B0000) and Broadcom BCM43143 SDIO Wi-Fi Card.

Inferred from the HP DeskJet 2620 firmware (`sd_ctrl.cpp`, `ultra_spectra`, and
Broadcom `dhdsdio` / `bcmsdh` / `siutils` / `aiutils` / `dhd_cdc` drivers at
`0xA03BF7A8..0xA046B000`):

- Standard SDHCI register layout at `0xBD0B0000..0xBD0B00FF`, routed to AXON
  interrupt controller source `63` (`0x3F`).
- Broadcom `BCM43143` (`0x02D0:0xA887`, ChipID `0x1602A887`, rev 2) SDIO WLAN
  card with:
  - Function 0: CCCR, FBR1, FBR2, CIS0 (`0x1000`), CIS1 (`0x1100`), CIS2 (`0x1200`).
  - Function 1: SDIO control registers (`0x10000..0x1001F`), 32 KB sliding
    SiliconBackplane/AMBA AXI window (`0x00000..0x0FFFF`), ChipCommon (`0x18000000`),
    AXI EROM discovery table (`0x18001000`), SDIOD core (`0x18002000`), ARMCM3 core
    (`0x18003000`), SOCRAM core (`0x18004000`), D11 MAC core (`0x18005000`), AI
    wrappers (`0x18100000..0x18105000`), and 512 KB SOCRAM (`0x00000000..0x0007FFFF`)
    for firmware (`rtecdc_43143.bin`) and NVRAM upload/verification.
  - Function 2: SDPCM + BCDC packet/ioctl framing for `WLC_GET_VAR` / `WLC_SET_VAR`
    (`cur_etheraddr`, `ver`, `event_msgs`, etc.) and `WLC_GET_REVINFO`.
"""
from __future__ import annotations

from collections import deque
import socket
import struct
from typing import Callable


SDIO_BASE = 0xBD0B0000
SDIO_IRQ = 63


class SDIOError(RuntimeError):
    """An access to the SDIO host controller used an unsupported address or width."""


class Bcm43143SdioCard:
    """Stateful Broadcom BCM43143 (0x02D0:0xA887) SDIO 2.0 High-Speed WLAN device."""

    # Broadcom WLC_E_* async event codes (encapsulated in 0x886C ETHER_TYPE_BRCM frames)
    WLC_E_SET_SSID = 15
    WLC_E_LINK = 16
    WLC_E_DEAUTH_IND = 6
    WLC_E_DISASSOC_IND = 12
    WLC_E_SCAN_COMPLETE = 26
    WLC_E_ESCAN_RESULT = 69

    # Standard 802.11 Information Elements for emulated scan APs
    WPA2_RSN_IE = bytes([
        0x30, 0x14, 0x01, 0x00, 0x00, 0x0F, 0xAC, 0x04,
        0x01, 0x00, 0x00, 0x0F, 0xAC, 0x04, 0x01, 0x00,
        0x00, 0x0F, 0xAC, 0x02, 0x00, 0x00,
    ])
    OPEN_RATES_IE = b"\x01\x04\x82\x84\x8b\x96"

    # AMBA AXI EROM table at 0x18001000 for BCM43143 (5 cores + EROM_END)
    EROM_WORDS: tuple[int, ...] = (
        # 1. ChipCommon (0x800, rev 40) @ 0x18000000, wrapper @ 0x18100000
        0x4BF80001, 0x28004201, 0x18000005, 0x181000C5,
        # 2. SDIOD Device Core (0x829, rev 15) @ 0x18002000, wrapper @ 0x18102000
        0x4BF82901, 0x0F004201, 0x18002005, 0x181020C5,
        # 3. ARM Cortex-M3 Core (0x82A, rev 5) @ 0x18003000, wrapper @ 0x18103000
        0x4BF82A01, 0x05004201, 0x18003005, 0x181030C5,
        # 4. SOCRAM Core (0x80E, rev 16) @ 0x18004000, wrapper @ 0x18104000
        0x4BF80E01, 0x10004201, 0x18004005, 0x181040C5,
        # 5. 802.11 D11 MAC Core (0x812, rev 41) @ 0x18005000, wrapper @ 0x18105000
        0x4BF81201, 0x29004201, 0x18005005, 0x181050C5,
        # EROM_END
        0x0000000F,
    )

    # profile="printer": what the owner's printer reported to the custom
    # firmware (analysis/wifi.md): chip ID 0x1702A887, six Broadcom cores at
    # these addresses, 7 SOCRAM banks (448 KiB) and the 6.20.198.22 version
    # string. The enumeration ROM is served at EROM_ADDR_PRINTER.
    EROM_ADDR_PRINTER = 0x1810F000
    EROM_WORDS_PRINTER: tuple[int, ...] = (
        0x4BF80001, 0x2B004201, 0x18000005, 0x181000C5,   # ChipCommon 0x800 rev 43
        0x4BF81201, 0x25004201, 0x18001005, 0x181010C5,   # D11 MAC 0x812 rev 37
        0x4BF82901, 0x0F004201, 0x18002005, 0x181020C5,   # SDIO device 0x829 rev 15
        0x4BF82A01, 0x07004201, 0x18003005, 0x181030C5,   # ARM Cortex-M3 0x82A rev 7
        0x4BF80E01, 0x14004201, 0x18004005, 0x181040C5,   # SOCRAM 0x80E rev 20
        0x4BF81A01, 0x11004201, 0x18005005, 0x181050C5,   # USB device 0x81A rev 17
        0x0000000F,
    )
    VER_PRINTER = b"wl0: Mar 17 2016 15:56:01 version 6.20.198.22 (r625840) FWID 01-0\n\x00"
    VER_HP = (
        b"43143b0-roml/sdio-ag-p2p-pno-aoe-pktfilter-keepalive-sr-"
        b"mchan-proptxstatus Version: 6.10.190.49\x00"
    )

    _STATIC_F0: bytes | None = None
    _STATIC_F1_CTL: bytes | None = None

    def __init__(
        self,
        mac_address: bytes = b"\x30\xE1\x71\x26\x20\x01",
        *,
        on_irq_change: Callable[[], None] | None = None,
        profile: str = "hp",
    ):
        self.mac_address = bytearray(mac_address)
        self.on_irq_change = on_irq_change
        self._socram: bytearray | None = None
        if profile not in ("hp", "printer"):
            raise ValueError(f"unknown BCM43143 profile {profile!r}")
        self.profile = profile
        # Optional peer for the custom firmware's Wi-Fi link (wifi_net.VirtualWifiNetwork).
        self.network = None
        self.reset()

    def set_profile(self, profile: str) -> None:
        """Switch between HP's emulated chip ("hp") and the printer's real one ("printer")."""
        if profile not in ("hp", "printer"):
            raise ValueError(f"unknown BCM43143 profile {profile!r}")
        self.profile = profile
        self._socram = None
        self.iovars[b"ver"] = self.VER_PRINTER if profile == "printer" else self.VER_HP

    @property
    def socram(self) -> bytearray:
        if self._socram is None:
            self._socram = bytearray((448 if self.profile == "printer" else 512) * 1024)
        return self._socram

    @socram.setter
    def socram(self, value: bytearray) -> None:
        self._socram = value

    def reset(self) -> None:
        if Bcm43143SdioCard._STATIC_F0 is None:
            self.f0 = bytearray(0x2000)
            self.f1_ctl = bytearray(0x100)
            self._init_f0_and_cis()
            self._init_f1_ctl()
            Bcm43143SdioCard._STATIC_F0 = bytes(self.f0)
            Bcm43143SdioCard._STATIC_F1_CTL = bytes(self.f1_ctl)
        else:
            self.f0 = bytearray(Bcm43143SdioCard._STATIC_F0)
            self.f1_ctl = bytearray(Bcm43143SdioCard._STATIC_F1_CTL)
        # 512 KB on-chip SOCRAM (8 banks x 64 KB) for firmware & NVRAM (lazy)
        self._socram = None
        self.sb_window = 0x18000000
        self.chipctrl_addr = 0
        self.chipctrl_data: dict[int, int] = {}
        self.pmuctrl_addr = 0
        self.pmuctrl_data: dict[int, int] = {}
        self.sdiod_intstatus = 0
        self.sdiod_hostintmask = 0
        self.sdiod_mbox = 0
        self.sdiod_mboxdata = 0
        self.socram_bankidx = 0
        self.socram_bankinfo: dict[int, int] = {i: 0x00000007 for i in range(8)}
        self.wrapper_ioctrl: dict[int, int] = {i: 0x00000001 for i in range(6)}
        self.wrapper_resetctrl: dict[int, int] = {i: 0x00000000 for i in range(6)}
        self.backplane_regs: dict[int, int] = {}
        # Function 2 SDPCM / BCDC state
        self.rx_seq = 0
        self.tx_max = 64
        self.last_tx_seq = 0
        self.f2_rx_frame = bytearray()
        self.f2_rx_offset = 0
        self.f2_rx_queue: deque[tuple[int, bytes]] = deque()
        self.ioctl_count = 0
        self.last_ioctl_cmd = 0
        self.last_var_name = ""
        self.tx_packets: list[bytes] = []
        self.on_tx_packet: Callable[[bytes], None] | None = None
        self.on_ioctl: Callable[[int], None] | None = None
        # 802.11 radio, station (ifidx=0), and SoftAP (ifidx=1) state
        self.channel = 6
        self.rssi = -42
        self.noise = -90
        self.rate_500kbps = 144  # 72 Mbps
        self.associated = False
        self.associated_ssid = b"HP-Print-Lab-2.4G"
        self.associated_bssid = b"\x00\x11\x22\x33\x44\x55"
        self.uap_ssid = b"HP-Setup<17>DeskJet 2600 series"
        self.scan_aps: list[dict[str, object]] = [
            {
                "ssid": b"HP-Print-Lab-2.4G",
                "bssid": b"\x00\x11\x22\x33\x44\x55",
                "channel": 6,
                "rssi": -42,
                "noise": -90,
                "capability": 0x0411,
                "ies": self.WPA2_RSN_IE,
            },
            {
                "ssid": b"Office_Guest_WiFi",
                "bssid": b"\x66\x77\x88\x99\xAA\xBB",
                "channel": 11,
                "rssi": -65,
                "noise": -92,
                "capability": 0x0401,
                "ies": self.OPEN_RATES_IE,
            },
        ]
        self.iovars: dict[bytes, bytes] = {
            b"cur_etheraddr": bytes(self.mac_address),
            b"perm_etheraddr": bytes(self.mac_address),
            b"ver": self.VER_PRINTER if self.profile == "printer" else self.VER_HP,
            b"event_msgs": bytes(16),
        }

    def clear_scan_aps(self) -> None:
        """Remove all configured 802.11 access points from the scan table."""
        self.scan_aps.clear()

    def add_scan_ap(
        self,
        ssid: str | bytes,
        bssid: bytes = b"\x00\x11\x22\x33\x44\x55",
        *,
        channel: int = 6,
        rssi: int = -42,
        noise: int = -90,
        security: str = "wpa2",
        capability: int | None = None,
        ies: bytes | None = None,
    ) -> None:
        """Add an 802.11 access point to be returned by `iscanresults`."""
        ssid_b = ssid.encode("utf-8") if isinstance(ssid, str) else bytes(ssid)
        is_open = security.lower() in ("open", "none", "")
        if capability is None:
            capability = 0x0401 if is_open else 0x0411
        if ies is None:
            ies = self.OPEN_RATES_IE if is_open else self.WPA2_RSN_IE
        self.scan_aps.append({
            "ssid": ssid_b[:32],
            "bssid": bytes(bssid[:6]).ljust(6, b"\x00"),
            "channel": int(channel),
            "rssi": int(rssi),
            "noise": int(noise),
            "capability": int(capability),
            "ies": bytes(ies),
        })

    @classmethod
    def build_bss_info(
        cls,
        *,
        bssid: bytes,
        ssid: str | bytes,
        channel: int = 6,
        rssi: int = -42,
        noise: int = -90,
        capability: int = 0x0411,
        ies: bytes = WPA2_RSN_IE,
    ) -> bytes:
        """Build a Broadcom `wl_bss_info_t` v109 structure (`0x80 + len(ies)` bytes)."""
        ssid_b = (ssid.encode("utf-8") if isinstance(ssid, str) else bytes(ssid))[:32]
        bssid_b = bytes(bssid[:6]).ljust(6, b"\x00")
        ie_bytes = bytes(ies)
        while len(ie_bytes) & 3:
            ie_bytes += b"\x00"
        total_len = 0x80 + len(ie_bytes)
        b = bytearray(total_len)
        struct.pack_into("<II", b, 0x00, 109, total_len)
        b[0x08:0x0E] = bssid_b
        struct.pack_into("<HHB", b, 0x0E, 100, capability & 0xFFFF, len(ssid_b))
        b[0x13 : 0x13 + len(ssid_b)] = ssid_b
        struct.pack_into("<I", b, 0x34, 4)
        b[0x38:0x3C] = b"\x82\x84\x8b\x96"
        chanspec = 0x2B00 | (channel & 0xFF)
        struct.pack_into("<HHBBhbB", b, 0x48, chanspec, 0, 1, 0, int(rssi), int(noise), 1)
        struct.pack_into("<II", b, 0x74, 0x80, len(ie_bytes))
        b[0x80 : 0x80 + len(ie_bytes)] = ie_bytes
        return bytes(b)

    def build_iscan_results(self, *, status: int = 0) -> bytes:
        """Build a Broadcom `wl_iscan_results_t` buffer containing all `self.scan_aps`."""
        bss_blobs = [
            self.build_bss_info(
                bssid=ap["bssid"],          # type: ignore[arg-type]
                ssid=ap["ssid"],            # type: ignore[arg-type]
                channel=ap["channel"],      # type: ignore[arg-type]
                rssi=ap["rssi"],            # type: ignore[arg-type]
                noise=ap["noise"],          # type: ignore[arg-type]
                capability=ap["capability"],# type: ignore[arg-type]
                ies=ap["ies"],              # type: ignore[arg-type]
            )
            for ap in self.scan_aps
        ]
        all_bss = b"".join(bss_blobs)
        return struct.pack("<IIII", status, 16 + len(all_bss), 109, len(bss_blobs)) + all_bss

    def build_brcm_event(
        self,
        event_type: int,
        *,
        status: int = 0,
        reason: int = 0,
        auth_type: int = 0,
        flags: int = 0,
        bssid: bytes | None = None,
        ifname: bytes = b"eth1",
        ifidx: int = 0,
        bsscfgidx: int = 0,
        event_data: bytes = b"\x00\x00\x00\x00",
    ) -> bytes:
        """Build a Broadcom `0x886C` (`ETHER_TYPE_BRCM`) async event Ethernet frame."""
        if len(event_data) < 4:
            event_data = bytes(event_data) + b"\x00" * (4 - len(event_data))
        bssid_b = bytes((bssid if bssid is not None else self.associated_bssid)[:6]).ljust(6, b"\x00")
        eth_hdr = bytes(self.mac_address[:6]) + bssid_b + b"\x88\x6c"
        bcm_hdr = struct.pack(
            ">HHB3sH",
            0x8001,                     # BCMILCP_SUBTYPE_VENDOR_LONG
            10 + 48 + len(event_data),  # length after ethertype
            0,
            b"\x00\x10\x18",            # BRCM OUI
            0x0001,                     # BCMILCP_BCM_SUBTYPE_EVENT
        )
        ev_msg = struct.pack(
            ">HHIIIII6s16sBB",
            2,                          # wl_event_msg_t version = 2
            flags & 0xFFFF,
            event_type & 0xFFFFFFFF,
            status & 0xFFFFFFFF,
            reason & 0xFFFFFFFF,
            auth_type & 0xFFFFFFFF,
            len(event_data),
            bssid_b,
            bytes(ifname[:16]).ljust(16, b"\x00"),
            ifidx & 0xFF,
            bsscfgidx & 0xFF,
        )
        return eth_hdr + bcm_hdr + ev_msg + bytes(event_data)

    def queue_brcm_event(self, event_type: int, **kwargs: object) -> None:
        """Queue a Broadcom `0x886C` event frame to the host driver over Function 2."""
        ifidx = int(kwargs.get("ifidx", 0))  # type: ignore[arg-type]
        pkt = self.build_brcm_event(event_type, **kwargs)  # type: ignore[arg-type]
        self.queue_ethernet_frame(pkt, ifidx=ifidx)

    def connect_ap(
        self,
        ssid: str | bytes = b"HP-Print-Lab-2.4G",
        bssid: bytes = b"\x00\x11\x22\x33\x44\x55",
        *,
        channel: int = 6,
        rssi: int = -42,
    ) -> None:
        """Associate the STA interface (`ifidx=0`) with an AP and queue `WLC_E_SET_SSID`."""
        self.associated_ssid = (ssid.encode("utf-8") if isinstance(ssid, str) else bytes(ssid))[:32]
        self.associated_bssid = bytes(bssid[:6]).ljust(6, b"\x00")
        self.channel = int(channel)
        self.rssi = int(rssi)
        self.associated = True
        self.queue_brcm_event(self.WLC_E_SET_SSID, status=0, bssid=self.associated_bssid, ifidx=0)

    def disconnect_ap(self, *, reason: int = 3) -> None:
        """Disassociate the STA interface (`ifidx=0`) and queue `WLC_E_DEAUTH_IND`."""
        self.associated = False
        self.queue_brcm_event(
            self.WLC_E_DEAUTH_IND,
            status=0,
            reason=reason,
            bssid=self.associated_bssid,
            ifidx=0,
        )

    def _notify_irq(self) -> None:
        if self.on_irq_change is not None:
            self.on_irq_change()

    def _init_f0_and_cis(self) -> None:
        f0 = self.f0
        f0[0x00] = 0x32  # CCCR v1.20, SDIO v2.00
        f0[0x01] = 0x02  # SD Spec v2.00
        f0[0x02] = 0x00  # I/O Enable
        f0[0x03] = 0x00  # I/O Ready
        f0[0x04] = 0x00  # Int Enable
        f0[0x05] = 0x00  # Int Pending
        f0[0x06] = 0x00  # I/O Abort
        f0[0x07] = 0x00  # Bus Interface Control
        f0[0x08] = 0x17  # Card Capability: SDC | SMB | SRW | S4MI (LSC=0)
        # Common CIS pointer = 0x001000 (little-endian 24-bit at 0x09..0x0B)
        f0[0x09] = 0x00
        f0[0x0A] = 0x10
        f0[0x0B] = 0x00
        f0[0x10] = 0x40  # Fn0 block size = 64
        f0[0x11] = 0x00
        f0[0x12] = 0x01  # Power Control: SMPC=1
        f0[0x13] = 0x01  # High-Speed: SHS=1

        # FBR1 (0x100) and FBR2 (0x200)
        for fbr_base, iface_code, cis_ptr, blk_size in (
            (0x100, 0x07, 0x001100, 0x0040),
            (0x200, 0x02, 0x001200, 0x0200),
        ):
            f0[fbr_base] = iface_code
            f0[fbr_base + 0x09 : fbr_base + 0x0C] = cis_ptr.to_bytes(3, "little")
            f0[fbr_base + 0x10 : fbr_base + 0x12] = blk_size.to_bytes(2, "little")

        # CIS0 at 0x1000: MANFID (0x02D0:0xA887), FUNCID, FUNCE (max_blk=512, 25MHz)
        cis0 = bytes([
            0x20, 0x04, 0xD0, 0x02, 0x87, 0xA8,
            0x21, 0x02, 0x0C, 0x00,
            0x22, 0x04, 0x00, 0x00, 0x02, 0x32,
            0xFF,
        ])
        f0[0x1000 : 0x1000 + len(cis0)] = cis0

        # CIS1 at 0x1100 and CIS2 at 0x1200: FUNCID + 42-byte FUNCE v2.0
        funce_fn = bytearray(42)
        funce_fn[0] = 0x01  # Extended data 0x01
        funce_fn[1] = 0x01  # Function Info
        funce_fn[2] = 0x20  # SDIO Rev 2.0
        # Max Block Size at tuple body offset 12..13 -> 0x0200 (512 bytes)
        funce_fn[12] = 0x00
        funce_fn[13] = 0x02
        # Enable Timeout Val at tuple body offset 28..29 -> 0x000A
        funce_fn[28] = 0x0A
        funce_fn[29] = 0x00
        cis_fn = bytes([0x21, 0x02, 0x0C, 0x00, 0x22, 0x2A]) + bytes(funce_fn) + b"\xFF"
        f0[0x1100 : 0x1100 + len(cis_fn)] = cis_fn
        f0[0x1200 : 0x1200 + len(cis_fn)] = cis_fn

    def _init_f1_ctl(self) -> None:
        self.f1_ctl[0x08] = 0x08  # SBSDIO_WATERMARK
        self.f1_ctl[0x09] = 0x00  # SBSDIO_DEVICE_CTL
        self.f1_ctl[0x0A] = 0x00  # SBADDRLOW
        self.f1_ctl[0x0B] = 0x00  # SBADDRMID
        self.f1_ctl[0x0C] = 0x18  # SBADDRHIGH (default 0x18000000)
        self.f1_ctl[0x0D] = 0x00  # FRAMECTRL
        self.f1_ctl[0x0E] = 0xC0  # CHIPCLKCSR (ALP_AVAIL | HT_AVAIL)
        self.f1_ctl[0x1F] = 0x00  # SLEEPCSR

    @property
    def card_irq_pending(self) -> bool:
        return bool(self.f0[0x04] & 0x01) and bool(self.f0[0x05] & self.f0[0x04] & 0xFE)

    def read_byte(self, fn: int, addr: int) -> int:
        if fn == 0:
            if 0 <= addr < len(self.f0):
                return self.f0[addr]
            return 0
        if fn == 1:
            if 0x10000 <= addr < 0x10100:
                return self.f1_ctl[addr - 0x10000]
            if addr < 0x10000:
                b = self._read_backplane_bytes(self.sb_window | (addr & 0x7FFF), 1)
                return b[0]
            return 0
        if fn == 2:
            return 0
        return 0

    def write_byte(self, fn: int, addr: int, val: int) -> None:
        val &= 0xFF
        if fn == 0:
            if addr == 0x02:
                self.f0[0x02] = val
                self.f0[0x03] = val  # I/O Ready mirrors I/O Enable immediately
            elif addr == 0x06:
                self.f0[0x06] = 0    # I/O Abort self-clears
                self.f2_rx_frame.clear()
                self.f2_rx_offset = 0
                self.f2_rx_queue.clear()
                self.f0[0x05] = 0
                self.sdiod_intstatus &= ~0x00800040
                self._notify_irq()
            elif addr == 0x13:
                self.f0[0x13] = (val & 0xFE) | 0x01  # Preserve SHS=1
            elif 0 <= addr < len(self.f0):
                self.f0[addr] = val
                if addr in (0x04, 0x05):
                    self._notify_irq()
            return
        if fn == 1:
            if 0x10000 <= addr < 0x10100:
                off = addr - 0x10000
                if off in (0x0A, 0x0B, 0x0C):
                    if off == 0x0A:
                        val &= 0x80
                    self.f1_ctl[off] = val
                    self.sb_window = (
                        (self.f1_ctl[0x0C] << 24)
                        | (self.f1_ctl[0x0B] << 16)
                        | ((self.f1_ctl[0x0A] & 0x80) << 8)
                    )
                elif off == 0x0D:
                    # SBSDIO_FUNC1_FRAMECTRL: RF_TERM (1) / WF_TERM (2) self-clear
                    self.f1_ctl[off] = 0
                    for c in range(0x19, 0x1E):
                        self.f1_ctl[c] = 0
                elif off == 0x0E:
                    # SBSDIO_FUNC1_CHIPCLKCSR: clock request -> ALP_AVAIL | HT_AVAIL
                    self.f1_ctl[off] = 0x00 if val == 0 else ((val & 0x3F) | 0xC0)
                elif off == 0x1F:
                    self.f1_ctl[off] = 0x03 if (val & 1) else 0x00
                else:
                    self.f1_ctl[off] = val
            elif addr < 0x10000:
                self._write_backplane_bytes(self.sb_window | (addr & 0x7FFF), bytes([val]))

    def _read_backplane_word(self, sb_addr: int) -> int:
        sb_addr &= ~3
        if 0 <= sb_addr < len(self.socram):
            return int.from_bytes(self.socram[sb_addr : sb_addr + 4], "little")
        printer = self.profile == "printer"
        # ChipCommon (0x18000000..0x18000FFF)
        if sb_addr == 0x18000000:
            # ChipID: BCM43143 (0xA887), rev 2, AXI; core count 6 (HP model) or 7 (printer)
            return 0x1702A887 if printer else 0x1602A887
        if sb_addr == 0x18000004:
            return 0x10080000  # Capabilities: CC_CAP_PMU
        if sb_addr == 0x180000FC:
            return self.EROM_ADDR_PRINTER if printer else 0x18001000  # EROM pointer
        if printer and self.EROM_ADDR_PRINTER <= sb_addr < self.EROM_ADDR_PRINTER + 0x100:
            idx = (sb_addr - self.EROM_ADDR_PRINTER) // 4
            return self.EROM_WORDS_PRINTER[idx] if idx < len(self.EROM_WORDS_PRINTER) else 0x0000000F
        if sb_addr == 0x18000600:
            return self.backplane_regs.get(sb_addr, 0x00000000)
        if sb_addr == 0x18000604:
            return 0x00000011  # PMUCapabilities: pmurev = 17 (matches 0xA8870011)
        if sb_addr == 0x18000608:
            return 0x00000003  # PMUStatus:alp/ht ready
        if sb_addr == 0x1800064C:
            return 0x00000000  # Save/Restore capability disabled
        if sb_addr == 0x18000650:
            return self.chipctrl_addr
        if sb_addr == 0x18000654:
            return self.chipctrl_data.get(self.chipctrl_addr, 0)
        if sb_addr == 0x18000658:
            return self.pmuctrl_addr
        if sb_addr == 0x1800065C:
            return self.pmuctrl_data.get(self.pmuctrl_addr, 0)
        # AXI EROM table (0x18001000..0x180010FF)
        if not printer and 0x18001000 <= sb_addr < 0x18001100:
            idx = (sb_addr - 0x18001000) // 4
            if idx < len(self.EROM_WORDS):
                return self.EROM_WORDS[idx]
            return 0x0000000F
        # SDIOD Core (0x18002000..0x18002FFF)
        if sb_addr == 0x18002020:
            return self.sdiod_intstatus
        if sb_addr == 0x18002024:
            return self.sdiod_hostintmask
        if sb_addr == 0x18002040:
            return self.sdiod_mbox
        if sb_addr == 0x18002044:
            return 0x00000000
        if sb_addr == 0x18002048:
            return self.sdiod_mboxdata
        if sb_addr == 0x1800204C:
            return 0x00040002  # FW ready / protocol version
        # SOCRAM Core (0x18004000..0x18004FFF)
        if sb_addr == 0x18004000:
            return 0x00000070 if printer else 0x00000080  # coreinfo: 7 or 8 banks of 64 KiB (n << 4)
        if sb_addr == 0x18004010:
            return self.socram_bankidx
        if sb_addr == 0x18004040:
            return self.socram_bankinfo.get(self.socram_bankidx & 0xFF, 0x00000007)
        # AI Wrappers (0x18100000..0x18105FFF)
        if 0x18100000 <= sb_addr < 0x18106000:
            core_idx = (sb_addr - 0x18100000) >> 12
            reg_off = sb_addr & 0xFFF
            if reg_off == 0x408:
                return self.wrapper_ioctrl.get(core_idx, 1)
            if reg_off == 0x500:
                return 0x00000000  # io_status
            if reg_off == 0x800:
                return self.wrapper_resetctrl.get(core_idx, 0)
            if reg_off == 0x804:
                return 0x00000000  # resetstatus
        return self.backplane_regs.get(sb_addr, 0)

    def _write_backplane_word(self, sb_addr: int, val: int) -> None:
        sb_addr &= ~3
        val &= 0xFFFFFFFF
        if 0 <= sb_addr < len(self.socram):
            self.socram[sb_addr : sb_addr + 4] = val.to_bytes(4, "little")
            return
        if sb_addr == 0x18000650:
            self.chipctrl_addr = val
            return
        if sb_addr == 0x18000654:
            self.chipctrl_data[self.chipctrl_addr] = val
            return
        if sb_addr == 0x18000658:
            self.pmuctrl_addr = val
            return
        if sb_addr == 0x1800065C:
            self.pmuctrl_data[self.pmuctrl_addr] = val
            return
        if sb_addr == 0x18002020:
            # SDIOD intstatus is Write-1-to-Clear (W1C)
            self.sdiod_intstatus &= ~val
            if not (self.sdiod_intstatus & 0x000000F0) and not self.f2_rx_frame and not self.f2_rx_queue:
                self.f0[0x05] = 0
                self._notify_irq()
            return
        if sb_addr == 0x18002024:
            self.sdiod_hostintmask = val
            return
        if sb_addr == 0x18002040:
            self.sdiod_mbox = val
            if val & 0x08:  # SMB_INT_ACK
                self.sdiod_intstatus &= ~0x000000F0
            return
        if sb_addr == 0x18002048:
            self.sdiod_mboxdata = val
            return
        if sb_addr == 0x18004010:
            self.socram_bankidx = val
            return
        if sb_addr == 0x18004040:
            self.socram_bankinfo[self.socram_bankidx & 0xFF] = val
            return
        if 0x18100000 <= sb_addr < 0x18106000:
            core_idx = (sb_addr - 0x18100000) >> 12
            reg_off = sb_addr & 0xFFF
            if reg_off == 0x408:
                self.wrapper_ioctrl[core_idx] = val
                return
            if reg_off == 0x800:
                self.wrapper_resetctrl[core_idx] = val
                return
        self.backplane_regs[sb_addr] = val

    def _read_backplane_bytes(self, sb_addr: int, length: int) -> bytes:
        if 0 <= sb_addr and sb_addr + length <= len(self.socram):
            return bytes(self.socram[sb_addr : sb_addr + length])
        out = bytearray(length)
        pos = 0
        while pos < length:
            cur = sb_addr + pos
            word = self._read_backplane_word(cur)
            wbytes = word.to_bytes(4, "little")
            byte_off = cur & 3
            chunk = min(length - pos, 4 - byte_off)
            out[pos : pos + chunk] = wbytes[byte_off : byte_off + chunk]
            pos += chunk
        return bytes(out)

    def _write_backplane_bytes(self, sb_addr: int, data: bytes) -> None:
        length = len(data)
        if 0 <= sb_addr and sb_addr + length <= len(self.socram):
            self.socram[sb_addr : sb_addr + length] = data
            return
        pos = 0
        while pos < length:
            cur = sb_addr + pos
            byte_off = cur & 3
            chunk = min(length - pos, 4 - byte_off)
            if byte_off == 0 and chunk == 4:
                val = int.from_bytes(data[pos : pos + 4], "little")
                self._write_backplane_word(cur, val)
            else:
                word = self._read_backplane_word(cur)
                wbytes = bytearray(word.to_bytes(4, "little"))
                wbytes[byte_off : byte_off + chunk] = data[pos : pos + chunk]
                self._write_backplane_word(cur, int.from_bytes(wbytes, "little"))
            pos += chunk

    def read_extended(self, fn: int, addr: int, length: int, incr: bool) -> bytes:
        if fn == 0:
            if incr and 0 <= addr and addr + length <= len(self.f0):
                return bytes(self.f0[addr : addr + length])
            return bytes(self.read_byte(0, addr + (i if incr else 0)) for i in range(length))
        if fn == 1:
            sb_addr = self.sb_window | (addr & 0x7FFF)
            if incr:
                return self._read_backplane_bytes(sb_addr, length)
            word = self._read_backplane_bytes(sb_addr, 4)
            return (word * ((length + 3) // 4))[:length]
        if fn == 2:
            return self._read_f2_frame(length)
        return bytes(length)

    def write_extended(self, fn: int, addr: int, data: bytes, incr: bool) -> None:
        if fn == 0:
            for i, b in enumerate(data):
                self.write_byte(0, addr + (i if incr else 0), b)
            return
        if fn == 1:
            sb_addr = self.sb_window | (addr & 0x7FFF)
            if incr:
                self._write_backplane_bytes(sb_addr, data)
            else:
                for off in range(0, len(data), 4):
                    self._write_backplane_bytes(sb_addr, data[off : off + 4])
            return
        if fn == 2:
            self._write_f2_frame(data)

    def _pack_sdpcm(self, payload: bytes, channel: int) -> bytearray:
        total_len = 12 + len(payload)
        cksum = (~total_len) & 0xFFFF
        sdpcm_hdr = struct.pack(
            "<HHBBBBBBBB",
            total_len,
            cksum,
            self.rx_seq,
            channel & 0x0F,
            0,                  # next_len
            12,                 # header data offset
            0,                  # flow control
            self.tx_max & 0xFF, # max_seq credit
            0,
            0,
        )
        self.rx_seq = (self.rx_seq + 1) & 0xFF
        return bytearray(sdpcm_hdr + payload)

    def _activate_next_f2_rx(self) -> None:
        if not self.f2_rx_frame and self.f2_rx_queue:
            chan, payload = self.f2_rx_queue.popleft()
            self.f2_rx_frame = self._pack_sdpcm(payload, chan)
            self.f2_rx_offset = 0
            self.sdiod_intstatus |= 0x00800040  # I_HMB_FRAME_IND | I_XMTDATA_AVAIL
            self.f0[0x05] = 0x06                # Fn1 | Fn2 interrupt pending
            self._notify_irq()

    def queue_f2_frame(self, payload: bytes, *, channel: int = 2) -> None:
        """Queue an SDPCM payload on Function 2 (`channel`: 0=Control, 1=Event, 2=Data)."""
        self.f2_rx_queue.append((channel & 0x0F, bytes(payload)))
        self._activate_next_f2_rx()

    def queue_ethernet_frame(self, eth_frame: bytes, *, ifidx: int = 1) -> None:
        """Wrap a raw Ethernet II frame in a BCDC v2 header and queue on Function 2 Data channel."""
        if len(eth_frame) < 60:
            eth_frame = bytes(eth_frame) + b"\x00" * (60 - len(eth_frame))
        bdc_hdr = bytes([0x20, 0x00, ifidx & 0x0F, 0x00])
        self.queue_f2_frame(bdc_hdr + eth_frame, channel=2)

    def _read_f2_frame(self, req_len: int) -> bytes:
        if not self.f2_rx_frame and self.f2_rx_queue:
            self._activate_next_f2_rx()
        if not self.f2_rx_frame:
            self.f0[0x05] = 0
            self._notify_irq()
            return bytes(req_len)
        rem = len(self.f2_rx_frame) - self.f2_rx_offset
        if rem <= req_len:
            chunk = bytes(self.f2_rx_frame[self.f2_rx_offset :])
            self.f2_rx_frame.clear()
            self.f2_rx_offset = 0
            if self.f2_rx_queue:
                self._activate_next_f2_rx()
            else:
                self.f0[0x05] = 0
                self.sdiod_intstatus &= ~0x00800040
                self._notify_irq()
            return chunk + bytes(req_len - len(chunk))
        chunk = bytes(self.f2_rx_frame[self.f2_rx_offset : self.f2_rx_offset + req_len])
        self.f2_rx_offset += req_len
        return chunk

    def _write_f2_frame(self, data: bytes) -> None:
        if len(data) < 12:
            return
        framelen = int.from_bytes(data[0:2], "little")
        if framelen < 12 or framelen > len(data):
            return
        tx_seq = data[4]
        chan = data[5] & 0x0F
        doff = data[7]
        self.last_tx_seq = tx_seq
        self.tx_max = (tx_seq + 64) & 0xFF
        if self.f2_rx_frame and self.f2_rx_offset == 0:
            self.f2_rx_frame[9] = self.tx_max
        if chan in (1, 2) and doff >= 12 and len(data) >= doff + 4:
            bdc_data_offset = data[doff + 3] * 4
            eth_start = doff + 4 + bdc_data_offset
            if eth_start < framelen:
                pkt = bytes(data[eth_start:framelen])
                self.tx_packets.append(pkt)
                if self.on_tx_packet is not None:
                    self.on_tx_packet(pkt)
                if self.network is not None:
                    self.network.on_frame(pkt)
            return
        if chan != 0 or doff < 12 or len(data) < doff + 16:
            return
        cmd, ioc_len, flags, _status = struct.unpack_from("<IIII", data, doff)
        reqid = (flags >> 16) & 0xFFFF
        ifidx = (flags >> 12) & 0x0F
        is_set = bool(flags & 0x02)
        payload = bytes(data[doff + 16 : min(len(data), doff + 16 + ioc_len)])
        self.ioctl_count += 1
        self.last_ioctl_cmd = cmd

        if self.network is not None:
            handled = self.network.handle_ioctl(cmd, is_set, ifidx, payload, ioc_len)
            if handled is not None:
                net_payload, net_status, after = handled
                net_hdr = struct.pack("<IIII", cmd, len(net_payload), (reqid << 16) & 0xFFFF0000,
                                      net_status & 0xFFFFFFFF)
                self.queue_f2_frame(net_hdr + net_payload, channel=0)
                for action in after:
                    action()
                if self.on_ioctl is not None:
                    self.on_ioctl(cmd)
                return

        resp_payload = b""
        emit_scan_complete = False
        emit_set_ssid_event = False
        if cmd in (262, 263):  # WLC_GET_VAR (262) / WLC_SET_VAR (263)
            nul = payload.find(0)
            var_name = payload[:nul] if nul != -1 else payload
            val_data = payload[nul + 1 :] if nul != -1 else b""
            self.last_var_name = var_name.decode("latin1", errors="ignore")
            if is_set or cmd == 263:
                if var_name == b"vndr_ie":
                    if val_data.startswith(b"add\x00"):
                        self.iovars[var_name] = val_data[4:]
                    elif val_data.startswith(b"del\x00"):
                        self.iovars[var_name] = b"\x00\x00\x00\x00"
                    else:
                        self.iovars[var_name] = val_data
                elif var_name == b"iscan":
                    self.iovars[var_name] = val_data
                    emit_scan_complete = True
                elif var_name:
                    self.iovars[var_name] = val_data
                resp_payload = bytes(min(ioc_len, 16))
            else:
                if var_name == b"iscanresults":
                    raw = self.build_iscan_results(status=0)
                    resp_payload = raw[:ioc_len]
                elif var_name == b"assoc_info":
                    raw = struct.pack("<7I", len(self.WPA2_RSN_IE), 0, 0, 0, 0, 0, 0)
                    resp_payload = raw[:ioc_len]
                elif var_name == b"assoc_req_ies":
                    raw = self.WPA2_RSN_IE
                    resp_payload = raw[:ioc_len] + bytes(max(0, min(ioc_len, 64) - len(raw)))
                elif var_name == b"counters":
                    raw = struct.pack("<HH", 6, 256) + b"\x00" * 252
                    resp_payload = raw[:ioc_len]
                elif var_name in self.iovars:
                    raw = self.iovars[var_name]
                    resp_payload = raw[:ioc_len] + bytes(max(0, min(ioc_len, 64) - len(raw)))
                else:
                    resp_payload = bytes(min(max(ioc_len, 4), 64))
        elif cmd == 1:  # WLC_GET_VERSION
            resp_payload = struct.pack("<I", 12)
        elif cmd == 12:  # WLC_GET_RATE
            resp_payload = struct.pack("<i", self.rate_500kbps)
        elif cmd == 23:  # WLC_GET_BSSID
            if ifidx == 1:
                resp_payload = bytes(self.mac_address[:6])
            elif self.associated:
                resp_payload = bytes(self.associated_bssid[:6]).ljust(6, b"\x00")
            else:
                resp_payload = b"\x00" * 6
        elif cmd == 25:  # WLC_GET_SSID
            if ifidx == 1:
                ssid = bytes(self.uap_ssid[:32])
                resp_payload = struct.pack("<I32s", len(ssid), ssid)
            elif self.associated:
                ssid = bytes(self.associated_ssid[:32])
                resp_payload = struct.pack("<I32s", len(ssid), ssid)
            else:
                resp_payload = b"\x00" * 36
        elif cmd == 26:  # WLC_SET_SSID
            if len(payload) >= 4:
                ssid_len = min(int.from_bytes(payload[0:4], "little"), 32)
                ssid = bytes(payload[4 : 4 + ssid_len])
                if ifidx == 1:
                    self.uap_ssid = ssid
                elif ssid_len > 0:
                    self.associated_ssid = ssid
                    self.associated = True
                    emit_set_ssid_event = True
                else:
                    self.associated = False
            resp_payload = bytes(min(ioc_len, 36))
        elif cmd == 29:  # WLC_GET_CHANNEL (channel_info_t: hw, target, scan)
            resp_payload = struct.pack("<III", self.channel, self.channel, 0)
        elif cmd == 52:  # WLC_DISASSOC
            self.associated = False
            resp_payload = b"\x00\x00\x00\x00"
        elif cmd == 98:  # WLC_GET_REVINFO (68-byte wlc_rev_info_t)
            resp_payload = struct.pack(
                "<17I",
                0x14E4,      # vendorid
                0x43EC,      # deviceid
                0x12055000,  # radiorev
                2,           # chiprev
                41,          # corerev
                0x058E,      # boardid
                0x14E4,      # boardvendor
                0x1100,      # boardrev
                0x060A0000,  # driverrev
                0x03600000,  # ucoderev
                2,           # bus (SDIO)
                0xA887,      # chipnum (43143)
                4,           # phytype (HT)
                17,          # phyrev
                12,          # anarev
                0,           # chippkg
                1,           # nvramrev
            )
        elif cmd == 127:  # WLC_GET_RSSI
            resp_payload = struct.pack("<i", self.rssi)
        elif cmd == 135:  # WLC_GET_PHY_NOISE
            resp_payload = struct.pack("<i", self.noise)
        elif cmd == 136:  # WLC_GET_BSS_INFO
            bss = self.build_bss_info(
                bssid=self.associated_bssid,
                ssid=self.associated_ssid,
                channel=self.channel,
                rssi=self.rssi,
                noise=self.noise,
            )
            raw = struct.pack("<I", 109) + bss
            resp_payload = raw[:ioc_len] + bytes(max(0, min(ioc_len, 512) - len(raw)))
        else:
            resp_payload = bytes(min(ioc_len, 64))

        resp_flags = (reqid << 16) & 0xFFFF0000
        cdc_hdr = struct.pack("<IIII", cmd, len(resp_payload), resp_flags, 0)
        self.queue_f2_frame(cdc_hdr + resp_payload, channel=0)
        if emit_scan_complete:
            self.queue_brcm_event(self.WLC_E_SCAN_COMPLETE, status=0, ifidx=0)
        if emit_set_ssid_event:
            self.queue_brcm_event(
                self.WLC_E_SET_SSID, status=0, bssid=self.associated_bssid, ifidx=0
            )
        if self.on_ioctl is not None:
            self.on_ioctl(cmd)


class SdioHostController:
    """AXON SDHCI Host Controller at 0xBD0B0000..0xBD0B00FF (IRQ 63)."""

    _STATIC_WIDTHS: dict[int, int] = {
        SDIO_BASE + off: (4 if (off & 3) == 0 else (2 if (off & 1) == 0 else 1))
        for off in range(0x100)
    }

    def __init__(
        self,
        *,
        set_irq: Callable[[int, bool], None],
        dma_read: Callable[[int, int], bytes] | None = None,
        dma_write: Callable[[int, bytes], None] | None = None,
        card: Bcm43143SdioCard | None = None,
    ):
        self.set_irq = set_irq
        self.dma_read = dma_read
        self.dma_write = dma_write
        self.card = card if card is not None else Bcm43143SdioCard()

        self.widths: dict[int, int] = SdioHostController._STATIC_WIDTHS
        self._in_command = False
        self._observer = None
        self.reset()

    def reset(self) -> None:
        self.sdma_addr = 0
        self.block_size = 0x0200
        self.block_count = 0
        self.argument = 0
        self.transfer_mode = 0
        self.command = 0
        self.response = [0, 0, 0, 0]
        self.host_ctrl = 0
        self.clock_ctrl = 0x0002  # Internal Clock Stable
        self.int_status = 0
        self.int_status_en = 0
        self.int_signal_en = 0
        self.cmd_count = 0
        self._in_command = False
        if self.card.on_irq_change is None:
            self.card.on_irq_change = self._update_irq

    def inject_ethernet_frame(self, eth_frame: bytes, *, ifidx: int = 1) -> None:
        """Queue an incoming Ethernet II frame on the BCM43143 WLAN card and update SDIO IRQ."""
        self.card.queue_ethernet_frame(eth_frame, ifidx=ifidx)
        if self.card.on_irq_change != self._update_irq:
            self._update_irq()

    def inject_brcm_event(self, event_type: int, **kwargs: object) -> None:
        """Queue a Broadcom `0x886C` async event frame on the BCM43143 WLAN card."""
        self.card.queue_brcm_event(event_type, **kwargs)
        if self.card.on_irq_change != self._update_irq:
            self._update_irq()

    def clear_scan_aps(self) -> None:
        """Clear all configured 802.11 scan access points on the BCM43143 WLAN card."""
        self.card.clear_scan_aps()

    def add_scan_ap(self, ssid: str | bytes, bssid: bytes = b"\x00\x11\x22\x33\x44\x55", **kwargs: object) -> None:
        """Add an 802.11 access point to the BCM43143 WLAN scan results."""
        self.card.add_scan_ap(ssid, bssid, **kwargs)  # type: ignore[arg-type]

    def connect_ap(
        self,
        ssid: str | bytes = b"HP-Print-Lab-2.4G",
        bssid: bytes = b"\x00\x11\x22\x33\x44\x55",
        *,
        channel: int = 6,
        rssi: int = -42,
    ) -> None:
        """Associate the BCM43143 STA interface (`ifidx=0`) with an access point."""
        self.card.connect_ap(ssid, bssid, channel=channel, rssi=rssi)
        if self.card.on_irq_change != self._update_irq:
            self._update_irq()

    def disconnect_ap(self, *, reason: int = 3) -> None:
        """Disassociate the BCM43143 STA interface (`ifidx=0`)."""
        self.card.disconnect_ap(reason=reason)
        if self.card.on_irq_change != self._update_irq:
            self._update_irq()

    def _update_irq(self) -> None:
        if self._in_command:
            return
        obs = self._observer
        if obs is not None and not obs._synchronizing and obs.state.sdio_dirty:
            obs.flush_sdio()
        if self.card.card_irq_pending and (self.int_status_en & 0x0100):
            self.int_status |= 0x0100
        else:
            self.int_status &= ~0x0100
        active = bool(self.int_status & self.int_status_en & self.int_signal_en)
        self.set_irq(SDIO_IRQ, active)
        if obs is not None and not obs._synchronizing:
            obs.sync_sdio()
            obs.sync_irqs()

    def _read_reg32(self, offset: int) -> int:
        if offset == 0x00:
            return self.sdma_addr
        if offset == 0x04:
            return (self.block_count << 16) | 0x7000 | (self.block_size & 0x0FFF)
        if offset == 0x08:
            return self.argument
        if offset == 0x0C:
            return (self.command << 16) | self.transfer_mode
        if offset == 0x10:
            return self.response[0]
        if offset == 0x14:
            return self.response[1]
        if offset == 0x18:
            return self.response[2]
        if offset == 0x1C:
            return self.response[3]
        if offset == 0x24:
            # Present State: Card Inserted (16), Stable (17), Detect (18), WP (19), DAT[3:0] (20..23)
            return 0x01FF0000
        if offset == 0x28:
            return self.host_ctrl
        if offset == 0x2C:
            return self.clock_ctrl
        if offset == 0x30:
            self._update_irq()
            return self.int_status
        if offset == 0x34:
            return self.int_status_en
        if offset == 0x38:
            return self.int_signal_en
        if offset == 0x3C:
            return 0
        if offset == 0x40:
            # Capabilities: 3.3V/3.0V/1.8V, High-Speed, SDMA, ADMA2, 512B blocks, 48MHz base clock
            return 0x07E830B0
        if offset == 0x44:
            return 0
        if offset == 0x48:
            return 0x000000FF
        if offset == 0xFC:
            slot_int = 1 if (self.int_status & self.int_status_en & self.int_signal_en) else 0
            return 0x02000000 | slot_int
        return 0

    def _write_reg32(self, offset: int, value: int) -> None:
        value &= 0xFFFFFFFF
        if offset == 0x00:
            self.sdma_addr = value
        elif offset == 0x04:
            self.block_size = value & 0x0FFF
            self.block_count = (value >> 16) & 0xFFFF
        elif offset == 0x08:
            self.argument = value
        elif offset == 0x0C:
            self.transfer_mode = value & 0xFFFF
            self.command = (value >> 16) & 0xFFFF
            self._execute_command()
        elif offset == 0x28:
            self.host_ctrl = value
        elif offset == 0x2C:
            # Software Reset bits (24..26) self-clear
            if value & 0x01000000:  # Reset All
                self.int_status = 0
            if value & 0x02000000:  # Reset CMD
                self.int_status &= ~0x000F0001
            if value & 0x04000000:  # Reset DAT
                self.int_status &= ~0x0070000A
            clk = value & 0x00FFFFFF
            if clk & 0x0001:
                clk |= 0x0002  # Internal Clock Stable immediately
            self.clock_ctrl = clk
            self._update_irq()
        elif offset == 0x30:
            # Normal & Error Interrupt Status: Write-1-to-Clear (except bit 8 Card Interrupt)
            w1c_mask = value & ~0x00000100
            self.int_status &= ~w1c_mask
            self._update_irq()
        elif offset == 0x34:
            self.int_status_en = value
            self._update_irq()
        elif offset == 0x38:
            self.int_signal_en = value
            self._update_irq()

    def _execute_command(self) -> None:
        self.cmd_count += 1
        cmd_idx = (self.command >> 8) & 0x3F
        arg = self.argument
        self.response = [0, 0, 0, 0]
        self._in_command = True
        try:
            if cmd_idx == 0:
                # GO_IDLE_STATE
                self.int_status |= 0x0001
            elif cmd_idx == 8:
                # SEND_IF_COND
                self.response[0] = arg
                self.int_status |= 0x0001
            elif cmd_idx == 5:
                # IO_SEND_OP_COND -> Ready (bit 31), 2 I/O functions (bits 30:28 = 2), OCR = 0xFFFF00
                self.response[0] = 0xA0FFFF00
                self.int_status |= 0x0001
            elif cmd_idx == 3:
                # SEND_RELATIVE_ADDR -> RCA = 0x0001
                self.response[0] = 0x00010000
                self.int_status |= 0x0001
            elif cmd_idx == 7:
                # SELECT/DESELECT_CARD
                self.response[0] = 0x00001E00
                self.int_status |= 0x0003
            elif cmd_idx == 52:
                # IO_RW_DIRECT
                rw = (arg >> 31) & 1
                fn = (arg >> 28) & 7
                raw = (arg >> 27) & 1
                addr = (arg >> 9) & 0x1FFFF
                wdata = arg & 0xFF
                if rw:
                    self.card.write_byte(fn, addr, wdata)
                    rdata = self.card.read_byte(fn, addr) if raw else wdata
                else:
                    rdata = self.card.read_byte(fn, addr)
                self.response[0] = 0x00001000 | (rdata & 0xFF)
                self.int_status |= 0x0001
            elif cmd_idx == 53:
                # IO_RW_EXTENDED
                rw = (arg >> 31) & 1
                fn = (arg >> 28) & 7
                blk_mode = (arg >> 27) & 1
                incr = bool((arg >> 26) & 1)
                addr = (arg >> 9) & 0x1FFFF
                count = arg & 0x1FF
                if count == 0:
                    count = 512
                blk_sz = self.block_size if self.block_size > 0 else 512
                total_bytes = count * blk_sz if blk_mode else count

                if rw:
                    if self.dma_read is not None and self.sdma_addr:
                        payload = self.dma_read(self.sdma_addr, total_bytes)
                    else:
                        payload = bytes(total_bytes)
                    self.card.write_extended(fn, addr, payload, incr)
                else:
                    payload = self.card.read_extended(fn, addr, total_bytes, incr)
                    if self.dma_write is not None and self.sdma_addr:
                        self.dma_write(self.sdma_addr, payload)

                self.sdma_addr = (self.sdma_addr + total_bytes) & 0xFFFFFFFF
                self.block_count = 0
                self.response[0] = 0x00001000
                self.int_status |= 0x0003  # Command Complete | Transfer Complete
            else:
                # Unsupported commands (e.g. CMD55 SD memory probe) -> Command Timeout Error
                self.int_status |= 0x00010000
        finally:
            self._in_command = False
            self._update_irq()

    @staticmethod
    def _decode_access(address: int, size: int, op: str) -> tuple[int, int]:
        if not (SDIO_BASE <= address < SDIO_BASE + 0x100) or size not in (1, 2, 4):
            raise SDIOError(f"unmodeled SDIO {op}: {address:#010x}, size {size}")
        reg_off = (address - SDIO_BASE) & ~3
        byte_off = (address - SDIO_BASE) & 3
        if byte_off + size > 4:
            raise SDIOError(f"misaligned SDIO {op}: {address:#010x}, size {size}")
        return reg_off, byte_off

    def read(self, address: int, size: int) -> int:
        obs = self._observer
        if obs is not None and obs.state.sdio_dirty:
            obs.flush_sdio()
        reg_off, byte_off = self._decode_access(address, size, "read")
        raw = self._read_reg32(reg_off).to_bytes(4, "big")
        return int.from_bytes(raw[byte_off : byte_off + size], "big")

    def write(self, address: int, size: int, value: int) -> None:
        obs = self._observer
        if obs is not None and obs.state.sdio_dirty:
            obs.flush_sdio()
        reg_off, byte_off = self._decode_access(address, size, "write")
        if size == 4:
            self._write_reg32(reg_off, value)
            if obs is not None and not obs._synchronizing:
                obs.sync_sdio()
            return
        masked = value & ((1 << (size * 8)) - 1)
        if reg_off == 0x30:
            shift = (4 - byte_off - size) * 8
            self._write_reg32(0x30, masked << shift)
            if obs is not None and not obs._synchronizing:
                obs.sync_sdio()
            return
        cur = bytearray(self._read_reg32(reg_off).to_bytes(4, "big"))
        cur[byte_off : byte_off + size] = masked.to_bytes(size, "big")
        merged = int.from_bytes(cur, "big")
        if reg_off == 0x0C and byte_off >= 2:
            self.transfer_mode = merged & 0xFFFF
            if obs is not None and not obs._synchronizing:
                obs.sync_sdio()
            return
        self._write_reg32(reg_off, merged)
        if obs is not None and not obs._synchronizing:
            obs.sync_sdio()


def ip_checksum(data: bytes) -> int:
    """Compute the RFC 1071 16-bit one's-complement checksum."""
    if len(data) & 1:
        data += b"\x00"
    s = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return (~s) & 0xFFFF


def build_arp_request(src_mac: bytes, src_ip: str, dst_ip: str) -> bytes:
    """Build an Ethernet broadcast ARP request frame."""
    eth_hdr = b"\xff\xff\xff\xff\xff\xff" + src_mac + struct.pack("!H", 0x0806)
    arp_pkt = struct.pack(
        "!HHBBH6s4s6s4s",
        1,
        0x0800,
        6,
        4,
        1,
        src_mac,
        socket.inet_aton(src_ip),
        b"\x00" * 6,
        socket.inet_aton(dst_ip),
    )
    return eth_hdr + arp_pkt


def build_icmp_echo(
    src_mac: bytes,
    dst_mac: bytes,
    src_ip: str,
    dst_ip: str,
    seq: int = 1,
    payload: bytes = b"HPDeskJet2620Ping!",
) -> bytes:
    """Build an Ethernet IPv4 ICMP Echo Request frame."""
    icmp_hdr = struct.pack("!BBHHH", 8, 0, 0, 0x1234, seq)
    icmp_cksum = ip_checksum(icmp_hdr + payload)
    icmp_pkt = struct.pack("!BBHHH", 8, 0, icmp_cksum, 0x1234, seq) + payload
    ip_len = 20 + len(icmp_pkt)
    src_ip_b = socket.inet_aton(src_ip)
    dst_ip_b = socket.inet_aton(dst_ip)
    ip_hdr = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        ip_len,
        0x1000 + seq,
        0,
        64,
        1,
        0,
        src_ip_b,
        dst_ip_b,
    )
    ip_cksum = ip_checksum(ip_hdr)
    ip_hdr = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        ip_len,
        0x1000 + seq,
        0,
        64,
        1,
        ip_cksum,
        src_ip_b,
        dst_ip_b,
    )
    return dst_mac + src_mac + struct.pack("!H", 0x0800) + ip_hdr + icmp_pkt


def build_tcp_packet(
    src_mac: bytes,
    dst_mac: bytes,
    src_ip: str,
    dst_ip: str,
    sport: int,
    dport: int,
    seq: int,
    ack: int,
    flags: int,
    payload: bytes = b"",
) -> bytes:
    """Build an Ethernet IPv4 TCP segment frame."""
    src_ip_b = socket.inet_aton(src_ip)
    dst_ip_b = socket.inet_aton(dst_ip)
    tcp_hdr = struct.pack("!HHIIBBHHH", sport, dport, seq, ack, 5 << 4, flags, 16384, 0, 0)
    pseudo = struct.pack("!4s4sBBH", src_ip_b, dst_ip_b, 0, 6, len(tcp_hdr) + len(payload))
    tcp_cksum = ip_checksum(pseudo + tcp_hdr + payload)
    tcp_pkt = (
        struct.pack("!HHIIBBHHH", sport, dport, seq, ack, 5 << 4, flags, 16384, tcp_cksum, 0)
        + payload
    )
    ip_len = 20 + len(tcp_pkt)
    ip_hdr = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        ip_len,
        0x2000 + (seq & 0xFFF),
        0,
        64,
        6,
        0,
        src_ip_b,
        dst_ip_b,
    )
    ip_cksum = ip_checksum(ip_hdr)
    ip_hdr = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        ip_len,
        0x2000 + (seq & 0xFFF),
        0,
        64,
        6,
        ip_cksum,
        src_ip_b,
        dst_ip_b,
    )
    return dst_mac + src_mac + struct.pack("!H", 0x0800) + ip_hdr + tcp_pkt


_ip_checksum = ip_checksum
_build_arp_request = build_arp_request
_build_icmp_echo = build_icmp_echo
_build_tcp_packet = build_tcp_packet
