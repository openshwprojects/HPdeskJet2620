"""AXON I2C controller and an optional in-memory serial EEPROM.

The firmware driver at 0xa0009370..0xa0009546 establishes the register layout,
BUSY polling, reset, and streaming-byte handshakes. Its negative probe at
0xa000c7ac requires an unacknowledged address to remain BUSY until reset. That
NACK behavior is inferred from firmware, not a controller datasheet. No NACK
status bit is invented. Byte latency is an uncalibrated simulation parameter.
"""
from __future__ import annotations

from typing import Callable


class I2CError(RuntimeError):
    """The controller encountered an unsupported access or transfer."""


class EEPROM:
    """ST M24512-W geometry with explicit contents kept only in memory.

    The board's 24512WP marking identifies 64 KiB and 128-byte write pages.
    Programming starts after the final transmitted byte, wraps within its
    page, and rejects addressing until the write cycle completes. The default
    5000-tick cycle represents the specified 5 ms maximum only under a 1 MHz
    virtual timer assumption; the emulator is not calibrated to real time.
    Creating an erased device is an explicit caller choice, never a default.

    ``write_protect`` optionally supplies the active-high WC pin. Per ST
    DS6520 Rev31 sections 2.4/5.1, protected writes ACK address bytes but NACK
    data bytes; reads are independent of WC. Tables 17/18 require WC low from
    before START through 1 us after STOP, rather than specify STOP-only
    sampling. This byte-level model checks WC when each byte is submitted and
    completed (including STOP); sub-byte glitches/setup/hold are not modeled.
    https://www.st.com/resource/en/datasheet/m24512-w.pdf
    """

    PAGE_SIZE = 128

    def __init__(self, image: bytes, *, write_cycle_ticks: int = 5000,
                 write_protect: Callable[[], bool] | None = None):
        if len(image) != 0x10000:
            raise ValueError('EEPROM image must contain exactly 65536 bytes')
        if not isinstance(write_cycle_ticks, int) or write_cycle_ticks < 1:
            raise ValueError('EEPROM write_cycle_ticks must be positive')
        if write_protect is not None and not callable(write_protect):
            raise ValueError('EEPROM write_protect must be a callable or None')
        self.image = bytearray(image)
        self.write_cycle_ticks = write_cycle_ticks
        self.write_protect = write_protect
        self.busy = False

    @staticmethod
    def encode_nvm_type2(value: int) -> bytes:
        """Encode a 1-byte type-2 NVM item as [val, ~val & 0xFF] per 0xa000cb8c."""
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFF:
            raise ValueError('type-2 NVM value must fit in one byte (0..0xff)')
        return bytes((value, (~value) & 0xFF))

    @staticmethod
    def encode_nvm_type0(data: bytes) -> bytes:
        """Encode a type-0 NVM item as [data, ~data, checksum] per 0xa000c9a2 / 0xa000c9f6."""
        if not data:
            raise ValueError('type-0 NVM data must be non-empty')
        inv = bytes((~b) & 0xFF for b in data)
        csum = (~data[0]) & 0xFF
        for b in data[1:]:
            csum ^= b
        return bytes(data) + inv + bytes((csum & 0xFF,))

    @classmethod
    def create_factory_image(
        cls,
        *,
        board_id: int = 0x68BF,
        board_num: int = 0x1F,
        powerdown_state: int = 0x03,
        load_mode: int = 0x04,
        fwupdate_status: int = 0xAA,
    ) -> bytes:
        """Create a 64 KiB EEPROM image populated with the bootloader's version-0x40 NVM fields."""
        if isinstance(board_id, bool) or not isinstance(board_id, int) or not 0 <= board_id <= 0xFFFF:
            raise ValueError('board_id must be a 16-bit integer')
        buf = bytearray([0xFF]) * 0x10000
        # Version 0x40 layout table at 0x20001804 / 0x20011924:
        buf[0x00] = 0x40
        buf[0x01:0x03] = cls.encode_nvm_type2(board_num)
        buf[0x03:0x05] = cls.encode_nvm_type2(0x00)
        buf[0x05] = 0x00
        buf[0x06:0x08] = cls.encode_nvm_type2(powerdown_state)
        buf[0x0C:0x11] = cls.encode_nvm_type0(board_id.to_bytes(2, 'big'))
        buf[0x48:0x4A] = cls.encode_nvm_type2(load_mode)
        buf[0x4A:0x4C] = cls.encode_nvm_type2(fwupdate_status)
        return bytes(buf)

    def is_write_protected(self) -> bool:
        return bool(self.write_protect()) if self.write_protect is not None else False

    def read(self, address: int) -> int:
        return self.image[address & 0xFFFF]

    def program_page(self, address: int, data: bytes,
                     schedule: Callable[[int, Callable[[], None]], None]) -> bool:
        if self.busy:
            raise I2CError('EEPROM programming requested during a write cycle')
        if self.is_write_protected():
            return False
        page = address & ~(self.PAGE_SIZE - 1)
        changes = {page | ((address + index) & (self.PAGE_SIZE - 1)): value
                   for index, value in enumerate(data)}
        self.busy = True

        def programmed() -> None:
            for offset, value in changes.items():
                self.image[offset & 0xFFFF] = value
            self.busy = False

        schedule(self.write_cycle_ticks, programmed)
        return True


class Undercroft:
    """Undercroft I2C companion device at 7-bit address 0x1F (0x3E write / 0x3F read).

    Modeled from undercroft.c (0xA0468E3E..0xA0469CB8) and UNDERCROFT_CHIP_TABLE
    at 0xA0622C68:
    - Commands are written to subaddress 0x00.
    - Response header is read from subaddress 0x80 ([0x80] = status 0x00,
      [0x81] = payload length), and payload bytes from subaddress 0x82+.
    - Opcode 0x62 / 0x20 (echo self-test, 0xA0469C1C): echoes [2:pkt[1]] payload.
    - Opcode 0x41 (read NVM, 0xA0469706): returns count bytes at offset + 2 CRC bytes.
    - Opcode 0x42 (write NVM, 0xA0469642): writes count bytes at offset.
    - Opcode 0x00 (device info/counter, 0xA0469A10): returns 20 response bytes.
    - Opcode 0xC9 (MAC/challenge, 0xA04699B4): returns 18 response bytes.
    """

    def __init__(self, nvm: bytes | None = None):
        self.nvm = bytearray(256)
        if nvm is not None:
            self.nvm[:min(256, len(nvm))] = nvm[:256]
        else:
            # Default chip identification bits checked by 0xA04695B0 against
            # UNDERCROFT_CHIP_TABLE (0xA0622C68): (nvm[0]>>4)==3, (nvm[3]>>1)==0, (nvm[5]>>4)==2.
            self.nvm[0] = 0x30
            self.nvm[3] = 0x00
            self.nvm[5] = 0x20
        self.busy = False
        self.status = 0x00
        self._response_payload = b""

    def is_write_protected(self) -> bool:
        return False

    def read(self, address: int) -> int:
        if address == 0x80:
            return self.status
        if address == 0x81:
            return len(self._response_payload) & 0xFF
        if address >= 0x82:
            idx = address - 0x82
            return self._response_payload[idx] if idx < len(self._response_payload) else 0
        return 0

    def program_page(self, address: int, data: bytes,
                     schedule: Callable[[int, Callable[[], None]], None]) -> bool:
        del address, schedule
        self.status = 0x00
        if not data:
            self._response_payload = b""
            return True
        opcode = data[0]
        if opcode in (0x62, 0x20):
            pkt_len = data[1] if len(data) >= 2 else len(data)
            self._response_payload = bytes(data[2:pkt_len])
        elif opcode == 0x40:
            # 23-entry partition layout of the 256-byte NVM (0xA0469118 / 0xA0622C68):
            # field 0: type 0xC at 0x00 (7B); fields 1..20: type 0xE at 0x07..0x53 (20x4B);
            # field 21: type 0xB at 0x57 (4B); field 22: type 0xA at 0x5B (165B).
            entries = [(0x0C, 0x00)] + [(0x0E, 0x07 + 4 * i) for i in range(20)] + [
                (0x0B, 0x57), (0x0A, 0x5B),
            ]
            buf = bytearray(2 + len(entries) * 4 + 2)
            for idx, (ftype, foff) in enumerate(entries):
                buf[2 + idx * 4] = ftype
                buf[2 + idx * 4 + 3] = foff
            self._response_payload = bytes(buf)
        elif opcode == 0x41 and len(data) >= 6:
            offset = data[4]
            count = data[5]
            chunk = bytes(self.nvm[(offset + i) & 0xFF] for i in range(count))
            self._response_payload = chunk + b"\x00\x00"
        elif opcode == 0x42 and len(data) >= 6:
            offset = data[4]
            count = data[5]
            for i in range(min(count, max(0, len(data) - 6))):
                self.nvm[(offset + i) & 0xFF] = data[6 + i]
            self._response_payload = b""
        elif opcode == 0x00:
            self._response_payload = bytes(20)
        elif opcode == 0xC9:
            self._response_payload = bytes(18)
        else:
            self._response_payload = bytes(16)
        return True


class I2CController:
    """One controller with explicit seven-bit-address endpoint attachments.

    Control bits inferred from the driver: 0 enable, 1 continue reads,
    2 continue writes, 7 BUSY (read-only), 8 reset command, 9 word addresses.
    Reading a completed byte starts the next byte if continuation was latched
    when the previous byte started. This permits the driver's two-byte-tail
    read sequence without a firmware-specific shortcut.
    """

    BUSY = 0x80
    RESET = 0x100

    def __init__(self, base: int, schedule: Callable[[int, Callable[[], None]], None],
                 devices: dict[int, EEPROM] | None = None, byte_ticks: int = 160,
                 on_interrupt: Callable[[bool], None] | None = None,
                 invalidate_mmio: Callable[[int, int], None] | None = None):
        if not isinstance(byte_ticks, int) or byte_ticks < 1:
            raise ValueError('I2C byte_ticks must be a positive integer')
        self.base, self.schedule, self.byte_ticks = base, schedule, byte_ticks
        self._on_interrupt = on_interrupt
        self._invalidate_mmio = invalidate_mmio
        self.devices = dict(devices or {})
        if any(not 0 <= address < 128 for address in self.devices):
            raise ValueError('I2C device addresses must be seven-bit values')
        self.widths = {base + offset: 2 for offset in
                       (0, 2, 4, 6, 8, 0xA, 0xC, 0xE, 0x12, 0x14, 0x1A, 0x1C)}
        self.registers = {offset: 0 for offset in (2, 4, 6, 8, 0xA, 0xC, 0xE, 0x12, 0x14, 0x1A, 0x1C)}
        self.control = 0
        self.busy = False
        self.interrupt_pending = False
        self._generation = 0
        self._endpoint: EEPROM | None = None
        self._slave = 0
        self._address = 0
        self._reading: bool | None = None
        self._continue = False
        self._write_start = 0
        self._write_data = bytearray()
        self._record: dict | None = None
        self.transactions: list[dict] = []

    @property
    def irq_pending(self) -> bool:
        return bool(self.control & 0x08) and self.interrupt_pending

    def _update_irq(self) -> None:
        if self._on_interrupt is not None:
            self._on_interrupt(self.irq_pending)

    def _offset(self, address: int, size: int) -> int:
        if self.widths.get(address) != size:
            raise I2CError(f'unmodeled I2C access: {address:#010x}, size {size}')
        return address - self.base

    def read(self, address: int, size: int) -> int:
        offset = self._offset(address, size)
        if offset == 0:
            return self.control | (self.BUSY if self.busy else 0)
        if offset != 8:
            return self.registers[offset]
        if self.busy:
            raise I2CError('I2C data read before the byte transfer completed')
        value = self.registers[8]
        if self._reading is True:
            if self._continue:
                self._transfer()
            else:
                self._reading = None
                self._endpoint = None
        return value

    def write(self, address: int, size: int, value: int) -> None:
        offset = self._offset(address, size)
        value &= 0xFFFF
        if offset == 0:
            if value & self.RESET:
                self._generation += 1
                if self.busy and self._record is not None:
                    self._record['aborted'] = True
                self.busy = False
                self.interrupt_pending = False
                self._endpoint = None
                self._reading = None
                self._continue = False
                self._write_data.clear()
            self.control = value & ~(self.BUSY | self.RESET)
            self._update_irq()
            return
        if offset == 0xA:
            self.registers[offset] = value
            if value & 1:
                self.interrupt_pending = False
                self._update_irq()
            return
        if self.busy and offset in (2, 4, 6, 8):
            raise I2CError('I2C transfer register write while BUSY')
        if offset in (2, 4, 6, 8) and value > 0xFF:
            raise I2CError(f'unsupported I2C byte-register value: {value:#06x}')
        self.registers[offset] = value
        if offset == 2:
            if not self.control & 1:
                raise I2CError('I2C transfer requested while the controller is disabled')
            self._slave = value >> 1
            self._endpoint = self.devices.get(self._slave)
            self._reading = bool(value & 1)
            self._address = self.registers[6]
            if self.control & 0x200:
                self._address |= self.registers[4] << 8
            self._write_start = self._address
            self._write_data.clear()
            self._transfer()
        elif offset == 8 and self._reading is False:
            self._transfer()

    def _transfer(self) -> None:
        reading = self._reading
        address = self._address
        endpoint = self._endpoint
        if endpoint is not None and endpoint.busy:
            endpoint = None
        self._continue = bool(self.control & (2 if reading else 4))
        data = endpoint.read(address) if reading and endpoint else self.registers[8]
        protected = bool(endpoint is not None and not reading and endpoint.is_write_protected())
        record = {
            'slave': self._slave, 'address': address, 'read': bool(reading),
            'data': data if endpoint else None, 'ack': endpoint is not None and not protected,
            'completed': False, 'aborted': False,
        }
        if protected:
            record.update(address_ack=True, nack_phase='data', write_protected=True)
        self._record = record
        self.transactions.append(record)
        self.busy = True
        if self._invalidate_mmio is not None:
            self._invalidate_mmio(self.base, 2)
        if endpoint is None or protected:
            # A missing target never produces fabricated data. The firmware's
            # ordinary timeout path must issue RESET to recover this bus.
            return
        generation = self._generation
        continuing = self._continue

        def complete() -> None:
            if generation != self._generation:
                return
            if not reading:
                pending = bytes(self._write_data) + bytes((data,))
                # The final data ACK and STOP share a boundary in this coarse
                # model. Once accepted, later WC changes cannot cancel a
                # programming cycle that is already in progress.
                accepted = (not endpoint.is_write_protected()) if continuing else (
                    endpoint.program_page(self._write_start, pending, self.schedule))
                if not accepted:
                    record.update(ack=False, address_ack=True, nack_phase='data', write_protected=True)
                    return  # The controller remains BUSY on this data NACK.
                self._write_data.append(data)
            self.registers[8] = data
            self.busy = False
            if self._invalidate_mmio is not None:
                self._invalidate_mmio(self.base, 2)
            self.interrupt_pending = True
            self._address = ((address + 1) & 0xFFFF) if reading else (
                (self._write_start & ~(EEPROM.PAGE_SIZE - 1))
                | ((address + 1) & (EEPROM.PAGE_SIZE - 1)))
            record['completed'] = True
            if not reading and not continuing:
                self._endpoint = None
                self._reading = None
            self._update_irq()

        self.schedule(self.byte_ticks, complete)
