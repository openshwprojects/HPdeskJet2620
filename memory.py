"""Physical flash, shared DDR apertures, and ARM946 TCM overlays.

The board photograph identifies one Etron EM6AB160 (512 Mbit/64 MiB) DDR chip.
Firmware MPU setup describes 128 MiB windows at 0x20000000, 0x60000000 and
0xa0000000 with different cache attributes. Sharing DDR through these windows
is an explicit board-map inference; the second 64 MiB repeats address lines.

Unicorn has a unified instruction/data memory map. DTCM data accesses work,
but fetching instructions from an enabled DTCM aperture is rejected instead
of incorrectly fetching DTCM contents (the real CPU would use the outer bus).
Instruction fetches from a load-mode aperture are also rejected: temporary
outer-bus writes could affect Unicorn's translation before the next instruction
hook restores those bytes.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import sys

from unicorn import (
     Uc, UC_ARCH_ARM, UC_MODE_ARM, UC_MODE_BIG_ENDIAN,
    UC_HOOK_MEM_WRITE, UC_HOOK_MEM_FETCH_PROT, UC_HOOK_CODE,
    UC_PROT_ALL, UC_PROT_READ, UC_PROT_WRITE, UC_PROT_EXEC,
)
from unicorn.arm_const import UC_ARM_REG_CPSR, UC_CPU_ARM_946
from unicorn.unicorn_py3 import unicorn as _uc_bindings

from cpu import CONTROL, CP15, CpuModelError, TcmMapping

import gc
import os
import weakref

_uc_reg_read = _uc_bindings.uclib.uc_reg_read
_uc_hook_add = _uc_bindings.uclib.uc_hook_add
_uc_hook_del = _uc_bindings.uclib.uc_hook_del
_C_CODE_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint64, ctypes.c_uint32, ctypes.c_void_p)
_C_MEM_CB = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_int, ctypes.c_uint64, ctypes.c_int, ctypes.c_int64, ctypes.c_void_p
)
_C_EVENTMEM_CB = ctypes.CFUNCTYPE(
    ctypes.c_bool, ctypes.c_void_p, ctypes.c_int, ctypes.c_uint64, ctypes.c_int, ctypes.c_int64, ctypes.c_void_p
)

FLASH_SIZE = 0x01000000
DRAM_SIZE = 0x04000000
DRAM_APERTURES = (0x20000000, 0x60000000, 0xA0000000)

_FLASH_POOL: list[tuple[bytearray, ctypes.Array, memoryview, bytes | None]] = []
_DRAM_POOL: list[tuple] = []
_POOLED_UCS: list[Uc] = []
_POOL_ALLOCATED = 0
_PENDING_GC_CHECK: dict[tuple[bool, bool], int] = {}
_CANONICAL_DUMP: bytes | None = None
_LAST_MATCHED_DUMP: bytes | None = None
_NATIVE_LIB_FUNCS = False


def _get_dram_funcs():
    global _NATIVE_LIB_FUNCS
    if _NATIVE_LIB_FUNCS is False:
        try:
            from native_observer import _get_native_library
            lib = _get_native_library()
            _NATIVE_LIB_FUNCS = (
                getattr(lib, 'native_alloc_dram', None),
                getattr(lib, 'native_prepare_dram', None),
                getattr(lib, 'native_dirty_dram_offsets', None),
            )
        except Exception:
            _NATIVE_LIB_FUNCS = (None, None, None)
    return _NATIVE_LIB_FUNCS


@dataclass(frozen=True)
class _BusRange:
    address: int
    size: int
    pointer: int
    permissions: int
    buf: bytearray | memoryview | None = None


class PhysicalMemory:
    def __init__(self, uc: Uc | None, dump: bytes, cpu: CP15, *,
                 absent_flash_window: bool = False,
                 init_dram: bytes | bytearray | memoryview | None = None,
                 init_dram_patches: list | None = None,
                 init_tcm: dict | None = None,
                 native_observer: bool = False):
        global _POOL_ALLOCATED, _PENDING_GC_CHECK, _CANONICAL_DUMP, _LAST_MATCHED_DUMP
        if len(dump) != FLASH_SIZE:
            raise ValueError('physical flash image must be 16 MiB')
        self.cpu = cpu
        self._reg_buf = ctypes.c_uint32()
        self._reg_buf_ptr = ctypes.byref(self._reg_buf)
        if type(dump) is bytes:
            if _CANONICAL_DUMP is None:
                _CANONICAL_DUMP = dump
                _LAST_MATCHED_DUMP = dump
            elif dump is _CANONICAL_DUMP or dump is _LAST_MATCHED_DUMP:
                dump = _CANONICAL_DUMP
            elif dump[:64] == _CANONICAL_DUMP[:64] and dump == _CANONICAL_DUMP:
                _LAST_MATCHED_DUMP = dump
                dump = _CANONICAL_DUMP
            self._dump = dump
        elif isinstance(dump, bytearray):
            self._dump = dump
        else:
            self._dump = bytes(dump)
        self._dump_mv = memoryview(self._dump)
        self._native_observer_mode = bool(native_observer)
        alloc_dram, prep_dram, _ = _get_dram_funcs()
        init_len = len(init_dram) if init_dram is not None else 0
        init_ref = init_dram if (init_len > 0 and type(init_dram) is bytes) else None
        dump_is_bytes = type(self._dump) is bytes
        can_pool_uc = uc is None and cpu.itcm_size == 8192 and cpu.dtcm_size == 4096

        def _find_pool_match() -> int:
            # Prefer exact match on both init_dram reference and flash dump type/reference
            for i in range(len(_DRAM_POOL) - 1, -1, -1):
                entry = _DRAM_POOL[i]
                if (entry[5] is not None) != can_pool_uc:
                    continue
                if can_pool_uc and entry[5][10] != self._native_observer_mode:
                    continue
                dram_ok = (entry[3] is init_ref) if init_ref is not None else (entry[4] == 0)
                if dram_ok:
                    f_tuple = entry[6]
                    if f_tuple is not None:
                        if dump_is_bytes and f_tuple[3] is self._dump:
                            return i
                        if not dump_is_bytes and (f_tuple[3] is self._dump or f_tuple[3] is None):
                            return i
                    else:
                        return i
            for i in range(len(_DRAM_POOL) - 1, -1, -1):
                entry = _DRAM_POOL[i]
                if (entry[5] is not None) != can_pool_uc:
                    continue
                if can_pool_uc and entry[5][10] != self._native_observer_mode:
                    continue
                if (entry[3] is init_ref) if init_ref is not None else (entry[4] == 0):
                    return i
            return -1

        pool_key = (can_pool_uc, self._native_observer_mode)
        self._pool_key = pool_key
        match_idx = _find_pool_match()
        if match_idx < 0 and _PENDING_GC_CHECK.get(pool_key, 0) > 0:
            gc.collect(0)
            match_idx = _find_pool_match()
            if match_idx < 0 and _PENDING_GC_CHECK.get(pool_key, 0) > 0:
                gc.collect(1)
                match_idx = _find_pool_match()
                if match_idx < 0 and _PENDING_GC_CHECK.get(pool_key, 0) > 0:
                    gc.collect(2)
                    match_idx = _find_pool_match()
            _PENDING_GC_CHECK[pool_key] = 0
        _PENDING_GC_CHECK[pool_key] = _PENDING_GC_CHECK.get(pool_key, 0) + 1

        init_ptr = (
            None if not init_len else
            (init_dram if type(init_dram) is bytes else (ctypes.c_char * init_len).from_buffer(init_dram))
        )
        pooled_entry = None
        if match_idx >= 0:
            pooled_entry = _DRAM_POOL.pop(match_idx)
        elif _DRAM_POOL and _POOL_ALLOCATED >= 4:
            for i in range(len(_DRAM_POOL) - 1, -1, -1):
                entry = _DRAM_POOL[i]
                if (entry[5] is not None) == can_pool_uc and (not can_pool_uc or entry[5][10] == self._native_observer_mode):
                    pooled_entry = _DRAM_POOL.pop(i)
                    break

        self._pooled_uc = False
        self._clean_ctx = None
        self._mmio_bundle = None
        self._tcm_dirty = False
        self._mpu_dirty = False
        self._mpu_state = None
        self._code_cache_valid = False
        self._exec_dram_pages: bytearray | None = None
        self._exec_dram_c = None
        prev_absent_flash = None
        prev_mappings: tuple[TcmMapping, ...] = ()
        flash_changed = True
        itcm_changed = True
        dram_exec_changed = True

        def _sync_flash_buf(prev_dump) -> bool:
            changed = False
            if prev_dump is not self._dump:
                prev_dirty = getattr(prev_dump, '_dirty_end', None)
                cur_dirty = getattr(self._dump, '_dirty_end', None)
                if prev_dirty is not None and cur_dirty is not None:
                    limit = max(prev_dirty, cur_dirty)
                    if self._flash_mv[:limit] != self._dump_mv[:limit]:
                        self._flash_mv[:limit] = self._dump_mv[:limit]
                        changed = True
                else:
                    if self._flash_mv[:0x10000] != self._dump_mv[:0x10000]:
                        self._flash_mv[:0x10000] = self._dump_mv[:0x10000]
                        changed = True
                    if self._flash_buf != self._dump:
                        self._flash_mv[:] = self._dump_mv
                        changed = True
            elif not dump_is_bytes:
                dirty_end = getattr(self._dump, '_dirty_end', None)
                if dirty_end is not None:
                    if self._flash_mv[:dirty_end] != self._dump_mv[:dirty_end]:
                        self._flash_mv[:dirty_end] = self._dump_mv[:dirty_end]
                        changed = True
                else:
                    if self._flash_mv[:0x10000] != self._dump_mv[:0x10000]:
                        self._flash_mv[:0x10000] = self._dump_mv[:0x10000]
                        changed = True
                    if self._flash_buf != self._dump:
                        self._flash_mv[:] = self._dump_mv
                        changed = True
            return changed

        if pooled_entry is not None and can_pool_uc and pooled_entry[5] is not None:
            (self._dram_buf, self._dram, self._dram_mv, prev_init_ref, prev_init_len,
             uc_bundle, flash_tuple, tcm_tuple, self._mmio_bundle) = pooled_entry
            (self.uc, self._clean_ctx, prev_absent_flash, prev_mappings,
             self._tcm_dirty, self._mpu_dirty, self._mpu_state,
             self._exec_dram_pages, self._exec_dram_c, prev_flash_dirty, _) = uc_bundle
            self._uch = self.uc._uch
            self._pooled_uc = True
            self.uc.context_restore(self._clean_ctx)
            self.uc._hook_exception = None
            self._flash_buf, self._flash, self._flash_mv, prev_dump = flash_tuple
            flash_changed = _sync_flash_buf(prev_dump) or prev_flash_dirty
            same_init = 1 if (init_len > 0 and prev_init_ref is init_dram and type(init_dram) is bytes) else 0
            exec_ptr = ctypes.addressof(self._exec_dram_c) if self._exec_dram_c is not None else None
            if prep_dram is not None:
                dram_exec_changed = bool(prep_dram(
                    ctypes.addressof(self._dram), DRAM_SIZE, init_ptr, init_len, same_init, prev_init_len, exec_ptr
                ))
            elif init_len > 0:
                ctypes.memmove(self._dram, init_ptr, init_len)
                if init_len < DRAM_SIZE:
                    ctypes.memset(ctypes.addressof(self._dram) + init_len, 0, DRAM_SIZE - init_len)
                dram_exec_changed = True
            else:
                ctypes.memset(ctypes.addressof(self._dram), 0, DRAM_SIZE)
                dram_exec_changed = True
            if init_dram_patches:
                mv = self._dram_mv
                exec_pages = self._exec_dram_pages
                for off, page in init_dram_patches:
                    mv[off:off + len(page)] = page
                    if exec_pages is not None and exec_pages[off >> 12]:
                        dram_exec_changed = True
            self._itcm_buf, self._dtcm_buf, self._tcm, self._itcm_mv = tcm_tuple
            itcm_changed = False
            if init_tcm is not None:
                itcm_src = init_tcm.get('itcm')
                if itcm_src is not None and self._itcm_mv is not None:
                    if self._itcm_mv != itcm_src:
                        ctypes.memmove(self._tcm['itcm'], itcm_src, len(itcm_src))
                        itcm_changed = True
                    self._tcm_dirty = True
                dtcm_src = init_tcm.get('dtcm')
                if dtcm_src is not None and self._tcm['dtcm'] is not None:
                    ctypes.memmove(self._tcm['dtcm'], dtcm_src, len(dtcm_src))
                    self._tcm_dirty = True
            elif self._tcm_dirty:
                self._tcm_dirty = False
                itcm_changed = True
                if self._itcm_buf is not None:
                    ctypes.memset(self._tcm['itcm'], 0, len(self._itcm_buf))
                if self._dtcm_buf is not None:
                    ctypes.memset(self._tcm['dtcm'], 0, len(self._dtcm_buf))
        else:
            if uc is None:
                uc = Uc(UC_ARCH_ARM, UC_MODE_ARM | UC_MODE_BIG_ENDIAN)
                uc.ctl_set_cpu_model(UC_CPU_ARM_946)
                if can_pool_uc:
                    self._pooled_uc = True
                    fin = getattr(uc, '_Uc__finalizer', None)
                    if fin is not None:
                        fin.atexit = False
                    _POOLED_UCS.append(uc)
            self.uc = uc
            self._uch = uc._uch
            if _FLASH_POOL and not self._pooled_uc:
                self._flash_buf, self._flash, self._flash_mv, prev_dump = _FLASH_POOL.pop()
                _sync_flash_buf(prev_dump)
            else:
                self._flash_buf = bytearray(self._dump)
                self._flash = (ctypes.c_char * FLASH_SIZE).from_buffer(self._flash_buf)
                self._flash_mv = memoryview(self._flash_buf)
            if pooled_entry is not None:
                self._dram_buf, self._dram, self._dram_mv, prev_init_ref, prev_init_len = pooled_entry[:5]
                if prep_dram is not None:
                    same_init = 1 if (init_len > 0 and prev_init_ref is init_dram and type(init_dram) is bytes) else 0
                    prep_dram(ctypes.addressof(self._dram), DRAM_SIZE, init_ptr, init_len, same_init, prev_init_len, None)
                elif init_len > 0:
                    ctypes.memmove(self._dram, init_ptr, init_len)
                    if init_len < DRAM_SIZE:
                        ctypes.memset(ctypes.addressof(self._dram) + init_len, 0, DRAM_SIZE - init_len)
                else:
                    ctypes.memset(ctypes.addressof(self._dram), 0, DRAM_SIZE)
            else:
                _POOL_ALLOCATED += 1
                ptr = alloc_dram(DRAM_SIZE) if alloc_dram is not None else None
                if ptr:
                    self._dram = (ctypes.c_char * DRAM_SIZE).from_address(ptr)
                    self._dram_mv = memoryview(self._dram).cast('B')
                    self._dram_buf = self._dram_mv
                    if init_len > 0 and prep_dram is not None:
                        prep_dram(ptr, DRAM_SIZE, init_ptr, init_len, 0, 0, None)
                    elif init_len > 0:
                        ctypes.memmove(self._dram, init_ptr, init_len)
                else:
                    self._dram_buf = bytearray(DRAM_SIZE)
                    self._dram = (ctypes.c_char * DRAM_SIZE).from_buffer(self._dram_buf)
                    self._dram_mv = memoryview(self._dram_buf)
                    if init_len > 0:
                        ctypes.memmove(self._dram, init_ptr, init_len)
            if init_dram_patches:
                mv = self._dram_mv
                for off, page in init_dram_patches:
                    mv[off:off + len(page)] = page
            self._itcm_buf = bytearray(cpu.itcm_size) if cpu.itcm_size else None
            self._dtcm_buf = bytearray(cpu.dtcm_size) if cpu.dtcm_size else None
            self._tcm = {
                'itcm': (ctypes.c_char * cpu.itcm_size).from_buffer(self._itcm_buf) if self._itcm_buf is not None else None,
                'dtcm': (ctypes.c_char * cpu.dtcm_size).from_buffer(self._dtcm_buf) if self._dtcm_buf is not None else None,
            }
            self._itcm_mv = memoryview(self._itcm_buf) if self._itcm_buf is not None else None
            if init_tcm is not None:
                for k, v in init_tcm.items():
                    if v is not None and self._tcm.get(k) is not None:
                        ctypes.memmove(self._tcm[k], v, len(v))
                        self._tcm_dirty = True

        if self._exec_dram_pages is None:
            self._exec_dram_pages = bytearray(16384)
            self._exec_dram_c = (ctypes.c_uint8 * 16384).from_buffer(self._exec_dram_pages)
        self._flash_clean = True
        self._flash_dirty_start = FLASH_SIZE
        self._flash_dirty_end = 0
        self._flash_mirrored = False
        self._dram_init_ref = init_ref
        self._dram_init_len = init_len
        self._bus = [_BusRange(0, FLASH_SIZE, ctypes.addressof(self._flash),
                               UC_PROT_READ | UC_PROT_EXEC, self._flash_buf)]
        # Optional, explicit SPI board profile: absent device windows start
        # at MiB63 (register 0xfff). The undriven serial return is pulled high.
        # Keep the remaining address gap unmapped. This is bus input, not RAM.
        self._absent_flash = None
        absent_flash_changed = False
        if absent_flash_window:
            if pooled_entry is not None and self._pooled_uc and prev_absent_flash is not None:
                self._absent_flash = prev_absent_flash
            else:
                self._absent_flash = ctypes.create_string_buffer(b'\xff' * 0x100000, 0x100000)
                absent_flash_changed = True
            self._bus.append(_BusRange(0x03f00000, 0x100000,
                                       ctypes.addressof(self._absent_flash), UC_PROT_READ, None))
        elif pooled_entry is not None and self._pooled_uc and prev_absent_flash is not None:
            self.uc.mem_unmap(0x03f00000, 0x100000)
            absent_flash_changed = True
        for base in DRAM_APERTURES:
            for repeat in range(2):
                self._bus.append(_BusRange(base + repeat * DRAM_SIZE, DRAM_SIZE,
                                           ctypes.addressof(self._dram), UC_PROT_ALL, self._dram_buf))
        self._bus.sort(key=lambda r: r.address)
        if pooled_entry is None or not self._pooled_uc:
            for region in self._bus:
                if region.address == 0 and 0 < cpu.itcm_size < region.size:
                    uc.mem_map_ptr(0, cpu.itcm_size, region.permissions, region.pointer)
                    uc.mem_map_ptr(cpu.itcm_size, region.size - cpu.itcm_size,
                                   region.permissions, region.pointer + cpu.itcm_size)
                elif region.address == 0x60000000 and 0 < cpu.dtcm_size < region.size:
                    uc.mem_map_ptr(region.address, cpu.dtcm_size, region.permissions, region.pointer)
                    uc.mem_map_ptr(region.address + cpu.dtcm_size, region.size - cpu.dtcm_size,
                                   region.permissions, region.pointer + cpu.dtcm_size)
                else:
                    uc.mem_map_ptr(region.address, region.size, region.permissions, region.pointer)
            if self._pooled_uc:
                self._clean_ctx = uc.context_save()
                self._clean_ctx._to_free = False
                _POOLED_UCS.append(self._clean_ctx)
        elif absent_flash_window and absent_flash_changed:
            self.uc.mem_map_ptr(0x03f00000, 0x100000, UC_PROT_READ, ctypes.addressof(self._absent_flash))
        self._mappings: tuple[TcmMapping, ...] = prev_mappings
        self._tcm_load_mode = any(m.load_mode for m in prev_mappings)
        itcm = next((m for m in prev_mappings if m.kind == 'itcm'), None)
        self._itcm_size = itcm.size if itcm is not None else 0
        self._itcm_phys_size = itcm.physical_size if itcm is not None else 0
        dtcm = next((m for m in prev_mappings if m.kind == 'dtcm'), None)
        self._dtcm_base = dtcm.address if dtcm is not None else 0
        self._dtcm_end = (dtcm.address + dtcm.size) if dtcm is not None else 0
        self._layout_version = 0
        self._all_hooks: list[int] = []
        self._cb_refs: dict[int, object] = {}
        self._load_hooks = []
        self._pending_restore: list[tuple] = []
        self.error: str | None = None
        if self._pooled_uc:
            uc_obj = self.uc
            uc_obj._active_memory_ref = weakref.ref(self)
            if not hasattr(uc_obj, '_persistent_fetch_hook'):
                def _dispatch_fetch(uc_ptr, access, address, size, value, user_data):
                    mem_inst = uc_obj._active_memory_ref()
                    return mem_inst._fetch_protected(uc_ptr, access, address, size, value, user_data) if mem_inst is not None else False
                uc_obj._persistent_fetch_cb = _C_EVENTMEM_CB(_dispatch_fetch)
                handle = _uc_bindings.uc_hook_h()
                status = _uc_hook_add(
                    self._uch, ctypes.byref(handle), UC_HOOK_MEM_FETCH_PROT,
                    ctypes.cast(uc_obj._persistent_fetch_cb, ctypes.c_void_p),
                    None, ctypes.c_uint64(1), ctypes.c_uint64(0))
                if status:
                    raise _uc_bindings.UcError(status)
                uc_obj._persistent_fetch_hook = handle.value
            self._fetch_hook = uc_obj._persistent_fetch_hook
        else:
            self_ref = weakref.ref(self)
            self._c_fetch_cb = _C_EVENTMEM_CB(
                lambda uc, acc, addr, sz, val, ud: bool((m := self_ref()) is not None and m._fetch_protected(uc, acc, addr, sz, val, ud))
            )
            self._fetch_hook = self._add_c_hook(UC_HOOK_MEM_FETCH_PROT, self._c_fetch_cb)
        tcm_remapped = self.sync_tcm()
        if tcm_remapped or absent_flash_changed or flash_changed or itcm_changed or dram_exec_changed:
            self._mpu_state = None
        if self._mpu_dirty and not self.cpu.mpu_enabled:
            self._mpu_dirty = False
            self._mpu_state = None
            for start, inclusive_end, current in self.uc.mem_regions():
                size = inclusive_end + 1 - start
                target = self.max_permissions(start) or (UC_PROT_READ | UC_PROT_WRITE)
                if current != target:
                    self.uc.mem_protect(start, size, target)
            self._code_cache_valid = False
        elif pooled_entry is not None and self._pooled_uc:
            self._code_cache_valid = not (flash_changed or itcm_changed or dram_exec_changed or tcm_remapped or absent_flash_changed)
        else:
            self._code_cache_valid = False
        if not self._code_cache_valid and pooled_entry is not None:
            ctypes.memset(ctypes.addressof(self._exec_dram_c), 0, 16384)
        if pooled_entry is not None and self._pooled_uc and not self._code_cache_valid:
            self.uc.ctl_flush_tb()

    def _release(self) -> None:
        if sys is None or sys.is_finalizing():
            self._dram_buf = None
            self._flash_buf = None
            return
        dram_buf = getattr(self, '_dram_buf', None)
        if dram_buf is None:
            return
        pk = getattr(self, '_pool_key', None)
        if pk is not None and _PENDING_GC_CHECK.get(pk, 0) > 0:
            _PENDING_GC_CHECK[pk] -= 1
        self._dram_buf = None
        flash_buf = getattr(self, '_flash_buf', None)
        self._flash_buf = None
        if getattr(self, '_pending_restore', None):
            self.before_instruction()
        flash_was_dirty = not getattr(self, '_flash_clean', True)
        if flash_was_dirty and flash_buf is not None:
            s, e = self._flash_dirty_start, self._flash_dirty_end
            if s < e:
                self._flash_mv[s:e] = self._dump_mv[s:e]
            self._flash_clean = True
        if getattr(self, '_pooled_uc', False) and self.uc is not None:
            had_temp_hooks = bool(self._all_hooks or self.uc._callbacks)
            if getattr(self, '_tcm_load_mode', False) and self._mappings:
                self.cpu.values[CONTROL] = 0x00000078
                self.sync_tcm()
                self._mpu_state = None
                had_temp_hooks = True
            for h in self._all_hooks:
                _uc_hook_del(self._uch, h)
            self._all_hooks.clear()
            self._cb_refs.clear()
            if self.uc._callbacks:
                for h in list(self.uc._callbacks.keys()):
                    _uc_hook_del(self._uch, h)
                self.uc._callbacks.clear()
            if len(_DRAM_POOL) < 6:
                dump_ref = (
                    self._dump
                    if getattr(self, '_flash_clean', False)
                    and (type(self._dump) is bytes or hasattr(self._dump, '_dirty_end'))
                    else None
                )
                _DRAM_POOL.append((
                    dram_buf, self._dram, self._dram_mv,
                    getattr(self, '_dram_init_ref', None),
                    getattr(self, '_dram_init_len', 0),
                    (
                        self.uc,
                        self._clean_ctx,
                        self._absent_flash,
                        self._mappings,
                        self._tcm_dirty,
                        self._mpu_dirty,
                        getattr(self, '_mpu_state', None),
                        getattr(self, '_exec_dram_pages', None),
                        getattr(self, '_exec_dram_c', None),
                        flash_was_dirty or had_temp_hooks,
                        getattr(self, '_native_observer_mode', False),
                    ),
                    (flash_buf, self._flash, self._flash_mv, dump_ref),
                    (self._itcm_buf, self._dtcm_buf, self._tcm, self._itcm_mv),
                    self._mmio_bundle,
                ))
            return
        if flash_buf is not None and len(_FLASH_POOL) < 4:
            dump_ref = (
                self._dump
                if getattr(self, '_flash_clean', False)
                and (type(self._dump) is bytes or hasattr(self._dump, '_dirty_end'))
                else None
            )
            _FLASH_POOL.append((flash_buf, self._flash, self._flash_mv, dump_ref))
        if len(_DRAM_POOL) < 4:
            _DRAM_POOL.append((
                dram_buf, self._dram, self._dram_mv,
                getattr(self, '_dram_init_ref', None),
                getattr(self, '_dram_init_len', 0),
                None, None, None, None,
            ))

    def __del__(self) -> None:
        self._release()

    def read_block_view(self, address: int, size: int) -> memoryview | None:
        if self._tcm_load_mode or size <= 0:
            return None
        end = address + size
        if self._dtcm_end and address < self._dtcm_end and end > self._dtcm_base:
            return None
        top = address >> 28
        if (top == 2 or top == 6 or top == 10) and (address & 0x0FFFFFFF) + size <= 0x08000000:
            off = address & 0x03FFFFFF
            if off + size <= DRAM_SIZE:
                return self._dram_mv[off:off + size]
        if self._itcm_size and address < self._itcm_size:
            if end <= self._itcm_size and self._itcm_phys_size and self._itcm_mv is not None:
                off = address & (self._itcm_phys_size - 1)
                if off + size <= self._itcm_phys_size:
                    return self._itcm_mv[off:off + size]
            return None
        if end <= FLASH_SIZE:
            return self._flash_mv[address:end]
        return None

    def _bus_range(self, address: int) -> _BusRange | None:
        for r in self._bus:
            if r.address <= address < r.address + r.size:
                return r
        return None

    def _bus_segments(self, address: int, size: int):
        """Yield mapped outer-bus pieces within an extent, skipping holes."""
        end = address + size
        for region in self._bus:
            start, stop = max(address, region.address), min(end, region.address + region.size)
            if start < stop:
                yield start, stop - start, region.pointer + start - region.address, region.permissions

    def max_permissions(self, address: int) -> int:
        """Physical rights; caller can intersect them with CPU MPU rights."""
        for mapping in self._mappings:
            if mapping.address <= address < mapping.address + mapping.size:
                if mapping.load_mode:
                    bus = self._bus_range(address)
                    return ((bus.permissions | UC_PROT_WRITE) & ~UC_PROT_EXEC) if bus else 0
                return UC_PROT_ALL if mapping.kind == 'itcm' else UC_PROT_READ | UC_PROT_WRITE
        bus = self._bus_range(address)
        return bus.permissions if bus else 0

    def _add_c_hook(self, htype: int, cb, begin: int = 1, end: int = 0) -> int:
        handle = _uc_bindings.uc_hook_h()
        status = _uc_hook_add(
            self._uch, ctypes.byref(handle), htype, cb, None,
            ctypes.c_uint64(begin), ctypes.c_uint64(end),
        )
        if status != _uc_bindings.uc.UC_ERR_OK:
            raise _uc_bindings.UcError(status)
        h = handle.value
        self._all_hooks.append(h)
        self._cb_refs[h] = cb
        if not self._pooled_uc:
            self.uc._callbacks[h] = cb
        return h

    def _del_c_hook(self, h: int) -> None:
        _uc_hook_del(self._uch, h)
        if h in self._cb_refs:
            del self._cb_refs[h]
        try:
            self._all_hooks.remove(h)
        except ValueError:
            pass
        if h in self.uc._callbacks:
            del self.uc._callbacks[h]

    def before_instruction(self) -> None:
        """Restore outer-bus bytes touched by the previous load-mode store.

        Unicorn's write hook runs before the store. The hook separately writes
        TCM and remembers outer-bus bytes, which must be restored before the
        next instruction can read them. Reverse order also handles overlapping
        stores within one instruction.
        """
        if not self._pending_restore:
            return
        for target, start, end, original in reversed(self._pending_restore):
            if target is not None:
                target[start:end] = original
            else:
                ctypes.memmove(start, original, end)
        self._pending_restore.clear()

    def _load_store(self, uc, access, address, size, value, mapping: TcmMapping):
        try:
            if size < 1 or size > 8:
                raise CpuModelError(f'unsupported TCM load-mode store width {size}')
            if address + size > mapping.address + mapping.size:
                raise CpuModelError('TCM load-mode store crosses its aperture')
            if address < FLASH_SIZE and address + size <= FLASH_SIZE:
                self._pending_restore.append((self._flash_buf, address, address + size, self._dump_mv[address:address + size]))
            else:
                bus = self._bus_range(address)
                if bus is None or address + size > bus.address + bus.size:
                    raise CpuModelError('TCM load-mode store requires an implemented outer-bus range')
                if bus.buf is not None:
                    b_off = address - bus.address
                    self._pending_restore.append((bus.buf, b_off, b_off + size, bytes(bus.buf[b_off:b_off + size])))
                else:
                    pointer = bus.pointer + address - bus.address
                    self._pending_restore.append((None, pointer, size, ctypes.string_at(pointer, size)))
            # Unicorn invokes ordinary write hooks before its protection fault
            # hook. Check before changing TCM so a denied store has no TCM effect.
            if self.cpu._mpu_enabled:
                try:
                    _uc_reg_read(self._uch, UC_ARM_REG_CPSR, self._reg_buf_ptr)
                    user = (self._reg_buf.value & 0x1F) == 0x10
                    if (address, size, False, True, user) not in self.cpu._allowed_access:
                        self.cpu.check_access(address, size, write=True, user=user)
                except CpuModelError as exc:
                    self.error = str(exc)
                    raise
            tcm_buf = self._itcm_buf if mapping.kind == 'itcm' else self._dtcm_buf
            offset = (address - mapping.address) & (mapping.physical_size - 1)
            first = min(size, mapping.physical_size - offset)
            if first == size and self.cpu.big_endian:
                if size == 4:
                    tcm_buf[offset] = (value >> 24) & 0xFF
                    tcm_buf[offset + 1] = (value >> 16) & 0xFF
                    tcm_buf[offset + 2] = (value >> 8) & 0xFF
                    tcm_buf[offset + 3] = value & 0xFF
                    return
                if size == 2:
                    tcm_buf[offset] = (value >> 8) & 0xFF
                    tcm_buf[offset + 1] = value & 0xFF
                    return
                if size == 1:
                    tcm_buf[offset] = value & 0xFF
                    return
            byteorder = 'big' if self.cpu.big_endian else 'little'
            data = int(value & ((1 << (size * 8)) - 1)).to_bytes(size, byteorder)
            tcm_buf[offset:offset + first] = data[:first]
            if first != size:
                tcm_buf[:size - first] = data[first:]
        except BaseException as exc:
            if self.uc._hook_exception is None:
                self.uc._hook_exception = exc
            self.uc.emu_stop()

    def _fetch_protected(self, uc, access, address, size, value, _):
        mapping = next((r for r in self._mappings if (r.kind == 'dtcm' or r.load_mode)
                        and r.address <= address < r.address + r.size), None)
        if mapping:
            reason = 'TCM in load mode' if mapping.load_mode else 'enabled DTCM'
            self.error = (
                f'instruction fetch at {address:#010x} overlaps {reason}; '
                'separate instruction/data bus translation is not implemented')
            # Unicorn reports UC_ERR_FETCH_PROT before reraising a Python
            # exception from an invalid-access hook. Preserve our diagnostic
            # explicitly so the emulator can prefer it to the generic error.
            self.uc.emu_stop()
        return False

    def _before_load_instruction(self, uc, address, size, _):
        if self._pending_restore:
            self.before_instruction()

    def sync_tcm(self) -> bool:
        """Apply CP15 enable/load/base/size changes, preserving physical banks."""
        self.before_instruction()
        mappings = tuple(r for r in self.cpu.tcm_regions() if r.enabled)
        if mappings == self._mappings:
            return False
        if mappings:
            self._tcm_dirty = True
        for mapping in mappings:
            if mapping.address & 0xFFF or mapping.size & 0xFFF:
                raise CpuModelError('TCM mapping must be page aligned')
            if mapping.size > 0x08000000:
                raise CpuModelError('TCM apertures above 128 MiB are not implemented')
            if mapping.physical_size != len(self._tcm[mapping.kind]):
                raise CpuModelError('physical TCM capacity cannot change after reset')
            if mapping.load_mode:
                covered = sum(size for _, size, _, _ in
                              self._bus_segments(mapping.address, mapping.size))
                if covered != mapping.size:
                    raise CpuModelError('TCM load mode requires implemented underlying bus memory')
        for index, first in enumerate(mappings):
            for second in mappings[index + 1:]:
                if max(first.address, second.address) < min(first.address + first.size,
                                                           second.address + second.size):
                    raise CpuModelError('overlapping ITCM/DTCM buses are not implemented')
        for hook in self._load_hooks:
            self._del_c_hook(hook)
        self._load_hooks.clear()
        unchanged = set(self._mappings) & set(mappings)
        to_remove = [m for m in self._mappings if m not in unchanged]
        to_add = [m for m in mappings if m not in unchanged]
        partial_overlap = any(
            max(o.address, n.address) < min(o.address + o.size, n.address + n.size)
            and (o.address, o.size) != (n.address, n.size)
            for o in to_remove for n in to_add
        )
        new_extents = set() if partial_overlap else {(m.address, m.size) for m in to_add}
        removed_extents = set() if partial_overlap else {(m.address, m.size) for m in to_remove}
        for old in to_remove:
            self.uc.mem_unmap(old.address, old.size)
            if (old.address, old.size) not in new_extents:
                for address, size, pointer, permissions in self._bus_segments(old.address, old.size):
                    self.uc.mem_map_ptr(address, size, permissions, pointer)
        for mapping in to_add:
            # Existing bus mappings may cover only part of a TCM aperture.
            if (mapping.address, mapping.size) not in removed_extents:
                for address, size, _, _ in self._bus_segments(mapping.address, mapping.size):
                    self.uc.mem_unmap(address, size)
            if mapping.load_mode:
                for address, size, pointer, permissions in self._bus_segments(mapping.address, mapping.size):
                    self.uc.mem_map_ptr(address, size, (permissions | UC_PROT_WRITE) & ~UC_PROT_EXEC, pointer)
            else:
                pointer = ctypes.addressof(self._tcm[mapping.kind])
                permissions = UC_PROT_ALL if mapping.kind == 'itcm' else UC_PROT_READ | UC_PROT_WRITE
                for offset in range(0, mapping.size, mapping.physical_size):
                    self.uc.mem_map_ptr(mapping.address + offset,
                                        min(mapping.physical_size, mapping.size - offset),
                                        permissions, pointer)
        self_ref = weakref.ref(self)
        for mapping in mappings:
            if mapping.load_mode:
                m = mapping
                cb = _C_MEM_CB(lambda uc, acc, addr, sz, val, ud, m=m: (mem := self_ref()) is not None and mem._load_store(uc, acc, addr, sz, val, m))
                self._load_hooks.append(self._add_c_hook(
                    UC_HOOK_MEM_WRITE, cb,
                    begin=mapping.address, end=mapping.address + mapping.size - 1))
                if mapping.address < FLASH_SIZE:
                    self._flash_clean = False
                    if mapping.address < self._flash_dirty_start:
                        self._flash_dirty_start = mapping.address
                    end_flash = min(FLASH_SIZE, mapping.address + mapping.size)
                    if end_flash > self._flash_dirty_end:
                        self._flash_dirty_end = end_flash
        self._mappings = mappings
        self._tcm_load_mode = any(m.load_mode for m in mappings)
        itcm = next((m for m in mappings if m.kind == 'itcm'), None)
        if itcm is not None:
            self._itcm_size = itcm.size
            self._itcm_phys_size = itcm.physical_size
        else:
            self._itcm_size = 0
            self._itcm_phys_size = 0
        dtcm = next((m for m in mappings if m.kind == 'dtcm'), None)
        if dtcm is not None:
            self._dtcm_base = dtcm.address
            self._dtcm_end = dtcm.address + dtcm.size
        else:
            self._dtcm_base = 0
            self._dtcm_end = 0
        self._layout_version += 1
        if self._tcm_load_mode:
            # Only load mode needs this per-instruction hook; normal TCM and
            # DDR use shared native backing and need no Python memory hooks.
            pending = self._pending_restore
            cb = _C_CODE_CB(lambda uc, addr, sz, ud: bool(pending and (mem := self_ref()) is not None and mem.before_instruction()))
            self._load_hooks.append(self._add_c_hook(UC_HOOK_CODE, cb))
        return True

    def dram_sha256(self) -> str:
        import hashlib
        _, _, dirty_dram = _get_dram_funcs()
        if dirty_dram is not None and self._dram_init_len == 0:
            offsets = (ctypes.c_uint32 * 16384)()
            n = dirty_dram(ctypes.addressof(self._dram), DRAM_SIZE, offsets, 16384)
            if n >= 0:
                h = hashlib.sha256()
                mv = self._dram_mv
                for i in range(n):
                    off = offsets[i]
                    h.update(off.to_bytes(4, 'little'))
                    h.update(mv[off:off + 4096])
                return h.hexdigest()
        return hashlib.sha256(self._dram).hexdigest()

    def mirror_flash_write(self, address: int, length: int, source: bytes | bytearray | memoryview) -> None:
        """Reflect an emulated SPI program/erase into the memory-mapped flash.

        Used only when the SPI endpoint is in its explicit writable mode: on the
        board the CPU window and the SPI command path read the same chip, so a
        write through the controller must become visible at 0x00000000+addr.
        The dirty range is recorded so the pooled flash buffer is restored
        from the dump when this instance is released, and ``check_flash`` stops
        treating the deviation as a fault.
        """
        if length <= 0:
            return
        end = min(FLASH_SIZE, address + length)
        self._flash_mv[address:end] = source[address:end]
        self._flash_clean = False
        self._flash_mirrored = True
        if address < self._flash_dirty_start:
            self._flash_dirty_start = address
        if end > self._flash_dirty_end:
            self._flash_dirty_end = end
        try:
            self.uc.ctl_remove_cache(address, end)
        except Exception:  # pragma: no cover - translation cache flush is best effort
            pass

    def check_flash(self) -> bool:
        self.before_instruction()
        if not self._flash_clean:
            if self._flash_mirrored:
                return False  # explicit writable-SPI emulation; restored on release
            s, e = self._flash_dirty_start, self._flash_dirty_end
            if s < e and self._flash_mv[s:e] != self._dump_mv[s:e]:
                raise CpuModelError('physical flash backing was modified')
            self._flash_clean = True
            self._flash_dirty_start = FLASH_SIZE
            self._flash_dirty_end = 0
        return True

