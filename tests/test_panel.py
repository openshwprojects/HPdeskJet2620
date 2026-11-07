import unittest

from hardware import BootHardware, HardwareError
from panel import PanelError, PanelPort, SerialShiftPanel


class PanelPortTests(unittest.TestCase):
    def test_output_clocks_do_not_invent_a_panel_response(self):
        board = BootHardware()
        for word in (9, 11, 15, 13, 9):
            board.write(PanelPort.OUTPUT, 2, word)
            board.advance(100)
        self.assertEqual(board.panel.output_writes, [9, 11, 15, 13, 9])
        self.assertEqual(board.panel.output_word, 9)
        self.assertEqual(board.read(PanelPort.OUTPUT, 2), 9)
        with self.assertRaisesRegex(HardwareError, 'input.*unknown'):
            board.read(PanelPort.INPUT, 2)

    def test_external_input_can_change_independently_of_output(self):
        board = BootHardware(panel_input_word=0)
        self.assertEqual(board.read(PanelPort.INPUT, 2), 0)
        board.panel.drive_input(0x100)
        board.write(PanelPort.OUTPUT, 2, 9)
        self.assertEqual(board.read(PanelPort.INPUT, 2), 0x100)
        board.panel.drive_input(None)
        with self.assertRaises(HardwareError):
            board.read(PanelPort.INPUT, 2)

    def test_unsupported_bits_directions_and_widths_stop(self):
        board = BootHardware()
        for address, size, value in ((PanelPort.OUTPUT, 2, 0x10),
                                     (PanelPort.OUTPUT, 4, 9),
                                     (PanelPort.INPUT, 2, 0)):
            with self.subTest(address=address, size=size, value=value):
                with self.assertRaises(HardwareError):
                    board.write(address, size, value)
        self.assertEqual(board.panel.output_writes, [])

    def test_invalid_external_words_are_rejected(self):
        for value in (-1, 65536, 1.5):
            with self.assertRaises(ValueError):
                PanelPort(value)

    def test_alternate_byte_engine_does_not_fake_completion_or_timeout(self):
        board = BootHardware(panel_input_word=0)
        board.write(PanelPort.OUTPUT, 2, 0)
        board.write(PanelPort.CONFIG, 2, 0x140)
        board.write(PanelPort.CONTROL, 2, 0)
        self.assertEqual(board.panel.configuration[PanelPort.CONFIG], 0x140)
        with self.assertRaisesRegex(HardwareError, 'unresolved completion'):
            board.write(PanelPort.TX, 2, 1)
        board.advance(100000)
        for address in (PanelPort.STATUS, PanelPort.RX):
            with self.assertRaisesRegex(HardwareError, 'not established'):
                board.read(address, 2)


class SerialShiftPanelTests(unittest.TestCase):
    def test_board_gpio26_strobe_commits_a_real_clocked_frame(self):
        board = BootHardware(serial_panel_inputs=0x3ffff)
        direction = board.gpio.base + 26 * 8 + 4
        output = board.gpio.base + 26 * 8 + 2
        board.write(PanelPort.OUTPUT, 2, 9)
        board.write(output, 2, 0)
        self.assertIsNone(board.panel.endpoint.strobe)
        board.write(direction, 2, 1)
        board.write(output, 2, 1)
        outgoing = 0xa123456789abcdef
        incoming = 0
        for index in range(64):
            word = 9 | (((outgoing >> index) & 1) << 2)
            board.write(PanelPort.OUTPUT, 2, word)
            board.write(PanelPort.OUTPUT, 2, word | 2)
            board.advance(3)
            if index < 34:
                incoming |= (board.read(PanelPort.INPUT, 2) >> 8) << index
            board.write(PanelPort.OUTPUT, 2, word)
        self.assertEqual(incoming, 0x3ffff | (0x35 << 18) | (0xef << 26))
        self.assertEqual(board.panel.endpoint.frames, [])
        board.write(output, 2, 0)
        board.write(output, 2, 1)
        self.assertEqual(board.panel.endpoint.frames[0]['output'], outgoing)
        board.write(direction, 2, 0)
        self.assertIsNone(board.panel.endpoint.strobe)
        with self.assertRaisesRegex(HardwareError, 'no active'):
            board.read(PanelPort.INPUT, 2)

        # Direct active-low GPIO LEDs: power_led on GPIO 28, wireless_led on GPIO 0
        self.assertEqual(board.panel.endpoint.gpio_leds, {'power_led': False, 'wireless_led': False})
        board.write(board.gpio.base + 28 * 8 + 2, 2, 0)
        board.write(board.gpio.base + 28 * 8 + 4, 2, 1)
        self.assertEqual(board.panel.endpoint.gpio_leds, {'power_led': True, 'wireless_led': False})
        board.write(board.gpio.base + 0 * 8 + 2, 2, 0)
        board.write(board.gpio.base + 0 * 8 + 4, 2, 1)
        self.assertEqual(board.panel.endpoint.gpio_leds, {'power_led': True, 'wireless_led': True})
        board.write(board.gpio.base + 28 * 8 + 2, 2, 1)
        board.write(board.gpio.base + 0 * 8 + 4, 2, 0)
        self.assertEqual(board.panel.endpoint.gpio_leds, {'power_led': False, 'wireless_led': False})

    def start(self, endpoint):
        port = PanelPort(endpoint=endpoint)
        port.write(port.OUTPUT, 2, 9)
        port.set_strobe(0)
        port.set_strobe(1)
        return port

    def clock(self, port, bit):
        word = 9 | (bit << 2)
        port.write(port.OUTPUT, 2, word)
        port.write(port.OUTPUT, 2, word | 2)

    def frame(self, port, outgoing):
        incoming = 0
        endpoint = port.endpoint
        for index in range(endpoint.frame_bits):
            self.clock(port, (outgoing >> index) & 1)
            if index < endpoint.input_width + 16:
                sample = port.read(port.INPUT, 2)
                self.assertEqual(sample, port.read(port.INPUT, 2))
                incoming |= (sample >> 8) << index
            port.write(port.OUTPUT, 2, 9 | (((outgoing >> index) & 1) << 2))
        port.set_strobe(0)
        port.set_strobe(1)
        return incoming

    def test_clocked_inputs_signature_and_feedback_from_actual_output(self):
        endpoint = SerialShiftPanel(0x2abcd)
        port = self.start(endpoint)
        outgoing = 0x98123456fedcba69
        incoming = self.frame(port, outgoing)
        self.assertEqual(incoming, 0x2abcd | (0x35 << 18) | (0x69 << 26))
        self.assertEqual(endpoint.frames, [{'bit_count': 64, 'output': outgoing,
                                            'raw_inputs': 0x2abcd}])

    def test_raw_inputs_latch_at_strobe_and_are_not_auto_inverted(self):
        endpoint = SerialShiftPanel(0x3ffff)
        port = self.start(endpoint)
        endpoint.set_inputs(0)
        self.assertEqual(self.frame(port, 0) & 0x3ffff, 0x3ffff)
        self.assertEqual(self.frame(port, 0) & 0x3ffff, 0)

    def test_wrong_signature_reaches_wire_unchanged(self):
        endpoint = SerialShiftPanel(0, signature=0x34)
        port = self.start(endpoint)
        incoming = self.frame(port, 0xa5)
        self.assertEqual((incoming >> 18) & 0xffff, 0xa534)
        self.assertNotEqual((incoming >> 18) & 0xffff, 0xa535)

    def test_reads_and_data_only_writes_do_not_shift(self):
        endpoint = SerialShiftPanel(1)
        port = self.start(endpoint)
        self.clock(port, 1)
        for word in (15, 11, 15):
            port.write(port.OUTPUT, 2, word)
            self.assertEqual(port.read(port.INPUT, 2), 0x100)
        self.assertEqual(endpoint.bit_count, 1)
        self.assertEqual(endpoint.shifted_output, 1)

    def test_reset_and_disconnection_abort_without_latching_output(self):
        endpoint = SerialShiftPanel(0)
        port = self.start(endpoint)
        self.clock(port, 1)
        port.write(port.OUTPUT, 2, 1)
        self.assertEqual(endpoint.frames, [])
        self.assertEqual(endpoint.aborted_frames[0]['reason'], 'reset')
        with self.assertRaisesRegex(PanelError, 'no active'):
            port.read(port.INPUT, 2)
        port.write(port.OUTPUT, 2, 9)
        port.set_strobe(0)
        port.set_strobe(1)
        self.clock(port, 1)
        port.set_strobe(None)
        self.assertEqual(endpoint.aborted_frames[-1]['reason'], 'strobe disconnected')

    def test_incomplete_and_overlong_frames_are_rejected(self):
        endpoint = SerialShiftPanel(0)
        port = self.start(endpoint)
        self.clock(port, 1)
        port.set_strobe(0)
        with self.assertRaisesRegex(PanelError, 'before a complete'):
            port.set_strobe(1)
        self.assertEqual(endpoint.frames, [])
        port = self.start(SerialShiftPanel(0))
        for _ in range(64):
            self.clock(port, 0)
        with self.assertRaisesRegex(PanelError, 'exceeds'):
            self.clock(port, 0)
        self.assertEqual(port.output_word, 9)
        self.assertEqual(port.endpoint.output_word, port.output_word)
        self.assertEqual(port.endpoint.bit_count, 64)

    def test_unspecified_tail_stays_unknown_and_can_be_explicitly_driven(self):
        for tail in (None, 0, 1):
            port = self.start(SerialShiftPanel(0, tail_bit=tail))
            for _ in range(35):
                self.clock(port, 0)
            if tail is None:
                with self.assertRaisesRegex(PanelError, 'tail is unknown'):
                    port.read(port.INPUT, 2)
            else:
                self.assertEqual(port.read(port.INPUT, 2), tail << 8)

    def test_invalid_profiles_and_conflicting_drivers_are_rejected(self):
        for kwargs in ({'raw_inputs': -1}, {'raw_inputs': 1 << 18},
                       {'raw_inputs': 0, 'signature': 256},
                       {'raw_inputs': 0, 'input_width': 0},
                       {'raw_inputs': 0, 'frame_bits': 20},
                       {'raw_inputs': 0, 'reset_active_level': 2},
                       {'raw_inputs': 0, 'tail_bit': 2}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                SerialShiftPanel(**kwargs)
        with self.assertRaisesRegex(ValueError, 'mutually exclusive'):
            PanelPort(0, endpoint=SerialShiftPanel(0))

    def test_button_press_release_and_last_output(self):
        endpoint = SerialShiftPanel(0x3ffff)
        self.assertIsNone(endpoint.last_output)
        self.assertFalse(endpoint.is_button_pressed(5))
        endpoint.press_button(5)
        self.assertTrue(endpoint.is_button_pressed(5))
        self.assertEqual(endpoint.raw_inputs, 0x3ffff & ~(1 << 5))
        port = self.start(endpoint)
        outgoing = 0x1122334455667788
        incoming = self.frame(port, outgoing)
        self.assertEqual(incoming & 0x3ffff, 0x3ffff & ~(1 << 5))
        self.assertEqual(endpoint.last_output, outgoing)
        endpoint.release_button(5)
        self.assertFalse(endpoint.is_button_pressed(5))
        self.assertEqual(endpoint.raw_inputs, 0x3ffff)
        for bad_index in (-1, 18, '0', 'unknown_btn', True, False):
            with self.subTest(bad_index=bad_index), self.assertRaises(ValueError):
                endpoint.press_button(bad_index)
        with self.assertRaises(ValueError):
            endpoint.set_inputs(True)

    def test_named_buttons_key_ids_and_display_decoding(self):
        endpoint = SerialShiftPanel(0x3ffff)
        with self.assertRaisesRegex(PanelError, 'no latched'):
            endpoint.decode_frame()
        self.assertIsNone(endpoint.logical_output)

        # Press and release buttons by name and by firmware key ID
        endpoint.press_button('cancel')
        endpoint.press_button(0x202D)  # copy_color (bit 8)
        self.assertTrue(endpoint.is_button_pressed(6))
        self.assertTrue(endpoint.is_button_pressed('cancel'))
        self.assertTrue(endpoint.is_button_pressed(0x2004))
        self.assertTrue(endpoint.is_button_pressed('copy_color'))
        self.assertEqual(endpoint.pressed_buttons, ('cancel', 'copy_color'))
        endpoint.release_button(0x2004)
        endpoint.release_button('copy_color')
        self.assertEqual(endpoint.pressed_buttons, ())

        # Press and release multi-button chords by name, key ID, and packed word
        for chord_name, ((btn_a, btn_b), key_id, packed_word) in SerialShiftPanel.BUTTON_CHORDS.items():
            endpoint.press_chord(chord_name)
            self.assertTrue(endpoint.is_chord_pressed(chord_name))
            self.assertTrue(endpoint.is_chord_pressed(key_id))
            self.assertTrue(endpoint.is_chord_pressed(packed_word))
            self.assertIn(chord_name, endpoint.active_chords)
            self.assertTrue(endpoint.is_button_pressed(btn_a))
            self.assertTrue(endpoint.is_button_pressed(btn_b))
            self.assertEqual(SerialShiftPanel.decode_key_event(key_id), chord_name)
            self.assertEqual(SerialShiftPanel.decode_key_event(packed_word), chord_name)
            endpoint.release_chord(key_id)
            self.assertFalse(endpoint.is_chord_pressed(chord_name))
            self.assertEqual(endpoint.pressed_buttons, ())

        for btn_name, (bit, key_id) in SerialShiftPanel.BUTTONS.items():
            self.assertEqual(SerialShiftPanel.decode_key_event(bit), btn_name)
            self.assertEqual(SerialShiftPanel.decode_key_event(key_id), btn_name)
        endpoint.press_chord(0x0306FFFF)
        endpoint.release_all_buttons()
        self.assertEqual(endpoint.pressed_buttons, ())
        for bad_chord in ('bad_chord', 0x1234, True):
            with self.subTest(bad_chord=bad_chord), self.assertRaises(ValueError):
                endpoint.press_chord(bad_chord)
        with self.assertRaises(ValueError):
            SerialShiftPanel.decode_key_event(True)

        # Verify every 7-segment character in SEVEN_SEG_CHARS decodes accurately
        port = self.start(endpoint)
        for seg_set, expected_char in SerialShiftPanel.SEVEN_SEG_CHARS.items():
            logical = 0
            for seg in seg_set:
                bit, _ = SerialShiftPanel.SEVEN_SEG_BITS[seg]
                logical |= 1 << bit
            raw = logical ^ SerialShiftPanel.OUTPUT_POLARITY_MASK
            self.frame(port, raw)
            decoded = endpoint.decode_frame()
            self.assertEqual(decoded['logical_output'], logical)
            self.assertEqual(decoded['seven_seg_char'], expected_char)
            self.assertEqual(frozenset(decoded['seven_seg_segments']), seg_set)

        # Verify full booted-state frame (0xfa01a87c00) decoding and ASCII rendering
        endpoint.set_gpio_led('power_led', True)
        with self.assertRaises(ValueError):
            endpoint.set_gpio_led('unknown_led', True)
        boot_raw = 0x000000FA01A87C00
        info = endpoint.decode_frame(boot_raw)
        self.assertEqual(info['logical_output'], 0x000000FA01A87300)
        self.assertEqual(info['seven_seg_char'], '0')
        self.assertEqual(info['wifi_bars'], 1)              # bit 24 is the smallest Wi-Fi bar
        self.assertEqual(info['gpio_leds'], {'power_led': True, 'wireless_led': False})
        self.assertIn('power_led', info['active_indicators'])
        self.assertIn('wifi_bar_1', info['active_indicators'])
        self.assertIn('ink_drop_icon', info['active_indicators'])
        self.assertIn('color_ink_outline', info['active_indicators'])
        self.assertIn('black_ink_outline', info['active_indicators'])
        self.assertIn('paper_icon', info['active_indicators'])
        self.assertIn('exclamation_icon', info['active_indicators'])
        for led_or_icon in ('wifi_icon', 'ink_led', 'resume_led'):
            self.assertIn(led_or_icon, info['active_indicators'])
        self.assertNotIn('info_led', info['active_indicators'])
        rendered = endpoint.render_display(boot_raw)
        self.assertIn("Char:  '0'", rendered)
        self.assertIn('Wi-Fi: ((o)) [1/5]', rendered)
        self.assertIn('[C:...]', rendered)
        self.assertIn('[K:...]', rendered)
        self.assertIn('GPIO LEDs:  power_led=ON, wireless_led=OFF', rendered)

    def test_encode_frame_ui_lite_commands_and_temporal_analysis(self):
        endpoint = SerialShiftPanel(0x3ffff)
        with self.assertRaisesRegex(PanelError, 'no latched'):
            endpoint.analyze_frames()

        # Verify encode_frame round-trips through decode_frame
        encoded = SerialShiftPanel.encode_frame(
            seven_seg_char='e',
            wifi_bars=3,
            color_ink_bars=2,
            black_ink_bars=1,
            indicators=('exclamation_icon', 'letter_e_icon', 0x040A, 45),
        )
        decoded = endpoint.decode_frame(encoded)
        self.assertEqual(decoded['seven_seg_char'], 'E')
        self.assertEqual(decoded['wifi_bars'], 3)
        self.assertEqual(decoded['color_ink_bars'], 2)
        self.assertEqual(decoded['black_ink_bars'], 1)
        for expected_ind in ('exclamation_icon', 'letter_e_icon', 'drop_on_paper_icon', 'unused_bit45',
                             'wifi_icon', 'wifi_bar_1', 'wifi_bar_3', 'color_ink_bar_2', 'color_ink_outline',
                             'black_ink_bar_1', 'black_ink_outline'):
            self.assertIn(expected_ind, decoded['active_indicators'])
        for absent in ('wifi_bar_4', 'color_ink_bar_3', 'black_ink_bar_2'):
            self.assertNotIn(absent, decoded['active_indicators'])
        # Levels fill from the bottom bar (ink) and the smallest bar (Wi-Fi), as seen on the printer.
        logical = decoded['logical_output']
        self.assertEqual([(logical >> b) & 1 for b in (16, 18, 17)], [1, 1, 0])
        self.assertEqual([(logical >> b) & 1 for b in (27, 22, 26)], [1, 0, 0])
        self.assertEqual([(logical >> b) & 1 for b in (24, 28, 29, 30, 31)], [1, 1, 1, 0, 0])

        # Verify resolve_led_id, resolve_mode_id, and build_ui_lite_commands
        self.assertEqual(SerialShiftPanel.resolve_led_id('letter_e_icon'), 0x0373)
        self.assertEqual(SerialShiftPanel.resolve_led_id('power_led'), 0x0372)
        self.assertEqual(SerialShiftPanel.resolve_led_id(0x07D1), 0x0372)
        self.assertEqual(SerialShiftPanel.resolve_led_id('seg_a'), 0x0402)
        self.assertEqual(SerialShiftPanel.resolve_mode_id('solid'), 0x03D6)
        self.assertEqual(SerialShiftPanel.resolve_mode_id(0x03DB), 0x03DB)
        batches = SerialShiftPanel.build_ui_lite_commands(
            seven_seg_char='4',
            wifi_bars=2,
            color_ink_bars=3,
            black_ink_bars=1,
            indicators={'letter_e_icon': 'blink_fast', 'power_led': 'solid'},
            batch_size=3,
        )
        self.assertTrue(all(b.startswith(b'udws "') and b.endswith(b';"') for b in batches))
        joined = b' '.join(batches)
        self.assertIn(b'ui_lite.set_led 972, 982', joined)
        self.assertIn(b'ui_lite.set_led 883, 986', joined)
        self.assertIn(b'ui_lite.set_led 882, 982', joined)

        # Clock a 3-frame sequence with blinking 'E' -> ' ' -> '4', animating Wi-Fi bars 1 -> 2 -> 4, and button presses
        port = self.start(endpoint)
        endpoint.set_gpio_led('power_led', True)
        endpoint.set_gpio_led('wireless_led', False)
        endpoint.press_button('cancel')
        self.frame(
            port,
            SerialShiftPanel.encode_frame(
                seven_seg_char='E',
                wifi_bars=1,
                color_ink_bars=3,
                indicators=('exclamation_icon',),
            ),
        )
        endpoint.release_all_buttons()
        endpoint.set_gpio_led('wireless_led', True)
        endpoint.press_chord('wireless_resume')
        self.frame(
            port,
            SerialShiftPanel.encode_frame(
                seven_seg_char=' ',
                wifi_bars=2,
                color_ink_bars=3,
            ),
        )
        endpoint.release_all_buttons()
        endpoint.set_gpio_led('wireless_led', False)
        self.frame(
            port,
            SerialShiftPanel.encode_frame(
                seven_seg_char='4',
                wifi_bars=4,
                color_ink_bars=3,
                indicators=('exclamation_icon',),
            ),
        )
        summary = endpoint.analyze_frames()
        self.assertEqual(summary['frame_count'], 3)
        self.assertEqual(summary['seven_seg_sequence'], ('E', ' ', '4'))
        self.assertTrue(summary['seven_seg_blinking'])
        self.assertEqual(summary['wifi_bars_range'], (1, 4))
        self.assertTrue(summary['wifi_animating'])
        self.assertEqual(summary['color_ink_bars_range'], (3, 3))
        self.assertIn('power_led', summary['steady_indicators'])
        self.assertIn('color_ink_outline', summary['steady_indicators'])
        self.assertIn('exclamation_icon', summary['blinking_indicators'])
        self.assertIn('wireless_led', summary['blinking_indicators'])
        self.assertIn('wifi_bar_4', summary['blinking_indicators'])
        self.assertEqual(summary['button_events'], ('cancel', 'wireless_resume'))
        temp_render = endpoint.render_display(show_temporal=True)
        self.assertIn('Steady:', temp_render)
        self.assertIn('Blinking:', temp_render)
        self.assertIn("7seg=['E' -> ' ' -> '4']", temp_render)
        self.assertIn('wifi=1..4/5', temp_render)


class PanelLiveFirmwareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from analysis.uart_selftest import load_default_dump_and_snapshot

        loaded = load_default_dump_and_snapshot()
        if loaded is None:
            raise unittest.SkipTest('dump.bin or Stage-22 snapshot not available')
        cls.dump, cls.snap = loaded

    def test_live_firmware_serial_panel_button_and_led_frames(self):
        from analysis.uart_selftest import (
            REGISTERS,
            UC_ARM_REG_CPSR,
            build_and_restore,
            capture_snapshot,
            step_restored,
        )

        warm_emu = build_and_restore(self.dump, self.snap, native_observer=True)
        warm_ep = warm_emu.board.panel.endpoint
        self.assertIsNotNone(warm_ep)
        self.assertEqual(warm_ep.gpio_leds, {'power_led': True, 'wireless_led': False})
        warm_ep.press_button('resume')
        self.assertTrue(warm_ep.is_button_pressed(2))
        self.assertTrue(warm_ep.is_button_pressed(0x2028))
        step_restored(warm_emu, 240_000)
        warm_snap = capture_snapshot(warm_emu)
        del warm_emu

        outcomes = []
        for native in (False, True):
            emu = None
            emu = build_and_restore(self.dump, warm_snap, native_observer=native)
            ep = emu.board.panel.endpoint
            ep.press_button('resume')
            step_restored(emu, 50_000)
            self.assertGreaterEqual(len(ep.frames), 1)
            self.assertEqual(ep.frames[-1]['raw_inputs'], 0x3ffff & ~(1 << 2))
            self.assertIsNotNone(ep.last_output)
            boot_info = ep.decode_frame()
            self.assertEqual(boot_info['seven_seg_char'], '0')
            self.assertEqual(boot_info['gpio_leds'], {'power_led': True, 'wireless_led': True})
            self.assertIn('power_led', boot_info['active_indicators'])
            self.assertIn('wireless_led', boot_info['active_indicators'])
            self.assertIn('wifi_bar_1', boot_info['active_indicators'])
            self.assertIn('ink_drop_icon', boot_info['active_indicators'])
            outcomes.append(
                (
                    emu.instructions,
                    emu.board.ticks,
                    tuple(emu.uc.reg_read(r) for r in REGISTERS),
                    emu.uc.reg_read(UC_ARM_REG_CPSR),
                    list(ep.frames),
                    dict(ep.gpio_leds),
                    bytes(emu.uart),
                )
            )
        self.assertEqual(outcomes[0], outcomes[1])
        emu.board.uart_frame_ticks = 1
        emu.board.queue_rx(b'udws "fp.num_user_keys_down;"\r')
        step_restored(emu, 285_000)
        self.assertIn(b'1;udws() returns 0 (0x0)', bytes(emu.uart))

    def test_live_firmware_display_test_mode_and_seven_segment_control(self):
        import struct
        from analysis.uart_selftest import (
            build_and_restore,
            step_restored,
        )

        done = [False]
        cursor_ref = [0]

        def on_uart_byte(val: int) -> None:
            if (val == 0x20 and emu.uart.endswith(b'-> ')
                    and not emu.board.uart_rx
                    and b'udws() returns 0' in emu.uart[cursor_ref[0]:]):
                done[0] = True
                emu.reason = 'udws-done'
                emu.uc.emu_stop()

        on_uart_byte.trigger_byte = 0x13E20
        emu = build_and_restore(self.dump, self.snap, on_uart=on_uart_byte)
        ep = emu.board.panel.endpoint
        self.assertEqual(ep.gpio_leds, {'power_led': True, 'wireless_led': False})

        def run_udws(cmd: bytes, max_steps: int = 1_500_000) -> bytes:
            cursor_ref[0] = len(emu.uart)
            done[0] = False
            emu.board.queue_rx(cmd + b'\r')
            step_restored(emu, max_steps)
            self.assertTrue(done[0])
            return bytes(emu.uart[cursor_ref[0]:])

        def _stop_after(extra: int = 0) -> None:
            cur = emu._native_observer.state.instructions if emu._native_observer is not None else emu.instructions
            target = cur + max(0, extra)
            emu._limit = target
            if emu._native_observer is not None:
                emu._native_observer.state.limit = target

        def wait_char(expected_char: str) -> None:
            if ep.last_output is not None and ep.decode_frame()['seven_seg_char'] == expected_char:
                return

            def _on_frame(_frame: dict) -> None:
                if ep.decode_frame()['seven_seg_char'] == expected_char:
                    ep.on_frame = None
                    _stop_after(0)

            ep.on_frame = _on_frame
            try:
                step_restored(emu, 400_000)
            finally:
                ep.on_frame = None

        def wait_frame(extra: int = 2_000) -> None:
            def _on_frame(_frame: dict) -> None:
                ep.on_frame = None
                _stop_after(extra)

            ep.on_frame = _on_frame
            try:
                step_restored(emu, 400_000)
            finally:
                ep.on_frame = None

        emu.board.uart_frame_ticks = 1
        run_udws(b'udws "fp.test_enter;"')
        wait_char(' ')
        enter_info = ep.decode_frame()
        self.assertEqual(enter_info['seven_seg_char'], ' ')
        self.assertEqual(enter_info['gpio_leds'], {'power_led': False, 'wireless_led': False})

        run_udws(b'udws "fp.test_leds_on;"')
        wait_char('8')
        all_on = ep.decode_frame()
        self.assertEqual(all_on['logical_output'] | 0x300000, 0x000020FFFFFFFB00)
        self.assertEqual(all_on['seven_seg_char'], '8')
        self.assertEqual(all_on['wifi_bars'], 5)
        self.assertEqual(all_on['color_ink_bars'], 3)
        self.assertEqual(all_on['black_ink_bars'], 3)
        self.assertEqual(all_on['gpio_leds'], {'power_led': True, 'wireless_led': True})
        self.assertIn('power_led', all_on['active_indicators'])
        self.assertIn('wireless_led', all_on['active_indicators'])

        # Turn all LEDs off, light segments b (bit 38) and c (bit 37) to display '1', and turn power_led (0x07D1=2001) ON
        run_udws(b'udws "fp.test_leds_off; fp.set_led_state 38, 1, 0; fp.set_led_state 37, 1, 0; fp.set_led_state 2001, 1, 0;"')
        wait_char('1')
        digit_one = ep.decode_frame()
        self.assertEqual(digit_one['seven_seg_char'], '1')
        self.assertEqual(digit_one['gpio_leds'], {'power_led': True, 'wireless_led': False})
        self.assertIn("Char:  '1'", ep.render_display())
        self.assertIn('GPIO LEDs:  power_led=ON, wireless_led=OFF', ep.render_display())

        # Press single buttons and a multi-key chord and verify fp.test_dump_keys records them
        for btn in ('cancel', 'copy_color', 'power'):
            ep.press_button(btn)
            wait_frame()
            ep.release_button(btn)
            wait_frame()
        ep.press_chord('wireless_cancel')
        wait_frame()
        ep.release_chord('wireless_cancel')
        wait_frame()
        dump_out = run_udws(b'udws "fp.test_dump_keys; fp.test_exit;"')
        self.assertIn(b'6, 8, 1, 50790399,', dump_out)
        self.assertEqual(
            [ep.decode_key_event(v) for v in (6, 8, 1, 50790399)],
            ['cancel', 'copy_color', 'power', 'wireless_cancel'],
        )

        # Verify Power-hold 3-digit service combo (copy_color=+100, resume=+10, copy_black=+1)
        ep.press_button('power')
        wait_frame()
        for tap_btn in ('copy_color', 'resume', 'resume', 'copy_black'):
            ep.press_button(tap_btn)
            wait_frame()
            ep.release_button(tap_btn)
            wait_frame()
        tens, hundreds, ones = struct.unpack('>III', emu.uc.mem_read(0x2093C060, 12))
        self.assertEqual((hundreds, tens, ones), (100, 20, 1))
        ep.release_button('power')
        for _ in range(3):
            wait_frame()
            if emu.uc.mem_read(0x2093C05C, 1)[0] == 0:
                break
        self.assertEqual(emu.uc.mem_read(0x2093C05C, 1)[0], 0)

        # Drive color/black ink bar graphs via build_ui_lite_commands and verify multi-frame temporal analysis
        for cmd in ep.build_ui_lite_commands(color_ink_bars=3, black_ink_bars=2):
            run_udws(cmd)
        wait_frame(0)
        ui_lite_info = ep.decode_frame()
        self.assertEqual(ui_lite_info['color_ink_bars'], 3)
        self.assertEqual(ui_lite_info['black_ink_bars'], 2)
        temporal = ep.analyze_frames()
        self.assertIn(' ', temporal['seven_seg_sequence'])
        self.assertIn('8', temporal['seven_seg_sequence'])
        self.assertIn('1', temporal['seven_seg_sequence'])
        self.assertTrue(temporal['seven_seg_blinking'])
        for expected_event in ('cancel', 'copy_color', 'power', 'wireless_cancel'):
            self.assertIn(expected_event, temporal['button_events'])

        # Verify CarriageAsic missing vs. installed cartridges and copy_color workflow
        emu.carriage_asic.configure_cartridges(installed=False)
        emu.carriage_asic.apply_to_firmware(emu)
        missing_out = run_udws(b'udws "ui_lite.refresh_status; ui_lite.get_state;"')
        self.assertIn(b'Error: 0x10, Pen: 0x03', missing_out)

        emu.carriage_asic.configure_cartridges(installed=True, color_bars=3, black_bars=2)
        emu.carriage_asic.apply_to_firmware(emu)
        status_out = run_udws(b'udws "ui_lite.refresh_status; ui_lite.get_state;"')
        self.assertIn(b'Current state: 1, Error: 0x00, Pen: 0x00', status_out)
        wait_frame(0)
        idle_info = ep.decode_frame()
        self.assertEqual((idle_info['color_ink_bars'], idle_info['black_ink_bars']), (3, 2))

        copy_out = run_udws(
            b'udws "ui.send_button 8237, 0; ui.send_button 8237, 1; ui.send_button 8237, 0; ui.send_button 8237, 1; ui_lite.get_state;"'
        )
        self.assertIn(b'Current state: 4', copy_out)
        wait_frame(0)
        self.assertIn('2', ep.analyze_frames()['seven_seg_sequence'])

    def test_configured_alternate_byte_engine_transfers_bytes(self):
        import struct
        from custom_firmware import ArmBeAssembler
        from emulator import Emulator
        from memory import FLASH_SIZE
        from panel import PanelPort

        port = PanelPort(byte_engine_rx=[0x35, 0x7A])
        self.assertEqual(port.read(PanelPort.STATUS, 2), 0)
        port.write(PanelPort.TX, 2, 0xAA)
        self.assertEqual(port.tx_bytes, [0xAA])
        self.assertEqual(port.read(PanelPort.STATUS, 2), 1)
        self.assertEqual(port.read(PanelPort.RX, 2), 0x35)
        self.assertEqual(port.read(PanelPort.STATUS, 2), 0)

        port.write(PanelPort.TX, 2, 0x55)
        self.assertEqual(port.read(PanelPort.STATUS, 2), 1)
        self.assertEqual(port.read(PanelPort.RX, 2), 0x7A)
        with self.assertRaises(ValueError):
            port.configure_byte_engine([True])

        # Verify guest CPU execution of PanelPort byte engine has 100% Python vs. C observer parity
        asm = ArmBeAssembler(base_address=0x00002070)
        asm.ldr_lit(8, 'panel_base')       # r8 = 0xBD0B1400
        asm.ldr_lit(9, 'out_base')         # r9 = 0x20000200
        asm.ldrh_imm(0, 8, 0x14)           # r0 = STATUS before TX (0)
        asm.mov_imm(1, 0xAA)
        asm.strh_imm(1, 8, 0x08)           # TX = 0xAA
        asm.ldrh_imm(2, 8, 0x14)           # r2 = STATUS after TX (1)
        asm.ldrh_imm(3, 8, 0x16)           # r3 = RX (0x35), clears STATUS
        asm.ldrh_imm(4, 8, 0x14)           # r4 = STATUS after RX (0)
        asm.str_imm(0, 9, 0x00)
        asm.str_imm(2, 9, 0x04)
        asm.str_imm(3, 9, 0x08)
        asm.str_imm(4, 9, 0x0C)
        asm.label('halt')
        asm.b('halt')
        asm.label('panel_base')
        asm.emit(0xBD0B1400)
        asm.label('out_base')
        asm.emit(0x20000200)
        code = asm.finalize()

        rom = bytearray(0x2070 + len(code))
        rom[0x00:0x04] = b'\xE5\x9F\xF0\x18'
        rom[0x20:0x24] = (0x00002070).to_bytes(4, 'big')
        rom[0x2070:0x2070 + len(code)] = code
        img = bytes(rom) + (b'\xFF' * (FLASH_SIZE - len(rom)))

        outcomes = []
        for use_native in (False, True):
            emu = Emulator(img, native_observer=use_native)
            emu.board.panel.configure_byte_engine([0x35])
            emu.run(max_instructions=64, stop='budget')
            vals = struct.unpack('>IIII', emu.uc.mem_read(0x20000200, 16))
            outcomes.append((vals, list(emu.board.panel.tx_bytes)))
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(outcomes[0], ((0, 1, 0x35, 0), [0xAA]))

    def test_front_panel_ui_controller_headless_widgets_and_buttons(self):
        from micci import CarriageAsic
        from panel_gui import FrontPanelUIController, SEVEN_SEG_POLYGONS

        ctrl = FrontPanelUIController()
        state = ctrl.set_ui_lite_state(
            seven_seg='8',
            wifi_bars=3,
            color_ink_bars=3,
            black_ink_bars=1,
            indicators=('wireless_attention_icon', 'exclamation_icon', 'letter_e_icon', 'resume_led'),
        )
        self.assertEqual(state['seven_segment']['char'], '8')
        self.assertEqual(set(state['seven_segment']['active_polygons']), set(SEVEN_SEG_POLYGONS))
        self.assertEqual(state['wifi_bars']['bars'], [True, True, True, False, False])
        self.assertTrue(state['wifi_bars']['icon'])
        self.assertTrue(state['wifi_bars']['attention'])
        self.assertTrue(state['leds']['resume_led'])
        self.assertFalse(state['leds']['ink_led'])
        self.assertEqual(state['color_ink']['bars'], [True, True, True])
        self.assertTrue(state['color_ink']['outline'])
        self.assertEqual(state['black_ink']['bars'], [True, False, False])
        self.assertTrue(state['badges']['exclamation_icon'])
        self.assertTrue(state['badges']['letter_e_icon'])
        self.assertFalse(state['badges']['paper_icon'])
        self.assertEqual(ctrl.panel.frames[-1]['bit_count'], 64)

        ctrl.press_button('copy_color')
        self.assertTrue(ctrl.get_widget_state()['buttons']['copy_color'])
        ctrl.release_button('copy_color')
        self.assertFalse(ctrl.get_widget_state()['buttons']['copy_color'])

        asic = CarriageAsic()
        with self.assertRaises(ValueError):
            asic.configure_cartridges(color_bars=True)
        with self.assertRaises(ValueError):
            asic.configure_cartridges(black_bars=4)

    def test_custom_bare_metal_armv5_firmware_boots_with_observer_parity(self):
        import struct
        from custom_firmware import (
            ArmBeAssembler,
            MAILBOX_BASE,
            STATUS_MAGIC_BAD_SIG,
            STATUS_MAGIC_OK,
            build_custom_firmware_image,
            build_stage2_container_image,
        )
        from emulator import Emulator
        from memory import FLASH_SIZE
        from panel import SerialShiftPanel

        custom_img, meta = build_custom_firmware_image(
            seven_seg='A',
            wifi_bars=4,
            color_ink_bars=3,
            black_ink_bars=2,
            indicators=('wireless_attention_icon',),
            power_led=True,
            wireless_led=True,
        )
        helper_panel = SerialShiftPanel(0x3FFFF)
        helper_panel.press_button('copy_color')
        panel_inputs = helper_panel.raw_inputs

        results = []
        for use_native in (False, True):
            emu = Emulator(
                custom_img,
                serial_panel_inputs=panel_inputs,
                native_observer=use_native,
            )
            res = emu.run(max_instructions=20_000, stop='budget')
            ep = emu.board.panel.endpoint
            decoded = ep.decode_frame()
            raw_in, sig, btn_mask, magic = struct.unpack(
                '>IIII', emu.uc.mem_read(MAILBOX_BASE, 16)
            )
            results.append((
                res.pc,
                bytes(emu.uart),
                ep.last_output,
                dict(ep.gpio_leds),
                raw_in,
                sig,
                btn_mask,
                magic,
                decoded['seven_seg_char'],
                decoded['wifi_bars'],
                decoded['color_ink_bars'],
                decoded['black_ink_bars'],
            ))

        self.assertEqual(results[0], results[1])
        (
            pc,
            uart_bytes,
            latched_frame,
            gpio_leds,
            _raw_in,
            sig,
            btn_mask,
            magic,
            seg_char,
            wifi_bars,
            c_bars,
            k_bars,
        ) = results[0]
        self.assertEqual(pc, meta.halt_pc)
        self.assertEqual(uart_bytes, meta.banner)
        self.assertEqual(latched_frame, meta.frame_64)
        self.assertEqual(gpio_leds, {'power_led': True, 'wireless_led': True})
        self.assertEqual(sig, 0x35)
        self.assertEqual(btn_mask, panel_inputs)
        self.assertEqual(magic, STATUS_MAGIC_OK)
        self.assertEqual(seg_char, 'A')
        self.assertEqual((wifi_bars, c_bars, k_bars), (4, 3, 2))

        # Verify custom firmware detects an unexpected panel signature (e.g. 0x12 != 0x35)
        bad_emu = Emulator(custom_img, serial_panel_inputs=panel_inputs, native_observer=True)
        bad_emu.board.panel.endpoint.signature = 0x12
        bad_emu._native_observer.sync_panel()
        bad_emu.run(max_instructions=20_000, stop='budget')
        _, bad_sig, _, bad_magic = struct.unpack('>IIII', bad_emu.uc.mem_read(MAILBOX_BASE, 16))
        self.assertEqual(bad_sig, 0x12)
        self.assertEqual(bad_magic, STATUS_MAGIC_BAD_SIG)

        # Verify custom firmware initialized AXON UART0 pad-mux, baud/FIFO divisors, and watchdog feed
        self.assertEqual(bad_emu.board._stored(0xBD040400, 2), 0x000C)
        self.assertEqual(bad_emu.board._stored(0xBD06080E, 2), 0x0001)
        self.assertEqual(bad_emu.board._stored(0xBD060810, 2), 0x0001)
        self.assertEqual(bad_emu.board._stored(0xBD0B3C00, 2), 0x1000)
        self.assertEqual(bad_emu.board._stored(0xBD0B3C04, 2), 0x03FF)
        self.assertEqual(bad_emu.board._stored(0xBD0B4C00, 2), 0x2111)
        self.assertEqual(bad_emu.board._stored(0xBD0B4C02, 2), 0x2111)

        # Verify ArmBeAssembler bl, bx, mvn_imm, and and_reg mnemonics execute accurately
        sub_asm = ArmBeAssembler(base_address=0x00002070)
        sub_asm.ldr_lit(8, 'out_ptr')
        sub_asm.bl('helper_sub')
        sub_asm.str_imm(0, 8, 0x00)
        sub_asm.label('done')
        sub_asm.b('done')
        sub_asm.label('helper_sub')
        sub_asm.mvn_imm(0, 0)                  # r0 = 0xFFFFFFFF
        sub_asm.mov_imm(1, 0x5A)               # r1 = 0x5A
        sub_asm.and_reg(0, 0, 1)               # r0 = r0 & r1 = 0x5A
        sub_asm.bx(14)                         # bx lr
        sub_asm.label('out_ptr')
        sub_asm.emit(0x20000300)
        sub_code = sub_asm.finalize()
        sub_rom = bytearray(0x2070 + len(sub_code))
        sub_rom[0x00:0x04] = b'\xE5\x9F\xF0\x18'
        sub_rom[0x20:0x24] = (0x00002070).to_bytes(4, 'big')
        sub_rom[0x2070:0x2070 + len(sub_code)] = sub_code
        sub_img = bytes(sub_rom) + (b'\xFF' * (FLASH_SIZE - len(sub_rom)))
        sub_emu = Emulator(sub_img, native_observer=True)
        sub_emu.run(max_instructions=32, stop='budget')
        self.assertEqual(struct.unpack('>I', sub_emu.uc.mem_read(0x20000300, 4))[0], 0x5A)

        # Verify Stage-2 container packaging boots through the unmodified HP Stage-1 bootloader
        from analysis.uart_selftest import load_default_dump
        from custom_firmware import build_stage2_container_image

        stage2_img, stage2_meta = build_stage2_container_image(
            load_default_dump(),
            seven_seg='8',
            wifi_bars=4,
            color_ink_bars=3,
            black_ink_bars=3,
        )
        stage2_emu = Emulator(
            stage2_img,
            serial_panel_inputs=panel_inputs,
            spi_profile='byte-final',
            native_observer=True,
        )
        stage2_res = stage2_emu.run(max_instructions=4_500_000, stop='budget')
        self.assertEqual(stage2_res.pc, stage2_meta.halt_pc)
        self.assertIn(b'[Boot] calling (*0xa0020000)()\n\r', bytes(stage2_emu.uart))
        self.assertTrue(bytes(stage2_emu.uart).endswith(stage2_meta.banner))
        _, s2_sig, s2_btns, s2_magic = struct.unpack('>IIII', stage2_emu.uc.mem_read(MAILBOX_BASE, 16))
        self.assertEqual(s2_sig, 0x35)
        self.assertEqual(s2_btns, panel_inputs)
        self.assertEqual(s2_magic, STATUS_MAGIC_OK)
        self.assertEqual(stage2_emu.board.panel.endpoint.decode_frame()['seven_seg_char'], '8')


if __name__ == '__main__':
    unittest.main()


