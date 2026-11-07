"""USB serial console experiment (fw/app/usbcon.c) against the emulator's model
of the AXON USB device controller (usb.py): enumeration as a CDC-ACM device,
then shell commands typed over bulk OUT with their output on bulk IN."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_fw_bootloader import DUMP, _have_compiler  # noqa: E402

PROMPT = b'app> '


@unittest.skipUnless(DUMP.exists(), 'dump.bin (HP stage-1) is required')
@unittest.skipUnless(_have_compiler(), 'no ARM C compiler available')
class UsbConsoleTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from fw.build import build_all
        cls.tmp = tempfile.TemporaryDirectory()
        manifest = build_all(Path(cls.tmp.name) / 'usb', app_version='1.0.0', app_sequence=1,
                             build_time=1_700_000_000)
        cls.flash = manifest['_flash_path'].read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_until(self, vp, condition, budget=200_000_000, step=1_000_000):
        used = 0
        while not condition() and used < budget:
            vp.run_steps(step)
            used += step
        return condition()

    def control(self, vp, usb, send):
        count = len(usb.control_transfers)
        send()
        self.assertTrue(self.run_until(vp, lambda: len(usb.control_transfers) > count))
        return usb.control_transfers[-1]

    def test_enumeration_and_shell_over_usb(self):
        from fw.harness import boot_to_app
        vp = boot_to_app(self.flash)
        usb = vp.emu.board.usb
        out = vp.command('usb on', PROMPT)
        self.assertIn('usb: pull-up on', out)
        self.assertTrue(usb.pullup_enabled)
        usb.bus_reset(high_speed=True)
        dev = self.control(vp, usb, lambda: usb.setup_get_descriptor(1, 0, w_length=64)).response
        self.assertEqual(len(dev), 18)
        self.assertEqual(dev[:2], b'\x12\x01')
        self.assertEqual(dev[4], 0x02)                               # CDC
        self.assertEqual(dev[8:12], b'\x09\x12\x01\x00')             # 1209:0001, little-endian
        usb.setup_set_address(9)
        self.assertTrue(self.run_until(vp, lambda: usb.device_address == 9))
        cfg = self.control(vp, usb, lambda: usb.setup_get_descriptor(2, 0, w_length=255)).response
        self.assertEqual(len(cfg), 67)
        self.assertEqual(cfg[:4], bytes([9, 2, 67, 0]))
        self.assertEqual(cfg[55:59], bytes([0x01, 0x02, 0x00, 0x02]))   # bulk OUT 1, 512 bytes (high speed)
        self.assertEqual(cfg[62:66], bytes([0x82, 0x02, 0x00, 0x02]))   # bulk IN 2
        name = self.control(vp, usb, lambda: usb.setup_get_descriptor(3, 2, w_length=255)).response
        self.assertEqual(name[2:].decode('utf-16-le'), 'DeskJet 2620 serial console')
        self.assertFalse(self.control(vp, usb, lambda: usb.setup_set_configuration(1)).stalled)
        coding = self.control(vp, usb, lambda: usb.send_setup(bytes([0xA1, 0x21, 0, 0, 0, 0, 7, 0]))).response
        self.assertEqual(coding, bytes([0x00, 0xC2, 0x01, 0x00, 0, 0, 8]))   # 115200 8N1
        self.assertFalse(self.control(vp, usb, lambda: usb.send_setup(
            bytes([0x21, 0x22, 3, 0, 0, 0, 0, 0]))).stalled)                  # DTR/RTS
        self.assertTrue(self.control(vp, usb, lambda: usb.send_setup(
            bytes([0xC0, 0x99, 0, 0, 0, 0, 0, 0]))).stalled)                  # unknown vendor request
        # The shell over USB: type on bulk OUT, read the answer on bulk IN.
        received = bytearray()

        def got(text):
            received.extend(usb.drain_bulk_in(2))
            return text in received

        usb.send_bulk_out(1, b'version\r')
        self.assertTrue(self.run_until(vp, lambda: got(b'HPDJ app v1.0.0 seq 1')), bytes(received))
        self.assertTrue(self.run_until(vp, lambda: got(b'app> ')))
        self.assertIn(b'version', bytes(received))                    # the typed line is echoed
        self.assertIn('HPDJ app v1.0.0', vp.uart_text())              # and the UART sees it too
        received.clear()
        usb.send_bulk_out(1, b'usb\r')
        self.assertTrue(self.run_until(vp, lambda: got(b'configured')), bytes(received))
        self.assertIn(b'configured (config register 1)', bytes(received))
        self.assertGreater(usb.endpoint_transfers[0][2].count(b'v'), 0)   # typed bytes arrived in an armed DMA
        out = vp.command('usb off', PROMPT)
        self.assertIn('usb: off', out)
        self.assertFalse(usb.pullup_enabled)


if __name__ == '__main__':
    unittest.main()
