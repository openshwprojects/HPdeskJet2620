"""SPI command framing and immutable flash data, independently of boot PCs."""
import unittest
import heapq
import hashlib

from spi import FlashWindow, SPIController, SPIError, W25Q128JV


class FlashTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.image = bytes(range(256)) * (W25Q128JV.SIZE // 256)

    def setUp(self):
        self.flash = W25Q128JV(self.image)

    def transfer(self, data):
        self.flash.select()
        try:
            return [self.flash.exchange(value) for value in data]
        finally:
            self.flash.deselect()

    def test_jedec_id_requires_cs_and_real_dummy_clocks(self):
        self.assertIsNone(self.flash.exchange(0x9F))
        self.assertEqual(self.transfer([0x9F, 0, 0, 0]), [None, 0xEF, 0x40, 0x18])
        self.assertEqual(self.flash.transactions[-1]['tx'], [0x9F, 0, 0, 0])
        self.assertTrue(self.flash.transactions[-1]['complete'])
        other = W25Q128JV(self.image, ordering_option='IM')
        other.select()
        self.assertEqual([other.exchange(b) for b in (0x9F, 0, 0, 0)],
                         [None, 0xEF, 0x70, 0x18])

    def test_read_address_and_sequential_wrap_use_actual_image(self):
        self.assertEqual(self.transfer([0x03, 0x12, 0x34, 0xFE, 0, 0, 0]),
                         [None] * 4 + [0xFE, 0xFF, 0])
        self.assertEqual(self.transfer([0x03, 0xFF, 0xFF, 0xFF, 0, 0]),
                         [None] * 4 + [0xFF, 0])
        self.assertIs(self.flash.image, self.image)

    def test_fast_read_consumes_one_dummy_byte(self):
        self.assertEqual(self.transfer([0x0B, 0, 0, 0x23, 0xA5, 0, 0]),
                         [None] * 5 + [0x23, 0x24])

    def test_cs_release_discards_partial_address(self):
        self.transfer([0x03, 0x12, 0x34])
        self.assertEqual(self.transfer([0x9F, 0, 0, 0]), [None, 0xEF, 0x40, 0x18])
        self.assertEqual(self.transfer([0x03, 0, 0, 0x42, 0]), [None] * 4 + [0x42])

    def test_status_factory_profile_and_write_latch_cs_commit(self):
        self.assertEqual(self.transfer([0x05, 0, 0]), [None, 0, 0])
        self.assertEqual(self.transfer([0x35, 0]), [None, 2])
        self.assertEqual(self.transfer([0x15, 0]), [None, 0x60])
        self.flash.select()
        self.flash.exchange(0x06)
        self.assertFalse(self.flash.write_enabled)
        self.flash.deselect()
        self.assertEqual(self.transfer([0x05, 0]), [None, 2])
        self.transfer([0x04, 0])  # Extra byte invalidates single-byte WRDI.
        self.assertTrue(self.flash.write_enabled)
        self.transfer([0x04])
        self.assertFalse(self.flash.write_enabled)

    def test_legacy_device_id_consumes_three_dummy_bytes(self):
        self.assertEqual(self.transfer([0xAB, 1, 2, 3, 0, 0]),
                         [None] * 4 + [0x17, 0x17])

    def test_unknown_writes_and_uncaptured_metadata_are_explicit_errors(self):
        original = self.flash.image
        for opcode in (0x02, 0x20, 0xC7, 0x01, 0x5A, 0x4B, 0xFE):
            with self.subTest(opcode=opcode), self.assertRaises(SPIError):
                self.transfer([opcode])
        self.assertIs(self.flash.image, original)
        self.assertEqual(self.flash.image, self.image)

    def test_nonvolatile_status_must_be_explicit_and_reset_state_valid(self):
        self.flash = W25Q128JV(self.image, status_registers=(0x1C, 0x42, 0x20))
        self.assertEqual(self.transfer([0x05, 0]), [None, 0x1C])
        self.assertEqual(self.transfer([0x35, 0]), [None, 0x42])
        with self.assertRaises(ValueError):
            W25Q128JV(self.image, status_registers=(1, 2, 0x60))
        with self.assertRaises(ValueError):
            W25Q128JV(self.image, status_registers=(0, 0, 0x60))


class ControllerBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.controller = SPIController()
        self.base = self.controller.base

    def test_window_encoding_matches_driver_start_and_inclusive_end_mib(self):
        self.controller.write(self.base + 0x18, 2, 15 << 6)
        self.assertEqual(self.controller.window(0), FlashWindow(0, 0, 16 << 20))
        self.controller.write(self.base + 0x1A, 2, (31 << 6) | 16)
        self.assertEqual(self.controller.window(1), FlashWindow(1, 16 << 20, 32 << 20))
        with self.assertRaisesRegex(SPIError, 'reset values'):
            self.controller.window(2)

    def test_undecoded_transfer_faults_without_flash_side_effects(self):
        flash = W25Q128JV(bytes(W25Q128JV.SIZE))
        self.controller.devices[0] = flash
        self.controller.write(self.base + 8, 4, 0x23)
        with self.assertRaisesRegex(SPIError, 'unresolved control bit 5'):
            self.controller.write(self.base + 0x14, 4, 0x9F000000)
        self.assertEqual(flash.transactions, [])
        self.assertFalse(flash.selected)
        self.assertEqual(self.controller.read(self.base, 4), 0)
        with self.assertRaisesRegex(SPIError, 'receive data unavailable'):
            self.controller.read(self.base + 4, 4)

    def test_access_widths_are_checked(self):
        with self.assertRaisesRegex(SPIError, 'unmodeled SPI access'):
            self.controller.write(self.base + 0x18, 4, 0)
        with self.assertRaisesRegex(SPIError, 'high bits'):
            self.controller.write(self.base + 8, 4, 0x10000)

    def test_board_routes_registers_and_preserves_strict_transfer_boundary(self):
        from hardware import BootHardware, HardwareError
        board = BootHardware()
        self.assertIn(self.base & ~0xFFF, board.pages)
        self.assertEqual(board.read(self.base, 4), 0)
        self.assertEqual(board.read(self.base + 8, 4), 0)
        board.write(self.base + 8, 4, 0x23)
        self.assertEqual(board.read(self.base + 8, 4), 0x23)
        with self.assertRaisesRegex(HardwareError, 'unresolved control bit 5'):
            board.write(self.base + 0x14, 4, 0x9F000000)
        with self.assertRaisesRegex(HardwareError, 'reset values'):
            board.read(self.base + 0x18, 2)


class Clock:
    def __init__(self):
        self.now = 0
        self.events = []
        self.sequence = 0

    def schedule(self, delay, callback):
        self.sequence += 1
        heapq.heappush(self.events, (self.now + delay, self.sequence, callback))

    def advance(self, ticks):
        target = self.now + ticks
        while self.events and self.events[0][0] <= target:
            self.now, _, callback = heapq.heappop(self.events)
            callback()
        self.now = target


class SerializerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.image = bytes(range(256)) * (W25Q128JV.SIZE // 256)

    def make(self, profile='byte-final', pull=1, bit_ticks=1):
        self.clock = Clock()
        self.flash = W25Q128JV(self.image)
        self.controller = SPIController(schedule=self.clock.schedule, profile=profile,
                                        miso_pull=pull, devices={0: self.flash},
                                        bit_ticks=bit_ticks)
        self.base = self.controller.base

    def request(self, value, *, held=True, wide=False, device=0):
        control = (device << 10) | 2 | int(held) | (0 if wide else 0x20)
        self.controller.write(self.base + 8, 4, control)
        self.controller.write(self.base + 0x14, 4, value)

    def exchange(self, value, **kwargs):
        self.request(value << 24, **kwargs)
        self.clock.advance(16)

    def jedec(self, device=0):
        self.exchange(0x9F, device=device)
        self.exchange(0, device=device)
        self.exchange(0, wide=True, device=device)
        self.exchange(0, held=False, device=device)
        return self.controller.read(self.base + 4, 4)

    def test_candidate_profiles_produce_same_real_jedec_wire_transaction(self):
        for profile in SPIController.PROFILES:
            with self.subTest(profile=profile):
                self.make(profile)
                self.assertEqual(self.jedec(), 0xFFEF4018)
                self.assertEqual(self.flash.transactions[-1]['tx'], [0x9F, 0, 0, 0])
                self.assertEqual(self.flash.transactions[-1]['rx'], [None, 0xEF, 0x40, 0x18])
                self.assertTrue(self.flash.transactions[-1]['complete'])
                self.assertFalse(self.flash.selected)
                self.assertEqual(self.controller.read(self.base, 4), 0)
                self.assertEqual(self.controller.rx_known_mask, 0xFFFFFFFF)

    def test_long_nonperiodic_array_read_wraps_and_preserves_every_wire_byte(self):
        # A 256-byte repeating image can hide dropped or repeated transfers.
        # Exercise both serializers with an independent nonperiodic source,
        # including the actual last-address to zero transition.
        image = hashlib.shake_256(b'SPI sequential read regression').digest(W25Q128JV.SIZE)
        start = W25Q128JV.SIZE - 137
        expected = image[start:] + image[:163]
        for profile in SPIController.PROFILES:
            with self.subTest(profile=profile):
                self.make(profile)
                self.flash = W25Q128JV(image)
                self.controller.devices[0] = self.flash
                for value in (3, start >> 16, (start >> 8) & 255, start & 255):
                    self.exchange(value)
                incoming = bytearray()
                for _ in range(len(expected)):
                    self.exchange(0)
                    incoming.append(self.controller.rx_value & 255)
                self.assertEqual(incoming, expected)
                # The candidate definitions differ at release. A byte-final
                # release clocks one more real byte; wide-final does not.
                self.exchange(0, held=False)
                wire = self.flash.transactions[-1]
                count = len(expected) + int(profile == 'byte-final')
                self.assertEqual(wire['tx'], [3, 255, 255, 119] + [0] * count)
                self.assertEqual(wire['rx'][4:304], list(expected))
                self.assertTrue(wire['complete'])
                self.assertIs(self.flash.image, image)

    def test_jedec_overrun_latches_error_instead_of_inventing_more_id_bytes(self):
        self.make()
        for value in (0x9F, 0, 0, 0):
            self.exchange(value)
        self.assertEqual(self.controller.rx_value, 0xFFEF4018)
        self.exchange(0)
        self.assertFalse(self.controller.transfers[-1]['complete'])
        with self.assertRaises(SPIError):
            self.controller.read(self.base, 4)
        self.assertTrue(self.flash.selected)

    def test_clock_and_cs_edges_advance_from_scheduled_time_not_status_reads(self):
        self.make(bit_ticks=3)
        self.request(0x9F000000)
        self.assertTrue(self.flash.selected)
        self.assertEqual(self.flash.transactions[-1]['tx'], [])
        for _ in range(100):
            self.assertEqual(self.controller.read(self.base, 4), 1 << 11)
        self.clock.advance(23)
        self.assertEqual(self.flash.transactions[-1]['tx'], [])
        self.clock.advance(1)
        self.assertEqual(self.flash.transactions[-1]['tx'], [0x9F])
        self.assertEqual(self.controller.read(self.base, 4), 13 << 11)
        self.request(0, held=False)
        self.clock.advance(23)
        self.assertTrue(self.flash.selected)
        self.clock.advance(1)
        self.assertFalse(self.flash.selected)
        self.assertEqual(self.flash.transactions[-1]['tx'], [0x9F, 0])
        self.assertEqual(self.controller.read(self.base, 4), 0)

    def test_wide_profile_shifts_both_msb_bytes_and_cs_only_adds_no_clocks(self):
        self.make('wide-final')
        self.request(0x03120000, wide=True)
        self.clock.advance(8)
        self.assertEqual(self.flash.transactions[-1]['tx'], [3])
        self.assertTrue(self.controller.busy)
        self.clock.advance(8)
        self.assertEqual(self.flash.transactions[-1]['tx'], [3, 0x12])
        self.request(0x34560000, wide=True)
        self.clock.advance(16)
        self.exchange(0)
        self.assertEqual(self.controller.rx_value & 0xFF, self.image[0x123456])
        before = self.controller.rx_value, self.controller.rx_known_mask
        self.request(0xABCDEF01, held=False)
        self.assertTrue(self.flash.selected)
        self.clock.advance(1)
        self.assertFalse(self.flash.selected)
        self.assertEqual((self.controller.rx_value, self.controller.rx_known_mask), before)
        self.assertEqual(self.flash.transactions[-1]['tx'], [3, 0x12, 0x34, 0x56, 0])

    def test_byte_profile_always_clocks_one_msb_including_final_write(self):
        self.make()
        self.request(0x03123456, wide=True)
        self.clock.advance(8)
        self.assertEqual(self.flash.transactions[-1]['tx'], [3])
        for value in (0x12, 0x34, 0x56):
            self.exchange(value)
        self.exchange(0, held=False)
        self.assertEqual(self.controller.read(self.base + 4, 4), 0xFFFFFF56)
        self.assertEqual(self.flash.transactions[-1]['tx'], [3, 0x12, 0x34, 0x56, 0])
        self.assertIs(self.flash.image, self.image)

    def test_unknown_bits_are_preserved_until_known_data_shifts_them_out(self):
        self.make(pull=None)
        for value in (3, 0, 0, 0xF0):
            self.exchange(value)
        self.assertEqual(self.controller.rx_known_mask, 0)
        self.exchange(0)
        self.assertEqual(self.controller.rx_known_mask, 0xFF)
        self.assertEqual(self.controller.rx_value & 0xFF, 0xF0)
        with self.assertRaisesRegex(SPIError, 'unknown MISO/reset bits'):
            self.controller.read(self.base + 4, 4)
        for _ in range(3):
            self.exchange(0)
        self.assertEqual(self.controller.read(self.base + 4, 4), 0xF0F1F2F3)

    def test_disconnected_slots_use_explicit_pull_and_never_an_endpoint_id(self):
        for profile in SPIController.PROFILES:
            for pull, expected in ((1, 0xFFFFFFFF), (0, 0)):
                with self.subTest(profile=profile, pull=pull):
                    self.make(profile, pull=pull)
                    self.assertEqual(self.jedec(device=1), expected)
                    self.assertEqual(self.flash.transactions, [])
        self.make(pull=None)
        with self.assertRaisesRegex(SPIError, 'unknown MISO/reset bits'):
            self.jedec(device=2)
        self.assertEqual(self.controller.rx_known_mask, 0)

    def test_busy_conflicts_and_device_changes_fail_without_corrupting_transfer(self):
        self.make()
        self.request(0x05000000)
        for address, size, value in ((self.base + 8, 4, 0x23),
                                     (self.base + 0x14, 4, 0),
                                     (self.base + 0x18, 2, 0)):
            with self.assertRaisesRegex(SPIError, 'active'):
                self.controller.write(address, size, value)
        with self.assertRaisesRegex(SPIError, 'active'):
            self.controller.read(self.base + 4, 4)
        self.clock.advance(8)
        with self.assertRaisesRegex(SPIError, 'device change'):
            self.controller.write(self.base + 8, 4, 0x423)
        self.assertEqual(self.controller.control, 0x23)
        self.exchange(0, held=False)
        self.controller.write(self.base + 8, 4, 0x423)
        self.assertEqual(self.controller.control, 0x423)
        self.assertEqual(self.flash.transactions[-1]['tx'], [5, 0])

    def test_endpoint_errors_latch_and_surface_on_next_status_poll(self):
        self.make()
        self.request(0x02000000)
        self.clock.advance(8)
        self.assertFalse(self.controller.busy)
        self.assertFalse(self.controller.transfers[-1]['complete'])
        self.assertIn('read-only', self.controller.transfers[-1]['error'])
        with self.assertRaisesRegex(SPIError, 'read-only'):
            self.controller.read(self.base, 4)
        with self.assertRaisesRegex(SPIError, 'read-only'):
            self.controller.write(self.base + 8, 4, 0x22)
        self.assertIs(self.flash.image, self.image)

    def test_reset_mapping_configuration_widths_and_unknown_controls_remain_strict(self):
        self.make()
        with self.assertRaisesRegex(SPIError, 'reset values'):
            self.controller.window(0)
        for control, message in ((4, 'unmodeled SPI control bits'),
                                 (0x1002, 'unmodeled SPI control bits'),
                                 (0xC23, 'device 3')):
            with self.assertRaisesRegex(SPIError, message):
                self.controller.write(self.base + 8, 4, control)
        with self.assertRaisesRegex(SPIError, 'unmodeled SPI access'):
            self.controller.write(self.base + 0x14, 2, 0x9F)
        with self.assertRaisesRegex(SPIError, 'does not fit'):
            self.controller.write(self.base + 0x14, 4, -1)
        with self.assertRaisesRegex(SPIError, 'receive data unavailable'):
            self.controller.read(self.base + 4, 4)
        self.make('wide-final')
        with self.assertRaisesRegex(SPIError, 'without an asserted CS'):
            self.request(0, held=False)

    def test_profile_validation_does_not_silently_enable_unknown_modes(self):
        with self.assertRaises(ValueError):
            SPIController(profile='automatic')
        with self.assertRaises(ValueError):
            SPIController(profile='byte-final')
        with self.assertRaises(ValueError):
            SPIController(miso_pull=2)
        with self.assertRaises(ValueError):
            SPIController(bit_ticks=0)


if __name__ == '__main__':
    unittest.main()
