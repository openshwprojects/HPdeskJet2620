"""Self-tests for uploading Hello World to RAM via commands and verifying in DRAM/filesystem."""
import unittest
from analysis.uart_selftest import load_default_dump_and_snapshot, run_uart_commands, DEFAULT_SNAPSHOT


class RamUploadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cached = load_default_dump_and_snapshot()
        if cached is None:
            raise unittest.SkipTest(f"Stage-22 snapshot not found at {DEFAULT_SNAPSHOT}")
        cls.dump, cls.snap = cached

    def test_raw_upload_hello_world_command(self) -> None:
        """Verify uploading simple Hello World via ram.file_write2 (raw bytes) to /ram."""
        cmds = [
            'udws "ram.file_write2 /ram/hello.txt 12 Hello World!;"',
            'ls /ram',
            'udws "ustd.cat /ram/hello.txt;"',
        ]
        results, emu = run_uart_commands(self.dump, self.snap, cmds, max_instructions=4_000_000)
        self.assertIsNone(emu.error)
        self.assertEqual(len(results), 3)

        res_dict = dict(results)
        self.assertIn("12;udws() returns 0 (0x0)", res_dict[cmds[0]])
        self.assertIn("hello.txt", res_dict[cmds[1]])
        self.assertIn("12", res_dict[cmds[1]])
        self.assertIn("Hello World!;udws() returns 0 (0x0)", res_dict[cmds[2]])

    def test_hex_upload_hello_world_command(self) -> None:
        """Verify uploading simple Hello World via ram.file_write (hex-encoded stream) to /ram."""
        cmds = [
            'udws "ram.file_write /ram/hello_hex.txt 12 48656c6c6f20576f726c6421;"',
            'ls /ram',
            'udws "ustd.cat /ram/hello_hex.txt;"',
        ]
        results, emu = run_uart_commands(self.dump, self.snap, cmds, max_instructions=4_000_000)
        self.assertIsNone(emu.error)
        self.assertEqual(len(results), 3)

        res_dict = dict(results)
        self.assertIn("24;udws() returns 0 (0x0)", res_dict[cmds[0]])
        self.assertIn("hello_hex.txt", res_dict[cmds[1]])
        self.assertIn("12", res_dict[cmds[1]])
        self.assertIn("Hello World!;udws() returns 0 (0x0)", res_dict[cmds[2]])

    def test_ram_file_copy_and_traverse(self) -> None:
        """Verify copying uploaded Hello World within RAM filesystem and viewing directory tree."""
        cmds = [
            'udws "ram.file_write2 /ram/hello.txt 12 Hello World!;"',
            'cp /ram/hello.txt /tmp/hello_copied.txt',
            'll /tmp',
            'udws "ustd.cat /tmp/hello_copied.txt;"',
        ]
        results, emu = run_uart_commands(self.dump, self.snap, cmds, max_instructions=5_000_000)
        self.assertIsNone(emu.error)
        self.assertEqual(len(results), 4)

        res_dict = dict(results)
        self.assertIn("cp() returns 0 (0x0)", res_dict['cp /ram/hello.txt /tmp/hello_copied.txt'])
        self.assertIn("hello_copied.txt", res_dict['ll /tmp'])
        self.assertIn("Hello World!;udws() returns 0 (0x0)", res_dict['udws "ustd.cat /tmp/hello_copied.txt;"'])

    def test_dram_physical_memory_storage(self) -> None:
        """Verify that uploaded Hello World payload is physically stored in guest DDR DRAM."""
        cmds = [
            'udws "ram.file_write2 /ram/hello.txt 12 Hello World!;"',
            'udws "ustd.cat /ram/hello.txt;"',
        ]
        results, emu = run_uart_commands(self.dump, self.snap, cmds, max_instructions=3_000_000)
        self.assertIsNone(emu.error)

        dram = bytes(emu.memory._dram_mv)
        pos = dram.find(b"Hello World!")
        self.assertGreaterEqual(pos, 0, "Uploaded 'Hello World!' string not found in guest DDR DRAM")
        guest_addr = 0x20000000 + pos
        self.assertTrue(
            0x20000000 <= guest_addr < 0x24000000,
            f"Expected guest DRAM address in 0x20000000..0x24000000, got 0x{guest_addr:08x}",
        )

    def test_ram_upload_python_observer_parity(self) -> None:
        """Verify 100% execution parity under pure Python observer (native_observer=False)."""
        cmds = [
            'udws "ram.file_write2 /ram/hello.txt 12 Hello World!; ustd.cat /ram/hello.txt;"',
            'ls /ram',
        ]
        # Test using build_and_restore directly with native_observer=False
        from analysis.uart_selftest import build_and_restore

        cmd_idx = [0]
        cmd_start = [0]
        results = []

        def on_uart_byte(val: int) -> None:
            if val == 0x20 and emu.uart.endswith((b"\r-> ", b"\n-> ")) and not emu.board.uart_rx:
                if cmd_idx[0] > 0:
                    out = bytes(emu.uart[cmd_start[0] :]).decode("latin1", errors="replace")
                    results.append((cmds[cmd_idx[0] - 1], out))
                if cmd_idx[0] < len(cmds):
                    cmd_start[0] = len(emu.uart)
                    emu.board.queue_rx((cmds[cmd_idx[0]] + "\r").encode("ascii"))
                    cmd_idx[0] += 1
                else:
                    emu.reason = "all-commands-completed"
                    emu.uc.emu_stop()

        on_uart_byte.trigger_byte = 0x13E20  # type: ignore[attr-defined]
        emu = build_and_restore(self.dump, self.snap, on_uart=on_uart_byte, native_observer=False)
        emu.board.uart_frame_ticks = 1
        start_pc = self.snap["cpu"]["regs"][-1] | (1 if (self.snap["cpu"]["cpsr"] & 0x20) else 0)
        emu._started = True
        emu._limit = 4_000_000
        emu._stop = "budget"

        cmd_start[0] = len(emu.uart)
        emu.board.queue_rx((cmds[0] + "\r").encode("ascii"))
        cmd_idx[0] = 1

        emu.uc.emu_start(start_pc, 0xFFFFFFFF)
        emu._finish_instruction()

        self.assertIsNone(emu.error)
        self.assertEqual(len(results), 2)
        res_dict = dict(results)
        self.assertIn("12;Hello World!;udws() returns 0 (0x0)", res_dict[cmds[0]])
        self.assertIn("hello.txt", res_dict[cmds[1]])

    def test_interactive_ram_hello_world_execution(self) -> None:
        """Verify uploading Thumb machine code into DDR RAM via UART commands, registering as shell command, and executing."""
        from analysis.uart_selftest import build_and_restore
        from panel_gui import FrontPanelUIController

        emu = build_and_restore(self.dump, self.snap, native_observer=True)
        emu.board.uart_frame_ticks = 1
        controller = FrontPanelUIController(emu)

        info = controller.run_ram_hello_world('Hello, World from RAM!')
        self.assertEqual(info['ram_addr'], 0x20010000)
        self.assertEqual(info['exec_addr'], 0xA0010001)

        self.assertIn('20010000: b500a003 49014788 202abd00 a03cdab7', info['d_output'])
        self.assertIn('0x20010000 -> 0xb500a003', info['udw_dm_output'])
        self.assertIn('FUNC : hello (a0010001) : Run Hello from RAM', info['help_output'])
        self.assertIn('Hello, World from RAM!', info['hello_output'])
        self.assertIn('hello() returns 42 (0x2a)', info['hello_output'])
        self.assertIn('Hello, World from RAM!', info['func_call_output'])
        self.assertIn('func_call() returns 42 (0x2a)', info['func_call_output'])

    def test_controller_ram_file_methods(self) -> None:
        """Verify FrontPanelUIController upload_ram_file, cat_ram_file, list_ram_files, and copy_ram_file."""
        from analysis.uart_selftest import build_and_restore
        from panel_gui import FrontPanelUIController

        emu = build_and_restore(self.dump, self.snap, native_observer=True)
        emu.board.uart_frame_ticks = 1
        controller = FrontPanelUIController(emu)

        written = controller.upload_ram_file('/ram/test_controller.txt', 'Controller Test Payload')
        self.assertEqual(written, 23)

        read_back = controller.cat_ram_file('/ram/test_controller.txt')
        self.assertIn('Controller Test Payload', read_back)

        listing = controller.list_ram_files('/ram')
        self.assertIn('test_controller.txt', listing)

        cp_res = controller.copy_ram_file('/ram/test_controller.txt', '/tmp/test_controller_cp.txt')
        self.assertIn('cp() returns 0', cp_res)

        copied_read = controller.cat_ram_file('/tmp/test_controller_cp.txt')
        self.assertIn('Controller Test Payload', copied_read)


if __name__ == "__main__":
    unittest.main(verbosity=2)

