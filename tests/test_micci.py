import unittest

from micci import Mcasic8014, MicciController, MicciError, RegisterDevice


class MicciTests(unittest.TestCase):
    def setUp(self):
        self.callbacks = []
        self.device = RegisterDevice(0x8014, writable_registers=frozenset({6}))
        self.controller = MicciController(
            0xbd300800, lambda delay, callback: self.callbacks.append((delay, callback)),
            {(0, 6): self.device})
        self.controller.write(0xbd300800, 2, 0x2400)

    def test_read_identity_busy_until_completion(self):
        self.controller.write(0xbd3008b0, 4, 0)
        self.controller.write(0xbd3008ac, 2, 0xc02f)
        self.assertEqual(self.controller.read(0xbd3008b0, 4), 0x80000000)
        for _ in range(20):
            self.assertTrue(self.controller.read(0xbd3008b0, 4) & 0x80000000)
        self.callbacks.pop()[1]()
        self.assertEqual(self.controller.read(0xbd3008b0, 4), 0x8014)

    def test_write_takes_effect_when_transaction_finishes(self):
        self.controller.write(0xbd3008b0, 4, 0x1234)
        self.controller.write(0xbd3008ac, 2, 0xc18f)
        with self.assertRaises(MicciError):
            self.device.read(6)
        self.callbacks.pop()[1]()
        self.assertEqual(self.device.read(6), 0x1234)

    def test_overlapping_command_is_rejected(self):
        self.controller.write(0xbd3008ac, 2, 0xc02f)
        with self.assertRaisesRegex(MicciError, 'while busy'):
            self.controller.write(0xbd3008b0, 4, 5)

    def test_missing_device_and_unknown_register_do_not_complete(self):
        with self.assertRaisesRegex(MicciError, 'endpoint'):
            self.controller.write(0xbd3008ac, 2, 0xc03f)
        with self.assertRaisesRegex(MicciError, 'register read'):
            self.controller.write(0xbd3008ac, 2, 0xc06f)
        self.assertFalse(self.controller.busy)
        self.assertEqual(self.callbacks, [])

    def test_identity_is_immutable(self):
        with self.assertRaisesRegex(MicciError, 'register write'):
            self.controller.write(0xbd3008ac, 2, 0xc00f)
        self.assertEqual(self.device.read(0), 0x8014)

    def test_adc_conversion_requires_elapsed_time(self):
        device = Mcasic8014(lambda delay, callback: self.callbacks.append((delay, callback)))
        device.write(0x10, 0x830f)
        self.assertEqual(device.read(0x11), 0)
        self.assertTrue(device.adc_busy)
        self.callbacks.pop()[1]()
        self.assertEqual(device.read(0x11), 0x8200)
        self.assertFalse(device.adc_busy)

    def test_supply_sample_changes_with_physical_input(self):
        values = []
        for supply in (0, 12000, 24000, 28050):
            device = Mcasic8014(lambda delay, callback: self.callbacks.append((delay, callback)),
                               supply_mv=supply)
            device.write(0x10, 0x8400)
            self.callbacks.pop()[1]()
            values.append(device.read(0x11) & 1023)
        self.assertEqual(values, [0, 438, 875, 1023])

    def test_unknown_adc_input_is_rejected_at_command_submission(self):
        device = Mcasic8014(lambda delay, callback: self.callbacks.append((delay, callback)))
        self.controller.devices[(0, 6)] = device
        self.controller.write(0xbd3008b0, 4, 0x8303)
        with self.assertRaisesRegex(MicciError, 'input selector'):
            self.controller.write(0xbd3008ac, 2, 0xc40f)
        self.assertFalse(self.controller.busy)
        self.assertFalse(device.adc_busy)
        self.assertEqual(self.callbacks, [])


if __name__ == '__main__':
    unittest.main()
