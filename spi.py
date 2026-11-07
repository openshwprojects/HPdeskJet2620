"""Read-only Winbond SPI flash and the established AXON register interface.

The flash byte protocol follows Winbond W25Q128JV Rev G, sections 7 and 8:
https://www.winbond.com/resource-files/w25q128jv%20revg%2004082019%20plus.pdf

The AXON serializer defaults to a strict boundary because control bit 5 and
final-byte/CS timing remain unresolved. Explicit candidate profiles allow the
two interpretations consistent with the observed identification sequences to
be compared. They clock the endpoint byte protocol; neither returns a canned
ID. These profiles are hypotheses, not a claim that AXON timing is decoded.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


class SPIError(RuntimeError):
    """A flash command or controller operation needs unmodeled behavior."""


class W25Q128JV:
    """Byte-level Standard SPI endpoint with an immutable 16 MiB array.

    ``select``/``deselect`` are CS falling/rising edges. ``exchange`` clocks
    eight bits MSB first and returns one output byte, or None when DO is not
    driven. The caller must resolve an undriven pin using its board wiring.

    The default IQ profile has ID EF4018 and factory status bytes 00/02/60.
    Actual nonvolatile status bits are not present in a main-array dump; a
    caller with captured status registers can supply them explicitly. IM/JM
    devices use ID EF7018 and default QE=0. Unique-ID, security, and SFDP data
    are not fabricated.

    By default the array is immutable and every program/erase/status-write
    command faults, preserving the strict read-only boundary. ``writable=True``
    enables an explicit, datasheet-faithful program/erase model (W25Q128JV
    sections 8.2.12-8.2.19) for *emulation testing only*: it never touches a
    physical device. In writable mode Page Program (0x02) clears bits within a
    256-byte page (NOR AND semantics), Sector/Block Erase (0x20/0x52/0xD8) and
    Chip Erase (0xC7/0x60) set a region to 0xFF, each requires a prior Write
    Enable (0x06), and the WEL latch self-clears on completion. WIP (status
    bit 0) asserts for ``wip_read_latency`` RDSR polls after a mutation so a
    firmware busy-poll can be exercised; with latency 0 the operation reports
    ready immediately. Status-register writes, security registers, and block
    locks remain unmodeled and still fault.

    Two framing rules follow the device: a Chip Erase must end at its opcode
    byte, Sector/Block Erase take exactly a 24-bit address, and Page Program
    takes that address plus at least one data byte; a command clocked with
    incomplete framing is ignored and leaves WEL set, as the real part does.
    Issuing a program/erase with WEL clear is a *deliberate* deviation: the
    device would silently ignore it, but this model raises so a driver that
    forgot its Write Enable fails loudly in emulation instead of appearing to
    work. Programs longer than a page wrap within it, so the last data wins.
    """

    SIZE = 16 * 1024 * 1024
    _READ_COMMANDS = frozenset((0x03, 0x0B, 0x05, 0x35, 0x15, 0x9F, 0xAB))
    _LATCH_COMMANDS = frozenset((0x06, 0x04))
    _MUTATING_COMMANDS = frozenset((
        0x01, 0x11, 0x31, 0x02, 0x32, 0x20, 0x52, 0xD8, 0xC7, 0x60,
        0x42, 0x44, 0x36, 0x39, 0x7E, 0x98,
    ))
    # Subset given a faithful model when writable=True; others still fault.
    _ERASE_SIZES = {0x20: 0x1000, 0x52: 0x8000, 0xD8: 0x10000}
    _CHIP_ERASE = frozenset((0xC7, 0x60))
    _SUPPORTED_WRITE = frozenset((0x02,)) | frozenset(_ERASE_SIZES) | _CHIP_ERASE

    def __init__(self, image: bytes, *, ordering_option: str = 'IQ',
                 status_registers: tuple[int, int, int] | None = None,
                 writable: bool = False, wip_read_latency: int = 0):
        if len(image) != self.SIZE:
            raise ValueError('W25Q128JV image must contain exactly 16 MiB')
        if ordering_option not in ('IQ', 'JQ', 'IM', 'JM'):
            raise ValueError('W25Q128JV ordering_option must be IQ, JQ, IM, or JM')
        if not isinstance(wip_read_latency, int) or wip_read_latency < 0:
            raise ValueError('wip_read_latency must be a non-negative integer')
        quad_default = ordering_option.endswith('Q')
        self.writable = bool(writable)
        self.image = bytearray(image) if self.writable else bytes(image)
        self.jedec_id = bytes((0xEF, 0x40 if quad_default else 0x70, 0x18))
        if status_registers is None:
            status_registers = (0, 2 if quad_default else 0, 0x60)
        if len(status_registers) != 3 or any(
                not isinstance(value, int) or not 0 <= value <= 0xFF
                for value in status_registers):
            raise ValueError('status_registers must contain three bytes')
        if status_registers[0] & 3 or status_registers[1] & 0x80:
            raise ValueError('reset status must have BUSY, WEL, and SUS clear')
        if quad_default and not status_registers[1] & 2:
            raise ValueError('IQ/JQ profiles have factory-fixed QE=1')
        self.status_registers = tuple(status_registers)
        self.write_enabled = False
        self.selected = False
        self._command: int | None = None
        self._count = 0
        self._address = 0
        self._prog = bytearray()
        self._wip = False
        self._wip_remaining = 0
        self._wip_latency = wip_read_latency
        self._record: dict | None = None
        self.transactions: list[dict] = []
        self.writes: list[dict] = []
        # Opt-in: clocking a JEDEC read past its three documented bytes returns
        # 0x00 instead of faulting (used with SPIController.rx_read_shift to
        # replay what happened on the physical printer).
        self.relaxed_jedec = False
        # Optional listener ``on_write(address, length)`` invoked after a
        # program/erase has been applied to ``image`` (writable mode only).
        self.on_write: Callable[[int, int], None] | None = None

    def select(self) -> None:
        if self.selected:
            raise SPIError('SPI CS is already asserted')
        self.selected = True
        self._command, self._count, self._address = None, 0, 0
        self._record = {'command': None, 'tx': [], 'rx': [], 'complete': False}
        self.transactions.append(self._record)

    def deselect(self) -> None:
        if not self.selected:
            return
        # WREN/WRDI require CS to rise immediately after the opcode byte.
        if self._count == 0 and self._command in self._LATCH_COMMANDS:
            self.write_enabled = self._command == 0x06
        elif (self.writable and self._command in self._SUPPORTED_WRITE
              and self.write_enabled and self._write_is_framed(self._command)):
            # A program/erase executes on the CS rising edge that ends it, but
            # only when its opcode/address framing is complete. An incompletely
            # addressed command (CS rises early) or a Chip Erase with stray
            # trailing clocks is ignored by the device and leaves WEL set.
            self._apply_write(self._command)
        self.selected = False
        self._record['complete'] = True
        self._record = None
        self._command = None

    def _write_is_framed(self, command: int) -> bool:
        """Whether a program/erase was clocked with complete framing.

        ``_count`` is the number of bytes clocked after the opcode. Chip Erase
        takes none, Sector/Block Erase take exactly a 24-bit (three byte)
        address, and Page Program takes that address followed by data. A
        command whose CS rises before the address is complete, or a Chip Erase
        followed by stray clocks, is not executed by the real device.
        """
        if command in self._CHIP_ERASE:
            return self._count == 0
        if command in self._ERASE_SIZES:
            return self._count == 3
        return self._count >= 4  # Page Program: 24-bit address + >=1 data byte

    def _apply_write(self, command: int) -> None:
        """Commit a validated program/erase to the mutable array (NOR rules)."""
        if command == 0x02:  # Page Program: AND within a 256-byte page, wrapping.
            page_base = self._address & ~0xFF
            offset = self._address & 0xFF
            for i, byte in enumerate(self._prog):
                target = page_base | ((offset + i) & 0xFF)
                self.image[target] &= byte
            record = {'op': 'program', 'address': self._address,
                      'length': len(self._prog)}
        elif command in self._ERASE_SIZES:
            size = self._ERASE_SIZES[command]
            base = self._address & ~(size - 1) & (self.SIZE - 1)
            self.image[base:base + size] = b'\xFF' * size
            record = {'op': 'erase', 'address': base, 'length': size}
        else:  # Chip Erase (0xC7 / 0x60).
            self.image[:] = b'\xFF' * self.SIZE
            record = {'op': 'chip-erase', 'address': 0, 'length': self.SIZE}
        self.write_enabled = False  # WEL self-clears when the operation starts.
        if self._wip_latency > 0:
            self._wip = True
            self._wip_remaining = self._wip_latency
        self.writes.append(record)
        if self.on_write is not None:
            if command == 0x02:  # a wrapped program touches the whole page
                self.on_write(record['address'] & ~0xFF, 0x100)
            else:
                self.on_write(record['address'], record['length'])

    def exchange(self, value: int) -> int | None:
        if not isinstance(value, int) or not 0 <= value <= 0xFF:
            raise ValueError('SPI exchange requires one byte')
        if not self.selected:
            return None
        result = self._exchange(value)
        self._record['tx'].append(value)
        self._record['rx'].append(result)
        return result

    def _exchange(self, value: int) -> int | None:
        if self._command is None:
            if value in self._MUTATING_COMMANDS:
                if not self.writable or value not in self._SUPPORTED_WRITE:
                    raise SPIError(f'read-only SPI flash: command {value:#04x} is unimplemented')
                if not self.write_enabled:
                    raise SPIError(f'W25Q128JV command {value:#04x} issued without a '
                                   'preceding Write Enable (WEL clear)')
                self._command = value
                self._record['command'] = value
                self._address = 0
                self._prog = bytearray()
                return None
            if value not in self._READ_COMMANDS | self._LATCH_COMMANDS:
                raise SPIError(f'unmodeled W25Q128JV command {value:#04x}')
            self._command = value
            self._record['command'] = value
            return None

        command = self._command
        index = self._count
        if command == 0x9F:
            if index >= len(self.jedec_id):
                if not self.relaxed_jedec:
                    raise SPIError('W25Q128JV JEDEC output beyond the three documented ID bytes')
                result = 0x00   # opt-in: the real part keeps clocking out undocumented bytes
            else:
                result = self.jedec_id[index]
        elif command in (0x05, 0x35, 0x15):
            number = {0x05: 0, 0x35: 1, 0x15: 2}[command]
            result = self.status_registers[number]
            if number == 0:
                result |= int(self.write_enabled) << 1
                if self._wip:
                    result |= 1  # Busy/WIP, polled by the firmware after a write.
                    self._wip_remaining -= 1
                    if self._wip_remaining <= 0:
                        self._wip = False
        elif command == 0x02:  # Page Program: three address bytes, then data.
            if index < 3:
                self._address = ((self._address << 8) | value) & (self.SIZE - 1)
            else:
                self._prog.append(value)
            result = None
        elif command in self._ERASE_SIZES:  # Sector/Block Erase: three address bytes.
            if index < 3:
                self._address = ((self._address << 8) | value) & (self.SIZE - 1)
            result = None
        elif command in self._CHIP_ERASE:  # Chip Erase takes no address or data.
            result = None
        elif command in (0x03, 0x0B):
            if index < 3:
                self._address = (self._address << 8) | value
                result = None
            elif command == 0x0B and index == 3:
                result = None
            else:
                result = self.image[self._address]
                self._address = (self._address + 1) & (self.SIZE - 1)
        elif command == 0xAB:
            result = None if index < 3 else 0x17
        else:  # Extra clocks invalidate WREN/WRDI; no array is changed.
            result = None
        self._count += 1
        return result


@dataclass(frozen=True)
class FlashWindow:
    device: int
    start: int
    end: int  # Exclusive CPU memory aperture limit.


class SPIController:
    """AXON registers with optional, explicitly hypothetical serializers.

    The constructor does not invent boot-ROM mapping-register values: absent
    explicit values, reading a window register faults until firmware writes it.
    SPI boot straps give status bit 0 = 0; the serializer is initially idle.
    The register layout and selected-device field come from 0xa000ce98..cf12.
    Mapping writes at 0xa000d21e encode start/end MiB in bits 5:0/11:6.

    ``profile=None`` preserves the undecoded TX boundary. ``byte-final`` clocks
    eight bits for every TX write, including the write releasing CS; bit 5 has
    no effect on clock count. ``wide-final`` clocks eight bits with bit 5 set,
    sixteen with it clear, and releases CS without clocks when bit 0 is clear.
    Both send the MSB of TX first. These alternatives produce the same wire
    bytes for every identification sequence observed in this dump. Other
    register sequences can distinguish them and should not be assumed valid
    for actual hardware without a capture or controller documentation.

    Status field bits 15:11 are 13 while CS is held and 0 after CS release,
    matching the driver's completion checks. The intermediate value 1 and
    ``bit_ticks`` are explicit simulation choices, not measured state/timing.
    Bit 1 is retained as observed initialization configuration; its semantics
    are unknown. Other control bits outside 0, 1, 5, and 11:10 remain strict.

    RX is a 32-bit shift register accompanied by an unknown-bit mask. Undriven
    MISO clocks remain unknown unless board wiring supplies ``miso_pull``.
    Register reads never make time pass. Endpoint failures latch and surface
    on the next register access, including the driver's next status poll.
    """

    PROFILES = ('byte-final', 'wide-final')
    CONTROL_MASK = 0xC23
    BUSY_STATE = 1
    HELD_STATE = 13

    def __init__(self, base: int = 0xBD063800, *,
                 schedule: Callable[[int, Callable[[], None]], None] | None = None,
                 profile: str | None = None, miso_pull: int | None = None,
                 devices: dict[int, W25Q128JV] | None = None,
                 window_registers: tuple[int, int, int] | None = None,
                 bit_ticks: int = 1):
        if profile is not None and profile not in self.PROFILES:
            raise ValueError(f'SPI profile must be None or one of {self.PROFILES}')
        if profile is not None and not callable(schedule):
            raise ValueError('SPI serializer profile requires a schedule callback')
        if miso_pull not in (None, 0, 1):
            raise ValueError('SPI miso_pull must be None, 0, or 1')
        if not isinstance(bit_ticks, int) or bit_ticks < 1:
            raise ValueError('SPI bit_ticks must be a positive integer')
        self.base = base
        self.schedule = schedule
        self.profile = profile
        self.miso_pull = miso_pull
        self.bit_ticks = bit_ticks
        self.devices = dict(devices or {})
        if any(not isinstance(index, int) or not 0 <= index <= 2
               for index in self.devices):
            raise ValueError('SPI controller has three flash device slots')
        if window_registers is not None and (
                len(window_registers) != 3 or any(
                    not isinstance(value, int) or not 0 <= value <= 0xFFF
                    for value in window_registers)):
            raise ValueError('SPI window_registers must contain three 12-bit values')
        self.window_registers = list(window_registers or (None, None, None))
        self.control = 0
        self.busy = False
        self.selected_device: int | None = None
        self.rx_value = 0
        self.rx_known_mask = 0
        self.transfers: list[dict] = []
        self._error: SPIError | None = None
        # Opt-in fault injection reproducing the first boot on the physical
        # printer (2026-11-04): reading RX after every byte with control bit 5
        # always set returned the JEDEC id one bit off (de8030), while HP's
        # pattern, which clears bit 5 on one receive transfer of the CS cycle
        # and reads RX once at the end, returned ef4018. The mechanism is NOT
        # known; this flag only reproduces that observation: in a CS cycle
        # without any bit-5-clear transfer, RX reads return the value shifted
        # left by one bit. Off by default.
        self.rx_read_shift = False
        self._cycle_has_receive = False
        self.widths = {base + offset: 4 for offset in (0, 4, 8, 0x14)}
        self.widths.update({base + offset: 2 for offset in (0x18, 0x1A, 0x1C)})

    def _offset(self, address: int, size: int) -> int:
        if self.widths.get(address) != size:
            raise SPIError(f'unmodeled SPI access: {address:#010x}, size {size}')
        return address - self.base

    def read(self, address: int, size: int) -> int:
        offset = self._offset(address, size)
        if self._error is not None:
            raise self._error
        if offset == 0:
            state = (self.BUSY_STATE if self.busy else self.HELD_STATE
                     if self.selected_device is not None else 0)
            return state << 11  # SPI boot protocol strap bit 0 is clear.
        if offset == 8:
            return self.control
        if offset in (0x18, 0x1A, 0x1C):
            value = self.window_registers[(offset - 0x18) // 2]
            if value is None:
                raise SPIError('SPI boot window reset values have not been supplied')
            return value
        if offset == 4:
            if not self.transfers:
                raise SPIError('SPI receive data unavailable before a modeled transfer')
            if self.busy:
                raise SPIError('SPI receive register read during an active transfer')
            if self.rx_known_mask != 0xFFFFFFFF:
                raise SPIError('SPI receive register contains unknown MISO/reset bits '
                               f'(known_mask={self.rx_known_mask:#010x}); supply '
                               'physical MISO pull wiring or clock known data')
            if self.rx_read_shift and not self._cycle_has_receive:
                return (self.rx_value << 1) & 0xFFFFFFFF
            return self.rx_value
        raise SPIError('SPI transmit register read is unmodeled')

    def write(self, address: int, size: int, value: int) -> None:
        offset = self._offset(address, size)
        if self._error is not None:
            raise self._error
        if not isinstance(value, int) or not 0 <= value < 1 << (size * 8):
            raise SPIError(f'SPI write value does not fit {size} bytes: {value!r}')
        if offset == 8:
            if value & ~0xFFFF:
                raise SPIError(f'unmodeled SPI control high bits {value:#010x}')
            if self.profile is not None:
                if self.busy:
                    raise SPIError('SPI control change during an active transfer')
                if value & ~self.CONTROL_MASK:
                    raise SPIError(f'unmodeled SPI control bits {value:#010x}')
                device = (value >> 10) & 3
                if device > 2:
                    raise SPIError('SPI selected device 3 is outside the three flash slots')
                if self.selected_device is not None and device != self.selected_device:
                    raise SPIError('SPI device change while CS is asserted')
            self.control = value
        elif offset in (0x18, 0x1A, 0x1C):
            if self.busy:
                raise SPIError('SPI window change during an active transfer')
            if value & ~0xFFF:
                raise SPIError(f'unmodeled SPI window high bits {value:#06x}')
            self.window_registers[(offset - 0x18) // 2] = value
        elif offset == 0x14:
            if self.profile is None:
                raise SPIError(
                    'AXON SPI transfer requires unresolved control bit 5 and final-byte/CS '
                    f'timing (control={self.control:#06x}, tx={value:#010x})')
            self._transmit(value)
        else:
            raise SPIError(f'SPI write to read-only register {offset:#x}')

    def _transmit(self, value: int) -> None:
        if self.busy:
            raise SPIError('SPI transmit while another transfer is active')
        device = (self.control >> 10) & 3
        held = bool(self.control & 1)
        if self.profile == 'byte-final':
            count = 1
        else:
            count = (1 if self.control & 0x20 else 2) if held else 0
        if count == 0 and self.selected_device is None:
            raise SPIError('SPI CS-only release without an asserted CS')
        endpoint = self.devices.get(device)
        if self.selected_device is None:
            if endpoint is not None:
                endpoint.select()
            self.selected_device = device
            self._cycle_has_receive = False
        if count and not self.control & 0x20:
            self._cycle_has_receive = True
        tx = list(value.to_bytes(4, 'big')[:count])
        record = {'device': device, 'control': self.control, 'tx': tx,
                  'rx': [], 'release_cs': not held, 'complete': False}
        self.transfers.append(record)
        self.busy = True

        def finish() -> None:
            if not held:
                if endpoint is not None:
                    endpoint.deselect()
                self.selected_device = None
            self.busy = False
            record['complete'] = True

        def step(index: int) -> None:
            try:
                if tx:
                    incoming = endpoint.exchange(tx[index]) if endpoint is not None else None
                    record['rx'].append(incoming)
                    known = 0xFF if incoming is not None or self.miso_pull is not None else 0
                    resolved = incoming if incoming is not None else 0xFF if self.miso_pull == 1 else 0
                    self.rx_value = ((self.rx_value << 8) | resolved) & 0xFFFFFFFF
                    self.rx_known_mask = ((self.rx_known_mask << 8) | known) & 0xFFFFFFFF
                if index + 1 < len(tx):
                    self.schedule(8 * self.bit_ticks, lambda: step(index + 1))
                else:
                    finish()
            except SPIError as exc:
                self._error = exc
                self.busy = False
                record['error'] = str(exc)

        self.schedule((8 if tx else 1) * self.bit_ticks, lambda: step(0))

    def window(self, device: int) -> FlashWindow:
        if not 0 <= device <= 2:
            raise ValueError('SPI window device must be 0, 1, or 2')
        value = self.read(self.base + 0x18 + device * 2, 2)
        return FlashWindow(device, (value & 0x3F) << 20,
                           (((value >> 6) & 0x3F) + 1) << 20)
