"""Tests for analysis.nvm_decode against HP's own captures (read-only, no printer)."""
import hashlib
import unittest
from pathlib import Path

from analysis import nvm_decode as nd

ROOT = Path(__file__).resolve().parents[1]
MIRROR = ROOT / 'references/physical-51-nvm-mirror/nvm-ram-mirror-65536.bin'
HEADER = ROOT / 'references/physical-15-nvm-header/nvm-header-0000-00ff.bin'
CATALOG = ROOT / 'references/physical-nvm-catalog.json'
REFS = ROOT / 'references'


class CodecTests(unittest.TestCase):
    def test_aes_fips197_vector(self):
        cipher = nd.AES128(bytes(range(16)))
        self.assertEqual(cipher.encrypt(bytes.fromhex('00112233445566778899aabbccddeeff')).hex(),
                         '69c4e0d86a7b0430d8cdb78070b4c55a')

    def test_chip_id_key_derivation_order(self):
        words = [0x11111111, 0x22222222, 0x33333333, 0x44444444]  # 0x10, 0x14, 0x18, 0x1c
        secret = bytes.fromhex('44444444333333332222222211111111')
        self.assertEqual(nd.key_from_chip_id(words), hashlib.sha256(b'\x0a\x94\x01\x00' + secret).digest()[:16])

    def test_encoder_classes_decode_back(self):
        for value, signed in [(0, False), (31, False), (32, False), (2047, False), (2048, False),
                              (0x3FFFF, False), (0x40000, False), (-1, True), (-8, True), (-9, True),
                              (-2048, True), (2047, True), (-0x40000, True)]:
            length, first = nd.encoder_header(value, signed)
            body = (value & 0xFFFFFFFF).to_bytes(4, 'big')[4 - (length - 1):] if length > 1 else b''
            if length == 1:
                raw = bytes([first])
            elif length == 2:
                raw = bytes([first, value & 0xFF])
            elif length == 3:
                raw = bytes([first]) + (value & 0xFFFF).to_bytes(2, 'big')
            else:
                raw = bytes([first]) + body
            kind, size, decoded, _ = nd.token(raw, 0)
            self.assertEqual((kind, size), ('B', length))
            if signed:
                decoded = nd.sign_extend(decoded, nd.B_BITS[length])
            self.assertEqual(decoded & 0xFFFFFFFF, value & 0xFFFFFFFF, (value, raw.hex()))

    def test_delta_and_blob_tokens(self):
        self.assertEqual(nd.token(b'\x40', 0)[:3], ('A', 1, 1))
        self.assertEqual(nd.token(b'\xe0\x73', 0)[:3], ('A', 2, 0x74))
        self.assertEqual(nd.token(b'\xfd\x00\x0d', 0)[:3], ('A', 3, 0x1000E))
        self.assertEqual(nd.token(b'\xc2\xaa\xbb', 0)[:3], ('C', 3, b'\xaa\xbb'))
        self.assertEqual(nd.token(b'\xf0' + bytes(5), 0)[:2], ('C', 6))

    def test_layout_revision_4_matches_get_map_details(self):
        _, table = nd.layout_for(0x40)
        regions = {r: (o, n) for r, o, n in table}
        self.assertEqual(regions[0x4], (0x5C, 70))      # "Error Log"
        self.assertEqual(regions[0x6], (0xA4, 14))      # "Eagle Bits"
        self.assertEqual(regions[0x14], (0x52, 4))      # "Clock Offset"
        self.assertEqual(regions[0x7], (0xB2, 65358))   # "NVM3 Offset"
        self.assertEqual(regions[0x9], (0x0C, 5))       # board ID record (analysis/board_id.md)

    def test_erased_image(self):
        self.assertTrue(nd.decode(b'\xff' * nd.EEPROM_SIZE)['erased'])


@unittest.skipUnless(MIRROR.exists(), 'reference NVM mirror not present')
class MirrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.image = MIRROR.read_bytes()
        cls.result = nd.decode(cls.image, CATALOG if CATALOG.exists() else None, refs=REFS)
        cls.items = {i['item']: i for i in cls.result['boot']['items']}

    def test_boot_items(self):
        self.assertEqual(self.result['boot']['map_revision'], 4)
        self.assertEqual(self.items[4]['decoded']['board_id'], '0x68a4')
        self.assertEqual(self.items[4]['firmware_status'], 2)
        self.assertEqual(self.items[3]['value'], 0xA9)
        self.assertEqual(self.items[12]['decoded']['signed'], -1)   # nos_nvm_sirius.get 18 -> -1

    def test_serial_is_redacted_by_default(self):
        serial = self.items[5]['decoded']['serial']
        self.assertEqual(self.items[5]['firmware_status'], 2)
        self.assertEqual(len(serial), 14)
        self.assertTrue(serial.startswith('*' * 10))
        self.assertNotIn(self.image[0x1F:0x29].decode('latin-1'), str(self.result['boot']))

    def test_error_log_matches_errlog_capture(self):
        log = self.result['boot']['error_log']
        self.assertTrue(log['checksum_ok'])
        entry = log['entries'][0]
        self.assertEqual((entry['assert_id'], entry['line'], entry['page_count']), ('0xc4eb99f7', 1733, 775))

    def test_nvm3_matches_hp_statistics(self):
        nvm3 = self.result['nvm3']
        self.assertEqual(nvm3['metadata_bytes'], 589)        # "MetaData and wastage: 589"
        self.assertEqual(nvm3['free_pages'], 150)            # "Free Pages: 150"
        self.assertEqual(nvm3['holes_total'], 2102)          # "Combined Holes: 2102"
        self.assertEqual(nvm3['stream_bytes'], 8906)         # "Current Usage: 8906"
        self.assertEqual(len(nvm3['objects']), 1003)         # "Total # of objects: 1003"
        self.assertTrue(nvm3['journal_map_is_identity'])
        self.assertTrue(nvm3['dsids_strictly_ascending'])
        self.assertEqual(nvm3['stream_errors'], [])

    @unittest.skipUnless(CATALOG.exists(), 'catalog not present')
    def test_catalog_and_hp_values_agree(self):
        nvm3 = self.result['nvm3']
        self.assertEqual(nvm3['catalog_objects_absent'], [])
        self.assertEqual(nvm3['stream_objects_not_in_catalog'], [])
        scalars = self.result['crosscheck']['scalars']
        self.assertEqual(scalars['single_byte_exact_match'], 667)
        self.assertEqual(scalars['single_byte_mismatch'], [])
        self.assertEqual(scalars['not_found_as_scalar'], [])
        self.assertEqual(self.result['crosscheck']['arrays']['element_count_mismatch'], [])
        values = {o['name']: o['values'] for o in nvm3['objects']}
        self.assertEqual(values['DSID_TOTAL_PEN_COUNT_K'][0]['value'], 10)
        self.assertEqual(values['DSID_PAPER_CAL_B_OFFSET_SETTING'][0]['value'], -3)
        lo, hi = values['DSID_TOTAL_ENGINE_PAGE_COUNT'][0]['range']
        self.assertTrue(lo <= 775 <= hi)

    @unittest.skipUnless(HEADER.exists(), 'header capture not present')
    def test_header_capture_is_mirror_prefix(self):
        header = HEADER.read_bytes()
        self.assertEqual(header, self.image[:256])
        partial = nd.decode(header)
        self.assertEqual(partial['boot']['items'], self.result['boot']['items'])
        self.assertFalse(partial['nvm3']['available'])


if __name__ == '__main__':
    unittest.main()
