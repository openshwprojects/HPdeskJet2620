"""The custom firmware's front-panel menu (fw/app/ui.c) in the emulator: the
buttons are pressed in the panel model and the display is decoded from the
frames the firmware shifts out."""
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
class PanelMenuTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from fw.build import build_all
        cls.tmp = tempfile.TemporaryDirectory()
        manifest = build_all(Path(cls.tmp.name) / 'ui', app_version='1.0.0', app_sequence=1,
                             build_time=1_700_000_000)
        cls.flash = manifest['_flash_path'].read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        from fw.harness import boot_to_app
        from wifi_net import VirtualWifiNetwork
        self.vp = boot_to_app(self.flash)
        self.net = VirtualWifiNetwork(self.vp.emu.board.sdio.card)
        self.ep = self.vp.emu.board.panel.endpoint
        self.gpio = self.vp.emu.board.gpio

    def run_until(self, condition, budget=1_500_000_000, step=2_000_000):
        used = 0
        while not condition() and used < budget:
            self.vp.run_steps(step)
            used += step
        return condition()

    def frames_later(self, n):
        target = len(self.ep.frames) + n
        self.assertTrue(self.run_until(lambda: len(self.ep.frames) >= target))

    def tap(self, button, *, frames_down=2):
        """Press for a couple of menu polls, then release."""
        self.ep.press_button(button)
        self.frames_later(frames_down)
        self.ep.release_button(button)

    def text_after(self, mark, needle, budget=1_500_000_000):
        self.assertTrue(self.run_until(lambda: needle.encode() in self.vp.uart_bytes(mark), budget=budget),
                        self.vp.uart_text(mark)[-1500:])
        return self.vp.uart_text(mark)

    def display(self):
        return self.ep.decode_frame()

    def led(self, pin):
        return self.gpio.directions[pin] == 1 and self.gpio.output_latches[pin] == 0

    def test_wireless_scan_access_point_info_and_cancel(self):
        self.frames_later(1)                                   # the menu draws by itself
        self.assertEqual(self.display()['seven_seg_char'], ' ')
        mark = len(self.vp.emu.uart)
        self.tap('wireless')
        text = self.text_after(mark, 'ui: 2 networks')
        self.assertIn('ui: wireless: scanning', text)
        self.assertIn('wifi: firmware says: wl0:', text)       # the chip firmware was loaded on demand
        self.assertIn('ui: 2 networks, strongest -42 dBm', text)
        self.frames_later(1)
        shown = self.display()
        self.assertEqual((shown['seven_seg_char'], shown['wifi_bars']), ('2', 5))
        # Hold Wireless for two seconds: the printer's own access point.
        mark = len(self.vp.emu.uart)
        self.ep.press_button('wireless')
        text = self.text_after(mark, 'is up: join it')
        self.ep.release_button('wireless')
        self.assertIn('ui: wireless held: starting the access point', text)
        self.assertIn('wifi: access point "HPDJ-2001" is up', text)
        self.frames_later(2)
        shown = self.display()
        self.assertEqual(shown['seven_seg_char'], 'A')
        self.assertIn('wifi_direct_icon', shown['active_indicators'])
        self.assertTrue(self.led(0))                           # blue Wireless LED: link
        self.assertNotIn('ui: wireless: scanning', self.vp.uart_text(mark))   # a long press is not a tap
        # A station joins; Wireless now shows how many.
        self.net.associate_client()
        self.assertTrue(self.run_until(lambda: self.net.client_ip is not None))
        mark = len(self.vp.emu.uart)
        self.tap('wireless')
        self.text_after(mark, 'ui: wireless: 1 station on the access point')
        self.frames_later(1)
        self.assertEqual(self.display()['seven_seg_char'], '1')
        # Info: the address, one character at a time.
        mark = len(self.vp.emu.uart)
        start = len(self.ep.frames)
        self.tap('info', frames_down=1)
        self.text_after(mark, 'ui: info: address 192.168.4.1')
        self.frames_later(80)                                  # 11 characters * 0.8 s at 0.15 s per frame
        chars = [self.ep.decode_frame(frame['output'])['seven_seg_char'] for frame in self.ep.frames[start:]]
        chars = [c for c in chars if c != ' ']                # blanks separate the characters
        collapsed = [c for i, c in enumerate(chars) if i == 0 or c != chars[i - 1]]
        self.assertIn(''.join(['1', '9', '2', '-', '1', '6', '8', '-', '4', '-', '1']), ''.join(collapsed))
        # Cancel switches Wi-Fi off.
        mark = len(self.vp.emu.uart)
        self.tap('cancel', frames_down=1)
        self.text_after(mark, 'ui: cancel: Wi-Fi off')
        self.frames_later(2)
        self.assertFalse(self.led(0))
        out = self.vp.command('wifi', PROMPT)
        self.assertIn('wifi: mode off', out)

    def test_resume_test_power_soft_off_and_manual_panel(self):
        self.frames_later(1)
        mark = len(self.vp.emu.uart)
        start = len(self.ep.frames)
        self.tap('resume', frames_down=1)
        self.text_after(mark, 'ui: resume: display test')
        self.frames_later(3)
        lit = [self.ep.decode_frame(f['output']) for f in self.ep.frames[start:]]
        full = [d for d in lit if d['seven_seg_char'] == '8' and d['wifi_bars'] == 5 and
                (d['color_ink_bars'], d['black_ink_bars']) == (3, 3)]
        self.assertTrue(full)
        self.assertIn('exclamation_icon', full[0]['active_indicators'])
        self.frames_later(10)                                  # back to normal after one second
        self.assertEqual(self.display()['seven_seg_char'], ' ')
        # Power: soft off and on again.
        self.assertTrue(self.led(28))
        mark = len(self.vp.emu.uart)
        self.tap('power', frames_down=1)
        self.text_after(mark, 'ui: power: soft off')
        self.frames_later(2)
        self.assertEqual(self.ep.logical_output, 0)            # nothing lit
        self.assertFalse(self.led(28))                         # power LED off
        mark = len(self.vp.emu.uart)
        self.tap('power', frames_down=1)
        self.text_after(mark, 'ui: power: on')
        self.assertTrue(self.led(28))
        # Driving the panel by hand pauses the menu until "ui on".
        out = self.vp.command('lcd 7', PROMPT)
        self.assertIn('ui: the panel menu is paused', out)
        mark = len(self.vp.emu.uart)
        self.ep.press_button('resume')
        self.vp.run_steps(40_000_000)
        self.ep.release_button('resume')
        self.vp.run_steps(10_000_000)
        self.assertNotIn('ui:', self.vp.uart_text(mark))
        out = self.vp.command('ui', PROMPT)
        self.assertIn('ui: panel menu on, paused (ui on resumes)', out)
        out = self.vp.command('ui on', PROMPT)
        self.assertIn('ui: panel menu on', out)
        mark = len(self.vp.emu.uart)
        self.tap('copy_color', frames_down=1)
        self.text_after(mark, 'ui: copy_color pressed (no action)')

    def test_motor_init_writes_hp_register_state(self):
        mcasic = self.vp.emu.mcasic
        writes = []
        real_write = mcasic.write

        def logged_write(register, value):
            writes.append((register, value & 0xFFFF))
            return real_write(register, value)

        mcasic.write = logged_write
        try:
            out = self.vp.command('motor init', PROMPT)
        finally:
            mcasic.write = real_write
        self.assertIn('mcasic 30 <- 8000 ok', out)
        self.assertIn("motor: HP's register state applied (motors off)", out)
        # every motor off first, then HP's configuration order (0x20 enable bit
        # cleared, 0x21..0x23, 0x20 with the enable bit, 0x30, GPIO block); no motor word
        self.assertEqual(writes[:3], [(0x31, 0), (0x33, 0), (0x35, 0)])
        self.assertEqual(writes[3:], [(0x20, 0x0000), (0x21, 0x0101), (0x22, 0x0000), (0x23, 0x0000),
                                      (0x20, 0xC070), (0x30, 0x8000), (0x38, 0x0030), (0x39, 0x0030),
                                      (0x3A, 0xC000)])

    def test_sensors_and_door_icon(self):
        out = self.vp.command('sensors', PROMPT)
        self.assertRegex(out, r'door\s+gpio\s+8 = 1 -> inactive\s+DOOR_SENSOR')
        self.assertRegex(out, r'tray_ir_s1\s+gpio\s+1 = 0 -> inactive\s+TRAY_IR_SENSOR_S1')
        self.assertRegex(out, r'tray_ir_s3\s+gpio\s+3 = 0 -> inactive')
        self.assertIn('door: closed', out)
        # Door open (GPIO 8 low): the menu logs it and lights the "!" icon
        # (debounced over two polls; the first readings only record the state).
        self.frames_later(3)
        mark = len(self.vp.emu.uart)
        self.gpio.set_input(8, 0)
        self.text_after(mark, 'ui: door open')
        self.frames_later(2)
        self.assertIn('exclamation_icon', self.display()['active_indicators'])
        self.gpio.set_input(8, 1)
        self.text_after(mark, 'ui: door closed')
        self.frames_later(2)
        self.assertNotIn('exclamation_icon', self.display()['active_indicators'])
        # "sensors watch" reports changes as they happen.
        mark = len(self.vp.emu.uart)
        self.vp.send('sensors watch 2\r')
        self.text_after(mark, 'watching the sensors')
        self.gpio.set_input(1, 1)
        out = self.text_after(mark, 'done, ')
        self.assertIn('tray_ir_s1 active (gpio 1 = 1)', out)
        self.assertIn('done, 1 change', out)
        self.gpio.set_input(1, 0)

    def test_motors_and_buttons_test_command(self):
        mcasic = self.vp.emu.mcasic
        writes = []
        real_write = mcasic.write

        def logged_write(register, value):
            writes.append((register, value & 0xFFFF))
            return real_write(register, value)

        mcasic.write = logged_write
        try:
            mark = len(self.vp.emu.uart)
            self.vp.send('motorsAndButtonsTest 250\r')
            self.text_after(mark, 'mbt> ')
            self.assertIn("motor: HP's register state applied", self.vp.uart_text(mark))
            writes.clear()

            # Hold cancel -> Motor A fwd (0x31 <- 0x10fa at default PWM 250)
            mark = len(self.vp.emu.uart)
            self.ep.press_button('cancel')
            self.text_after(mark, '[mbt] HOLD cancel (bit 6): motor A fwd (reg 0x31 <- 0x10fa, pwm 250)')
            self.frames_later(1)
            self.assertEqual(self.display()['seven_seg_char'], 'A')
            self.assertIn((0x31, 0x10FA), writes)

            # Release cancel -> all motors off
            mark = len(self.vp.emu.uart)
            writes.clear()
            self.ep.release_button('cancel')
            self.text_after(mark, '[mbt] RELEASE cancel: motor A fwd off')
            self.assertIn((0x31, 0x0000), writes)

            # Hold wireless -> Motor B fwd (0x33 <- 0x10fa)
            mark = len(self.vp.emu.uart)
            writes.clear()
            self.ep.press_button('wireless')
            self.text_after(mark, '[mbt] HOLD wireless (bit 3): motor B fwd (reg 0x33 <- 0x10fa, pwm 250)')
            self.frames_later(1)
            self.assertEqual(self.display()['seven_seg_char'], 'b')
            self.assertIn((0x33, 0x10FA), writes)
            self.ep.release_button('wireless')
            self.text_after(mark, '[mbt] RELEASE wireless: motor B fwd off')

            # Sub-command pwm 350, then hold copy_color -> Motor C rev (0x35 <- 0x215e)
            mark = len(self.vp.emu.uart)
            self.vp.send('pwm 350\r')
            self.text_after(mark, '[mbt] pwm set to 350')
            mark = len(self.vp.emu.uart)
            writes.clear()
            self.ep.press_button('copy_color')
            self.text_after(mark, '[mbt] HOLD copy_color (bit 8): motor C rev (reg 0x35 <- 0x215e, pwm 350)')
            self.assertIn((0x35, 0x215E), writes)
            self.ep.release_button('copy_color')
            self.text_after(mark, '[mbt] RELEASE copy_color: motor C rev off')

            # Exit back to app>
            mark = len(self.vp.emu.uart)
            self.vp.send('exit\r')
            self.text_after(mark, 'motorsAndButtonsTest: all motors off, exiting')
            self.assertTrue(self.run_until(lambda: self.vp.uart_bytes().endswith(PROMPT)))
        finally:
            mcasic.write = real_write


if __name__ == '__main__':
    unittest.main()
