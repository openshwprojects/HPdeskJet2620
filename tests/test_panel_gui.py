"""Unit tests for panel_gui.py headless controller, widget states, and CLI entrypoint."""
from __future__ import annotations

import unittest
from panel_gui import BUTTON_LAYOUT, SEVEN_SEG_POLYGONS, FrontPanelUIController, main


class FrontPanelUIControllerUnitTests(unittest.TestCase):
    def test_button_layout_and_polygon_geometry(self) -> None:
        self.assertEqual(len(BUTTON_LAYOUT), 6)
        names = [str(b['name']) for b in BUTTON_LAYOUT]
        self.assertIn('power', names)
        self.assertIn('cancel', names)
        self.assertIn('resume', names)
        self.assertIn('wifi', names)
        self.assertIn('copy_black', names)
        self.assertIn('copy_color', names)

        # 7-segment display segments a..g
        for seg in ('a', 'b', 'c', 'd', 'e', 'f', 'g'):
            self.assertIn(seg, SEVEN_SEG_POLYGONS)
            self.assertGreaterEqual(len(SEVEN_SEG_POLYGONS[seg]), 4)

    def test_standalone_controller_widget_state(self) -> None:
        ctrl = FrontPanelUIController()
        state = ctrl.set_ui_lite_state(
            seven_seg='3',
            wifi_bars=2,
            color_ink_bars=1,
            black_ink_bars=2,
            indicators=('exclamation_icon',),
        )
        self.assertEqual(state['seven_segment']['char'], '3')
        self.assertEqual(state['wifi_bars']['bars'], [True, True, False, False, False])
        self.assertEqual(state['color_ink']['bars'], [True, False, False])
        self.assertEqual(state['black_ink']['bars'], [True, True, False])
        self.assertTrue(state['badges']['exclamation_icon'])

    def test_cli_headless_demo(self) -> None:
        res = main(['--headless-demo'])
        self.assertEqual(res, 0)

    def test_button_press_release_and_chord(self) -> None:
        ctrl = FrontPanelUIController()
        s1 = ctrl.press_button('power')
        self.assertTrue(s1['buttons']['power'])
        s2 = ctrl.release_button('power')
        self.assertFalse(s2['buttons']['power'])

        s3 = ctrl.trigger_chord('wireless_cancel')
        self.assertTrue(s3['buttons']['wireless'])
        self.assertTrue(s3['buttons']['cancel'])

    def test_cli_print_report(self) -> None:
        import io
        import sys
        buf = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            res = main(['--print-report', '974'])
        finally:
            sys.stdout = old_stdout
        self.assertEqual(res, 0)
        out = buf.getvalue()
        self.assertIn('Print Report 974', out)
        self.assertIn('Job Started:     True', out)

    def test_cli_usb_query(self) -> None:
        import io
        import sys
        buf = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            res = main(['--usb-query'])
        finally:
            sys.stdout = old_stdout
        self.assertEqual(res, 0)
        out = buf.getvalue()
        self.assertIn('VID=0x03f0 PID=0x0053', out)
        self.assertIn("Product='DeskJet 2600 series'", out)
        self.assertIn('MFG:HP;MDL:DeskJet 2600 series;', out)

    def test_cli_run_ram_hello(self) -> None:
        import io
        import sys
        buf = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            res = main(['--run-ram-hello', 'SelfTest Hello World!'])
        finally:
            sys.stdout = old_stdout
        self.assertEqual(res, 0)
        out = buf.getvalue()
        self.assertIn('Educational RAM Execution Demo', out)
        self.assertIn('SelfTest Hello World!', out)
        self.assertIn('hello() returns 42 (0x2a)', out)
        self.assertIn('func_call() returns 42 (0x2a)', out)

    def test_cli_ram_upload_and_list(self) -> None:
        import io
        import sys
        buf = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            res_up = main(['--upload-ram', '/ram/cli_test.txt', 'HelloFromCLI'])
            res_ls = main(['--list-ram', '/ram'])
        finally:
            sys.stdout = old_stdout
        self.assertEqual(res_up, 0)
        self.assertEqual(res_ls, 0)
        out = buf.getvalue()
        self.assertIn('Uploaded 12 bytes to /ram/cli_test.txt', out)
        self.assertIn('total files in /ram', out)


if __name__ == '__main__':
    unittest.main()

