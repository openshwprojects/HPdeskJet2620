"""Optional native instruction observer for the unchanged Unicorn CPU core.

Instruction accounting, the recent-PC ring, deadline comparisons, and a block
metadata cache run in C. Cache hits require freshly read code bytes and matching
CPU execution mode; CP15/MPU/TCM changes invalidate previous checks. CP15, MPU
checks on misses, MMIO, devices, and TCM behavior keep their Python models.
Time may accumulate between observations, but synchronization happens before
every MMIO access, special instruction, fault, and device deadline. Block checks
do not observe device time. The pending instruction completes at the same
boundary as the reference Python hook. Unicorn executes every CPU instruction.

Custom Python code/block hooks that inspect Emulator.instructions or board.ticks
must call emulator._native_observer.synchronize() on the emulation thread first.
The built-in event callbacks and final RunResult already see synchronized state.
Read-only progress monitoring can inspect state.instructions directly; it must
never synchronize or call Unicorn from a second thread while emulation runs.

The helper has no Unicorn link dependency: its stop function is supplied from
the already loaded Unicorn library. A local GCC compiles this repository's C
source into artifacts/native, keyed by source and host architecture. No binary
is downloaded. Set NATIVE_OBSERVER_CC to an alternate compiler executable.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
from pathlib import Path
import platform
import shutil
import weakref
import subprocess
import uuid

from unicorn import UC_HOOK_CODE, UC_HOOK_BLOCK, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE, UcError
from unicorn.arm_const import (
    UC_ARM_REG_CPSR, UC_ARM_REG_SPSR, UC_ARM_REG_LR, UC_ARM_REG_PC,
)
from unicorn.unicorn_py3 import unicorn as bindings


class NativeObserverError(ValueError):
    """The optional native observer cannot be built or loaded safely."""


_BRIDGE = ctypes.CFUNCTYPE(None, ctypes.c_uint64, ctypes.c_uint32, ctypes.c_uint32)
_MMIO_BRIDGE = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_uint64, ctypes.c_int, ctypes.c_int64)
_STOP_PTR = ctypes.cast(bindings.uclib.uc_emu_stop, ctypes.c_void_p).value
_READ_REG_PTR = ctypes.cast(bindings.uclib.uc_reg_read, ctypes.c_void_p).value
_WRITE_REG_PTR = ctypes.cast(bindings.uclib.uc_reg_write, ctypes.c_void_p).value
_READ_MEM_PTR = ctypes.cast(bindings.uclib.uc_mem_read, ctypes.c_void_p).value
_CACHED_LIB_KEY: tuple[int, int] | None = None
_CACHED_LIB: ctypes.CDLL | None = None
_CACHED_PATH: Path | None = None

_PURE_MMIO_READ_ADDRESSES = frozenset({
    0xBD040000, 0xBD040400, 0xBD040808, 0xBD04080A, 0xBD04080C,
    0xBD04080E, 0xBD040C40, 0xBD040C44, 0xBD040C48, 0xBD040C4C,
    0xBD050030, 0xBD05004C, 0xBD061C08, 0xBD062048, 0xBD065800, 0xBD080008,
    0xBD0B0024, 0xBD0B0028, 0xBD0B002C, 0xBD0B0034, 0xBD0B0038,
    0xBD0B0040, 0xBD0B0044, 0xBD0B0048,
    0xBD0B1002, 0xBD0B1004, 0xBD0B1410,
    0xBD0B2008, 0xBD0B2010, 0xBD0B2018, 0xBD0B2040,
    0xBD0B3C00, 0xBD0B3C04, 0xBD0B3C0E, 0xBD0B3C10,
    0xBD0B4004, 0xBD0B400E, 0xBD0B4010,
    0xBD300890, 0xBD3008A6, 0xBD3008B0, 0xBD300C90, 0xBD300CA6, 0xBD300CB0,
    0xBD302010, 0xBD302410,
})


class _BlockTag(ctypes.Structure):
    _fields_ = [
        ('address', ctypes.c_uint64), ('host_ptr', ctypes.c_uint64),
        ('generation', ctypes.c_uint32), ('size', ctypes.c_uint32),
        ('cpsr', ctypes.c_uint32), ('special_count', ctypes.c_uint16),
        ('cpsr_static', ctypes.c_uint8), ('_pad', ctypes.c_uint8),
    ]


class _BlockData(ctypes.Structure):
    _fields_ = [
        ('specials', ctypes.c_uint64 * 32), ('words', ctypes.c_uint32 * 32),
        ('types', ctypes.c_uint8 * 32), ('bytes', ctypes.c_uint8 * 1024),
    ]


class _MmioSlot(ctypes.Structure):
    _fields_ = [
        ('address', ctypes.c_uint64), ('mmio_gen', ctypes.c_uint64),
        ('host_ptr', ctypes.c_uint64), ('size', ctypes.c_uint32),
        ('value', ctypes.c_uint32),
    ]


class _State(ctypes.Structure):
    _fields_ = [
        ('instructions', ctypes.c_uint64), ('completed', ctypes.c_uint64),
        ('limit', ctypes.c_uint64), ('deadline', ctypes.c_uint64),
        ('pending', ctypes.c_uint64), ('stopped', ctypes.c_uint64),
        ('user_access', ctypes.c_uint64), ('irq_mask', ctypes.c_uint64),
        ('recent_total', ctypes.c_uint64),
        ('recent', ctypes.c_uint64 * 32), ('special_count', ctypes.c_uint64),
        ('specials', ctypes.c_uint64 * 32), ('words', ctypes.c_uint32 * 32),
        ('types', ctypes.c_uint8 * 32), ('all_special', ctypes.c_uint64),
        ('current_word', ctypes.c_uint64), ('current_kind', ctypes.c_uint64),
        ('generation', ctypes.c_uint64), ('cache_hits', ctypes.c_uint64),
        ('cache_misses', ctypes.c_uint64),
        ('cpsr_register', ctypes.c_uint32), ('spsr_register', ctypes.c_uint32),
        ('lr_register', ctypes.c_uint32), ('pc_register', ctypes.c_uint32),
        ('block_cpsr', ctypes.c_uint32), ('cpsr_valid', ctypes.c_uint32),
        ('maintenance_count', ctypes.c_uint64), ('on_event', ctypes.c_uint64),
        ('flash_ptr', ctypes.c_uint64), ('dram_ptr', ctypes.c_uint64),
        ('itcm_ptr', ctypes.c_uint64), ('tcm_load_mode', ctypes.c_uint64),
        ('itcm_enabled', ctypes.c_uint64), ('itcm_size', ctypes.c_uint64),
        ('itcm_phys_size', ctypes.c_uint64), ('dtcm_enabled', ctypes.c_uint64),
        ('dtcm_base', ctypes.c_uint64), ('dtcm_end', ctypes.c_uint64),
        ('base_ticks', ctypes.c_uint64), ('synced_completed', ctypes.c_uint64),
        ('timer_step', ctypes.c_uint64), ('timer_offset', ctypes.c_uint64),
        ('bd040_base_ptr', ctypes.c_uint64), ('bd040_read_gen', ctypes.c_uint64),
        ('cc8_write_gen', ctypes.c_uint64), ('cc8_dirty', ctypes.c_uint64),
        ('cc8_value', ctypes.c_uint32), ('timer_pending', ctypes.c_uint32),
        ('irq_claim', ctypes.c_uint32), ('fiq_claim', ctypes.c_uint32),
        ('max_irq_claim', ctypes.c_uint32), ('max_fiq_claim', ctypes.c_uint32),
        ('timer_mask0', ctypes.c_uint32), ('timer_mask1', ctypes.c_uint32),
        ('timer_mask_union', ctypes.c_uint32), ('nt_irq_claim', ctypes.c_uint32),
        ('nt_fiq_claim', ctypes.c_uint32), ('ack_802_dirty', ctypes.c_uint32),
        ('ack_802_value', ctypes.c_uint32), ('timer_active_mask', ctypes.c_uint32),
        ('timer_zeroed_mask', ctypes.c_uint32), ('us_ticks', ctypes.c_uint32),
        ('timers_in_c', ctypes.c_uint32), ('timer_ch_dirty_mask', ctypes.c_uint32),
        ('timer_expired_mask', ctypes.c_uint32), ('timer_val_dirty_mask', ctypes.c_uint32),
        ('timer_reload_zero_mask', ctypes.c_uint32), ('_timer_pad', ctypes.c_uint32),
        ('irq18_claim', ctypes.c_uint32), ('fiq18_claim', ctypes.c_uint32),
        ('irq19_claim', ctypes.c_uint32), ('fiq19_claim', ctypes.c_uint32),
        ('timer_last_val', ctypes.c_uint16 * 8),
        ('timer_ch_deadline', ctypes.c_uint64 * 8),
        ('timer_ch_period', ctypes.c_uint64 * 8),
        ('board_deadline', ctypes.c_uint64),
        ('next_timer_deadline', ctypes.c_uint64),
        ('bd0b3_base_ptr', ctypes.c_uint64), ('uart0_fast_gen', ctypes.c_uint64),
        ('uart0_mask', ctypes.c_uint32), ('uart0_mask_dirty', ctypes.c_uint32),
        ('uart0_state', ctypes.c_uint32), ('uart0_irq_claim', ctypes.c_uint32),
        ('uart_fifo_enabled', ctypes.c_uint32), ('uart_frame_steps', ctypes.c_uint32),
        ('uart_tx_active', ctypes.c_uint32), ('uart_tx_bridge_mode', ctypes.c_uint32),
        ('uart_tx_pending_byte', ctypes.c_uint32), ('uart_tx_write_count', ctypes.c_uint32),
        ('uart_tx_out_count', ctypes.c_uint32), ('uart_rx_head', ctypes.c_uint32),
        ('uart_rx_count', ctypes.c_uint32), ('uart_tx_trigger_byte', ctypes.c_uint32),
        ('uart_tx_deadline', ctypes.c_uint64),
        ('uart_tx_write_pcs', ctypes.c_uint32 * 256),
        ('uart_tx_write_insns', ctypes.c_uint32 * 256),
        ('uart_tx_out_insns', ctypes.c_uint32 * 256),
        ('uart_tx_write_vals', ctypes.c_uint16 * 256),
        ('uart_tx_out_bytes', ctypes.c_uint8 * 256),
        ('uart_rx_buf', ctypes.c_uint8 * 256),
        ('bd0b1_base_ptr', ctypes.c_uint64), ('panel_fast_gen', ctypes.c_uint64),
        ('panel_shifted_output', ctypes.c_uint64),
        ('panel_output_word', ctypes.c_uint32), ('panel_armed_strobe', ctypes.c_uint32),
        ('panel_bit_count', ctypes.c_uint32), ('panel_frame_bits', ctypes.c_uint32),
        ('panel_input_width', ctypes.c_uint32), ('panel_latched_inputs', ctypes.c_uint32),
        ('panel_signature', ctypes.c_uint32), ('panel_reset_level', ctypes.c_uint32),
        ('panel_tail_bit', ctypes.c_int32), ('panel_sampled_bit', ctypes.c_int32),
        ('panel_write_count', ctypes.c_uint32), ('_panel_pad', ctypes.c_uint32),
        ('panel_writes_buf', ctypes.c_uint8 * 256),
        ('bd0b0_base_ptr', ctypes.c_uint64), ('sdio_fast_gen', ctypes.c_uint64),
        ('sdio_int_status', ctypes.c_uint32), ('sdio_status_en', ctypes.c_uint32),
        ('sdio_signal_en', ctypes.c_uint32), ('sdio_resp0', ctypes.c_uint32),
        ('sdio_sdma_addr', ctypes.c_uint32), ('sdio_blk_reg', ctypes.c_uint32),
        ('sdio_arg', ctypes.c_uint32), ('sdio_card_irq', ctypes.c_uint32),
        ('sdio_dirty', ctypes.c_uint32), ('_sdio_pad', ctypes.c_uint32),
        ('wdog_enabled', ctypes.c_uint32), ('wdog_pet_dirty', ctypes.c_uint32),
        ('wdog_pet_comp', ctypes.c_uint64),
        ('mmio_gen', ctypes.c_uint64),
        ('bridge', ctypes.c_void_p), ('mmio_bridge', ctypes.c_void_p),
        ('stop', ctypes.c_void_p), ('read_reg', ctypes.c_void_p),
        ('write_reg', ctypes.c_void_p), ('read_mem', ctypes.c_void_p),
        ('mmio_cache', _MmioSlot * 64),
        ('exec_page_tag', ctypes.c_uint64 * 4096),
        ('tags', _BlockTag * 8192),
        ('data', _BlockData * 8192),
    ]


def build_native_observer() -> Path:
    global _CACHED_LIB_KEY, _CACHED_PATH
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        raise NativeObserverError('native observer currently requires a 64-bit host')
    source = Path(__file__).with_suffix('.c')
    st = source.stat()
    key = (st.st_mtime_ns, st.st_size)
    if key == _CACHED_LIB_KEY and _CACHED_PATH is not None and _CACHED_PATH.exists():
        return _CACHED_PATH
    arch = os.environ.get('PROCESSOR_ARCHITECTURE') or platform.machine()
    identity = source.read_bytes() + arch.encode() + os.name.encode()
    digest = hashlib.sha256(identity).hexdigest()[:20]
    suffix = '.dll' if os.name == 'nt' else '.so'
    destination = source.parent / 'artifacts' / 'native' / f'observer-{digest}{suffix}'
    if destination.exists():
        _CACHED_LIB_KEY, _CACHED_PATH = key, destination
        return destination
    compiler = os.environ.get('NATIVE_OBSERVER_CC') or shutil.which('gcc')
    if not compiler and os.name == 'nt':
        for candidate in (Path('W:/TOOLS/msys64/ucrt64/bin/gcc.exe'),
                          Path('C:/Qt/Tools/mingw1310_64/bin/gcc.exe')):
            if candidate.is_file():
                compiler = str(candidate)
                break
    if not compiler:
        raise NativeObserverError('native observer needs GCC; set NATIVE_OBSERVER_CC to its executable')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f'{destination.stem}-{uuid.uuid4().hex}{suffix}')
    command = [compiler, '-O3', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared']
    if os.name != 'nt':
        command.append('-fPIC')
    command.extend([str(source), '-o', str(temporary)])
    environment = os.environ.copy()
    # GCC's cc1/linker may live outside bin and still need its runtime DLLs.
    environment['PATH'] = str(Path(compiler).resolve().parent) + os.pathsep + environment.get('PATH', '')
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60,
                                env=environment)
        if result.returncode:
            raise NativeObserverError(f'native observer compiler failed ({result.returncode}): '
                                      f'{result.stderr.strip()}')
        # Concurrent builders may have produced the same immutable binary.
        if not destination.exists():
            temporary.replace(destination)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NativeObserverError(f'native observer build failed: {exc}') from exc
    finally:
        temporary.unlink(missing_ok=True)
    _CACHED_LIB_KEY, _CACHED_PATH = key, destination
    return destination


def _get_native_library() -> ctypes.CDLL:
    global _CACHED_LIB
    if _CACHED_LIB is not None:
        return _CACHED_LIB
    path = build_native_observer()
    if _CACHED_LIB is None or getattr(_CACHED_LIB, '_loaded_path', None) != path:
        lib = ctypes.CDLL(str(path))
        lib.observer_size.restype = ctypes.c_size_t
        lib.observer_abi.restype = ctypes.c_uint32
        if hasattr(lib, 'native_unpack_lzss'):
            lib.native_unpack_lzss.argtypes = (
                ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t
            )
            lib.native_unpack_lzss.restype = ctypes.c_int64
        if hasattr(lib, 'native_zero_dram'):
            lib.native_zero_dram.argtypes = (ctypes.c_void_p, ctypes.c_size_t)
            lib.native_zero_dram.restype = None
        if hasattr(lib, 'native_alloc_dram'):
            lib.native_alloc_dram.argtypes = (ctypes.c_size_t,)
            lib.native_alloc_dram.restype = ctypes.c_void_p
        if hasattr(lib, 'native_prepare_dram'):
            lib.native_prepare_dram.argtypes = (
                ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                ctypes.c_size_t, ctypes.c_int, ctypes.c_size_t, ctypes.c_void_p
            )
            lib.native_prepare_dram.restype = ctypes.c_uint64
        if hasattr(lib, 'native_dirty_dram_offsets'):
            lib.native_dirty_dram_offsets.argtypes = (
                ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t
            )
            lib.native_dirty_dram_offsets.restype = ctypes.c_int64
        if lib.observer_abi() != 13 or lib.observer_size() != ctypes.sizeof(_State):
            raise NativeObserverError('native observer ABI does not match its Python state layout')
        lib._loaded_path = path
        _CACHED_LIB = lib
    return _CACHED_LIB


_STATE_POOL: list[_State] = []
_STATE_ALLOCATED = 0
_NEXT_CONFIG_GEN = 0
_STATE_HEADER_SIZE = _State.exec_page_tag.offset


class NativeObserver:
    def __init__(self, emulator):
        global _NEXT_CONFIG_GEN, _STATE_ALLOCATED
        self.emulator = emulator
        self.board = emulator.board
        self.board._observer = self
        self.board.panel._observer = self
        self.board.sdio._observer = self
        mem = emulator.memory
        flash_ptr = ctypes.addressof(mem._flash)
        dram_ptr = ctypes.addressof(mem._dram)
        itcm_ptr = ctypes.addressof(mem._tcm['itcm']) if mem._tcm['itcm'] is not None else 0
        cache_key = (flash_ptr, dram_ptr, itcm_ptr, mem._mappings, getattr(emulator, '_mpu_plan_key', None))
        pages = self.board.pages
        pages_range = (pages[0], pages[-1] + 0xFFF) if pages else None
        uc_obj = emulator.uc
        pooled_obs = getattr(uc_obj, '_pooled_native_obs', None) if mem._pooled_uc else None
        reused_cache = False
        self._is_uc_pooled = False
        if pooled_obs is not None and pooled_obs[2] == pages_range:
            self.state, self.handles, _ = pooled_obs
            self._is_uc_pooled = True
            if getattr(mem, '_code_cache_valid', False) and getattr(self.state, '_last_cache_key', None) == cache_key:
                reused_cache = True
            ctypes.memset(ctypes.addressof(self.state), 0, _STATE_HEADER_SIZE)
        else:
            if _STATE_POOL:
                match_idx = -1
                if getattr(mem, '_code_cache_valid', False):
                    for idx in range(len(_STATE_POOL) - 1, -1, -1):
                        if getattr(_STATE_POOL[idx], '_last_cache_key', None) == cache_key:
                            match_idx = idx
                            break
                if match_idx >= 0:
                    self.state = _STATE_POOL.pop(match_idx)
                    reused_cache = True
                else:
                    self.state = _STATE_POOL.pop()
                ctypes.memset(ctypes.addressof(self.state), 0, _STATE_HEADER_SIZE)
            else:
                _STATE_ALLOCATED += 1
                self.state = _State()
            self.handles = []
        if reused_cache:
            self._config_gen = self.state._last_config_gen
        else:
            _NEXT_CONFIG_GEN += 1
            self._config_gen = _NEXT_CONFIG_GEN
        self.state.generation = (self._config_gen << 1) | (1 if emulator.cp15.mpu_enabled else 0)
        self.state.mmio_gen = 1
        self._synced_completed = 0
        self._synced_recent_total = 0
        self._synced_maintenance = 0
        self._last_mappings = None
        self._synchronizing = False
        self.library = _get_native_library()
        self._callback = _BRIDGE(self._callback if hasattr(self, '_callback') else self._bridge)
        self._mmio_callback = _MMIO_BRIDGE(self._mmio_bridge)
        self.state.bridge = ctypes.cast(self._callback, ctypes.c_void_p).value
        self.state.mmio_bridge = ctypes.cast(self._mmio_callback, ctypes.c_void_p).value
        self.state.stop = _STOP_PTR
        self.state.read_reg = _READ_REG_PTR
        self.state.write_reg = _WRITE_REG_PTR
        self.state.read_mem = _READ_MEM_PTR
        self.state.cpsr_register = UC_ARM_REG_CPSR
        self.state.spsr_register = UC_ARM_REG_SPSR
        self.state.lr_register = UC_ARM_REG_LR
        self.state.pc_register = UC_ARM_REG_PC
        self.state.deadline = 0xFFFFFFFFFFFFFFFF
        self.state.board_deadline = 0xFFFFFFFFFFFFFFFF
        self.state.flash_ptr = flash_ptr
        self.state.dram_ptr = dram_ptr
        self.state.itcm_ptr = itcm_ptr
        page_offs = emulator._mmio_page_offset
        base_ptr = emulator._mmio_base_ptr
        timer_page_off = page_offs.get(0xBD040000)
        self.state.bd040_base_ptr = base_ptr + timer_page_off if timer_page_off is not None else 0
        b3_off = page_offs.get(0xBD0B3000)
        self.state.bd0b3_base_ptr = base_ptr + b3_off if b3_off is not None else 0
        b1_off = page_offs.get(0xBD0B1000)
        self.state.bd0b1_base_ptr = base_ptr + b1_off if b1_off is not None else 0
        b0_off = page_offs.get(0xBD0B0000)
        self.state.bd0b0_base_ptr = base_ptr + b0_off if b0_off is not None else 0
        self.sync_memory_map()
        if not self._is_uc_pooled:
            hooks = [
                (UC_HOOK_BLOCK, self.library.observer_block, 1, 0),
                (UC_HOOK_CODE, self.library.observer_code, 1, 0),
            ]
            if pages:
                hooks.append((UC_HOOK_MEM_READ | UC_HOOK_MEM_WRITE, self.library.observer_mmio,
                              pages[0], pages[-1] + 0xFFF))
            persist_on_uc = mem._pooled_uc and pooled_obs is None
            for hook_type, callback, begin, end in hooks:
                handle = bindings.uc_hook_h()
                result = bindings.uclib.uc_hook_add(
                    emulator.uc._uch, ctypes.byref(handle), hook_type,
                    ctypes.cast(callback, ctypes.c_void_p),
                    ctypes.byref(self.state), ctypes.c_uint64(begin), ctypes.c_uint64(end))
                if result:
                    for old in self.handles:
                        emulator.uc.hook_del(old)
                    raise UcError(result)
                h = handle.value
                self.handles.append(h)
                if not persist_on_uc:
                    emulator.memory._all_hooks.append(h)
                    emulator.memory._cb_refs[h] = self
                    if not emulator.memory._pooled_uc:
                        emulator.uc._callbacks[h] = self
            if persist_on_uc:
                self._is_uc_pooled = True
                uc_obj._pooled_native_obs = (self.state, list(self.handles), pages_range)

    def _release(self) -> None:
        state = getattr(self, 'state', None)
        if state is not None:
            self.state = None
            emu = getattr(self, 'emulator', None)
            state._last_cache_key = (
                state.flash_ptr,
                state.dram_ptr,
                state.itcm_ptr,
                self._last_mappings,
                getattr(emu, '_mpu_plan_key', None) if emu is not None else None,
            )
            state._last_config_gen = self._config_gen
            if not getattr(self, '_is_uc_pooled', False) and len(_STATE_POOL) < 4:
                _STATE_POOL.append(state)
        self.emulator = None
        self._callback = None
        self._mmio_callback = None
        board = getattr(self, 'board', None)
        if board is not None:
            board._observer = None
            board.panel._observer = None
            board.sdio._observer = None
            self.board = None

    def __del__(self) -> None:
        self._release()

    def sync_memory_map(self) -> None:
        global _NEXT_CONFIG_GEN
        mappings = self.emulator.memory._mappings
        if mappings == self._last_mappings:
            return
        if self._last_mappings is not None:
            _NEXT_CONFIG_GEN += 1
            self._config_gen = _NEXT_CONFIG_GEN
            self.sync_mpu_mode()
        self._last_mappings = mappings
        state = self.state
        state.tcm_load_mode = 1 if any(m.load_mode for m in mappings) else 0
        itcm = next((m for m in mappings if m.kind == 'itcm'), None)
        if itcm is not None:
            state.itcm_enabled = 1
            state.itcm_size = itcm.size
            state.itcm_phys_size = itcm.physical_size
        else:
            state.itcm_enabled = 0
            state.itcm_size = 0
            state.itcm_phys_size = 0
        dtcm = next((m for m in mappings if m.kind == 'dtcm'), None)
        if dtcm is not None:
            state.dtcm_enabled = 1
            state.dtcm_base = dtcm.address
            state.dtcm_end = dtcm.address + dtcm.size
        else:
            state.dtcm_enabled = 0
            state.dtcm_base = 0
            state.dtcm_end = 0

    def configure(self, limit: int) -> None:
        if not 0 < limit < 0xFFFFFFFFFFFFFFFF:
            raise NativeObserverError('native observer instruction budget must fit 64 bits')
        state = self.state
        state.limit = limit
        state.cpsr_valid = 0
        self._uart0_allowed_gen = 0
        self._panel_allowed_gen = 0
        self._sdio_allowed_gen = 0
        state.bd040_read_gen = 0
        state.cc8_write_gen = 0
        state.panel_fast_gen = 0
        state.sdio_fast_gen = 0
        board = self.board
        board._state_gen += 1
        board._irq_cfg_dirty = True
        board._timer_mask_dirty = True
        td = board.timer_deadline
        board._timer_active_mask = (
            (1 if td[0] is not None else 0)
            | (2 if td[1] is not None else 0)
            | (4 if td[2] is not None else 0)
            | (8 if td[3] is not None else 0)
            | (16 if td[4] is not None else 0)
            | (32 if td[5] is not None else 0)
            | (64 if td[6] is not None else 0)
            | (128 if td[7] is not None else 0)
        )
        board._recompute_interrupts()
        self.refresh()

    def _flush_timer_channels_from_c(self) -> None:
        board, state = self.board, self.state
        if state.timer_val_dirty_mask:
            vm = state.timer_val_dirty_mask
            state.timer_val_dirty_mask = 0
            last_vals = state.timer_last_val
            for ch in range(8):
                if vm & (1 << ch):
                    board._store_raw(0xBD04081E + 4 * ch, 2, last_vals[ch])
        if state.timer_zeroed_mask:
            zm = state.timer_zeroed_mask
            state.timer_zeroed_mask = 0
            for ch in range(8):
                if zm & (1 << ch):
                    board._store_raw(0xBD04081E + 4 * ch, 2, 0)
        if state.timer_ch_dirty_mask:
            import heapq
            dm = state.timer_ch_dirty_mask
            state.timer_ch_dirty_mask = 0
            base_ticks = board.ticks
            synced_comp = self._synced_completed
            step = board.timer_step
            events = board._events
            cancelled = board._cancelled_seqs
            ch_dls = state.timer_ch_deadline
            for ch in range(8):
                if dm & (1 << ch):
                    old_seq = board._timer_event_seq[ch]
                    if old_seq:
                        cancelled.add(old_seq)
                        board._timer_event_seq[ch] = 0
                    board._timer_generation[ch] += 1
                    dl_comp = ch_dls[ch]
                    if dl_comp == 0xFFFFFFFFFFFFFFFF:
                        board.timer_deadline[ch] = None
                        board._timer_active_mask &= ~(1 << ch)
                    else:
                        dl_ticks = base_ticks + (dl_comp - synced_comp) * step
                        board.timer_deadline[ch] = dl_ticks
                        board._timer_active_mask |= 1 << ch
                        board._event_sequence += 1
                        seq = board._event_sequence
                        board._timer_event_seq[ch] = seq
                        heapq.heappush(events, (dl_ticks, seq, board._timer_callbacks[ch]))
            board._prune_cancelled_events()

    def _flush_wdog_from_c(self) -> None:
        board, state = self.board, self.state
        dirty = state.wdog_pet_dirty
        if not dirty:
            return
        state.wdog_pet_dirty = 0
        board._store_raw(0xBD040400, 2, state._sdio_pad)
        if dirty == 3:
            return
        cancelled = board._cancelled_seqs
        if dirty == 1:
            for stage in range(2):
                old_seq = board._wdog_event_seq[stage]
                if old_seq:
                    cancelled.add(old_seq)
                    board._wdog_event_seq[stage] = 0
                board._wdog_generation[stage] += 1
            board._prune_cancelled_events()
            return
        pet_ticks = board.ticks + (state.wdog_pet_comp - self._synced_completed) * board.timer_step
        total_180m = board._stored(0xBD040404, 4)
        reload_180m = board._stored(0xBD040408, 4)
        step_us = board.timer_step * board.us_ticks
        events = board._events
        import heapq
        for stage in range(2):
            old_seq = board._wdog_event_seq[stage]
            if old_seq:
                cancelled.add(old_seq)
            ticks_180m = max(180, total_180m if stage == 0 else (total_180m - reload_180m))
            delay_ticks = max(1, ticks_180m // 180) * step_us
            board._wdog_generation[stage] += 1
            board._event_sequence += 1
            seq = board._event_sequence
            board._wdog_event_seq[stage] = seq
            heapq.heappush(events, (pet_ticks + delay_ticks, seq, board._wdog_callbacks[stage]))
        board._prune_cancelled_events()

    def _flush_uart_from_c(self, *, transfer_in_flight: bool = True, update_status: bool = True) -> None:
        emulator, board, state = self.emulator, self.board, self.state
        if state.uart_rx_head:
            n = state.uart_rx_head
            state.uart_rx_head = 0
            rx = board.uart_rx
            for _ in range(min(n, len(rx))):
                rx.popleft()
            rx_len = len(rx)
            if rx_len == 0:
                state.uart_rx_count = 0
                board._update_uart_irqs()
            elif rx_len <= 256:
                for idx, b_val in enumerate(rx):
                    state.uart_rx_buf[idx] = b_val
                state.uart_rx_count = rx_len
            else:
                state.uart_rx_count = 0xFFFFFFFF
        wcnt = state.uart_tx_write_count
        ocnt = state.uart_tx_out_count
        on_event = emulator.on_event
        if on_event is not None and (wcnt or ocnt):
            state.uart_tx_write_count = 0
            state.uart_tx_out_count = 0
            wvals = state.uart_tx_write_vals
            wpcs = state.uart_tx_write_pcs
            winsns = state.uart_tx_write_insns
            obytes = state.uart_tx_out_bytes
            oinsns = state.uart_tx_out_insns
            uw = emulator.uart_writes
            if wcnt:
                board._store_raw(0xBD0B3C06, 2, wvals[wcnt - 1])
            if ocnt and update_status and state.uart_tx_active != 1:
                board.uart_tx_pending = None
                board._update_uart_irqs()
            on_uart = emulator.on_uart
            stop = getattr(emulator, '_stop', 'budget')
            base_ticks = board.ticks
            synced_comp = self._synced_completed
            step = board.timer_step
            wi = 0
            oi = 0
            while wi < wcnt or oi < ocnt:
                if wi < wcnt and (oi >= ocnt or winsns[wi] <= oinsns[oi]):
                    wpc = wpcs[wi]
                    wval = wvals[wi]
                    win = winsns[wi]
                    wi += 1
                    uw.append({'pc': wpc, 'address': 0xBD0B3C06, 'size': 2, 'value': wval})
                    on_event({'kind': 'mmio-write', 'pc': wpc, 'instruction': win,
                              'address': 0xBD0B3C06, 'size': 2, 'value': wval})
                else:
                    b = obytes[oi]
                    oin = oinsns[oi]
                    oi += 1
                    emulator.uart.append(b)
                    on_event({'kind': 'uart-transmitted', 'pc': 0, 'instruction': oin,
                              'value': b, 'ticks': base_ticks + (oin - synced_comp) * step})
                    if on_uart is not None:
                        on_uart(b)
                    if emulator.reason:
                        continue
                    if stop == 'first-byte':
                        emulator.reason = 'first-byte'
                        emulator.uc.emu_stop()
                    elif stop == 'banner' and emulator.uart.endswith(b'\n\r') and b'[Boot] Firmware build timestamp: ' in emulator.uart:
                        emulator.reason = 'banner'
                        emulator.uc.emu_stop()
            if emulator.reason:
                state.stopped = 1
        else:
            if wcnt:
                state.uart_tx_write_count = 0
                wvals = state.uart_tx_write_vals
                wpcs = state.uart_tx_write_pcs
                uw = emulator.uart_writes
                for i in range(wcnt):
                    uw.append({'pc': wpcs[i], 'address': 0xBD0B3C06, 'size': 2, 'value': wvals[i]})
                board._store_raw(0xBD0B3C06, 2, wvals[wcnt - 1])
            if ocnt:
                state.uart_tx_out_count = 0
                obytes = bytes(state.uart_tx_out_bytes[:ocnt])
                if update_status and state.uart_tx_active != 1:
                    board.uart_tx_pending = None
                    board._update_uart_irqs()
                on_uart = emulator.on_uart
                stop = getattr(emulator, '_stop', 'budget')
                if on_uart is None and stop == 'budget' and not emulator.reason:
                    emulator.uart.extend(obytes)
                else:
                    for b in obytes:
                        emulator.uart.append(b)
                        if on_uart is not None:
                            on_uart(b)
                        if emulator.reason:
                            continue
                        if stop == 'first-byte':
                            emulator.reason = 'first-byte'
                            emulator.uc.emu_stop()
                        elif stop == 'banner' and emulator.uart.endswith(b'\n\r') and b'[Boot] Firmware build timestamp: ' in emulator.uart:
                            emulator.reason = 'banner'
                            emulator.uc.emu_stop()
                if emulator.reason:
                    state.stopped = 1
        if transfer_in_flight and state.uart_tx_active == 1:
            import heapq
            dl_comp = state.uart_tx_deadline
            state.uart_tx_active = 2
            state.uart_tx_deadline = 0xFFFFFFFFFFFFFFFF
            board.uart_tx_pending = state.uart_tx_pending_byte
            board._update_uart_irqs()
            dl_ticks = board.ticks + (dl_comp - self._synced_completed) * board.timer_step
            board._event_sequence += 1
            heapq.heappush(board._events, (dl_ticks, board._event_sequence, board._uart_tx_callback))
            board._next_deadline = board._events[0][0]
        if update_status and (wcnt or ocnt):
            emulator._precache_uart_status(invalidate_on_miss=True)

    def sync_before_host_event(self) -> None:
        board, state = self.board, self.state
        if state._panel_pad & 0x20000:
            board.micci[0xBD300800].data = state._panel_pad & 0xFFFF
            state._panel_pad &= ~0x20000
        if state.panel_write_count:
            self.flush_panel()
        if state.sdio_dirty:
            self.flush_sdio()
        if state.uart0_mask_dirty:
            state.uart0_mask_dirty = 0
            board._store_raw(0xBD0B3C04, 2, state.uart0_mask)
            self.invalidate_mmio_slot(0xBD0B3C04, 2)
        if state.cc8_dirty:
            val = state.cc8_value
            state.cc8_dirty = 0
            board._store_raw(0xBD040CC8, 2, val)
            if val != board._cc8_value:
                board._cc8_value = val
                board._irq_threshold = val & 7
        if state.wdog_pet_dirty:
            self._flush_wdog_from_c()
        if state.timer_ch_dirty_mask | state.timer_val_dirty_mask | state.timer_zeroed_mask:
            self._flush_timer_channels_from_c()
        if (state.uart_tx_write_count | state.uart_tx_out_count | state.uart_rx_head) or state.uart_tx_active == 1:
            self._flush_uart_from_c(transfer_in_flight=True, update_status=True)
        if state.ack_802_dirty | state.timer_expired_mask:
            self.flush_ack_802()
        completed = state.completed
        if completed != self._synced_completed:
            target = board.ticks + (completed - self._synced_completed) * board.timer_step
            board.ticks = target
            self._synced_completed = completed
            state.base_ticks = target
            state.synced_completed = completed
        board._recompute_interrupts()

    def try_fast_advance(self) -> bool:
        board, state = self.board, self.state
        completed = state.completed
        if completed >= state.board_deadline:
            return False
        target = board.ticks + (completed - self._synced_completed) * board.timer_step
        if not state.timers_in_c:
            if target >= board._next_deadline:
                return False
            if state.wdog_pet_dirty:
                self._flush_wdog_from_c()
            if state.timer_ch_dirty_mask | state.timer_val_dirty_mask | state.timer_zeroed_mask:
                self._flush_timer_channels_from_c()
        need_dl = False
        if (state.uart_tx_write_count | state.uart_tx_out_count | state.uart_rx_head) or state.uart_tx_active == 1:
            need_dl = state.uart_tx_active == 1
            self._flush_uart_from_c(transfer_in_flight=True, update_status=True)
        if state.ack_802_dirty | state.timer_expired_mask:
            self.flush_ack_802()
            board._recompute_interrupts()
            self.sync_irqs()
        if not state.timers_in_c and target >= board._next_deadline:
            return False
        board.ticks = target
        self._synced_completed = completed
        state.base_ticks = target
        state.synced_completed = completed
        if need_dl:
            self.update_deadline()
        return True

    def flush_ack_802(self) -> None:
        board, state = self.board, self.state
        if state.ack_802_dirty or state.timer_expired_mask:
            if state.ack_802_dirty:
                state.ack_802_dirty = 0
                board._store_raw(0xBD040802, 2, state.ack_802_value)
            state.timer_expired_mask = 0
            board.timer_pending = state.timer_pending
            masks = board._sync_timer_masks()
            pending = board.timer_pending
            bits = (
                ((1 << 18) if (pending & masks[0]) else 0)
                | ((1 << 19) if (pending & masks[1]) else 0)
                | ((1 << 20) if (pending & masks[2]) else 0)
                | ((1 << 21) if (pending & masks[3]) else 0)
                | ((1 << 22) if (pending & masks[4]) else 0)
            )
            board.irq_raw_sources = (board.irq_raw_sources & ~(0x1F << 18)) | bits

    def flush_uart0_mask(self) -> None:
        board, state = self.board, self.state
        if state.uart0_mask_dirty:
            state.uart0_mask_dirty = 0
            board._store_raw(0xBD0B3C04, 2, state.uart0_mask)
            self.invalidate_mmio_slot(0xBD0B3C04, 2)
            board._update_uart_irqs()

    def _sync_uart0_fast(self) -> None:
        emulator, board, state = self.emulator, self.board, self.state
        gen = state.generation
        if (not state.on_event and not emulator._mpu_user
                and not state.user_access and state.bd0b3_base_ptr):
            if getattr(self, '_uart0_allowed_gen', 0) != gen:
                cp15 = emulator.cp15
                if (not cp15.is_access_allowed(0xBD0B3C04, 2, write=False)
                        or not cp15.is_access_allowed(0xBD0B3C04, 2, write=True)
                        or not cp15.is_access_allowed(0xBD0B3C06, 2, write=True)
                        or not cp15.is_access_allowed(0xBD0B3C0C, 2, write=False)
                        or not cp15.is_access_allowed(0xBD0B3C0E, 2, write=False)):
                    state.uart0_fast_gen = 0
                    return
                self._uart0_allowed_gen = gen
            fifo_enabled = board._stored(0xBD0B4C00, 2) != 0
            tx0_ready = board.uart_tx_pending is None and not board._uart_tx_fifo
            rx0_ready = bool(board.uart_rx)
            state.uart_fifo_enabled = 1 if fifo_enabled else 0
            state.uart0_state = ((0x40 if tx0_ready else 0) | (0x01 if rx0_ready else 0)) if fifo_enabled else 0
            state.uart0_mask = board._stored(0xBD0B3C04, 2)
            if state.uart_tx_active != 1:
                state.uart_tx_active = 0 if tx0_ready else 2
                state.uart_tx_deadline = 0xFFFFFFFFFFFFFFFF
            if (not board._uart_tx_fifo and board.timer_step == 1 and board.us_ticks > 0
                    and board.uart_frame_ticks >= 1
                    and board._uart0_cfg_ok
                    and state.timer_mask_union == (state.timer_mask0 | state.timer_mask1)
                    and not ((board.irq_raw_sources & board._irq_unmasked) & ~((0x1F << 18) | (1 << 38)))):
                state.uart_frame_steps = board.uart_frame_ticks
                stop_mode = getattr(emulator, '_stop', 'budget')
                on_uart = emulator.on_uart
                if stop_mode == 'first-byte':
                    state.uart_tx_bridge_mode = 2
                elif on_uart is not None:
                    trig = getattr(on_uart, 'trigger_byte', None)
                    if trig is not None:
                        state.uart_tx_trigger_byte = trig & 0x1FFFF
                        state.uart_tx_bridge_mode = 3
                        if state.uart_tx_out_count > 0:
                            state._timer_pad = 0x100 | state.uart_tx_out_bytes[state.uart_tx_out_count - 1]
                        elif emulator.uart:
                            state._timer_pad = 0x100 | emulator.uart[-1]
                        else:
                            state._timer_pad = 0
                    else:
                        state.uart_tx_bridge_mode = 2
                elif stop_mode == 'banner':
                    state.uart_tx_bridge_mode = 1
                else:
                    state.uart_tx_bridge_mode = 0
                if state.uart_rx_head == 0:
                    if not rx0_ready:
                        state.uart_rx_count = 0
                    else:
                        rx_len = len(board.uart_rx)
                        if rx_len <= 256:
                            for idx, b_val in enumerate(board.uart_rx):
                                state.uart_rx_buf[idx] = b_val
                            state.uart_rx_count = rx_len
                        else:
                            state.uart_rx_count = 0xFFFFFFFF
                state.uart0_irq_claim = board._uart0_irq_claim
                state.uart0_fast_gen = gen
                return
        state.uart0_fast_gen = 0

    def flush_panel(self) -> None:
        state = self.state
        cnt = state.panel_write_count
        if not cnt:
            return
        state.panel_write_count = 0
        panel = self.board.panel
        panel.output_writes.extend(state.panel_writes_buf[:cnt])
        panel.output_word = state.panel_output_word
        ep = panel.endpoint
        if ep is not None:
            ep.output_word = state.panel_output_word
            ep.shifted_output = state.panel_shifted_output
            ep.bit_count = state.panel_bit_count
            sbit = state.panel_sampled_bit
            ep.sampled_bit = None if sbit < 0 else sbit
            self.invalidate_mmio_slot(0xBD0B1410, 2)

    def sync_panel(self) -> None:
        emulator, board, state = self.emulator, self.board, self.state
        if state.panel_write_count:
            self.flush_panel()
        gen = state.generation
        if (not state.on_event and not emulator._mpu_user
                and not state.user_access and state.bd0b1_base_ptr):
            if getattr(self, '_panel_allowed_gen', 0) != gen:
                cp15 = emulator.cp15
                if (not cp15.is_access_allowed(0xBD0B1418, 2, write=True)
                        or not cp15.is_access_allowed(0xBD0B1418, 2, write=False)
                        or not cp15.is_access_allowed(0xBD0B1410, 2, write=False)):
                    state.panel_fast_gen = 0
                    return
                self._panel_allowed_gen = gen
            ep = board.panel.endpoint
            if ep is not None:
                state.panel_output_word = ep.output_word
                state.panel_armed_strobe = 1 if (ep.armed and ep.strobe == 1) else 0
                state.panel_bit_count = ep.bit_count
                state.panel_frame_bits = ep.frame_bits
                state.panel_input_width = ep.input_width
                state.panel_latched_inputs = ep.latched_inputs & 0xFFFFFFFF
                state.panel_signature = ep.signature & 0xFFFFFFFF
                state.panel_reset_level = ep.reset_active_level
                state.panel_tail_bit = -1 if ep.tail_bit is None else ep.tail_bit
                state.panel_sampled_bit = -1 if ep.sampled_bit is None else ep.sampled_bit
                state.panel_shifted_output = ep.shifted_output & 0xFFFFFFFFFFFFFFFF
            else:
                state.panel_output_word = board.panel.output_word
                state.panel_armed_strobe = 0
                state.panel_bit_count = 0
                state.panel_reset_level = 2
                state.panel_sampled_bit = -1
            state.panel_fast_gen = gen
            return
        state.panel_fast_gen = 0

    def flush_sdio(self) -> None:
        state = self.state
        dirty = state.sdio_dirty
        if not dirty:
            return
        state.sdio_dirty = 0
        sdio = self.board.sdio
        if dirty & 1:
            sdio.int_status_en = state.sdio_status_en
            self.invalidate_mmio_slot(0xBD0B0034, 4)
        if dirty & 2:
            sdio.int_signal_en = state.sdio_signal_en
            self.invalidate_mmio_slot(0xBD0B0038, 4)
        if dirty & 4:
            sdio.sdma_addr = state.sdio_sdma_addr
        if dirty & 8:
            blk = state.sdio_blk_reg
            sdio.block_size = blk & 0x0FFF
            sdio.block_count = (blk >> 16) & 0xFFFF
        if dirty & 16:
            sdio.argument = state.sdio_arg
        if dirty & (32 | 1):
            sdio.int_status = state.sdio_int_status

    def sync_sdio(self) -> None:
        emulator, board, state = self.emulator, self.board, self.state
        if state.sdio_dirty:
            self.flush_sdio()
        sdio = board.sdio
        gen = state.generation
        if (not state.on_event and not emulator._mpu_user
                and not state.user_access and state.bd0b0_base_ptr):
            if getattr(self, '_sdio_allowed_gen', 0) != gen:
                cp15 = emulator.cp15
                if (not cp15.is_access_allowed(0xBD0B0030, 4, write=False)
                        or not cp15.is_access_allowed(0xBD0B0030, 4, write=True)):
                    state.sdio_fast_gen = 0
                    return
                self._sdio_allowed_gen = gen
            state.sdio_int_status = sdio.int_status
            state.sdio_status_en = sdio.int_status_en
            state.sdio_signal_en = sdio.int_signal_en
            state.sdio_resp0 = sdio.response[0]
            state.sdio_sdma_addr = sdio.sdma_addr
            state.sdio_blk_reg = (sdio.block_count << 16) | 0x7000 | (sdio.block_size & 0x0FFF)
            state.sdio_arg = sdio.argument
            state.sdio_card_irq = 1 if sdio.card.card_irq_pending else 0
            state.sdio_fast_gen = gen
            return
        state.sdio_fast_gen = 0

    def update_deadline(self) -> None:
        board, state = self.board, self.state
        step = board.timer_step
        base_ticks = board.ticks
        synced_comp = self._synced_completed
        if (step == 1 and board.us_ticks > 0
                and (state.timer_mask_union & ~(state.timer_mask0 | state.timer_mask1)) == 0):
            state.timers_in_c = 1
            bd = 0xFFFFFFFFFFFFFFFF
            cancelled = board._cancelled_seqs
            for ev_dl, ev_seq, ev_cb in board._events:
                if ev_seq not in cancelled and not getattr(ev_cb, '_is_timer_ch', False):
                    if ev_dl < bd:
                        bd = ev_dl
            if bd != 0xFFFFFFFFFFFFFFFF:
                delay = bd - base_ticks
                if delay < 1:
                    delay = 1
                bd = synced_comp + delay
            state.board_deadline = bd
            min_t = 0xFFFFFFFFFFFFFFFF
            ch_dls = state.timer_ch_deadline
            ch_pers = state.timer_ch_period
            us = board.us_ticks
            board_dls = board.timer_deadline
            board_rels = board.timer_reload
            dm = state.timer_ch_dirty_mask
            for ch in range(8):
                if dm & (1 << ch):
                    cdl = ch_dls[ch]
                    if cdl < min_t:
                        min_t = cdl
                else:
                    dl = board_dls[ch]
                    if dl is not None:
                        delay = dl - base_ticks
                        if delay < 1:
                            delay = 1
                        cdl = synced_comp + delay
                        ch_dls[ch] = cdl
                        if cdl < min_t:
                            min_t = cdl
                    else:
                        ch_dls[ch] = 0xFFFFFFFFFFFFFFFF
                rel = board_rels[ch]
                ch_pers[ch] = (rel + 1) * us if rel > 0 else 0
            state.next_timer_deadline = min_t
            min_dl = bd if bd < min_t else min_t
            if state.uart_tx_active == 1 and state.uart_tx_deadline < min_dl:
                min_dl = state.uart_tx_deadline
            state.deadline = min_dl
        else:
            if state.wdog_pet_dirty:
                self._flush_wdog_from_c()
            if state.timer_ch_dirty_mask | state.timer_val_dirty_mask | state.timer_zeroed_mask:
                self._flush_timer_channels_from_c()
            state.timers_in_c = 0
            state.next_timer_deadline = 0xFFFFFFFFFFFFFFFF
            next_deadline = board._next_deadline
            if next_deadline != 0xFFFFFFFFFFFFFFFF:
                delay = next_deadline - base_ticks
                if delay < 1:
                    delay = 1
                steps = delay if step == 1 else (delay + step - 1) // step
                bd = synced_comp + steps
            else:
                bd = 0xFFFFFFFFFFFFFFFF
            state.board_deadline = bd
            state.deadline = bd

    def sync_irqs(self) -> None:
        board, state = self.board, self.state
        if state.uart0_mask_dirty:
            self.flush_uart0_mask()
        if board._irq_cfg_dirty:
            board._recompute_interrupts()
        max_irq = board._max_irq_claim
        max_fiq = board._max_fiq_claim
        state.max_irq_claim = max_irq
        state.max_fiq_claim = max_fiq
        state.nt_irq_claim = board._nt_irq_claim
        state.nt_fiq_claim = board._nt_fiq_claim
        state.irq18_claim = board._irq18_claim
        state.fiq18_claim = board._fiq18_claim
        state.irq19_claim = board._irq19_claim
        state.fiq19_claim = board._fiq19_claim
        if state.cc8_dirty:
            threshold = state.cc8_value & 7
            irq_claim = max_irq if (max_irq >> 8) > threshold else 0
            fiq_claim = max_fiq if (max_fiq >> 8) > threshold else 0
            state.irq_claim = irq_claim
            state.fiq_claim = fiq_claim
            state.irq_mask = (1 if irq_claim else 0) | (2 if fiq_claim else 0)
        else:
            state.cc8_value = board._cc8_value
            state.irq_mask = board.interrupt_mask
            state.irq_claim = board._irq_claim
            state.fiq_claim = board._fiq_claim
        self._sync_uart0_fast()

    def sync_timer_irqs(self) -> None:
        board, state = self.board, self.state
        masks = board._sync_timer_masks()
        state.timer_pending = board.timer_pending
        state.timer_mask0 = masks[0]
        state.timer_mask1 = masks[1]
        state.timer_mask_union = board._timer_mask_union
        state.timer_active_mask = board._timer_active_mask
        state.timer_reload_zero_mask = board._timer_reload_zero_mask
        self.sync_irqs()

    def refresh(self, *, full: bool = True) -> None:
        emulator, board, state = self.emulator, self.board, self.state
        if full and not self._synchronizing:
            self._uart0_allowed_gen = 0
            self._panel_allowed_gen = 0
            self._sdio_allowed_gen = 0
            state.panel_fast_gen = 0
            state.sdio_fast_gen = 0
            state.cpsr_valid = 0
        if emulator.memory._mappings != self._last_mappings:
            self.sync_memory_map()
        if board._irq_cfg_dirty:
            board._recompute_interrupts()
        masks = board._sync_timer_masks()
        state.stopped = 1 if emulator.reason else 0
        user_access = bool(state.user_access)
        emulator._memory_user_access = user_access
        on_event = emulator.on_event
        no_event = on_event is None or getattr(on_event, 'fast_mmio', False)
        state.on_event = 0 if no_event else 1
        state.irq_mask = board.interrupt_mask
        state.base_ticks = board.ticks
        state.synced_completed = self._synced_completed
        state.timer_step = board.timer_step
        state.us_ticks = board.us_ticks
        state.timer_offset = board._timer_offset & 0xFFFFFFFF
        state.cc8_value = board._cc8_value
        state.timer_pending = board.timer_pending
        state.irq_claim = board._irq_claim
        state.fiq_claim = board._fiq_claim
        state.max_irq_claim = board._max_irq_claim
        state.max_fiq_claim = board._max_fiq_claim
        state.nt_irq_claim = board._nt_irq_claim
        state.nt_fiq_claim = board._nt_fiq_claim
        state.irq18_claim = board._irq18_claim
        state.fiq18_claim = board._fiq18_claim
        state.irq19_claim = board._irq19_claim
        state.fiq19_claim = board._fiq19_claim
        state.timer_mask0 = masks[0]
        state.timer_mask1 = masks[1]
        state.timer_mask_union = board._timer_mask_union
        state.timer_active_mask = board._timer_active_mask
        state.timer_reload_zero_mask = board._timer_reload_zero_mask
        if state._panel_pad & 0x20000:
            board.micci[0xBD300800].data = state._panel_pad & 0xFFFF
            state._panel_pad = 0
        if not state.wdog_pet_dirty:
            state._sdio_pad = board._stored(0xBD040400, 2)
        state.wdog_enabled = (
            1 if (not (board.irq_raw_sources & 0x60) and (board._stored(0xBD060002, 2) & 1)) else 0
        )
        gen = state.generation
        state.mmio_gen = (gen << 32) | board._state_gen
        if no_event and not emulator._mpu_user and not user_access:
            cp15 = emulator.cp15
            if state.bd040_base_ptr and (state.bd040_read_gen != gen or state.cc8_write_gen != gen):
                if cp15.is_access_allowed(0xBD040814, 4, write=False) and cp15.is_access_allowed(0xBD040800, 2, write=False):
                    state.bd040_read_gen = gen
                if cp15.is_access_allowed(0xBD040CC8, 2, write=True) and cp15.is_access_allowed(0xBD040802, 2, write=True):
                    state.cc8_write_gen = gen
            mctrl0 = board.micci.get(0xBD300800)
            if mctrl0 is not None and not mctrl0.busy and (0xBD3008B0, 4, False, True, False) in cp15._allowed_access:
                state._panel_pad = 0x10000 | (mctrl0.data & 0xFFFF)
            else:
                state._panel_pad = 0
            self._sync_uart0_fast()
            if state.panel_fast_gen != gen:
                self.sync_panel()
            if state.sdio_fast_gen != gen:
                self.sync_sdio()
        else:
            state.bd040_read_gen = 0
            state.cc8_write_gen = 0
            state.uart0_fast_gen = 0
            state.panel_fast_gen = 0
            state.sdio_fast_gen = 0
            state._panel_pad = 0
            self._uart0_allowed_gen = 0
            self._panel_allowed_gen = 0
            self._sdio_allowed_gen = 0
        self.update_deadline()

    def invalidate(self) -> None:
        global _NEXT_CONFIG_GEN
        prev_gen = self._config_gen
        self.sync_memory_map()
        if self._config_gen == prev_gen:
            _NEXT_CONFIG_GEN += 1
            self._config_gen = _NEXT_CONFIG_GEN
            self.sync_mpu_mode()

    def sync_mpu_mode(self) -> None:
        self.state.cpsr_valid = 0
        self.state.generation = (self._config_gen << 1) | (1 if self.emulator.cp15.mpu_enabled else 0)

    def cache_exec_page(self, page: int, user: bool) -> None:
        state = self.state
        state.exec_page_tag[(page ^ (page >> 8)) & 4095] = (
            (state.generation << 32) | ((1 if user else 0) << 24) | (page & 0xFFFFF)
        )

    @staticmethod
    def _mmio_slot_index(address: int, size: int) -> int:
        return ((address >> 1) ^ (address >> 7) ^ size) & 63

    def invalidate_mmio_slot(self, address: int, size: int) -> None:
        self.state.mmio_cache[self._mmio_slot_index(address, size)].mmio_gen = 0

    def cache_mmio_read(self, address: int, size: int, value: int, off: int) -> None:
        emulator, state = self.emulator, self.state
        if state.on_event or emulator._mpu_user or state.user_access or emulator._memory_user_access:
            return
        if emulator.on_event is not None and address == 0xBD0B1410:
            return
        if (address & ~0xFFF) == 0xBD040000 and state.bd040_base_ptr:
            state.bd040_read_gen = state.generation
        elif address == 0xBD0B3C04:
            self._sync_uart0_fast()
        elif address == 0xBD0B1410:
            self.sync_panel()
        elif (address & ~0xFFF) == 0xBD0B0000:
            self.sync_sdio()
        if address in _PURE_MMIO_READ_ADDRESSES:
            slot = state.mmio_cache[self._mmio_slot_index(address, size)]
            slot.address = address
            slot.size = size
            slot.value = value & 0xFFFFFFFF
            slot.host_ptr = emulator._mmio_base_ptr + off
            slot.mmio_gen = (state.generation << 32) | self.board._state_gen

    def cache_mmio_write(self, address: int, size: int) -> None:
        emulator, state = self.emulator, self.state
        if state.on_event or emulator._mpu_user or state.user_access or emulator._memory_user_access:
            return
        if (address & ~0xFFF) == 0xBD040000 and state.bd040_base_ptr:
            state.cc8_write_gen = state.generation

    def set_specials(self, cp15, transfers) -> None:
        if not cp15 and not transfers:
            self.state.all_special = 0
            self.state.special_count = 0
            return
        records = [(address, word, 1) for address, word in cp15.items()]
        records.extend((address, word, 2) for address, word in transfers.items())
        self.state.all_special = len(records) > 32
        self.state.special_count = min(len(records), 32)
        for index, (address, word, kind) in enumerate(records[:32]):
            self.state.specials[index] = address
            self.state.words[index] = word
            self.state.types[index] = kind

    def copy_recent(self) -> None:
        total = self.state.recent_total
        if total == self._synced_recent_total:
            return
        self._synced_recent_total = total
        recent_pcs = self.emulator._recent_pcs
        recent_pcs.clear()
        recent = self.state.recent
        if total < 32:
            recent_pcs.extend(recent[:total])
        else:
            pos = total & 31
            if pos == 0:
                recent_pcs.extend(recent[:])
            else:
                recent_pcs.extend(recent[pos:])
                recent_pcs.extend(recent[:pos])

    def synchronize(self, *, recent: bool = False, refresh: bool = True) -> None:
        if self._synchronizing:
            return
        self._synchronizing = True
        try:
            emulator, board, state = self.emulator, self.board, self.state
            if state._panel_pad & 0x20000:
                board.micci[0xBD300800].data = state._panel_pad & 0xFFFF
                state._panel_pad &= ~0x20000
            if state.panel_write_count:
                self.flush_panel()
            if state.sdio_dirty:
                self.flush_sdio()
            uart0_dirty = state.uart0_mask_dirty
            if uart0_dirty:
                state.uart0_mask_dirty = 0
                board._store_raw(0xBD0B3C04, 2, state.uart0_mask)
                self.invalidate_mmio_slot(0xBD0B3C04, 2)
            expired_dirty = state.timer_expired_mask
            if expired_dirty:
                state.timer_expired_mask = 0
                board.timer_pending = state.timer_pending
            ack_dirty = state.ack_802_dirty
            if ack_dirty:
                state.ack_802_dirty = 0
                board._store_raw(0xBD040802, 2, state.ack_802_value)
                board.timer_pending = state.timer_pending
            cc8_dirty = state.cc8_dirty
            cc8_changed = False
            if cc8_dirty:
                state.cc8_dirty = 0
                val = state.cc8_value
                board._store_raw(0xBD040CC8, 2, val)
                if val != board._cc8_value:
                    board._cc8_value = val
                    board._irq_threshold = val & 7
                    cc8_changed = True
            if state.wdog_pet_dirty:
                self._flush_wdog_from_c()
            if state.timer_ch_dirty_mask | state.timer_val_dirty_mask | state.timer_zeroed_mask:
                self._flush_timer_channels_from_c()
            if (state.uart_tx_write_count | state.uart_tx_out_count | state.uart_rx_head) or state.uart_tx_active == 1:
                self._flush_uart_from_c(transfer_in_flight=True, update_status=True)
            emulator.instructions = state.instructions
            emulator._instruction_pending = bool(state.pending)
            emulator._memory_user_access = bool(state.user_access)
            maint = state.maintenance_count
            if maint != self._synced_maintenance:
                emulator.cp15.maintenance_count += maint - self._synced_maintenance
                self._synced_maintenance = maint
            if recent:
                self.copy_recent()
            try:
                completed = state.completed
                elapsed = completed - self._synced_completed
                # If a device callback faults mid-advance, do not replay that
                # instruction's completion during final cleanup.
                self._synced_completed = completed
                if elapsed:
                    emulator._advance_board(elapsed)
                    if not state.on_event and not emulator._mpu_user and not state.user_access:
                        allowed = emulator.cp15._allowed_access
                        mctrl = board.micci.get(0xBD300800)
                        if mctrl is not None and not mctrl.busy:
                            if (0xBD3008B0, 4, False, False, False) in allowed:
                                mpage = emulator._mmio_page_offset.get(0xBD300000)
                                if mpage is not None:
                                    moff = mpage + 0x8B0
                                    mval = mctrl.data & 0xFFFFFFFF
                                    emulator._mmio_u32[moff >> 2] = mval
                                    self.cache_mmio_read(0xBD3008B0, 4, mval, moff)
                            if (0xBD3008B0, 4, False, True, False) in allowed:
                                state._panel_pad = 0x10000 | (mctrl.data & 0xFFFF)
                        i2c = board.i2c
                        if i2c is not None and (0xBD065800, 2, False, False, False) in allowed:
                            ipage = emulator._mmio_page_offset.get(0xBD065000)
                            if ipage is not None:
                                ioff = ipage + 0x800
                                ival = i2c.control | (0x80 if i2c.busy else 0)
                                emulator._mmio_u16[ioff >> 1] = ival
                                self.cache_mmio_read(0xBD065800, 2, ival, ioff)
            finally:
                if uart0_dirty:
                    board._update_uart_irqs()
                if ack_dirty or expired_dirty:
                    board._update_timer_irqs()
                if cc8_changed:
                    board._apply_irq_threshold(board._cc8_value)
            if refresh:
                self.refresh()
        finally:
            self._synchronizing = False

    def finish(self) -> None:
        if self.state.pending:
            self.state.pending = 0
            self.state.completed += 1
        self.synchronize(recent=True)

    def _mmio_bridge(self, access: int, address: int, size: int, value: int) -> None:
        self.emulator._mmio(self.emulator.uc, access, address, size, value, None)

    def _bridge(self, address: int, size: int, kind: int) -> None:
        emulator = self.emulator
        try:
            if kind == 6:
                self._flush_uart_from_c(transfer_in_flight=False, update_status=False)
                return
            if kind == 4:
                # Block checks precede the pending instruction's completion.
                # They do not observe device time; faults synchronize in
                # Emulator._fault. Preserve C's per-instruction user flag.
                if self.state.current_kind:
                    emulator._check_block_fetch(address, size, self.state.current_word)
                else:
                    emulator._block_miss(address, size)
                self.state.stopped = 1 if emulator.reason else 0
                return
            if kind == 1:
                self.synchronize()
                return
            self.synchronize(recent=(kind == 2), refresh=False)
            if kind == 5 and not emulator.reason:
                self.state.cpsr_valid = 0
                emulator._check_interrupt(emulator.uc, address)
            elif kind == 2 and not emulator.reason:
                emulator.reason = 'instruction-limit'
            elif kind == 3 and not emulator.reason:
                self.state.cpsr_valid = 0
                captured_kind = self.state.current_kind
                emulator._special_instruction(
                    emulator.uc, address, size,
                    native_word=self.state.current_word if captured_kind else None,
                    native_user_transfer=captured_kind == 2)
                self.state.user_access = 1 if emulator._memory_user_access else 0
            self.refresh(full=emulator.on_event is not None)
        except BaseException as exc:
            # ctypes callbacks must never leak exceptions: Unicorn's Python
            # wrapper reraises this field after the native emulation returns.
            emulator.uc._hook_exception = exc
            self.state.stopped = 1
            emulator.uc.emu_stop()


