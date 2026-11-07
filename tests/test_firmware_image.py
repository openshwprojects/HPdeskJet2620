"""Offline extraction checks; these do not load sections into the emulator."""
import hashlib
from contextlib import contextmanager
from pathlib import Path
import struct
import unittest
from uuid import uuid4

from analysis.firmware_image import FirmwareImage, ImageError, unpack_lzss, write_extracted
from analysis.uart_decode import unpack


DUMP = Path(__file__).resolve().parents[1] / 'dump.bin'


@contextmanager
def extraction_test_directory():
    # tempfile's owner-only Windows ACL excludes the desktop tool sandbox.
    # Create one uniquely named workspace directory with inherited permissions.
    directory = DUMP.parent / f'.extract-test-{uuid4().hex}'
    if not directory.resolve().is_relative_to(DUMP.parent):
        raise AssertionError('temporary extraction path is outside the workspace')
    directory.mkdir()
    try:
        yield directory
    finally:
        for child in directory.iterdir():
            child.unlink()
        directory.rmdir()


class LzssExtractionTests(unittest.TestCase):
    def test_literal_overlapping_reference_and_initial_zero_history(self):
        self.assertEqual(unpack_lzss(bytes.fromhex('07 41 42 43 ee f6'), 12), b'ABC' * 4)
        self.assertEqual(unpack_lzss(bytes.fromhex('00 00 00'), 3), bytes(3))

    def test_truncation_and_declared_size_mismatches_are_rejected(self):
        for packed, size in ((b'\xff', 1), (b'\0\0', 3), (b'\x01A', 0), (b'\x01A', 2)):
            with self.subTest(packed=packed, size=size):
                with self.assertRaises(ImageError):
                    unpack_lzss(packed, size)


class FirmwareImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from analysis.uart_selftest import load_default_dump
        cls.raw = load_default_dump() or DUMP.read_bytes()
        cls.image = FirmwareImage(cls.raw)

    def test_header_and_section_links_expose_main_code(self):
        self.assertEqual(len(self.image.sections), 133)
        self.assertEqual(self.image.entry, 0xa04e2e05)
        source = self.image.by_name['.cromtext']
        target = self.image.by_descriptor[source.related_section]
        self.assertEqual((source.address, source.size), (0x791160, 0x39faf7))
        self.assertEqual((target.name, target.address, target.size), ('.text', 0xa00150d0, 0x4e2e5c))

    def test_main_text_matches_independently_extracted_bytes(self):
        text = self.image.extract('.text')
        self.assertEqual(hashlib.sha256(text).hexdigest(),
                         'ea48cad6bf6c98a75e1012037da50928a7029963cc1f3cc3507ca269602a2da5')
        self.assertEqual(text[:4], bytes.fromhex('47704770'))

    def test_boot_code_matches_existing_decoder_and_data_contains_nvm_descriptor(self):
        self.assertEqual(self.image.extract('.load_text'), unpack(self.raw[0x39ec:0xc9db]))
        rodata = self.image.extract('.load_rodata')
        offset = 0xa0011934 - self.image.by_name['.load_rodata'].address
        self.assertEqual(rodata[offset:offset + 8], bytes.fromhex('04090200a0011996'))

    def test_every_scatter_operation_matches_its_declared_destination(self):
        for operation in self.image.operations:
            with self.subTest(section=operation.section):
                self.assertEqual(len(self.image.extract(operation.section)), operation.expanded_size)

    def test_raw_relocations_and_zero_fill_are_distinct(self):
        self.assertEqual(self.image.extract('.nosi_text'), self.raw[0x32a0:0x39ec])
        self.assertEqual(self.image.extract('.revstr_rodata'), self.raw[0x2040:0x206c])
        self.assertEqual(self.image.extract('.nosd_bss'), bytes(0xae0))
        with self.assertRaises(ImageError):
            self.image.extract('.stack')

    def test_invalid_headers_and_cyclic_section_lists_are_rejected(self):
        damaged = bytearray(self.raw)
        for offset, value in ((0x20034, 0), (0x2006c, 0), (0x203a8, 0x203a8),
                              (0x203b4, 0x4000001)):
            orig = damaged[offset:offset + 4]
            struct.pack_into('>I', damaged, offset, value)
            try:
                with self.subTest(offset=hex(offset)):
                    with self.assertRaises(ImageError):
                        FirmwareImage(damaged)
            finally:
                damaged[offset:offset + 4] = orig

    def test_extraction_preserves_original_flash(self):
        self.image.extract('.text')
        self.assertEqual(hashlib.sha256(self.image.image).hexdigest(),
                         'b791436cf9152785fb5d3f6954aa5949d4ae7c7458af0e2031b9a41f5534c496')
        self.assertEqual(DUMP.read_bytes(), self.raw)

    def test_output_source_collisions_are_rejected_before_writes(self):
        for filename in ('text-a00150d0.bin', 'manifest.json'):
            with extraction_test_directory() as directory, self.subTest(filename=filename):
                source = directory / filename
                source.write_bytes(b'source must remain unchanged')
                with self.assertRaises(ImageError):
                    write_extracted(self.image, directory, ['.text'], source)
                self.assertEqual(source.read_bytes(), b'source must remain unchanged')
                self.assertEqual(list(directory.iterdir()), [source])

    def test_duplicate_output_paths_are_rejected(self):
        with extraction_test_directory() as directory:
            with self.assertRaises(ImageError):
                write_extracted(self.image, directory, ['.text', '.text'], DUMP)
            self.assertEqual(list(directory.iterdir()), [])


class CFirmwareCompilationTests(unittest.TestCase):
    """Test freestanding C compiler setup, compilation, and emulation for ARMv5 big-endian."""

    def test_find_arm_c_compiler(self) -> None:
        from custom_firmware import find_arm_c_compiler
        compiler = find_arm_c_compiler()
        self.assertIsNotNone(compiler, "ARM C compiler (zig, clang, or arm-none-eabi-gcc) not found on PATH")

    def test_compile_c_firmware_binary(self) -> None:
        from custom_firmware import compile_c_firmware
        c_bin, entry = compile_c_firmware('firmware.c', load_address=0xA0020000)
        self.assertGreater(len(c_bin), 0)
        self.assertEqual(entry, 0xA0020000)
        # Check first word is ARM branch or instruction
        self.assertEqual(len(c_bin) % 4, 0)

    def test_c_firmware_execution_native_observer(self) -> None:
        """Verify compiled C firmware boots and runs under C native observer."""
        from analysis.uart_selftest import load_default_dump
        from custom_firmware import build_c_stage2_container_image
        from emulator import Emulator

        dump = load_default_dump() or DUMP.read_bytes()
        img, entry = build_c_stage2_container_image(dump, 'firmware.c')
        emu = Emulator(img, serial_panel_inputs=0x3FFFF, spi_profile='byte-final', native_observer=True)
        emu.board.uart_frame_ticks = 1
        emu.run(max_instructions=3_600_000, stop='budget')

        uart_bytes = bytes(emu.uart)
        self.assertIn(b'[Boot] calling (*0xa0020000)()', uart_bytes)
        self.assertIn(b'[C-Firmware] Bare-metal ARMv5TE Big-Endian C Payload', uart_bytes)
        self.assertIn(b'[C-Firmware] Front Panel Shift Register: Sampled 0x35', uart_bytes)

        dram = bytes(emu.memory._dram_mv)
        sig = struct.unpack_from('>I', dram, 0x00000104)[0]
        btns = struct.unpack_from('>I', dram, 0x00000108)[0]
        magic = struct.unpack_from('>I', dram, 0x0000010C)[0]
        self.assertEqual(sig, 0x35)
        self.assertEqual(btns, 0x3FFFF)
        self.assertEqual(magic, 0xC0DE0035)

    def test_c_firmware_execution_python_observer(self) -> None:
        """Verify compiled C firmware boots and runs under pure Python observer (100% parity)."""
        import os
        from analysis.uart_selftest import load_default_dump
        from custom_firmware import build_c_custom_firmware_image, build_c_stage2_container_image
        from emulator import Emulator

        if os.environ.get('FULL_STAGE2_PARITY'):
            dump = load_default_dump() or DUMP.read_bytes()
            img, entry = build_c_stage2_container_image(dump, 'firmware.c')
            emu = Emulator(img, serial_panel_inputs=0x3FFFF, spi_profile='byte-final', native_observer=False)
            emu.run(max_instructions=3_600_000, stop='budget')
            self.assertIn(b'[Boot] calling (*0xa0020000)()', bytes(emu.uart))
        else:
            img, entry = build_c_custom_firmware_image('firmware.c')
            emu = Emulator(img, serial_panel_inputs=0x3FFFF, native_observer=False)
            emu.board.uart_frame_ticks = 1
            emu.run(max_instructions=15_000, stop='budget')

        uart_bytes = bytes(emu.uart)
        self.assertIn(b'[C-Firmware] Bare-metal ARMv5TE Big-Endian C Payload', uart_bytes)
        self.assertIn(b'[C-Firmware] Front Panel Shift Register: Sampled 0x35', uart_bytes)

        dram = bytes(emu.memory._dram_mv)
        sig = struct.unpack_from('>I', dram, 0x00000104)[0]
        btns = struct.unpack_from('>I', dram, 0x00000108)[0]
        magic = struct.unpack_from('>I', dram, 0x0000010C)[0]
        self.assertEqual(sig, 0x35)
        self.assertEqual(btns, 0x3FFFF)
        self.assertEqual(magic, 0xC0DE0035)

    def test_sample_c_firmware_compilation(self) -> None:
        """Verify sample C firmware compiles cleanly for both Stage-2 and standalone entries."""
        from custom_firmware import compile_c_firmware
        bin_s2, entry_s2 = compile_c_firmware('sample_firmware.c', load_address=0xA0020000)
        self.assertGreater(len(bin_s2), 0)
        self.assertEqual(entry_s2, 0xA0020000)
        self.assertEqual(len(bin_s2) % 4, 0)

        bin_rom, entry_rom = compile_c_firmware('sample_firmware.c', load_address=0x00002070)
        self.assertGreater(len(bin_rom), 0)
        self.assertEqual(entry_rom, 0x00002070)
        self.assertEqual(len(bin_rom) % 4, 0)

    def test_sample_c_firmware_standalone_native_observer(self) -> None:
        """Verify sample C firmware boots directly from ROM reset 0x00000000 under C native observer."""
        from custom_firmware import build_c_custom_firmware_image
        from emulator import Emulator

        img, entry = build_c_custom_firmware_image('sample_firmware.c')
        self.assertEqual(entry, 0x00002070)
        emu = Emulator(img, serial_panel_inputs=0x3FFFF, native_observer=True)
        emu.board.uart_frame_ticks = 1
        emu.run(max_instructions=20_000, stop='budget')

        uart_bytes = bytes(emu.uart)
        self.assertIn(b'Hello World from Freestanding C Sample Firmware!', uart_bytes)
        self.assertIn(b'[Sample-FW] Front Panel Shift Register: Sampled Signature 0x00000035', uart_bytes)
        self.assertIn(b'[Sample-FW] DRAM Mailbox: Initialized at 0x20000100 with Magic 0xC0DE0035', uart_bytes)

        dram = bytes(emu.memory._dram_mv)
        sig = struct.unpack_from('>I', dram, 0x00000104)[0]
        btns = struct.unpack_from('>I', dram, 0x00000108)[0]
        magic = struct.unpack_from('>I', dram, 0x0000010C)[0]
        self.assertEqual(sig, 0x35)
        self.assertEqual(btns, 0x3FFFF)
        self.assertEqual(magic, 0xC0DE0035)

    def test_sample_c_firmware_standalone_python_observer(self) -> None:
        """Verify sample C firmware boots directly from ROM reset under Python observer (100% parity)."""
        from custom_firmware import build_c_custom_firmware_image
        from emulator import Emulator

        img, entry = build_c_custom_firmware_image('sample_firmware.c')
        self.assertEqual(entry, 0x00002070)
        emu = Emulator(img, serial_panel_inputs=0x3FFFF, native_observer=False)
        emu.board.uart_frame_ticks = 1
        emu.run(max_instructions=20_000, stop='budget')

        uart_bytes = bytes(emu.uart)
        self.assertIn(b'Hello World from Freestanding C Sample Firmware!', uart_bytes)
        self.assertIn(b'[Sample-FW] Front Panel Shift Register: Sampled Signature 0x00000035', uart_bytes)
        self.assertIn(b'[Sample-FW] DRAM Mailbox: Initialized at 0x20000100 with Magic 0xC0DE0035', uart_bytes)

        dram = bytes(emu.memory._dram_mv)
        sig = struct.unpack_from('>I', dram, 0x00000104)[0]
        btns = struct.unpack_from('>I', dram, 0x00000108)[0]
        magic = struct.unpack_from('>I', dram, 0x0000010C)[0]
        self.assertEqual(sig, 0x35)
        self.assertEqual(btns, 0x3FFFF)
        self.assertEqual(magic, 0xC0DE0035)

    def test_sample_c_firmware_interactive_cli(self) -> None:
        """Verify sample C firmware interactive command shell under both Native and Python observers."""
        from custom_firmware import build_c_custom_firmware_image
        from emulator import Emulator
        from i2c import EEPROM

        commands = b'help\rping\rmailbox\rbuttons\rleds\rtimer\rspi\ri2c\rink\recho HelloInteractive\r'
        img, entry = build_c_custom_firmware_image('sample_firmware.c')
        factory_eeprom = EEPROM.create_factory_image()
        for native in (True, False):
            with self.subTest(native_observer=native):
                emu = Emulator(
                    img,
                    serial_panel_inputs=0x3FFFF,
                    spi_profile='byte-final',
                    eeprom=factory_eeprom,
                    native_observer=native,
                )
                emu.board.uart_frame_ticks = 1
                emu.board.queue_rx(commands)
                emu.run(max_instructions=55_000, stop='budget')

                uart_text = bytes(emu.uart).decode('latin1', errors='replace')
                self.assertIn('[Sample-FW] Available Commands:', uart_text)
                self.assertIn('[Sample-FW] pong', uart_text)
                self.assertIn('Signature: 0x00000035', uart_text)
                self.assertIn('Buttons:   0x0003FFFF', uart_text)
                self.assertIn('Status:    0xC0DE0035', uart_text)
                self.assertIn('[Sample-FW] Buttons: 0x0003FFFF', uart_text)
                self.assertIn('[Sample-FW] Toggled LEDs: Power=OFF, WiFi=OFF', uart_text)
                self.assertIn('[Sample-FW] Timer Ticks:', uart_text)
                self.assertIn('[Sample-FW] SPI Flash JEDEC ID: 0x00EF4018, Word0: 0xE59FF018', uart_text)
                self.assertIn('[Sample-FW] I2C EEPROM(0x50): ver=0x00000040 board=0x000068BF, Undercroft(0x1F): status=0x00000000', uart_text)
                self.assertIn('[Sample-FW] MICCI MCASIC=0x00008014, Carriage=0x00000801, PenStatus=0x00008100', uart_text)
                self.assertIn('[Sample-FW] Echo: HelloInteractive', uart_text)
                self.assertIn('DJ2620> ', uart_text)


if __name__ == '__main__':
    unittest.main()
