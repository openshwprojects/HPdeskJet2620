"""Record-format checks only: these bytes are not a physical EEPROM image."""
import unittest

from analysis.board_id import decode_record, decode_support


class BoardIdInspectionTests(unittest.TestCase):
    def test_rom_support_complement_and_supported_range(self):
        support = decode_support(bytes.fromhex('064501ff f9bafe00'))
        self.assertTrue(support['valid_complement'])
        self.assertEqual((support['M'], support['F']), (6, 0x45))
        self.assertEqual((support['minimum_N'], support['maximum_N']), (1, 31))
        self.assertEqual(support['fallback_id'], '0x68bf')
        self.assertFalse(decode_support(bytes.fromhex('064501ff f9bafe01'))['valid_complement'])

    def test_valid_placeholder_is_still_missing(self):
        result = decode_record(bytes.fromhex('001f ffe0 e0'))
        self.assertTrue(result['copies_are_complements'])
        self.assertTrue(result['primary_checksum_valid'])
        self.assertTrue(result['backup_checksum_valid'])
        self.assertEqual(result['firmware_status'], 2)
        self.assertEqual(result['selected_id'], '0x001f')
        self.assertTrue(result['missing_warning'])

    def test_erased_and_unfilled_records_choose_firmware_default(self):
        for record in (bytes([0xff]) * 5, bytes(5)):
            with self.subTest(raw=record.hex()):
                result = decode_record(record)
                self.assertEqual(result['selected_source'], 'firmware-default')
                self.assertEqual(result['selected_id'], '0x001f')
                self.assertEqual(result['firmware_status'], 0)

    def test_damaged_complement_recovers_primary(self):
        result = decode_record(bytes.fromhex('001f ffe1 e0'))
        self.assertFalse(result['copies_are_complements'])
        self.assertTrue(result['primary_checksum_valid'])
        self.assertFalse(result['backup_checksum_valid'])
        self.assertEqual(result['selected_source'], 'primary')
        self.assertEqual(result['firmware_status'], 1)

    def test_damaged_primary_recovers_complemented_copy(self):
        result = decode_record(bytes.fromhex('001e ffe0 e0'))
        self.assertEqual(result['selected_source'], 'complemented-copy')
        self.assertEqual(result['selected_id'], '0x001f')
        self.assertEqual(result['firmware_status'], 1)

    def test_damaged_checksum_preserves_matching_copies(self):
        result = decode_record(bytes.fromhex('001f ffe0 e1'))
        self.assertTrue(result['copies_are_complements'])
        self.assertFalse(result['primary_checksum_valid'])
        self.assertFalse(result['backup_checksum_valid'])
        self.assertEqual(result['selected_source'], 'primary')
        self.assertEqual(result['firmware_status'], 1)

    def test_ambiguous_copies_or_special_zero_checksum_select_default(self):
        # Both copies share the same XOR but disagree: reader cannot select one.
        ambiguous = decode_record(bytes.fromhex('001f fee1 e0'))
        self.assertEqual(ambiguous['selected_source'], 'firmware-default')
        # Firmware has a separate zero-checksum rejection on backup-only recovery.
        zero_checksum = decode_record(bytes.fromhex('0000 ff00 00'))
        self.assertTrue(zero_checksum['backup_checksum_valid'])
        self.assertEqual(zero_checksum['selected_source'], 'firmware-default')

    def test_record_size_is_explicit(self):
        for record in (b'', bytes(4), bytes(6)):
            with self.subTest(size=len(record)):
                with self.assertRaises(ValueError):
                    decode_record(record)


if __name__ == '__main__':
    unittest.main()
