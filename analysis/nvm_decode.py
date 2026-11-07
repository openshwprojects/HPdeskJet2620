"""Read-only decoder for the HP DeskJet 2620 settings EEPROM (ST M24512, 64 KiB).

    python -m analysis.nvm_decode <eeprom.bin> [--json] [--objects] [--crosscheck]
                                  [--chip-id W10,W14,W18,W1C | --nvm-key HEX32]
                                  [--show-identifiers]

The input may be a raw 64 KiB EEPROM dump, HP's 64 KiB RAM mirror of it
(references/physical-51-nvm-mirror/nvm-ram-mirror-65536.bin, same byte layout), or a
shorter prefix such as the 256-byte header capture; whatever is missing is reported
as unavailable. The file is only read.

Layout (every address below is an EEPROM byte offset):

* 0x0000-0x00b1  boot "NOS" region. Byte 0 selects one of three region tables in the
  boot ROM (0xa0010f8c); items are described by 8-byte descriptors at 0xa0011924 and
  read by 0xa000c9f6 with copy/complement/checksum recovery. Board ID, serial number,
  update-gate bits, clock offset and HP's hardware error log live here, in clear text.
* 0x00b2-0xffff  "NVM3": 589 bytes of metadata, then 253 pages of 256 bytes at 0x02ff.
  Pages 0-59 are the journal; the page map at 0x00bf lists the data pages in logical
  order; pages whose bit is set in the bitmap at 0x01bc end in a hole chain. With the
  holes removed, the logical pages form one stream: a raw 32-bit DSID, then for every
  stored object a prefix-coded DSID delta and its value tokens, sorted by DSID.
* Multi-byte value tokens are XORed with a per-DSID AES-128 keystream whose key the
  firmware derives from on-chip ID registers 0xbd040010..0xbd04001c (not stored in the
  EEPROM). Without that key only the 6-bit single-byte values and the high bits of
  wider values are readable; --chip-id or --nvm-key enables full decryption.

See analysis/nvm.md for the evidence behind each statement and its confidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EEPROM_SIZE = 0x10000

# ---------------------------------------------------------------------------
# Boot (NOS) region: proven from the boot ROM tables (load_rodata/load_data).
# ---------------------------------------------------------------------------
# (region, length); None = rest of the part. 0xa0010fa4 (0x30), 0xa0010ff4 (0x33)
# and 0x20001804 (0x40, initialised from load_data); selector table at 0xa0010f8c.
_COMMON = [(0x0, 1), (0xb, 2), (0xc, 2), (0x1, 1), (0xa, 2), (0x2, 2), (0x3, 2), (0x9, 5)]
LAYOUT_TABLES = {
    0x30: _COMMON + [(0x8, 0), (0xd, 0x1f), (0xe, 2), (0xf, 2), (0x10, 2), (0x12, 2), (0x13, 2),
                     (0x14, 4), (0x4, 0x46), (0x5, 2), (0x6, 0xe), (0x7, None)],
    0x33: _COMMON + [(0x8, 0xe), (0xd, 0x1f), (0xe, 2), (0xf, 2), (0x10, 2), (0x12, 2), (0x13, 2),
                     (0x14, 4), (0x4, 0x46), (0x5, 2), (0x6, 0xe), (0x7, None)],
    0x40: _COMMON + [(0x8, 0), (0xd, 0x29), (0xe, 2), (0xf, 2), (0x10, 2), (0x12, 2), (0x13, 2),
                     (0x14, 4), (0x11, 6), (0x4, 0x46), (0x5, 2), (0x6, 0xe), (0x7, None)],
}
LAYOUT_ADDRESSES = {0x30: '0xa0010fa4', 0x33: '0xa0010ff4', 0x40: '0x20001804'}

REGION_NAMES = {  # names printed by HP's nvm2.get_map_details are marked proven
    0x4: ('hardware error log (5 x 14-byte ring)', 'proven: get_map_details "Error Log" 0x5c/70; ring code 0xa0007314'),
    0x5: ('error log header: ring index + XOR checksum', 'proven: 0xa0007314/0xa0007540'),
    0x6: ('"Eagle bits"', 'proven name: get_map_details "Eagle Bits" 0xa4/14; meaning unknown'),
    0x7: ('NVM3 data store', 'proven: get_map_details "NVM3 Offset" 0xb2/65358'),
    0x14: ('clock offset (item 12)', 'proven: get_map_details "Clock Offset" 0x52/4'),
}

# Descriptors at 0xa0011924: item, region, payload length, validation mode, default.
NVM_ITEMS = [
    (0, 0x0, 1, 1, '00', 'NVM layout byte (high nibble = map revision)', 'proven: selects the region table (0xa0007590); get_map_details prints revision 4'),
    (1, 0xa, 1, 2, '03', 'unknown flags; nos_nvm_sirius keys 0-7 (bits) and 15 (byte); DS2 mirror DSID 65570', 'item/key mapping proven (table 0x20847eb4); meaning unknown'),
    (4, 0x9, 2, 0, '001f', 'board ID (Mfg/Fam/board number)', 'proven: analysis/board_id.md'),
    (2, 0xc, 1, 2, '08', 'unknown', 'unknown'),
    (3, 0xb, 1, 2, 'aa', 'boot update-gate bits (per-slot bit pairs)', 'strong: analysis/storage_path.md'),
    (5, 0xd, 20, 3, '303030303030303030303130000000000000ff4b', 'serial number (ASCII, NUL padded)', 'strong: HP-style serial text; firmware hashes its first 15 bytes into an NVM key (0xa0044e54)'),
    (6, 0xe, 1, 2, '00', 'unknown flags; nos_nvm_sirius keys 8-14, 19 (bits) and 16 (byte); DS2 mirror DSID 75032', 'item/key mapping proven; meaning unknown'),
    (7, 0xf, 1, 2, '00', 'unknown', 'unknown'),
    (8, 0x1, 1, 1, 'ff', 'unknown', 'unknown'),
    (9, 0x10, 1, 2, '4b', 'unknown', 'unknown'),
    (10, 0x12, 1, 2, '00', 'unknown', 'unknown'),
    (11, 0x13, 1, 2, '00', 'unknown', 'unknown'),
    (12, 0x14, 4, 1, '00000000', 'clock offset (32-bit, raw); nos_nvm_sirius key 18', 'proven region name; HP returned -1 for key 18'),
]
MODE_TEXT = {0: 'copy + complement + checksum', 1: 'raw', 2: 'copy + complement',
             3: 'copy + complement + checksum (length capped)', 4: 'empty'}


def layout_for(byte0: int) -> tuple[int, list[tuple[int, int, int]]]:
    """Return (table version, [(region, offset, length)]) as 0xa0007590/0xa00075f8 do."""
    if byte0 >> 4 >= 4:
        version = next((v for v in LAYOUT_TABLES if v >> 4 == byte0 >> 4), 0x40)
    else:
        version = byte0 if byte0 in LAYOUT_TABLES else 0x40  # missing table: ROM default
    table = list(LAYOUT_TABLES[version])
    if byte0 >> 4 >= 4:  # 0xa00075f8: region 8 grows with the low nibble
        table = [(r, ((byte0 & 0xf) + 3) * 4 + 2) if r == 0x8 else (r, n) for r, n in table]
    out, offset = [], 0
    for region, length in table:
        length = EEPROM_SIZE - offset if length is None else length
        out.append((region, offset, length))
        offset += length
    return version, out


def _xor(data: bytes) -> int:
    value = 0
    for byte in data:
        value ^= byte
    return value


def read_item(image: bytes, offset: int, region_len: int, payload: int, mode: int,
              default: bytes) -> dict:
    """Mirror of 0xa000c9f6: returns the selected bytes, the source and the status."""
    if mode == 3:
        payload = min(payload, (region_len - 1) // 2)
    if mode == 4 or region_len == 0:
        return {'selected': b'', 'source': 'empty', 'status': 2, 'checks': {}}
    if offset + region_len > len(image):
        return {'selected': None, 'source': 'unavailable', 'status': None, 'checks': {}}
    n = payload
    primary = image[offset:offset + n]
    if mode == 1:
        return {'selected': primary, 'source': 'raw', 'status': 2, 'checks': {}}
    backup = image[offset + n:offset + 2 * n]
    complement_ok = all(a == b ^ 0xFF for a, b in zip(primary, backup))
    checks = {'complement_ok': complement_ok}
    primary_ok = backup_ok = False
    stored = None
    if mode in (0, 3):
        stored = image[offset + 2 * n]
        primary_ok = (0xFF ^ mode ^ _xor(primary)) == stored
        backup_ok = (0xFF ^ mode ^ _xor(bytes(b ^ 0xFF for b in backup))) == stored
        checks.update(stored_checksum=f'0x{stored:02x}', primary_checksum_ok=primary_ok,
                      backup_checksum_ok=backup_ok)
    if not (complement_ok ^ primary_ok ^ backup_ok):
        return {'selected': default, 'source': 'firmware default', 'status': 0, 'checks': checks}
    if backup_ok:
        decoded = bytes(b ^ 0xFF for b in backup)
        if complement_ok:
            return {'selected': decoded, 'source': 'complemented copy', 'status': 2, 'checks': checks}
        if stored == 0:
            return {'selected': default, 'source': 'firmware default', 'status': 0, 'checks': checks}
        return {'selected': decoded, 'source': 'complemented copy (recovered)', 'status': 1, 'checks': checks}
    status = 1 if mode in (0, 3) else 2
    return {'selected': primary, 'source': 'primary copy', 'status': status, 'checks': checks}


def _firmware_string(pointer: int) -> str | None:
    """Resolve a code pointer to a C string in the reconstructed DRAM image, if present."""
    path = ROOT / 'artifacts/ghs/dram.bin'
    if not (0xA0000000 <= pointer < 0xA4000000) or not path.exists():
        return None
    with path.open('rb') as handle:
        handle.seek(pointer & 0x3FFFFFF)
        raw = handle.read(96).split(b'\0', 1)[0]
    return raw.decode('ascii') if raw and all(32 <= c < 127 for c in raw) else None


def decode_error_log(image: bytes, regions: dict) -> dict | None:
    """HP's boot-level assert ring (0xa0007314, writer 0xa00074b0, reader 0xa00073f6)."""
    if 0x4 not in regions or 0x5 not in regions:
        return None
    off, length = regions[0x4]
    hoff, _ = regions[0x5]
    if hoff + 2 > len(image):
        return None
    entry_size = 14 if length % 14 == 0 else 10
    count = length // entry_size
    header, check = image[hoff], image[hoff + 1]
    index = 0 if header == 0xFF or (header & 0x7F) > count else header & 0x7F
    body = image[off:off + length]
    entries = []
    for k in range(count):  # k = 0 is the most recent entry
        slot = index - k if k <= index else index + count - k
        if slot >= count:
            continue
        raw = body[slot * entry_size:(slot + 1) * entry_size]
        ident = int.from_bytes(raw[0:4], 'big')
        if ident == 0:
            continue
        entry = {'entry': k, 'slot': slot, 'offset': f'0x{off + slot * entry_size:04x}',
                 'assert_id': f'0x{ident:08x}', 'line': int.from_bytes(raw[4:6], 'big'),
                 'file_pointer': f'0x{int.from_bytes(raw[6:10], "big"):08x}',
                 'cid_inferred': f'0x{(ident >> 16) & 0xFFF:x}', 'info_inferred': f'0x{ident & 0xFFFF:04x}'}
        entry['file'] = _firmware_string(int.from_bytes(raw[6:10], 'big'))
        if entry_size == 14:
            entry['page_count'] = int.from_bytes(raw[10:14], 'big')
        entries.append(entry)
    return {'entry_size': entry_size, 'slots': count, 'ring_index': index, 'header_flag': bool(header & 0x80),
            'checksum_ok': _xor(body) == check, 'entries': entries}


def decode_boot(image: bytes, show_ids: bool) -> dict:
    version, table = layout_for(image[0])
    regions = {r: (o, n) for r, o, n in table}
    items = []
    for item, region, payload, mode, default, meaning, confidence in NVM_ITEMS:
        off, rlen = regions[region]
        result = read_item(image, off, rlen, payload, mode, bytes.fromhex(default))
        sel = result['selected']
        row = {'item': item, 'region': f'0x{region:x}', 'offset': f'0x{off:04x}', 'region_length': rlen,
               'mode': mode, 'mode_text': MODE_TEXT[mode], 'raw': image[off:off + rlen].hex() if off + rlen <= len(image) else None,
               'source': result['source'], 'firmware_status': result['status'], 'checks': result['checks'],
               'meaning': meaning, 'confidence': confidence, 'value': None}
        if sel is not None:
            row['value_hex'] = sel.hex()
            row['value'] = int.from_bytes(sel, 'big') if len(sel) <= 4 else None
        if item == 4 and sel is not None:
            ident = int.from_bytes(sel, 'big')
            row['decoded'] = {'board_id': f'0x{ident:04x}', 'M': (ident >> 12) & 0xF, 'F': (ident >> 5) & 0x7F, 'N': ident & 0x1F,
                              'missing_warning': ident == 0x1F}
        if item == 5 and sel is not None:
            text = sel.split(b'\0', 1)[0].decode('latin-1')
            row['decoded'] = {'serial': text if show_ids else mask(text), 'length': len(text), 'identifying': True}
            row['raw'] = row['raw'] if show_ids else '(redacted; use --show-identifiers)'
            row['value_hex'] = row['value_hex'] if show_ids else '(redacted)'
        if item == 0 and sel is not None:
            row['decoded'] = {'map_revision': sel[0] >> 4, 'low_nibble': sel[0] & 0xF}
        if item == 12 and sel is not None and len(sel) == 4:
            row['decoded'] = {'signed': int.from_bytes(sel, 'big', signed=True)}
        items.append(row)
    named = {item[1] for item in NVM_ITEMS}
    region_rows = []
    for region, off, length in table:
        name, why = REGION_NAMES.get(region, ('item %s' % ','.join(str(i[0]) for i in NVM_ITEMS if i[1] == region)
                                               if region in named else 'no boot item; purpose unknown',
                                               'descriptor table 0xa0011924' if region in named else 'unknown'))
        raw = image[off:off + length].hex() if region != 0x7 and off + length <= len(image) else None
        if region == 0xD and not show_ids:
            raw = '(redacted)'
        region_rows.append({'region': f'0x{region:x}', 'offset': f'0x{off:04x}', 'length': length,
                            'name': name, 'evidence': why, 'raw': raw})
    return {'layout_byte': f'0x{image[0]:02x}', 'layout_table': LAYOUT_ADDRESSES[version], 'map_revision': image[0] >> 4,
            'regions': region_rows, 'items': items, 'error_log': decode_error_log(image, regions),
            'nvm3_region': regions[0x7]}


def mask(text: str, keep: int = 4) -> str:
    return '*' * max(0, len(text) - keep) + text[-keep:]


# ---------------------------------------------------------------------------
# Prefix code (decoder 0xa02f3030..0xa02f336a, encoder 0xa02f2d7c..0xa02f2fec).
# ---------------------------------------------------------------------------
def token(buf: bytes, i: int) -> tuple[str, int, int | bytes | None, int]:
    """Return (kind, total length, value, header length). Kinds: A delta, B int, C bytes."""
    x = buf[i]
    if x == 0xFF:
        return 'END', 1, None, 1
    if x == 0xFE:
        n = int.from_bytes(buf[i + 1:i + 3], 'big')
        return 'C', 3 + n, bytes(buf[i + 3:i + 3 + n]), 3
    if x & 0xFC == 0xFC:
        return 'A', 3, (((x & 1) << 16) | int.from_bytes(buf[i + 1:i + 3], 'big')) + 1, 3
    if x & 0xFC == 0xF8:
        n = ((x & 3) << 8) | buf[i + 1]
        return 'C', 2 + n, bytes(buf[i + 2:i + 2 + n]), 2
    if x & 0xF0 == 0xF0:
        n = (x & 7) + 5
        return 'C', 1 + n, bytes(buf[i + 1:i + 1 + n]), 1
    if x & 0xE0 == 0xE0:
        return 'A', 2, (((x & 0xF) << 8) | buf[i + 1]) + 1, 2
    if x & 0xD0 == 0xD0:
        return 'B', 2, ((x & 0xF) << 8) | buf[i + 1], 1
    if x & 0xC8 == 0xC8:
        return 'B', 3, ((x & 7) << 16) | int.from_bytes(buf[i + 1:i + 3], 'big'), 1
    if x == 0xC7:
        return 'B', 5, int.from_bytes(buf[i + 1:i + 5], 'big'), 1
    if x & 0xC0 == 0xC0:
        n = x & 7
        return 'C', 1 + n, bytes(buf[i + 1:i + 1 + n]), 1
    if x & 0x80:
        return 'B', 1, x & 0x3F, 1
    if x & 0x40:
        return 'A', 1, (x & 0x3F) + 1, 1
    return 'BAD', 1, x, 1


B_BITS = {1: 6, 2: 12, 3: 19, 5: 32}


def sign_extend(value: int, bits: int) -> int:
    return value - (1 << bits) if bits < 33 and value >> (bits - 1) & 1 else value


def b_range(raw: bytes, signed: bool | None) -> tuple[int, int]:
    """Plaintext bounds of a multi-byte B token whose tail is encrypted (encoder 0xa02f2d7c)."""
    n, hi = len(raw), raw[0]
    if n == 2:
        top = (hi & 0xF) << 8
        lo, high = top, top | 0xFF
        if signed:
            lo, high = sign_extend(lo, 12), sign_extend(high, 12)
        else:
            lo = max(lo, 0x20)
    elif n == 3:
        top = (hi & 7) << 16
        lo, high = top, top | 0xFFFF
        if signed:
            lo, high = sign_extend(lo, 19), sign_extend(high, 19)
        else:
            lo = max(lo, 0x800)
    else:
        lo, high = (-(1 << 31), (1 << 31) - 1) if signed else (0x40000, 0xFFFFFFFF)
    return min(lo, high), max(lo, high)


# ---------------------------------------------------------------------------
# NVM3 pages, holes and the object stream.
# ---------------------------------------------------------------------------
NVM3_BASE = 0xB2
PAGE_MAP = 0xBF
HOLE_BITMAP = 0x1BC
JOURNAL_MAP = 0x1DC
PAGE_BASE = 0x2FF
PAGE_SIZE = 256
PAGE_COUNT = 253
JOURNAL_PAGES = 60


def page_bytes(image: bytes, page: int) -> bytes:
    return image[PAGE_BASE + page * PAGE_SIZE:PAGE_BASE + (page + 1) * PAGE_SIZE]


def hole_chain(page: bytes) -> tuple[list[tuple[int, int]], str]:
    """Holes chained back from the last byte (0xa02f138a, 0xa02f1346, 0xa02f1370)."""
    pos, holes = PAGE_SIZE - 1, []
    for _ in range(PAGE_SIZE):
        marker = page[pos]
        if marker == 0xC0:
            size = 1
        elif marker in (0xC1, 0xC9):
            size = 2
        elif marker in (0xC2, 0xCA):
            size = page[pos - 2]
        else:
            return holes, f'no hole descriptor at page offset 0x{pos:02x} (0x{marker:02x})'
        holes.append((pos - size + 1, size))
        if marker in (0xC0, 0xC9, 0xCA):
            return holes, 'ok'
        pos = page[pos - 1]
    return holes, 'loop'


def decode_nvm3(image: bytes) -> dict:
    out = {'base': f'0x{NVM3_BASE:04x}', 'available': len(image) >= EEPROM_SIZE}
    meta = image[NVM3_BASE:PAGE_BASE]
    out['header_raw'] = image[NVM3_BASE:PAGE_MAP].hex()
    if len(image) > NVM3_BASE + 5:
        out['header_word_0xb3'] = int.from_bytes(image[0xB3:0xB7], 'big')
    used_map = [p for p in image[PAGE_MAP:min(HOLE_BITMAP, len(image))] if p != 0xFF]
    out['page_map_partial'] = len(image) < HOLE_BITMAP
    out['page_map'] = used_map
    if len(image) < EEPROM_SIZE:
        out['note'] = f'image has {len(image)} bytes; pages need the full 64 KiB'
        return out
    bitmap = image[HOLE_BITMAP:HOLE_BITMAP + 32]
    journal = list(image[JOURNAL_MAP:JOURNAL_MAP + JOURNAL_PAGES])
    out.update(metadata_bytes=len(meta), page_base=f'0x{PAGE_BASE:04x}', page_size=PAGE_SIZE, pages=PAGE_COUNT,
               journal_pages=JOURNAL_PAGES, journal_map_is_identity=journal == list(range(JOURNAL_PAGES)),
               data_pages_mapped=len(used_map), free_pages=PAGE_COUNT - JOURNAL_PAGES - len(used_map))
    stream, sources, holes_total, page_rows = bytearray(), [], 0, []
    for logical, page in enumerate(used_map):
        data = page_bytes(image, page)
        has_holes = bool(bitmap[page // 8] >> (page % 8) & 1)
        holes, status = hole_chain(data) if has_holes else ([], 'full page (bitmap bit clear)')
        mask_ = bytearray(PAGE_SIZE)
        for start, size in holes:
            mask_[start:start + size] = b'\1' * size
        live = 0
        for k in range(PAGE_SIZE):
            if not mask_[k]:
                stream.append(data[k])
                sources.append(PAGE_BASE + page * PAGE_SIZE + k)
                live += 1
        holes_total += sum(size for _, size in holes)
        page_rows.append({'logical': logical, 'physical': page, 'offset': f'0x{PAGE_BASE + page * PAGE_SIZE:04x}',
                          'holes': holes, 'status': status, 'live_bytes': live})
    out.update(holes_total=holes_total, stream_bytes=len(stream), page_list=page_rows)
    objects, errors = parse_stream(bytes(stream), sources)
    out['objects'] = objects
    out['stream_errors'] = errors
    return out


def parse_stream(stream: bytes, sources: list[int]) -> tuple[list[dict], list[str]]:
    """Walk the DSID-sorted stream (reader 0xa02f261c): raw first DSID, then A deltas."""
    if len(stream) < 4:
        return [], ['stream shorter than the initial DSID']
    dsid, i, objects, errors = int.from_bytes(stream[:4], 'big'), 4, [], []
    current = None
    while i < len(stream):
        kind, length, value, header = token(stream, i)
        if i + length > len(stream):
            errors.append(f'truncated token at stream offset {i}')
            break
        if kind == 'A':
            dsid += value
            current = None
        elif kind in ('B', 'C'):
            if current is None or current['dsid'] != dsid:
                current = {'dsid': dsid, 'offset': f'0x{sources[i]:04x}', 'tokens': []}
                objects.append(current)
            current['tokens'].append({'kind': kind, 'raw': stream[i:i + length], 'header': header})
        else:
            errors.append(f'unexpected byte 0x{stream[i]:02x} at stream offset {i} (EEPROM 0x{sources[i]:04x})')
            break
        i += length
    return objects, errors


# ---------------------------------------------------------------------------
# Value keystream (0xa02f348c key setup, 0xa0044e54 KDF, 0xa02f34ee keystream).
# ---------------------------------------------------------------------------
def _aes_sbox() -> list[int]:
    sbox, p, q = [0] * 256, 1, 1
    while True:
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        rot = lambda v, s: ((v << s) | (v >> (8 - s))) & 0xFF
        sbox[p] = q ^ rot(q, 1) ^ rot(q, 2) ^ rot(q, 3) ^ rot(q, 4) ^ 0x63
        if p == 1:
            break
    sbox[0] = 0x63
    return sbox


_SBOX = _aes_sbox()


def _xtime(b: int) -> int:
    return ((b << 1) ^ 0x1B) & 0xFF if b & 0x80 else b << 1


class AES128:
    """Minimal AES-128 block encryption (FIPS-197); only what the keystream needs."""

    def __init__(self, key: bytes):
        if len(key) != 16:
            raise ValueError('AES-128 key must be 16 bytes')
        words = [list(key[i:i + 4]) for i in range(0, 16, 4)]
        rcon = 1
        for i in range(4, 44):
            t = list(words[i - 1])
            if i % 4 == 0:
                t = [_SBOX[b] for b in t[1:] + t[:1]]
                t[0] ^= rcon
                rcon = _xtime(rcon)
            words.append([a ^ b for a, b in zip(words[i - 4], t)])
        self.round_keys = [sum(words[4 * r:4 * r + 4], []) for r in range(11)]

    def encrypt(self, block: bytes) -> bytes:
        s = [a ^ b for a, b in zip(block, self.round_keys[0])]
        for rnd in range(1, 11):
            s = [_SBOX[b] for b in s]
            s = [s[(i + 4 * (i % 4)) % 16] for i in range(16)]  # ShiftRows (column-major)
            if rnd != 10:
                mixed = []
                for c in range(4):
                    a = s[4 * c:4 * c + 4]
                    t = a[0] ^ a[1] ^ a[2] ^ a[3]
                    mixed += [a[j] ^ t ^ _xtime(a[j] ^ a[(j + 1) % 4]) for j in range(4)]
                s = mixed
            s = [a ^ b for a, b in zip(s, self.round_keys[rnd])]
        return bytes(s)


def key_from_chip_id(words: list[int]) -> bytes:
    """SHA-256(BE16 0x0a94 | BE16 0x0100 | id[0x1c] id[0x18] id[0x14] id[0x10])[:16] (0xa0044e54)."""
    if len(words) != 4:
        raise ValueError('expected the four words at 0xbd040010, 0xbd040014, 0xbd040018, 0xbd04001c')
    secret = b''.join(w.to_bytes(4, 'big') for w in reversed(words))
    return hashlib.sha256(b'\x0a\x94\x01\x00' + secret).digest()[:16]


def keystream(cipher: AES128, dsid: int, length: int) -> bytes:
    blocks = (length + 15) // 16
    data = b''.join(cipher.encrypt(dsid.to_bytes(4, 'big') + bytes(10) + k.to_bytes(2, 'big'))
                    for k in range(blocks))
    return data[:length]


# ---------------------------------------------------------------------------
# Catalog (names/types) from the reference captures; optional.
# ---------------------------------------------------------------------------
IDENTIFYING = re.compile(r'SERIAL|MAC_ADDR|HOST_?NAME|UUID|UDN|SSID|PASS|PSK|KEY|CERT|EMAIL|ACCOUNT|'
                         r'DEVICE_ID|CLOUD|OWNER|LOCATION|CONTACT|BONJOUR|MDNS|IP_ADDR|PIN|DISPLAY_NAME|ADAPTOR_CONFIG|'
                         r'PROTOCOL_CONFIG|ACCESS_LIST|UID', re.I)


def load_catalog(path: Path | None) -> dict[int, dict]:
    if path is None or not path.exists():
        return {}
    catalog = json.loads(path.read_text())
    out = {}
    for obj in catalog['objects']:
        out[obj['dsid']] = {'name': obj.get('name'), 'nvm_type': obj.get('nvm_type'), 'file_id': obj.get('file_id'),
                            'byte_width': obj.get('byte_width'),
                            'element_bytes': obj.get('bytes_per_element_from_get_all'),
                            'element_count': obj.get('element_count_from_get_all')}
    return out


def unit_hint(name: str | None) -> str | None:
    if not name:
        return None
    for pattern, unit in (('_DIV_256', 'x256 (name suffix _DIV_256)'), ('_SEC$', 'seconds'),
                          ('PAGE_COUNT|_PAGES_|IMPRESSIONS', 'pages'), ('_COUNT$|_COUNTER', 'count'),
                          ('TEMP', 'temperature (unit unknown)')):
        if re.search(pattern, name):
            return unit
    return None


def describe_objects(nvm3: dict, catalog: dict, cipher: AES128 | None, show_ids: bool) -> dict:
    stats = {'objects': 0, 'tokens': 0, 'exact_single_byte': 0, 'encrypted_without_key': 0,
             'decrypted': 0, 'class_violations_after_decrypt': 0, 'named': 0}
    rows = []
    for obj in nvm3.get('objects', []):
        meta = catalog.get(obj['dsid'], {})
        nvm_type = meta.get('nvm_type') or ''
        signed = None if not nvm_type else nvm_type.endswith('_SIGNED') and 'UNSIGNED' not in nvm_type
        name = meta.get('name')
        identifying = name is None or bool(IDENTIFYING.search(name))  # unnamed: redact conservatively
        kinds = {t['kind'] for t in obj['tokens']}
        category = ('record array' if kinds == {'C'} else
                    'simple array' if len(obj['tokens']) > 1 else 'scalar') if not nvm_type else nvm_type
        stats['objects'] += 1
        stats['named'] += bool(name)
        values = []
        elem_bytes = meta.get('element_bytes') or (meta.get('byte_width') if 'ARRAY' in nvm_type else None)
        for index, tok in enumerate(obj['tokens']):
            stats['tokens'] += 1
            raw = tok['raw']
            entry = {'index': index, 'stored_hex': raw.hex()}
            if tok['kind'] == 'B':
                bits = B_BITS[len(raw)]
                entry['bits'] = bits
                if len(raw) == 1:
                    value = raw[0] & 0x3F
                    entry['value'] = sign_extend(value, 6) if signed else value
                    entry['status'] = 'exact (single-byte token, not encrypted)'
                    stats['exact_single_byte'] += 1
                elif cipher is not None:
                    off = index * (elem_bytes or 4) if 'ARRAY' in nvm_type else 0
                    ks = keystream(cipher, obj['dsid'], off + len(raw) - 1)[off:]
                    plain = raw[:1] + bytes(a ^ b for a, b in zip(raw[1:], ks))
                    _, _, value, _ = token(plain, 0)
                    lo, hi = b_range(raw, signed)
                    value = sign_extend(value, bits) if signed else value
                    entry['value'] = value
                    entry['status'] = 'decrypted (unverified key)'
                    stats['decrypted'] += 1
                    if not lo <= value <= hi:
                        stats['class_violations_after_decrypt'] += 1
                        entry['status'] += '; outside the encoder range for this token size'
                else:
                    lo, hi = b_range(raw, signed)
                    entry['value'] = None
                    entry['range'] = [lo, hi]
                    entry['status'] = 'encrypted; plaintext bounds from the clear header byte'
                    stats['encrypted_without_key'] += 1
            else:
                payload = raw[tok['header']:]
                entry['stored_length'] = len(payload)
                entry['record_bytes'] = elem_bytes
                if cipher is not None:
                    off = index * (elem_bytes or 0)
                    ks = keystream(cipher, obj['dsid'], off + len(payload))[off:]
                    plain = bytes(a ^ b for a, b in zip(payload, ks))
                    if elem_bytes and len(plain) < elem_bytes and plain:  # 0xa02f329e pads with last byte
                        plain += plain[-1:] * (elem_bytes - len(plain))
                    stats['decrypted'] += 1
                    if identifying and not show_ids:
                        entry['status'] = 'decrypted; redacted (identifying name)'
                    else:
                        entry['bytes_hex'] = plain.hex()
                        text = plain.split(b'\0', 1)[0]
                        if text and all(32 <= c < 127 for c in text):
                            entry['text'] = text.decode('ascii')
                        entry['status'] = 'decrypted (unverified key)'
                        if index and not elem_bytes:
                            entry['status'] += '; record size unknown, keystream offset assumed 0'
                else:
                    entry['status'] = 'encrypted record payload (length is clear)'
                    stats['encrypted_without_key'] += 1
            values.append(entry)
        rows.append({'dsid': obj['dsid'], 'name': name, 'nvm_type': nvm_type or None, 'category': category,
                     'file_id': meta.get('file_id'), 'offset': obj['offset'], 'elements': len(values),
                     'identifying_name': identifying, 'unit_hint': unit_hint(name), 'values': values})
    return {'stats': stats, 'objects': rows}


# ---------------------------------------------------------------------------
# Cross-checks against HP's own command output (references/).
# ---------------------------------------------------------------------------
def encoder_header(value: int, signed: bool) -> tuple[int, int]:
    """(token length, first byte) chosen by the encoder (0xa02f2d7c + 0xa02f2e92)."""
    if signed:
        if -8 <= value < 32:
            return 1, 0x80 | (value & 0x3F)
        if -2048 <= value < 2048:
            return 2, 0xD0 | ((value >> 8) & 0xF)
        if -0x40000 <= value < 0x40000:
            return 3, 0xC8 | ((value >> 16) & 7)
        return 5, 0xC7
    value &= 0xFFFFFFFF
    if value <= 0x1F:
        return 1, 0x80 | value
    if value <= 0x7FF:
        return 2, 0xD0 | (value >> 8)
    if value <= 0x3FFFF:
        return 3, 0xC8 | (value >> 16)
    return 5, 0xC7


def crosscheck(objects: list[dict], refs: Path) -> dict:
    by_dsid = {o['dsid']: o for o in objects}
    result = {}
    scalars_path = refs / 'physical-storage-scalars.json'
    if scalars_path.exists():
        scalars = json.loads(scalars_path.read_text())['values']
        exact_ok, exact_bad, header_ok, header_diff, missing = 0, [], 0, [], []
        for item in scalars:
            obj = by_dsid.get(item['dsid'])
            if not obj or len(obj['tokens']) != 1 or obj['tokens'][0]['kind'] != 'B':
                missing.append(item['dsid'])
                continue
            raw = obj['tokens'][0]['raw']
            signed = item['nvm_type'] == 'NVM_SIM_SIGNED'
            length, first = encoder_header(item['nvm_value'], signed)
            if len(raw) == 1:
                stored = sign_extend(raw[0] & 0x3F, 6) if signed else raw[0] & 0x3F
                if stored == item['nvm_value']:
                    exact_ok += 1
                else:
                    exact_bad.append({'dsid': item['dsid'], 'name': item['name'], 'eeprom': stored, 'hp': item['nvm_value']})
            elif length == len(raw) and first == raw[0]:
                header_ok += 1
            else:
                header_diff.append({'dsid': item['dsid'], 'name': item['name'], 'hp_value': item['nvm_value'],
                                    'stored_token': raw.hex()})
        result['scalars'] = {'hp_values': len(scalars), 'single_byte_exact_match': exact_ok,
                             'single_byte_mismatch': exact_bad,
                             'multi_byte_clear_header_consistent': header_ok,
                             'inconsistent_with_hp_value': header_diff, 'not_found_as_scalar': missing}
    records_dir = refs / 'physical-storage-records'
    if records_dir.is_dir():
        ok, diff, absent = 0, [], []
        for path in sorted(records_dir.glob('*.bin')):
            dsid, index = map(int, path.stem.split('-'))
            record = path.read_bytes()
            obj = by_dsid.get(dsid)
            if not obj or index >= len(obj['tokens']):
                absent.append(path.name)
                continue
            tok = obj['tokens'][index]
            stored = len(tok['raw']) - tok['header']
            trimmed = len(record)
            while trimmed > 1 and record[trimmed - 1] == record[trimmed - 2]:
                trimmed -= 1
            expected = max(trimmed, min(len(record), 8))  # empirical length rule
            if stored == expected:
                ok += 1
            else:
                diff.append({'record': path.name, 'stored_length': stored, 'expected_length': expected})
        result['records'] = {'hp_records': ok + len(diff) + len(absent), 'length_rule_match': ok,
                             'length_rule_mismatch': diff, 'absent': absent,
                             'rule': 'stored = max(len after trimming trailing repeats of the last byte, min(record size, 8)); empirical'}
    arrays_path = refs / 'physical-storage-arrays.json'
    if arrays_path.exists():
        ok, diff = 0, []
        for array in json.loads(arrays_path.read_text())['arrays']:
            obj = by_dsid.get(array['dsid'])
            stored = len(obj['tokens']) if obj else 0
            if stored == array['element_count']:
                ok += 1
            else:
                diff.append({'dsid': array['dsid'], 'name': array['name'], 'stored_elements': stored,
                             'hp_element_count': array['element_count']})
        result['arrays'] = {'element_count_match': ok, 'element_count_mismatch': diff}
    return result


# ---------------------------------------------------------------------------
def decode(image: bytes, catalog_path: Path | None = None, cipher: AES128 | None = None,
           show_ids: bool = False, refs: Path | None = None) -> dict:
    if not image:
        raise ValueError('empty image')
    if len(image) > EEPROM_SIZE:
        raise ValueError('image larger than the 64 KiB M24512')
    result = {'size': len(image), 'sha256': hashlib.sha256(image).hexdigest(),
              'kind': 'full 64 KiB image' if len(image) == EEPROM_SIZE else f'partial image ({len(image)} bytes)',
              'erased': image == b'\xff' * len(image)}
    if result['erased']:
        result['note'] = 'all bytes are 0xff (erased part); nothing to decode'
        return result
    result['boot'] = decode_boot(image, show_ids)
    nvm3 = decode_nvm3(image)
    catalog = load_catalog(catalog_path)
    result['catalog'] = str(catalog_path) if catalog else None
    result['key'] = 'supplied (decryption unverified on hardware)' if cipher else 'not supplied'
    if 'objects' in nvm3:
        if refs is not None:
            result['crosscheck'] = crosscheck(nvm3['objects'], refs)
        described = describe_objects(nvm3, catalog, cipher, show_ids)
        if cipher is not None and refs is not None and (refs / 'physical-storage-scalars.json').exists():
            # Strong key test: HP's own values for multi-byte scalars (the header-byte
            # range check in describe_objects alone cannot reject most wrong keys).
            hp = {v['dsid']: v['nvm_value'] for v in
                  json.loads((refs / 'physical-storage-scalars.json').read_text())['values']}
            equal = total = 0
            for obj in described['objects']:
                vals = obj['values']
                if obj['dsid'] in hp and len(vals) == 1 and vals[0].get('bits', 6) > 6:
                    total += 1
                    equal += (vals[0]['value'] or 0) & 0xFFFFFFFF == hp[obj['dsid']] & 0xFFFFFFFF
            result['key_check'] = {'multi_byte_scalars_compared': total, 'equal_to_hp_value': equal,
                                   'verdict': 'key plausible' if total and equal >= 0.8 * total else 'key rejected'}
        nvm3['objects'] = described['objects']
        nvm3['value_stats'] = described['stats']
        dsids = [o['dsid'] for o in described['objects']]
        nvm3['dsid_range'] = [min(dsids), max(dsids)] if dsids else None
        nvm3['dsids_strictly_ascending'] = all(a < b for a, b in zip(dsids, dsids[1:]))
        if catalog:
            nvm3['catalog_objects_absent'] = sorted(set(catalog) - set(dsids))
            nvm3['stream_objects_not_in_catalog'] = sorted(set(dsids) - set(catalog))
    result['nvm3'] = nvm3
    return result


def _fmt_value(entry: dict) -> str:
    if entry.get('value') is not None:
        return str(entry['value'])
    if 'range' in entry:
        lo, hi = entry['range']
        return f'[{lo}..{hi}] enc'
    if 'text' in entry:
        return repr(entry['text'])
    if 'bytes_hex' in entry:
        return entry['bytes_hex'][:48] + ('...' if len(entry['bytes_hex']) > 48 else '')
    if 'stored_length' in entry:
        return f'<{entry["stored_length"]} B enc>'
    return entry.get('status', '?')


def print_text(result: dict, objects: bool) -> None:
    print(f"NVM image: {result['kind']}, sha256 {result['sha256']}")
    if result.get('erased'):
        print(result['note'])
        return
    boot = result['boot']
    print(f"\nBoot (NOS) region 0x0000-0x00b1: layout byte {boot['layout_byte']} -> map revision "
          f"{boot['map_revision']}, region table {boot['layout_table']}")
    for row in boot['regions']:
        raw = row['raw'] if row['raw'] is None or len(row['raw']) <= 32 else row['raw'][:32] + '...'
        print(f"  region {row['region']:>4} @{row['offset']} len {row['length']:5}  {row['name']}"
              + (f"  [{raw}]" if raw else ''))
    print('  items (descriptor table 0xa0011924, reader 0xa000c9f6):')
    for item in boot['items']:
        shown = item.get('decoded') or item.get('value_hex')
        print(f"    item {item['item']:2} @{item['offset']} {item['mode_text']:<44} {item['source']:<30} "
              f"status {item['firmware_status']}: {shown}  -- {item['meaning']}")
    log = boot.get('error_log')
    if log:
        print(f"  hardware error log: {log['slots']} x {log['entry_size']} bytes, ring index {log['ring_index']}, "
              f"checksum {'ok' if log['checksum_ok'] else 'BAD'}")
        for e in log['entries']:
            print(f"    entry {e['entry']} (slot {e['slot']} @{e['offset']}): assert {e['assert_id']} line {e['line']} "
                  f"file {e['file'] or e['file_pointer']} page count {e.get('page_count')}")
    nvm3 = result['nvm3']
    print(f"\nNVM3 store {nvm3['base']}-0xffff:")
    if not nvm3['available']:
        print(f"  {nvm3.get('note')}; header {nvm3['header_raw']}; page map prefix {nvm3['page_map']}")
        return
    print(f"  metadata {nvm3['metadata_bytes']} bytes; header {nvm3['header_raw']}; {nvm3['pages']} pages x "
          f"{nvm3['page_size']} at {nvm3['page_base']}: {nvm3['journal_pages']} journal, {nvm3['data_pages_mapped']} "
          f"mapped data, {nvm3['free_pages']} free; holes {nvm3['holes_total']} bytes")
    print(f"  logical stream {nvm3['stream_bytes']} bytes, {len(nvm3['objects'])} objects, DSIDs "
          f"{nvm3['dsid_range']} ascending={nvm3['dsids_strictly_ascending']}, errors {nvm3['stream_errors'] or 'none'}")
    s = nvm3['value_stats']
    print(f"  values: {s['tokens']} tokens; {s['exact_single_byte']} exact single-byte; "
          f"{s['encrypted_without_key']} encrypted (key {result['key']}); {s['decrypted']} decrypted; "
          f"{s['named']} objects named from {result['catalog']}")
    if s['decrypted']:
        print(f"  decrypted values outside their token's encoder range: {s['class_violations_after_decrypt']} "
              f"(weak test: most wrong keys pass it)")
    if 'key_check' in result:
        k = result['key_check']
        print(f"  key check vs HP values: {k['equal_to_hp_value']}/{k['multi_byte_scalars_compared']} multi-byte "
              f"scalars equal -> {k['verdict']}")
    if 'crosscheck' in result:
        cc = result['crosscheck']
        if 'scalars' in cc:
            c = cc['scalars']
            print(f"  cross-check vs HP scalars: {c['single_byte_exact_match']} exact, {len(c['single_byte_mismatch'])} "
                  f"mismatched single-byte; {c['multi_byte_clear_header_consistent']} multi-byte consistent, "
                  f"{len(c['inconsistent_with_hp_value'])} inconsistent (HP values read 75 min before the mirror)")
        if 'records' in cc:
            c = cc['records']
            print(f"  cross-check vs HP records: length rule {c['length_rule_match']}/{c['hp_records']}")
        if 'arrays' in cc:
            c = cc['arrays']
            print(f"  cross-check vs HP arrays: element counts {c['element_count_match']} match, "
                  f"{len(c['element_count_mismatch'])} differ")
    if objects:
        print('\n  DSID   name                                              type                     values')
        for obj in nvm3['objects']:
            vals = ', '.join(_fmt_value(v) for v in obj['values'][:6]) + (' ...' if len(obj['values']) > 6 else '')
            print(f"  {obj['dsid']:6} {(obj['name'] or '?')[:48]:<49} {str(obj['nvm_type'] or obj['category'])[:24]:<24} {vals}")


def _json_default(value):
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    raise TypeError(type(value))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('image', type=Path)
    parser.add_argument('--json', action='store_true', help='print the full structured decode')
    parser.add_argument('--objects', action='store_true', help='list every NVM3 object in text mode')
    parser.add_argument('--catalog', type=Path, default=ROOT / 'references/physical-nvm-catalog.json',
                        help='DSID name/type catalog (default: the reference capture, if present)')
    parser.add_argument('--crosscheck', nargs='?', const=ROOT / 'references', type=Path,
                        help='compare with HP command captures in this directory (default references/)')
    parser.add_argument('--chip-id', help='four hex words read at 0xbd040010, 0x14, 0x18, 0x1c (e.g. "md 0xbd040010 4")')
    parser.add_argument('--nvm-key', help='16-byte AES key as hex, instead of --chip-id')
    parser.add_argument('--show-identifiers', action='store_true', help='do not redact serial numbers and similar')
    args = parser.parse_args()
    try:
        image = args.image.read_bytes()
        cipher = None
        if args.chip_id:
            words = [int(w, 16) for w in re.split(r'[\s,]+', args.chip_id.strip()) if w]
            cipher = AES128(key_from_chip_id(words))
        elif args.nvm_key:
            cipher = AES128(bytes.fromhex(args.nvm_key))
        result = decode(image, args.catalog, cipher, args.show_identifiers, args.crosscheck)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if args.json:
        print(json.dumps(result, indent=1, default=_json_default))
    else:
        print_text(result, args.objects)


if __name__ == '__main__':
    main()
