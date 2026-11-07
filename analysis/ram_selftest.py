"""Self-test for uploading simple Hello World via commands (to RAM): python -m analysis.ram_selftest."""
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analysis.uart_selftest import load_default_dump_and_snapshot, run_uart_commands, build_and_restore, DEFAULT_SNAPSHOT
from panel_gui import FrontPanelUIController


class RamUploadSelfTest(unittest.TestCase):
    """Verify uploading simple Hello World via commands into RAM filesystem and physical memory."""

    @classmethod
    def setUpClass(cls) -> None:
        cached = load_default_dump_and_snapshot()
        if cached is None:
            raise unittest.SkipTest(f"Stage-22 snapshot not found at {DEFAULT_SNAPSHOT}")
        cls.dump, cls.snap = cached

    def test_raw_and_hex_upload_to_ram_filesystem(self) -> None:
        """Upload Hello World to /ram via raw and hex commands, verify via ls, ustd.cat, cp, and DRAM."""
        upload_cmds = [
            'udws "ram.file_write2 /ram/hello.txt 12 Hello World!;"',
            'udws "ram.file_write /ram/hello_hex.txt 12 48656c6c6f20576f726c6421;"',
            'ls /ram',
            'udws "ustd.cat /ram/hello.txt;"',
            'udws "ustd.cat /ram/hello_hex.txt;"',
            'cp /ram/hello.txt /tmp/hello_copied.txt',
            'll /tmp',
            'udws "ustd.cat /tmp/hello_copied.txt;"',
            'udws "ram.statfs /ram;"',
        ]
        t0 = time.time()
        results, emu = run_uart_commands(self.dump, self.snap, upload_cmds, max_instructions=5_000_000)
        elapsed = time.time() - t0

        self.assertIsNone(emu.error, f"Emulator faulted during RAM upload: {emu.error}")
        self.assertEqual(len(results), len(upload_cmds))

        res_map = dict(results)
        self.assertIn("12;udws() returns 0 (0x0)", res_map['udws "ram.file_write2 /ram/hello.txt 12 Hello World!;"'])
        self.assertIn("24;udws() returns 0 (0x0)", res_map['udws "ram.file_write /ram/hello_hex.txt 12 48656c6c6f20576f726c6421;"'])
        self.assertIn("hello.txt", res_map['ls /ram'])
        self.assertIn("hello_hex.txt", res_map['ls /ram'])
        self.assertIn("Hello World!;udws() returns 0 (0x0)", res_map['udws "ustd.cat /ram/hello.txt;"'])
        self.assertIn("Hello World!;udws() returns 0 (0x0)", res_map['udws "ustd.cat /ram/hello_hex.txt;"'])
        self.assertIn("cp() returns 0 (0x0)", res_map['cp /ram/hello.txt /tmp/hello_copied.txt'])
        self.assertIn("hello_copied.txt", res_map['ll /tmp'])
        self.assertIn("Hello World!;udws() returns 0 (0x0)", res_map['udws "ustd.cat /tmp/hello_copied.txt;"'])
        self.assertIn("statfs() : block size = 1024", res_map['udws "ram.statfs /ram;"'])

        # Verify physical DRAM backing in guest memory space
        dram = bytes(emu.memory._dram_mv)
        pos = dram.find(b"Hello World!")
        self.assertGreaterEqual(pos, 0, "Uploaded 'Hello World!' string not found in physical DDR DRAM")

        print(
            f"\n[OK] Uploaded 'Hello World!' to /ram via commands (raw + hex), "
            f"verified via ls /ram, ustd.cat, cp, ll /tmp, and physical DRAM at 0x{0x20000000 + pos:08x} "
            f"in {elapsed:.2f}s ({emu.instructions:,} instructions, 0 faults)."
        )

    def test_interactive_machine_code_ram_execution(self) -> None:
        """Upload and execute Thumb Hello World machine code routine into DDR RAM via UART commands."""
        emu = build_and_restore(self.dump, self.snap, native_observer=True)
        emu.board.uart_frame_ticks = 1
        controller = FrontPanelUIController(emu)

        t0 = time.time()
        info = controller.run_ram_hello_world('Hello, World from RAM!')
        elapsed = time.time() - t0

        self.assertEqual(info['ram_addr'], 0x20010000)
        self.assertEqual(info['exec_addr'], 0xA0010001)

        self.assertIn('20010000: b500a003 49014788 202abd00 a03cdab7', info['d_output'])
        self.assertIn('0x20010000 -> 0xb500a003', info['udw_dm_output'])
        self.assertIn('FUNC : hello (a0010001) : Run Hello from RAM', info['help_output'])
        self.assertIn('Hello, World from RAM!', info['hello_output'])
        self.assertIn('hello() returns 42 (0x2a)', info['hello_output'])
        self.assertIn('Hello, World from RAM!', info['func_call_output'])
        self.assertIn('func_call() returns 42 (0x2a)', info['func_call_output'])

        print(
            f"\n[OK] Uploaded & executed Thumb Hello World machine code in DDR RAM at 0x20010000 -> 0xA0010001 "
            f"via UART developer commands in {elapsed:.2f}s, returns 42 (0x2a)."
        )

    def test_controller_ram_filesystem_helpers(self) -> None:
        """Upload, read, list, and copy RAM files using FrontPanelUIController helper methods."""
        emu = build_and_restore(self.dump, self.snap, native_observer=True)
        emu.board.uart_frame_ticks = 1
        controller = FrontPanelUIController(emu)

        t0 = time.time()
        written = controller.upload_ram_file('/ram/hello_ctrl.txt', 'Controller Hello World!')
        self.assertEqual(written, 23)

        content = controller.cat_ram_file('/ram/hello_ctrl.txt')
        self.assertIn('Controller Hello World!', content)

        ls_out = controller.list_ram_files('/ram')
        self.assertIn('hello_ctrl.txt', ls_out)

        cp_out = controller.copy_ram_file('/ram/hello_ctrl.txt', '/tmp/hello_ctrl_copy.txt')
        self.assertIn('cp() returns 0', cp_out)

        copy_content = controller.cat_ram_file('/tmp/hello_ctrl_copy.txt')
        self.assertIn('Controller Hello World!', copy_content)
        elapsed = time.time() - t0

        print(
            f"\n[OK] Controller RAM filesystem helpers verified (upload, cat, list, copy) in {elapsed:.2f}s."
        )


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    unittest.main(verbosity=2)
