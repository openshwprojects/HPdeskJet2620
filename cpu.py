"""ARM946E-S system control and memory-protection state.

The silicon's synthesized RAM/cache sizes have not been read from the board.
The default profile implements 8 KiB ITCM, 4 KiB DTCM, and no caches.  The TCM
sizes are explicit configurable assumptions: boot uses a 4 KiB DTCM window and
supports discovering ITCM size.  The no-cache descriptor matches Unicorn's
ARM946 CPU profile, rather than reporting caches that are not implemented.
The default reset profile sets B=1 to match the fixed big-endian backend; the
physical board's BIGENDINIT strap has not been established. A big-endian image
alone does not prove that strap: ARM DDI 0100I section A2.7.3 permits the early
MRC/ORR #0x80/MCR sequence present in this firmware before subword/Thumb use.

Register encodings: ARM DDI 0201D, sections 2.3 and 4-5:
https://documentation-service.arm.com/static/5e8e3ee588295d1e18d3aa82
Unicorn CPU profile: qemu/target/arm/cpu.c, arm946_initfn (2.1.4).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

CP15Key = tuple[int, int, int, int]  # opcode1, CRn, CRm, opcode2
CONTROL: CP15Key = (0, 1, 0, 0)
DTCM_REGION: CP15Key = (0, 9, 1, 0)
ITCM_REGION: CP15Key = (0, 9, 1, 1)
_CACHE_MISS = object()


class CpuModelError(RuntimeError):
    """Unsupported or architecturally unpredictable system operation."""


@dataclass(frozen=True)
class TcmMapping:
    kind: str
    address: int
    size: int
    physical_size: int
    enabled: bool
    load_mode: bool

    @property
    def base(self) -> int:
        return self.address

    def physical_offset(self, address: int) -> int:
        """Larger visible regions repeat the same physical TCM storage."""
        if not self.physical_size or not self.address <= address < self.address + self.size:
            raise ValueError('address outside TCM')
        return (address - self.address) % self.physical_size


@dataclass(frozen=True)
class MpuRegion:
    index: int
    address: int
    size: int
    instruction_ap: int
    data_ap: int


def _tcm_size_encoding(size: int) -> int:
    if not size:
        return 0
    if size < 4096 or size > 0x100000 or size & (size - 1):
        raise ValueError('physical TCM size must be zero or a power of two from 4 KiB to 1 MiB')
    return size.bit_length() - 10


class CP15:
    """Architectural CP15 state, separate from Unicorn's unsupported TCM regs.

    on_change(key, old_value, new_value) runs after each accepted write. The
    emulator must update its backing maps for CONTROL/DTCM_REGION/ITCM_REGION
    changes. In load mode, reads and instruction fetches bypass TCM while data
    writes target TCM. With ITCM enabled outside load mode, code and data both
    use ITCM. DTCM never supplies instruction fetches.

    check_access implements the MPU decision and raises on denied accesses;
    the emulator chooses whether to deliver an abort or report the fault.
    """

    # With no modeled caches or deferred write buffer, these architecturally
    # defined maintenance operations have no dirty state to process.
    CACHE_OPERATIONS = {
        (0, 7, 5, 0): 'invalidate-instruction-cache',
        (0, 7, 5, 1): 'invalidate-instruction-cache-line',
        (0, 7, 6, 0): 'invalidate-data-cache',
        (0, 7, 6, 1): 'invalidate-data-cache-line',
        (0, 7, 10, 1): 'clean-data-cache-line',
        (0, 7, 10, 2): 'clean-data-cache-index',
        (0, 7, 10, 4): 'drain-write-buffer',
        (0, 7, 14, 1): 'clean-invalidate-data-cache-line',
        (0, 7, 14, 2): 'clean-invalidate-data-cache-index',
    }

    def __init__(self, *, itcm_size: int = 8192, dtcm_size: int = 4096,
                 big_endian: bool = True,
                 on_change: Callable[[CP15Key, int, int], None] | None = None):
        self.itcm_size, self.dtcm_size = itcm_size, dtcm_size
        self.big_endian = bool(big_endian)
        i_code, d_code = _tcm_size_encoding(itcm_size), _tcm_size_encoding(dtcm_size)
        self.values: dict[CP15Key, int] = {
            (0, 0, 0, 0): 0x41059461,
            (0, 0, 0, 1): 0x0F004006,
            (0, 0, 0, 2): (d_code << 18) | (i_code << 6)
                | (0 if dtcm_size else 1 << 14) | (0 if itcm_size else 1 << 2),
            CONTROL: 0x78 | (0x80 if self.big_endian else 0),
            DTCM_REGION: d_code << 1,
            ITCM_REGION: i_code << 1,
            (0, 2, 0, 0): 0, (0, 2, 0, 1): 0, (0, 3, 0, 0): 0,
            (0, 5, 0, 2): 0, (0, 5, 0, 3): 0,
            (0, 9, 0, 0): 0, (0, 9, 0, 1): 0,
            **{(0, 6, region, 0): 0 for region in range(8)},
        }
        self.on_change = on_change
        self.maintenance_count = 0
        self._mpu_enabled = False
        self._mpu_regions_cache: tuple[MpuRegion, ...] | None = None
        self._caches_by_mode: tuple[
            tuple[dict[tuple[int, int, bool, bool, bool], str | None], set[tuple[int, int, bool, bool, bool]]],
            tuple[dict[tuple[int, int, bool, bool, bool], str | None], set[tuple[int, int, bool, bool, bool]]],
        ] = (({}, set()), ({}, set()))
        self._access_cache, self._allowed_access = self._caches_by_mode[0]

    def invalidate_mpu_cache(self) -> None:
        self._mpu_regions_cache = None
        for acc, allow in self._caches_by_mode:
            acc.clear()
            allow.clear()
        self.sync_mpu_mode()

    def sync_mpu_mode(self) -> None:
        self._mpu_enabled = bool(self.values[CONTROL] & 1)
        self._access_cache, self._allowed_access = self._caches_by_mode[1 if self._mpu_enabled else 0]

    @property
    def control(self) -> int:
        return self.values[CONTROL]

    @property
    def mpu_enabled(self) -> bool:
        return self._mpu_enabled

    def read(self, key: CP15Key) -> int:
        if key in ((0, 5, 0, 0), (0, 5, 0, 1)):
            extended = self.values[(0, 5, 0, key[3] + 2)]
            return sum(((extended >> (4 * n)) & 3) << (2 * n) for n in range(8))
        if key[:2] == (0, 6) and 0 <= key[2] < 8 and key[3] == 1:
            key = (*key[:3], 0)
        try:
            return self.values[key]
        except KeyError:
            raise CpuModelError(f'unmodeled CP15 read {key}') from None

    @staticmethod
    def _decode_aligned_size(value: int, kind: str, min_exp: int, max_exp: int, shift: int) -> int:
        exponent = (value >> 1) & 31
        if not min_exp <= exponent <= max_exp:
            raise CpuModelError(f'invalid {kind} region size encoding {exponent}')
        size = 1 << (exponent + shift)
        if (value & 0xFFFFF000) & (size - 1):
            raise CpuModelError(f'misaligned {kind} region {value:#010x}')
        return size

    @classmethod
    def _region_size(cls, value: int) -> int:
        return cls._decode_aligned_size(value, 'MPU', 11, 31, 1)

    @classmethod
    def _tcm_visible_size(cls, value: int) -> int:
        return cls._decode_aligned_size(value, 'TCM', 3, 23, 9)

    def write(self, key: CP15Key, value: int) -> str | None:
        value &= 0xFFFFFFFF
        if key in self.CACHE_OPERATIONS:
            self.maintenance_count += 1
            if self.on_change:
                self.on_change(key, 0, value)
            return self.CACHE_OPERATIONS[key]
        if key == (0, 7, 0, 4):
            return 'wait-for-interrupt'
        if key in ((0, 5, 0, 0), (0, 5, 0, 1)):
            key = (0, 5, 0, key[3] + 2)
            value = sum(((value >> (2 * n)) & 3) << (4 * n) for n in range(8))
        if key not in self.values or key[1] == 0:
            raise CpuModelError(f'unmodeled CP15 write {key}')
        if key == CONTROL:
            if bool(value & 0x80) != self.big_endian:
                raise CpuModelError('changing the configured CPU byte order is not implemented')
            if value & 2:
                # DDI 0201D Table 2-9: ARM946 bit 1 is reserved SBZ,
                # not the alignment-check enable found in other ARM cores.
                raise CpuModelError('ARM946 SCTLR bit 1 is reserved; alignment-check control is absent')
            # ARM946 has RES1[6:3], RES0[31:20,11:8,1].
            value = (value & 0x000FF085) | 0x78
            if value & 1 and not any(self.values[(0, 6, n, 0)] & 1 for n in range(8)):
                raise CpuModelError('MPU enabled without a valid region')
        elif key in (ITCM_REGION, DTCM_REGION):
            if key == ITCM_REGION and value & 0xFFFFF000:
                raise CpuModelError('ARM946 ITCM base is fixed at address zero')
            self._tcm_visible_size(value)
            value &= 0xFFFFF03E
        elif key[1] == 6 and value & 1:
            self._region_size(value)
        elif key[1] in (2, 3):
            value &= 0xFF
        elif key in ((0, 9, 0, 0), (0, 9, 0, 1)):
            value &= 0x80000003
        old_value = self.values[key]
        self.values[key] = value
        if key == CONTROL:
            if (old_value ^ value) & 1:
                self.sync_mpu_mode()
        elif key[1] in (5, 6) and old_value != value:
            self.invalidate_mpu_cache()
        if self.on_change:
            self.on_change(key, old_value, value)
        return None

    def tcm_regions(self) -> tuple[TcmMapping, TcmMapping]:
        mappings = []
        for kind, key, physical, enable_bit, load_bit in (
            ('itcm', ITCM_REGION, self.itcm_size, 18, 19),
            ('dtcm', DTCM_REGION, self.dtcm_size, 16, 17),
        ):
            value = self.values[key]
            mappings.append(TcmMapping(
                kind, value & 0xFFFFF000,
                self._tcm_visible_size(value) if physical else 0, physical,
                bool(physical and self.control & (1 << enable_bit)),
                bool(physical and self.control & (1 << load_bit)),
            ))
        return tuple(mappings)

    def mpu_regions(self) -> tuple[MpuRegion, ...]:
        if self._mpu_regions_cache is not None:
            return self._mpu_regions_cache
        regions = []
        for n in range(8):
            value = self.values[(0, 6, n, 0)]
            if value & 1:
                regions.append(MpuRegion(
                    n, value & 0xFFFFF000, self._region_size(value),
                    (self.values[(0, 5, 0, 3)] >> (n * 4)) & 15,
                    (self.values[(0, 5, 0, 2)] >> (n * 4)) & 15,
                ))
        self._mpu_regions_cache = tuple(regions)
        return self._mpu_regions_cache

    def check_access(self, address: int, size: int = 1, *, instruction: bool = False,
                     write: bool = False, user: bool = False) -> None:
        if not self._mpu_enabled:
            if size <= 0 or address < 0 or address + size > 0x100000000:
                raise ValueError('invalid memory access extent')
            self._allowed_access.add((address, size, instruction, write, user))
            return
        cache_key = (address, size, instruction, write, user)
        cached_err = self._access_cache.get(cache_key, _CACHE_MISS)
        if cached_err is not _CACHE_MISS:
            if cached_err is not None:
                raise CpuModelError(cached_err)
            return
        if size <= 0 or address < 0 or address + size > 0x100000000:
            raise ValueError('invalid memory access extent')
        regions = self.mpu_regions()
        # Permissions are constant between region boundaries. Checking only
        # endpoints misses a restricted region wholly inside a large extent
        # (for example, a translated instruction block).
        checkpoints = {address, address + size - 1}
        for region in regions:
            for boundary in (region.address, region.address + region.size):
                if address <= boundary < address + size:
                    checkpoints.add(boundary)
        for endpoint in sorted(checkpoints):
            region = next((r for r in reversed(regions)
                           if r.address <= endpoint < r.address + r.size), None)
            msg = None
            if region is None:
                msg = f'MPU no matching region at {endpoint:#010x}'
            else:
                ap = region.instruction_ap if instruction else region.data_ap
                if ap not in (0, 1, 2, 3, 5, 6):
                    msg = f'unpredictable MPU permission {ap} in region {region.index}'
                else:
                    allowed = (ap == 3 or (ap in (1, 2) and not user)
                               or (ap == 2 and user and not write)
                               or (ap == 5 and not user and not write)
                               or (ap == 6 and not write))
                    if not allowed:
                        operation = 'fetch' if instruction else 'write' if write else 'read'
                        msg = f'MPU denied {operation} at {endpoint:#010x}, region {region.index}'
            if msg is not None:
                self._access_cache[cache_key] = msg
                raise CpuModelError(msg)
        self._access_cache[cache_key] = None
        self._allowed_access.add(cache_key)

    def is_access_allowed(self, address: int, size: int = 1, *, instruction: bool = False,
                          write: bool = False, user: bool = False) -> bool:
        cache_key = (address, size, instruction, write, user)
        if cache_key in self._allowed_access:
            return True
        if cache_key in self._access_cache:
            return False
        try:
            self.check_access(address, size, instruction=instruction, write=write, user=user)
            return True
        except (CpuModelError, ValueError):
            return False

