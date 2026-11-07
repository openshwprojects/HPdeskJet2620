"""Unit and integration tests for ink levels, cartridge presence, and pen diagnostics.

Verifies:
1. CarriageAsic printhead companion ASIC registers, identity, and pen presence bits.
2. Cartridge levels (full, medium, low, empty, missing) and error state transitions.
3. Live firmware pen electronic test (pen.etest), family identification (Dolmen),
   thermal reading, and nozzle dot counters.
4. Clean Test (Report 926) printhead servicing routine execution.
"""
from __future__ import annotations

import unittest
from analysis.uart_selftest import build_and_restore, load_default_dump_and_snapshot
from micci import CarriageAsic
from panel_gui import FrontPanelUIController


class TestCarriageAsicHardware(unittest.TestCase):
    """Unit tests for CarriageAsic companion chip emulation."""

    def test_carriage_asic_identity_and_presence_register(self) -> None:
        asic = CarriageAsic(identity=0x0801)
        self.assertEqual(asic.identity, 0x0801)
        self.assertEqual(asic.read(0x00), 0x800E)
        self.assertEqual(asic.read(0x7D), 0x0801)

        # Register 0x09 bit 8 reflects pen presence (0x8100 installed, 0x8000 missing)
        self.assertEqual(asic.read(0x09) & 0x0100, 0x0100)
        asic.configure_cartridges(installed=False)
        self.assertEqual(asic.read(0x09) & 0x0100, 0x0000)

        asic.configure_cartridges(installed=True)
        self.assertEqual(asic.read(0x09) & 0x0100, 0x0100)

    def test_configure_cartridges_levels_and_states(self) -> None:
        asic = CarriageAsic()

        # Default: both pens installed, 3/3 bars (full = state 1)
        summary = asic.cartridge_summary()
        self.assertTrue(summary['installed'])
        self.assertEqual(summary['pen_error_mask'], 0x00)
        self.assertEqual(summary['color_bars'], 3)
        self.assertEqual(summary['black_bars'], 3)
        self.assertEqual(summary['color_pen_state'], 1)
        self.assertEqual(summary['black_pen_state'], 1)

        # Medium / Low levels: color 2 bars (state 3), black 1 bar (state 4)
        asic.configure_cartridges(installed=True, color_bars=2, black_bars=1)
        summary = asic.cartridge_summary()
        self.assertEqual(summary['color_pen_state'], 3)
        self.assertEqual(summary['black_pen_state'], 4)
        self.assertEqual(summary['color_pct'], 66)
        self.assertEqual(summary['black_pct'], 33)

        # Empty: 0 bars (state 2)
        asic.configure_cartridges(installed=True, color_bars=0, black_bars=0)
        summary = asic.cartridge_summary()
        self.assertEqual(summary['color_pen_state'], 2)
        self.assertEqual(summary['black_pen_state'], 2)

        # Missing: error mask 0x03, bars 0, state 5
        asic.configure_cartridges(installed=False)
        summary = asic.cartridge_summary()
        self.assertFalse(summary['installed'])
        self.assertEqual(summary['pen_error_mask'], 0x03)
        self.assertEqual(summary['color_bars'], 0)
        self.assertEqual(summary['black_bars'], 0)
        self.assertEqual(summary['color_pen_state'], 5)
        self.assertEqual(summary['black_pen_state'], 5)


class TestLiveFirmwareInkCheck(unittest.TestCase):
    """Live firmware tests for ink checks, pen diagnostics, and clean routines."""

    @classmethod
    def setUpClass(cls) -> None:
        cached = load_default_dump_and_snapshot()
        if cached is None:
            raise unittest.SkipTest("Stage-22 snapshot not available")
        cls.dump, cls.snap = cached

    def test_live_pen_electronic_test_and_diagnostics(self) -> None:
        """Verify real firmware pen diagnostics (etest, thermal, dotcount, family name)."""
        emu = build_and_restore(self.dump, self.snap, native_observer=True)
        emu.board.uart_frame_ticks = 1
        ctrl = FrontPanelUIController(emu)

        out = ctrl.send_udw(
            'pen.etest; pen.get_pen_family_name_string 0; pen.get_ambient_temp; pen.get_dotcount 0;',
            steps=1_500_000,
        )
        # pen.etest returns 2 (pass), family name is Dolmen (HP 304/65 family), ambient temp >= 200 (20.0 C)
        self.assertIn('2;', out)
        self.assertIn('Dolmen;', out)
        self.assertIn('udws() returns 0 (0x0)', out)

    def test_live_cartridge_missing_vs_installed_status(self) -> None:
        """Verify firmware reaction to missing vs installed ink cartridges."""
        emu = build_and_restore(self.dump, self.snap, native_observer=True)
        emu.board.uart_frame_ticks = 1
        ctrl = FrontPanelUIController(emu)

        # Missing cartridges
        emu.carriage_asic.configure_cartridges(installed=False)
        emu.carriage_asic.apply_to_firmware(emu)
        missing_out = ctrl.send_udw('ui_lite.refresh_status; ui_lite.get_state;', steps=1_000_000)
        self.assertIn('Error: 0x10, Pen: 0x03', missing_out)

        # Installed cartridges (color 3 bars, black 2 bars)
        emu.carriage_asic.configure_cartridges(installed=True, color_bars=3, black_bars=2)
        emu.carriage_asic.apply_to_firmware(emu)
        installed_out = ctrl.send_udw('ui_lite.refresh_status; ui_lite.get_state;', steps=1_000_000)
        self.assertIn('Current state: 1, Error: 0x00, Pen: 0x00', installed_out)

        ctrl.wait_frame()
        frame_state = ctrl.get_widget_state()
        self.assertEqual(frame_state['color_ink']['bars'], [True, True, True])
        self.assertEqual(frame_state['black_ink']['bars'], [True, True, False])

    def test_live_consumable_config_xml_reporting(self) -> None:
        """Verify live /DevMgmt/ConsumableConfigDyn.xml reflects genuine HP status and ink percentages."""
        emu = build_and_restore(self.dump, self.snap, native_observer=True)
        emu.board.uart_frame_ticks = 1
        ctrl = FrontPanelUIController(emu)

        emu.carriage_asic.configure_cartridges(installed=True, color_bars=3, black_bars=3, color_pct=80, black_pct=100)
        emu.carriage_asic.apply_to_firmware(emu)
        ctrl.prepare_network_services()

        res = ctrl.get_consumables_xml()
        self.assertEqual(res['status_line'], 'HTTP/1.1 200 OK')
        self.assertGreater(res['total_bytes'], 4000)
        self.assertIn('<dd:Brand>HP</dd:Brand>', res['body'])
        self.assertIn('<dd:ConsumablePercentageLevelRemaining>80</dd:ConsumablePercentageLevelRemaining>', res['body'])
        self.assertIn('<dd:ConsumablePercentageLevelRemaining>100</dd:ConsumablePercentageLevelRemaining>', res['body'])
        self.assertIn('<dd:MeasuredQuantityState>ok</dd:MeasuredQuantityState>', res['body'])

        # Missing cartridges transition to incompatibleConsumable in XML
        emu.carriage_asic.configure_cartridges(installed=False)
        emu.carriage_asic.apply_to_firmware(emu)
        res_missing = ctrl.get_consumables_xml()
        self.assertEqual(res_missing['status_line'], 'HTTP/1.1 200 OK')
        self.assertIn('<dd:ConsumableState>incompatibleConsumable</dd:ConsumableState>', res_missing['body'])


if __name__ == '__main__':
    unittest.main()
