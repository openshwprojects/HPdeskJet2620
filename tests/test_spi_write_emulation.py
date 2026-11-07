"""Emulator-level coverage for the opt-in SPI program/erase model.

Two things are proven here with the real firmware image in the loop:

1. The resident firmware's own flash *discovery* path, driven by actual reset
   execution, performs no program or erase even when the modeled flash is made
   physically writable -- i.e. nothing the running firmware reaches can mutate
   the 16 MiB SPI flash (analysis/update_writer.md section 2).
2. The emulator's *wired* AXON SPI controller (``board.spi``), with real
   virtual-time scheduling, can host an erase+program+read-back sequence
   against the writable endpoint. This is the emulation stand-in for the
   downloaded reflash agent that is absent from the dump: it shows the
   controller+endpoint model would faithfully host such code, with zero
   hardware risk.

The default (no ``spi_writable``) flash stays immutable, so ordinary runs keep
the strict read-only boundary.
"""
import hashlib
import pathlib
import unittest

from emulator import Emulator

DUMP = pathlib.Path(__file__).resolve().parent.parent / 'dump.bin'
DUMP_SHA256 = 'b791436cf9152785fb5d3f6954aa5949d4ae7c7458af0e2031b9a41f5534c496'


class RealDiscoveryPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dump = DUMP.read_bytes()
        assert hashlib.sha256(cls.dump).hexdigest() == DUMP_SHA256

    def test_real_discovery_path_leaves_writable_flash_untouched(self):
        # Boot the unmodified firmware with the flash made writable. Discovery
        # must still identify the part and must not issue any program/erase.
        emu = Emulator(self.dump, spi_profile='byte-final', spi_writable=True,
                       spi_wip_read_latency=3, serial_panel_inputs=0x3FFFF,
                       native_observer=True)
        result = emu.run(max_instructions=3_000_000, stop='budget')
        uart = bytes(result.uart).decode('latin1')
        self.assertIn('SPI Device 0 : W25Q128BV (16 Megabytes) (ID:0x00ef4018)', uart)
        # Anti-vacuity: the flash in this very run is genuinely writable, so an
        # empty write log is a real result, not an artifact of an immutable part.
        self.assertTrue(emu.spi_flash.writable)
        self.assertIsInstance(emu.spi_flash.image, bytearray)
        # The writable array is byte-identical to the input dump: no writer ran.
        self.assertEqual(emu.spi_flash.writes, [])
        self.assertEqual(bytes(emu.spi_flash.image), self.dump)
        self.assertEqual(hashlib.sha256(bytes(emu.spi_flash.image)).hexdigest(),
                         DUMP_SHA256)

    def test_default_spi_profile_flash_stays_immutable(self):
        emu = Emulator(self.dump, spi_profile='byte-final')  # no spi_writable
        self.assertFalse(emu.spi_flash.writable)
        self.assertIsInstance(emu.spi_flash.image, bytes)


class EmulatorControllerWriteTests(unittest.TestCase):
    """Drive the emulator's own wired controller + clock through a write."""

    @classmethod
    def setUpClass(cls):
        cls.dump = DUMP.read_bytes()

    def setUp(self):
        self.emu = Emulator(self.dump, spi_profile='byte-final', spi_writable=True)
        self.ctrl = self.emu.board.spi
        self.board = self.emu.board
        self.base = self.ctrl.base

    def tx(self, byte, *, held=True):
        # byte-final serializer: bit5 set, config bit1, bit0 = CS held, device 0.
        self.ctrl.write(self.base + 8, 4, (0 << 10) | 2 | int(held) | 0x20)
        self.ctrl.write(self.base + 0x14, 4, byte << 24)
        self.board.advance(16)  # let the scheduled transfer complete

    def op(self, opcode, *payload):
        seq = (opcode, *payload)
        for value in seq[:-1]:
            self.tx(value, held=True)
        self.tx(seq[-1], held=False)  # final byte releases CS and commits

    def read_back(self, address, length):
        for value in (0x03, (address >> 16) & 0xFF,
                      (address >> 8) & 0xFF, address & 0xFF):
            self.tx(value, held=True)
        out = bytearray()
        for _ in range(length):
            self.tx(0x00, held=True)
            out.append(self.ctrl.read(self.base + 4, 4) & 0xFF)
        self.tx(0x00, held=False)
        return bytes(out)

    def test_wired_controller_hosts_erase_then_program_then_read(self):
        self.tx(0x06, held=False)             # Write Enable
        self.op(0x20, 0x00, 0x00, 0x00)       # Sector Erase 0
        self.assertEqual(bytes(self.emu.spi_flash.image[:4]), b'\xFF' * 4)

        self.tx(0x06, held=False)             # Write Enable
        self.op(0x02, 0x00, 0x00, 0x00, 0xDE, 0xAD, 0xBE, 0xEF)  # Page Program
        self.assertEqual(bytes(self.emu.spi_flash.image[:4]), b'\xDE\xAD\xBE\xEF')

        self.assertEqual(self.read_back(0x000000, 4), b'\xDE\xAD\xBE\xEF')
        self.assertEqual([w['op'] for w in self.emu.spi_flash.writes],
                         ['erase', 'program'])
        # The rest of the array is untouched by a single-sector operation.
        self.assertEqual(bytes(self.emu.spi_flash.image[0x1000:0x1004]),
                         self.dump[0x1000:0x1004])

    def test_program_without_write_enable_latches_controller_error(self):
        from spi import SPIError
        self.tx(0x02, held=True)  # page-program opcode with WEL clear
        with self.assertRaises(SPIError):
            self.ctrl.read(self.base, 4)  # endpoint fault surfaces on next access


if __name__ == '__main__':
    unittest.main()
