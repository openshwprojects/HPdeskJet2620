"""Reset-to-board-diagnostics checks using the original, unmodified firmware.

Three real reset runs distinguish absent/erased EEPROM and an explicitly
static panel input. No instructions, RAM data, or PCs are replaced.
"""
from pathlib import Path
import unittest

from analysis.uart_selftest import load_default_dump
from emulator import Emulator, UART_TX


DUMP_PATH = Path(__file__).resolve().parents[1] / 'dump.bin'
EXPECTED_UART = (
    b'\n\r[Boot] ******************************************************\n\r'
    b'[Boot] Firmware build timestamp: Sat Jan 14, 2017 03:32:35PM\n\r'
    b'\n\r[Boot] ASIC ID (AXON RevA .0 fab0 Marvel)\n\r'
    b'[Boot] Entering NOS_HW_MCASIC_init\n\r'
    b'\n\r[Boot] Board Support (ROM)           : 0x064501ff (bytepacked)\n\r'
    b'[Boot] Unmapped Board ID (Compiled in): 0x68bf (bitpacked) M:0x06 F:0x45 N:0x1f\n\r'
    b'[Boot] Unmapped Board ID (from NVM)  : MISSING!  Tell the EEs\n\r'
    b'[Boot] Unmapped Board ID (ROM num)   : 0x68bf (bitpacked) M:0x06 F:0x45 N:0x1f\n\r'
    b'[Boot] Remapped Board ID (table)     : 0x68bf (bitpacked) M:0x06 F:0x45 N:0x1f\n\r'
)


class ExtendedBootTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dump = load_default_dump()
        cls.runs = {}
        profiles = (('absent', None, None),
                    ('erased', bytes([0xff]) * 0x10000, None),
                    ('panel-low', None, 0))
        for name, image, panel_word in profiles:
            events = []

            def record(event, output=events):
                if (event['kind'] == 'uart-transmitted' or
                        event['kind'] == 'mmio-write' and
                        (event['address'] == UART_TX or 0xbd0b2000 <= event['address'] < 0xbd0b2200) or
                        event['kind'] == 'mmio-read' and 0xbd0b1400 <= event['address'] < 0xbd0b1420):
                    output.append(event)

            record.fast_mmio = True
            emulator = Emulator(cls.dump, eeprom=image, panel_input_word=panel_word,
                                native_observer=True, on_event=record)
            result = emulator.run(max_instructions=3_000_000, stop='budget')
            cls.runs[name] = (emulator, result, events)

    @classmethod
    def tearDownClass(cls):
        cls.runs.clear()

    def test_original_firmware_reaches_board_diagnostics(self):
        self.assertEqual(len(EXPECTED_UART), 578)
        for name, (_, result, _) in self.runs.items():
            with self.subTest(profile=name):
                self.assertEqual(result.uart, EXPECTED_UART)
                self.assertEqual(result.reason, 'fault')
                if name == 'panel-low':
                    self.assertIn('0xbd0b1408', result.error)
                    self.assertEqual(result.pc, 0xa000d922)
                else:
                    self.assertIn('0xbd0b1410', result.error)
                    self.assertEqual(result.pc, 0xa000e2e0)
                self.assertLess(result.instructions, 3_000_000)

    def test_every_uart_byte_has_a_real_store_and_delayed_completion(self):
        for name, (emulator, result, events) in self.runs.items():
            with self.subTest(profile=name):
                writes = emulator.uart_writes
                self.assertEqual(bytes(item['value'] & 0xff for item in writes), result.uart)
                self.assertEqual({item['address'] for item in writes}, {UART_TX})
                self.assertEqual({item['size'] for item in writes}, {2})
                self.assertTrue(all(0xa0005720 <= item['pc'] < 0xa001082c for item in writes))
                issued = [item for item in events if item['kind'] == 'mmio-write'
                          and item['address'] == UART_TX]
                completed = [item for item in events if item['kind'] == 'uart-transmitted']
                self.assertEqual(len(issued), len(result.uart))
                self.assertEqual(len(completed), len(result.uart))
                for sent, received in zip(issued, completed):
                    self.assertEqual(sent['value'] & 0xff, received['value'])
                    self.assertGreater(received['instruction'], sent['instruction'])
                self.assertIsNone(emulator.board.uart_tx_pending)

    def test_gpio_initialization_runs_before_panel_boundary(self):
        for name, (emulator, _, events) in self.runs.items():
            with self.subTest(profile=name):
                gpio = emulator.board.gpio
                self.assertEqual(gpio.external_inputs, {})
                for pin in (7, 28):
                    self.assertEqual(gpio.output_latches[pin], 1)
                    self.assertEqual(gpio.directions[pin], 1)
                    # Firmware preloads the output, then selects output mode.
                    pin_events = [(item['address'], item['value']) for item in events
                                  if item['kind'] == 'mmio-write' and
                                  item['address'] in (gpio.base + pin * 8 + 2,
                                                      gpio.base + pin * 8 + 4)]
                    self.assertEqual(pin_events[:2], [(gpio.base + pin * 8 + 2, 1),
                                                     (gpio.base + pin * 8 + 4, 1)])
                self.assertEqual(gpio.directions[11], 0)
                self.assertEqual(gpio.event_status, [0] * gpio.pin_count)

    def test_unknown_panel_input_remains_a_strict_boundary(self):
        for name in ('absent', 'erased'):
            emulator, result, events = self.runs[name]
            with self.subTest(profile=name):
                self.assertIsNone(emulator.board.panel.input_word)
                self.assertIn('input', result.error)
                self.assertIn('unknown', result.error)
                self.assertTrue(emulator.board.panel.output_writes)
                self.assertFalse(any(item['kind'] == 'mmio-read' for item in events))

    def test_static_panel_input_reaches_unmodeled_byte_engine(self):
        emulator, result, events = self.runs['panel-low']
        panel = emulator.board.panel
        self.assertEqual(panel.input_word, 0)
        self.assertGreaterEqual(len(panel.output_writes), 394)
        self.assertEqual(panel.configuration[panel.CONFIG], 0x140)
        self.assertEqual(panel.configuration[panel.CONTROL], 0)
        self.assertEqual(emulator.board.gpio.directions[26], 0)
        self.assertEqual(emulator.board.gpio.output_latches[26], 0)
        reads = [item for item in events if item['kind'] == 'mmio-read']
        self.assertGreaterEqual(len(reads), 64)
        self.assertEqual({item['address'] for item in reads}, {panel.INPUT})
        self.assertEqual({item['value'] for item in reads}, {0})
        self.assertIn('completion semantics', result.error)
        self.assertGreater(result.instructions, 2_000_000)

    def test_absent_eeprom_follows_negative_probe_and_error_path(self):
        emulator, _, _ = self.runs['absent']
        transactions = emulator.board.i2c.transactions
        self.assertEqual(len(transactions), 2)
        self.assertEqual([item['slave'] for item in transactions], [0x7f, 0x50])
        for item in transactions:
            self.assertTrue(item['read'])
            self.assertFalse(item['ack'])
            self.assertFalse(item['completed'])
            self.assertTrue(item['aborted'])
            self.assertIsNone(item['data'])

    def test_erased_eeprom_returns_backing_bytes_through_i2c(self):
        emulator, _, _ = self.runs['erased']
        transactions = emulator.board.i2c.transactions
        self.assertEqual(len(transactions), 201)
        self.assertEqual(transactions[0]['slave'], 0x7f)
        self.assertFalse(transactions[0]['ack'])
        self.assertTrue(transactions[0]['aborted'])
        reads = transactions[1:]
        self.assertEqual([item['address'] for item in reads], list(range(200)))
        for item in reads:
            self.assertEqual(item['slave'], 0x50)
            self.assertTrue(item['read'])
            self.assertTrue(item['ack'])
            self.assertTrue(item['completed'])
            self.assertFalse(item['aborted'])
            self.assertEqual(item['data'], 0xff)
        self.assertEqual(emulator.eeprom.image, bytes([0xff]) * 0x10000)

    def test_mcasic_identity_and_analog_inputs_are_exercised(self):
        for name, (emulator, _, _) in self.runs.items():
            with self.subTest(profile=name):
                transactions = emulator.board.micci[0xbd300800].transactions
                self.assertEqual(transactions[0], {
                    'bus': 0, 'device': 6, 'register': 0, 'read': True, 'data': 0x8014,
                })
                self.assertTrue(all(item['device'] == 6 and item['bus'] == 0
                                    for item in transactions))
                conversions = emulator.mcasic.adc_conversions
                reference = [item for item in conversions if item['selector'] == 15]
                supply = [item for item in conversions if item['selector'] == 0]
                self.assertGreaterEqual(len(reference), 5)
                self.assertTrue(all(item['sample'] == 512 for item in reference))
                self.assertTrue(supply)
                self.assertTrue(all(item['sample'] == 875 for item in supply))
                self.assertEqual(emulator.mcasic.analog_profile['supply_mv'], 24000)
                self.assertFalse(emulator.mcasic.analog_profile['supply_measured'])
                self.assertFalse(emulator.mcasic.adc_busy)

    def test_physical_and_disk_flash_remain_unchanged(self):
        for name, (emulator, _, _) in self.runs.items():
            with self.subTest(profile=name):
                self.assertTrue(emulator.memory.check_flash())
        self.assertEqual(DUMP_PATH.read_bytes(), self.dump)


class SerialPanelBootTests(unittest.TestCase):
    """A connected, explicit panel profile advances the unmodified boot code."""

    @classmethod
    def setUpClass(cls):
        cls.dump = load_default_dump()
        cls.events = []

        def record(event):
            if (event['kind'] == 'uart-transmitted' or
                    event['kind'] == 'mmio-write' and event['address'] == UART_TX or
                    event['kind'].startswith('mmio-') and 0xbd063800 <= event['address'] < 0xbd063820):
                cls.events.append(event)

        record.fast_mmio = True
        cls.emulator = Emulator(cls.dump, serial_panel_inputs=0x3ffff,
                                native_observer=True, on_event=record)
        cls.result = cls.emulator.run(max_instructions=3_000_000, stop='budget')

    @classmethod
    def tearDownClass(cls):
        cls.emulator = None

    def test_real_reset_passes_panel_detection_and_reaches_spi_transfer(self):
        expected = (EXPECTED_UART +
                    b'[Boot] NVM powerdown state = 0x00000003\n\r'
                    b'[nos_ui] _powerup_buttons: 0x20002000\n\r'
                    b'[Boot] Load mode = 0x00000004\n\r'
                    b'[Boot] read NOS_NVM_FWUPDATE_STATUS = 0xaa\n\r'
                    b'[Boot] SPI Device 0 :')
        self.assertEqual(self.result.uart, expected)
        self.assertEqual(self.result.reason, 'fault')
        self.assertEqual(self.result.pc, 0xa000cee4)
        self.assertIn('control bit 5', self.result.error)
        self.assertIn('tx=0x9f000000', self.result.error)
        self.assertEqual(self.emulator.board.spi.control, 0x23)

    def test_panel_frames_are_physical_output_bits_and_explicit_inputs(self):
        endpoint = self.emulator.board.panel.endpoint
        self.assertEqual(endpoint.raw_inputs, 0x3ffff)
        self.assertEqual(len(endpoint.frames), 3)
        self.assertEqual(endpoint.aborted_frames, [])
        for frame in endpoint.frames:
            self.assertEqual(frame, {'bit_count': 64, 'output': 0x0f00, 'raw_inputs': 0x3ffff})
        # The original driver classified this as its serial-panel protocol.
        self.assertEqual(bytes(self.emulator.uc.mem_read(0x60001058, 4)), b'\0\0\0\1')
        self.assertEqual(self.emulator.board.panel.configuration[0xbd0b140e], 0)

    def test_added_uart_is_still_firmware_output_after_transmission(self):
        writes = self.emulator.uart_writes
        # The final space has been written but its frame is still in flight
        # when the unsupported SPI store stops the CPU and virtual clock.
        self.assertEqual(bytes(item['value'] & 0xff for item in writes), self.result.uart + b' ')
        completed = [event for event in self.events if event['kind'] == 'uart-transmitted']
        issued = [event for event in self.events if event['kind'] == 'mmio-write'
                  and event['address'] == UART_TX]
        self.assertEqual(bytes(event['value'] for event in completed), self.result.uart)
        self.assertEqual(len(writes), len(completed) + 1)
        for sent, finished in zip(issued, completed):
            self.assertGreater(finished['instruction'], sent['instruction'])
        self.assertEqual(self.emulator.board.uart_tx_pending, ord(' '))

    def test_flash_and_instruction_clock_remain_consistent(self):
        self.assertTrue(self.emulator.memory.check_flash())
        self.assertEqual(DUMP_PATH.read_bytes(), self.dump)
        self.assertEqual(self.emulator.board.ticks, self.result.instructions)


if __name__ == '__main__':
    unittest.main()
