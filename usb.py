"""AXON USB 2.0 High-Speed Device Controller (0xBD09BC00..0xBD09FC3E).

Reverse-engineered from:
- Bootloader USB recovery stack: 0xA000EA80..0xA000F464
- Main firmware USB device driver (udw20_drv.c, mod 84): 0xA0196000..0xA0199680

Register map:
- Endpoint 0..15 blocks at 0xBD09BC00 + 0x400 * ep:
  - +0x00 (4B): Endpoint control / max packet size / type
  - +0x04 (2B): Endpoint index / direction / interface mapping
  - +0x08 (2B): Endpoint status (bit 10 = halted/stalled)
  - +0x24 (4B): DMA channel 0 RAM buffer address
  - +0x28 (4B): DMA channel 0 transfer byte length
  - +0x2C (2B): DMA channel 0 direction/control
  - +0x2E (2B): DMA channel 0 start/busy strobe (bit 0 = 1 starts DMA)
  - +0x34 (4B): DMA channel 1 RAM buffer address
  - +0x38 (4B): DMA channel 1 transfer byte length
  - +0x3C (2B): DMA channel 1 direction/control
  - +0x3E (2B): DMA channel 1 start/busy strobe
- Global controller block at 0xBD09FC00..0xBD09FC3E:
  - 0xBD09FC00 (4B): Global control (bit 19 = soft reset, bit 16 = HS connect)
  - 0xBD09FC04 (4B): Device/link status (bit 0 = VBUS/attached, bits 10..11 = speed valid,
                     bits 8..9 = speed: 0 = High-Speed 480 Mbps, 1 = Full-Speed 12 Mbps)
  - 0xBD09FC08 (2B): USB device address (0..127)
  - 0xBD09FC0E (2B): PHY / D+ pullup control (bit 0 = pullup enable)
  - 0xBD09FC10 (4B): EP0 SETUP packet bytes 0..3 (bmRequestType, bRequest, wValue_lo, wValue_hi)
  - 0xBD09FC14 (4B): EP0 SETUP packet bytes 4..7 (wIndex_lo, wIndex_hi, wLength_lo, wLength_hi)
  - 0xBD09FC1A (2B): EP0 control handshake (0x0001 = ACK/complete, 0x0003 = STALL)
  - 0xBD09FC1C (2B): Endpoint interrupt enable 1
  - 0xBD09FC1E (2B): Endpoint interrupt status 1 (W1C, IRQ 24)
  - 0xBD09FC24 (2B): Endpoint DMA interrupt enable 2
  - 0xBD09FC26 (2B): Endpoint DMA interrupt status 2 (W1C, IRQ 24)
  - 0xBD09FC28 (2B): Global USB event interrupt status (W1C, IRQ 23:
                     bit 1 = Suspend, bit 2 = Resume, bit 3 = Bus Reset, bit 8 = EP0 SETUP)
  - 0xBD09FC2A (2B): Global USB event interrupt enable (IRQ 23)

Seen on the printer (2026-11-05, fw/app/usbcon.c, analysis/usb_console.md) and
modelled here as well:
- SET_ADDRESS is latched by the hardware when EP0's status stage is
  acknowledged (0xBD09FC1A = 1); 0xBD09FC08 holds the configuration value.
- Endpoint direction: OUT when bit 15 is set (HP's "wait" mode words) or for
  HP's bulk OUT type word (bit 24 without bit 26, e.g. 0x011000C7); IN otherwise.
- An OUT DMA started before any data arrived stays armed: packets land in the
  buffer as they come (+0x24 advances, +0x28 counts down) and the transfer
  completes only when the length is used up, not on a short packet.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


class USBError(Exception):
    """Raised on invalid USB controller MMIO access."""


@dataclass(frozen=True)
class UsbControlTransfer:
    setup: bytes
    response: bytes
    stalled: bool
    device_address: int


class UsbDeviceController:
    GLOBAL_BASE = 0xBD09FC00
    EP_BASE = 0xBD09BC00
    NUM_ENDPOINTS = 16

    EVENT_SUSPEND = 0x0002
    EVENT_RESUME = 0x0004
    EVENT_BUS_RESET = 0x0008
    EVENT_EP0_SETUP = 0x0100

    _STATIC_WIDTHS: dict[int, int] | None = None
    _STATIC_INIT_BYTES: dict[int, int] | None = None

    def __init__(
        self,
        *,
        set_irq: Callable[[int, bool], None] | None = None,
        dma_read: Callable[[int, int], bytes] | None = None,
        dma_write: Callable[[int, bytes], None] | None = None,
    ) -> None:
        self._set_irq = set_irq
        self._dma_read = dma_read
        self._dma_write = dma_write

        if UsbDeviceController._STATIC_WIDTHS is None:
            w: dict[int, int] = {
                base + 2 * i: 2
                for base in (*(self.EP_BASE + 0x400 * ep for ep in range(self.NUM_ENDPOINTS)), self.GLOBAL_BASE)
                for i in range(0x20)
            }
            for ep in range(self.NUM_ENDPOINTS):
                eb = self.EP_BASE + 0x400 * ep
                for off in (0x00, 0x10, 0x14, 0x20, 0x24, 0x28, 0x30, 0x34, 0x38):
                    w[eb + off] = 4
            for off in (0x00, 0x04, 0x10, 0x14, 0x30, 0x34, 0x38):
                w[self.GLOBAL_BASE + off] = 4
            UsbDeviceController._STATIC_WIDTHS = w
            UsbDeviceController._STATIC_INIT_BYTES = {
                self.EP_BASE + 0x400 * ep + 0x04 + b: ((ep >> 8) & 0xFF if b == 0 else (ep & 0xFF))
                for ep in range(self.NUM_ENDPOINTS)
                for b in (0, 1)
            }
        self.widths: dict[int, int] = UsbDeviceController._STATIC_WIDTHS

        self._bytes: dict[int, int] = {}
        self.attached: bool = False
        self.high_speed: bool = True
        self._device_address: int = 0
        self.last_setup: bytes = b"\x00" * 8
        self.control_transfers: list[UsbControlTransfer] = []
        self.endpoint_transfers: list[tuple[int, int, bytes]] = []
        self.on_control_transfer: Callable[[UsbControlTransfer], None] | None = None
        self.on_endpoint_transfer: Callable[[tuple[int, int, bytes]], None] | None = None
        self.rx_queues: dict[int, bytearray] = {ep: bytearray() for ep in range(self.NUM_ENDPOINTS)}
        self.rx_armed: set[int] = set()            # OUT endpoints with a DMA waiting for data
        self.tx_queues: dict[int, bytearray] = {ep: bytearray() for ep in range(self.NUM_ENDPOINTS)}
        self.reset()

    def reset(self) -> None:
        self._bytes = UsbDeviceController._STATIC_INIT_BYTES.copy()  # type: ignore[union-attr]
        self.attached = False
        self.high_speed = True
        self._device_address = 0
        self.last_setup = b"\x00" * 8
        if self.control_transfers:
            self.control_transfers.clear()
        if self.endpoint_transfers:
            self.endpoint_transfers.clear()
        for q in self.rx_queues.values():
            if q:
                q.clear()
        for q in self.tx_queues.values():
            if q:
                q.clear()
        self.rx_armed.clear()
        self._update_irqs()

    def _stored(self, address: int, size: int) -> int:
        return int.from_bytes(
            bytes(self._bytes.get(address + i, 0) for i in range(size)),
            "big",
        )

    def _store_raw(self, address: int, size: int, value: int) -> None:
        mask = (1 << (size * 8)) - 1
        self._bytes.update(enumerate((value & mask).to_bytes(size, "big"), address))

    def _valid_width(self, address: int, size: int) -> bool:
        if address not in self.widths:
            return False
        if size == 2:
            return True
        if size == 4 and (address & 3) == 0:
            return True
        return False

    def _update_irqs(self) -> None:
        if self._set_irq is None:
            return
        ev_status = self._stored(0xBD09FC28, 2)
        ev_enable = self._stored(0xBD09FC2A, 2)
        self._set_irq(23, bool(ev_status & ev_enable))

        ep_st1 = self._stored(0xBD09FC1C, 2)
        ep_en1 = self._stored(0xBD09FC1E, 2)
        ep_st2 = self._stored(0xBD09FC24, 2)
        ep_en2 = self._stored(0xBD09FC26, 2)
        self._set_irq(24, bool((ep_st1 & ep_en1) or (ep_st2 & ep_en2)))

    def attach(self, *, high_speed: bool = True) -> None:
        """Attach simulated USB host VBUS and link speed."""
        self.attached = True
        self.high_speed = high_speed
        status = 0x00000401 if high_speed else 0x00000501
        self._store_raw(0xBD09FC04, 4, status)

    def detach(self) -> None:
        """Detach simulated USB host."""
        self.attached = False
        self._store_raw(0xBD09FC04, 4, 0x00000000)

    def bus_reset(self, *, high_speed: bool = True) -> None:
        """Signal a USB bus reset from the host."""
        self.attach(high_speed=high_speed)
        self._device_address = 0
        self._store_raw(0xBD09FC08, 2, 0)
        self._store_raw(0xBD09FC28, 2, self._stored(0xBD09FC28, 2) | self.EVENT_BUS_RESET)
        self._update_irqs()

    def send_setup(self, setup_packet: bytes) -> None:
        """Deliver an 8-byte USB control SETUP packet to EP0."""
        if len(setup_packet) != 8:
            raise ValueError("USB SETUP packet must be exactly 8 bytes")
        if not self.attached:
            self.attach(high_speed=self.high_speed)
        self.last_setup = bytes(setup_packet)
        self._store_raw(0xBD09FC10, 4, int.from_bytes(setup_packet[0:4], "big"))
        self._store_raw(0xBD09FC14, 4, int.from_bytes(setup_packet[4:8], "big"))
        # In interrupt-driven SIE mode (udw20_drv.c), SET_ADDRESS (0x00, 0x05)
        # is handled directly by the USB SIE hardware.
        if (
            setup_packet[0] == 0x00
            and setup_packet[1] == 0x05
            and (self._stored(0xBD09FC2A, 2) & self.EVENT_EP0_SETUP)
        ):
            self._device_address = setup_packet[2] & 0x7F
            self._store_raw(0xBD09FC08, 2, self._device_address)
            self.control_transfers.append(
                UsbControlTransfer(
                    setup=self.last_setup,
                    response=b"",
                    stalled=False,
                    device_address=self.device_address,
                )
            )
            return
        self._store_raw(0xBD09FC28, 2, self._stored(0xBD09FC28, 2) | self.EVENT_EP0_SETUP)
        self._update_irqs()

    def setup_get_descriptor(
        self,
        desc_type: int,
        desc_index: int = 0,
        *,
        w_index: int = 0,
        w_length: int = 0xFF,
    ) -> None:
        """Send a standard USB GET_DESCRIPTOR (0x80, 0x06) control request to EP0."""
        w_value = ((desc_type & 0xFF) << 8) | (desc_index & 0xFF)
        pkt = (
            bytes([0x80, 0x06])
            + (w_value & 0xFFFF).to_bytes(2, "little")
            + (w_index & 0xFFFF).to_bytes(2, "little")
            + (w_length & 0xFFFF).to_bytes(2, "little")
        )
        self.send_setup(pkt)

    def setup_set_address(self, address: int) -> None:
        """Send a standard USB SET_ADDRESS (0x00, 0x05) control request to EP0."""
        if not 0 <= address <= 127:
            raise ValueError(f"USB device address must be in 0..127, got {address}")
        self.send_setup(bytes([0x00, 0x05, address & 0x7F, 0x00, 0x00, 0x00, 0x00, 0x00]))

    def setup_set_configuration(self, config_value: int = 1) -> None:
        """Send a standard USB SET_CONFIGURATION (0x00, 0x09) control request to EP0."""
        self.send_setup(bytes([0x00, 0x09, config_value & 0xFF, 0x00, 0x00, 0x00, 0x00, 0x00]))

    def setup_get_device_id(self, *, interface: int = 1, w_length: int = 0x0200) -> None:
        """Send an IEEE 1284 Printer Class GET_DEVICE_ID (0xA1, 0x00) request to EP0."""
        pkt = (
            bytes([0xA1, 0x00, 0x00, 0x00])
            + (interface & 0xFFFF).to_bytes(2, "little")
            + (w_length & 0xFFFF).to_bytes(2, "little")
        )
        self.send_setup(pkt)

    def send_bulk_out(self, ep: int, data: bytes) -> None:
        """Queue host-to-device payload on Bulk OUT endpoint slot `ep` (1..15)."""
        if not 1 <= ep < self.NUM_ENDPOINTS:
            raise ValueError(f"Endpoint slot must be in 1..15, got {ep}")
        if not data:
            return
        self.rx_queues[ep].extend(data)
        eb = self.EP_BASE + 0x400 * ep
        if ep in self.rx_armed:
            self._feed_armed_out(ep)
            return
        self._store_raw(eb + 0x08, 2, 0x0001)
        self._store_raw(eb + 0x10, 2, min(0x07FF, len(self.rx_queues[ep])) << 2)
        self._store_raw(0xBD09FC1C, 2, self._stored(0xBD09FC1C, 2) | (1 << ep))
        self._update_irqs()

    def _is_out(self, ctrl: int) -> bool:
        return bool(ctrl & 0x8000) or (ctrl & 0x05000000) == 0x01000000

    def _feed_armed_out(self, ep: int) -> None:
        """Move queued host data into an armed OUT DMA, packet by packet; finish when full."""
        eb = self.EP_BASE + 0x400 * ep
        addr = self._stored(eb + 0x24, 4)
        left = self._stored(eb + 0x28, 4)
        q = self.rx_queues[ep]
        n = min(left, len(q))
        if n and self._dma_write is not None:
            chunk = bytes(q[:n])
            del q[:n]
            self._dma_write(addr, chunk)
            self.endpoint_transfers.append((ep, 0, chunk))
            self._store_raw(eb + 0x24, 4, addr + n)
            self._store_raw(eb + 0x28, 4, left - n)
            if self.on_endpoint_transfer is not None:
                self.on_endpoint_transfer((ep, 0, chunk))
        if self._stored(eb + 0x28, 4) == 0:
            self.rx_armed.discard(ep)
            self._store_raw(eb + 0x2E, 2, 0)
            self._store_raw(eb + 0x2C, 2, 0xFFFF)
            self._store_raw(0xBD09FC24, 2, self._stored(0xBD09FC24, 2) | (1 << ep))
        self._update_irqs()

    def read_bulk_in(self, ep: int, max_len: int | None = None) -> bytes:
        """Read pending device-to-host bytes from Bulk IN endpoint slot `ep` (1..15)."""
        if not 1 <= ep < self.NUM_ENDPOINTS:
            raise ValueError(f"Endpoint slot must be in 1..15, got {ep}")
        q = self.tx_queues[ep]
        if max_len is None or max_len >= len(q):
            res = bytes(q)
            q.clear()
            return res
        res = bytes(q[:max_len])
        del q[:max_len]
        return res

    def drain_bulk_in(self, ep: int) -> bytes:
        """Drain and return all pending device-to-host bytes from Bulk IN endpoint `ep`."""
        return self.read_bulk_in(ep)

    @property
    def pullup_enabled(self) -> bool:
        return bool(self._stored(0xBD09FC0E, 2) & 1)

    @property
    def device_address(self) -> int:
        if self._device_address:
            return self._device_address
        return self._stored(0xBD09FC08, 2) & 0x7F

    def read(self, address: int, size: int) -> int:
        if not self._valid_width(address, size):
            raise USBError(f"unmodeled USB MMIO read: {address:#010x}, size {size}")
        val = self._stored(address, size)
        if self.EP_BASE <= address < self.GLOBAL_BASE and (address - self.EP_BASE) % 0x400 == 0x04 and size == 2:
            ep = (address - self.EP_BASE) // 0x400
            val = (val & 0xFFF0) | ep
        if (address == 0xBD09FC14 and size == 4) or (address == 0xBD09FC16 and size == 2):
            # Reading the end of the SETUP buffer acknowledges the
            # pending SETUP latch before the firmware re-enables mask 0x0100.
            self._store_raw(0xBD09FC28, 2, self._stored(0xBD09FC28, 2) & ~self.EVENT_EP0_SETUP)
            self._update_irqs()
        return val

    def write(self, address: int, size: int, value: int) -> None:
        if not self._valid_width(address, size):
            raise USBError(f"unmodeled USB MMIO write: {address:#010x}, size {size}")
        mask = (1 << (size * 8)) - 1
        value &= mask

        if address == 0xBD09FC00 and size == 4:
            if value & 0x00080000:
                # Soft reset clears self-clearing bit 19, device address, and pending status
                value &= ~0x00080000
                self._device_address = 0
                self._store_raw(0xBD09FC08, 2, 0)
                self._store_raw(0xBD09FC1C, 2, 0)
                self._store_raw(0xBD09FC24, 2, 0)
                self._store_raw(0xBD09FC28, 2, 0)
            self._store_raw(address, size, value)
            self._update_irqs()
            return

        if address in (0xBD09FC1C, 0xBD09FC24, 0xBD09FC28) and size == 2:
            # Write-1-to-Clear interrupt status registers
            self._store_raw(address, 2, self._stored(address, 2) & ~value)
            self._update_irqs()
            return

        if address in (0xBD09FC1E, 0xBD09FC26, 0xBD09FC2A) and size == 2:
            self._store_raw(address, 2, value)
            if address == 0xBD09FC1E:
                for ep in range(1, self.NUM_ENDPOINTS):
                    if (value & (1 << ep)) and self.rx_queues[ep]:
                        eb = self.EP_BASE + 0x400 * ep
                        self._store_raw(eb + 0x08, 2, 0x0001)
                        self._store_raw(eb + 0x10, 2, min(0x07FF, len(self.rx_queues[ep])) << 2)
                        self._store_raw(0xBD09FC1C, 2, self._stored(0xBD09FC1C, 2) | (1 << ep))
            self._update_irqs()
            return

        if address == 0xBD09FC1A and size == 2:
            self._store_raw(address, 2, value)
            self._store_raw(0xBD09FC28, 2, self._stored(0xBD09FC28, 2) & ~self.EVENT_EP0_SETUP)
            stalled = (value & 3) == 3
            payload = b""
            if not stalled and (value & 1) and self.last_setup[0] == 0x00 and self.last_setup[1] == 0x05:
                self._device_address = self.last_setup[2] & 0x7F   # latched by the hardware
            if not stalled and (value & 1):
                dma_active = self._stored(self.EP_BASE + 0x2E, 2) & 1
                dma_addr = self._stored(self.EP_BASE + 0x24, 4)
                dma_len = self._stored(self.EP_BASE + 0x28, 4)
                if dma_active and dma_len > 0 and self._dma_read is not None:
                    w_length = int.from_bytes(self.last_setup[6:8], "little")
                    actual_len = min(dma_len, w_length) if ((self.last_setup[0] & 0x80) or w_length == 0) else dma_len
                    payload = self._dma_read(dma_addr, actual_len) if actual_len > 0 else b""
                self._store_raw(self.EP_BASE + 0x2E, 2, 0)
            self.control_transfers.append(
                UsbControlTransfer(
                    setup=self.last_setup,
                    response=payload,
                    stalled=stalled,
                    device_address=self.device_address,
                )
            )
            self._update_irqs()
            if self.on_control_transfer is not None:
                self.on_control_transfer(self.control_transfers[-1])
            return

        if self.EP_BASE <= address < self.GLOBAL_BASE and (address - self.EP_BASE) % 0x400 == 0x04 and size == 2:
            ep = (address - self.EP_BASE) // 0x400
            value = (value & 0xFFF0) | ep

        self._store_raw(address, size, value)

        # Check non-EP0 endpoint control / DMA strobes
        if self.EP_BASE + 0x400 <= address < self.GLOBAL_BASE:
            ep = (address - self.EP_BASE) // 0x400
            off = (address - self.EP_BASE) % 0x400
            eb = self.EP_BASE + 0x400 * ep
            if off == 0x1C and size == 2:
                self._store_raw(0xBD09FC1C, 2, self._stored(0xBD09FC1C, 2) & ~(1 << ep))
                self._update_irqs()
            elif off in (0x20, 0x30) and size == 2:
                self._store_raw(eb + (0x2C if off == 0x20 else 0x3C), 2, 0)
                if off == 0x20 and self._stored(eb + 0x2E, 2) == 0:
                    self.rx_armed.discard(ep)
                self._store_raw(0xBD09FC24, 2, self._stored(0xBD09FC24, 2) & ~(1 << ep))
                if self.rx_queues[ep] and (self._stored(0xBD09FC1E, 2) & (1 << ep)):
                    self._store_raw(eb + 0x08, 2, 0x0001)
                    self._store_raw(eb + 0x10, 2, min(0x07FF, len(self.rx_queues[ep])) << 2)
                    self._store_raw(0xBD09FC1C, 2, self._stored(0xBD09FC1C, 2) | (1 << ep))
                self._update_irqs()
            elif off in (0x2E, 0x3E) and size == 2 and (value & 1):
                ch = 0 if off == 0x2E else 1
                dma_addr = self._stored(eb + (0x24 if ch == 0 else 0x34), 4)
                dma_len = self._stored(eb + (0x28 if ch == 0 else 0x38), 4)
                ctrl = self._stored(eb + 0x00, 4)
                ep_xfer = None
                if self._is_out(ctrl) and not self.rx_queues[ep] and ch == 0 and dma_len > 0:
                    # armed before any data: stays busy until packets fill it
                    self.rx_armed.add(ep)
                    self._update_irqs()
                    return
                if self._is_out(ctrl) and self.rx_queues[ep] and self._dma_write is not None:
                    chunk = bytes(self.rx_queues[ep][:dma_len])
                    del self.rx_queues[ep][:dma_len]
                    self._dma_write(dma_addr, chunk)
                    ep_xfer = (ep, ch, chunk)
                    self.endpoint_transfers.append(ep_xfer)
                    self._store_raw(eb + 0x08, 2, 0)
                    self._store_raw(eb + 0x10, 2, min(0x07FF, len(self.rx_queues[ep])) << 2)
                    self._store_raw(0xBD09FC1C, 2, self._stored(0xBD09FC1C, 2) & ~(1 << ep))
                    self._store_raw(eb + (0x28 if ch == 0 else 0x38), 4, dma_len - len(chunk))
                    self._store_raw(eb + (0x2C if ch == 0 else 0x3C), 2, 0xFFFF)
                    self._store_raw(0xBD09FC24, 2, self._stored(0xBD09FC24, 2) | (1 << ep))
                elif not self._is_out(ctrl) and (ctrl & 0xFFFF) and dma_len > 0 and self._dma_read is not None:
                    data = self._dma_read(dma_addr, dma_len)
                    ep_xfer = (ep, ch, data)
                    self.endpoint_transfers.append(ep_xfer)
                    self.tx_queues[ep].extend(data)
                    self._store_raw(eb + (0x28 if ch == 0 else 0x38), 4, 0)
                    self._store_raw(eb + (0x2C if ch == 0 else 0x3C), 2, 0xFFFF)
                    self._store_raw(0xBD09FC24, 2, self._stored(0xBD09FC24, 2) | (1 << ep))
                self._store_raw(address, 2, 0)
                self._update_irqs()
                if ep_xfer is not None and self.on_endpoint_transfer is not None:
                    self.on_endpoint_transfer(ep_xfer)
