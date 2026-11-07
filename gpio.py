"""AXON GPIO output, direction, input and interrupt-routing registers.

Firmware accessors 0xa0005a90, 0xa0005b08 and 0xa0005b14 establish the
stride-eight pin bank. The wrapper at 0xa0010348 preloads the output before
selecting direction 1; this supports 0=input and 1=output. Other direction
encodings, pull resistors, alternate functions and electrical timing remain
unknown and are not inferred from the boot path.
"""
from __future__ import annotations


class GPIOError(RuntimeError):
    """A GPIO access needs an external signal or unsupported hardware behavior."""


class GPIOController:
    """64 pins with explicit external inputs and retained routing state.

    The pin count is inferred from the IRQ accessor's four groups of 16 pins
    (word index ``3 - (pin >> 4)``). Eight route channels are inferred from the
    eight-byte software enable array before its per-pin routing table. These
    are board-profile assumptions, not silicon IDs read from a datasheet.

    A pin's input register reports its output latch when configured as an
    output. Input pins require an explicit external level; no pull-up/down is
    invented. Conflicting internal and external drives raise an error.

    The +0x400 bank is read as low-byte event status and cleared by writing
    zero in the firmware. The +0x500 banks retain masks in reversed 16-pin
    word order. Edge polarity and CPU IRQ delivery are not decoded: callers
    may inject an explicit raw event, but signal changes do not invent one.
    """

    _DEFAULT_WIDTHS: dict[int, int] | None = None

    def __init__(self, base: int = 0xBD0B2000, *, pin_count: int = 64,
                 external_inputs: dict[int, int] | None = None,
                 invalidate_mmio=None):
        if pin_count != 64:
            raise ValueError('only the inferred 64-pin GPIO layout is supported')
        self.base, self.pin_count = base, pin_count
        self._invalidate_mmio = invalidate_mmio
        self.output_latches = [0] * pin_count
        self.directions = [0] * pin_count
        self.external_inputs: dict[int, int] = {}
        self.event_status = [0] * pin_count
        self.irq_masks = [[0] * 4 for _ in range(8)]
        if base == 0xBD0B2000:
            if GPIOController._DEFAULT_WIDTHS is None:
                w = {
                    base + pin * 8 + offset: 2
                    for pin in range(pin_count) for offset in (0, 2, 4)
                }
                w.update({base + 0x400 + pin * 2: 2 for pin in range(pin_count)})
                w.update({base + 0x500 + channel * 0x10 + word * 2: 2
                          for channel in range(8) for word in range(4)})
                GPIOController._DEFAULT_WIDTHS = w
            self.widths = GPIOController._DEFAULT_WIDTHS
        else:
            self.widths = {
                base + pin * 8 + offset: 2
                for pin in range(pin_count) for offset in (0, 2, 4)
            }
            self.widths.update({base + 0x400 + pin * 2: 2 for pin in range(pin_count)})
            self.widths.update({base + 0x500 + channel * 0x10 + word * 2: 2
                                for channel in range(8) for word in range(4)})
        for pin, value in (external_inputs or {}).items():
            self.set_input(pin, value)

    @property
    def pages(self) -> list[int]:
        return sorted({address & ~0xFFF for address in self.widths})

    def contains(self, address: int) -> bool:
        return address in self.widths

    def _pin(self, pin: int) -> None:
        if not isinstance(pin, int) or not 0 <= pin < self.pin_count:
            raise ValueError(f'GPIO pin must be in 0..{self.pin_count - 1}')

    def set_input(self, pin: int, level: int | None) -> None:
        """Drive a known external level, or release it with ``None``.

        No edge event is synthesized because the edge-detection mode has not
        been decoded. This also makes a static board input profile explicit.
        """
        self._pin(pin)
        if level is None:
            self.external_inputs.pop(pin, None)
        elif level in (0, 1):
            self.external_inputs[pin] = int(level)
        else:
            raise ValueError('GPIO external level must be zero, one, or None')
        if self._invalidate_mmio is not None:
            self._invalidate_mmio(self.base + pin * 8, 2)

    def set_event(self, pin: int, status: int) -> None:
        """Supply raw pending status without claiming an edge interpretation."""
        self._pin(pin)
        if not isinstance(status, int) or not 0 <= status <= 0xFF:
            raise ValueError('GPIO event status must fit in eight bits')
        self.event_status[pin] = status

    def _offset(self, address: int, size: int) -> int:
        if self.widths.get(address) != size:
            raise GPIOError(f'unmodeled GPIO access: {address:#010x}, size {size}')
        return address - self.base

    def _sample(self, pin: int) -> int:
        external = self.external_inputs.get(pin)
        if self.directions[pin] == 1:
            value = self.output_latches[pin]
            if external is not None and external != value:
                raise GPIOError(f'GPIO pin {pin} has conflicting internal/external drives')
            return value
        if external is None:
            raise GPIOError(f'GPIO pin {pin} input level is unknown; configure an external input')
        return external

    def read(self, address: int, size: int) -> int:
        offset = self._offset(address, size)
        if offset < 0x200:
            pin, register = divmod(offset, 8)
            if register == 0:
                return self._sample(pin)
            if register == 2:
                return self.output_latches[pin]
            return self.directions[pin]
        if offset < 0x480:
            return self.event_status[(offset - 0x400) // 2]
        channel, word_offset = divmod(offset - 0x500, 0x10)
        return self.irq_masks[channel][word_offset // 2]

    def write(self, address: int, size: int, value: int) -> None:
        offset = self._offset(address, size)
        value &= 0xFFFF
        if offset < 0x200:
            pin, register = divmod(offset, 8)
            if register == 0:
                raise GPIOError(f'GPIO pin {pin} input register is read-only')
            if value not in (0, 1):
                name = 'output' if register == 2 else 'direction'
                raise GPIOError(f'unmodeled GPIO pin {pin} {name} encoding {value:#06x}')
            if register == 2:
                self.output_latches[pin] = value
            else:
                self.directions[pin] = value
            return
        if offset < 0x480:
            if value:
                raise GPIOError(f'unmodeled GPIO event-status write {value:#06x}')
            self.event_status[(offset - 0x400) // 2] = 0
            return
        channel, word_offset = divmod(offset - 0x500, 0x10)
        self.irq_masks[channel][word_offset // 2] = value
