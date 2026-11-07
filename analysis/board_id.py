"""Read-only inspection of this firmware's board support and EEPROM ID.

The EEPROM decoder follows Thumb routines A000C9A2/A000C9D0/A000C9F6.
It deliberately exposes the firmware's recovery behavior, including accepting
complementary copies when their checksum is damaged. It never creates an image.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


SUPPORT_OFFSETS = (0x2000, 0x2003C, 0xFFFF9C)
EEPROM_ID_OFFSET = 0x0C
DEFAULT_NVM_ID = 0x001F


def id_fields(value: int) -> dict[str, int]:
    """Decode the firmware's Mfg/Fam/board-number fields."""
    return {'M': (value >> 12) & 0xF, 'F': (value >> 5) & 0x7F, 'N': value & 0x1F}


def decode_support(pair: bytes) -> dict:
    if len(pair) != 8:
        raise ValueError('board support requires one word and its complemented word')
    word = int.from_bytes(pair[:4], 'big')
    complement = int.from_bytes(pair[4:], 'big')
    fields = {'M': (word >> 24) & 0xF, 'F': (word >> 16) & 0x7F,
              'minimum_N': (word >> 8) & 0x1F, 'maximum_N': word & 0x1F}
    fallback = (fields['M'] << 12) | (fields['F'] << 5) | fields['maximum_N']
    return {'support_word': f'0x{word:08x}', 'complement_word': f'0x{complement:08x}',
            'valid_complement': word ^ complement == 0xFFFFFFFF,
            **fields, 'fallback_id': f'0x{fallback:04x}'}


def decode_record(record: bytes) -> dict:
    """Decode exactly five bytes at EEPROM 0x0C; no repair or writes occur.

    Layout: two big-endian ID bytes, one complemented copy of those two bytes,
    then checksum = FF XOR ID[0] XOR ID[1]. Validation mode is zero for item 4.
    Status 0 selects the firmware default, 1 recovers data, and 2 accepts it.
    These status meanings are inferred from the reader's branches.
    """
    if len(record) != 5:
        raise ValueError('the board-ID record must contain exactly five bytes')
    first, second = record[:2], bytes(value ^ 0xFF for value in record[2:4])
    primary_checksum = 0xFF ^ first[0] ^ first[1]
    backup_checksum = 0xFF ^ second[0] ^ second[1]
    copies_match = first == second
    primary_ok = primary_checksum == record[4]
    backup_ok = backup_checksum == record[4]
    status, source, selected = 2, 'primary', first

    # A000CAA8..A000CAB6 XORs these three predicates, rather than requiring
    # every check to pass. Preserve that recovery policy exactly.
    if not (copies_match ^ primary_ok ^ backup_ok):
        status, source, selected = 0, 'firmware-default', DEFAULT_NVM_ID.to_bytes(2, 'big')
        reason = 'validation predicates do not select a usable copy'
    elif backup_ok:
        source, selected = 'complemented-copy', second
        if copies_match:
            reason = 'both copies and checksum agree'
        elif record[4] == 0:
            status, source, selected = 0, 'firmware-default', DEFAULT_NVM_ID.to_bytes(2, 'big')
            reason = 'firmware rejects an inconsistent backup-only result with zero checksum'
        else:
            status = 1
            reason = 'recovered the complemented copy using its checksum'
    else:
        status = 1
        reason = ('complementary copies retained despite damaged checksum' if copies_match
                  else 'recovered the primary copy using its checksum')

    selected_id = int.from_bytes(selected, 'big')
    return {'offset': '0x000c', 'raw': record.hex(),
            'primary_id': f'0x{int.from_bytes(first, "big"):04x}',
            'decoded_backup_id': f'0x{int.from_bytes(second, "big"):04x}',
            'copies_are_complements': copies_match,
            'stored_checksum': f'0x{record[4]:02x}',
            'expected_primary_checksum': f'0x{primary_checksum:02x}',
            'expected_backup_checksum': f'0x{backup_checksum:02x}',
            'primary_checksum_valid': primary_ok, 'backup_checksum_valid': backup_ok,
            'firmware_status': status, 'selected_source': source,
            'selected_id': f'0x{selected_id:04x}', 'selected_fields': id_fields(selected_id),
            'missing_warning': selected_id == DEFAULT_NVM_ID, 'reason': reason}


def inspect_flash(image: bytes) -> list[dict]:
    if len(image) != 0x1000000:
        raise ValueError('this dump layout requires a 16 MiB flash image')
    return [{'offset': f'0x{offset:06x}', **decode_support(image[offset:offset + 8])}
            for offset in SUPPORT_OFFSETS]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dump', type=Path, default=Path(__file__).resolve().parents[1] / 'dump.bin')
    parser.add_argument('--eeprom', type=Path, help='optional read-only 64 KiB EEPROM image')
    parser.add_argument('--json', action='store_true', help='print structured results')
    args = parser.parse_args()
    try:
        result = {'flash_support': inspect_flash(args.dump.read_bytes()), 'eeprom': None}
        if args.eeprom:
            eeprom = args.eeprom.read_bytes()
            if len(eeprom) != 0x10000:
                raise ValueError('EEPROM image must be exactly 64 KiB')
            result['eeprom'] = decode_record(eeprom[EEPROM_ID_OFFSET:EEPROM_ID_OFFSET + 5])
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if args.json:
        print(json.dumps(result, indent=2))
        return
    for support in result['flash_support']:
        valid = 'valid' if support['valid_complement'] else 'INVALID'
        print(f"Flash {support['offset']}: {support['support_word']} / "
              f"{support['complement_word']} ({valid}); M={support['M']:02x} "
              f"F={support['F']:02x}, N range {support['minimum_N']:02x}..{support['maximum_N']:02x}, "
              f"fallback {support['fallback_id']}")
    if result['eeprom'] is None:
        print('No EEPROM supplied. The physical board ID cannot be recovered from these support words.')
    else:
        record = result['eeprom']
        print(f"EEPROM 0x000c: {record['raw']} -> {record['selected_id']} "
              f"from {record['selected_source']}; {record['reason']}")
        if record['missing_warning']:
            print('This produces the firmware MISSING warning; ROM/default board selection remains available.')
    print('Firmware diagnostics identify M/F as Mfg/Fam and N as the board number.')


if __name__ == '__main__':
    main()
