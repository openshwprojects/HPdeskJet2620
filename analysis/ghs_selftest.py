"""Focused checks for the offline GHS decoder: python -m analysis.ghs_selftest."""
import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analysis.ghs_update import (
    AnalysisMemory, GHSUpdate, ImageError, checksum, expanded_length, relocate,
)


def image(commands):
    body = struct.pack('>II', 0xA0010001, 0xA0010011) + commands
    return b'\x7fGHS' + bytes(20) + struct.pack('>II', len(body) + 12, checksum(body)) + body


def bits(fields):
    value = position = 0
    for word, count in fields:
        value |= word << position
        position += count
    return value.to_bytes((position + 7) // 8, 'little')


class GHSChecks(unittest.TestCase):
    def test_checksum_is_incremental(self):
        self.assertEqual(checksum(b'\x01\x02\x03'), 0x4103)
        self.assertEqual(checksum(b'\x03', checksum(b'\x01\x02')), 0x4103)

    def test_grouped_raw_records_and_delta_addresses(self):
        command = (b'\x01' + struct.pack('>HHII', 2, 1, 0x60002000, 2) + b'AB' +
                   struct.pack('>HI', 2, 0x60003000) + b'\x01\x01x\x02\x02yz')
        update = GHSUpdate(image(command), 0)
        records = update.commands[0].records
        self.assertEqual([r['destination'] for r in records],
                         [0x60002000, 0x60003001, 0x60003003])
        self.assertEqual([r['stored_size'] for r in records], [2, 1, 2])
        self.assertEqual(update.entry, 0xA0010011)

    def test_corruption_and_truncation_fail(self):
        valid = bytearray(image(b'\x06\x00\x00'))
        valid[-1] ^= 1
        with self.assertRaisesRegex(ImageError, 'checksum mismatch'):
            GHSUpdate(valid, 0)
        with self.assertRaisesRegex(ImageError, 'truncated GHS'):
            GHSUpdate(image(b'\x06\x00\x01'), 0)
        with self.assertRaisesRegex(ImageError, 'unknown GHS command'):
            GHSUpdate(image(b'\x80'), 0)

    def test_lzss_length_and_truncation(self):
        self.assertEqual(expanded_length(b'\x01A\xee\xf0'), 4)
        with self.assertRaises(ImageError):
            expanded_length(b'\x00\xee')

    def test_relocation_prefix_and_lookahead(self):
        # Reset addend=0x20, default code table, one +4-byte word relocation,
        # then prefix 10 terminates. The decoder prefetches later real bytes.
        payload = bits([(1, 1), (0x20, 32), (0, 1), (1, 1), (0x200, 10)])
        memory = AnalysisMemory()
        memory.write(0x60001004, (0xFFFFFFF0).to_bytes(4, 'big'))
        result = relocate(memory, payload + b'\x04\x00\x00\x00',
                          base=0x60001000, payload_size=len(payload))
        self.assertEqual(memory.read(0x60001004, 4), b'\0\0\0\x10')
        self.assertEqual(result['word_writes'], 1)
        self.assertEqual(result['terminator'], 10)
        self.assertEqual(result['consumed_bits'], 45)
        with self.assertRaisesRegex(ImageError, 'truncated GHS relocation'):
            relocate(memory, payload, base=0x60001000, payload_size=len(payload))


if __name__ == '__main__':
    unittest.main()
