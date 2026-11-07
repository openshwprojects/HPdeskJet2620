"""Unit and integration tests for front-panel button and key-press emulation.

Verifies:
1. Shift-register active-low button press/release for all physical keys.
2. Resolution of button keys by name, key ID, and bit index.
3. Multi-button chords (wireless_cancel, wireless_resume, copy_color_resume, etc.).
4. Live firmware key capture via fp.test_enter and fp.test_dump_keys.
5. Power-button hold service combos modifying firmware service registers.
6. Interactive copy button reactions on the 7-segment display counter.
"""
from __future__ import annotations

import struct
import unittest
from analysis.uart_selftest import build_and_restore, load_default_dump_and_snapshot
from panel import SerialShiftPanel
from panel_gui import BUTTON_LAYOUT, BUTTON_EVENT_IDS, FrontPanelUIController


class TestPanelKeyEmulation(unittest.TestCase):
    """Unit tests for button resolution, press, release, and chord mechanics."""

    def setUp(self) -> None:
        self.panel = SerialShiftPanel(0x3FFFF)

    def test_all_physical_buttons_press_and_release(self) -> None:
        for name, (bit, key_id) in SerialShiftPanel.BUTTONS.items():
            self.panel.release_all_buttons()
            self.assertEqual(self.panel.raw_inputs, 0x3FFFF)

            # Press button -> active-low bit clears to 0
            self.panel.press_button(name)
            self.assertEqual(self.panel.raw_inputs, 0x3FFFF & ~(1 << bit))
            self.assertIn(name, self.panel.pressed_buttons)

            # Release button -> bit returns to 1
            self.panel.release_button(name)
            self.assertEqual(self.panel.raw_inputs, 0x3FFFF)
            self.assertNotIn(name, self.panel.pressed_buttons)

    def test_all_multi_button_chords(self) -> None:
        chords = (
            ('wireless_resume', ('wireless', 'resume'), 8329),
            ('wireless_cancel', ('wireless', 'cancel'), 8365),
            ('wireless_info', ('wireless', 'info'), 8316),
            ('info_cancel', ('info', 'cancel'), 8317),
            ('info_resume', ('info', 'resume'), 8318),
            ('resume_power', ('resume', 'power'), 8303),
            ('copy_color_resume', ('copy_color', 'resume'), 8301),
            ('copy_black_resume', ('copy_black', 'resume'), 8302),
        )
        for chord_name, (btn_a, btn_b), key_id in chords:
            self.panel.release_all_buttons()
            self.panel.press_chord(chord_name)
            self.assertIn(btn_a, self.panel.pressed_buttons)
            self.assertIn(btn_b, self.panel.pressed_buttons)

            self.panel.release_chord(chord_name)
            self.assertNotIn(btn_a, self.panel.pressed_buttons)
            self.assertNotIn(btn_b, self.panel.pressed_buttons)

    def test_controller_press_and_chords(self) -> None:
        ctrl = FrontPanelUIController(panel=self.panel)
        ctrl.press_button('power')
        state = ctrl.get_widget_state()
        self.assertTrue(state['buttons']['power'])

        ctrl.release_button('power')
        state = ctrl.get_widget_state()
        self.assertFalse(state['buttons']['power'])

        ctrl.trigger_chord('wireless_cancel')
        state = ctrl.get_widget_state()
        self.assertTrue(state['buttons']['wireless'])
        self.assertTrue(state['buttons']['cancel'])


class TestLiveFirmwareKeyRecording(unittest.TestCase):
    """Live firmware tests verifying real firmware receives and registers key presses."""

    @classmethod
    def setUpClass(cls) -> None:
        cached = load_default_dump_and_snapshot()
        if cached is None:
            raise unittest.SkipTest("Stage-22 snapshot not available")
        cls.dump, cls.snap = cached

    def test_live_firmware_key_dump(self) -> None:
        """Verify firmware fp.test_dump_keys accurately records physical button events and chords."""
        emu = build_and_restore(self.dump, self.snap, native_observer=True)
        emu.board.uart_frame_ticks = 1
        ctrl = FrontPanelUIController(emu)
        ep = emu.board.panel.endpoint

        # Enter key test mode
        ctrl.send_udw('fp.test_enter;', steps=1_000_000)
        ctrl.wait_frame()

        # Press single buttons and chord
        for btn in ('cancel', 'copy_color', 'power'):
            ep.press_button(btn)
            ctrl.wait_frame()
            ep.release_button(btn)
            ctrl.wait_frame()

        ep.press_chord('wireless_cancel')
        ctrl.wait_frame()
        ep.release_chord('wireless_cancel')
        ctrl.wait_frame()

        # Dump recorded keys from firmware buffer
        dump_out = ctrl.send_udw('fp.test_dump_keys; fp.test_exit;', steps=1_000_000)
        self.assertIn('6, 8, 1, 50790399,', dump_out)
        decoded = [ep.decode_key_event(v) for v in (6, 8, 1, 50790399)]
        self.assertEqual(decoded, ['cancel', 'copy_color', 'power', 'wireless_cancel'])

    def test_live_power_hold_service_combo(self) -> None:
        """Verify holding Power and tapping buttons updates firmware service counters."""
        emu = build_and_restore(self.dump, self.snap, native_observer=True)
        emu.board.uart_frame_ticks = 1
        ctrl = FrontPanelUIController(emu)
        ep = emu.board.panel.endpoint

        ep.press_button('power')
        ctrl.wait_frame()
        for tap_btn in ('copy_color', 'resume', 'resume', 'copy_black'):
            ep.press_button(tap_btn)
            ctrl.wait_frame()
            ep.release_button(tap_btn)
            ctrl.wait_frame()
        ep.release_button('power')
        ctrl.wait_frame()

        # Service registers in DRAM at 0x2093C060 (tens, hundreds, ones)
        tens, hundreds, ones = struct.unpack('>III', emu.uc.mem_read(0x2093C060, 12))
        self.assertEqual((tens, hundreds, ones), (20, 100, 1))

    def test_live_copy_button_counter_reaction(self) -> None:
        """Verify tapping Copy Color increments the 7-segment display counter."""
        emu = build_and_restore(self.dump, self.snap, native_observer=True)
        emu.board.uart_frame_ticks = 1
        ctrl = FrontPanelUIController(emu)

        # Ensure cartridges are ready so printer accepts copy commands
        emu.carriage_asic.configure_cartridges(installed=True, color_bars=3, black_bars=2)
        emu.carriage_asic.apply_to_firmware(emu)
        ctrl.send_udw('ui_lite.refresh_status;', steps=800_000)

        # Tap copy color twice
        copy_event = BUTTON_EVENT_IDS['copy_color']
        ctrl.send_udw(
            f'ui.send_button {copy_event}, 0; ui.send_button {copy_event}, 1; '
            f'ui.send_button {copy_event}, 0; ui.send_button {copy_event}, 1; '
            f'ui_lite.get_state;',
            steps=1_200_000,
        )
        ctrl.wait_frame()
        seq = ctrl.get_widget_state()['seven_segment']['sequence']
        self.assertIn('2', seq)


if __name__ == '__main__':
    unittest.main()
