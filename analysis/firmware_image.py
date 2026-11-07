"""Read-only section/scatter-table inspector for the supplied AXON flash image.

The application header and linked section records describe both ROM storage
and RAM destinations. Extraction is for analysis; the production emulator
continues to execute the firmware's own loader and decompressor.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import struct


import ctypes

_NATIVE_LZSS = None
_NATIVE_LZSS_CHECKED = False


def _get_native_lzss():
    global _NATIVE_LZSS, _NATIVE_LZSS_CHECKED
    if not _NATIVE_LZSS_CHECKED:
        _NATIVE_LZSS_CHECKED = True
        try:
            from native_observer import _get_native_library
            lib = _get_native_library()
            _NATIVE_LZSS = getattr(lib, 'native_unpack_lzss', None)
        except Exception:
            _NATIVE_LZSS = None
    return _NATIVE_LZSS


class ImageError(ValueError):
    pass


def unpack_lzss(packed: bytes, expected_size: int) -> bytes:
    """Decode the 4 KiB-window format at boot routine 0x3100, with bounds checks."""
    if not 0 <= expected_size <= 0x4000000:
        raise ImageError('expanded section size is outside the 64 MiB analysis limit')
    native_fn = _get_native_lzss()
    if native_fn is not None:
        packed_bytes = packed if type(packed) is bytes else bytes(packed)
        out_buf = ctypes.create_string_buffer(max(1, expected_size))
        rc = native_fn(packed_bytes, len(packed_bytes), out_buf, expected_size)
        if rc == -5:
            return out_buf.raw[:expected_size]
        if rc == -1:
            raise ImageError('LZSS output exceeds the declared destination section')
        if rc == -2:
            raise ImageError('truncated LZSS token')
        if rc == -3:
            raise ImageError('truncated LZSS back-reference')
        raise ImageError(f'LZSS size mismatch: {rc:#x}, expected {expected_size:#x}')
    output = bytearray()
    out_append = output.append
    out_extend = output.extend
    packed_len = len(packed)
    cursor = flags = 0
    fast_in_limit = packed_len - 18
    fast_out_limit = expected_size - 144
    fast_limit8 = packed_len - 8
    fast_limit4 = packed_len - 4
    while cursor < packed_len:
        if not (flags & 0x100) and cursor <= fast_in_limit:
            out_len = len(output)
            if 0x1000 <= out_len <= fast_out_limit:
                while cursor <= fast_in_limit and out_len <= fast_out_limit:
                    fb = packed[cursor]
                    cursor += 1
                    if fb == 0xFF:
                        out_extend(packed[cursor : cursor + 8])
                        cursor += 8
                        out_len += 8
                        continue
                    for bit in (1, 2, 4, 8, 16, 32, 64, 128):
                        if fb & bit:
                            out_append(packed[cursor])
                            cursor += 1
                            out_len += 1
                        else:
                            second = packed[cursor + 1]
                            source = out_len - (((out_len + 0xFED - (packed[cursor] | ((second & 0xF0) << 4))) & 0xFFF) + 1)
                            length = (second & 0x0F) + 3
                            cursor += 2
                            if source + length <= out_len:
                                out_extend(output[source : source + length])
                            else:
                                for _ in range(length):
                                    out_append(output[source])
                                    source += 1
                            out_len += length
                if cursor >= packed_len:
                    break
        flags >>= 1
        if not (flags & 0x100):
            if len(output) > expected_size:
                raise ImageError('LZSS output exceeds the declared destination section')
            fb = packed[cursor]
            cursor += 1
            if fb == 0xFF and cursor <= fast_limit8:
                out_extend(packed[cursor : cursor + 8])
                cursor += 8
                continue
            flags = 0xFF00 | fb
            if cursor >= packed_len:
                raise ImageError('truncated LZSS token')
        if flags & 1:
            if (flags & 0x80F) == 0x80F and cursor <= fast_limit4:
                out_extend(packed[cursor : cursor + 4])
                cursor += 4
                flags >>= 3
                continue
            if cursor >= packed_len:
                raise ImageError('truncated LZSS token')
            out_append(packed[cursor])
            cursor += 1
        else:
            if cursor + 1 >= packed_len:
                raise ImageError('truncated LZSS token' if cursor >= packed_len else 'truncated LZSS back-reference')
            second = packed[cursor + 1]
            offset = packed[cursor] | ((second & 0xF0) << 4)
            length = (second & 0x0F) + 3
            cursor += 2
            out_len = len(output)
            source = out_len - (((out_len + 0xFED - offset) & 0xFFF) + 1)
            if source >= 0 and source + length <= out_len:
                out_extend(output[source : source + length])
            else:
                for _ in range(length):
                    out_append(output[source] if source >= 0 else 0)
                    source += 1
            if out_len + length > expected_size:
                raise ImageError('LZSS output exceeds the declared destination section')
    out_len = len(output)
    if out_len > expected_size:
        raise ImageError('LZSS output exceeds the declared destination section')
    if out_len != expected_size:
        raise ImageError(f'LZSS size mismatch: {out_len:#x}, expected {expected_size:#x}')
    return bytes(output)


@dataclass(frozen=True)
class Section:
    descriptor: int
    name: str
    address: int
    size: int
    flags: int
    related_section: int


@dataclass(frozen=True)
class LoadOperation:
    table_entry: int
    kind: str
    destination: int
    source: int
    stored_size: int
    expanded_size: int
    section: str
    source_section: str | None


class FirmwareImage:
    def __init__(self, image: bytes, header_offset: int = 0x20030):
        self._mv = memoryview(image)
        self._len = len(self._mv)
        self.header_offset = header_offset
        self._check_range(header_offset, 0x6C)
        words = struct.unpack_from('>27I', self._mv, header_offset)
        if words[1] != 0x3CA55A3C or words[2] != 0x6C:
            raise ImageError('application header magic or size does not match this layout')
        if words[15] != header_offset:
            raise ImageError('application header self-pointer does not match its location')
        self.entry = words[14]
        self.support_word = words[3]
        self.sections = self._sections(words[17])
        self.by_name = {section.name: section for section in self.sections}
        if len(self.by_name) != len(self.sections):
            raise ImageError('duplicate section names')
        self.by_descriptor = {section.descriptor: section for section in self.sections}
        self.operations = tuple(self._operations('zero', words[18], words[20]) +
                                self._operations('copy', words[21], words[22]) +
                                self._operations('lzss', words[24], words[25]))
        self.image = image if type(image) is bytes else bytes(image)
        self._mv = memoryview(self.image)
        self._decoded: dict[str, bytes] = {}

    def _check_range(self, offset: int, size: int) -> None:
        if offset < 0 or size < 0 or offset + size > self._len:
            raise ImageError(f'image range is outside the dump: {offset:#x}+{size:#x}')

    def _slice(self, offset: int, size: int) -> bytes:
        self._check_range(offset, size)
        return self.image[offset:offset + size]

    def _sections(self, pointer: int) -> tuple[Section, ...]:
        sections, seen = [], set()
        mv = self._mv
        img_len = self._len
        while pointer:
            if pointer in seen or len(seen) >= 1024:
                raise ImageError('section list is cyclic or exceeds the analysis limit')
            seen.add(pointer)
            self._check_range(pointer, 24)
            following, name_ptr, address, size, flags, related = struct.unpack_from(
                '>6I', mv, pointer)
            if size > 0x4000000 or address + size > 0x100000000:
                raise ImageError('section range exceeds the 32-bit address/64 MiB size limits')
            name_len = min(128, img_len - name_ptr)
            self._check_range(name_ptr, name_len)
            name_data = bytes(mv[name_ptr:name_ptr + name_len])
            if b'\0' not in name_data:
                raise ImageError('section name is not terminated')
            try:
                name = name_data.split(b'\0', 1)[0].decode('ascii')
            except UnicodeDecodeError as error:
                raise ImageError('section name is not ASCII') from error
            if not name.startswith('.') or any(not 32 <= ord(char) < 127 for char in name):
                raise ImageError('invalid section name')
            sections.append(Section(pointer, name, address, size, flags, related))
            pointer = following
        return tuple(sections)

    def _operations(self, kind: str, start: int, end: int) -> list[LoadOperation]:
        if end < start or (end - start) % 12:
            raise ImageError(f'invalid {kind} scatter-table bounds')
        self._check_range(start, end - start)
        mv = self._mv
        operations = []
        for pointer in range(start, end, 12):
            destination, source, size = struct.unpack_from('>3I', mv, pointer)
            if size == 0:
                continue
            if kind == 'zero':
                matches = [section for section in self.sections
                           if section.address == destination and section.size == size]
                if len(matches) != 1 or source != 0:
                    raise ImageError('zero-fill entry has no unique declared destination')
                target, stored = matches[0], None
            else:
                self._check_range(source, size)
                matches = []
                for section in self.sections:
                    target = self.by_descriptor.get(section.related_section)
                    if (target and section.address == source and section.size >= size and
                            target.address == destination):
                        matches.append((target, section))
                if len(matches) != 1:
                    raise ImageError(f'{kind} entry has no unique linked source/destination')
                target, stored = matches[0]
                if kind == 'copy' and target.size != size:
                    raise ImageError('raw-copy length disagrees with destination section')
            operations.append(LoadOperation(pointer, kind, destination, source, size,
                                             target.size, target.name,
                                             stored.name if stored else None))
        return operations

    def extract(self, name: str) -> bytes:
        if name in self._decoded:
            return self._decoded[name]
        section = self.by_name.get(name)
        if section is None:
            raise ImageError(f'unknown section {name!r}')
        operation = next((item for item in self.operations if item.section == name), None)
        if operation:
            if operation.kind == 'zero':
                result = bytes(operation.expanded_size)
            else:
                stored = self._slice(operation.source, operation.stored_size)
                result = (unpack_lzss(stored, operation.expanded_size)
                          if operation.kind == 'lzss' else stored)
        elif section.address + section.size <= len(self.image) and section.size:
            result = self._slice(section.address, section.size)
        elif section.size == 0:
            result = b''
        else:
            raise ImageError(f'section {name} has no supported backing operation')
        self._decoded[name] = result
        return result

    def manifest(self) -> dict:
        return {'dump_sha256': hashlib.sha256(self.image).hexdigest(),
                'header_offset': self.header_offset, 'entry': self.entry,
                'support_word': self.support_word,
                'sections': [asdict(section) for section in self.sections],
                'operations': [asdict(operation) for operation in self.operations]}


def write_extracted(image: FirmwareImage, directory: Path, names: list[str],
                    source_path: Path | None = None) -> dict:
    """Write analysis artifacts after checking every output against the source."""
    plans = []
    for name in names:
        section = image.by_name.get(name)
        if section is None:
            raise ImageError(f'unknown section {name!r}')
        safe_name = ''.join(char if char.isalnum() or char in '-_' else '_'
                            for char in name.lstrip('.'))
        plans.append((name, section, directory / f'{safe_name}-{section.address:08x}.bin'))
    manifest_path = directory / 'manifest.json'
    outputs = [path for _, _, path in plans] + [manifest_path]
    resolved = [path.resolve() for path in outputs]
    if len(set(resolved)) != len(resolved):
        raise ImageError('extraction output paths collide')
    if source_path is not None:
        source_resolved = source_path.resolve()
        for path in outputs:
            if path.resolve() == source_resolved or (
                    path.exists() and source_path.exists() and path.samefile(source_path)):
                raise ImageError('extraction output would overwrite the source dump')
    # Validate all selected bytes before creating or replacing any output.
    decoded = [(name, section, path, image.extract(name)) for name, section, path in plans]
    manifest = image.manifest()
    manifest['extracted'] = []
    directory.mkdir(parents=True, exist_ok=True)
    for name, section, path, data in decoded:
        path.write_bytes(data)
        manifest['extracted'].append({'section': name, 'address': section.address,
            'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'file': path.name})
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dump', type=Path, default=Path(__file__).resolve().parents[1] / 'dump.bin')
    parser.add_argument('--header', type=lambda value: int(value, 0), default=0x20030)
    parser.add_argument('--section', action='append', help='named section to extract; may repeat')
    parser.add_argument('--extract-dir', type=Path, help='write separate analysis files and a JSON manifest')
    parser.add_argument('--include-zero', action='store_true', help='also extract zero-fill operations')
    parser.add_argument('--json', action='store_true', help='print complete section/operation metadata')
    args = parser.parse_args()
    try:
        image = FirmwareImage(args.dump.read_bytes(), args.header)
        manifest = image.manifest()
        if args.extract_dir:
            names = args.section or [item.section for item in image.operations
                                      if args.include_zero or item.kind != 'zero']
            manifest = write_extracted(image, args.extract_dir, names, args.dump)
        if args.json:
            print(json.dumps(manifest, indent=2))
        else:
            print(f'{len(image.sections)} sections; {len(image.operations)} nonempty scatter operations; entry {image.entry:#010x}')
            for operation in image.operations:
                print(f'{operation.kind:4} {operation.source:08x}+{operation.stored_size:06x} -> '
                      f'{operation.destination:08x}+{operation.expanded_size:06x} {operation.section}')
    except (OSError, ImageError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()
