"""Regression checks for execution of the supplied dump from its reset vector."""
from pathlib import Path
import unittest

import sys
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analysis.ghs_selftest import GHSChecks
from analysis.uart_decode import unpack
from analysis.uart_selftest import UartCommandSelfTest, load_default_dump
from emulator import Emulator
from cpu import CONTROL
from tests.test_native_observer_invariants import _TestImage
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1


DUMP_PATH = Path(__file__).resolve().parents[1] / "dump.bin"
EXPECTED_BANNER = (
    b"\n\r[Boot] ******************************************************\n\r"
    b"[Boot] Firmware build timestamp: Sat Jan 14, 2017 03:32:35PM\n\r"
)
UART_TX = 0xBD0B3C06


class ResetBootTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dump = load_default_dump()
        cls.uart_events = []

        def record_uart(event):
            if (event['kind'] == 'uart-transmitted'
                    or event['kind'] == 'mmio-write' and event['address'] == UART_TX):
                cls.uart_events.append(event)

        record_uart.fast_mmio = True
        cls.emulator = Emulator(cls.dump, native_observer=True, on_event=record_uart)
        cls.ram_before_boot = bytes(cls.emulator.uc.mem_read(0xA0005720, 16))
        cls.result = cls.emulator.run(max_instructions=2_000_000, stop="banner")

    @classmethod
    def tearDownClass(cls):
        cls.emulator = None

    def test_reset_reaches_exact_firmware_banner(self):
        self.assertEqual(self.result.reason, "banner", self.result.error)
        self.assertIsNone(self.result.error)
        self.assertEqual(self.result.uart, EXPECTED_BANNER)
        self.assertGreater(self.result.instructions, 100_000)
        self.assertLess(self.result.instructions, 2_000_000)

    def test_uart_bytes_come_from_firmware_halfword_stores(self):
        writes = self.emulator.uart_writes
        self.assertEqual(len(writes), len(EXPECTED_BANNER))
        self.assertEqual(bytes(write["value"] for write in writes), EXPECTED_BANNER)
        self.assertEqual({write["pc"] for write in writes}, {0xA000FEE0})
        self.assertEqual({write["address"] for write in writes}, {UART_TX})
        self.assertEqual({write["size"] for write in writes}, {2})

    def test_uart_output_follows_transmit_completion(self):
        writes = [event for event in self.uart_events if event['kind'] == 'mmio-write']
        completed = [event for event in self.uart_events if event['kind'] == 'uart-transmitted']
        self.assertEqual(bytes(event['value'] for event in completed), EXPECTED_BANNER)
        self.assertEqual(len(writes), len(completed))
        for issued, finished in zip(writes, completed):
            self.assertEqual(issued['value'] & 0xFF, finished['value'])
            self.assertGreater(finished['instruction'], issued['instruction'])
        self.assertIsNone(self.emulator.board.uart_tx_pending)

    def test_uart_milestone_keeps_instruction_clock_accounting_exact(self):
        self.assertEqual(self.emulator.board.ticks,
                         self.result.instructions * self.emulator.board.timer_step)

    def test_firmware_decompresses_executable_ram(self):
        self.assertEqual(self.ram_before_boot, bytes(16))
        expected = unpack(self.dump[0x39EC:0xC9DB])
        self.assertEqual(len(expected), 0xA001082C - 0xA0005720)
        actual = bytes(self.emulator.uc.mem_read(0xA0005720, len(expected)))
        self.assertEqual(actual, expected)

    def test_decompressed_bytes_share_all_ddr_apertures(self):
        expected = unpack(self.dump[0x39EC:0xC9DB])
        for base in (0x20000000, 0x60000000, 0xA0000000):
            for mirror in (0, 0x04000000):
                with self.subTest(base=hex(base), mirror=hex(mirror)):
                    self.assertEqual(bytes(self.emulator.uc.mem_read(
                        base + mirror + 0x5720, len(expected))), expected)

    def test_boot_does_not_modify_physical_or_disk_flash(self):
        self.assertTrue(self.emulator.memory.check_flash())
        self.assertEqual(DUMP_PATH.read_bytes(), self.dump)

    def test_first_byte_stop_preserves_leading_line_feed(self):
        emulator = Emulator(self.dump, native_observer=True)
        result = emulator.run(max_instructions=2_000_000, stop="first-byte")
        self.assertEqual(result.reason, "first-byte", result.error)
        self.assertIsNone(result.error)
        self.assertEqual(result.uart, b"\n")
        self.assertEqual(len(emulator.uart_writes), 1)
        self.assertEqual(emulator.uart_writes[0]["pc"], 0xA000FEE0)
        self.assertLess(result.instructions, self.result.instructions)

    def test_instruction_budget_stops_before_uart(self):
        emulator = Emulator(self.dump)
        result = emulator.run(max_instructions=32, stop="banner")
        self.assertEqual(result.reason, "instruction-limit", result.error)
        self.assertEqual(result.instructions, 32)
        self.assertEqual(result.uart, b"")
        self.assertEqual(emulator.uart_writes, [])
        self.assertIsNone(result.error)


class ProtectionIntegrationTests(unittest.TestCase):
    def test_denied_mmio_write_has_no_transmitter_side_effect(self):
        # Preserve the image's reset vector, but use a tiny explicit test
        # program at its reset target: STRH r0,[r1]; B .
        dump = _TestImage(load_default_dump())
        dump[0x2070:0x2078] = bytes.fromhex('e1c100b0 eafffffe')
        dump._dirty_end = 0x3000
        emulator = Emulator(dump)
        emulator.uc.reg_write(UC_ARM_REG_R0, ord('A'))
        emulator.uc.reg_write(UC_ARM_REG_R1, UART_TX)
        # Region zero covers executable flash; region one permits only
        # privileged reads in the UART page. No peripheral write is allowed.
        emulator.cp15.write((0, 6, 0, 0), 0x0000002F)
        emulator.cp15.write((0, 6, 1, 0), 0xBD0B3017)
        emulator.cp15.write((0, 5, 0, 2), 0x56)
        emulator.cp15.write((0, 5, 0, 3), 0x06)
        emulator.cp15.write(CONTROL, emulator.cp15.control | 1)
        result = emulator.run(max_instructions=8, stop='budget')
        self.assertEqual(result.reason, 'fault')
        self.assertIsNotNone(result.error)
        self.assertIsNone(emulator.board.uart_tx_pending)
        self.assertEqual(emulator.board.drain_uart(), b'')
        self.assertEqual(emulator.uart_writes, [])
        self.assertEqual(result.uart, b'')
        self.assertTrue(emulator.memory.check_flash())

    def test_dynamic_limit_update_stops_python_and_native_observers_identically(self):
        dump = _TestImage(load_default_dump())
        dump[0x2070:0x2078] = bytes.fromhex('e1c100b0 eafffffe')
        dump._dirty_end = 0x3000
        outcomes = []
        for native in (False, True):
            emu_box = [None]

            def on_uart(val: int) -> None:
                e = emu_box[0]
                if val == ord('A') and e is not None:
                    cur = e._native_observer.state.instructions if e._native_observer is not None else e.instructions
                    target = cur + 25
                    e._limit = target
                    if e._native_observer is not None:
                        e._native_observer.state.limit = target

            emu = Emulator(dump, native_observer=native, on_uart=on_uart)
            emu.uc.reg_write(UC_ARM_REG_R0, ord('A'))
            emu.uc.reg_write(UC_ARM_REG_R1, UART_TX)
            emu_box[0] = emu
            res = emu.run(max_instructions=10_000, stop='budget')
            outcomes.append((res.reason, res.instructions, res.pc, res.uart))
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(outcomes[0][0], 'instruction-limit')
        self.assertLess(outcomes[0][1], 500)

    def test_cli_full_emulation_flag_and_gpio_override(self):
        import json
        import tempfile
        from emulator import main as emu_main

        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / 'report.json'
            rc = emu_main([
                str(DUMP_PATH),
                '--full-emulation',
                '--gpio-input', '8=0',
                '--max-instructions', '64',
                '--report', str(report_path),
            ])
            self.assertEqual(rc, 1)
            report = json.loads(report_path.read_text(encoding='utf-8'))
            self.assertEqual(report['eeprom_profile'], 'factory')
            self.assertEqual(report['serial_panel_inputs'], 0x3FFFF)
            self.assertEqual(report['spi_profile'], 'byte-final')
            self.assertEqual(report['gpio_external_inputs']['8'], 0)
            self.assertEqual(report['gpio_external_inputs']['15'], 1)

    def test_load_default_dump_and_snapshot_regenerates_when_missing(self):
        import tempfile
        from unittest.mock import patch
        import analysis.uart_selftest as ust

        with tempfile.TemporaryDirectory() as tmpdir:
            missing_snap = Path(tmpdir) / 'snap_missing.pkl'
            fake_snap = {'ticks': 12345}

            def fake_gen(dump=None, save_path=None):
                Path(save_path).write_bytes(b'placeholder')
                return fake_snap

            with patch.object(ust, '_CACHED_DUMP_AND_SNAP', None), \
                 patch.object(ust, 'DEFAULT_SNAPSHOT', missing_snap), \
                 patch.object(ust, 'generate_default_snapshot', side_effect=fake_gen) as mock_gen:
                loaded = ust.load_default_dump_and_snapshot()
                self.assertIsNotNone(loaded)
                self.assertEqual(loaded[1], fake_snap)
                mock_gen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
