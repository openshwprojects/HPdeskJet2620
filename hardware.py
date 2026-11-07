"""Stateful, deterministic AXON boot peripherals inferred from the firmware.

Register addresses and the polled status bits are supported by the dump. Timing
values are adjustable simulation parameters: the ASIC's clocks and peripheral
latencies have not been measured. Time advances with CPU execution, never with
register reads. No device depends on a firmware PC or a desired boot outcome.
"""
from __future__ import annotations

from collections import deque
import heapq
from typing import Callable

from micci import MicciController, MicciError, RegisterDevice
from i2c import I2CController, I2CError
from gpio import GPIOController, GPIOError
from panel import PanelPort, PanelError, SerialShiftPanel
from spi import SPIController, SPIError
from usb import UsbDeviceController, USBError
from sdio import SdioHostController, SDIOError


UART_CONTROL = 0xBD0B3C00
UART_TX = 0xBD0B3C06
UART_RX = 0xBD0B3C0C
UART_STATUS = 0xBD0B3C0E
TIMER = 0xBD040814
PLL_CONTROL = 0xBD041400
PLL_STATUS = 0xBD041424
SECOND_PLL_RESET = 0xBD061C08
SECOND_PLL_CONTROL = 0xBD042C04
SECOND_PLL_CONFIG = 0xBD042C08
SECOND_PLL_LOCK = 1 << 23
SDRAM_CONTROL = 0xBD062042
SDRAM_STATUS = 0xBD062048
BUS_MONITOR_ENABLE = 0xBD062800
BUS_MONITOR_STATUS = 0xBD062802
BUS_MONITOR_ADDRESS = 0xBD06280C
RESET_RETENTION = 0xBD04083C
WATCHDOG_CONTROL = 0xBD040400
WATCHDOG_COUNT = 0xBD040404
WATCHDOG_RELOAD = 0xBD040408


class HardwareError(RuntimeError):
    """An access needs hardware behavior that has not been modeled."""


_PERIPHERAL_ERRORS = (
    SPIError, PanelError, GPIOError, I2CError, MicciError, USBError, SDIOError,
)


class BootHardware:
    """Register bank, virtual clock, PLL, SDRAM calibration, and UART.

    ``timer_step`` is the number of virtual timer ticks per instruction. It is
    retained as a configuration knob, with a default of one; these ticks are
    not calibrated to real seconds. Lock/calibration/frame durations use the
    same units and do not scale with how frequently firmware polls a register.

    Only UART status bit 8 (transmit ready) is established. No speculative RX
    ready, interrupt, framing, or baud-generator bits are returned. A single
    transmit slot models backpressure; the actual FIFO depth is still unknown.
    """

    def __init__(self, timer_step: int = 1, *, us_ticks: int = 4,
                 pll_lock_ticks: int = 20,
                 sdram_calibration_ticks: int = 256, uart_frame_ticks: int = 160,
                 asic_id: int = 0x00400000,
                 gpio_inputs: dict[int, int] | None = None,
                 panel_input_word: int | None = None,
                 serial_panel_inputs: int | None = None,
                 serial_panel_tail_bit: int | None = None,
                 spi_profile: str | None = None,
                 spi_miso_pull: int | None = None,
                 spi_window_registers: tuple[int, int, int] | None = None,
                 micci_devices: dict[int, dict[tuple[int, int], RegisterDevice]] | None = None,
                  usb_dma_read: Callable[[int, int], bytes] | None = None,
                  usb_dma_write: Callable[[int, bytes], None] | None = None,
                  sdio_dma_read: Callable[[int, int], bytes] | None = None,
                  sdio_dma_write: Callable[[int, bytes], None] | None = None):
        for name, value in (
            ('timer_step', timer_step), ('us_ticks', us_ticks),
            ('pll_lock_ticks', pll_lock_ticks),
            ('sdram_calibration_ticks', sdram_calibration_ticks),
            ('uart_frame_ticks', uart_frame_ticks),
        ):
            if not isinstance(value, int) or not 0 < value <= 0xFFFFFFFF:
                raise ValueError(f'{name} must be between 1 and 0xffffffff')
        if not isinstance(asic_id, int) or not 0 <= asic_id <= 0xFFFFFFFF:
            raise ValueError('asic_id must be a 32-bit unsigned integer')
        self.timer_step = timer_step
        self.us_ticks = us_ticks
        self.pll_lock_ticks = pll_lock_ticks
        self.sdram_calibration_ticks = sdram_calibration_ticks
        self.uart_frame_ticks = uart_frame_ticks
        self.asic_id = asic_id
        self.ticks = 0
        self._timer_offset = 0
        self.pll_locked = False
        self.second_pll_locked = False
        self.sdram_ready = False
        self._pll_generation = 0
        self._second_pll_generation = 0
        self._second_pll_reset_released = False
        self._sdram_generation = 0
        self._event_sequence = 0
        self._events: list[tuple[int, int, Callable[[], None]]] = []
        self._next_deadline = 0xFFFFFFFFFFFFFFFF
        self._state_gen = 1
        self._observer = None
        self.uart_tx_pending: int | None = None
        self._uart_tx_fifo: deque[int] = deque()
        self.uart_rx: deque[int] = deque()
        self.uart1_rx: deque[int] = deque()
        self._uart_output = bytearray()
        self.timer_pending = 0
        self.timer_reload = [0] * 8
        self.timer_deadline: list[int | None] = [None] * 8
        self._timer_generation = [0] * 8
        self._timer_event_seq = [0] * 8
        self._cancelled_seqs: set[int] = set()
        self._sec_timer_generation = [0] * 3
        self._sec_timer_pending = 0
        self._wdog_generation = [0] * 2
        self._wdog_event_seq = [0] * 2
        self.irq_raw_sources = 0
        self._max_irq_claim = 0
        self._max_fiq_claim = 0
        self._nt_irq_claim = 0
        self._nt_fiq_claim = 0
        self._uart0_irq_claim = 0
        self._uart0_cfg_ok = False
        self._irq18_claim = 0
        self._fiq18_claim = 0
        self._irq19_claim = 0
        self._fiq19_claim = 0
        self._irq_claim = 0
        self._fiq_claim = 0
        self.interrupt_mask = 0
        self._irq_cfg_dirty = True
        self._irq_threshold = 0
        self._irq_unmasked = 0
        self._irq_fiq_mask = 0
        self._irq_prio_packed = 0
        self._timer_mask_dirty = True
        self._timer_group_masks = [0, 0, 0, 0, 0]
        self._timer_mask_union = 0
        self._timer_active_mask = 0
        self._timer_reload_zero_mask = 0xFF
        self._cc8_value = 0
        self._timer_callbacks = [self._make_timer_callback(ch) for ch in range(8)]
        self._wdog_callbacks = [self._make_wdog_callback(st) for st in range(2)]

        def on_uart_transmitted() -> None:
            if self.uart_tx_pending is not None:
                self._uart_output.append(self.uart_tx_pending)
                self.uart_tx_pending = None
            if self._uart_tx_fifo:
                self._start_uart_tx()
            else:
                self._update_uart_irqs()

        on_uart_transmitted._is_uart = True
        self._uart_tx_callback = on_uart_transmitted

        if BootHardware._BASE_REGISTER_BYTES is None:
            BootHardware._init_static_tables()
        self.read_only = BootHardware._BASE_READ_ONLY
        self.extra_widths = BootHardware._BASE_EXTRA_WIDTHS
        self._register_bytes = BootHardware._BASE_REGISTER_BYTES.copy()
        self.micci = {
            base: MicciController(
                base, self.schedule, (micci_devices or {}).get(base),
                invalidate_mmio=self._invalidate_mmio_slot,
            )
            for base in (0xBD300800, 0xBD300C00)
        }
        self.i2c = I2CController(0xBD065800, self.schedule,
                                 on_interrupt=lambda active: self.set_irq_level(3, active),
                                 invalidate_mmio=self._invalidate_mmio_slot)
        self.gpio = GPIOController(external_inputs=gpio_inputs,
                                   invalidate_mmio=self._invalidate_mmio_slot)
        endpoint = (SerialShiftPanel(raw_inputs=serial_panel_inputs, tail_bit=serial_panel_tail_bit)
                    if serial_panel_inputs is not None else None)
        self.panel = PanelPort(panel_input_word, endpoint=endpoint)
        self.spi = SPIController(schedule=self.schedule, profile=spi_profile,
                                 miso_pull=spi_miso_pull,
                                 window_registers=spi_window_registers)
        self.usb = UsbDeviceController(set_irq=self.set_irq_level,
                                       dma_read=usb_dma_read,
                                       dma_write=usb_dma_write)
        self.sdio = SdioHostController(
            set_irq=self.set_irq_level,
            dma_read=sdio_dma_read if sdio_dma_read is not None else usb_dma_read,
            dma_write=sdio_dma_write if sdio_dma_write is not None else usb_dma_write,
        )
        controllers = (*self.micci.values(), self.i2c, self.gpio, self.panel,
                       self.spi, self.usb, self.sdio)
        self._controllers = controllers
        if BootHardware._CACHED_ALL_WIDTHS is None:
            all_widths = BootHardware._BASE_WIDTHS.copy()
            periph_idx: dict[int, int] = {}
            for idx, controller in enumerate(controllers):
                all_widths.update(controller.widths)
                for address in controller.widths:
                    periph_idx[address] = idx
            write_valid = (
                set(all_widths.items())
                | {(addr, sz) for addr, sizes in BootHardware._BASE_EXTRA_WIDTHS.items() for sz in sizes}
            )
            read_valid = (
                (write_valid | set(BootHardware._BASE_READ_ONLY.items()))
                - {(UART_TX, 2), (0xBD0B4006, 2)}
            )
            BootHardware._CACHED_ALL_WIDTHS = all_widths
            BootHardware._CACHED_PERIPHERAL_IDX = periph_idx
            BootHardware._CACHED_WRITE_VALID = write_valid
            BootHardware._CACHED_READ_VALID = read_valid
            BootHardware._CACHED_PAGES = sorted({a & ~0xFFF for a in all_widths.keys() | BootHardware._BASE_READ_ONLY.keys()})
        self._peripheral_idx = BootHardware._CACHED_PERIPHERAL_IDX
        self.widths = BootHardware._CACHED_ALL_WIDTHS
        self._write_valid: set[tuple[int, int]] = BootHardware._CACHED_WRITE_VALID
        self._read_valid: set[tuple[int, int]] = BootHardware._CACHED_READ_VALID

    _BASE_WIDTHS: dict[int, int] = {}
    _BASE_READ_ONLY: dict[int, int] = {}
    _BASE_EXTRA_WIDTHS: dict[int, set[int]] = {}
    _BASE_REGISTER_BYTES: dict[int, int] | None = None
    _CACHED_ALL_WIDTHS: dict[int, int] | None = None
    _CACHED_PERIPHERAL_IDX: dict[int, int] = {}
    _CACHED_WRITE_VALID: set[tuple[int, int]] | None = None
    _CACHED_READ_VALID: set[tuple[int, int]] | None = None
    _CACHED_PAGES: list[int] | None = None

    @classmethod
    def _init_static_tables(cls) -> None:
        widths = {a: 4 for a in range(0xBD041400, 0xBD041424, 4)}
        widths.update({a: 4 for a in (
            0xBD040010, 0xBD040014, 0xBD040018, 0xBD04001C,
            TIMER, 0xBD04100C, SECOND_PLL_CONTROL, SECOND_PLL_CONFIG, 0xBD042C48, 0xBD042C4C,
            0xBD060C10, 0xBD060C14, 0xBD060C18, 0xBD060C34,
            0xBD062400, 0xBD062404,
            0xBD06FC14, 0xBD06FC18, 0xBD06FC1C,
            0xBD066C08, 0xBD066C0C,
            0xBD067000, 0xBD067010, 0xBD067014, 0xBD067020, 0xBD067024,
            0xBD067474, 0xBD067478, 0xBD067480, 0xBD067484,
            0xBD067800, 0xBD067804, 0xBD067808, 0xBD06780C, 0xBD067810, 0xBD067814, 0xBD06781C,
            0xBD068014, 0xBD06840C, 0xBD068424, 0xBD068428,
            0xBD06B000, 0xBD06B004, 0xBD06B008, 0xBD06B018, 0xBD06B01C, 0xBD06B020, 0xBD06B024, 0xBD06B028, 0xBD06B02C,
            0xBD070C00, 0xBD070C04, 0xBD070C08, 0xBD070C0C,
            0xBD070C10, 0xBD070C14, 0xBD070C1C,
            0xBD071404, 0xBD071408,
            WATCHDOG_COUNT, WATCHDOG_RELOAD,
            0xBD0B1008, 0xBD0B1010,
            0xBD300038, 0xBD30003C, 0xBD300040, 0xBD300044,
            0xBD300804, 0xBD300C08, 0xBD300808, 0xBD300C04,
            0xBD302010, 0xBD302018, 0xBD302410, 0xBD302418,
            0xBD302804, 0xBD302C00, 0xBD302C04,
            0xBD303800, 0xBD30380C, 0xBD303818,
        )})
        widths.update({0xBD040C00 + a: 4 for a in (
            *range(0x00, 0x10, 4), *range(0x30, 0x40, 4),
            *range(0x40, 0x50, 4), *range(0x70, 0x80, 4),
            *range(0x80, 0x90, 4), *range(0xD0, 0x100, 4),
        )})
        widths.update({a: 2 for a in (
            0xBD040802, 0xBD040804, 0xBD040806, 0xBD040808, 0xBD04080A,
            0xBD04080C, 0xBD04080E, 0xBD040812,
            *(0xBD04081C + 2 * i for i in range(16)),
            0xBD040CB0, 0xBD040CB8, 0xBD040CC0, 0xBD040CC4, 0xBD040CC8,
            0xBD041000, 0xBD041002, 0xBD041004, 0xBD041006, 0xBD041008, 0xBD041010,
            0xBD042800,
            RESET_RETENTION, WATCHDOG_CONTROL, BUS_MONITOR_ENABLE, 0xBD060002,
            0xBD06080E, 0xBD060810, 0xBD060C22, 0xBD060C38, 0xBD060C60,
            *(0xBD061C00 + 2 * i for i in range(8)),
            0xBD06081A, 0xBD06081C, 0xBD06081E, 0xBD060820,
            0xBD060822, 0xBD060824, 0xBD060826, 0xBD06082C,
            0xBD060842, 0xBD060844, 0xBD06084A, 0xBD06084C, 0xBD060850, 0xBD060852,
            0xBD06240C,
            0xBD066402, 0xBD066404, 0xBD066406, 0xBD066408,
            0xBD066C00, 0xBD066C02, 0xBD066C04, 0xBD066C0E, 0xBD066C10, 0xBD066C12,
            0xBD067004, 0xBD067006, 0xBD06700A, 0xBD06700C, 0xBD067018, 0xBD06701A, 0xBD06701C, 0xBD06701E,
            *(0xBD067400 + 2 * i for i in range(0x39)),
            0xBD06747C, 0xBD067488, 0xBD06748A, 0xBD06748C,
            0xBD067818, 0xBD06781A, 0xBD067820,
            0xBD068000, 0xBD068002, 0xBD068004, 0xBD068006, 0xBD068008, 0xBD06800A, 0xBD06800C, 0xBD06800E,
            0xBD068010, 0xBD068012, 0xBD068018, 0xBD06801A, 0xBD06801C, 0xBD06801E, 0xBD068020,
            0xBD068400, 0xBD068402, 0xBD068404, 0xBD068406, 0xBD068408, 0xBD068410, 0xBD068412, 0xBD068414,
            0xBD068416, 0xBD068418, 0xBD06841A, 0xBD06841C, 0xBD06841E, 0xBD068420, 0xBD068422, 0xBD06842A, 0xBD06842C,
            *(0xBD068800 + 2 * i for i in range(14)),
            *(0xBD068C00 + 2 * i for i in range(14)),
            *(0xBD069800 + 2 * i for i in range(5)),
            0xBD06B00C, 0xBD06B00E, 0xBD06B010, 0xBD06B012, 0xBD06B014, 0xBD06B016,
            0xBD06B400,
            *(0xBD06FC00 + 2 * i for i in range(10)),
            *(0xBD070404 + 2 * i for i in range(15)),
            0xBD070C18, 0xBD070C1A, 0xBD070C20, 0xBD070CA8, 0xBD070CAC,
            *(0xBD071000 + 2 * i for i in range(0x28)),
            0xBD071C00, 0xBD071C18, 0xBD071C1A,
            0xBD072000, 0xBD072002, 0xBD072004,
            0xBD073000, 0xBD073002,
            0xBD073412, 0xBD073414, 0xBD073416,
            0xBD0B0400,
            0xBD0B1000, 0xBD0B1002, 0xBD0B1004, 0xBD0B1006, 0xBD0B100C, 0xBD0B100E,
            UART_CONTROL, 0xBD0B3C04, UART_TX, 0xBD0B3C08,
            0xBD0B4000, 0xBD0B4004, 0xBD0B4006, 0xBD0B4008,
            0xBD0B4C00, 0xBD0B4C02,
            *(0xBD0B5000 + (ch << 10) + off for ch in range(4) for off in (0, 2, 4, 6)),
            *(0xBD0B5C00 + 2 * i for i in range(0x12)),
            *(0xBD0B6000 + 2 * i for i in range(4)),
            *(0xBD0B6800 + 2 * i for i in range(0x10)),
            *(0xBD0B6C00 + 2 * i for i in range(0x10)),
            *(0xBD0B7000 + 2 * i for i in range(0x12)),
            0xBD300000, 0xBD300002, 0xBD300004, 0xBD30000E,
            0xBD300010, 0xBD300012, 0xBD300014, 0xBD300018,
            0xBD30001A, 0xBD30001C, 0xBD300026, 0xBD300028,
            0xBD30002A, 0xBD30002C, 0xBD300030, 0xBD300034,
            0xBD300054, 0xBD30005A, 0xBD30005C, 0xBD30005E,
            0xBD300060, 0xBD300064, 0xBD300066, 0xBD300068,
            0xBD30006A, 0xBD30006C,
            0xBD300800, 0xBD300C00,
            *(0xBD301000 + 2 * i for i in range(9)),
            0xBD302000, 0xBD302002, 0xBD302004, 0xBD302024, 0xBD302026,
            0xBD302800, 0xBD302802, 0xBD302C02, 0xBD303C00,
            *(0xBD304000 + 2 * i for i in range(8)),
        )})
        widths.update({a: 1 for a in range(0xBD0B0402, 0xBD0B0408)})
        widths.update({0xBD062000 + a: 2 for a in (
            0x00, 0x02, 0x04, 0x06, 0x08, 0x0A, 0x0C, 0x0E,
            0x10, 0x12, 0x14, 0x16, 0x22, 0x28, 0x2A, 0x2E,
            0x30, 0x42, 0x44, 0x46, 0x4C, 0x4E, 0x50, 0x52,
            0x56, 0x58, 0x5A, 0x5C,
        )})
        read_only = {
            0xBD040000: 4, 0xBD040010: 4, 0xBD040014: 4, 0xBD040018: 4, 0xBD04001C: 4,
            0xBD040800: 2, 0xBD040818: 4, PLL_STATUS: 4,
            SDRAM_STATUS: 2, UART_RX: 2, UART_STATUS: 2, 0xBD0B3C10: 2,
            0xBD0B400C: 2, 0xBD0B400E: 2, 0xBD0B4010: 2,
            BUS_MONITOR_STATUS: 2, BUS_MONITOR_ADDRESS: 4,
        }
        # The dump uses both widths at this pin-control address. Byte backing
        # makes overlapping views coherent on the big-endian memory bus.
        extra_widths = {
            0xBD04100C: {1, 2}, 0xBD04100D: {1}, 0xBD04100E: {1, 2}, 0xBD04100F: {1},
            0xBD061C08: {4}, 0xBD066C0C: {2}, 0xBD068428: {2},
            0xBD0B0402: {2}, 0xBD0B0404: {2}, 0xBD0B0406: {2},
            0xBD302804: {2}, 0xBD302C00: {2}, 0xBD302C04: {2}, 0xBD303800: {2},
        }
        reg_bytes: dict[int, int] = {
            addr + i: 0
            for addr, sz in (*widths.items(), *read_only.items())
            for i in range(sz)
        }
        for addr, sizes in extra_widths.items():
            for sz in sizes:
                for i in range(sz):
                    reg_bytes.setdefault(addr + i, 0)
        reg_bytes[0xBD04080F] = 1
        for addr in range(0xBD040C40, 0xBD040C50):
            reg_bytes[addr] = 0xFF
        reg_bytes[0xBD0B0405] = 0x03
        reg_bytes[0xBD0B3C04] = 0xFF
        reg_bytes[0xBD0B3C05] = 0xFF
        reg_bytes[0xBD0B4004] = 0xFF
        reg_bytes[0xBD0B4005] = 0xFF
        cls._BASE_WIDTHS = widths
        cls._BASE_READ_ONLY = read_only
        cls._BASE_EXTRA_WIDTHS = extra_widths
        cls._BASE_REGISTER_BYTES = reg_bytes

    def _store_raw(self, address: int, size: int, value: int) -> None:
        rb = self._register_bytes
        if size == 2:
            rb[address] = (value >> 8) & 0xFF
            rb[address + 1] = value & 0xFF
        elif size == 4:
            rb[address] = (value >> 24) & 0xFF
            rb[address + 1] = (value >> 16) & 0xFF
            rb[address + 2] = (value >> 8) & 0xFF
            rb[address + 3] = value & 0xFF
        else:
            rb.update(enumerate(value.to_bytes(size, 'big'), address))

    @property
    def timer(self) -> int:
        return (self.ticks + self._timer_offset) & 0xFFFFFFFF

    @property
    def irq_asserted(self) -> bool:
        return self._irq_claim != 0

    @property
    def fiq_asserted(self) -> bool:
        return self._fiq_claim != 0

    def _invalidate_mmio_slot(self, address: int, size: int) -> None:
        if self._observer is not None:
            self._observer.invalidate_mmio_slot(address, size)

    def set_irq_level(self, source: int, active: bool) -> None:
        if not 0 <= source < 128:
            return
        bit = 1 << source
        old = self.irq_raw_sources
        if active:
            self.irq_raw_sources |= bit
        else:
            self.irq_raw_sources &= ~bit
        if self.irq_raw_sources != old:
            obs = self._observer
            if obs is not None and not obs._synchronizing:
                if obs.state.ack_802_dirty or obs.state.timer_expired_mask:
                    obs.flush_ack_802()
                if obs.state.uart0_mask_dirty:
                    obs.flush_uart0_mask()
            self._recompute_interrupts()
            if obs is not None and not obs._synchronizing:
                obs.sync_irqs()

    def _recompute_interrupts(self) -> None:
        if self._irq_cfg_dirty:
            self._irq_cfg_dirty = False
            obs = self._observer
            if obs is not None and obs.state.cc8_dirty:
                obs.state.cc8_dirty = 0
                self._store_raw(0xBD040CC8, 2, obs.state.cc8_value)
            self._cc8_value = self._stored(0xBD040CC8, 2)
            self._irq_threshold = self._cc8_value & 7
            unmasked = 0
            fiq_mask = 0
            for bank in range(4):
                shift = bank << 5
                unmasked |= ((~self._stored(0xBD040C4C - 4 * bank, 4)) & 0xFFFFFFFF) << shift
                fiq_mask |= self._stored(0xBD040C0C - 4 * bank, 4) << shift
            prio_packed = 0
            for idx in range(8):
                prio_packed |= self._stored(0xBD040CFC - 4 * idx, 4) << (idx << 5)
            nonzero_prio = fiq_mask
            for s in range(128):
                if (prio_packed >> (s << 1)) & 3:
                    nonzero_prio |= 1 << s
            self._irq_unmasked = unmasked & nonzero_prio
            self._irq_fiq_mask = fiq_mask
            self._irq_prio_packed = prio_packed
            prio18 = (prio_packed >> 36) & 3
            prio19 = (prio_packed >> 38) & 3
            prio38 = (prio_packed >> 76) & 3
            if (self._irq_unmasked >> 18) & 1:
                if (fiq_mask >> 18) & 1:
                    self._irq18_claim = 0
                    self._fiq18_claim = ((4 | prio18) << 8) | 18
                else:
                    self._irq18_claim = (prio18 << 8) | 18
                    self._fiq18_claim = 0
            else:
                self._irq18_claim = 0
                self._fiq18_claim = 0
            if (self._irq_unmasked >> 19) & 1:
                if (fiq_mask >> 19) & 1:
                    self._irq19_claim = 0
                    self._fiq19_claim = ((4 | prio19) << 8) | 19
                else:
                    self._irq19_claim = (prio19 << 8) | 19
                    self._fiq19_claim = 0
            else:
                self._irq19_claim = 0
                self._fiq19_claim = 0
            if ((self._irq_unmasked >> 38) & 1) and not ((fiq_mask >> 38) & 1):
                self._uart0_irq_claim = (prio38 << 8) | 38
            else:
                self._uart0_irq_claim = 0
            self._uart0_cfg_ok = not (((self._irq_unmasked & fiq_mask) >> 38) & 1)
        raw = self.irq_raw_sources
        if not raw:
            self._max_irq_claim = 0
            self._max_fiq_claim = 0
            self._nt_irq_claim = 0
            self._nt_fiq_claim = 0
            self._irq_claim = 0
            self._fiq_claim = 0
            self.interrupt_mask = 0
            return
        temp = raw & self._irq_unmasked
        if not temp:
            self._max_irq_claim = 0
            self._max_fiq_claim = 0
            self._nt_irq_claim = 0
            self._nt_fiq_claim = 0
            self._irq_claim = 0
            self._fiq_claim = 0
            self.interrupt_mask = 0
            return
        threshold = self._irq_threshold
        fiq_mask = self._irq_fiq_mask
        prio_packed = self._irq_prio_packed
        best_irq_prio = 0
        best_irq_claim = 0
        best_fiq_prio = 0
        best_fiq_claim = 0
        nt_irq_prio = 0
        nt_irq_claim = 0
        nt_fiq_prio = 0
        nt_fiq_claim = 0
        while temp:
            lsb = temp & -temp
            source = lsb.bit_length() - 1
            temp ^= lsb
            fiq_class = (fiq_mask >> source) & 1
            prio_low = (prio_packed >> (source << 1)) & 3
            prio = (fiq_class << 2) | prio_low
            if prio < 4:
                if prio > best_irq_prio:
                    best_irq_prio = prio
                    best_irq_claim = (prio << 8) | source
                if not (18 <= source <= 22) and prio > nt_irq_prio:
                    nt_irq_prio = prio
                    nt_irq_claim = (prio << 8) | source
            else:
                if prio > best_fiq_prio:
                    best_fiq_prio = prio
                    best_fiq_claim = (prio << 8) | source
                if not (18 <= source <= 22) and prio > nt_fiq_prio:
                    nt_fiq_prio = prio
                    nt_fiq_claim = (prio << 8) | source
        self._max_irq_claim = best_irq_claim
        self._max_fiq_claim = best_fiq_claim
        self._nt_irq_claim = nt_irq_claim
        self._nt_fiq_claim = nt_fiq_claim
        self._irq_claim = best_irq_claim if best_irq_prio > threshold else 0
        self._fiq_claim = best_fiq_claim if best_fiq_prio > threshold else 0
        self.interrupt_mask = (1 if self._irq_claim else 0) | (2 if self._fiq_claim else 0)

    def _apply_irq_threshold(self, value: int) -> None:
        self._cc8_value = value
        if self._irq_cfg_dirty:
            self._recompute_interrupts()
            return
        threshold = value & 7
        self._irq_threshold = threshold
        if not self.irq_raw_sources:
            return
        self._irq_claim = self._max_irq_claim if (self._max_irq_claim >> 8) > threshold else 0
        self._fiq_claim = self._max_fiq_claim if (self._max_fiq_claim >> 8) > threshold else 0
        self.interrupt_mask = (1 if self._irq_claim else 0) | (2 if self._fiq_claim else 0)

    def _sync_timer_masks(self) -> list[int]:
        masks = self._timer_group_masks
        if self._timer_mask_dirty:
            self._timer_mask_dirty = False
            for g in range(5):
                masks[g] = self._stored(0xBD040804 + 2 * g, 2)
            self._timer_mask_union = masks[0] | masks[1] | masks[2] | masks[3] | masks[4]
        return masks

    def _update_timer_irqs(self) -> None:
        masks = self._sync_timer_masks()
        pending = self.timer_pending
        bits = (
            ((1 << 18) if (pending & masks[0]) else 0)
            | ((1 << 19) if (pending & masks[1]) else 0)
            | ((1 << 20) if (pending & masks[2]) else 0)
            | ((1 << 21) if (pending & masks[3]) else 0)
            | ((1 << 22) if (pending & masks[4]) else 0)
        )
        old = self.irq_raw_sources
        new = (old & ~(0x1F << 18)) | bits
        if new != old:
            self.irq_raw_sources = new
            self._recompute_interrupts()
        if self._observer is not None and not self._observer._synchronizing:
            self._observer.sync_timer_irqs()

    def _prune_cancelled_events(self) -> None:
        events = self._events
        cancelled = self._cancelled_seqs
        if cancelled:
            while events and events[0][1] in cancelled:
                cancelled.discard(heapq.heappop(events)[1])
            if len(cancelled) > 32 and len(cancelled) * 2 >= len(events):
                events[:] = [ev for ev in events if ev[1] not in cancelled]
                heapq.heapify(events)
                cancelled.clear()
        self._next_deadline = events[0][0] if events else 0xFFFFFFFFFFFFFFFF

    def _cancel_timer_channel(self, channel: int) -> None:
        old_seq = self._timer_event_seq[channel]
        if old_seq:
            self._cancelled_seqs.add(old_seq)
            self._timer_event_seq[channel] = 0
        self._timer_generation[channel] += 1
        self.timer_deadline[channel] = None
        self._timer_active_mask &= ~(1 << channel)

    def _make_timer_callback(self, channel: int) -> Callable[[], None]:
        bit = 1 << channel

        def expire() -> None:
            self._timer_event_seq[channel] = 0
            self.timer_pending |= bit
            reload_val = self.timer_reload[channel]
            if reload_val > 0:
                self._schedule_timer_channel(channel, reload_val + 1)
            else:
                self.timer_deadline[channel] = None
                self._timer_active_mask &= ~bit
            self._update_timer_irqs()

        expire._is_timer = True
        expire._is_timer_ch = True
        return expire

    def _make_wdog_callback(self, stage: int) -> Callable[[], None]:
        irq = 5 + stage

        def wdog_expire() -> None:
            self._wdog_event_seq[stage] = 0
            self.set_irq_level(irq, True)

        wdog_expire._is_timer = True
        return wdog_expire

    def _schedule_timer_channel(self, channel: int, delay_us: int) -> None:
        old_seq = self._timer_event_seq[channel]
        if old_seq:
            self._cancelled_seqs.add(old_seq)
        self._timer_generation[channel] += 1
        delay_ticks = max(1, delay_us) * self.timer_step * self.us_ticks
        self.timer_deadline[channel] = self.ticks + delay_ticks
        self._timer_active_mask |= 1 << channel
        self.schedule(delay_ticks, self._timer_callbacks[channel])
        self._timer_event_seq[channel] = self._event_sequence

    def _update_uart_irqs(self) -> None:
        obs = self._observer
        if obs is not None and obs.state.uart0_mask_dirty:
            obs.state.uart0_mask_dirty = 0
            self._store_raw(0xBD0B3C04, 2, obs.state.uart0_mask)
        fifo_enabled = self._stored(0xBD0B4C00, 2) != 0
        mask0 = self._stored(0xBD0B3C04, 2)
        tx0_ready = self.uart_tx_pending is None and not self._uart_tx_fifo
        rx0_ready = bool(self.uart_rx)
        if obs is not None:
            obs.state.uart_fifo_enabled = 1 if fifo_enabled else 0
            obs.state.uart0_mask = mask0
            obs.state.uart0_state = ((0x40 if tx0_ready else 0) | (0x01 if rx0_ready else 0)) if fifo_enabled else 0
            if obs.state.uart_tx_active != 1:
                obs.state.uart_tx_active = 0 if tx0_ready else 2
        irq0 = fifo_enabled and (
            (not (mask0 & 0x0040) and tx0_ready)
            or (not (mask0 & 0x0001) and rx0_ready)
        )
        self.set_irq_level(38, irq0)

        mask1 = self._stored(0xBD0B4004, 2)
        rx1_ready = bool(self.uart1_rx)
        irq1 = fifo_enabled and (
            not (mask1 & 0x0040)
            or (not (mask1 & 0x0001) and rx1_ready)
        )
        self.set_irq_level(39, irq1)

    @property
    def pages(self) -> list[int]:
        if (BootHardware._CACHED_PAGES is not None
                and len(self.widths) == len(BootHardware._CACHED_ALL_WIDTHS)
                and len(self.read_only) == len(BootHardware._BASE_READ_ONLY)):
            return list(BootHardware._CACHED_PAGES)
        return sorted({a & ~0xFFF for a in self.widths.keys() | self.read_only.keys()})

    def schedule(self, delay_ticks: int, callback: Callable[[], None]) -> None:
        """Schedule a device transition relative to the current virtual time."""
        if not isinstance(delay_ticks, int) or delay_ticks < 0:
            raise ValueError('delay_ticks must be a nonnegative integer')
        self._event_sequence += 1
        deadline = self.ticks + delay_ticks
        events = self._events
        cancelled = self._cancelled_seqs
        if cancelled:
            while events and events[0][1] in cancelled:
                cancelled.discard(heapq.heappop(events)[1])
        heapq.heappush(events, (deadline, self._event_sequence, callback))
        self._next_deadline = events[0][0]

    def advance(self, instructions: int = 1) -> None:
        """Advance devices by executed instructions, including idle polling."""
        if not isinstance(instructions, int) or instructions < 0:
            raise ValueError('instructions must be a nonnegative integer')
        target = self.ticks + instructions * self.timer_step
        events = self._events
        cancelled = self._cancelled_seqs
        while events and events[0][0] <= target:
            deadline, seq, callback = heapq.heappop(events)
            if seq in cancelled:
                cancelled.discard(seq)
                continue
            if getattr(callback, '_is_uart', False):
                if self._observer is not None:
                    self._observer.invalidate_mmio_slot(0xBD0B3C0E, 2)
            elif not getattr(callback, '_is_timer', False):
                self._state_gen += 1
            self.ticks = deadline
            callback()
        self._prune_cancelled_events()
        self.ticks = target

    def drain_uart(self) -> bytes:
        """Return and remove bytes whose transmit frames have completed."""
        output = bytes(self._uart_output)
        self._uart_output.clear()
        return output

    def queue_rx(self, data: bytes) -> None:
        """Queue receive-register bytes."""
        obs = self._observer
        if obs is not None and not obs._synchronizing:
            obs.sync_before_host_event()
        if obs is not None:
            obs.invalidate_mmio_slot(0xBD0B3C0C, 2)
            obs.invalidate_mmio_slot(0xBD0B3C0E, 2)
        self.uart_rx.extend(data)
        self._update_uart_irqs()
        if obs is not None and not obs._synchronizing:
            obs.refresh()

    def _stored(self, address: int, size: int) -> int:
        rb = self._register_bytes
        try:
            if size == 2:
                return (rb[address] << 8) | rb[address + 1]
            if size == 4:
                return ((rb[address] << 24) | (rb[address + 1] << 16)
                        | (rb[address + 2] << 8) | rb[address + 3])
            if size == 1:
                return rb[address]
        except KeyError:
            pass
        rb_get = rb.get
        return int.from_bytes(bytes(rb_get(address + i, 0)
                                    for i in range(size)), 'big')

    def _valid_width(self, address: int, size: int, *, reading: bool) -> bool:
        return (address, size) in (self._read_valid if reading else self._write_valid)

    def read(self, address: int, size: int) -> int:
        if address == TIMER and size == 4:
            return (self.ticks + self._timer_offset) & 0xFFFFFFFF
        p_idx = self._peripheral_idx.get(address)
        if p_idx is not None:
            if address == 0xBD065808 and self._observer is not None:
                self._observer.invalidate_mmio_slot(0xBD065800, 2)
            try:
                return self._controllers[p_idx].read(address, size)
            except _PERIPHERAL_ERRORS as exc:
                raise HardwareError(str(exc)) from exc
        if (address, size) not in self._read_valid:
            raise HardwareError(f'unmodeled MMIO read: {address:#010x}, size {size}')
        if address == 0xBD040000:
            return self.asic_id
        if address == 0xBD040800:
            return self.timer_pending
        if address == 0xBD301010:
            return self._sec_timer_pending
        if address == 0xBD040818:
            scale = max(1, self._stored(0xBD04080E, 2))
            return (self.timer * scale) & 0xFFFFFFFF
        if 0xBD04081C <= address < 0xBD04083C and (address - 0xBD04081C) & 2:
            channel = (address - 0xBD04081E) // 4
            deadline = self.timer_deadline[channel]
            if deadline is None or deadline <= self.ticks:
                return 0
            return ((deadline - self.ticks) // (self.timer_step * self.us_ticks)) & 0xFFFF
        if address == 0xBD040CB8:
            return self._irq_claim
        if address == 0xBD040CC0:
            return self._fiq_claim
        if address == PLL_STATUS:
            return 0x10000 if self.pll_locked else 0
        if address == SECOND_PLL_CONTROL:
            return self._stored(address, size) | (SECOND_PLL_LOCK if self.second_pll_locked else 0)
        if address == SDRAM_STATUS:
            return 0x4000 if self.sdram_ready else 0
        if address in (UART_STATUS, 0xBD0B400E):
            fifo_enabled = self._stored(0xBD0B4C00, 2) != 0
            tx_ready = (self.uart_tx_pending is None and not self._uart_tx_fifo) if address == UART_STATUS else True
            rx_queue = self.uart_rx if address == UART_STATUS else self.uart1_rx
            status = (0x0180 if fifo_enabled else 0x0100) if tx_ready else 0
            if fifo_enabled and rx_queue:
                status |= 0x0004
            return status
        if address in (UART_RX, 0xBD0B400C):
            if address == UART_RX and self._observer is not None:
                self._observer.invalidate_mmio_slot(0xBD0B3C0E, 2)
            else:
                self._state_gen += 1
            rx_queue = self.uart_rx if address == UART_RX else self.uart1_rx
            val = rx_queue.popleft() if rx_queue else 0
            self._update_uart_irqs()
            return val
        if address == 0xBD0B3C04 and self._observer is not None and self._observer.state.uart0_mask_dirty:
            self._observer.flush_uart0_mask()
        return self._stored(address, size)

    def _start_uart_tx(self) -> None:
        if self.uart_tx_pending is not None or not self._uart_tx_fifo:
            return
        self.uart_tx_pending = self._uart_tx_fifo.popleft()
        self._update_uart_irqs()
        self.schedule(self.uart_frame_ticks, self._uart_tx_callback)

    def sync_panel_gpio_leds(self) -> None:
        """Synchronize direct GPIO front-panel LEDs (power_led on GPIO 28, wireless_led on GPIO 0)."""
        self.panel.set_gpio_led(
            'power_led',
            self.gpio.directions[28] == 1 and self.gpio.output_latches[28] == 0,
        )
        self.panel.set_gpio_led(
            'wireless_led',
            self.gpio.directions[0] == 1 and self.gpio.output_latches[0] == 0,
        )

    def write(self, address: int, size: int, value: int) -> None:
        obs = self._observer
        if address == 0xBD0B1418 and size == 2:
            if obs is not None and self.panel.endpoint is not None:
                obs.invalidate_mmio_slot(0xBD0B1410, 2)
        elif address == UART_TX and size == 2:
            if obs is not None:
                obs.invalidate_mmio_slot(0xBD0B3C0E, 2)
        elif address == 0xBD0B4C00 and size == 2:
            if obs is not None:
                obs.invalidate_mmio_slot(0xBD0B3C0E, 2)
                obs.invalidate_mmio_slot(0xBD0B400E, 2)
        elif address in (
            0xBD0B3C04, 0xBD0B4004, 0xBD040400,
            0xBD040808, 0xBD04080A, 0xBD04080C, 0xBD04080E,
            0xBD061C08, 0xBD061C0A,
            0xBD062048, 0xBD0B1002, 0xBD0B1004, 0xBD0B3C00, 0xBD0B3C10, 0xBD0B4010,
            0xBD300890, 0xBD3008A6, 0xBD300C90, 0xBD300CA6,
        ) and size == 2:
            if obs is not None:
                if address == 0xBD0B3C04 and obs.state.uart0_mask_dirty:
                    obs.state.uart0_mask_dirty = 0
                obs.invalidate_mmio_slot(address, 2)
                if address in (0xBD061C08, 0xBD061C0A):
                    obs.invalidate_mmio_slot(0xBD061C08, 4)
        elif address in (
            0xBD040C40, 0xBD040C44, 0xBD040C48, 0xBD040C4C,
            0xBD061C08,
            0xBD3008B0, 0xBD300CB0, 0xBD302010, 0xBD302410,
        ) and size == 4:
            if obs is not None:
                obs.invalidate_mmio_slot(address, 4)
                if address == 0xBD061C08:
                    obs.invalidate_mmio_slot(0xBD061C08, 2)
                    obs.invalidate_mmio_slot(0xBD061C0A, 2)
        elif address == 0xBD302018 and size == 4:
            if obs is not None:
                obs.invalidate_mmio_slot(0xBD302010, 4)
        elif address == 0xBD302418 and size == 4:
            if obs is not None:
                obs.invalidate_mmio_slot(0xBD302410, 4)
        elif 0xBD0B0000 <= address < 0xBD0B0100:
            if obs is not None:
                if 0xBD0B0028 <= address < 0xBD0B0030:
                    obs.invalidate_mmio_slot(address & ~3, 4)
                elif 0xBD0B0034 <= address < 0xBD0B0038:
                    obs.invalidate_mmio_slot(0xBD0B0034, 4)
                elif 0xBD0B0038 <= address < 0xBD0B003C:
                    obs.invalidate_mmio_slot(0xBD0B0038, 4)
        elif 0xBD0B2000 <= address < 0xBD0B2200 and size == 2:
            if obs is not None:
                obs.invalidate_mmio_slot(address & ~7, 2)
                if address in (0xBD0B20D2, 0xBD0B20D4) and self.panel.endpoint is not None:
                    obs.invalidate_mmio_slot(0xBD0B1410, 2)
        elif not (0xBD040800 <= address < 0xBD040840
                  or 0xBD040C00 <= address < 0xBD040D00
                  or 0xBD0B1000 <= address < 0xBD0B1014
                  or 0xBD0B2000 <= address < 0xBD0B2200
                  or 0xBD09BC00 <= address <= 0xBD09FC3F
                  or 0xBD300800 <= address < 0xBD300900
                  or 0xBD300C00 <= address < 0xBD300D00
                  or address in (0xBD04100C, WATCHDOG_COUNT, WATCHDOG_RELOAD)):
            self._state_gen += 1
        p_idx = self._peripheral_idx.get(address)
        if p_idx is not None:
            peripheral = self._controllers[p_idx]
            try:
                peripheral.write(address, size, value)
                # Both panel drivers use logical GPIO 12, physical pin 26,
                # as the serial frame strobe. Direction changes release it.
                if address in (self.gpio.base + 26 * 8 + 2, self.gpio.base + 26 * 8 + 4):
                    level = (self.gpio.read(self.gpio.base + 26 * 8, 2)
                             if self.gpio.directions[26] == 1 else self.gpio.external_inputs.get(26))
                    self.panel.set_strobe(level)
                elif address in (
                    self.gpio.base + 28 * 8 + 2,
                    self.gpio.base + 28 * 8 + 4,
                    self.gpio.base + 0 * 8 + 2,
                    self.gpio.base + 0 * 8 + 4,
                ):
                    self.sync_panel_gpio_leds()
            except _PERIPHERAL_ERRORS as exc:
                raise HardwareError(str(exc)) from exc
            return
        if (address, size) not in self._write_valid:
            raise HardwareError(f'unmodeled MMIO write: {address:#010x}, size {size}')
        if address == UART_TX and self.uart_tx_pending is not None and not self._stored(0xBD0B4C00, 2):
            raise HardwareError('UART transmit write while its modeled slot is full')
        if address == WATCHDOG_CONTROL and value & 3 and not (self._stored(0xBD060002, 2) & 1):
            # The driver uses two count words and two enable bits. When the
            # watchdog routing (0xBD060002) is not initialized, reject bare enable.
            raise HardwareError('watchdog enable requires unmodeled dual-stage expiry behavior')
        if address == SECOND_PLL_RESET:
            previous = self._stored(SECOND_PLL_RESET, 4)
        elif address in (WATCHDOG_CONTROL, PLL_CONTROL, SDRAM_CONTROL):
            previous = self._stored(address, size)
        else:
            previous = 0
        value &= (1 << (size * 8)) - 1
        if address == SECOND_PLL_CONTROL:
            # Bit 23 is polled as lock status, not writable configuration.
            value &= ~SECOND_PLL_LOCK
        self._store_raw(address, size, value)

        if address == TIMER:
            self._timer_offset = (value - self.ticks) & 0xFFFFFFFF
        elif address == 0xBD040802:
            self.timer_pending &= ~value
            self._update_timer_irqs()
        elif address in (0xBD040804, 0xBD040806, 0xBD040808, 0xBD04080A, 0xBD04080C):
            self._timer_mask_dirty = True
            self._update_timer_irqs()
        elif address == 0xBD040812:
            for ch in range(8):
                if (value >> ch) & 1:
                    self._cancel_timer_channel(ch)
            self._prune_cancelled_events()
        elif 0xBD04081C <= address < 0xBD04083C:
            channel = (address - 0xBD04081C) // 4
            if (address - 0xBD04081C) & 2:
                # Countdown / one-shot register (0xBD04081E + 4*ch)
                if value == 0:
                    self._cancel_timer_channel(channel)
                    self._prune_cancelled_events()
                else:
                    self._schedule_timer_channel(channel, value)
            else:
                # Periodic reload register (0xBD04081C + 4*ch)
                self.timer_reload[channel] = value
                if value:
                    self._timer_reload_zero_mask &= ~(1 << channel)
                else:
                    self._timer_reload_zero_mask |= 1 << channel
                if value > 0 and self.timer_deadline[channel] is None:
                    self._schedule_timer_channel(channel, value + 1)
        elif 0xBD040C00 <= address < 0xBD040D00:
            if address == 0xBD040CC8:
                self._apply_irq_threshold(value)
            else:
                self._irq_cfg_dirty = True
                self._recompute_interrupts()
        elif address == WATCHDOG_CONTROL:
            if value & 0x0C:
                for stage in range(2):
                    if value & (4 << stage):
                        old_seq = self._wdog_event_seq[stage]
                        if old_seq:
                            self._cancelled_seqs.add(old_seq)
                            self._wdog_event_seq[stage] = 0
                        self._wdog_generation[stage] += 1
                        self.set_irq_level(5 + stage, False)
            if value & 3:
                total_180m = self._stored(WATCHDOG_COUNT, 4)
                reload_180m = self._stored(WATCHDOG_RELOAD, 4)
                for stage in range(2):
                    if (value & (1 << stage)) and (not (previous & (1 << stage)) or (value & (4 << stage))):
                        old_seq = self._wdog_event_seq[stage]
                        if old_seq:
                            self._cancelled_seqs.add(old_seq)
                        ticks_180m = max(180, total_180m if stage == 0 else (total_180m - reload_180m))
                        delay_ticks = max(1, ticks_180m // 180) * self.timer_step * self.us_ticks
                        self._wdog_generation[stage] += 1
                        self.schedule(delay_ticks, self._wdog_callbacks[stage])
                        self._wdog_event_seq[stage] = self._event_sequence
            if self._cancelled_seqs:
                self._prune_cancelled_events()
        elif address == SECOND_PLL_RESET:
            current_reset = self._stored(SECOND_PLL_RESET, 4)
            if current_reset & 0x1000:
                self._second_pll_reset_released = False
                self._second_pll_generation += 1
                self.second_pll_locked = False
            elif previous & 0x1000:
                self._second_pll_reset_released = True
                self._start_second_pll()
        elif address in (SECOND_PLL_CONTROL, SECOND_PLL_CONFIG):
            self._start_second_pll()
        elif address == PLL_CONTROL:
            if value & 0x10000:
                self._pll_generation += 1
                self.pll_locked = False
            elif previous & 0x10000:
                generation = self._pll_generation

                def lock() -> None:
                    if generation == self._pll_generation:
                        self.pll_locked = True

                self.schedule(self.pll_lock_ticks, lock)
        elif address == SDRAM_CONTROL:
            if not value & 1:
                self._sdram_generation += 1
                self.sdram_ready = False
            elif not previous & 1:
                generation = self._sdram_generation

                def calibrated() -> None:
                    if generation == self._sdram_generation:
                        self.sdram_ready = True

                self.schedule(self.sdram_calibration_ticks, calibrated)
        elif address == 0xBD0B1002:
            if value & 0x0001:
                self._store_raw(0xBD0B1004, 2, 0)
                self._store_raw(0xBD0B1010, 4, 0)
            if value & 0x0010:
                self._store_raw(0xBD0B1004, 2, 0)
                self._store_raw(0xBD0B1002, 2, value & ~0x0010)
            cmd_mask = 0x0006 if (value & 0x0006) == 0x0006 else 0x0024 if (value & 0x0024) == 0x0024 else 0
            if cmd_mask:
                acc = self._stored(0xBD0B1010, 4)
                if cmd_mask == 0x0006:
                    dma_addr = self._stored(0xBD0B1008, 4)
                    dma_len = self._stored(0xBD0B100C, 2)
                    if dma_len > 0 and self.sdio.dma_read is not None:
                        buf = self.sdio.dma_read(dma_addr, dma_len)
                        if len(buf) & 1:
                            buf = buf + b'\x00'
                        for i in range(0, len(buf), 2):
                            acc += (buf[i] << 8) | buf[i + 1]
                while acc >> 16:
                    acc = (acc & 0xFFFF) + (acc >> 16)
                self._store_raw(0xBD0B1010, 4, acc & 0xFFFFFFFF)
                self._store_raw(0xBD0B1004, 2, 0x0001)
                self._store_raw(0xBD0B1002, 2, value & ~cmd_mask)
        elif address == UART_TX:
            self._uart_tx_fifo.append(value & 0xFF)
            self._start_uart_tx()
        elif address in (0xBD0B3C04, 0xBD0B4004, 0xBD0B4C00):
            self._update_uart_irqs()
        elif address in (0xBD30100E, 0xBD301010):
            if address == 0xBD301010:
                self._sec_timer_pending &= ~value
                self._store_raw(0xBD301010, 2, self._sec_timer_pending)
            en_mask = self._stored(0xBD30100E, 2)
            for ch, irq in ((0, 11), (1, 12), (2, 10)):
                self.set_irq_level(irq, bool(((en_mask >> ch) & 1) and ((self._sec_timer_pending >> ch) & 1)))
        elif address == 0xBD302018:
            self._store_raw(0xBD302010, 4, value)
        elif address == 0xBD302418:
            self._store_raw(0xBD302410, 4, value)

    def _start_second_pll(self) -> None:
        """Infer reset/configure/lock behavior from reset routine 0x284c.

        That routine strobes bit 12 of the shared pin/control word, programs
        two clock words, and polls control bit 23. The detailed divider fields
        are not decoded: nonzero configuration arms a timed lock after reset
        release. Clock routing words remain plain configuration storage.
        """
        self._second_pll_generation += 1
        self.second_pll_locked = False
        if not (self._second_pll_reset_released
                and self._stored(SECOND_PLL_CONTROL, 4)
                and self._stored(SECOND_PLL_CONFIG, 4)):
            return
        generation = self._second_pll_generation

        def lock() -> None:
            if generation == self._second_pll_generation:
                self.second_pll_locked = True

        self.schedule(self.pll_lock_ticks, lock)
