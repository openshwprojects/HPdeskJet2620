"""I2C bus timing and EEPROM behavior without firmware-address shortcuts."""
import unittest

from hardware import BootHardware, HardwareError
from i2c import EEPROM


BASE = 0xBD065800


class I2CTests(unittest.TestCase):
    def setUp(self):
        self.board = BootHardware()
        self.image = bytes(range(256)) * 256
        self.eeprom = EEPROM(self.image, write_cycle_ticks=100)
        self.board.i2c.devices[0x50] = self.eeprom

    def write(self, offset, value):
        self.board.write(BASE + offset, 2, value)

    def read(self, offset):
        return self.board.read(BASE + offset, 2)

    def address(self, offset, *, read, control):
        self.write(0, control)
        self.write(4, offset >> 8)
        self.write(6, offset & 0xFF)
        self.write(2, 0xA1 if read else 0xA0)

    def complete(self):
        self.board.advance(self.board.i2c.byte_ticks)

    def test_single_byte_read_is_scheduled_and_uses_sixteen_bit_address(self):
        self.address(0x1234, read=True, control=0x201)
        for _ in range(100):
            self.assertTrue(self.read(0) & 0x80)
        self.board.advance(self.board.i2c.byte_ticks - 1)
        self.assertTrue(self.read(0) & 0x80)
        self.board.advance()
        self.assertFalse(self.read(0) & 0x80)
        self.assertEqual(self.read(8), 0x34)
        self.assertFalse(self.read(0) & 0x80)
        self.assertEqual(len(self.board.i2c.transactions), 1)

    def test_read_tail_latches_continuation_at_byte_start(self):
        self.address(0x1234, read=True, control=0x203)
        self.complete()
        self.assertEqual(self.read(8), 0x34)
        self.assertTrue(self.read(0) & 0x80)
        self.complete()
        # The actual driver clears continue while two bytes remain, before
        # consuming the penultimate byte. The final byte must still transfer.
        self.write(0, 0x201)
        self.assertEqual(self.read(8), 0x35)
        self.assertTrue(self.read(0) & 0x80)
        self.complete()
        self.assertEqual(self.read(8), 0x36)
        self.assertFalse(self.read(0) & 0x80)
        self.assertEqual(len(self.board.i2c.transactions), 3)

    def test_streamed_writes_commit_only_after_transfer_completion(self):
        self.write(8, 0xA1)
        self.address(0x1234, read=False, control=0x205)
        self.assertEqual(self.eeprom.read(0x1234), 0x34)
        self.complete()
        self.assertEqual(self.eeprom.read(0x1234), 0x34)
        self.write(8, 0xA2)
        self.complete()
        self.write(0, 0x201)
        self.write(8, 0xA3)
        self.complete()
        self.assertTrue(self.eeprom.busy)
        self.assertEqual(bytes(self.eeprom.image[0x1234:0x1237]), b'456')
        self.board.advance(self.eeprom.write_cycle_ticks)
        self.assertFalse(self.eeprom.busy)
        self.assertEqual(bytes(self.eeprom.image[0x1234:0x1237]), b'\xA1\xA2\xA3')
        self.assertEqual(self.image[0x1234:0x1237], b'456')
        self.write(8, 0xA4)  # A staged byte cannot create a fresh transaction.
        self.complete()
        self.assertEqual(self.eeprom.read(0x1237), 0x37)
        self.assertEqual(len(self.board.i2c.transactions), 3)

    def test_unattached_addresses_remain_busy_until_reset(self):
        for slave in (0x7F, 0x51):
            with self.subTest(slave=slave):
                self.write(0, 1)
                self.write(2, (slave << 1) | 1)
                self.board.advance(100000)
                self.assertTrue(self.read(0) & 0x80)
                record = self.board.i2c.transactions[-1]
                self.assertFalse(record['ack'])
                self.assertIsNone(record['data'])
                self.write(0, 0x100)
                self.write(0, 1)
                self.assertFalse(self.read(0) & 0x80)
                self.assertTrue(record['aborted'])
                self.assertFalse(record['completed'])

    def test_reset_cancels_an_inflight_eeprom_write(self):
        self.write(8, 0xA1)
        self.address(0x1234, read=False, control=0x201)
        self.board.advance(1)
        self.write(0, 0x100)
        self.complete()
        self.assertEqual(self.eeprom.read(0x1234), 0x34)
        self.assertFalse(self.read(0) & 0x80)
        self.assertTrue(self.board.i2c.transactions[-1]['aborted'])

    def test_busy_status_is_read_only_and_configuration_retains_values(self):
        self.write(0, 0x81)
        self.assertEqual(self.read(0), 1)
        self.write(0xC, 449)
        self.assertEqual(self.read(0xC), 449)
        with self.assertRaises(HardwareError):
            self.board.read(BASE, 4)
        with self.assertRaises(HardwareError):
            self.read(0x10)

    def test_page_write_wraps_and_device_nacks_during_programming(self):
        self.write(8, 0xA1)
        self.address(0x127F, read=False, control=0x205)
        self.complete()
        self.write(0, 0x201)
        self.write(8, 0xA2)
        self.complete()
        self.assertTrue(self.eeprom.busy)
        self.address(0x127F, read=True, control=0x201)
        self.assertFalse(self.board.i2c.transactions[-1]['ack'])
        self.assertTrue(self.read(0) & 0x80)
        self.board.advance(self.eeprom.write_cycle_ticks)
        self.assertFalse(self.eeprom.busy)
        self.assertEqual(self.eeprom.read(0x127F), 0xA1)
        self.assertEqual(self.eeprom.read(0x1200), 0xA2)
        self.assertEqual(self.eeprom.read(0x1280), 0x80)
        self.assertTrue(self.read(0) & 0x80)  # Address NACK remains latched.
        self.write(0, 0x100)
        self.address(0x127F, read=True, control=0x201)
        self.complete()
        self.assertEqual(self.read(8), 0xA1)

    def test_device_is_absent_by_default_and_image_size_is_explicit(self):
        self.assertEqual(BootHardware().i2c.devices, {})
        with self.assertRaises(ValueError):
            EEPROM(b'')
        with self.assertRaises(ValueError):
            EEPROM(bytes(1024))

    def test_write_protect_nacks_data_without_blocking_reads(self):
        protected = EEPROM(self.image, write_cycle_ticks=100, write_protect=lambda: True)
        self.board.i2c.devices[0x50] = protected
        self.write(8, 0xA1)
        self.address(0x1234, read=False, control=0x201)
        self.board.advance(10000)
        record = self.board.i2c.transactions[-1]
        self.assertTrue(record['address_ack'])
        self.assertFalse(record['ack'])
        self.assertEqual(record['nack_phase'], 'data')
        self.assertTrue(record['write_protected'])
        self.assertFalse(record['completed'])
        self.assertTrue(self.read(0) & 0x80)
        self.assertFalse(protected.busy)
        self.assertEqual(bytes(protected.image), self.image)
        self.write(0, 0x100)
        self.address(0x1234, read=True, control=0x201)
        self.complete()
        self.assertEqual(self.read(8), 0x34)
        self.assertTrue(self.board.i2c.transactions[-1]['ack'])

    def test_protection_asserted_during_byte_prevents_programming(self):
        wc = [False]
        self.eeprom.write_protect = lambda: wc[0]
        self.write(8, 0xA1)
        self.address(0x1234, read=False, control=0x201)
        self.board.advance(1)
        wc[0] = True
        self.complete()
        self.assertFalse(self.board.i2c.transactions[-1]['ack'])
        self.assertFalse(self.eeprom.busy)
        self.assertTrue(self.read(0) & 0x80)
        self.board.advance(10000)
        self.assertEqual(bytes(self.eeprom.image), self.image)

    def test_protection_after_stop_does_not_cancel_started_programming(self):
        wc = [False]
        self.eeprom.write_protect = lambda: wc[0]
        self.write(8, 0xA1)
        self.address(0x1234, read=False, control=0x201)
        self.complete()
        self.assertTrue(self.eeprom.busy)
        self.board.advance(1)
        wc[0] = True
        self.board.advance(self.eeprom.write_cycle_ticks)
        self.assertEqual(self.eeprom.read(0x1234), 0xA1)
        self.assertEqual(self.image[0x1234], 0x34)

    def test_endpoint_programming_api_also_obeys_write_protect(self):
        protected = EEPROM(self.image, write_protect=lambda: True)
        self.assertFalse(protected.program_page(0, b'new', self.board.schedule))
        self.board.advance(10000)
        self.assertEqual(bytes(protected.image), self.image)
        self.assertFalse(protected.busy)
        with self.assertRaises(ValueError):
            EEPROM(self.image, write_protect=True)

    def test_factory_eeprom_image_checksums_and_live_bootloader_board_id(self):
        from pathlib import Path
        from emulator import Emulator

        img = EEPROM.create_factory_image()
        self.assertEqual(len(img), 65536)
        self.assertEqual(img[0x00], 0x40)
        self.assertEqual(img[0x01:0x03], b'\x1f\xe0')
        self.assertEqual(img[0x0C:0x11], b'\x68\xbf\x97\x40\x28')
        self.assertEqual(img[0x4A:0x4C], b'\xaa\x55')

        dump_path = Path(__file__).resolve().parent.parent / 'dump.bin'
        if dump_path.exists():
            from analysis.uart_selftest import load_default_dump

            def on_uart(b: int) -> None:
                if b == 0x0A and b'Unmapped Board ID (from NVM)' in emu.uart:
                    emu.reason = 'board-id'
                    emu.uc.emu_stop()

            on_uart.trigger_byte = 0x0A
            emu = Emulator(
                load_default_dump(),
                eeprom=img,
                power_button_wakeup=True,
                gpio_inputs={1: 0, 2: 0, 3: 0, 8: 1, 11: 0, 15: 1},
                serial_panel_inputs=0x3FFFF,
                serial_panel_tail_bit=1,
                spi_profile='byte-final',
                native_observer=True,
                on_uart=on_uart,
            )
            emu.run(max_instructions=2_500_000, stop='budget')
            uart_text = bytes(emu.uart).decode('latin1', errors='replace')
            self.assertIn(
                'Unmapped Board ID (from NVM)  : 0x68bf (bitpacked) M:0x06 F:0x45 N:0x1f',
                uart_text,
            )
            self.assertNotIn('MISSING! Tell the EEs', uart_text)


if __name__ == '__main__':
    unittest.main()
