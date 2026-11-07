"""Read-only SPI evidence inventory across all initialized firmware sections.

Run from the repository root with ``python -m analysis.spi_probe``. This uses
the actual section/scatter records; it neither patches nor executes firmware.
Direct references are useful evidence, not a complete interprocedural analysis.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import struct

from analysis.firmware_image import FirmwareImage


SPI_START, SPI_END = 0xA000CE98, 0xA000D252
REGISTERS = (0xBD063800, 0xBD063804, 0xBD063808, 0xBD063814,
             0xBD063818, 0xBD06381A, 0xBD06381C)


def occurrences(data: bytes, needle: bytes) -> list[int]:
    result, cursor = [], 0
    while (cursor := data.find(needle, cursor)) != -1:
        result.append(cursor)
        cursor += 1
    return result


def direct_thumb_calls(data: bytes, address: int) -> list[dict]:
    """Find ARMv5 Thumb BL encodings; callers must verify code/data boundaries."""
    words = struct.unpack(f'>{len(data) // 2}H', data[:len(data) & ~1])
    calls = []
    for index in range(len(words) - 1):
        upper, lower = words[index], words[index + 1]
        if upper & 0xF800 != 0xF000 or lower & 0xF800 != 0xF800:
            continue
        displacement = ((upper & 0x7FF) << 12) | ((lower & 0x7FF) << 1)
        if displacement & 0x400000:
            displacement -= 0x800000
        source = address + index * 2
        target = source + 4 + displacement
        if SPI_START <= target < SPI_END:
            calls.append({'address': hex(source), 'target': hex(target)})
    return calls


def flash_profiles(image: FirmwareImage) -> list[dict]:
    section = image.by_name['.load_rodata']
    data = image.extract(section.name)
    result = []
    for address in range(0xA0011A98, 0xA00120C8 + 1, 36):
        offset = address - section.address
        entry = data[offset:offset + 36]
        identifier, _, name_pointer = struct.unpack_from('>3I', entry)
        name_offset = name_pointer - section.address
        name = data[name_offset:data.index(0, name_offset)].decode('ascii')
        blocks = int.from_bytes(entry[14:16], 'big')
        block_size = int.from_bytes(entry[16:20], 'big')
        result.append({'address': hex(address), 'jedec_id': f'{identifier:08x}',
                       'name': name, 'size': blocks * block_size})
    return result


def clock_schedules() -> dict:
    # Arguments to 0xa000ceba, independently transcribed from 0xa000cf12.
    # Every r1=1 transfer and every final r2=0 transfer has zero TX data.
    sequences = {
        'jedec': [(0x9F, 0, 1), (0, 0, 1), (0, 1, 1), (0, 0, 0)],
        'legacy_ab': [(0xAB, 0, 1), (0, 0, 1), (0, 0, 1), (0, 1, 1), (0, 0, 0)],
        'legacy_85': [(0x85, 0, 1), (0, 1, 1), (0, 0, 1), (0, 0, 0)],
        'extended_jedec': [(0x9F, 0, 1), (0, 0, 1), (0, 1, 1),
                           (0, 0, 1), (0, 0, 1), (0, 0, 0)],
    }
    result = {}
    for name, arguments in sequences.items():
        final_byte_model = [value for value, _, _ in arguments]
        sixteen_bit_model = []
        for value, wide, held in arguments:
            if held:
                sixteen_bit_model.append(value)
                if wide:
                    sixteen_bit_model.append(0)
        result[name] = {
            'arguments': arguments,
            'eight_bit_with_final_byte': final_byte_model,
            'eight_sixteen_bit_with_final_cs_only': sixteen_bit_model,
            'identical_clocked_byte_sequence': final_byte_model == sixteen_bit_model,
        }
    return result


def inspect(image: FirmwareImage) -> dict:
    sections = []
    for operation in image.operations:
        if operation.kind == 'zero':
            continue
        data = image.extract(operation.section)
        literals = {hex(register): [hex(operation.destination + offset)
                    for offset in occurrences(data, register.to_bytes(4, 'big'))]
                    for register in REGISTERS}
        sections.append({
            'name': operation.section, 'address': hex(operation.destination),
            'size': len(data),
            'register_literals': {key: value for key, value in literals.items() if value},
            'direct_thumb_spi_calls': direct_thumb_calls(data, operation.destination),
        })
    profiles = flash_profiles(image)
    return {'sections': sections, 'flash_profiles': profiles,
            'board_profile': next(item for item in profiles if item['jedec_id'] == '00ef4018'),
            'observationally_equivalent_transfer_models': clock_schedules(),
            'limitation': 'No indirect-call or complete data-flow analysis; this is not proof '
                          'that computed references cannot exist.'}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dump', type=Path, default=Path(__file__).resolve().parents[1] / 'dump.bin')
    args = parser.parse_args()
    print(json.dumps(inspect(FirmwareImage(args.dump.read_bytes())), indent=2))


if __name__ == '__main__':
    main()
