"""Unit and integration tests for the live printing pipeline and emulation backend.

Verifies:
1. Mcasic8014 optical encoder peak detector registers (0x2A..0x2D).
2. Diagnostic print report triggering (report 974) via FrontPanelUIController.
3. Print pipeline execution in both pure Python and C native observer modes.
4. Active print job status and VP03/JM_REPORTS report generation.
"""
from __future__ import annotations

import unittest
from analysis.uart_selftest import build_and_restore, load_default_dump_and_snapshot
from micci import Mcasic8014
from panel_gui import FrontPanelUIController


class TestMcasicOpticalEncoder(unittest.TestCase):
    """Test MCASIC peak detector register emulation."""

    def test_optical_encoder_peak_detector_defaults(self) -> None:
        mcasic = Mcasic8014(lambda _ticks, _cb: None)
        # Unwritten / 0 reads back calibrated optical encoder limits
        self.assertEqual(mcasic.read(0x2A), 0x00D0)
        self.assertEqual(mcasic.read(0x2B), 0x0030)
        self.assertEqual(mcasic.read(0x2C), 0x00D0)
        self.assertEqual(mcasic.read(0x2D), 0x0030)

    def test_optical_encoder_peak_detector_reset_write(self) -> None:
        mcasic = Mcasic8014(lambda _ticks, _cb: None)
        # Writing 0x80 (firmware peak detector reset) still yields calibrated limits on read
        mcasic.write(0x2A, 0x80)
        mcasic.write(0x2B, 0x80)
        mcasic.write(0x2C, 0x80)
        mcasic.write(0x2D, 0x80)
        self.assertEqual(mcasic.read(0x2A), 0x00D0)
        self.assertEqual(mcasic.read(0x2B), 0x0030)
        self.assertEqual(mcasic.read(0x2C), 0x00D0)
        self.assertEqual(mcasic.read(0x2D), 0x0030)


class TestPrintingPipeline(unittest.TestCase):
    """Test the end-to-end print pipeline in emulation."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.dump, cls.snap = load_default_dump_and_snapshot()

    def test_print_report_execution_native_observer(self) -> None:
        """Verify real firmware print execution under C native observer."""
        emu = build_and_restore(self.dump, self.snap, native_observer=True)
        emu.board.uart_frame_ticks = 1
        controller = FrontPanelUIController(emu)

        result = controller.trigger_print_report(974, steps=3_500_000)
        self.assertEqual(result['report_id'], 974)
        self.assertTrue(result['job_started'], f"Job not started: {result['output']}")
        self.assertIn('JM_REPORTS', str(result['output']))
        self.assertIn('REPORTS', str(result['output']))
        self.assertGreater(result['vp03_run'], 0)
        raster = controller.render_print_raster()
        self.assertTrue(raster['has_raster_data'])
        self.assertEqual(raster['vp03_run'], result['vp03_run'])

    def test_print_report_execution_python_observer(self) -> None:
        """Verify real firmware print execution under pure Python observer (100% parity)."""
        emu = build_and_restore(self.dump, self.snap, native_observer=False)
        emu.board.uart_frame_ticks = 1
        controller = FrontPanelUIController(emu)

        result = controller.trigger_print_report(974, steps=3_500_000)
        self.assertEqual(result['report_id'], 974)
        self.assertTrue(result['job_started'], f"Job not started: {result['output']}")
        self.assertIn('JM_REPORTS', str(result['output']))
        self.assertIn('REPORTS', str(result['output']))
        self.assertGreater(result['vp03_run'], 0)
        raster = controller.render_print_raster()
        self.assertTrue(raster['has_raster_data'])
        self.assertEqual(raster['vp03_run'], result['vp03_run'])


if __name__ == '__main__':
    unittest.main()
