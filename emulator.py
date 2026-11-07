"""HP DeskJet 2620 boot emulator: ARM/Thumb execution with UART capture.

The original image executes from its reset vector, including its scatter loader
and decompressor. CP15, physical memory, and decoded peripheral protocols are
modeled explicitly. This remains a partial boot emulator.
"""
from __future__ import annotations

import argparse
from collections import deque
import ctypes
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sys
from typing import Callable
import weakref

from hardware import (
    BootHardware, HardwareError, PLL_CONTROL, PLL_STATUS, TIMER, UART_RX, UART_STATUS, UART_TX,
    _PERIPHERAL_ERRORS,
)
from cpu import CONTROL, CP15, CpuModelError, DTCM_REGION, ITCM_REGION
from memory import FLASH_SIZE, PhysicalMemory
from micci import CarriageAsic, Mcasic8014
from i2c import EEPROM, Undercroft
from spi import W25Q128JV

from unicorn import (
    Uc, UcError, UC_ARCH_ARM, UC_MODE_ARM, UC_MODE_BIG_ENDIAN,
    UC_HOOK_CODE, UC_HOOK_BLOCK, UC_HOOK_MEM_INVALID, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE,
    UC_MEM_READ, UC_PROT_ALL, UC_PROT_READ, UC_PROT_WRITE, UC_PROT_EXEC,
)
from unicorn.arm_const import (
    UC_CPU_ARM_946, UC_ARM_REG_CPSR, UC_ARM_REG_SPSR, UC_ARM_REG_PC, UC_ARM_REG_SP,
    UC_ARM_REG_LR, UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2,
    UC_ARM_REG_R3, UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6,
    UC_ARM_REG_R7, UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_R10,
    UC_ARM_REG_R11, UC_ARM_REG_R12,
)
from unicorn.unicorn_py3 import unicorn as _uc_bindings

_uc_reg_read = _uc_bindings.uclib.uc_reg_read
_uc_reg_write = _uc_bindings.uclib.uc_reg_write
_uc_mem_read = _uc_bindings.uclib.uc_mem_read
_uc_hook_add = _uc_bindings.uclib.uc_hook_add
_C_CODE_CB = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_uint64, ctypes.c_uint32, ctypes.c_void_p
)
_C_MMIO_CB = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_int, ctypes.c_uint64, ctypes.c_int, ctypes.c_int64, ctypes.c_void_p
)
_C_EVENTMEM_CB = ctypes.CFUNCTYPE(
    ctypes.c_bool, ctypes.c_void_p, ctypes.c_int, ctypes.c_uint64, ctypes.c_int, ctypes.c_int64, ctypes.c_void_p
)

REGISTERS = (
    UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
    UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7,
    UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_R10, UC_ARM_REG_R11,
    UC_ARM_REG_R12, UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC,
)

_FAST_MMIO_READ_ADDRESSES = frozenset({
    0xBD040000, 0xBD040400, 0xBD040800, 0xBD040804, 0xBD040806,
    0xBD040808, 0xBD04080A, 0xBD04080C, 0xBD04080E, 0xBD040814,
    0xBD040818, 0xBD040C40, 0xBD040C44, 0xBD040C48, 0xBD040C4C,
    0xBD040CB8, 0xBD040CC0, 0xBD040CC8, 0xBD050030, 0xBD05004C,
    0xBD061C08, 0xBD062048, 0xBD065000, 0xBD065800, 0xBD080008,
    *(0xBD09BC00 + 0x400 * ep + off for ep in range(16)
      for off in (0x00, 0x04, 0x08, 0x10, 0x24, 0x28, 0x2C, 0x2E, 0x34, 0x38, 0x3C, 0x3E)),
    0xBD09FC00, 0xBD09FC04, 0xBD09FC08, 0xBD09FC0E, 0xBD09FC10,
    0xBD09FC14, 0xBD09FC16, 0xBD09FC1C, 0xBD09FC1E, 0xBD09FC24,
    0xBD09FC26, 0xBD09FC28, 0xBD09FC2A,
    0xBD0B0010, 0xBD0B0014, 0xBD0B0018, 0xBD0B001C, 0xBD0B0020,
    0xBD0B0024, 0xBD0B0028, 0xBD0B002C, 0xBD0B0030, 0xBD0B0034,
    0xBD0B0038, 0xBD0B0040, 0xBD0B0044, 0xBD0B0048, 0xBD0B00FC,
    0xBD0B1000, 0xBD0B1002, 0xBD0B1004, 0xBD0B1006, 0xBD0B1008,
    0xBD0B100C, 0xBD0B100E, 0xBD0B1010, 0xBD0B1410,
    0xBD0B2008, 0xBD0B2010, 0xBD0B2018, 0xBD0B2040,
    0xBD0B3C00, 0xBD0B3C04, 0xBD0B3C0E, 0xBD0B3C10, 0xBD0B4004, 0xBD0B400E,
    0xBD0B4010, 0xBD300890, 0xBD3008A6, 0xBD3008B0,
    0xBD300C90, 0xBD300CA6, 0xBD300CB0,
    0xBD302010, 0xBD302410,
})

_SHA256_CACHE: list[tuple[bytes | None, str]] = [(None, ''), (None, '')]


def _fast_sha256(data: bytes, *, is_dump: bool = False) -> str:
    if type(data) is bytes:
        slot = 0 if is_dump else 1
        cached_bytes, cached_digest = _SHA256_CACHE[slot]
        if data is cached_bytes or data == cached_bytes:
            _SHA256_CACHE[slot] = (data, cached_digest)
            return cached_digest
        digest = hashlib.sha256(data).hexdigest()
        _SHA256_CACHE[slot] = (data, digest)
        return digest
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class RunResult:
    reason: str
    instructions: int
    pc: int
    uart: bytes
    error: str | None


class Emulator:
    def __init__(self, dump: bytes, *, timer_step: int = 1, us_ticks: int = 4,
                 mcasic: bool = True, supply_mv: int = 24000,
                 analog_inputs: dict[int, int] | None = None,
                 power_button_wakeup: bool = False,
                 eeprom: bytes | None = None,
                 undercroft: bool = True,
                 gpio_inputs: dict[int, int] | None = None,
                 panel_input_word: int | None = None,
                 serial_panel_inputs: int | None = None,
                 serial_panel_tail_bit: int | None = None,
                 spi_profile: str | None = None,
                 spi_writable: bool = False,
                 spi_wip_read_latency: int = 0,
                 native_observer: bool = False,
                 init_dram: bytes | bytearray | memoryview | None = None,
                 init_dram_patches: list | None = None,
                 init_cp15: dict | None = None,
                 init_tcm: dict | None = None,
                 on_uart: Callable[[int], None] | None = None,
                 on_event: Callable[[dict], None] | None = None):
        if len(dump) != FLASH_SIZE:
            raise ValueError(f'expected a 16 MiB dump, got {len(dump)} bytes')
        if dump[:4] != b'\xe5\x9f\xf0\x18' or dump[0x20:0x24] != b'\x00\x00\x20\x70':
            raise ValueError('unsupported reset vector; expected this big-endian DeskJet image')
        self_ref = weakref.ref(self)
        board = BootHardware(timer_step, us_ticks=us_ticks,
                             gpio_inputs=gpio_inputs,
                             panel_input_word=panel_input_word,
                             serial_panel_inputs=serial_panel_inputs,
                             serial_panel_tail_bit=serial_panel_tail_bit,
                             spi_profile=spi_profile,
                             spi_miso_pull=1 if spi_profile else None,
                             spi_window_registers=(0x3c0, 0xfff, 0xfff) if spi_profile else None,
                             usb_dma_read=self._usb_dma_read,
                             usb_dma_write=self._usb_dma_write)
        self.board = board
        self._dump_sha256: str | None = None
        self._reg_buf = ctypes.c_uint32()
        self._reg_buf_ptr = ctypes.byref(self._reg_buf)
        self._block_buf = ctypes.create_string_buffer(4096)
        self._block_buf_ptr = ctypes.addressof(self._block_buf)
        self._arm_block_cache: dict[tuple[int, int | bytes], tuple[bytes | bytearray, dict[int, int], dict[int, int], bool, bool, bool]] = {}
        self._thumb_block_cache: dict[tuple[int, int], tuple[bytes | bytearray, bool]] = {}
        self.cp15 = CP15()
        if init_cp15 is not None:
            self.cp15.values.update(init_cp15)
            self.cp15.sync_mpu_mode()
        self._native_observer = None
        # The candidate board profile has one 16 MiB flash and two absent
        # device slots. Their explicit reset windows select the last MiB;
        # unconnected MISO is pulled high. Neither is a measured strap value.
        self.memory = PhysicalMemory(None, dump, self.cp15,
                                     absent_flash_window=spi_profile is not None,
                                     init_dram=init_dram,
                                     init_dram_patches=init_dram_patches,
                                     init_tcm=init_tcm,
                                     native_observer=native_observer)
        self.uc = self.memory.uc
        self._uch = self.uc._uch
        self._dram_base = ctypes.addressof(self.memory._dram)
        self._dram_buf = self.memory._dram_buf
        self.cp15.on_change = self._system_control_changed
        self._mpu_user = False
        self._fetch_checks_by_mode: tuple[set[tuple[int, int, bool]], set[tuple[int, int, bool]]] = (set(), set())
        self._fetch_pages_by_mode: tuple[set[tuple[int, bool]], set[tuple[int, bool]]] = (set(), set())
        self._mpu_plan_key = None
        self._mpu_diff_plan: list[tuple[int, int, int, int]] = []
        self._mpu_last_enabled = False
        mpu_state = self.memory._mpu_state
        if mpu_state is not None:
            (self._mpu_plan_key, self._mpu_diff_plan, self._mpu_last_enabled,
             prev_arm_cache, prev_thumb_cache, prev_fetch_checks,
             prev_fetch_pages, prev_cp15_caches) = mpu_state
            if self.memory._code_cache_valid:
                self._arm_block_cache = prev_arm_cache
                self._thumb_block_cache = prev_thumb_cache
                self._fetch_checks_by_mode = prev_fetch_checks
                self._fetch_pages_by_mode = prev_fetch_pages
                self.cp15._caches_by_mode = prev_cp15_caches
        mode_idx = 1 if self.cp15.mpu_enabled else 0
        self._fetch_checks: set[tuple[int, int, bool]] = self._fetch_checks_by_mode[mode_idx]
        self._fetch_pages: set[tuple[int, bool]] = self._fetch_pages_by_mode[mode_idx]
        # ``spi_writable`` is an explicit emulation-only opt-in: it makes the
        # modeled flash accept the datasheet program/erase protocol so a write
        # path can be exercised in the emulator. It never touches hardware and
        # defaults off, preserving the immutable boundary for every other run.
        self.spi_flash = (
            W25Q128JV(dump, writable=spi_writable,
                      wip_read_latency=spi_wip_read_latency)
            if spi_profile else None)
        if self.spi_flash:
            board.spi.devices[0] = self.spi_flash
            if spi_writable:
                # One physical chip: a program/erase issued through the
                # controller must show up in the CPU's flash window too.
                self.spi_flash.on_write = self._mirror_spi_write
        self.mcasic = Mcasic8014(board.schedule, supply_mv=supply_mv,
                                 analog_inputs=analog_inputs,
                                 power_button_wakeup=power_button_wakeup) if mcasic else None
        self.carriage_asic = CarriageAsic() if mcasic else None
        if self.mcasic:
            board.micci[0xBD300800].devices[(0, 6)] = self.mcasic
        if self.carriage_asic:
            board.micci[0xBD300C00].devices[(1, 4)] = self.carriage_asic
        # NVM write helper C81A/C838 drives logical GPIO 0 (physical pin 7)
        # low around writes and high afterward: inferred active-high EEPROM WC.
        self.eeprom = EEPROM(eeprom, write_protect=lambda: bool(
            board.read(0xBD0B2038, 2))) if eeprom is not None else None
        if self.eeprom:
            board.i2c.devices[0x50] = self.eeprom
        self.undercroft = Undercroft() if undercroft else None
        if self.undercroft:
            board.i2c.devices[0x1F] = self.undercroft
        self.eeprom_sha256 = _fast_sha256(eeprom) if eeprom is not None else None
        pages = board.pages
        mmio_total = len(pages) * 0x1000
        mmio_bundle = self.memory._mmio_bundle
        if mmio_bundle is not None and mmio_bundle[0] == pages:
            (_, self._mmio_backing, self._mmio_u8, self._mmio_u16,
             self._mmio_u32, self._mmio_page_offset, self._mmio_base_ptr) = mmio_bundle
            ctypes.memset(self._mmio_base_ptr, 0, mmio_total)
        else:
            self._mmio_backing = ctypes.create_string_buffer(mmio_total)
            self._mmio_u8 = (ctypes.c_uint8 * mmio_total).from_buffer(self._mmio_backing)
            self._mmio_u16 = (ctypes.c_uint16.__ctype_be__ * (mmio_total >> 1)).from_buffer(self._mmio_backing)
            self._mmio_u32 = (ctypes.c_uint32.__ctype_be__ * (mmio_total >> 2)).from_buffer(self._mmio_backing)
            self._mmio_page_offset: dict[int, int] = {}
            mmio_base_ptr = ctypes.addressof(self._mmio_backing)
            self._mmio_base_ptr = mmio_base_ptr
            for idx, page in enumerate(pages):
                self._mmio_page_offset[page] = idx * 0x1000
            run_start = 0
            for idx in range(1, len(pages) + 1):
                if idx == len(pages) or pages[idx] != pages[idx - 1] + 0x1000:
                    self.uc.mem_map_ptr(
                        pages[run_start],
                        (idx - run_start) * 0x1000,
                        UC_PROT_READ | UC_PROT_WRITE,
                        mmio_base_ptr + run_start * 0x1000,
                    )
                    run_start = idx
            if self.memory._pooled_uc:
                self.memory._mmio_bundle = (
                    list(pages), self._mmio_backing, self._mmio_u8, self._mmio_u16,
                    self._mmio_u32, self._mmio_page_offset, self._mmio_base_ptr,
                )
        if init_cp15 is not None or self.memory._mpu_dirty:
            self._sync_mpu()
        self._cpsr_valid = False
        self._ifm_valid = False
        self._cached_cpsr = 0x2D3
        self._cached_cpsr_deliverable = 0
        # Unicorn requires CPSR.E as well as BIG_ENDIAN for big-endian data.
        self._reg_write(UC_ARM_REG_CPSR, 0x2D3)
        self._block_cp15: dict[int, int] = {}
        self._block_user_transfers: dict[int, int] = {}
        self._block_has_specials = False
        self._block_irq_deliverable_mask = 3
        self._memory_user_access = False
        self.uart = bytearray()
        self.uart_writes: list[dict] = []
        self._recent_pcs: deque[int] = deque(maxlen=32)
        self._recent_ring: list[int] = [0] * 32
        self._synced_py_recent = 0
        self._recent_append = self._recent_pcs.append
        if self.memory._pooled_uc:
            uc_obj = self.uc
            uc_obj._active_emulator_ref = weakref.ref(self)
            if not hasattr(uc_obj, '_persistent_invalid_hook'):
                def _dispatch_invalid(uc_ptr, access, address, size, value, user_data):
                    emu_inst = uc_obj._active_emulator_ref()
                    return emu_inst._invalid_memory(uc_ptr, access, address, size, value, user_data) if emu_inst is not None else False
                uc_obj._persistent_invalid_cb = _C_EVENTMEM_CB(_dispatch_invalid)
                handle = _uc_bindings.uc_hook_h()
                err = _uc_hook_add(
                    self._uch, ctypes.byref(handle), UC_HOOK_MEM_INVALID,
                    ctypes.cast(uc_obj._persistent_invalid_cb, ctypes.c_void_p),
                    None, ctypes.c_uint64(1), ctypes.c_uint64(0))
                if err:
                    raise UcError(err)
                uc_obj._persistent_invalid_hook = handle.value
            if not native_observer:
                pages_range = (pages[0], pages[-1] + 0xFFF) if pages else None
                if getattr(uc_obj, '_persistent_py_hooks_range', -1) == pages_range:
                    pass
                elif not hasattr(uc_obj, '_persistent_py_hooks_range'):
                    emu = None
                    board = None
                    timer_step = 1
                    limit = 0
                    recent_ring = None
                    mem = None
                    arm_get = None
                    thumb_get = None
                    mmio_fn = None

                    uch = self._uch
                    reg_buf = self._reg_buf
                    reg_buf_ptr = self._reg_buf_ptr

                    def _bind_py_emu(e):
                        nonlocal emu, board, timer_step, limit, recent_ring, mem, arm_get, thumb_get, mmio_fn, uch, reg_buf, reg_buf_ptr
                        if e is None:
                            emu = board = recent_ring = mem = arm_get = thumb_get = mmio_fn = reg_buf = reg_buf_ptr = None
                        else:
                            emu = e
                            board = e.board
                            timer_step = board.timer_step
                            limit = e._limit
                            recent_ring = e._recent_ring
                            mem = e.memory
                            arm_get = e._arm_block_cache.get
                            thumb_get = e._thumb_block_cache.get
                            mmio_fn = e._mmio
                            uch = e._uch
                            reg_buf = e._reg_buf
                            reg_buf_ptr = e._reg_buf_ptr

                    def _dispatch_mmio(uc_ptr, access, address, size, value, user_data):
                        if mmio_fn is not None:
                            mmio_fn(uc_ptr, access, address, size, value, user_data)
                        else:
                            e = uc_obj._active_emulator_ref()
                            if e is not None:
                                e._mmio(uc_ptr, access, address, size, value, user_data)

                    def _dispatch_block(uc_ptr, address, size, user_data):
                        e = emu
                        if e is None:
                            e = uc_obj._active_emulator_ref()
                            if e is not None:
                                e._block(uc_ptr, address, size, user_data)
                            return
                        try:
                            if not e.reason:
                                if e._block_has_specials:
                                    e._block_cp15 = {}
                                    e._block_user_transfers = {}
                                    e._block_has_specials = False
                                if e._cpsr_valid:
                                    cpsr = e._cached_cpsr
                                    user_ok = True
                                elif e._ifm_valid and ((address | size) & 2):
                                    cpsr = e._cached_cpsr | 0x20
                                    e._cached_cpsr = cpsr
                                    user_ok = True
                                else:
                                    _uc_reg_read(uch, UC_ARM_REG_CPSR, reg_buf_ptr)
                                    cpsr = reg_buf.value
                                    e._cached_cpsr = cpsr
                                    if e._ifm_valid:
                                        user_ok = True
                                    else:
                                        e._cached_cpsr_deliverable = (0 if (cpsr & 0x80) else 1) | (0 if (cpsr & 0x40) else 2)
                                        user_ok = ((cpsr & 0x1F) == 0x10) == e._mpu_user
                                if user_ok:
                                    if cpsr & 0x20:
                                        cached_t = thumb_get((address, size))
                                        if cached_t is not None:
                                            data, view, cpsr_static, ifm_static, page_num, layout_ver = cached_t
                                            if (page_num, e._mpu_user) in e._fetch_pages and layout_ver == mem._layout_version and data == view:
                                                e._block_irq_deliverable_mask = e._cached_cpsr_deliverable
                                                e._cpsr_valid = cpsr_static
                                                e._ifm_valid = ifm_static
                                                return
                                    else:
                                        cached = arm_get((address, size))
                                        if cached is not None:
                                            data, view, cp15, transfers, has_specials, can_unmask_irq, cpsr_static, page_num, layout_ver = cached
                                            if (page_num, e._mpu_user) in e._fetch_pages and layout_ver == mem._layout_version and data == view:
                                                e._block_irq_deliverable_mask = 3 if can_unmask_irq else e._cached_cpsr_deliverable
                                                e._cpsr_valid = cpsr_static
                                                e._ifm_valid = (not can_unmask_irq) and (not has_specials)
                                                if has_specials:
                                                    e._block_cp15 = cp15
                                                    e._block_user_transfers = transfers
                                                    e._block_has_specials = True
                                                return
                            e._block_miss(address, size)
                        except BaseException as exc:
                            uc_obj._hook_exception = exc
                            uc_obj.emu_stop()

                    def _dispatch_insn(uc_ptr, address, size, user_data):
                        e = emu
                        if e is None:
                            e = uc_obj._active_emulator_ref()
                            if e is not None:
                                e._instruction(uc_ptr, address, size, user_data)
                            return
                        try:
                            if e._memory_user_access:
                                e._memory_user_access = False
                            if e._instruction_pending:
                                target = board.ticks + timer_step
                                if target < board._next_deadline:
                                    board.ticks = target
                                else:
                                    e._instruction_pending = False
                                    e._advance_board(1)
                                    if e.reason:
                                        return
                                    e._instruction_pending = True
                            elif e.reason:
                                return
                            else:
                                e._instruction_pending = True
                            if (board.interrupt_mask & e._block_irq_deliverable_mask) and e._check_interrupt(uc_obj, address):
                                e._instruction_pending = False
                                return
                            insns = e.instructions
                            if insns >= e._limit:
                                e._instruction_pending = False
                                e.reason = 'instruction-limit'
                                uc_obj.emu_stop()
                                return
                            e.instructions = insns + 1
                            recent_ring[insns & 31] = address
                            if e._block_has_specials and (address in e._block_cp15 or address in e._block_user_transfers):
                                e._special_instruction(uc_obj, address, size)
                        except BaseException as exc:
                            uc_obj._hook_exception = exc
                            uc_obj.emu_stop()

                    orig_emu_start = uc_obj.emu_start

                    def _py_emu_start(begin, until, timeout=0, count=0):
                        _bind_py_emu(uc_obj._active_emulator_ref())
                        try:
                            return orig_emu_start(begin, until, timeout=timeout, count=count)
                        finally:
                            _bind_py_emu(None)

                    uc_obj.emu_start = _py_emu_start
                    uc_obj._bind_py_emu = _bind_py_emu
                    uc_obj._persistent_py_mmio_cb = _C_MMIO_CB(_dispatch_mmio)
                    uc_obj._persistent_py_block_cb = _C_CODE_CB(_dispatch_block)
                    uc_obj._persistent_py_insn_cb = _C_CODE_CB(_dispatch_insn)
                    if pages_range is not None:
                        h1 = _uc_bindings.uc_hook_h()
                        if _uc_hook_add(self._uch, ctypes.byref(h1), UC_HOOK_MEM_READ | UC_HOOK_MEM_WRITE,
                                        ctypes.cast(uc_obj._persistent_py_mmio_cb, ctypes.c_void_p),
                                        None, ctypes.c_uint64(pages_range[0]), ctypes.c_uint64(pages_range[1])):
                            raise UcError(1)
                    h2 = _uc_bindings.uc_hook_h()
                    if _uc_hook_add(self._uch, ctypes.byref(h2), UC_HOOK_BLOCK,
                                    ctypes.cast(uc_obj._persistent_py_block_cb, ctypes.c_void_p),
                                    None, ctypes.c_uint64(1), ctypes.c_uint64(0)):
                        raise UcError(1)
                    h3 = _uc_bindings.uc_hook_h()
                    if _uc_hook_add(self._uch, ctypes.byref(h3), UC_HOOK_CODE,
                                    ctypes.cast(uc_obj._persistent_py_insn_cb, ctypes.c_void_p),
                                    None, ctypes.c_uint64(1), ctypes.c_uint64(0)):
                        raise UcError(1)
                    uc_obj._persistent_py_hooks_range = pages_range
                else:
                    self._insn_hook_fn = self._make_insn_hook()
                    if pages:
                        self._c_mmio_cb = _C_MMIO_CB(lambda uc_p, acc, addr, sz, val, ud: (e := self_ref()) is not None and e._mmio(uc_p, acc, addr, sz, val, ud))
                        self._add_c_hook(UC_HOOK_MEM_READ | UC_HOOK_MEM_WRITE, self._c_mmio_cb,
                                         begin=pages[0], end=pages[-1] + 0xFFF)
                    self._c_block_cb = _C_CODE_CB(lambda uc_p, addr, sz, ud: (e := self_ref()) is not None and e._block(uc_p, addr, sz, ud))
                    self._c_insn_cb = _C_CODE_CB(self._insn_hook_fn)
                    self._add_c_hook(UC_HOOK_BLOCK, self._c_block_cb)
                    self._add_c_hook(UC_HOOK_CODE, self._c_insn_cb)
        else:
            if pages and not native_observer:
                self._c_mmio_cb = _C_MMIO_CB(lambda uc_p, acc, addr, sz, val, ud: (e := self_ref()) is not None and e._mmio(uc_p, acc, addr, sz, val, ud))
                self._add_c_hook(UC_HOOK_MEM_READ | UC_HOOK_MEM_WRITE, self._c_mmio_cb,
                                 begin=pages[0], end=pages[-1] + 0xFFF)
            self._c_invalid_cb = _C_EVENTMEM_CB(lambda uc_p, acc, addr, sz, val, ud: bool((e := self_ref()) is not None and e._invalid_memory(uc_p, acc, addr, sz, val, ud)))
            self._add_c_hook(UC_HOOK_MEM_INVALID, self._c_invalid_cb)
            if not native_observer:
                self._c_block_cb = _C_CODE_CB(lambda uc_p, addr, sz, ud: (e := self_ref()) is not None and e._block(uc_p, addr, sz, ud))
                self._c_insn_cb = _C_CODE_CB(self._make_insn_hook())
                self._add_c_hook(UC_HOOK_BLOCK, self._c_block_cb)
                self._add_c_hook(UC_HOOK_CODE, self._c_insn_cb)
        self.instructions = 0
        self._instruction_pending = False
        self.reason: str | None = None
        self.error: str | None = None
        self.on_uart = on_uart
        self.on_event = on_event
        self.on_frame = None
        self._started = False
        if native_observer:
            from native_observer import NativeObserver
            self._native_observer = NativeObserver(self)

    def __del__(self) -> None:
        uc_obj = getattr(self, 'uc', None)
        if uc_obj is not None and getattr(uc_obj, '_bind_py_emu', None) is not None:
            uc_obj._bind_py_emu(None)
        obs = getattr(self, '_native_observer', None)
        if obs is not None:
            self._native_observer = None
            obs._release()
        mem = getattr(self, 'memory', None)
        if mem is not None:
            self.memory = None
            mem._release()

    @property
    def recent_pcs(self) -> deque[int]:
        if self._native_observer is not None:
            self._native_observer.copy_recent()
            return self._recent_pcs
        total = self.instructions
        if total != self._synced_py_recent:
            self._synced_py_recent = total
            recent_pcs = self._recent_pcs
            recent_pcs.clear()
            ring = self._recent_ring
            if total < 32:
                recent_pcs.extend(ring[:total])
            else:
                pos = total & 31
                if pos == 0:
                    recent_pcs.extend(ring)
                else:
                    recent_pcs.extend(ring[pos:])
                    recent_pcs.extend(ring[:pos])
        return self._recent_pcs

    @property
    def dump_sha256(self) -> str:
        if self._dump_sha256 is None:
            self._dump_sha256 = _fast_sha256(self.memory._dump, is_dump=True)
        return self._dump_sha256

    def _add_c_hook(self, hook_type: int, callback, begin: int = 1, end: int = 0) -> int:
        handle = _uc_bindings.uc_hook_h()
        err = _uc_hook_add(
            self._uch, ctypes.byref(handle), hook_type,
            ctypes.cast(callback, ctypes.c_void_p),
            None, ctypes.c_uint64(begin), ctypes.c_uint64(end))
        if err:
            raise UcError(err)
        h = handle.value
        self.memory._all_hooks.append(h)
        self.memory._cb_refs[h] = callback
        if not self.memory._pooled_uc:
            self.uc._callbacks[h] = callback
        return h

    def _reg_read(self, reg_id: int) -> int:
        _uc_reg_read(self._uch, reg_id, self._reg_buf_ptr)
        return self._reg_buf.value

    def _reg_write(self, reg_id: int, value: int) -> None:
        if reg_id == UC_ARM_REG_CPSR:
            self._cpsr_valid = False
            self._ifm_valid = False
        self._reg_buf.value = value
        _uc_reg_write(self._uch, reg_id, self._reg_buf_ptr)

    def _usb_dma_read(self, address: int, length: int) -> bytes:
        off = address & 0x03FFFFFF
        if length < 0 or off + length > len(self.memory._dram):
            raise HardwareError(f'DMA read out of DRAM bounds: {address:#010x}, length {length}')
        return self.memory._dram_mv[off:off + length].tobytes()

    def _usb_dma_write(self, address: int, data: bytes) -> None:
        off = address & 0x03FFFFFF
        if off + len(data) > len(self.memory._dram):
            raise HardwareError(f'DMA write out of DRAM bounds: {address:#010x}, length {len(data)}')
        self.memory._dram_mv[off:off + len(data)] = data

    def _event(self, kind: str, **fields) -> None:
        if self.on_event is not None:
            pc = fields.pop('pc', None)
            if pc is None:
                _uc_reg_read(self._uch, UC_ARM_REG_PC, self._reg_buf_ptr)
                pc = self._reg_buf.value
            self.on_event({'kind': kind, 'pc': pc,
                           'instruction': self.instructions, **fields})

    def _fault(self, message: str) -> None:
        if self._native_observer is not None:
            self._native_observer.synchronize(recent=True)
        self.reason = 'fault'
        self.error = message
        if self._native_observer is not None:
            self._native_observer.refresh()
        self.uc.emu_stop()

    def _invalid_memory(self, uc, access, address, size, value, _) -> bool:
        self._fault(self.memory.error or f'invalid memory access {access} at {address:#010x}, size {size}, '
                    f'PC {self._reg_read(UC_ARM_REG_PC):#010x}')
        return False

    def _system_control_changed(self, key, old, new) -> None:
        if self.memory._pending_restore:
            self.memory.before_instruction()
        if key[1] not in (1, 5, 6, 9):
            return
        if key == CONTROL:
            tcm_changed = bool((old ^ new) & 0xF0000)
            mpu_changed = bool((old ^ new) & 1)
            if not tcm_changed and not mpu_changed:
                return
            tcm_synced = self.memory.sync_tcm() if tcm_changed else False
            if mpu_changed or tcm_synced:
                self._sync_mpu()
            elif self._native_observer is not None:
                self._native_observer.sync_memory_map()
            return
        if key[1] == 9:
            if key in (ITCM_REGION, DTCM_REGION) and old != new:
                if self.memory.sync_tcm():
                    self._sync_mpu()
            return
        if old != new and self.cp15.mpu_enabled:
            self._sync_mpu()

    def _sync_mpu(self) -> None:
        """Apply MPU permissions to native pages, including overlap priority.

        Caches are absent in this CPU profile, so accesses use the shared physical
        backing. Both privileged and user permissions are reflected in Unicorn's
        map; mode transitions are checked at each basic block.
        """
        cp15 = self.cp15
        vals = cp15.values
        plan_key = (
            vals[(0, 5, 0, 2)], vals[(0, 5, 0, 3)],
            vals[(0, 6, 0, 0)], vals[(0, 6, 1, 0)],
            vals[(0, 6, 2, 0)], vals[(0, 6, 3, 0)],
            vals[(0, 6, 4, 0)], vals[(0, 6, 5, 0)],
            vals[(0, 6, 6, 0)], vals[(0, 6, 7, 0)],
            self.memory._mappings, self._mpu_user,
        )
        enabled = bool(vals[CONTROL] & 1)
        mode_idx = 1 if enabled else 0
        self._cpsr_valid = False
        self._ifm_valid = False
        if plan_key == self._mpu_plan_key:
            cp15.sync_mpu_mode()
            self._fetch_checks = self._fetch_checks_by_mode[mode_idx]
            self._fetch_pages = self._fetch_pages_by_mode[mode_idx]
            if self._native_observer is not None:
                self._native_observer.sync_mpu_mode()
            if enabled != self._mpu_last_enabled:
                self._mpu_last_enabled = enabled
                self.memory._mpu_dirty = True
                for begin, size, physical, allowed_en in self._mpu_diff_plan:
                    self.uc.mem_protect(begin, size, allowed_en if enabled else physical)
            self.memory._mpu_state = (
                self._mpu_plan_key,
                self._mpu_diff_plan,
                self._mpu_last_enabled,
                self._arm_block_cache,
                self._thumb_block_cache,
                self._fetch_checks_by_mode,
                self._fetch_pages_by_mode,
                cp15._caches_by_mode,
            )
            return

        cp15.invalidate_mpu_cache()
        for s in self._fetch_checks_by_mode:
            s.clear()
        for s in self._fetch_pages_by_mode:
            s.clear()
        self._fetch_checks = self._fetch_checks_by_mode[mode_idx]
        self._fetch_pages = self._fetch_pages_by_mode[mode_idx]
        self.memory._code_cache_valid = False
        if self._native_observer is not None:
            self._native_observer.invalidate()

        regions = cp15.mpu_regions()
        mmio_pages = set(self.board.pages)
        boundary_set = {p for r in regions for p in (r.address, r.address + r.size)}
        for bus_r in self.memory._bus:
            boundary_set.add(bus_r.address)
            boundary_set.add(bus_r.address + bus_r.size)
        for tcm_m in self.memory._mappings:
            boundary_set.add(tcm_m.address)
            boundary_set.add(tcm_m.address + tcm_m.size)
        boundaries = sorted(boundary_set)
        coalesced: list[tuple[int, int, int]] = []
        for start, inclusive_end, current in self.uc.mem_regions():
            if (coalesced and coalesced[-1][1] + 1 == start and coalesced[-1][2] == current
                    and (start & ~0xFFF) not in mmio_pages
                    and (coalesced[-1][0] & ~0xFFF) not in mmio_pages):
                coalesced[-1] = (coalesced[-1][0], inclusive_end, current)
            else:
                coalesced.append((start, inclusive_end, current))
        diff_plan: list[tuple[int, int, int, int]] = []
        has_regions = bool(regions)
        if has_regions:
            cp15._mpu_enabled = True
            cp15._access_cache, cp15._allowed_access = cp15._caches_by_mode[1]
        try:
            for start, inclusive_end, current in coalesced:
                end = inclusive_end + 1
                points = [start] + [p for p in boundaries if start < p < end] + [end]
                for begin, stop in zip(points, points[1:]):
                    physical = self.memory.max_permissions(begin)
                    if (begin & ~0xFFF) in mmio_pages:
                        physical = UC_PROT_READ | UC_PROT_WRITE
                    allowed_en = 0
                    if has_regions:
                        for flag, operation in (
                            (UC_PROT_READ, {}), (UC_PROT_WRITE, {'write': True}),
                            (UC_PROT_EXEC, {'instruction': True}),
                        ):
                            if not (physical & flag):
                                continue
                            try:
                                cp15.check_access(begin, stop - begin, user=self._mpu_user, **operation)
                                allowed_en |= flag
                            except CpuModelError:
                                pass
                        allowed_en &= physical
                    if physical != allowed_en:
                        diff_plan.append((begin, stop - begin, physical, allowed_en))
                    target_perm = allowed_en if enabled else physical
                    if current != target_perm:
                        self.memory._mpu_dirty = True
                        self.uc.mem_protect(begin, stop - begin, target_perm)
        finally:
            if has_regions:
                cp15.sync_mpu_mode()
        self._mpu_plan_key = plan_key if has_regions else None
        self._mpu_diff_plan = diff_plan if has_regions else []
        self._mpu_last_enabled = enabled
        if not enabled and not has_regions:
            self.memory._mpu_dirty = False
        self.memory._mpu_state = (
            self._mpu_plan_key,
            self._mpu_diff_plan,
            self._mpu_last_enabled,
            self._arm_block_cache,
            self._thumb_block_cache,
            self._fetch_checks_by_mode,
            self._fetch_pages_by_mode,
            cp15._caches_by_mode,
        )

    @staticmethod
    def _condition_passed(condition: int, cpsr: int) -> bool:
        if condition == 14:
            return True
        n = (cpsr >> 31) & 1
        z = (cpsr >> 30) & 1
        c = (cpsr >> 29) & 1
        v = (cpsr >> 28) & 1
        return bool((z, not z, c, not c, n, not n, v, not v,
                     c and not z, not c or z, n == v, n != v,
                     not z and n == v, z or n != v, True, False)[condition])

    def _check_block_fetch(self, address: int, size: int, cpsr: int) -> None:
        user = (cpsr & 0x1F) == 0x10
        if user != self._mpu_user:
            self._mpu_user = user
            self._sync_mpu()
        top = address >> 28
        if (top == 2 or top == 6 or top == 10) and size > 0:
            exec_dram = self.memory._exec_dram_pages
            if exec_dram is not None:
                exec_dram[(address & 0x03FFFFFF) >> 12] = 1
                exec_dram[((address + size - 1) & 0x03FFFFFF) >> 12] = 1
        same_page = size > 0 and ((address ^ (address + size - 1)) < 0x1000)
        page_key = (address >> 12, user)
        if not (same_page and page_key in self._fetch_pages):
            fetch = (address, size, user)
            if fetch not in self._fetch_checks:
                try:
                    self.cp15.check_access(address, size, instruction=True, user=user)
                except CpuModelError as exc:
                    self._fault(str(exc))
                    return
                self._fetch_checks.add(fetch)
            if same_page:
                self._fetch_pages.add(page_key)
        if same_page and self._native_observer is not None:
            self._native_observer.cache_exec_page(address >> 12, user)

    def _block(self, uc, address, size, _) -> None:
        """Decode coprocessor transfers once per translated basic block.

        The instruction hook remains responsible for exact budget accounting.
        Reading each ARM instruction separately through ctypes dominated runtime.
        No persistent code cache is used, so copied/self-modified code is observed.
        """
        try:
            if not self.reason and (self._cpsr_valid or (self._ifm_valid and ((address | size) & 2) and not self._block_has_specials)):
                if (self._cached_cpsr & 0x20) or ((address | size) & 2):
                    cached_t = self._thumb_block_cache.get((address, size))
                    if cached_t is not None:
                        data, view, cpsr_static, ifm_static, page_num, layout_ver = cached_t
                        if (page_num, self._mpu_user) in self._fetch_pages and layout_ver == self.memory._layout_version and data == view:
                            self._cached_cpsr |= 0x20
                            self._block_irq_deliverable_mask = self._cached_cpsr_deliverable
                            self._cpsr_valid = cpsr_static
                            self._ifm_valid = ifm_static
                            return
                else:
                    cached = self._arm_block_cache.get((address, size))
                    if cached is not None:
                        data, view, cp15, transfers, has_specials, can_unmask_irq, cpsr_static, page_num, layout_ver = cached
                        if (page_num, self._mpu_user) in self._fetch_pages and layout_ver == self.memory._layout_version and data == view:
                            self._block_irq_deliverable_mask = 3 if can_unmask_irq else self._cached_cpsr_deliverable
                            self._cpsr_valid = cpsr_static
                            self._ifm_valid = (not can_unmask_irq) and (not has_specials)
                            if has_specials:
                                self._block_cp15 = cp15
                                self._block_user_transfers = transfers
                                self._block_has_specials = True
                            return
            self._block_miss(address, size)
        except BaseException as exc:
            self.uc._hook_exception = exc
            self.uc.emu_stop()

    def _block_miss(self, address: int, size: int) -> None:
        try:
            if self._native_observer is not None:
                self._native_observer.set_specials({}, {})
            if self._block_has_specials:
                self._block_cp15 = {}
                self._block_user_transfers = {}
                self._block_has_specials = False
            if self._native_observer is None and (self._cpsr_valid or (self._ifm_valid and ((address | size) & 2))):
                cpsr = (self._cached_cpsr | 0x20) if ((address | size) & 2) else self._cached_cpsr
                self._cached_cpsr = cpsr
                self._cpsr_valid = False
            else:
                _uc_reg_read(self._uch, UC_ARM_REG_CPSR, self._reg_buf_ptr)
                cpsr = self._reg_buf.value
                self._cached_cpsr = cpsr
                self._cpsr_valid = False
                self._ifm_valid = False
            user = (cpsr & 0x1F) == 0x10
            if (user != self._mpu_user
                    or size <= 0
                    or ((address ^ (address + size - 1)) >= 0x1000)
                    or (address >> 12, user) not in self._fetch_pages):
                self._check_block_fetch(address, size, cpsr)
            if self.reason:
                return
            cpsr_deliverable = (0 if (cpsr & 0x80) else 1) | (0 if (cpsr & 0x40) else 2)
            self._cached_cpsr_deliverable = cpsr_deliverable
            mem = self.memory
            layout_ver = mem._layout_version
            if cpsr & 0x20:
                self._block_irq_deliverable_mask = cpsr_deliverable
                if self._native_observer is not None:
                    self._cpsr_valid = False
                    self._ifm_valid = False
                    return
                cached_t = self._thumb_block_cache.get((address, size))
                if cached_t is not None and cached_t[5] == layout_ver and cached_t[0] == cached_t[1]:
                    self._cpsr_valid = cached_t[2]
                    self._ifm_valid = cached_t[3]
                    return
                if not mem._tcm_load_mode and size > 0 and (not mem._dtcm_end or mem._dtcm_base >= 0x28000000):
                    top = address >> 28
                    if (top == 2 or top == 10 or top == 6) and (address & 0x0FFFFFFF) + size <= 0x04000000:
                        off = address & 0x03FFFFFF
                        view = mem._dram_mv[off:off + size]
                    elif address + size <= mem._itcm_size and mem._itcm_size == mem._itcm_phys_size:
                        view = mem._itcm_mv[address:address + size]
                    elif mem._itcm_size <= address and address + size <= 0x01000000:
                        view = mem._flash_mv[address:address + size]
                    else:
                        view = mem.read_block_view(address, size)
                else:
                    view = mem.read_block_view(address, size)
                if view is not None:
                    data = bytes(view)
                    page_num = (address >> 12) if (size > 0 and ((address ^ (address + size - 1)) < 0x1000)) else -1
                else:
                    if size <= 4096 and _uc_mem_read(self._uch, address, self._block_buf, size) == 0:
                        data = ctypes.string_at(self._block_buf_ptr, size)
                    else:
                        data = bytes(self.uc.mem_read(address, size))
                    page_num = -1
                can_switch_t = bool(size & 1)
                can_change_ifm = can_switch_t
                for offset in range(0, len(data) - 1, 2):
                    hw = int.from_bytes(data[offset:offset + 2], 'big')
                    top4 = hw >> 12
                    if top4 == 4:
                        if (hw & 0xFF00) == 0x4700 or ((hw & 0xFC00) == 0x4400 and (hw & 0x0087) == 0x0087):
                            can_switch_t = True
                    elif top4 == 11:
                        if (hw & 0xFF00) == 0xBD00:
                            can_switch_t = True
                        elif (hw & 0x0F00) not in (0x0000, 0x0400, 0x0500, 0x0C00):
                            can_switch_t = True
                            can_change_ifm = True
                    elif top4 == 13:
                        if (hw & 0xFE00) == 0xDE00:
                            can_switch_t = True
                            can_change_ifm = True
                    elif top4 == 14:
                        if (hw & 0xF800) == 0xE800:
                            can_switch_t = True
                cpsr_static = (not can_switch_t) and (view is not None)
                ifm_static = (not can_change_ifm) and (view is not None)
                self._cpsr_valid = cpsr_static
                self._ifm_valid = ifm_static
                if view is not None:
                    self._thumb_block_cache[(address, size)] = (
                        data, view, cpsr_static, ifm_static, page_num, layout_ver
                    )
                return
            cached = self._arm_block_cache.get((address, size))
            if cached is not None and cached[8] == layout_ver and cached[0] == cached[1]:
                _, _, cp15, transfers, has_specials, can_unmask_irq, cpsr_static, _, _ = cached
                self._block_irq_deliverable_mask = 3 if can_unmask_irq else cpsr_deliverable
                is_py = self._native_observer is None
                self._cpsr_valid = cpsr_static and is_py
                self._ifm_valid = (not can_unmask_irq) and (not has_specials) and is_py
                if has_specials:
                    self._block_cp15 = cp15
                    self._block_user_transfers = transfers
                    self._block_has_specials = True
                    if not is_py:
                        self._native_observer.set_specials(cp15, transfers)
                return
            if not mem._tcm_load_mode and size > 0 and (not mem._dtcm_end or mem._dtcm_base >= 0x28000000):
                top = address >> 28
                if (top == 2 or top == 10 or top == 6) and (address & 0x0FFFFFFF) + size <= 0x04000000:
                    off = address & 0x03FFFFFF
                    view = mem._dram_mv[off:off + size]
                elif address + size <= mem._itcm_size and mem._itcm_size == mem._itcm_phys_size:
                    view = mem._itcm_mv[address:address + size]
                elif mem._itcm_size <= address and address + size <= 0x01000000:
                    view = mem._flash_mv[address:address + size]
                else:
                    view = mem.read_block_view(address, size)
            else:
                view = mem.read_block_view(address, size)
            if view is not None:
                data = bytes(view)
                cache_key = (address, size)
                page_num = (address >> 12) if (size > 0 and ((address ^ (address + size - 1)) < 0x1000)) else -1
            else:
                if size <= 4096 and _uc_mem_read(self._uch, address, self._block_buf, size) == 0:
                    data = ctypes.string_at(self._block_buf_ptr, size)
                else:
                    data = bytes(self.uc.mem_read(address, size))
                cache_key = (address, data)
                page_num = -1
                cached = self._arm_block_cache.get(cache_key)
                if cached is not None:
                    _, _, cp15, transfers, has_specials, can_unmask_irq, _, _, _ = cached
                    self._block_irq_deliverable_mask = 3 if can_unmask_irq else cpsr_deliverable
                    self._cpsr_valid = False
                    self._ifm_valid = False
                    if has_specials:
                        self._block_cp15 = cp15
                        self._block_user_transfers = transfers
                        self._block_has_specials = True
                        if self._native_observer is not None:
                            self._native_observer.set_specials(cp15, transfers)
                    return
            cp15 = {}
            transfers = {}
            can_unmask_irq = False
            can_change_cpsr = bool(size & 3)
            for offset in range(0, len(data) - 3, 4):
                word = int.from_bytes(data[offset:offset + 4], 'big')
                if (word >> 28) == 15:
                    can_change_cpsr = True
                    if (word & 0xFE000000) != 0xFA000000:
                        can_unmask_irq = True
                elif word & 0x0F000F10 == 0x0E000F10:
                    cp15[address + offset] = word
                    can_change_cpsr = True
                elif word & 0x0D200000 == 0x04200000:
                    # ARM single data transfer, P=0 and W=1: LDRT/STRT and
                    # their byte forms. The access uses User permissions even
                    # if the current processor mode is privileged.
                    transfers[address + offset] = word
                    can_change_cpsr = True
                else:
                    top2 = (word >> 26) & 3
                    if top2 == 0:
                        if (word & 0x0D900000) == 0x01000000 and (word & 0x0FC000F0) != 0x00000090:
                            can_change_cpsr = True
                            if (word & 0x0FFFFFF0) not in (0x012FFF10, 0x012FFF30):
                                can_unmask_irq = True
                        elif ((word >> 12) & 15) == 15 and (word & (1 << 20)):
                            can_change_cpsr = True
                            can_unmask_irq = True
                    elif top2 == 1:
                        if ((word >> 12) & 15) == 15 and (word & (1 << 20)):
                            can_change_cpsr = True
                    elif top2 == 2:
                        if ((word >> 25) & 1) == 0 and (word & 0x00408000) != 0:
                            can_change_cpsr = True
                            if word & 0x00400000:
                                can_unmask_irq = True
                    else:
                        can_change_cpsr = True
                        can_unmask_irq = True
            has_specials = bool(cp15 or transfers)
            cpsr_static = (not can_change_cpsr) and (not has_specials) and (view is not None)
            is_py = self._native_observer is None
            self._cpsr_valid = cpsr_static and is_py
            self._ifm_valid = (not can_unmask_irq) and (not has_specials) and (view is not None) and is_py
            self._block_irq_deliverable_mask = 3 if can_unmask_irq else cpsr_deliverable
            self._arm_block_cache[cache_key] = (
                data, view, cp15, transfers, has_specials, can_unmask_irq, cpsr_static, page_num, layout_ver
            )
            if has_specials:
                self._block_cp15 = cp15
                self._block_user_transfers = transfers
                self._block_has_specials = True
                if not is_py:
                    self._native_observer.set_specials(cp15, transfers)
        except BaseException as exc:
            self.uc._hook_exception = exc
            self.uc.emu_stop()

    def _check_interrupt(self, uc, address: int) -> bool:
        mask = self.board.interrupt_mask
        if not mask:
            return False
        cpsr = self._reg_read(UC_ARM_REG_CPSR)
        if (mask & 2) and not (cpsr & 0x40):
            new_cpsr = (cpsr & ~0x3F) | 0xD1
            vector = 0x0000001C
            claim = self.board._fiq_claim
            kind = 'fiq'
        elif (mask & 1) and not (cpsr & 0x80):
            new_cpsr = (cpsr & ~0x3F) | 0x92
            vector = 0x00000018
            claim = self.board._irq_claim
            kind = 'irq'
        else:
            return False
        self._reg_write(UC_ARM_REG_CPSR, new_cpsr)
        self._cached_cpsr = new_cpsr
        self._cpsr_valid = False
        self._ifm_valid = False
        self._reg_write(UC_ARM_REG_SPSR, cpsr)
        self._reg_write(UC_ARM_REG_LR, (address & ~1) + 4)
        self._reg_write(UC_ARM_REG_PC, vector)
        if self.on_event is not None:
            self._event(kind, pc=address, source=claim & 0xFF, priority=claim >> 8)
        return True

    def _make_insn_hook(self):
        board = self.board
        timer_step = board.timer_step
        recent_ring = self._recent_ring
        uc = self.uc

        def _insn(uc_h, address, size, _) -> None:
            try:
                if self._memory_user_access:
                    self._memory_user_access = False
                if self._instruction_pending:
                    target = board.ticks + timer_step
                    if target < board._next_deadline:
                        board.ticks = target
                    else:
                        self._instruction_pending = False
                        self._advance_board(1)
                        if self.reason:
                            return
                        self._instruction_pending = True
                elif self.reason:
                    return
                else:
                    self._instruction_pending = True
                if (board.interrupt_mask & self._block_irq_deliverable_mask) and self._check_interrupt(uc, address):
                    self._instruction_pending = False
                    return
                insns = self.instructions
                if insns >= self._limit:
                    self._instruction_pending = False
                    self.reason = 'instruction-limit'
                    uc.emu_stop()
                    return
                self.instructions = insns + 1
                recent_ring[insns & 31] = address
                if self._block_has_specials and (address in self._block_cp15 or address in self._block_user_transfers):
                    self._special_instruction(uc, address, size)
            except BaseException as exc:
                uc._hook_exception = exc
                uc.emu_stop()

        return _insn

    def _instruction(self, uc, address, size, _) -> None:
        try:
            if self._memory_user_access:
                self._memory_user_access = False
            board = self.board
            # Account for the preceding instruction before starting another one.
            # A UART deadline can stop here without inventing an extra instruction.
            if self._instruction_pending:
                target = board.ticks + board.timer_step
                if target < board._next_deadline:
                    board.ticks = target
                else:
                    self._instruction_pending = False
                    self._advance_board(1)
                    if self.reason:
                        return
                    self._instruction_pending = True
            elif self.reason:
                return
            else:
                self._instruction_pending = True
            if (board.interrupt_mask & self._block_irq_deliverable_mask) and self._check_interrupt(self.uc, address):
                self._instruction_pending = False
                return
            # Check the budget before execution; counted instructions include CP15
            # operations that this hook emulates itself.
            insns = self.instructions
            if insns >= self._limit:
                self._instruction_pending = False
                self.reason = 'instruction-limit'
                self.uc.emu_stop()
                return
            self.instructions = insns + 1
            self._recent_ring[insns & 31] = address
            if self._block_has_specials and (address in self._block_cp15 or address in self._block_user_transfers):
                self._special_instruction(self.uc, address, size)
        except BaseException as exc:
            self.uc._hook_exception = exc
            self.uc.emu_stop()

    def _special_instruction(self, uc, address: int, size: int, *,
                             native_word: int | None = None,
                             native_user_transfer: bool = False) -> None:
        """Shared architectural checks after either observer counts the opcode."""
        self._cpsr_valid = False
        self._ifm_valid = False
        transfer = (native_word if native_user_transfer else None) if native_word is not None else self._block_user_transfers.get(address)
        if transfer is not None:
            cpsr = self._reg_read(UC_ARM_REG_CPSR)
            if self._condition_passed(transfer >> 28, cpsr):
                try:
                    base_register = (transfer >> 16) & 15
                    if base_register == 15:
                        raise CpuModelError('unpredictable unprivileged transfer with r15 base')
                    # P=0 means the actual access uses Rn before the native
                    # instruction applies its immediate/register writeback.
                    memory_address = self._reg_read(REGISTERS[base_register])
                    access_size = 1 if transfer & (1 << 22) else 4
                    if memory_address & (access_size - 1):
                        raise CpuModelError('unaligned ARM946 unprivileged word transfer is not implemented')
                    self.cp15.check_access(memory_address, access_size,
                                           write=not bool(transfer & (1 << 20)), user=True)
                    self._memory_user_access = True
                except CpuModelError as exc:
                    self._fault(f'{exc} at {address:#x}')
            return
        word = native_word if native_word is not None else self._block_cp15.get(address)
        if word is None:
            return
        cpsr = self._reg_read(UC_ARM_REG_CPSR)
        # Only MRC/MCR p15, not CDP/LDC or unrecognized coprocessors.
        if not self._condition_passed(word >> 28, cpsr):
            return
        if (cpsr & 0x1F) == 0x10:
            self._fault(f'CP15 instruction in User mode at {address:#x} would raise '
                        'Undefined Instruction; exception delivery is not implemented')
            return
        key = ((word >> 21) & 7, (word >> 16) & 15, word & 15, (word >> 5) & 7)
        register = (word >> 12) & 15
        reading = bool(word & (1 << 20))
        try:
            if reading:
                value = self.cp15.read(key)
                if register == 15:
                    self._reg_write(UC_ARM_REG_CPSR, (cpsr & 0x0FFFFFFF) | (value & 0xF0000000))
                else:
                    self._reg_write(REGISTERS[register], value)
            else:
                if register == 15:
                    raise CpuModelError('unpredictable MCR with r15')
                value = self._reg_read(REGISTERS[register])
                operation = self.cp15.write(key, value)
                if operation == 'wait-for-interrupt':
                    self.reason = 'wait-for-interrupt'
                    self.uc.emu_stop()
                elif operation == 'invalidate-instruction-cache':
                    # The CPU profile has no caches, but Unicorn's translated
                    # blocks are the emulator's instruction cache: code written
                    # through another DRAM alias (0x20... for code executed at
                    # 0xA0...) is not reliably invalidated by Unicorn's own
                    # self-modifying-code tracking. Firmware invalidates the
                    # I-cache exactly when it has replaced code, so honour it.
                    self._invalidate_translations()
                elif operation == 'invalidate-instruction-cache-line':
                    self._invalidate_translations(value & ~0x1F, 0x20)
        except CpuModelError as exc:
            self._fault(f'{exc} at {address:#x}')
            return
        if self.on_event is not None:
            self._event('cp15-read' if reading else 'cp15-write', register=key, value=value)
        self._reg_write(UC_ARM_REG_PC, address + 4)

    def _precache_uart_status(self, *, invalidate_on_miss: bool = False) -> None:
        observer = self._native_observer
        if observer is None:
            return
        if (not observer.state.on_event and not self._mpu_user and not observer.state.user_access
                and (0xBD0B3C0E, 2, False, False, False) in self.cp15._allowed_access):
            page_off = self._mmio_page_offset.get(0xBD0B3000)
            if page_off is not None:
                board = self.board
                fifo_enabled = board._stored(0xBD0B4C00, 2) != 0
                tx_ready = board.uart_tx_pending is None and not board._uart_tx_fifo
                status = ((0x0180 if fifo_enabled else 0x0100) if tx_ready else 0) | (0x0004 if (fifo_enabled and board.uart_rx) else 0)
                off = page_off + 0xC0E
                self._mmio_u16[off >> 1] = status
                observer.cache_mmio_read(0xBD0B3C0E, 2, status, off)
        elif invalidate_on_miss:
            observer.invalidate_mmio_slot(0xBD0B3C0E, 2)

    def _precache_panel_input(self, page_off: int, *, user: bool = False,
                              invalidate_on_miss: bool = False) -> None:
        observer = self._native_observer
        if observer is None:
            return
        ep = self.board.panel.endpoint
        if (ep is not None and not ep.reset_asserted and ep.armed
                and ep.bit_count and ep.sampled_bit is not None
                and (0xBD0B1410, 2, False, False, user) in self.cp15._allowed_access):
            in_val = ep.sampled_bit << 8
            in_off = page_off + 0x410
            self._mmio_u16[in_off >> 1] = in_val
            observer.cache_mmio_read(0xBD0B1410, 2, in_val, in_off)
        elif invalidate_on_miss:
            observer.invalidate_mmio_slot(0xBD0B1410, 2)

    def _record_uart_write(self, size: int, value: int) -> None:
        _uc_reg_read(self._uch, UC_ARM_REG_PC, self._reg_buf_ptr)
        self.uart_writes.append({'pc': self._reg_buf.value,
                                 'address': UART_TX, 'size': size, 'value': value})

    def _mmio(self, uc, access, address, size, value, _) -> None:
        page_off = self._mmio_page_offset.get(address & ~0xFFF)
        if page_off is None:
            return
        observer = self._native_observer
        need_refresh = False
        try:
            if (observer is not None and not self._mpu_user
                    and not observer.state.user_access):
                state = observer.state
                if access == UC_MEM_READ:
                    if (address in _FAST_MMIO_READ_ADDRESSES
                            and self.cp15.is_access_allowed(address, size, write=False)
                            and ((address & ~0xFFF) != 0xBD040000
                                 or not (state.ack_802_dirty | state.cc8_dirty | state.timer_zeroed_mask))
                            and observer.try_fast_advance()):
                        self.instructions = state.instructions
                        self._instruction_pending = bool(state.pending)
                        self._memory_user_access = False
                        board = self.board
                        val = ((board.ticks + board._timer_offset) & 0xFFFFFFFF
                               if (address == 0xBD040814 and size == 4)
                               else board.read(address, size))
                        off = page_off + (address & 0xFFF)
                        if size == 2 and not (off & 1):
                            self._mmio_u16[off >> 1] = val
                        elif size == 4 and not (off & 3):
                            self._mmio_u32[off >> 2] = val
                        elif size == 1:
                            self._mmio_u8[off] = val
                        else:
                            self.uc.mem_write(address, val.to_bytes(size, 'big'))
                        if not state.on_event and not (off & (size - 1)):
                            observer.cache_mmio_read(address, size, val, off)
                        if self.on_event is not None:
                            _uc_reg_read(self._uch, UC_ARM_REG_PC, self._reg_buf_ptr)
                            self.on_event({'kind': 'mmio-read', 'pc': self._reg_buf.value,
                                           'instruction': self.instructions,
                                           'address': address, 'size': size, 'value': val})
                        return
                    if (address == 0xBD065808 and size == 2
                            and not self.board.i2c.busy
                            and (self.board.i2c._endpoint is None or not self.board.i2c._endpoint.busy)
                            and self.cp15.is_access_allowed(0xBD065808, 2, write=False)
                            and observer.try_fast_advance()):
                        self.instructions = state.instructions
                        self._instruction_pending = bool(state.pending)
                        self._memory_user_access = False
                        val = self.board.read(0xBD065808, 2)
                        off = page_off + 0x808
                        self._mmio_u16[off >> 1] = val
                        observer.update_deadline()
                        if (0xBD065800, 2, False, False, False) in self.cp15._allowed_access:
                            i2c = self.board.i2c
                            ival = i2c.control | (0x80 if i2c.busy else 0)
                            ioff = page_off + 0x800
                            self._mmio_u16[ioff >> 1] = ival
                            if not state.on_event:
                                observer.cache_mmio_read(0xBD065800, 2, ival, ioff)
                        if self.on_event is not None:
                            _uc_reg_read(self._uch, UC_ARM_REG_PC, self._reg_buf_ptr)
                            self.on_event({'kind': 'mmio-read', 'pc': self._reg_buf.value,
                                           'instruction': self.instructions,
                                           'address': 0xBD065808, 'size': 2, 'value': val})
                        return
                if access != UC_MEM_READ and not state.on_event:
                    if (size == 2
                            and (address == 0xBD0B1418 or address == UART_TX
                                 or address in (0xBD0B3C04, 0xBD0B4004, 0xBD040802, 0xBD040400,
                                                0xBD061C08, 0xBD0B1002, 0xBD0B100C,
                                                0xBD3008AC, 0xBD300CAC)
                                 or (0xBD0B2000 <= address < 0xBD0B2200 and (address & 7) in (2, 4))
                                 or 0xBD04081E <= address < 0xBD04083C)
                            and self.cp15.is_access_allowed(address, 2, write=True)):
                        if address == 0xBD0B1418 and (self.on_event is None or observer.try_fast_advance()):
                            if state.panel_write_count:
                                observer.flush_panel()
                            panel = self.board.panel
                            ep = panel.endpoint
                            need_precache = (
                                ep is None or ((value >> 3) & 1) == ep.reset_active_level
                                or (not (ep.output_word & 2) and bool(value & 2))
                            )
                            panel.write(0xBD0B1418, 2, value)
                            if need_precache:
                                self._precache_panel_input(page_off, invalidate_on_miss=True)
                            if self.on_event is not None:
                                self.instructions = state.instructions
                                self._event('mmio-write', address=0xBD0B1418, size=2, value=value)
                            return
                        board = self.board
                        if address == UART_TX:
                            if (board.uart_tx_pending is None and not board._uart_tx_fifo
                                    and observer.try_fast_advance()):
                                u16_val = value & 0xFFFF
                                board._store_raw(UART_TX, 2, u16_val)
                                board.uart_tx_pending = u16_val & 0xFF
                                board._update_uart_irqs()
                                board.schedule(board.uart_frame_ticks, board._uart_tx_callback)
                                observer.update_deadline()
                                self._precache_uart_status(invalidate_on_miss=True)
                                self._record_uart_write(2, value)
                                if self.on_event is not None:
                                    self.instructions = state.instructions
                                    self._event('mmio-write', address=UART_TX, size=2, value=value)
                                return
                        elif address in (0xBD0B3C04, 0xBD0B4004):
                            if observer.try_fast_advance():
                                u16_val = value & 0xFFFF
                                if address == 0xBD0B3C04:
                                    state.uart0_mask_dirty = 0
                                    state.uart0_mask = u16_val
                                board._store_raw(address, 2, u16_val)
                                board._update_uart_irqs()
                                if self.cp15.is_access_allowed(address, 2, write=False):
                                    off = page_off + (address & 0xFFF)
                                    self._mmio_u16[off >> 1] = u16_val
                                    observer.cache_mmio_read(address, 2, u16_val, off)
                                else:
                                    observer.invalidate_mmio_slot(address, 2)
                                if self.on_event is not None:
                                    self.instructions = state.instructions
                                    self._event('mmio-write', address=address, size=2, value=value)
                                return
                        elif address == 0xBD040802:
                            if observer.try_fast_advance():
                                if state.ack_802_dirty:
                                    state.ack_802_dirty = 0
                                    board.timer_pending = state.timer_pending
                                u16_val = value & 0xFFFF
                                board._store_raw(0xBD040802, 2, u16_val)
                                board.timer_pending &= ~u16_val
                                if state.bd040_base_ptr:
                                    state.cc8_write_gen = state.generation
                                board._update_timer_irqs()
                                if self.on_event is not None:
                                    self.instructions = state.instructions
                                    self._event('mmio-write', address=0xBD040802, size=2, value=value)
                                return
                        elif address == 0xBD040400:
                            if state.wdog_pet_dirty:
                                observer._flush_wdog_from_c()
                            if (not (value & 3 and not (board._stored(0xBD060002, 2) & 1))
                                    and observer.try_fast_advance()):
                                board.write(0xBD040400, 2, value)
                                u16_val = value & 0xFFFF
                                state._sdio_pad = u16_val
                                state.wdog_enabled = (
                                    1 if (not (board.irq_raw_sources & 0x60) and (board._stored(0xBD060002, 2) & 1)) else 0
                                )
                                if state.bd040_base_ptr:
                                    state.cc8_write_gen = state.generation
                                observer.update_deadline()
                                if self.cp15.is_access_allowed(0xBD040400, 2, write=False):
                                    off = page_off + 0x400
                                    self._mmio_u16[off >> 1] = u16_val
                                    observer.cache_mmio_read(0xBD040400, 2, u16_val, off)
                                if self.on_event is not None:
                                    self.instructions = state.instructions
                                    self._event('mmio-write', address=0xBD040400, size=2, value=value)
                                return
                        elif address == 0xBD061C08:
                            if self.on_event is None or observer.try_fast_advance():
                                u16_val = value & 0xFFFF
                                board._store_raw(0xBD061C08, 2, u16_val)
                                observer.invalidate_mmio_slot(0xBD061C08, 4)
                                if self.cp15.is_access_allowed(0xBD061C08, 2, write=False):
                                    off = page_off + 0xC08
                                    self._mmio_u16[off >> 1] = u16_val
                                    observer.cache_mmio_read(0xBD061C08, 2, u16_val, off)
                                else:
                                    observer.invalidate_mmio_slot(0xBD061C08, 2)
                                if self.on_event is not None:
                                    self.instructions = state.instructions
                                    self._event('mmio-write', address=0xBD061C08, size=2, value=value)
                                return
                        elif address == 0xBD0B100C:
                            if self.on_event is None or observer.try_fast_advance():
                                board._store_raw(0xBD0B100C, 2, value & 0xFFFF)
                                if self.on_event is not None:
                                    self.instructions = state.instructions
                                    self._event('mmio-write', address=0xBD0B100C, size=2, value=value)
                                return
                        elif address == 0xBD0B1002:
                            if self.on_event is None or observer.try_fast_advance():
                                board.write(0xBD0B1002, 2, value)
                                if self.cp15.is_access_allowed(0xBD0B1002, 2, write=False):
                                    u16_val = board._stored(0xBD0B1002, 2)
                                    off = page_off + 0x002
                                    self._mmio_u16[off >> 1] = u16_val
                                    observer.cache_mmio_read(0xBD0B1002, 2, u16_val, off)
                                else:
                                    observer.invalidate_mmio_slot(0xBD0B1002, 2)
                                if self.on_event is not None:
                                    self.instructions = state.instructions
                                    self._event('mmio-write', address=0xBD0B1002, size=2, value=value)
                                return
                        elif address in (0xBD3008AC, 0xBD300CAC):
                            if observer.try_fast_advance():
                                if address == 0xBD3008AC and (state._panel_pad & 0x20000):
                                    board.micci[0xBD300800].data = state._panel_pad & 0xFFFF
                                    state._panel_pad = 0
                                self.instructions = state.instructions
                                self._instruction_pending = bool(state.pending)
                                self._memory_user_access = False
                                board.write(address, 2, value)
                                observer.update_deadline()
                                data_addr = address + 4
                                mctrl = board.micci.get(address & ~0x3FF)
                                if (data_addr, 4, False, False, False) in self.cp15._allowed_access and mctrl is not None:
                                    mval = (mctrl.data & 0xFFFFFFFF) | (0x80000000 if mctrl.busy else 0)
                                    moff = page_off + (data_addr & 0xFFF)
                                    self._mmio_u32[moff >> 2] = mval
                                    observer.cache_mmio_read(data_addr, 4, mval, moff)
                                if address == 0xBD3008AC:
                                    if mctrl is not None and not mctrl.busy and (0xBD3008B0, 4, False, True, False) in self.cp15._allowed_access:
                                        state._panel_pad = 0x10000 | (mctrl.data & 0xFFFF)
                                    else:
                                        state._panel_pad = 0
                                if self.on_event is not None:
                                    self._event('mmio-write', address=address, size=2, value=value)
                                return
                        elif 0xBD0B2000 <= address < 0xBD0B2200 and (address & 7) in (2, 4):
                            if (value & 0xFFFF) in (0, 1) and (self.on_event is None or observer.try_fast_advance()):
                                board.write(address, 2, value)
                                if self.on_event is not None:
                                    self.instructions = state.instructions
                                    self._event('mmio-write', address=address, size=2, value=value)
                                return
                        elif (0xBD04081E <= address < 0xBD04083C
                                and ((address - 0xBD04081E) & 3) == 0 and (value & 0xFFFF) > 0
                                and observer.try_fast_advance()):
                            ch = (address - 0xBD04081E) >> 2
                            bit = 1 << ch
                            zm = state.timer_zeroed_mask
                            if zm & bit:
                                state.timer_zeroed_mask = zm & ~bit
                            if state.timer_val_dirty_mask & bit:
                                state.timer_val_dirty_mask &= ~bit
                            if state.timer_ch_dirty_mask & bit:
                                state.timer_ch_dirty_mask &= ~bit
                            u16_val = value & 0xFFFF
                            board._store_raw(address, 2, u16_val)
                            board._schedule_timer_channel(ch, u16_val)
                            state.timer_active_mask |= bit
                            if state.bd040_base_ptr:
                                state.cc8_write_gen = state.generation
                            observer.update_deadline()
                            if self.on_event is not None:
                                self.instructions = state.instructions
                                self._event('mmio-write', address=address, size=2, value=value)
                            return
                    elif (size == 4
                            and (address in (0xBD302018, 0xBD0B1008, 0xBD3008B0, 0xBD300CB0)
                                 or (0xBD0B0000 <= address <= 0xBD0B0038 and not (address & 3)))
                            and self.cp15.is_access_allowed(address, 4, write=True)):
                        if address == 0xBD0B1008 and (self.on_event is None or observer.try_fast_advance()):
                            self.board._store_raw(0xBD0B1008, 4, value & 0xFFFFFFFF)
                            if self.on_event is not None:
                                self.instructions = state.instructions
                                self._event('mmio-write', address=0xBD0B1008, size=4, value=value)
                            return
                        if address in (0xBD3008B0, 0xBD300CB0):
                            u32_val = value & 0xFFFFFFFF
                            mctrl = self.board.micci.get(address & ~0x3FF)
                            if (mctrl is not None and not mctrl.busy and not (u32_val & 0xFFFF0000)
                                    and (self.on_event is None or observer.try_fast_advance())):
                                mctrl.data = u32_val
                                if (address, 4, False, False, False) in self.cp15._allowed_access:
                                    off = page_off + (address & 0xFFF)
                                    self._mmio_u32[off >> 2] = u32_val
                                    observer.cache_mmio_read(address, 4, u32_val, off)
                                else:
                                    observer.invalidate_mmio_slot(address, 4)
                                if address == 0xBD3008B0:
                                    state._panel_pad = 0x10000 | (u32_val & 0xFFFF)
                                if self.on_event is not None:
                                    self.instructions = state.instructions
                                    self._event('mmio-write', address=address, size=4, value=value)
                                return
                            if observer.try_fast_advance():
                                if address == 0xBD3008B0 and (state._panel_pad & 0x20000):
                                    self.board.micci[0xBD300800].data = state._panel_pad & 0xFFFF
                                    state._panel_pad = 0
                                self.instructions = state.instructions
                                self._instruction_pending = bool(state.pending)
                                self._memory_user_access = False
                                self.board.write(address, 4, u32_val)
                                if (address, 4, False, False, False) in self.cp15._allowed_access:
                                    off = page_off + (address & 0xFFF)
                                    self._mmio_u32[off >> 2] = u32_val
                                    observer.cache_mmio_read(address, 4, u32_val, off)
                                if address == 0xBD3008B0 and mctrl is not None and not mctrl.busy:
                                    state._panel_pad = 0x10000 | (mctrl.data & 0xFFFF)
                                if self.on_event is not None:
                                    self._event('mmio-write', address=address, size=4, value=value)
                                return
                        if address == 0xBD302018 and (self.on_event is None or observer.try_fast_advance()):
                            u32_val = value & 0xFFFFFFFF
                            self.board._store_raw(0xBD302018, 4, u32_val)
                            self.board._store_raw(0xBD302010, 4, u32_val)
                            if (0xBD302010, 4, False, False, False) in self.cp15._allowed_access:
                                in_off = page_off + 0x010
                                self._mmio_u32[in_off >> 2] = u32_val
                                observer.cache_mmio_read(0xBD302010, 4, u32_val, in_off)
                            else:
                                observer.invalidate_mmio_slot(0xBD302010, 4)
                            if self.on_event is not None:
                                self.instructions = state.instructions
                                self._event('mmio-write', address=0xBD302018, size=4, value=value)
                            return
                        if 0xBD0B0000 <= address <= 0xBD0B0038 and observer.try_fast_advance():
                            self.instructions = state.instructions
                            self._instruction_pending = bool(state.pending)
                            self._memory_user_access = False
                            if state.sdio_dirty:
                                observer.flush_sdio()
                            u32_val = value & 0xFFFFFFFF
                            sdio = self.board.sdio
                            if address == 0xBD0B0030:
                                sdio.int_status &= ~(u32_val & 0xFFFFFEFF)
                                sdio._update_irq()
                            elif address in (0xBD0B0034, 0xBD0B0038):
                                if address == 0xBD0B0034:
                                    sdio.int_status_en = u32_val
                                else:
                                    sdio.int_signal_en = u32_val
                                sdio._update_irq()
                                if (address, 4, False, False, False) in self.cp15._allowed_access:
                                    off = page_off + (address & 0xFFF)
                                    self._mmio_u32[off >> 2] = u32_val
                                    observer.cache_mmio_read(address, 4, u32_val, off)
                                else:
                                    observer.invalidate_mmio_slot(address, 4)
                            else:
                                sdio.write(address, 4, u32_val)
                                if 0xBD0B0028 <= address < 0xBD0B0030:
                                    # Host/clock control read back modified values (e.g. Internal
                                    # Clock Stable); drop any cached read like BootHardware.write does.
                                    observer.invalidate_mmio_slot(address & ~3, 4)
                            if self.on_event is not None:
                                self._event('mmio-write', address=address, size=4, value=value)
                            return
            need_refresh = observer is not None
            if need_refresh:
                observer.synchronize(refresh=False)
            # Unicorn's ordinary memory hook can precede a protection fault.
            # Denied transactions must not change peripheral state.
            write = access != UC_MEM_READ
            user = self._mpu_user or self._memory_user_access
            if (address, size, False, write, user) not in self.cp15._allowed_access:
                self.cp15.check_access(address, size, write=write, user=user)
            if not write:
                value = self.board.read(address, size)
                off = page_off + (address & 0xFFF)
                if size == 2 and not (off & 1):
                    self._mmio_u16[off >> 1] = value
                elif size == 4 and not (off & 3):
                    self._mmio_u32[off >> 2] = value
                elif size == 1:
                    self._mmio_u8[off] = value
                else:
                    self.uc.mem_write(address, value.to_bytes(size, 'big'))
                if observer is not None and not (off & (size - 1)):
                    observer.cache_mmio_read(address, size, value, off)
                if address == UART_RX:
                    self._precache_uart_status()
                elif address == 0xBD065808 and observer is not None and not user and (0xBD065800, 2, False, False, False) in self.cp15._allowed_access:
                    i2c = self.board.i2c
                    ival = i2c.control | (0x80 if i2c.busy else 0)
                    ioff = page_off + 0x800
                    self._mmio_u16[ioff >> 1] = ival
                    observer.cache_mmio_read(0xBD065800, 2, ival, ioff)
                if self.on_event is not None:
                    self._event('mmio-read', address=address, size=size, value=value)
            else:
                if (self.spi_flash and address in (0xbd063818, 0xbd06381a, 0xbd06381c)
                        and value != (0x3c0, 0xfff, 0xfff)[(address - 0xbd063818) // 2]):
                    raise HardwareError('SPI window remapping beyond the explicit one-flash '
                                        'board profile is not implemented')
                self.board.write(address, size, value)
                if observer is not None and size == 2:
                    if (address & ~0xFFF) == 0xBD040000:
                        observer.cache_mmio_write(address, size)
                    elif address == 0xBD0B1418:
                        self._precache_panel_input(page_off, user=user)
                    elif address in (UART_TX, 0xBD0B4C00):
                        self._precache_uart_status()
                    elif address in (0xBD065800, 0xBD065802, 0xBD065808) and not user and (0xBD065800, 2, False, False, False) in self.cp15._allowed_access:
                        i2c = self.board.i2c
                        ival = i2c.control | (0x80 if i2c.busy else 0)
                        ioff = page_off + 0x800
                        self._mmio_u16[ioff >> 1] = ival
                        observer.cache_mmio_read(0xBD065800, 2, ival, ioff)
                    elif address in (0xBD0B3C04, 0xBD0B4004, 0xBD040400, 0xBD061C08) and not user and (address, 2, False, False, False) in self.cp15._allowed_access:
                        u16_val = value & 0xFFFF
                        off = page_off + (address & 0xFFF)
                        self._mmio_u16[off >> 1] = u16_val
                        observer.cache_mmio_read(address, 2, u16_val, off)
                elif observer is not None and size == 4 and not user:
                    if address == 0xBD302018 and (0xBD302010, 4, False, False, False) in self.cp15._allowed_access:
                        u32_val = value & 0xFFFFFFFF
                        in_off = page_off + 0x010
                        self._mmio_u32[in_off >> 2] = u32_val
                        observer.cache_mmio_read(0xBD302010, 4, u32_val, in_off)
                    elif address in (0xBD0B0034, 0xBD0B0038) and (address, 4, False, False, False) in self.cp15._allowed_access:
                        u32_val = value & 0xFFFFFFFF
                        off = page_off + (address & 0xFFF)
                        self._mmio_u32[off >> 2] = u32_val
                        observer.cache_mmio_read(address, 4, u32_val, off)
                if self.on_event is not None:
                    self._event('mmio-write', address=address, size=size, value=value)
                if address == UART_TX:
                    self._record_uart_write(size, value)
        except (HardwareError, CpuModelError, *_PERIPHERAL_ERRORS) as exc:
            self._fault(str(exc))
        except BaseException as exc:
            self.uc._hook_exception = exc
            if observer is not None:
                observer.state.stopped = 1
            self.uc.emu_stop()
        finally:
            if need_refresh:
                observer.refresh(full=self.on_event is not None)

    def _advance_board(self, instructions: int = 1) -> None:
        board = self.board
        target = board.ticks + instructions * board.timer_step
        if target < board._next_deadline:
            board.ticks = target
            return
        board.advance(instructions)
        # Most instructions produce no UART frame. Avoid allocating an
        # empty bytes object on every one; device deadlines still run.
        if board._uart_output:
            self._collect_uart()

    def _finish_instruction(self) -> None:
        self._cpsr_valid = False
        self._ifm_valid = False
        if self._native_observer is not None:
            self._native_observer.finish()
            return
        if self._instruction_pending:
            self._instruction_pending = False
            self._advance_board(1)

    def _mirror_spi_write(self, address: int, length: int) -> None:
        self.memory.mirror_flash_write(address, length, self.spi_flash.image)

    def _invalidate_translations(self, address: int | None = None, length: int = 0) -> None:
        """Drop Unicorn translated blocks: all of them, or those covering a range.

        Called for the guest's I-cache invalidation operations. The emulator's
        own block-metadata caches re-check code bytes on every hit and need no
        action here.
        """
        try:
            if address is None:
                self.uc.ctl_flush_tb()
            else:
                self.uc.ctl_remove_cache(address, address + length)
        except UcError:  # pragma: no cover - best effort; next fetch retranslates anyway
            pass

    def _collect_uart(self) -> None:
        for value in self.board.drain_uart():
            self.uart.append(value)
            if self.on_event is not None:
                self._event('uart-transmitted', value=value, ticks=self.board.ticks)
            if self.on_uart:
                self.on_uart(value)
            if self.reason:
                continue
            if self._stop == 'first-byte':
                self.reason = 'first-byte'
                self.uc.emu_stop()
            elif self._stop == 'banner' and self.uart.endswith(b'\n\r') and b'[Boot] Firmware build timestamp: ' in self.uart:
                self.reason = 'banner'
                self.uc.emu_stop()
        self._precache_uart_status()

    def run(self, max_instructions: int = 2_000_000, stop: str = 'banner') -> RunResult:
        if self._started:
            raise ValueError('create a new Emulator for each reset run')
        if max_instructions < 1:
            raise ValueError('max_instructions must be positive')
        if stop not in ('banner', 'first-byte', 'budget'):
            raise ValueError('stop must be banner, first-byte, or budget')
        self._started = True
        self._cpsr_valid = False
        self._ifm_valid = False
        self._limit, self._stop = max_instructions, stop
        bind_py = None
        if self._native_observer is not None:
            self._native_observer.configure(max_instructions)
        else:
            bind_py = getattr(self.uc, '_bind_py_emu', None)
            if bind_py is not None:
                bind_py(self)
        try:
            self.uc.emu_start(0, 0xFFFFFFFF)
        except (UcError, CpuModelError, HardwareError) as exc:
            self.reason = 'fault'
            self.error = self.memory.error or self.error or str(exc)
        finally:
            if bind_py is not None:
                bind_py(None)
        try:
            self._finish_instruction()
            self.memory.check_flash()
        except (CpuModelError, HardwareError) as exc:
            self.reason, self.error = 'fault', str(exc)
        return RunResult(self.reason or 'stopped', self.instructions,
                         self._reg_read(UC_ARM_REG_PC), bytes(self.uart), self.error)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dump', nargs='?', type=Path, default=Path(__file__).with_name('dump.bin'))
    parser.add_argument('--max-instructions', type=int, default=5_000_000)
    parser.add_argument('--stop', choices=('banner', 'first-byte', 'budget'), default='budget')
    parser.add_argument('--timer-step', type=int, default=1)
    parser.add_argument('--native-observer', action='store_true',
                        help='optional compiled instruction observer; CPU, firmware and devices remain unchanged')
    parser.add_argument('--full-emulation', action='store_true',
                        help='enable full Stage-22 hardware profile (factory EEPROM, serial panel, SPI flash, GPIO/ADC inputs, power button wakeup)')
    parser.add_argument('--no-mcasic', action='store_true', help='omit the inferred secondary-controller profile')
    parser.add_argument('--supply-mv', type=int, default=24000,
                        help='simulated external supply voltage in millivolts (default: 24000; unmeasured)')
    parser.add_argument('--gpio-input', action='append', default=[], metavar='PIN=LEVEL',
                        help='explicit external GPIO level (0 or 1); repeat for multiple pins')
    panel_group = parser.add_mutually_exclusive_group()
    panel_group.add_argument('--panel-input-word', type=lambda s: int(s, 0),
                        help='explicit static front-panel input register; not a detected panel response')
    panel_group.add_argument('--serial-panel-inputs', type=lambda s: int(s, 0), metavar='BITS',
                             help='attach serial panel with explicit 18 raw active-low inputs (0x3ffff: all released)')
    parser.add_argument('--spi-profile', choices=('byte-final', 'wide-final'),
                        help='explicit candidate SPI timing model, one flash, pulled-high absent slots; default is strict')
    eeprom_group = parser.add_mutually_exclusive_group()
    eeprom_group.add_argument('--eeprom', type=Path, help='separate 64 KiB I2C EEPROM image')
    eeprom_group.add_argument('--blank-eeprom', action='store_true',
                              help='attach an explicitly erased EEPROM; actual NVM contents remain unknown')
    eeprom_group.add_argument('--factory-eeprom', action='store_true',
                              help='attach a 64 KiB EEPROM populated with version-0x40 Board ID 0x68bf NVM fields')
    parser.add_argument('--uart-out', type=Path, help='save exact UART bytes, including LF/CR order')
    parser.add_argument('--trace', type=Path, help='write MMIO/CP15 events as JSONL')
    parser.add_argument('--report', type=Path, help='write run summary as JSON')
    args = parser.parse_args(argv)
    trace = None
    try:
        # Do not let output options overwrite the source dump or each other.
        outputs = [p.resolve() for p in (args.uart_out, args.trace, args.report) if p]
        inputs = [args.dump.resolve()] + ([args.eeprom.resolve()] if args.eeprom else [])
        if set(inputs) & set(outputs) or len(outputs) != len(set(outputs)):
            raise ValueError('input image, UART, trace, and report paths must be distinct')
        dump = args.dump.read_bytes()
        gpio_inputs = {1: 0, 2: 0, 3: 0, 8: 1, 11: 0, 15: 1} if args.full_emulation else {}
        seen_gpio_pins: set[int] = set()
        for setting in args.gpio_input:
            try:
                pin_text, level_text = setting.split('=')
                pin, level = int(pin_text, 0), int(level_text, 0)
            except ValueError:
                raise ValueError(f'invalid GPIO input {setting!r}; expected PIN=0 or PIN=1') from None
            if pin in seen_gpio_pins:
                raise ValueError(f'GPIO input {pin} was specified more than once')
            seen_gpio_pins.add(pin)
            gpio_inputs[pin] = level
        if args.full_emulation:
            if not (args.eeprom or args.factory_eeprom or args.blank_eeprom):
                args.factory_eeprom = True
            if args.panel_input_word is None and args.serial_panel_inputs is None:
                args.serial_panel_inputs = 0x3FFFF
            if args.spi_profile is None:
                args.spi_profile = 'byte-final'
        eeprom = (
            args.eeprom.read_bytes() if args.eeprom
            else EEPROM.create_factory_image() if args.factory_eeprom
            else bytes([0xFF]) * 0x10000 if args.blank_eeprom
            else None
        )
        analog_inputs = None
        if args.full_emulation:
            adc_state = [0]

            def adc_ch2_noise() -> int:
                adc_state[0] += 1
                step = adc_state[0]
                pair = (step - 1) // 2
                bit = (pair ^ (pair >> 1) ^ (pair >> 3)) & 1
                return 512 | (bit if (step & 1) else (bit ^ 1))

            analog_inputs = {i: 512 for i in range(1, 20) if i != 15}
            analog_inputs[2] = adc_ch2_noise
        if args.trace:
            trace = args.trace.open('w', encoding='utf-8')

        def output_byte(value: int) -> None:
            sys.stdout.write(chr(value))
            sys.stdout.flush()

        def event(record: dict) -> None:
            if trace:
                trace.write(json.dumps(record) + '\n')

        emulator = Emulator(dump, timer_step=args.timer_step, mcasic=not args.no_mcasic,
                            supply_mv=args.supply_mv, eeprom=eeprom, gpio_inputs=gpio_inputs,
                            analog_inputs=analog_inputs,
                            power_button_wakeup=args.full_emulation,
                            panel_input_word=args.panel_input_word,
                            serial_panel_inputs=args.serial_panel_inputs,
                            serial_panel_tail_bit=1 if args.full_emulation else None,
                            spi_profile=args.spi_profile,
                            native_observer=args.native_observer,
                            on_uart=output_byte,
                            on_event=event if trace else None)
        result = emulator.run(args.max_instructions, args.stop)
        if args.uart_out:
            args.uart_out.write_bytes(result.uart)
        if args.report:
            args.report.write_text(json.dumps({
                'dump_sha256': emulator.dump_sha256, 'reason': result.reason,
                'native_observer': args.native_observer,
                'instructions': result.instructions, 'pc': f'{result.pc:#010x}',
                'uart_bytes': len(result.uart), 'uart_text': result.uart.decode('latin-1'),
                'uart_tx_pending': emulator.board.uart_tx_pending,
                'first_uart_write': emulator.uart_writes[0] if emulator.uart_writes else None,
                'recent_pcs': [f'{pc:#010x}' for pc in emulator.recent_pcs],
                'registers': {name: f'{emulator.uc.reg_read(reg):#010x}' for name, reg in
                              zip([f'r{i}' for i in range(13)] + ['sp', 'lr', 'pc'], REGISTERS)},
                'cpsr': f'{emulator.uc.reg_read(UC_ARM_REG_CPSR):#010x}',
                'virtual_ticks': emulator.board.ticks,
                'timer': emulator.board.timer,
                'analog_profile': emulator.mcasic.analog_profile if emulator.mcasic else None,
                'gpio_external_inputs': gpio_inputs,
                'gpio_outputs': {str(pin): emulator.board.gpio.output_latches[pin]
                                 for pin, direction in enumerate(emulator.board.gpio.directions) if direction},
                'panel_input_word': args.panel_input_word,
                'serial_panel_inputs': args.serial_panel_inputs,
                'serial_panel_frames': emulator.board.panel.endpoint.frames if emulator.board.panel.endpoint else None,
                'serial_panel_display': (
                    {
                        'latest': emulator.board.panel.endpoint.decode_frame(),
                        'temporal': emulator.board.panel.endpoint.analyze_frames(),
                    }
                    if emulator.board.panel.endpoint and emulator.board.panel.endpoint.frames
                    else None
                ),
                'spi_profile': args.spi_profile,
                'spi_miso_pull': emulator.board.spi.miso_pull,
                'spi_window_registers': emulator.board.spi.window_registers,
                'spi_transfers': emulator.board.spi.transfers,
                'spi_flash_transactions': emulator.spi_flash.transactions if emulator.spi_flash else None,
                'panel_output_writes': emulator.board.panel.output_writes,
                'eeprom_profile': 'supplied' if args.eeprom else 'factory' if args.factory_eeprom else 'erased' if args.blank_eeprom else 'absent',
                'eeprom_input_sha256': emulator.eeprom_sha256,
                'eeprom_write_protect_gpio': 7 if emulator.eeprom else None,
                'i2c_transactions': emulator.board.i2c.transactions,
                'micci_transactions': {f'{base:#010x}': controller.transactions
                                       for base, controller in emulator.board.micci.items()},
                'flash_unchanged': emulator.memory.check_flash(),
                'error': result.error,
            }, indent=2) + '\n', encoding='utf-8')
        print(f'\n[{result.reason}] {result.instructions:,} instructions; '
              f'PC={result.pc:#010x}; UART={len(result.uart)} bytes', file=sys.stderr)
        if result.error:
            print(result.error, file=sys.stderr)
            print('Recent PCs: ' + ' '.join(f'{pc:#x}' for pc in emulator.recent_pcs), file=sys.stderr)
        return 0 if result.reason in ('banner', 'first-byte') else 1
    except (OSError, ValueError, UcError) as exc:
        print(f'emulator: {exc}', file=sys.stderr)
        return 2
    finally:
        if trace:
            trace.close()


if __name__ == '__main__':
    raise SystemExit(main())
