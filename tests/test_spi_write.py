"""Program/erase model for the W25Q128JV in explicit ``writable`` mode.

These tests cover the optional, datasheet-faithful write path used to exercise
a flash write in emulation only. Default (immutable) behavior is re-asserted so
the strict read-only boundary is preserved when ``writable`` is not requested.
All values follow Winbond W25Q128JV Rev G sections 8.2.12-8.2.19.
"""
import heapq
import unittest

from spi import SPIController, SPIError, W25Q128JV


def _pattern_image() -> bytes:
    return bytes(range(256)) * (W25Q128JV.SIZE // 256)


class WritableEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.image = _pattern_image()

    def make(self, **kwargs):
        return W25Q128JV(self.image, writable=True, **kwargs)

    def frame(self, flash, data):
        flash.select()
        try:
            return [flash.exchange(value) for value in data]
        finally:
            flash.deselect()

    def wren(self, flash):
        flash.select()
        flash.exchange(0x06)
        flash.deselect()

    # --- default immutability is preserved -----------------------------------

    def test_default_endpoint_stays_immutable_and_faults_on_program(self):
        flash = W25Q128JV(self.image)  # writable defaults False
        self.assertIsInstance(flash.image, bytes)
        flash.select()
        flash.exchange(0x06)
        flash.deselect()
        flash.select()
        with self.assertRaises(SPIError):
            flash.exchange(0x02)  # Page Program rejected even with WREN latched.
        flash.deselect()
        self.assertIs(flash.image, self.image)

    def test_writable_mode_copies_array_and_leaves_source_untouched(self):
        flash = self.make()
        self.assertIsInstance(flash.image, bytearray)
        self.assertIsNot(flash.image, self.image)
        self.wren(flash)
        self.frame(flash, [0x20, 0, 0, 0])  # erase sector 0
        self.assertEqual(bytes(flash.image[:16]), b'\xFF' * 16)
        self.assertEqual(self.image[:16], bytes(range(16)))  # class image intact

    # --- write-enable latch gating -------------------------------------------

    def test_program_without_write_enable_faults(self):
        flash = self.make()
        flash.select()
        with self.assertRaises(SPIError):
            flash.exchange(0x02)
        flash.deselect()

    def test_write_enable_latch_self_clears_after_each_program(self):
        flash = self.make()
        self.wren(flash)
        self.assertTrue(flash.write_enabled)
        self.frame(flash, [0x02, 0x00, 0x10, 0x00, 0xA5])
        self.assertFalse(flash.write_enabled)  # WEL cleared on completion
        flash.select()
        with self.assertRaises(SPIError):
            flash.exchange(0x02)  # a second write needs a fresh WREN
        flash.deselect()

    # --- page program semantics ----------------------------------------------

    def test_page_program_only_clears_bits(self):
        flash = self.make()
        # Byte at 0x001000 holds pattern value 0x00; use a byte that is 0x3C.
        addr = 0x00103C  # value 0x3C in the repeating pattern
        self.assertEqual(flash.image[addr], 0x3C)
        self.wren(flash)
        self.frame(flash, [0x02, 0x00, 0x10, 0x3C, 0x0F])
        self.assertEqual(flash.image[addr], 0x3C & 0x0F)  # NOR AND, not 0x0F
        self.assertEqual(flash.writes[-1], {'op': 'program', 'address': addr, 'length': 1})

    def test_page_program_wraps_within_256_byte_page(self):
        flash = self.make()
        self.wren(flash)
        # Start two bytes before a page boundary with four data bytes.
        self.frame(flash, [0x02, 0x00, 0x00, 0xFE, 0x00, 0x00, 0x00, 0x00])
        self.assertEqual(flash.image[0x0000FE], 0x00)
        self.assertEqual(flash.image[0x0000FF], 0x00)
        # Wrapped back to the start of the SAME page, not into the next page.
        self.assertEqual(flash.image[0x000000], 0x00)
        self.assertEqual(flash.image[0x000001], 0x00)
        self.assertEqual(flash.image[0x000100], 0x00)  # next page untouched (was 0)
        self.assertEqual(flash.image[0x0001FE], 0xFE)  # unrelated byte intact

    # --- erase granularities and alignment -----------------------------------

    def test_sector_erase_is_4kib_aligned(self):
        flash = self.make()
        self.wren(flash)
        self.frame(flash, [0x20, 0x01, 0xAB, 0xCD])  # address 0x01ABCD, inside 0x01A000
        base = 0x01A000
        self.assertEqual(flash.image[base - 1], (base - 1) & 0xFF)  # below untouched
        self.assertEqual(bytes(flash.image[base:base + 0x1000]), b'\xFF' * 0x1000)
        self.assertEqual(flash.image[base + 0x1000], 0x00)  # above untouched
        self.assertEqual(flash.writes[-1],
                         {'op': 'erase', 'address': base, 'length': 0x1000})

    def test_block_erase_sizes(self):
        for opcode, size in ((0x52, 0x8000), (0xD8, 0x10000)):
            with self.subTest(opcode=opcode):
                flash = self.make()
                self.wren(flash)
                self.frame(flash, [opcode, 0x02, 0x00, 0x00])  # address 0x020000
                base = 0x020000 & ~(size - 1)
                self.assertEqual(bytes(flash.image[base:base + size]), b'\xFF' * size)
                self.assertEqual(flash.image[base + size], (base + size) & 0xFF)

    def test_chip_erase_clears_entire_array(self):
        for opcode in (0xC7, 0x60):
            with self.subTest(opcode=opcode):
                flash = self.make()
                self.wren(flash)
                self.frame(flash, [opcode])
                self.assertEqual(flash.image.count(0xFF), W25Q128JV.SIZE)
                self.assertEqual(flash.writes[-1]['op'], 'chip-erase')

    # --- incomplete framing is ignored, not silently committed ---------------

    def test_erase_with_incomplete_address_does_not_commit(self):
        flash = self.make()
        self.wren(flash)
        self.frame(flash, [0x20, 0x01])  # CS rises after one address byte
        self.assertEqual(flash.writes, [])
        self.assertEqual(flash.image[:4], self.image[:4])  # array untouched
        self.assertTrue(flash.write_enabled)  # WEL stays set (no op executed)

    def test_chip_erase_with_stray_clocks_does_not_commit(self):
        flash = self.make()
        self.wren(flash)
        self.frame(flash, [0xC7, 0x00])  # Chip Erase must end at its opcode byte
        self.assertEqual(flash.writes, [])
        self.assertNotEqual(flash.image.count(0xFF), W25Q128JV.SIZE)

    def test_program_with_only_partial_address_does_not_commit(self):
        flash = self.make()
        self.wren(flash)
        self.frame(flash, [0x02, 0x00, 0x10])  # CS rises after two address bytes
        self.assertEqual(flash.writes, [])
        self.assertEqual(flash.image[:0x2000], self.image[:0x2000])

    def test_program_with_address_but_no_data_does_not_commit(self):
        flash = self.make()
        self.wren(flash)
        self.frame(flash, [0x02, 0x00, 0x10, 0x00])  # full address, zero data bytes
        self.assertEqual(flash.writes, [])
        self.assertEqual(flash.image[:0x2000], self.image[:0x2000])

    # --- busy/WIP polling ------------------------------------------------------

    def test_wip_bit_asserts_then_clears_across_status_polls(self):
        flash = self.make(wip_read_latency=2)
        self.wren(flash)
        self.frame(flash, [0x20, 0, 0, 0])
        # Two polls read WIP set, the third reads ready; WEL stays clear.
        self.assertEqual(self.frame(flash, [0x05, 0]), [None, 0x01])
        self.assertEqual(self.frame(flash, [0x05, 0]), [None, 0x01])
        self.assertEqual(self.frame(flash, [0x05, 0]), [None, 0x00])

    def test_default_latency_reports_ready_immediately(self):
        flash = self.make()  # wip_read_latency = 0
        self.wren(flash)
        self.frame(flash, [0x20, 0, 0, 0])
        self.assertEqual(self.frame(flash, [0x05, 0]), [None, 0x00])  # never busy

    # --- strictness retained for unmodeled writes ----------------------------

    def test_unsupported_write_commands_still_fault_in_writable_mode(self):
        for opcode in (0x01, 0x31, 0x32, 0x42, 0x44, 0x36):
            with self.subTest(opcode=opcode):
                flash = self.make()
                self.wren(flash)
                flash.select()
                with self.assertRaises(SPIError):
                    flash.exchange(opcode)
                flash.deselect()

    def test_reads_are_unaffected_by_writable_mode(self):
        flash = self.make()
        self.assertEqual(self.frame(flash, [0x9F, 0, 0, 0]), [None, 0xEF, 0x40, 0x18])
        self.assertEqual(self.frame(flash, [0x03, 0x00, 0x00, 0x05, 0]),
                         [None, None, None, None, 0x05])


class _Clock:
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


class ControllerWritePathTests(unittest.TestCase):
    """Drive erase+program+readback through the AXON byte-final serializer."""

    @classmethod
    def setUpClass(cls):
        cls.image = _pattern_image()

    def setUp(self):
        self.clock = _Clock()
        self.flash = W25Q128JV(self.image, writable=True)
        self.controller = SPIController(schedule=self.clock.schedule,
                                        profile='byte-final', miso_pull=1,
                                        devices={0: self.flash})
        self.base = self.controller.base

    def tx(self, byte, *, held=True):
        control = (0 << 10) | 2 | int(held) | 0x20  # byte-final, device 0
        self.controller.write(self.base + 8, 4, control)
        self.controller.write(self.base + 0x14, 4, byte << 24)
        self.clock.advance(16)

    def op(self, opcode, payload):
        """CS-held opcode + payload, final byte releases CS."""
        seq = [opcode, *payload]
        for value in seq[:-1]:
            self.tx(value, held=True)
        self.tx(seq[-1], held=False)

    def test_erase_then_program_lands_in_array_and_reads_back(self):
        # Write-enable, erase sector 0, write-enable, program four bytes at 0.
        self.tx(0x06, held=False)
        self.assertTrue(self.flash.write_enabled)
        self.op(0x20, [0x00, 0x00, 0x00])
        self.assertEqual(bytes(self.flash.image[:4]), b'\xFF' * 4)

        self.tx(0x06, held=False)
        self.op(0x02, [0x00, 0x00, 0x00, 0xDE, 0xAD, 0xBE, 0xEF])
        self.assertEqual(bytes(self.flash.image[:4]), b'\xDE\xAD\xBE\xEF')
        self.assertEqual([w['op'] for w in self.flash.writes], ['erase', 'program'])

        # Read the four programmed bytes back over the same controller path.
        readback = bytearray()
        for value in (0x03, 0x00, 0x00, 0x00):
            self.tx(value, held=True)
        for _ in range(4):
            self.tx(0x00, held=True)
            readback.append(self.controller.read(self.base + 4, 4) & 0xFF)
        self.tx(0x00, held=False)
        self.assertEqual(bytes(readback), b'\xDE\xAD\xBE\xEF')

    def test_controller_program_without_wren_latches_endpoint_error(self):
        # No WREN: the endpoint faults, the controller latches it for the poll.
        self.tx(0x02, held=True)
        with self.assertRaises(SPIError):
            self.controller.read(self.base, 4)


if __name__ == '__main__':
    unittest.main()
