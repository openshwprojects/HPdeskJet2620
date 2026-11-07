"""Inspect the GHS update stream using the boot parser's command format.

This is offline analysis, never a replacement for emulated loader execution.
The input dump is only read. Unknown commands and truncated records fail.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from analysis.firmware_image import FirmwareImage, ImageError, unpack_lzss


def checksum(data: bytes, value: int = 0) -> int:
    for byte in data:
        value = (((value << 7) | (value >> 25)) + byte) & 0xFFFFFFFF
    return value


class Stream:
    def __init__(self, image: bytes, start: int, end: int):
        if not 0 <= start <= end <= len(image):
            raise ImageError('GHS stream range is outside its source')
        self.image, self.position, self.end = image, start, end

    def take(self, size: int) -> bytes:
        if size < 0 or self.position + size > self.end:
            raise ImageError(f'truncated GHS record at {self.position:#x}')
        result = self.image[self.position:self.position + size]
        self.position += size
        return result

    def integer(self, size: int) -> int:
        return int.from_bytes(self.take(size), 'big')


def expanded_length(packed: bytes) -> int:
    stream = Stream(packed, 0, len(packed))
    flags = size = 0
    while stream.position < stream.end:
        flags >>= 1
        if not flags & 0x100:
            flags = 0xFF00 | stream.integer(1)
        stream.integer(1)
        size += 1 if flags & 1 else (stream.integer(1) & 15) + 3
        if size > 0x4000000:
            raise ImageError('GHS expansion exceeds the analysis limit')
    return size


class AnalysisMemory:
    """Private zeroed analysis banks with the emulator's DDR alias convention."""
    def __init__(self):
        self.dram = bytearray(0x4000000)
        self.itcm = bytearray(0x2000)
        self.dtcm = bytearray(0x1000)

    def region(self, address: int, size: int) -> tuple[bytearray, int]:
        if size < 0:
            raise ImageError('negative analysis memory extent')
        if 0 <= address < len(self.itcm):
            bank, offset = self.itcm, address
        elif 0x60000000 <= address < 0x60001000:
            bank, offset = self.dtcm, address - 0x60000000
        elif any(base <= address < base + 0x8000000 for base in
                 (0x20000000, 0x60000000, 0xA0000000)):
            bank, offset = self.dram, address & 0x3FFFFFF
        else:
            raise ImageError(f'unmodeled analysis memory address {address:#x}')
        if offset + size > len(bank):
            raise ImageError('analysis memory access crosses its backing bank')
        return bank, offset

    def read(self, address: int, size: int) -> bytes:
        bank, offset = self.region(address, size)
        return bytes(bank[offset:offset + size])

    def write(self, address: int, data: bytes) -> None:
        bank, offset = self.region(address, len(data))
        bank[offset:offset + len(data)] = data

    @classmethod
    def scatter_loaded(cls, image: FirmwareImage) -> 'AnalysisMemory':
        memory = cls()
        for item in image.operations:
            memory.write(item.destination, image.extract(item.section))
        return memory


class RelocationBits:
    """LSB-first, 32-bit lookahead at A000B27C; consume refills the high bits."""
    def __init__(self, data: bytes):
        self.data, self.position, self.value = data, 0, 0

    def consume(self, count: int) -> int:
        if not 0 <= count <= 32 or self.position + count > len(self.data) * 8:
            raise ImageError(f'truncated GHS relocation bitstream at bit {self.position}, '
                             f'need {count}, available {len(self.data) * 8}')
        refill = 0
        for index in range(count):
            position = self.position + index
            refill |= ((self.data[position // 8] >> (position & 7)) & 1) << index
        self.position += count
        self.value = ((self.value >> count) | (refill << (32 - count))) & 0xFFFFFFFF
        return self.value

    def table(self, word: int) -> int:
        table = word >> 1 if word & 1 else 0x1AC688
        self.codes = [(table >> (index * 3)) & 7 for index in range(7)]
        return self.consume(22 if word & 1 else 1)


def relocate(memory: AnalysisMemory, data: bytes, base: int = 0,
             payload_size: int | None = None) -> dict:
    bits = RelocationBits(data)
    word = bits.consume(32)
    kind = 9 if word & 1 else 8
    word = bits.consume(1)
    address, addend, writes = base, 0, 0
    widths = (0, 1, 2, 4, 8, 16, 32)
    starts = (1, 2, 4, 8, 0x18, 0x118, 0x10118)
    while True:
        if 1 <= kind <= 7:
            code = bits.codes[kind - 1]
            if code >= len(widths):
                raise ImageError('GHS relocation table selects unsupported code 7')
            width = widths[code]
            delta = starts[code] + (word & ((1 << width) - 1))
            address = (address + delta * 4) & 0xFFFFFFFF
            if address:
                value = (int.from_bytes(memory.read(address, 4), 'big') + addend) & 0xFFFFFFFF
                memory.write(address, value.to_bytes(4, 'big'))
                writes += 1
            word = bits.consume(width)
        elif kind in (8, 9):
            if kind == 8:
                addend = (addend + (word & 0xFFFF) + 1) & 0xFFFFFFFF
                word = bits.consume(16)
            else:
                addend = word
                word = bits.consume(32)
            word = bits.table(word)
            address = base
        else:
            break
        if not word:
            raise ImageError('GHS relocation prefix has no terminating one bit')
        kind = (word & -word).bit_length()
        word = bits.consume(kind)
    payload_size = len(data) if payload_size is None else payload_size
    # The firmware prefetches 32 bits and can read up to four source bytes
    # beyond the declared relocation payload, into the following command.
    logical_bits = bits.position - 32
    if logical_bits > payload_size * 8:
        raise ImageError('GHS relocation terminator exceeds its declared payload')
    return {'word_writes': writes, 'consumed_bits': logical_bits,
            'payload_bits': payload_size * 8, 'prefetched_bits': bits.position,
            'terminator': kind}


@dataclass
class Command:
    address: int
    opcode: int
    records: list[dict]
    end: int = 0


class GHSUpdate:
    def __init__(self, image: bytes, address: int = 0xC93E98):
        self.image, self.address = bytes(image), address
        header = Stream(self.image, address, len(image))
        if header.take(4) != b'\x7fGHS':
            raise ImageError('GHS update magic does not match')
        header.take(20)
        self.size = header.integer(4)
        self.end = address + 20 + self.size
        if self.size < 20 or self.end > len(image):
            raise ImageError('GHS update extent is invalid')
        self.expected_checksum = header.integer(4)
        self.actual_checksum = checksum(self.image[address + 32:self.end])
        if self.actual_checksum != self.expected_checksum:
            raise ImageError('GHS encoded-stream checksum mismatch')
        self.original_entry = header.integer(4)
        self.entry = header.integer(4)
        stream = Stream(self.image, header.position, self.end)
        self.commands: list[Command] = []
        while stream.position < stream.end:
            command = Command(stream.position, stream.integer(1), [])
            self._parse(command, stream)
            command.end = stream.position
            self.commands.append(command)

    def _parse(self, command: Command, stream: Stream) -> None:
        op = command.opcode
        if op in (1, 2, 5):
            # AFD2/B042/B00A: first group has full addresses/lengths;
            # subsequent groups use one-byte address deltas and lengths.
            groups = stream.integer(2)
            for group in range(groups):
                count = stream.integer(2)
                current = stream.integer(4) if group else 0
                for _ in range(count):
                    current = ((current + stream.integer(1)) & 0xFFFFFFFF) if group else stream.integer(4)
                    size = 1 if op == 2 else stream.integer(1 if group else 4)
                    source = stream.position
                    packed = stream.take(size)
                    expanded = expanded_length(packed) if op == 5 else size
                    command.records.append({'destination': current, 'source': source,
                                            'stored_size': size, 'expanded_size': expanded})
        elif op == 3:
            # AEDE computes memmove(destination=address+delta, source=address).
            for _ in range(stream.integer(2)):
                source, size, delta = stream.integer(4), stream.integer(4), stream.integer(4)
                command.records.append({'source_address': source,
                    'destination': (source + delta) & 0xFFFFFFFF, 'size': size})
        elif op == 4:
            for _ in range(stream.integer(4)):
                start, end = stream.integer(4), stream.integer(4)
                if end < start:
                    raise ImageError('GHS checksum range is reversed')
                command.records.append({'start': start, 'end': end})
            command.records.append({'checksum': stream.integer(4)})
        elif op == 6:
            for _ in range(stream.integer(2)):
                destination, size, value = stream.integer(4), stream.integer(2), stream.integer(1)
                command.records.append({'destination': destination, 'size': size, 'value': value})
        elif op == 7:
            size = stream.integer(4)
            command.records.append({'source': stream.position, 'size': size})
            stream.take(size)
        else:
            raise ImageError(f'unknown GHS command {op:#x} at {command.address:#x}')

    def report(self) -> dict:
        return {'address': self.address, 'end': self.end,
                'stream_checksum': f'{self.actual_checksum:08x}',
                'original_entry': self.original_entry, 'entry': self.entry,
                'commands': [{'address': item.address, 'end': item.end,
                              'opcode': item.opcode, 'records': item.records}
                             for item in self.commands]}

    def verify(self, memory: AnalysisMemory, *, skip_checksums: int = 1) -> dict:
        """Apply commands to private analysis RAM and check firmware checksums.

        Normal boot passes one, skipping the first in-CPU checksum. We still
        calculate and compare it independently and record that distinction.
        """
        if not self.commands or self.commands[-1].opcode != 4:
            raise ImageError('verified GHS stream must end with a runtime checksum command')
        checks, relocations = [], []
        for command in self.commands:
            op = command.opcode
            if op in (1, 2, 5):
                for item in command.records:
                    data = self.image[item['source']:item['source'] + item['stored_size']]
                    if op == 5:
                        data = unpack_lzss(data, item['expanded_size'])
                    memory.write(item['destination'], data)
            elif op == 3:
                for item in command.records:
                    memory.write(item['destination'], memory.read(item['source_address'], item['size']))
            elif op == 4:
                actual, total = 0, 0
                for item in command.records[:-1]:
                    data = memory.read(item['start'], item['end'] - item['start'])
                    actual = checksum(data, actual)
                    total += len(data)
                expected = command.records[-1]['checksum']
                checks.append({'command': command.address, 'bytes': total,
                    'actual': f'{actual:08x}', 'expected': f'{expected:08x}',
                    'matches': actual == expected, 'firmware_skips': bool(skip_checksums)})
                if actual != expected:
                    raise ImageError(f'GHS runtime checksum mismatch at {command.address:#x}: '
                                     f'{actual:08x} != {expected:08x}')
                if skip_checksums:
                    skip_checksums -= 1
            elif op == 6:
                for item in command.records:
                    memory.write(item['destination'], bytes([item['value']]) * item['size'])
            elif op == 7:
                item = command.records[0]
                data = self.image[item['source']:item['source'] + item['size'] + 4]
                relocations.append(relocate(memory, data, payload_size=item['size']))
        ranges = []
        for item in self.commands[-1].records[:-1]:
            data = memory.read(item['start'], item['end'] - item['start'])
            ranges.append({**item, 'sha256': hashlib.sha256(data).hexdigest()})
        return {'checksums': checks, 'relocations': relocations,
                'entry': self.entry, 'entry_bytes': memory.read(self.entry & ~1, 32).hex(),
                'final_ranges': ranges}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dump', type=Path, default=Path(__file__).resolve().parents[1] / 'dump.bin')
    parser.add_argument('--address', type=lambda value: int(value, 0), default=0xC93E98)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--verify', action='store_true',
                        help='privately scatter-load and apply the update, checking both runtime checksums')
    args = parser.parse_args()
    try:
        raw = args.dump.read_bytes()
        update = GHSUpdate(raw, args.address)
        verification = update.verify(AnalysisMemory.scatter_loaded(FirmwareImage(raw))) if args.verify else None
        if args.json:
            print(json.dumps(verification if verification else update.report(), indent=2))
        else:
            print(f'GHS {update.address:08x}..{update.end:08x}: checksum {update.actual_checksum:08x}')
            print(f'Entry {update.original_entry:08x} -> {update.entry:08x}')
            for item in update.commands:
                print(f'{item.address:08x} opcode {item.opcode}: {len(item.records)} records; end {item.end:08x}')
            if verification:
                for item in verification['checksums']:
                    print(f"Runtime checksum {item['actual']} PASS over {item['bytes']} bytes"
                          + (' (firmware normally skips this check)' if item['firmware_skips'] else ''))
                print(f"Relocations: {verification['relocations']}")
                print(f"Entry bytes: {verification['entry_bytes']}")
    except (ImageError, OSError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()
