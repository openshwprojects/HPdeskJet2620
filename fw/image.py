"""Image header format and 16 MiB flash image assembly for the HPDJ firmware.

The numeric layout constants are read from ``fw/common/layout.h`` so the C
code and this module cannot drift apart. The header format mirrors
``fw/common/image.h`` (64 bytes, big-endian u32 fields, CRC-32/IEEE).
"""
from __future__ import annotations

import re
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

FW_DIR = Path(__file__).resolve().parent
ROOT_DIR = FW_DIR.parent
LAYOUT_H = FW_DIR / 'common' / 'layout.h'

IMAGE_MAGIC = 0x4850444A          # "HPDJ"
IMAGE_HEADER_VERSION = 1
IMAGE_TYPE_BL = 1
IMAGE_TYPE_APP = 2
IMAGE_NAME_LEN = 20
HEADER_STRUCT = struct.Struct('>IIIIIIIIII20sI')
assert HEADER_STRUCT.size == 64

# HP stage-1 container header fields patched to scatter-load the bootloader
# (see custom_firmware._patch_stage2_container for the derivation).
CONTAINER_ENTRY_FIELD = 0x00020068
CONTAINER_ZERO_RANGE = 0x00020078
CONTAINER_RAW_RANGE = 0x00020084
CONTAINER_LZSS_RANGE = 0x00020090
CONTAINER_RAW_ENTRY = 0x000201EC

# Regions of the stock image that would make HP stage-1 overlay its own
# firmware update on top of our code (analysis/update_writer.md section 1):
GHS_HEADER_SECTOR = 0x00C93000    # GHS stream magic 7F474853 at 0xC93E98
HP_FOOTER_SECTOR = 0x00FFF000     # footer magic 3CA55A7D at 0xFFFF94


def parse_c_defines(path: Path = LAYOUT_H) -> dict[str, int]:
    """Evaluate the plain numeric ``#define``s of a C header."""
    values: dict[str, int] = {}
    define = re.compile(r'^\s*#define\s+([A-Za-z_]\w*)\s+(.+?)\s*(?:/\*.*)?$')
    for line in path.read_text(encoding='utf-8').splitlines():
        m = define.match(line)
        if not m or '(' in m.group(1):
            continue
        name, expr = m.group(1), m.group(2)
        if expr.startswith('(') and re.match(r'^\(\(\(\(unsigned', expr):
            continue
        expr = re.sub(r'\b(0x[0-9A-Fa-f]+|\d+)[uUlL]+\b', r'\1', expr)
        try:
            values[name] = int(eval(expr, {'__builtins__': {}}, dict(values)))  # noqa: S307 - own header
        except Exception:
            continue
    return values


LAYOUT = parse_c_defines()
FLASH_TOTAL_SIZE = LAYOUT['FLASH_TOTAL_SIZE']
FLASH_SECTOR_SIZE = LAYOUT['FLASH_SECTOR_SIZE']
FLASH_BL_OFFSET = LAYOUT['FLASH_BL_OFFSET']
FLASH_BL_SIZE = LAYOUT['FLASH_BL_SIZE']
FLASH_PROTECT_END = LAYOUT['FLASH_PROTECT_END']
FLASH_SLOT_A_OFFSET = LAYOUT['FLASH_SLOT_A_OFFSET']
FLASH_SLOT_B_OFFSET = LAYOUT['FLASH_SLOT_B_OFFSET']
FLASH_SLOT_SIZE = LAYOUT['FLASH_SLOT_SIZE']
IMAGE_HEADER_SIZE = LAYOUT['IMAGE_HEADER_SIZE']
BL_LOAD_ADDR = LAYOUT['BL_LOAD_ADDR']
BL_CODE_MAX = LAYOUT['BL_CODE_MAX']
BL_DATA_ADDR = LAYOUT['BL_DATA_ADDR']
BL_DATA_SIZE = LAYOUT['BL_DATA_SIZE']
BL_STACK_TOP = LAYOUT['BL_STACK_TOP']
APP_LOAD_ADDR = LAYOUT['APP_LOAD_ADDR']
APP_CODE_MAX = LAYOUT['APP_CODE_MAX']
APP_DATA_ADDR = LAYOUT['APP_DATA_ADDR']
APP_DATA_SIZE = LAYOUT['APP_DATA_SIZE']
APP_STACK_TOP = LAYOUT['APP_STACK_TOP']
STAGING_ADDR = LAYOUT['STAGING_ADDR']
BOOTINFO_ADDR = LAYOUT['BOOTINFO_ADDR']
SLOT_OFFSETS = (FLASH_SLOT_A_OFFSET, FLASH_SLOT_B_OFFSET)


def crc32(data: bytes | bytearray | memoryview) -> int:
    return zlib.crc32(bytes(data)) & 0xFFFFFFFF


def version_u32(text: str) -> int:
    parts = [int(p) for p in text.split('.')]
    while len(parts) < 3:
        parts.append(0)
    if len(parts) != 3 or any(not 0 <= p <= 255 for p in parts):
        raise ValueError(f'version must be MAJOR.MINOR.PATCH with each part 0..255, got {text!r}')
    return (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8)


def version_str(value: int) -> str:
    return f'{(value >> 24) & 0xFF}.{(value >> 16) & 0xFF}.{(value >> 8) & 0xFF}'


@dataclass(frozen=True)
class ImageHeader:
    image_type: int
    version: int
    sequence: int
    payload_size: int
    load_address: int
    entry_address: int
    payload_crc32: int
    build_time: int
    name: str
    header_version: int = IMAGE_HEADER_VERSION
    header_crc32: int | None = None

    def pack(self) -> bytes:
        body = HEADER_STRUCT.pack(
            IMAGE_MAGIC, self.header_version, self.image_type, self.version, self.sequence,
            self.payload_size, self.load_address, self.entry_address, self.payload_crc32,
            self.build_time, self.name.encode('ascii')[:IMAGE_NAME_LEN].ljust(IMAGE_NAME_LEN, b'\0'), 0)
        return body[:60] + struct.pack('>I', crc32(body[:60]))

    @classmethod
    def unpack(cls, data: bytes) -> 'ImageHeader':
        if len(data) < IMAGE_HEADER_SIZE:
            raise ValueError('header truncated')
        (magic, hver, itype, ver, seq, size, load, entry, pcrc, btime, name, hcrc) = HEADER_STRUCT.unpack(
            data[:IMAGE_HEADER_SIZE])
        if magic != IMAGE_MAGIC:
            raise ValueError(f'bad image magic {magic:#010x}')
        return cls(itype, ver, seq, size, load, entry, pcrc, btime,
                   name.rstrip(b'\0').decode('ascii', 'replace'), hver, hcrc)

    @property
    def version_text(self) -> str:
        return version_str(self.version)


def make_image(payload: bytes, *, image_type: int, version: int, sequence: int,
               load_address: int, entry_address: int, build_time: int, name: str) -> bytes:
    """Header + payload (payload is padded to a multiple of four bytes)."""
    if len(payload) % 4:
        payload = payload + b'\0' * (4 - len(payload) % 4)
    max_size = BL_CODE_MAX if image_type == IMAGE_TYPE_BL else APP_CODE_MAX
    if len(payload) > max_size:
        raise ValueError(f'payload {len(payload)} bytes exceeds the {max_size}-byte limit for type {image_type}')
    if not load_address <= entry_address < load_address + len(payload) or entry_address & 3:
        raise ValueError(f'entry {entry_address:#x} outside payload at {load_address:#x}')
    if not 0 <= sequence <= 0x7FFFFFFF:
        raise ValueError('sequence must be 0..0x7FFFFFFF (the bootloader rejects larger values)')
    hdr = ImageHeader(image_type, version, sequence, len(payload), load_address, entry_address,
                      crc32(payload), build_time, name)
    return hdr.pack() + payload


def validate_image(data: bytes, *, expect_type: int | None = None) -> ImageHeader:
    """Raise ValueError unless ``data`` is a complete, consistent header+payload."""
    hdr = ImageHeader.unpack(data)
    if crc32(data[:60]) != hdr.header_crc32:
        raise ValueError('header crc mismatch')
    if expect_type is not None and hdr.image_type != expect_type:
        raise ValueError(f'image type {hdr.image_type} != expected {expect_type}')
    if len(data) < IMAGE_HEADER_SIZE + hdr.payload_size:
        raise ValueError('payload truncated')
    payload = data[IMAGE_HEADER_SIZE:IMAGE_HEADER_SIZE + hdr.payload_size]
    if crc32(payload) != hdr.payload_crc32:
        raise ValueError('payload crc mismatch')
    return hdr


def describe_image(data: bytes) -> str:
    hdr = validate_image(data)
    kind = {IMAGE_TYPE_BL: 'bootloader', IMAGE_TYPE_APP: 'application'}.get(hdr.image_type, '?')
    return (f'{kind} "{hdr.name}" v{hdr.version_text} seq {hdr.sequence}, {hdr.payload_size} bytes, '
            f'load {hdr.load_address:#010x} entry {hdr.entry_address:#010x}, '
            f'payload crc {hdr.payload_crc32:08x}, build time {hdr.build_time}')


def patch_container_for_bootloader(img: bytearray, bl_image: bytes) -> None:
    """Write the BL image at FLASH_BL_OFFSET and point HP stage-1 at its payload."""
    hdr = validate_image(bl_image, expect_type=IMAGE_TYPE_BL)
    if hdr.load_address != BL_LOAD_ADDR:
        raise ValueError(f'bootloader must load at {BL_LOAD_ADDR:#x}, header says {hdr.load_address:#x}')
    if len(bl_image) > FLASH_BL_SIZE:
        raise ValueError('bootloader image does not fit its flash region')
    img[FLASH_BL_OFFSET:FLASH_BL_OFFSET + FLASH_BL_SIZE] = b'\xFF' * FLASH_BL_SIZE
    img[FLASH_BL_OFFSET:FLASH_BL_OFFSET + len(bl_image)] = bl_image
    payload_offset = FLASH_BL_OFFSET + IMAGE_HEADER_SIZE
    struct.pack_into('>I', img, CONTAINER_ENTRY_FIELD, hdr.entry_address)
    struct.pack_into('>3I', img, CONTAINER_ZERO_RANGE, 0x00020180, 0x00020180, 0x00020180)
    struct.pack_into('>3I', img, CONTAINER_RAW_RANGE, CONTAINER_RAW_ENTRY, CONTAINER_RAW_ENTRY + 12, CONTAINER_RAW_ENTRY)
    struct.pack_into('>3I', img, CONTAINER_LZSS_RANGE, 0x000202D0, 0x000202D0, 0x000202D0)
    struct.pack_into('>3I', img, CONTAINER_RAW_ENTRY, hdr.load_address, payload_offset, hdr.payload_size)


def write_slot(img: bytearray, slot: int, app_image: bytes | None) -> None:
    """Erase a slot (0xFF) and optionally place an application image in it."""
    base = SLOT_OFFSETS[slot]
    img[base:base + FLASH_SLOT_SIZE] = b'\xFF' * FLASH_SLOT_SIZE
    if app_image is not None:
        hdr = validate_image(app_image, expect_type=IMAGE_TYPE_APP)
        if hdr.load_address != APP_LOAD_ADDR:
            raise ValueError(f'application must load at {APP_LOAD_ADDR:#x}, header says {hdr.load_address:#x}')
        if len(app_image) > FLASH_SLOT_SIZE:
            raise ValueError('application image does not fit a slot')
        img[base:base + len(app_image)] = app_image


def build_flash_image(base_dump: bytes, bl_image: bytes, app_image: bytes | None, *,
                      slot: int = 0, neutralize_hp_update: bool = True) -> bytes:
    """Assemble the 16 MiB image to program with an external SPI programmer.

    The HP stage-1 bootloader (first 0x22000 bytes) is kept byte for byte and
    its container header is repointed at our bootloader. Slot ``slot`` receives
    the application, the other slot is erased. With ``neutralize_hp_update``
    the HP footer sector and the GHS stream header sector are erased so that a
    unit whose EEPROM says "update applied" cannot overlay HP code onto ours.
    Everything else keeps the original dump contents.
    """
    if len(base_dump) != FLASH_TOTAL_SIZE:
        raise ValueError(f'base dump must be {FLASH_TOTAL_SIZE} bytes')
    img = bytearray(base_dump)
    patch_container_for_bootloader(img, bl_image)
    for s in range(len(SLOT_OFFSETS)):
        write_slot(img, s, app_image if s == slot else None)
    if neutralize_hp_update:
        for sector in (GHS_HEADER_SECTOR, HP_FOOTER_SECTOR):
            img[sector:sector + FLASH_SECTOR_SIZE] = b'\xFF' * FLASH_SECTOR_SIZE
    return bytes(img)


def read_slot_image(flash: bytes, slot: int) -> bytes | None:
    """Return the validated header+payload stored in a slot, or None."""
    base = SLOT_OFFSETS[slot]
    try:
        hdr = ImageHeader.unpack(flash[base:base + IMAGE_HEADER_SIZE])
    except ValueError:
        return None
    data = bytes(flash[base:base + IMAGE_HEADER_SIZE + hdr.payload_size])
    try:
        validate_image(data, expect_type=IMAGE_TYPE_APP)
    except ValueError:
        return None
    return data
