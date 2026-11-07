"""Unit and integration tests for Scanner / CIS subsystem and eSCL AirScan services.

Verifies:
1. Live eSCL scanner diagnostics over UDW (`escl.get_scan_status`, `escl.get_capabilities`).
2. Copy job submission (`smgr_copy.mfg_copy`) and INTSCAN/COPY job state machine.
3. HTTP eSCL endpoints (`/eSCL/ScannerStatus`, `/eSCL/ScannerCapabilities`) over Wi-Fi.
4. Parity between pure Python and C native observer modes.
"""
from __future__ import annotations

import unittest
from analysis.uart_selftest import build_and_restore, load_default_dump_and_snapshot
from panel_gui import FrontPanelUIController


class TestScannerSubsystem(unittest.TestCase):
    """Integration tests for the live scanner subsystem, copy pipeline, and eSCL protocol."""

    @classmethod
    def setUpClass(cls) -> None:
        cached = load_default_dump_and_snapshot()
        if cached is None:
            raise unittest.SkipTest("Stage-22 snapshot not available")
        cls.dump, cls.snap = cached

    def test_live_scanner_escl_udw_diagnostics(self) -> None:
        """Verify eSCL scanner diagnostics and capabilities via UDW."""
        emu = build_and_restore(self.dump, self.snap, native_observer=True)
        emu.board.uart_frame_ticks = 1
        ctrl = FrontPanelUIController(emu)

        out = ctrl.query_scanner_status()
        self.assertIn("Scanner State:Stopped", out)
        self.assertIn("eSCL Version: 2.62", out)
        self.assertIn("udws() returns 0 (0x0)", out)

    def test_live_copy_job_execution(self) -> None:
        """Verify copy job creation triggers INTSCAN and COPY jobs."""
        emu = build_and_restore(self.dump, self.snap, native_observer=True)
        emu.board.uart_frame_ticks = 1
        ctrl = FrontPanelUIController(emu)

        out = ctrl.trigger_copy_job(0)
        self.assertIn("Job: INTSCAN  State: RUNNING", out)
        self.assertIn("Job: COPY", out)
        self.assertIn("udws() returns 0 (0x0)", out)

    def test_live_escl_http_endpoints(self) -> None:
        """Verify live /eSCL/ScannerStatus and /eSCL/ScannerCapabilities XML over Wi-Fi."""
        emu = build_and_restore(self.dump, self.snap, native_observer=True)
        emu.board.uart_frame_ticks = 1
        ctrl = FrontPanelUIController(emu)
        ctrl.prepare_network_services()

        status_res = ctrl.get_scanner_status_xml()
        self.assertEqual(status_res["status_line"], "HTTP/1.1 200 OK")
        self.assertGreater(status_res["total_bytes"], 500)
        self.assertIn("<pwg:Version>2.62</pwg:Version>", status_res["body"])
        self.assertIn("<pwg:State>Stopped</pwg:State>", status_res["body"])

        caps_res = ctrl.get_scanner_capabilities_xml()
        self.assertEqual(caps_res["status_line"], "HTTP/1.1 200 OK")
        self.assertGreater(caps_res["total_bytes"], 4000)
        self.assertIn("<scan:ScannerCapabilities", caps_res["body"])
        self.assertIn("<pwg:Version>2.62</pwg:Version>", caps_res["body"])

    def test_scanner_parity_python_vs_native_observer(self) -> None:
        """Verify identical scanner status output across Python and Native observer engines."""
        results = []
        for native in (False, True):
            emu = build_and_restore(self.dump, self.snap, native_observer=native)
            emu.board.uart_frame_ticks = 1
            ctrl = FrontPanelUIController(emu)
            out = ctrl.query_scanner_status()
            results.append(out)

        self.assertEqual(results[0], results[1])
        self.assertIn("Scanner State:Stopped", results[0])


if __name__ == "__main__":
    unittest.main()
