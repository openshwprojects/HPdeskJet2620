"""Differential checks: both observers execute the same original reset image."""
import hashlib
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analysis.uart_selftest import load_default_dump
from emulator import Emulator, REGISTERS
from native_observer import NativeObserverError, build_native_observer
from tests.test_native_observer_invariants import _TestImage
from unicorn.arm_const import UC_ARM_REG_CPSR


DUMP_PATH = Path(__file__).resolve().parents[1] / 'dump.bin'


def snapshot(emulator, result, events):
    return {
        'result': result,
        'registers': tuple(emulator.uc.reg_read(register) for register in REGISTERS),
        'cpsr': emulator.uc.reg_read(UC_ARM_REG_CPSR),
        'cp15': dict(emulator.cp15.values),
        'ddr': emulator.memory.dram_sha256(),
        'tcm': tuple(hashlib.sha256(bank).hexdigest() if bank is not None else None
                     for bank in emulator.memory._tcm.values()),
        'ticks': emulator.board.ticks,
        'timer': emulator.board.timer,
        'recent_pcs': tuple(emulator.recent_pcs),
        'events': events,
        'uart_writes': emulator.uart_writes,
        'pending_uart': emulator.board.uart_tx_pending,
    }


class NativeResetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            build_native_observer()
        except NativeObserverError as exc:
            raise unittest.SkipTest(str(exc)) from exc
        dump = load_default_dump()
        cls.results = []
        cls.emulators = []
        for native in (False, True):
            events = []
            emulator = Emulator(dump, native_observer=native,
                                serial_panel_inputs=0x3FFFF,
                                spi_profile='byte-final', on_event=events.append)
            result = emulator.run(100_000, 'budget')
            cls.results.append(snapshot(emulator, result, events))
            cls.emulators.append(emulator)

    @classmethod
    def tearDownClass(cls):
        cls.emulators.clear()

    def test_original_reset_cpu_memory_and_clock_are_identical(self):
        self.assertEqual(self.results[0], self.results[1])
        self.assertEqual(self.results[1]['result'].instructions, 100_000)
        self.assertEqual(self.results[1]['ticks'], 100_000)
        self.assertIsNone(self.results[1]['result'].error)

    def test_complete_mmio_and_cp15_event_history_is_identical(self):
        events = self.results[1]['events']
        self.assertGreater(len(events), 100)
        self.assertEqual(self.results[0]['events'], events)
        self.assertIn('cp15-write', {event['kind'] for event in events})
        self.assertIn('mmio-write', {event['kind'] for event in events})

    def test_native_observer_is_optional_and_flash_unchanged(self):
        self.assertIsNone(self.emulators[0]._native_observer)
        self.assertGreater(self.emulators[1]._native_observer.state.cache_hits, 0)
        for emulator in self.emulators:
            self.assertTrue(emulator.memory.check_flash())
        self.assertEqual(hashlib.sha256(DUMP_PATH.read_bytes()).hexdigest(),
                         self.emulators[0].dump_sha256)

    def test_ram_ldrt_does_not_leak_user_access_into_native_observer_refresh(self):
        from cpu import CONTROL
        from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3

        # Program at reset target 0x2070:
        #   e4b10000  ldrt r0, [r1], #0   (RAM access with user permissions)
        #   e1c230b0  strh r3, [r2]       (privileged-only MMIO write to UART_TX)
        #   e1d200b8  ldrh r0, [r2, #8]   (privileged-only MMIO read from UART_STATUS)
        #   eafffffe  b .
        base_dump = _TestImage(load_default_dump())
        base_dump[0x2070:0x2080] = bytes.fromhex('e4b10000 e1c230b0 e1d200b8 eafffffe')
        base_dump._dirty_end = 0x3000

        outcomes = []
        for native in (False, True):
            emu = Emulator(base_dump, native_observer=native)
            emu.uc.mem_write(0x20000100, b'\x12\x34\x56\x78')
            emu.uc.reg_write(UC_ARM_REG_R1, 0x20000100)
            emu.uc.reg_write(UC_ARM_REG_R2, 0xBD0B3C06)
            emu.uc.reg_write(UC_ARM_REG_R3, ord('Z'))
            # Region 0: Flash 0x00000000..0x00FFFFFF, AP=3 (full access)
            # Region 1: DRAM  0x20000000..0x23FFFFFF, AP=3 (user + priv R/W)
            # Region 2: MMIO  0xBD000000..0xBDFFFFFF, AP=1 (privileged R/W only, user fault)
            emu.cp15.write((0, 6, 0, 0), 0x0000002F)
            emu.cp15.write((0, 6, 1, 0), 0x20000033)
            emu.cp15.write((0, 6, 2, 0), 0xBD00002F)
            emu.cp15.write((0, 5, 0, 2), 0x133)
            emu.cp15.write((0, 5, 0, 3), 0x003)
            emu.cp15.write(CONTROL, emu.cp15.control | 1)
            res = emu.run(max_instructions=6, stop='budget')
            self.assertEqual(res.reason, 'instruction-limit', res.error)
            self.assertFalse(emu._memory_user_access)
            if emu._native_observer is not None:
                self.assertEqual(emu._native_observer.state.user_access, 0)
            outcomes.append((res.reason, emu.uc.reg_read(UC_ARM_REG_R0), emu.board.uart_tx_pending))
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(outcomes[0][2], ord('Z'))

    def test_dma_helpers_reject_out_of_bounds_dram_ranges(self):
        from hardware import HardwareError

        emu = self.emulators[0]
        with self.assertRaises(HardwareError):
            emu._usb_dma_read(0x23FFFFF0, 32)
        with self.assertRaises(HardwareError):
            emu._usb_dma_write(0x23FFFFF0, b'x' * 32)


if __name__ == '__main__':
    unittest.main()

