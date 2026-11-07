"""MICCI2 serial controller and an explicitly configured register endpoint.

The command encoding and busy bit come from hw_micci2_NOS.c routines in the
dump (0xa00101bc and 0xa001025a). Transaction latency is a simulation parameter,
not a measured hardware rate. Unknown endpoint registers stop emulation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


class MicciError(RuntimeError):
    """A MICCI access requires behavior that has not been established."""


@dataclass
class RegisterDevice:
    """An external device profile; no implicit zero reads or automatic success.

    Register zero is an immutable identity. Other reset values and writable
    registers must be supplied explicitly by the caller. Written configuration
    registers can subsequently be read back. Dynamic registers need a more
    specific endpoint implementation.
    """

    identity: int
    reset_values: dict[int, int] = field(default_factory=dict)
    writable_registers: frozenset[int] = frozenset()

    def __post_init__(self) -> None:
        if not 0 <= self.identity <= 0xffff:
            raise ValueError('MICCI identity must be a 16-bit value')
        if 0 in self.reset_values or 0 in self.writable_registers:
            raise ValueError('MICCI register zero is the immutable identity')
        self.registers = dict(self.reset_values)

    def read(self, register: int) -> int:
        if register == 0:
            return self.identity
        if register not in self.registers:
            raise MicciError(f'unmodeled MCASIC register read: {register:#04x}')
        return self.registers[register] & 0xffff

    def validate_write(self, register: int, value: int) -> None:
        if register not in self.writable_registers:
            raise MicciError(f'unmodeled MCASIC register write: {register:#04x} = {value:#06x}')

    def write(self, register: int, value: int) -> None:
        self.validate_write(register, value)
        self.registers[register] = value & 0xffff


class Mcasic8014(RegisterDevice):
    """Partial external MCASIC selected by this dump's board descriptor.

    Identity 0x8014 and register numbers come from the descriptor at
    0x200014d4. Reset status registers 1 and 7 are assumed to have no latched
    external events. Configuration registers reset to zero; their complete
    bit definitions have not been established. This is an explicit virtual
    board profile, not a claim that the printer's analog state was measured.

    ADC mux 15 is modeled as an ideal half-scale reference (512/1024), inferred
    from the firmware's calibration check. Mux 0 measures an externally
    configured supply using the firmware's 28.05 V full-scale conversion.
    Other mux selections require explicit raw inputs. No analog channel is
    populated solely to satisfy a firmware threshold.
    """

    def __init__(self, schedule: Callable[[int, Callable[[], None]], None], *,
                 supply_mv: int = 24000, adc_ticks: int = 64,
                 analog_inputs: dict[int, int | Callable[[], int]] | None = None,
                 power_button_wakeup: bool = False):
        if not 0 <= supply_mv <= 28050:
            raise ValueError('MCASIC supply_mv must be between 0 and 28050')
        if adc_ticks < 1:
            raise ValueError('MCASIC adc_ticks must be positive')
        inputs = dict(analog_inputs or {})
        if any(not 0 <= key <= 255 or (not callable(value) and not 0 <= value <= 1023)
               for key, value in inputs.items()):
            raise ValueError('MCASIC ADC inputs require 8-bit selectors and 10-bit values')
        if 0 in inputs or 15 in inputs:
            raise ValueError('MCASIC ADC selectors 0 and 15 are supply and internal reference')
        writable = frozenset({
            1, 2, 3, 6, 7, 8, 9, 10, 12, 0x0d, 0x10,
            0x16, 0x1a, 0x1b, 0x1c, 0x1d,
            0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x29, 0x2a, 0x2b, 0x2c, 0x2d,
            0x30, 0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3a, 0x3b,
        })
        reset_values = {register: 0 for register in writable | {1, 0x11, 0x4e}}
        reset_values[0x3d] = 0x0100
        if power_button_wakeup:
            reset_values[7] = 0x0001
        super().__init__(0x8014, reset_values, writable)
        self.schedule = schedule
        self.supply_mv = supply_mv
        self.adc_ticks = adc_ticks
        self.analog_inputs = inputs
        self.adc_busy = False
        self.power_off_requested = False
        self.adc_conversions: list[dict] = []
        self.analog_profile = {
            'supply_mv': supply_mv,
            'supply_measured': False,
            'reference': 'ideal half-scale internal reference',
            'adc_bits': 10,
            'adc_full_scale_mv': 28050,
            'other_inputs': {k: ('callable' if callable(v) else v) for k, v in inputs.items()},
        }

    def press_power_button(self, *, held: bool = False) -> None:
        """Latch a runtime power-button press on Register 7 (bit 4, optional live bit 1)."""
        self.registers[7] |= 0x0010
        if held:
            self.registers[7] |= 0x0002
        else:
            self.registers[7] &= ~0x0002

    def _check_adc_selector(self, selector: int) -> None:
        if selector not in (0, 15) and selector not in self.analog_inputs:
            raise MicciError(f'unmodeled MCASIC ADC input selector: {selector:#04x}')

    def _adc_sample(self, selector: int) -> int:
        if selector == 15:
            return 512
        if selector == 0:
            return (self.supply_mv * 1023 + 14025) // 28050
        if selector in self.analog_inputs:
            raw = self.analog_inputs[selector]
            sample = int(raw()) if callable(raw) else raw
            if not 0 <= sample <= 1023:
                raise ValueError('MCASIC ADC callable input must return a 10-bit value')
            return sample
        raise MicciError(f'unmodeled MCASIC ADC input selector: {selector:#04x}')

    def read(self, register: int) -> int:
        if register in (0x2A, 0x2C):
            val = self.registers.get(register, 0)
            if val in (0, 0x80):
                return 0x00D0
            return val & 0xFFFF
        if register in (0x2B, 0x2D):
            val = self.registers.get(register, 0)
            if val in (0, 0x80):
                return 0x0030
            return val & 0xFFFF
        return super().read(register)

    def validate_write(self, register: int, value: int) -> None:
        if register == 0 and value == 0x0004:
            return
        super().validate_write(register, value)
        if register == 0x10 and value & 0x8000:
            if self.adc_busy:
                raise MicciError('MCASIC ADC start while conversion is in progress')
            self._check_adc_selector(value & 0xff)

    def write(self, register: int, value: int) -> None:
        self.validate_write(register, value)
        if register == 0:
            return
        if register == 1:
            self.registers[1] &= ~(value & 0xffff)
            return
        if register == 7:
            if value & 0x0004:
                self.registers[7] &= ~0x0001
            if value & 0x0040:
                self.registers[7] &= ~0x0010
            if value & 0x8000:
                self.registers[7] |= 0x8000
                self.power_off_requested = True
            self.registers[7] = (self.registers[7] & 0x8013) | (value & ~0x8057 & 0xffff)
            return
        if register != 0x10 or not value & 0x8000:
            super().write(register, value)
            return
        selector = value & 0xff
        sample = self._adc_sample(selector)
        self.registers[0x10] = value & 0xffff
        self.registers[0x11] = 0
        self.adc_busy = True
        self.adc_conversions.append({'selector': selector, 'sample': sample,
                                     'mode': (value >> 8) & 0x7f})

        def complete() -> None:
            self.registers[0x11] = 0x8000 | sample
            self.registers[0x10] &= ~0x8000
            self.adc_busy = False

        complete._is_timer = True
        self.schedule(self.adc_ticks, complete)


class CarriageAsic(RegisterDevice):
    """Carriage / printhead companion ASIC on MICCI controller 1, bus 1, device 4."""

    # Firmware tUiLite pen status codes consumed by 0xa044f26e..0xa044f466:
    # 1 = 3/3 bars (Full), 3 = 2/3 bars (Medium), 4 = 1/3 bar (Low),
    # 2 = 0/3 bars (Very Low/Empty), 5 = Missing/Error (blinking icon).
    PEN_STATE_BY_BARS: dict[int, int] = {
        3: 1,
        2: 3,
        1: 4,
        0: 2,
    }
    BARS_BY_PEN_STATE: dict[int, int] = {
        1: 3,
        3: 2,
        4: 1,
        2: 0,
        5: 0,
    }
    IDS_STATUS_BASE = 0x20DF37B4

    def __init__(self, identity: int = 0x0801, *,
                 pens_installed: bool = True,
                 color_bars: int = 3,
                 black_bars: int = 3):
        reset_values = {reg: 0 for reg in range(1, 0x80)}
        reset_values.update({0x09: 0x8100, 0x0C: 0x8000, 0x7D: identity & 0xFFFF})
        super().__init__(identity & 0xFFFF, reset_values, frozenset(range(1, 0x80)))
        self.registers[0x00] = 0x800E
        self.pens_installed = True
        self.color_bars = 3
        self.black_bars = 3
        self.color_pct = 100
        self.black_pct = 100
        self.configure_cartridges(
            installed=pens_installed,
            color_bars=color_bars,
            black_bars=black_bars,
        )

    def configure_cartridges(self, *, installed: bool = True,
                             color_bars: int = 3,
                             black_bars: int = 3,
                             color_pct: int | None = None,
                             black_pct: int | None = None) -> None:
        if isinstance(color_bars, bool) or not isinstance(color_bars, int) or not 0 <= color_bars <= 3:
            raise ValueError('color_bars must be in 0..3')
        if isinstance(black_bars, bool) or not isinstance(black_bars, int) or not 0 <= black_bars <= 3:
            raise ValueError('black_bars must be in 0..3')
        if color_pct is not None and (isinstance(color_pct, bool) or not isinstance(color_pct, int) or not 0 <= color_pct <= 100):
            raise ValueError('color_pct must be in 0..100')
        if black_pct is not None and (isinstance(black_pct, bool) or not isinstance(black_pct, int) or not 0 <= black_pct <= 100):
            raise ValueError('black_pct must be in 0..100')
        self.pens_installed = bool(installed)
        self.color_bars = int(color_bars)
        self.black_bars = int(black_bars)
        default_pct = {3: 100, 2: 66, 1: 33, 0: 5}
        self.color_pct = default_pct[self.color_bars] if color_pct is None else int(color_pct)
        self.black_pct = default_pct[self.black_bars] if black_pct is None else int(black_pct)
        if not self.pens_installed:
            self.registers[0x09] = 0x8000
        else:
            self.registers[0x09] = 0x8100

    def cartridge_summary(self) -> dict[str, object]:
        color_state = self.PEN_STATE_BY_BARS[self.color_bars] if self.pens_installed else 5
        black_state = self.PEN_STATE_BY_BARS[self.black_bars] if self.pens_installed else 5
        return {
            'installed': self.pens_installed,
            'pen_error_mask': 0x00 if self.pens_installed else 0x03,
            'color_bars': self.color_bars if self.pens_installed else 0,
            'black_bars': self.black_bars if self.pens_installed else 0,
            'color_pct': self.color_pct if self.pens_installed else 0,
            'black_pct': self.black_pct if self.pens_installed else 0,
            'color_pen_state': color_state,
            'black_pen_state': black_state,
        }

    def apply_to_firmware(self, target) -> dict[str, object]:
        """Synchronize cartridge presence and ink bar state with the firmware IDS table and LEDM network tables."""
        uc = getattr(target, 'uc', target)
        summary = self.cartridge_summary()
        payload = (
            int(summary['pen_error_mask']).to_bytes(4, 'big')
            + int(summary['color_pct']).to_bytes(4, 'big')
            + int(summary['black_pct']).to_bytes(4, 'big')
            + int(summary['color_pen_state']).to_bytes(4, 'big')
            + int(summary['black_pen_state']).to_bytes(4, 'big')
        )
        uc.mem_write(self.IDS_STATUS_BASE + 4, payload)

        installed = bool(summary['installed'])
        color_pct = int(summary['color_pct'])
        black_pct = int(summary['black_pct'])
        import struct
        ledm_entries = [
            (0x20CFF584, 0 if installed else 1),
            (0x20CFF588, 0 if installed else 1),
            (0x20CFF58C, 0 if installed else 1),
            (0x20D05A74, 1 if installed else 0),
            (0x20D05ABC, 1 if installed else 0),
            (0x20D05A80, 0x80000000 if installed else 0),
            (0x20D05AD4, 0x20000000 if installed else 0),
            (0x2134B284, 1 if installed else 0),
            (0x2134B3D4, 0),
            (0x2134B27C, 0 if installed else 0x10),
            (0x2134B3CC, 0 if installed else 0x10),
            (0x2134B28C, color_pct if installed else 0),
            (0x2134B3DC, black_pct if installed else 0),
            (0x2134B234, 0),
            (0x2134B384, 0),
        ]
        for addr, val in ledm_entries:
            uc.mem_write(addr, struct.pack('>I', val))

        if hasattr(target, '_native_observer') and target._native_observer is not None:
            target._native_observer.synchronize(refresh=True)

        return summary

    def read(self, register: int) -> int:
        if register == 0x00:
            return 0x800E
        if register == 0x7D:
            return self.identity
        return super().read(register)

    def validate_write(self, register: int, value: int) -> None:
        if register == 0x00:
            return
        super().validate_write(register, value)

    def write(self, register: int, value: int) -> None:
        self.validate_write(register, value)
        val = value & 0xFFFF
        if register in (0x00, 0x7D):
            return
        if register == 0x0C:
            val |= 0x8000
        elif register == 0x09:
            val = 0x8000 | (val & 0x7E00) | (0x0100 if self.pens_installed else 0x0000)
        super().write(register, val)


class MicciController:
    """One MICCI2 bridge, two sub-buses, and up to eight endpoints per bus.

    ``devices`` keys are (sub-bus, device). The dump selects controller zero,
    sub-bus zero, device six for the MCASIC. Its firmware descriptor expects
    identity 0x8014; supplying that identity is an explicit board assumption.
    A missing device raises an error instead of fabricating a serial response.
    """

    def __init__(self, base: int, schedule: Callable[[int, Callable[[], None]], None],
                 devices: dict[tuple[int, int], RegisterDevice] | None = None,
                 transaction_ticks: int = 32,
                 invalidate_mmio: Callable[[int, int], None] | None = None):
        if transaction_ticks < 1:
            raise ValueError('MICCI transaction_ticks must be positive')
        self.base = base
        self.schedule = schedule
        self.devices = dict(devices or {})
        self.transaction_ticks = transaction_ticks
        self._invalidate_mmio = invalidate_mmio
        self.widths = {base + offset: size for offset, size in (
            (0, 2), (4, 4), (8, 4), (0x10, 4), (0x14, 2), (0x18, 4), (0x1c, 2),
            (0x90, 2), (0x92, 2), (0x94, 2),
            (0xa6, 2), (0xa8, 2), (0xaa, 2), (0xac, 2), (0xb0, 4),
        )}
        for offset in range(0x20, 0x90, 2):
            self.widths[base + offset] = 2
        for slot in range(4):
            slot_base = 0xb4 + 16 * slot
            self.widths[base + slot_base] = 4
            self.widths[base + slot_base + 4] = 2
            self.widths[base + slot_base + 8] = 4
            self.widths[base + slot_base + 12] = 4
        self.config = {0: 0, 4: 0, 8: 0, 0x10: 0, 0x18: 0}
        self.table: dict[int, int] = {}
        self.data = 0
        self.command = 0
        self.busy = False
        self.write_data = 0
        self.write_command = 0
        self.write_busy = False
        self.error_status = 0
        self.irq_enable = 0
        self.transactions: list[dict] = []

    def _offset(self, address: int, size: int) -> int:
        if self.widths.get(address) != size:
            raise MicciError(f'unmodeled MICCI access: {address:#010x}, size {size}')
        return address - self.base

    def read(self, address: int, size: int) -> int:
        offset = self._offset(address, size)
        if offset in self.config:
            return self.config[offset]
        if offset in (0x14, 0x1c):
            return 0
        if 0x20 <= offset < 0x90 or 0xb4 <= offset < 0x100:
            return self.table.get(offset, 0)
        if offset == 0x90:
            return 0x0020 if self.write_busy else 0x0001
        if offset == 0x92:
            return self.write_data
        if offset == 0x94:
            return self.write_command
        if offset == 0xa6:
            return self.error_status
        if offset == 0xa8:
            return self.irq_enable
        if offset == 0xaa:
            raise MicciError(f'unmodeled MICCI status-clear read: {address:#010x}')
        if offset == 0xac:
            return self.command
        return self.data | (0x80000000 if self.busy else 0)

    def _decode_command(self, value: int) -> tuple[int, int, int, RegisterDevice | None]:
        bus, device = (value >> 4) & 1, (value >> 13) & 7
        register = (value >> 6) & 0x7f
        return bus, device, register, self.devices.get((bus, device))

    def write(self, address: int, size: int, value: int) -> None:
        offset = self._offset(address, size)
        if offset in self.config:
            self.config[offset] = value & ((1 << (8 * size)) - 1)
            return
        if 0x20 <= offset < 0x90 or 0xb4 <= offset < 0x100:
            self.table[offset] = value & ((1 << (8 * size)) - 1)
            return
        if offset in (0x14, 0x1c, 0x90, 0xa6):
            raise MicciError(f'unmodeled MICCI status write: {address:#010x}')
        if offset == 0xa8:
            self.irq_enable = value & 0xffff
            return
        if offset == 0xaa:
            self.error_status &= ~(value & 0xffff)
            if self._invalidate_mmio is not None:
                self._invalidate_mmio(self.base + 0xa6, 2)
            return
        if offset == 0x92:
            if self.write_busy:
                raise MicciError('MICCI write FIFO data write while busy')
            self.write_data = value & 0xffff
            return
        if offset == 0x94:
            if self.write_busy:
                raise MicciError('MICCI write FIFO command while busy')
            if not self.config[0]:
                raise MicciError('MICCI command before controller configuration')
            if value & 0x2f != 0:
                raise MicciError(f'unmodeled MICCI write FIFO command flags: {value:#06x}')
            bus, device, register, endpoint = self._decode_command(value)
            if endpoint is None:
                if not (self.config[0] & 0x4000):
                    raise MicciError(f'unmodeled MICCI endpoint: controller {self.base:#010x}, '
                                     f'bus {bus}, device {device}, write register {register:#04x}')
            else:
                endpoint.validate_write(register, self.write_data)
            response = self.write_data
            self.write_command = value & 0xffff
            self.write_busy = True
            if self._invalidate_mmio is not None:
                self._invalidate_mmio(self.base + 0x90, 2)
            self.transactions.append({'bus': bus, 'device': device, 'register': register,
                                      'read': False, 'data': response})

            def complete_write() -> None:
                if endpoint is not None:
                    endpoint.write(register, response)
                self.write_busy = False
                if self._invalidate_mmio is not None:
                    self._invalidate_mmio(self.base + 0x90, 2)

            complete_write._is_timer = True
            self.schedule(self.transaction_ticks, complete_write)
            return
        if self.busy:
            raise MicciError('MICCI transaction or data write while busy')
        if offset == 0xb0:
            if value & 0xffff0000:
                raise MicciError(f'unmodeled MICCI transmit data bits: {value:#010x}')
            self.data = value
            if self._invalidate_mmio is not None:
                self._invalidate_mmio(self.base + 0xb0, 4)
            return
        if not self.config[0]:
            raise MicciError('MICCI command before controller configuration')
        if value & 0xf != 0xf:
            raise MicciError(f'unmodeled MICCI command flags: {value:#06x}')
        bus, device, register, endpoint = self._decode_command(value)
        reading = bool(value & 0x20)
        if endpoint is None:
            if reading and (self.config[0] & 0x4000):
                self.command = value & 0xffff
                self.busy = True
                if self._invalidate_mmio is not None:
                    self._invalidate_mmio(self.base + 0xb0, 4)
                self.transactions.append({'bus': bus, 'device': device, 'register': register,
                                          'read': True, 'data': 0})

                def complete_probe() -> None:
                    self.data = 0
                    self.error_status |= 0x0040
                    self.busy = False
                    if self._invalidate_mmio is not None:
                        self._invalidate_mmio(self.base + 0xb0, 4)
                        self._invalidate_mmio(self.base + 0xa6, 2)

                complete_probe._is_timer = True
                self.schedule(self.transaction_ticks, complete_probe)
                return
            raise MicciError(f'unmodeled MICCI endpoint: controller {self.base:#010x}, '
                             f'bus {bus}, device {device}, '
                             f'{"read" if reading else "write"} register {register:#04x}')
        # Validate at command submission. Scheduled callbacks never conceal an
        # unsupported endpoint access inside the virtual clock's event queue.
        if reading:
            response = endpoint.read(register)
        else:
            endpoint.validate_write(register, self.data)
            response = self.data
        self.command = value & 0xffff
        self.busy = True
        if self._invalidate_mmio is not None:
            self._invalidate_mmio(self.base + 0xb0, 4)
        self.transactions.append({'bus': bus, 'device': device, 'register': register,
                                  'read': reading, 'data': response})

        def complete() -> None:
            if not reading:
                endpoint.write(register, response)
            self.data = response & 0xffff
            self.busy = False
            if self._invalidate_mmio is not None:
                self._invalidate_mmio(self.base + 0xb0, 4)

        complete._is_timer = True
        self.schedule(self.transaction_ticks, complete)

