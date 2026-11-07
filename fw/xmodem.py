"""XMODEM-CRC / XMODEM-1K sender as a transport-agnostic state machine.

Feed it every byte the receiver sends (``feed``) and transmit whatever
``pending()`` returns. Works for a blocking serial port (fw.tool) and for the
emulator, where bytes arrive through a UART callback (fw.harness).
"""
from __future__ import annotations

SOH, STX, EOT, ACK, NAK, CAN, CRC_REQ = 0x01, 0x02, 0x04, 0x06, 0x15, 0x18, ord('C')


def crc16_xmodem(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


class XmodemSender:
    """Sends ``data`` in 1 KiB (or 128 B) CRC blocks, padded with ``pad``."""

    def __init__(self, data: bytes, *, block_size: int = 1024, pad: int = 0x1A, max_retries: int = 10):
        if block_size not in (128, 1024):
            raise ValueError('XMODEM block size must be 128 or 1024')
        self.data = bytes(data)
        self.block_size = block_size
        self.pad = pad
        self.max_retries = max_retries
        self.blocks = [self.data[i:i + block_size] for i in range(0, len(self.data), block_size)] or [b'']
        self.index = 0            # next block to send (0-based)
        self.retries = 0
        self.started = False
        self.eot_sent = False
        self.done = False
        self.error: str | None = None
        self._out = bytearray()
        self.events: list[str] = []

    # -- helpers ---------------------------------------------------------------
    def _frame(self, index: int) -> bytes:
        payload = self.blocks[index].ljust(self.block_size, bytes([self.pad]))
        num = (index + 1) & 0xFF
        head = bytes([STX if self.block_size == 1024 else SOH, num, 0xFF - num])
        return head + payload + crc16_xmodem(payload).to_bytes(2, 'big')

    def _send_current(self) -> None:
        if self.index >= len(self.blocks):
            self._out += bytes([EOT])
            self.eot_sent = True
        else:
            self._out += self._frame(self.index)

    # -- public API ------------------------------------------------------------
    def pending(self) -> bytes:
        out = bytes(self._out)
        self._out.clear()
        return out

    def feed(self, byte: int) -> None:
        if self.done:
            return
        if byte == CRC_REQ and not self.started:
            self.started = True
            self.events.append('start')
            self._send_current()
        elif byte == ACK and self.started:
            if self.eot_sent:
                self.done = True
                self.events.append('done')
                return
            self.index += 1
            self.retries = 0
            self._send_current()
        elif byte == NAK and self.started:
            self.retries += 1
            self.events.append(f'nak:{self.index}')
            if self.retries > self.max_retries:
                self.error = 'too many NAKs'
                self.done = True
                return
            self._send_current()
        elif byte == CAN:
            self.error = 'cancelled by receiver'
            self.done = True
            self.events.append('cancel')

    @property
    def progress(self) -> float:
        return min(1.0, self.index / max(1, len(self.blocks)))
