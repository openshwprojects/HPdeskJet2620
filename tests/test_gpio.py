"""GPIO state and strict handling of unconfigured external signals."""
import unittest

from gpio import GPIOController, GPIOError


BASE = 0xBD0B2000


class GPIOTests(unittest.TestCase):
    def setUp(self):
        self.gpio = GPIOController()

    def test_output_latch_can_be_preloaded_before_output_enable(self):
        pin = 7
        address = BASE + pin * 8
        self.gpio.write(address + 2, 2, 1)
        self.assertEqual(self.gpio.read(address + 2, 2), 1)
        with self.assertRaisesRegex(GPIOError, 'input level is unknown'):
            self.gpio.read(address, 2)
        self.gpio.write(address + 4, 2, 1)
        self.assertEqual(self.gpio.read(address, 2), 1)
        self.gpio.write(address + 2, 2, 0)
        self.assertEqual(self.gpio.read(address, 2), 0)

    def test_input_requires_an_explicit_level_and_can_be_released(self):
        address = BASE + 63 * 8
        self.gpio.set_input(63, 1)
        self.assertEqual(self.gpio.read(address, 2), 1)
        self.gpio.set_input(63, 0)
        self.assertEqual(self.gpio.read(address, 2), 0)
        self.gpio.set_input(63, None)
        with self.assertRaises(GPIOError):
            self.gpio.read(address, 2)

    def test_direction_change_exposes_external_signal_instead_of_stale_latch(self):
        self.gpio.write(BASE + 2, 2, 1)
        self.gpio.write(BASE + 4, 2, 1)
        self.assertEqual(self.gpio.read(BASE, 2), 1)
        self.gpio.write(BASE + 4, 2, 0)
        self.gpio.set_input(0, 0)
        self.assertEqual(self.gpio.read(BASE, 2), 0)
        self.assertEqual(self.gpio.read(BASE + 2, 2), 1)

    def test_conflicting_drives_are_not_silently_resolved(self):
        self.gpio.write(BASE + 2, 2, 1)
        self.gpio.write(BASE + 4, 2, 1)
        self.gpio.set_input(0, 0)
        with self.assertRaisesRegex(GPIOError, 'conflicting'):
            self.gpio.read(BASE, 2)
        self.gpio.set_input(0, 1)
        self.assertEqual(self.gpio.read(BASE, 2), 1)

    def test_event_status_is_explicit_and_cleared_by_zero(self):
        pin = 18
        address = BASE + 0x400 + pin * 2
        self.assertEqual(self.gpio.read(address, 2), 0)
        self.gpio.set_input(pin, 0)
        self.gpio.set_input(pin, 1)
        self.assertEqual(self.gpio.read(address, 2), 0)
        self.gpio.set_event(pin, 3)
        self.assertEqual(self.gpio.read(address, 2), 3)
        with self.assertRaises(GPIOError):
            self.gpio.write(address, 2, 1)
        self.gpio.write(address, 2, 0)
        self.assertEqual(self.gpio.read(address, 2), 0)

    def test_irq_mask_words_retain_each_channel_independently(self):
        for channel in range(8):
            for group in range(4):
                address = BASE + 0x500 + channel * 16 + (3 - group) * 2
                self.gpio.write(address, 2, 1 << (channel + group))
        for channel in range(8):
            for group in range(4):
                address = BASE + 0x500 + channel * 16 + (3 - group) * 2
                self.assertEqual(self.gpio.read(address, 2), 1 << (channel + group))

    def test_unknown_width_offsets_and_direction_codes_stop(self):
        for address, size in ((BASE, 4), (BASE + 6, 2), (BASE + 0x200, 2),
                              (BASE + 0x480, 2), (BASE + 0x508, 2),
                              (BASE + 0x580, 2)):
            with self.subTest(address=hex(address), size=size):
                with self.assertRaises(GPIOError):
                    self.gpio.read(address, size)
        with self.assertRaises(GPIOError):
            self.gpio.write(BASE, 2, 1)
        with self.assertRaises(GPIOError):
            self.gpio.write(BASE + 4, 2, 2)
        self.assertEqual(self.gpio.read(BASE + 4, 2), 0)

    def test_profile_validation_and_page_inventory(self):
        gpio = GPIOController(external_inputs={7: 1})
        self.assertEqual(gpio.read(BASE + 7 * 8, 2), 1)
        self.assertEqual(gpio.pages, [BASE])
        self.assertTrue(gpio.contains(BASE + 7 * 8 + 2))
        self.assertFalse(gpio.contains(BASE + 6))
        for pin, level in ((64, 0), (-1, 1), (0, 2)):
            with self.assertRaises(ValueError):
                gpio.set_input(pin, level)


if __name__ == '__main__':
    unittest.main()
