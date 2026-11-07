"""Signal registers used by the bit-banged front-panel detection driver.

Both early routine 0xa000e0f6 and main routine 0xa00e8576 establish the serial
panel's input/signature/output-feedback framing; see analysis/panel_deep.md.
The inferred serial endpoint is opt-in. Disconnected input remains unknown,
and the separate byte engine's completion semantics remain unsupported.
"""
from __future__ import annotations


class PanelError(RuntimeError):
    pass


class SerialShiftPanel:
    """Explicit signal-level profile for the firmware's serial panel.

    A GPIO26 low-to-high strobe latches raw active-low inputs and starts a
    frame. Clock rising edges capture MOSI; repeated MISO reads never advance
    it. MISO is input_width input bits, signature byte, then feedback from the
    first captured output byte, all LSB first. A following strobe latches the
    complete output frame. Bit3 is modeled as reset with configurable active
    polarity; the first firmware detection trial uses active-low reset.

    Framing and signature are corroborated by two firmware stages. Actual
    chip type, launch/setup timing, reset polarity and strobe electrical
    behavior remain inferred. Inputs must be supplied explicitly. Input tail
    bits beyond the verified sequence are unknown unless tail_bit is supplied.
    """

    OUTPUT_POLARITY_MASK = 0x0F00

    BUTTONS: dict[str, tuple[int, int]] = {
        'power': (1, 0x2001),
        'resume': (2, 0x2028),
        'wireless': (3, 0x209B),
        'cancel': (6, 0x2004),
        'info': (7, 0x20AB),
        'copy_color': (8, 0x202D),
        'copy_black': (9, 0x202C),
    }
    BUTTON_BITS: dict[int, str] = {bit: name for name, (bit, _) in BUTTONS.items()}
    BUTTON_KEY_IDS: dict[int, int] = {key_id: bit for _, (bit, key_id) in BUTTONS.items()}

    # 8 multi-key chords registered by UI init 0xa044dc22 from table 0x20862c80..0x20862cf0:
    #   name -> ((button_a, button_b), chord_key_id, packed_chord_word)
    BUTTON_CHORDS: dict[str, tuple[tuple[str, str], int, int]] = {
        'wireless_resume': (('wireless', 'resume'), 0x2089, 0x0302FFFF),
        'wireless_cancel': (('wireless', 'cancel'), 0x20AD, 0x0306FFFF),
        'wireless_info': (('wireless', 'info'), 0x207C, 0x0307FFFF),
        'info_cancel': (('info', 'cancel'), 0x207D, 0x0706FFFF),
        'info_resume': (('info', 'resume'), 0x207E, 0x0702FFFF),
        'resume_power': (('resume', 'power'), 0x206F, 0x0201FFFF),
        'copy_color_resume': (('copy_color', 'resume'), 0x206D, 0x0802FFFF),
        'copy_black_resume': (('copy_black', 'resume'), 0x206E, 0x0902FFFF),
    }
    CHORD_KEY_IDS: dict[int, str] = {key_id: name for name, (_, key_id, _) in BUTTON_CHORDS.items()}
    CHORD_WORDS: dict[int, str] = {word: name for name, (_, _, word) in BUTTON_CHORDS.items()}

    # 7-segment display bit mapping from FP_HWDEFS (0x2083a208) and UI driver 0xa0451d5c:
    #   a (top)          = ID 0x0402 -> bit 39 (0x27)
    #   b (top-right)    = ID 0x0403 -> bit 38 (0x26)
    #   c (bottom-right) = ID 0x0404 -> bit 37 (0x25)
    #   d (bottom)       = ID 0x0405 -> bit 36 (0x24)
    #   e (bottom-left)  = ID 0x0406 -> bit 33 (0x21)
    #   f (top-left)     = ID 0x0407 -> bit 35 (0x23)
    #   g (middle)       = ID 0x0408 -> bit 34 (0x22)
    SEVEN_SEG_BITS: dict[str, tuple[int, int]] = {
        'a': (39, 0x0402),
        'b': (38, 0x0403),
        'c': (37, 0x0404),
        'd': (36, 0x0405),
        'e': (33, 0x0406),
        'f': (35, 0x0407),
        'g': (34, 0x0408),
    }

    SEVEN_SEG_CHARS: dict[frozenset[str], str] = {
        frozenset(): ' ',
        frozenset({'a', 'b', 'c', 'd', 'e', 'f'}): '0',
        frozenset({'b', 'c'}): '1',
        frozenset({'a', 'b', 'd', 'e', 'g'}): '2',
        frozenset({'a', 'b', 'c', 'd', 'g'}): '3',
        frozenset({'b', 'c', 'f', 'g'}): '4',
        frozenset({'a', 'c', 'd', 'f', 'g'}): '5',
        frozenset({'a', 'c', 'd', 'e', 'f', 'g'}): '6',
        frozenset({'a', 'b', 'c'}): '7',
        frozenset({'a', 'b', 'c', 'd', 'e', 'f', 'g'}): '8',
        frozenset({'a', 'b', 'c', 'd', 'f', 'g'}): '9',
        frozenset({'a', 'b', 'c', 'e', 'f', 'g'}): 'A',
        frozenset({'c', 'd', 'e', 'f', 'g'}): 'b',
        frozenset({'a', 'd', 'e', 'f'}): 'C',
        frozenset({'b', 'c', 'd', 'e', 'g'}): 'd',
        frozenset({'a', 'd', 'e', 'f', 'g'}): 'E',
        frozenset({'a', 'e', 'f', 'g'}): 'F',
        frozenset({'b', 'c', 'd'}): 'J',
        frozenset({'a', 'b', 'e', 'f', 'g'}): 'P',
        frozenset({'b', 'c', 'd', 'e', 'f'}): 'U',
        frozenset({'a', 'b', 'c', 'e', 'f'}): 'n',
        frozenset({'g'}): '-',
    }
    SEVEN_SEG_BY_CHAR: dict[str, frozenset[str]] = {
        **{ch: segs for segs, ch in SEVEN_SEG_CHARS.items()},
        **{ch.swapcase(): segs for segs, ch in SEVEN_SEG_CHARS.items() if ch.isalpha()},
    }

    # Shift-register indicator bits and UI_LITE IDs from FP_HWDEFS (0x2083a208).
    # The names describe what lights on the owner's printer, seen one bit at a
    # time with the custom firmware's "lcd walk" (analysis/front_panel.md).
    # Bits 8, 9 and 11 are the orange LEDs at the Ink (Cancel), Resume and Info
    # buttons; LEDs are active-low, hence OUTPUT_POLARITY_MASK. Ink bars count
    # from the bottom and Wi-Fi bars from the smallest. Bits 10 and 45 light
    # nothing on the 2620.
    INDICATORS: dict[int, tuple[str, int]] = {
        8: ('ink_led', 0x03FD),
        9: ('resume_led', 0x03BD),
        11: ('info_led', 0x03F9),
        12: ('wifi_icon', 0x03E6),
        13: ('paper_icon', 0x03AD),
        14: ('exclamation_icon', 0x0374),
        15: ('letter_e_icon', 0x0373),
        16: ('color_ink_bar_1', 0x03CE),
        17: ('color_ink_bar_3', 0x03CC),
        18: ('color_ink_bar_2', 0x03CD),
        19: ('color_ink_outline', 0x03FF),
        20: ('wifi_direct_icon', 0x040C),
        21: ('ink_drop_icon', 0x040B),
        22: ('black_ink_bar_2', 0x03D1),
        23: ('black_ink_outline', 0x03FE),
        24: ('wifi_bar_1', 0x03E7),
        25: ('drop_on_paper_icon', 0x040A),
        26: ('black_ink_bar_3', 0x03D0),
        27: ('black_ink_bar_1', 0x03D2),
        28: ('wifi_bar_2', 0x03E8),
        29: ('wifi_bar_3', 0x03E9),
        30: ('wifi_bar_4', 0x03EA),
        31: ('wifi_bar_5', 0x03EB),
        32: ('wireless_attention_icon', 0x03BE),
        45: ('unused_bit45', 0x03C3),
    }
    # Gauge layout: a level lights its bars from the bottom (ink) or from the
    # smallest (Wi-Fi). Within each ink gauge HP's IDs run top to bottom.
    WIFI_BAR_BITS: tuple[int, ...] = (24, 28, 29, 30, 31)
    COLOR_INK_BAR_BITS: tuple[int, ...] = (16, 18, 17)
    BLACK_INK_BAR_BITS: tuple[int, ...] = (27, 22, 26)
    WIFI_ICON_BIT = 12
    COLOR_INK_OUTLINE_BIT = 19
    BLACK_INK_OUTLINE_BIT = 23
    WIFI_ICON_ID = 0x03E6
    WIFI_BAR_IDS: tuple[int, ...] = (0x03E7, 0x03E8, 0x03E9, 0x03EA, 0x03EB)
    COLOR_INK_BAR_IDS: tuple[int, ...] = (0x03CE, 0x03CD, 0x03CC)
    BLACK_INK_BAR_IDS: tuple[int, ...] = (0x03D2, 0x03D1, 0x03D0)
    COLOR_INK_OUTLINE_ID = 0x03FF
    BLACK_INK_OUTLINE_ID = 0x03FE
    MAX_WIFI_BARS = 5
    INDICATOR_BY_NAME: dict[str, tuple[int, int]] = {
        name: (bit, fw_id) for bit, (name, fw_id) in INDICATORS.items()
    }
    INDICATOR_BY_ID: dict[int, tuple[int, str]] = {
        fw_id: (bit, name) for bit, (name, fw_id) in INDICATORS.items()
    }

    # Direct GPIO button LEDs from FP_HWDEFS (0x2083a480) and GPIO table (0x20ab6394):
    #   name -> (physical_gpio_pin, gpio_led_id, fp_led_id)
    # Both are active-low outputs (direction == 1 and output_latch == 0 -> LED ON).
    GPIO_LEDS: dict[str, tuple[int, int, int]] = {
        'power_led': (28, 0x07D1, 0x0372),
        'wireless_led': (0, 0x07F5, 0x03C2),
    }

    # Firmware UI_LITE LED mode constants consumed by 0xa044de30 (ui_lite.set_led <id>, <mode>):
    UI_LITE_MODES: dict[str, int] = {
        'off': 0x03D5,
        'solid': 0x03D6,
        'blink_slow': 0x03D7,
        'blink': 0x03D8,
        'blink_alt': 0x03D9,
        'blink_fast': 0x03DA,
        'wifi_cascade': 0x03DB,
    }
    UI_LITE_MODE_IDS: dict[int, str] = {mode_id: name for name, mode_id in UI_LITE_MODES.items()}

    def __init__(self, raw_inputs: int, *, input_width: int = 18,
                 signature: int = 0x35, frame_bits: int = 64,
                 reset_active_level: int = 0, tail_bit: int | None = None):
        if isinstance(input_width, bool) or not isinstance(input_width, int) or not 1 <= input_width <= 32:
            raise ValueError('panel input width must be in 1..32')
        if isinstance(frame_bits, bool) or not isinstance(frame_bits, int) or not input_width + 16 <= frame_bits <= 96:
            raise ValueError('panel frame must contain the input and verification sequence')
        if isinstance(signature, bool) or not isinstance(signature, int) or not 0 <= signature <= 0xff:
            raise ValueError('panel signature must fit in eight bits')
        if isinstance(reset_active_level, bool) or reset_active_level not in (0, 1):
            raise ValueError('panel reset active level must be zero or one')
        if isinstance(tail_bit, bool) or tail_bit not in (None, 0, 1):
            raise ValueError('panel tail bit must be zero, one, or None')
        self.input_width = input_width
        self.signature = signature
        self.frame_bits = frame_bits
        self.reset_active_level = reset_active_level
        self.tail_bit = tail_bit
        self.set_inputs(raw_inputs)
        self.gpio_leds: dict[str, bool] = {name: False for name in self.GPIO_LEDS}
        self._frame_gpio_leds: list[dict[str, bool]] = []
        self.gpio_led_transitions: list[dict[str, object]] = []
        self.output_word = 0
        self.strobe: int | None = None
        self.armed = False
        self.bit_count = 0
        self.shifted_output = 0
        self.latched_inputs = 0
        self.sampled_bit = None
        self.frames: list[dict] = []
        self.aborted_frames: list[dict] = []
        self.on_frame = None

    def set_gpio_led(self, name: str, active: bool) -> None:
        """Update the active state of a direct GPIO front-panel LED ('power_led' or 'wireless_led')."""
        if name not in self.GPIO_LEDS:
            raise ValueError(f'unknown panel GPIO LED: {name!r}')
        new_val = bool(active)
        if self.gpio_leds.get(name) != new_val:
            self.gpio_led_transitions.append({
                'name': name,
                'active': new_val,
                'frame_index': len(self.frames),
            })
        self.gpio_leds[name] = new_val

    def set_inputs(self, raw_inputs: int) -> None:
        if isinstance(raw_inputs, bool) or not isinstance(raw_inputs, int) or not 0 <= raw_inputs < (1 << self.input_width):
            raise ValueError(f'panel raw inputs must fit in {self.input_width} bits')
        self.raw_inputs = raw_inputs

    def _resolve_button_index(self, button: int | str) -> int:
        if isinstance(button, str):
            entry = self.BUTTONS.get(button)
            if entry is None or entry[0] >= self.input_width:
                raise ValueError(f'unknown panel button: {button!r}')
            return entry[0]
        if not isinstance(button, bool) and isinstance(button, int) and button in self.BUTTON_KEY_IDS:
            bit = self.BUTTON_KEY_IDS[button]
            if bit < self.input_width:
                return bit
        if isinstance(button, bool) or not isinstance(button, int) or not 0 <= button < self.input_width:
            raise ValueError(f'panel button index must be in 0..{self.input_width - 1}')
        return button

    def _resolve_chord_buttons(self, chord: str | int) -> tuple[str, str]:
        if isinstance(chord, str):
            entry = self.BUTTON_CHORDS.get(chord)
            if entry is None:
                raise ValueError(f'unknown panel button chord: {chord!r}')
            return entry[0]
        if not isinstance(chord, bool) and isinstance(chord, int):
            name = self.CHORD_KEY_IDS.get(chord) or self.CHORD_WORDS.get(chord)
            if name is not None:
                return self.BUTTON_CHORDS[name][0]
        raise ValueError(f'unknown panel button chord: {chord!r}')

    def press_button(self, index: int | str) -> None:
        """Assert an active-low front-panel input bit (0 = pressed) by bit index, button name, or key ID."""
        bit = self._resolve_button_index(index)
        self.raw_inputs &= ~(1 << bit)

    def release_button(self, index: int | str) -> None:
        """Deassert an active-low front-panel input bit (1 = released) by bit index, button name, or key ID."""
        bit = self._resolve_button_index(index)
        self.raw_inputs |= 1 << bit

    def release_all_buttons(self) -> None:
        """Release all front-panel buttons (set all input bits to active-low idle 1)."""
        self.raw_inputs = (1 << self.input_width) - 1

    def is_button_pressed(self, index: int | str) -> bool:
        """Return True when active-low front-panel input `index` (bit, name, or key ID) is pressed (0)."""
        bit = self._resolve_button_index(index)
        return not bool((self.raw_inputs >> bit) & 1)

    def press_chord(self, chord: str | int) -> None:
        """Press both buttons of a multi-key chord by chord name, key ID (0x20xx), or packed word."""
        btn_a, btn_b = self._resolve_chord_buttons(chord)
        self.press_button(btn_a)
        self.press_button(btn_b)

    def release_chord(self, chord: str | int) -> None:
        """Release both buttons of a multi-key chord by chord name, key ID (0x20xx), or packed word."""
        btn_a, btn_b = self._resolve_chord_buttons(chord)
        self.release_button(btn_a)
        self.release_button(btn_b)

    def is_chord_pressed(self, chord: str | int) -> bool:
        """Return True when both buttons of `chord` are simultaneously pressed."""
        btn_a, btn_b = self._resolve_chord_buttons(chord)
        return self.is_button_pressed(btn_a) and self.is_button_pressed(btn_b)

    @property
    def pressed_buttons(self) -> tuple[str, ...]:
        """Return the names of all currently pressed front-panel buttons."""
        pressed: list[str] = []
        for bit in range(self.input_width):
            if not ((self.raw_inputs >> bit) & 1):
                pressed.append(self.BUTTON_BITS.get(bit, f'bit_{bit}'))
        return tuple(pressed)

    @property
    def active_chords(self) -> tuple[str, ...]:
        """Return the names of any multi-button chords currently held down."""
        return tuple(
            name for name, ((btn_a, btn_b), _, _) in self.BUTTON_CHORDS.items()
            if self.is_button_pressed(btn_a) and self.is_button_pressed(btn_b)
        )

    def decode_buttons_from_inputs(self, raw_inputs: int | None = None) -> dict[str, tuple[str, ...]]:
        """Decode an 18-bit raw_inputs word into pressed button names and active chord names."""
        if raw_inputs is None:
            raw_inputs = self.frames[-1]['raw_inputs'] if self.frames else self.raw_inputs
        if isinstance(raw_inputs, bool) or not isinstance(raw_inputs, int) or not 0 <= raw_inputs < (1 << self.input_width):
            raise ValueError(f'panel raw inputs must fit in {self.input_width} bits')
        pressed: list[str] = []
        for bit in range(self.input_width):
            if not ((raw_inputs >> bit) & 1):
                pressed.append(self.BUTTON_BITS.get(bit, f'bit_{bit}'))
        pressed_set = set(pressed)
        chords = tuple(
            name for name, ((btn_a, btn_b), _, _) in self.BUTTON_CHORDS.items()
            if btn_a in pressed_set and btn_b in pressed_set
        )
        return {'pressed_buttons': tuple(pressed), 'active_chords': chords}

    @classmethod
    def decode_key_event(cls, value: int) -> str:
        """Decode a firmware key bit index, Key ID (0x20xx), or packed chord word (0xXXXXFFFF) into its name."""
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError('key event value must be an int')
        if value in cls.BUTTON_BITS:
            return cls.BUTTON_BITS[value]
        if value in cls.BUTTON_KEY_IDS:
            return cls.BUTTON_BITS[cls.BUTTON_KEY_IDS[value]]
        if value in cls.CHORD_KEY_IDS:
            return cls.CHORD_KEY_IDS[value]
        if value in cls.CHORD_WORDS:
            return cls.CHORD_WORDS[value]
        return f'unknown_{value:#x}'

    @classmethod
    def resolve_led_id(cls, led: int | str) -> int:
        """Resolve an indicator name, 7-segment name ('a'..'g' or 'seg_a'..'seg_g'), GPIO LED name, or firmware ID to its UI_LITE LED ID."""
        if isinstance(led, str):
            if led in cls.INDICATOR_BY_NAME:
                return cls.INDICATOR_BY_NAME[led][1]
            if led in cls.GPIO_LEDS:
                return cls.GPIO_LEDS[led][2]
            seg_key = led[4:] if led.startswith('seg_') else led
            if seg_key in cls.SEVEN_SEG_BITS:
                return cls.SEVEN_SEG_BITS[seg_key][1]
            raise ValueError(f'unknown panel indicator or LED name: {led!r}')
        if not isinstance(led, bool) and isinstance(led, int):
            if led in cls.INDICATOR_BY_ID:
                return led
            for _, fw_id in cls.SEVEN_SEG_BITS.values():
                if led == fw_id:
                    return fw_id
            for _, gpio_id, fp_id in cls.GPIO_LEDS.values():
                if led in (gpio_id, fp_id):
                    return fp_id
        raise ValueError(f'unknown panel indicator or LED ID: {led!r}')

    @classmethod
    def resolve_mode_id(cls, mode: int | str) -> int:
        """Resolve a UI_LITE LED mode name ('off', 'solid', 'blink', 'blink_fast', 'wifi_cascade') or integer (0x03D5..0x03DB)."""
        if isinstance(mode, str):
            if mode in cls.UI_LITE_MODES:
                return cls.UI_LITE_MODES[mode]
            raise ValueError(f'unknown UI_LITE LED mode: {mode!r}')
        if not isinstance(mode, bool) and isinstance(mode, int) and mode in cls.UI_LITE_MODE_IDS:
            return mode
        raise ValueError(f'unknown UI_LITE LED mode: {mode!r}')

    @classmethod
    def encode_frame(
        cls,
        *,
        seven_seg_char: str = ' ',
        wifi_bars: int = 0,
        color_ink_bars: int = 0,
        black_ink_bars: int = 0,
        indicators: tuple[str | int, ...] | list[str | int] | set[str | int] = (),
        apply_polarity: bool = True,
    ) -> int:
        """Encode high-level 7-segment character, bar graphs, and indicators into a 64-bit shift-register word."""
        if not isinstance(seven_seg_char, str) or seven_seg_char not in cls.SEVEN_SEG_BY_CHAR:
            raise ValueError(f'unsupported 7-segment character: {seven_seg_char!r}')
        if isinstance(wifi_bars, bool) or not isinstance(wifi_bars, int) or not 0 <= wifi_bars <= cls.MAX_WIFI_BARS:
            raise ValueError(f'wifi_bars must be in 0..{cls.MAX_WIFI_BARS}')
        if isinstance(color_ink_bars, bool) or not isinstance(color_ink_bars, int) or not 0 <= color_ink_bars <= 3:
            raise ValueError('color_ink_bars must be in 0..3')
        if isinstance(black_ink_bars, bool) or not isinstance(black_ink_bars, int) or not 0 <= black_ink_bars <= 3:
            raise ValueError('black_ink_bars must be in 0..3')
        logical = 0
        for seg in cls.SEVEN_SEG_BY_CHAR[seven_seg_char]:
            bit, _ = cls.SEVEN_SEG_BITS[seg]
            logical |= 1 << bit
        if wifi_bars > 0:
            logical |= 1 << cls.WIFI_ICON_BIT
            for b in cls.WIFI_BAR_BITS[:wifi_bars]:
                logical |= 1 << b
        if color_ink_bars > 0:
            logical |= 1 << cls.COLOR_INK_OUTLINE_BIT
            for b in cls.COLOR_INK_BAR_BITS[:color_ink_bars]:
                logical |= 1 << b
        if black_ink_bars > 0:
            logical |= 1 << cls.BLACK_INK_OUTLINE_BIT
            for b in cls.BLACK_INK_BAR_BITS[:black_ink_bars]:
                logical |= 1 << b
        for item in indicators:
            if isinstance(item, str):
                if item in cls.INDICATOR_BY_NAME:
                    logical |= 1 << cls.INDICATOR_BY_NAME[item][0]
                elif item in cls.SEVEN_SEG_BITS:
                    logical |= 1 << cls.SEVEN_SEG_BITS[item][0]
                else:
                    raise ValueError(f'unknown shift-register indicator: {item!r}')
            elif not isinstance(item, bool) and isinstance(item, int):
                if item in cls.INDICATORS:
                    logical |= 1 << item
                elif item in cls.INDICATOR_BY_ID:
                    logical |= 1 << cls.INDICATOR_BY_ID[item][0]
                else:
                    raise ValueError(f'unknown shift-register indicator bit or ID: {item!r}')
            else:
                raise ValueError(f'invalid indicator specifier: {item!r}')
        return (logical ^ cls.OUTPUT_POLARITY_MASK) if apply_polarity else logical

    @classmethod
    def build_ui_lite_commands(
        cls,
        *,
        seven_seg_char: str | None = None,
        seven_seg_mode: str | int = 'solid',
        wifi_bars: int | None = None,
        color_ink_bars: int | None = None,
        black_ink_bars: int | None = None,
        indicators: dict[str | int, str | int] | None = None,
        batch_size: int = 3,
    ) -> list[bytes]:
        """Build batched UDW commands (`udws "ui_lite.set_led <id>, <mode>; ..."`) for the live firmware."""
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise ValueError('batch_size must be a positive int')
        ops: list[tuple[int, int]] = []
        off_id = cls.UI_LITE_MODES['off']
        solid_id = cls.UI_LITE_MODES['solid']
        if seven_seg_char is not None:
            if not isinstance(seven_seg_char, str) or seven_seg_char not in cls.SEVEN_SEG_BY_CHAR:
                raise ValueError(f'unsupported 7-segment character: {seven_seg_char!r}')
            seg_mode = cls.resolve_mode_id(seven_seg_mode)
            active_segs = cls.SEVEN_SEG_BY_CHAR[seven_seg_char]
            for seg, (_, fw_id) in cls.SEVEN_SEG_BITS.items():
                ops.append((fw_id, seg_mode if seg in active_segs else off_id))
        if color_ink_bars is not None:
            if isinstance(color_ink_bars, bool) or not isinstance(color_ink_bars, int) or not 0 <= color_ink_bars <= 3:
                raise ValueError('color_ink_bars must be in 0..3')
            ops.append((cls.COLOR_INK_OUTLINE_ID, solid_id if color_ink_bars > 0 else off_id))
            for idx, fw_id in enumerate(cls.COLOR_INK_BAR_IDS):
                ops.append((fw_id, solid_id if idx < color_ink_bars else off_id))
        if black_ink_bars is not None:
            if isinstance(black_ink_bars, bool) or not isinstance(black_ink_bars, int) or not 0 <= black_ink_bars <= 3:
                raise ValueError('black_ink_bars must be in 0..3')
            ops.append((cls.BLACK_INK_OUTLINE_ID, solid_id if black_ink_bars > 0 else off_id))
            for idx, fw_id in enumerate(cls.BLACK_INK_BAR_IDS):
                ops.append((fw_id, solid_id if idx < black_ink_bars else off_id))
        if wifi_bars is not None:
            if isinstance(wifi_bars, bool) or not isinstance(wifi_bars, int) or not 0 <= wifi_bars <= cls.MAX_WIFI_BARS:
                raise ValueError(f'wifi_bars must be in 0..{cls.MAX_WIFI_BARS}')
            ops.append((cls.WIFI_ICON_ID, solid_id if wifi_bars > 0 else off_id))
            for idx, fw_id in enumerate(cls.WIFI_BAR_IDS):
                ops.append((fw_id, solid_id if idx < wifi_bars else off_id))
        if indicators:
            for led_spec, mode_spec in indicators.items():
                ops.append((cls.resolve_led_id(led_spec), cls.resolve_mode_id(mode_spec)))
        batches: list[bytes] = []
        for i in range(0, len(ops), batch_size):
            chunk = ops[i:i + batch_size]
            body = '; '.join(f'ui_lite.set_led {lid}, {mid}' for lid, mid in chunk)
            batches.append(f'udws "{body};"'.encode('ascii'))
        return batches

    @property
    def last_output(self) -> int | None:
        """Return the most recently latched 64-bit LED/display shift-register word, if any."""
        return self.frames[-1]['output'] if self.frames else None

    @property
    def logical_output(self) -> int | None:
        """Return the most recently latched 64-bit word after undoing firmware's 0x0F00 polarity mask."""
        out = self.last_output
        return (out ^ self.OUTPUT_POLARITY_MASK) if out is not None else None

    def decode_frame(self, output: int | None = None, *, gpio_leds: dict[str, bool] | None = None) -> dict:
        """Decode a 64-bit shift-register frame into 7-segment display state and active icons/LEDs."""
        if output is None:
            output = self.last_output
        if output is None:
            raise PanelError('no latched serial-panel output frame available to decode')
        leds = self.gpio_leds if gpio_leds is None else gpio_leds
        logical = output ^ self.OUTPUT_POLARITY_MASK
        segments = tuple(
            seg for seg, (bit, _) in self.SEVEN_SEG_BITS.items()
            if (logical >> bit) & 1
        )
        char = self.SEVEN_SEG_CHARS.get(frozenset(segments), '?')
        active_indicators: list[str] = []
        active_ids: list[int] = []
        for bit, (name, fw_id) in sorted(self.INDICATORS.items()):
            if (logical >> bit) & 1:
                active_indicators.append(name)
                active_ids.append(fw_id)
        for seg, (bit, fw_id) in self.SEVEN_SEG_BITS.items():
            if (logical >> bit) & 1:
                active_ids.append(fw_id)
        for name, (_, gpio_id, fp_id) in self.GPIO_LEDS.items():
            if leds.get(name, False):
                active_indicators.append(name)
                active_ids.extend((fp_id, gpio_id))
        wifi_bars = sum(1 for b in self.WIFI_BAR_BITS if (logical >> b) & 1)
        color_ink_bars = sum(1 for b in self.COLOR_INK_BAR_BITS if (logical >> b) & 1)
        black_ink_bars = sum(1 for b in self.BLACK_INK_BAR_BITS if (logical >> b) & 1)
        return {
            'raw_output': output,
            'logical_output': logical,
            'seven_seg_segments': segments,
            'seven_seg_char': char,
            'wifi_bars': wifi_bars,
            'color_ink_bars': color_ink_bars,
            'black_ink_bars': black_ink_bars,
            'gpio_leds': dict(leds),
            'active_indicators': tuple(active_indicators),
            'active_ids': tuple(sorted(active_ids)),
        }

    def analyze_frames(self, window: int | None = None) -> dict[str, object]:
        """Analyze latched frames to detect steady vs. blinking indicators, 7-seg sequences, and bar animations."""
        if not self.frames:
            raise PanelError('no latched serial-panel frames available to analyze')
        if window is not None and (isinstance(window, bool) or not isinstance(window, int) or window < 1):
            raise ValueError('window must be a positive int or None')
        total = len(self.frames)
        start = max(0, total - window) if window is not None else 0
        selected_frames = self.frames[start:]
        selected_gpio = (
            self._frame_gpio_leds[start:]
            if len(self._frame_gpio_leds) == total
            else [dict(self.gpio_leds)] * len(selected_frames)
        )
        decoded_list = [
            self.decode_frame(frame['output'], gpio_leds=gpio_state)
            for frame, gpio_state in zip(selected_frames, selected_gpio)
        ]
        all_indicator_names = [name for _, (name, _) in sorted(self.INDICATORS.items())] + list(self.GPIO_LEDS.keys())
        toggled_gpio = {
            rec['name']
            for rec in self.gpio_led_transitions
            if isinstance(rec.get('frame_index'), int) and rec['frame_index'] >= start
        }
        steady: list[str] = []
        blinking: list[str] = []
        inactive: list[str] = []
        count = len(decoded_list)
        for name in all_indicator_names:
            hits = sum(1 for d in decoded_list if name in d['active_indicators'])
            if name in toggled_gpio and 0 < hits < count:
                blinking.append(name)
            elif name in toggled_gpio and hits in (0, count) and len(
                {rec['active'] for rec in self.gpio_led_transitions if rec.get('name') == name and rec.get('frame_index', 0) >= start}
            ) > 1:
                blinking.append(name)
            elif hits == count:
                steady.append(name)
            elif hits == 0:
                inactive.append(name)
            else:
                blinking.append(name)
        seq: list[str] = []
        for d in decoded_list:
            ch = d['seven_seg_char']
            if not seq or seq[-1] != ch:
                seq.append(ch)
        wifi_vals = [d['wifi_bars'] for d in decoded_list]
        c_vals = [d['color_ink_bars'] for d in decoded_list]
        k_vals = [d['black_ink_bars'] for d in decoded_list]
        btn_events: list[str] = []
        for frame in selected_frames:
            dec_in = self.decode_buttons_from_inputs(frame['raw_inputs'])
            for ch_name in dec_in['active_chords']:
                if ch_name not in btn_events:
                    btn_events.append(ch_name)
            if not dec_in['active_chords']:
                for b_name in dec_in['pressed_buttons']:
                    if b_name not in btn_events:
                        btn_events.append(b_name)
        return {
            'frame_count': count,
            'steady_indicators': tuple(steady),
            'blinking_indicators': tuple(blinking),
            'inactive_indicators': tuple(inactive),
            'seven_seg_sequence': tuple(seq),
            'seven_seg_blinking': len(seq) > 1,
            'wifi_bars_range': (min(wifi_vals), max(wifi_vals)),
            'wifi_animating': min(wifi_vals) != max(wifi_vals),
            'color_ink_bars_range': (min(c_vals), max(c_vals)),
            'black_ink_bars_range': (min(k_vals), max(k_vals)),
            'button_events': tuple(btn_events),
        }

    def render_display(
        self,
        output: int | None = None,
        *,
        show_temporal: bool = False,
        window: int | None = None,
    ) -> str:
        """Render the HP DeskJet 2620 front-panel LCD and LED state as ASCII art."""
        info = self.decode_frame(output)
        segs = set(info['seven_seg_segments'])
        top = ' _ ' if 'a' in segs else '   '
        mid = (
            ('|' if 'f' in segs else ' ')
            + ('_' if 'g' in segs else ' ')
            + ('|' if 'b' in segs else ' ')
        )
        bot = (
            ('|' if 'e' in segs else ' ')
            + ('_' if 'd' in segs else ' ')
            + ('|' if 'c' in segs else ' ')
        )
        ind = set(info['active_indicators'])
        wifi_str = ('((o))' if 'wifi_icon' in ind else '  .  ') + f" [{info['wifi_bars']}/{self.MAX_WIFI_BARS}]"
        c_ink = ('[C:' + '#' * info['color_ink_bars'] + '.' * (3 - info['color_ink_bars']) + ']') if 'color_ink_outline' in ind or info['color_ink_bars'] else '[C:---]'
        k_ink = ('[K:' + '#' * info['black_ink_bars'] + '.' * (3 - info['black_ink_bars']) + ']') if 'black_ink_outline' in ind or info['black_ink_bars'] else '[K:---]'
        gpio_str = ', '.join(
            f"{name}={'ON' if active else 'OFF'}"
            for name, active in info['gpio_leds'].items()
        )
        lines = [
            '+---------------------------+',
            f'|  {top}   Wi-Fi: {wifi_str:<11s} |',
            f"|  {mid}   Ink:   {c_ink} {k_ink} |",
            f"|  {bot}   Char:  '{info['seven_seg_char']}'         |",
            '+---------------------------+',
            f"Indicators: {', '.join(info['active_indicators']) if info['active_indicators'] else 'none'}",
            f"GPIO LEDs:  {gpio_str}",
            f"Buttons:    {', '.join(self.pressed_buttons) if self.pressed_buttons else 'none'}",
        ]
        if show_temporal and self.frames:
            summary = self.analyze_frames(window=window)
            lines.append(
                f"Steady:     {', '.join(summary['steady_indicators']) if summary['steady_indicators'] else 'none'}"
            )
            lines.append(
                f"Blinking:   {', '.join(summary['blinking_indicators']) if summary['blinking_indicators'] else 'none'}"
            )
            seq_str = ' -> '.join(repr(ch) for ch in summary['seven_seg_sequence'])
            w_min, w_max = summary['wifi_bars_range']
            anim_str = (f'{w_min}..{w_max}/{self.MAX_WIFI_BARS}' if summary['wifi_animating']
                        else f'{w_max}/{self.MAX_WIFI_BARS} (static)')
            lines.append(f'Temporal:   frames={summary["frame_count"]}, 7seg=[{seq_str}], wifi={anim_str}')
        return '\n'.join(lines)

    @property
    def reset_asserted(self) -> bool:
        return ((self.output_word >> 3) & 1) == self.reset_active_level

    def _clear_frame(self, reason: str) -> None:
        if self.bit_count:
            self.aborted_frames.append({'bit_count': self.bit_count,
                                        'output': self.shifted_output, 'reason': reason})
        self.armed = False
        self.bit_count = self.shifted_output = 0
        self.sampled_bit = None

    def set_strobe(self, level: int | None) -> None:
        if level not in (None, 0, 1):
            raise ValueError('panel strobe must be zero, one, or None')
        previous = self.strobe
        self.strobe = level
        if level is None:
            self._clear_frame('strobe disconnected')
            return
        if previous != 0 or level != 1 or self.reset_asserted:
            return
        latched = None
        if self.bit_count == self.frame_bits:
            latched = {'bit_count': self.bit_count, 'output': self.shifted_output,
                       'raw_inputs': self.latched_inputs}
            self.frames.append(latched)
            self._frame_gpio_leds.append(dict(self.gpio_leds))
        elif self.bit_count:
            self._clear_frame('incomplete frame at strobe')
            raise PanelError('serial-panel strobe before a complete output frame')
        self.bit_count = self.shifted_output = 0
        self.latched_inputs = self.raw_inputs
        self.sampled_bit = None
        self.armed = True
        if latched is not None and self.on_frame is not None:
            self.on_frame(latched)

    def write_output(self, word: int) -> None:
        previous = self.output_word
        reset_asserted = ((word >> 3) & 1) == self.reset_active_level
        rising_clock = not previous & 2 and bool(word & 2)
        if (not reset_asserted and rising_clock and self.armed and self.strobe == 1
                and self.bit_count >= self.frame_bits):
            raise PanelError('serial-panel clock exceeds configured frame length')
        self.output_word = word
        if reset_asserted:
            self._clear_frame('reset')
            return
        if not rising_clock or not self.armed or self.strobe != 1:
            return
        index = self.bit_count
        self.shifted_output |= ((word >> 2) & 1) << index
        if index < self.input_width:
            self.sampled_bit = (self.latched_inputs >> index) & 1
        elif index < self.input_width + 8:
            self.sampled_bit = (self.signature >> (index - self.input_width)) & 1
        elif index < self.input_width + 16:
            self.sampled_bit = (self.shifted_output >> (index - self.input_width - 8)) & 1
        else:
            self.sampled_bit = self.tail_bit
        self.bit_count += 1

    def read_input(self) -> int:
        if self.reset_asserted or not self.armed or not self.bit_count:
            raise PanelError('serial-panel input has no active clocked frame')
        if self.sampled_bit is None:
            raise PanelError('serial-panel input tail is unknown; configure an explicit tail bit')
        return self.sampled_bit << 8


class PanelPort:
    INPUT = 0xBD0B1410
    OUTPUT = 0xBD0B1418
    CONFIG = 0xBD0B140E
    CONTROL = 0xBD0B140A
    TX = 0xBD0B1408
    STATUS = 0xBD0B1414
    RX = 0xBD0B1416

    def __init__(self, input_word: int | None = None, *,
                 endpoint: SerialShiftPanel | None = None,
                 byte_engine_rx: list[int] | tuple[int, ...] | None = None):
        self.widths = {address: 2 for address in (self.INPUT, self.OUTPUT, self.CONFIG,
                                                self.CONTROL, self.TX, self.STATUS, self.RX)}
        # The fallback driver initializes these before its command handshake.
        # Bit meanings beyond the observed configuration are still unknown.
        self.configuration = {self.CONFIG: 0, self.CONTROL: 0}
        self.output_word = 0
        self.input_word: int | None = None
        self.output_writes: list[int] = []
        self.endpoint = endpoint
        self._observer = None
        self.byte_engine_enabled = False
        self.byte_engine_rx_queue: list[int] = []
        self.tx_bytes: list[int] = []
        self.byte_status = 0
        self.byte_rx = 0
        if byte_engine_rx is not None:
            self.configure_byte_engine(byte_engine_rx)
        self.drive_input(input_word)

    def configure_byte_engine(self, rx_bytes: list[int] | tuple[int, ...] | None) -> None:
        obs = self._observer
        if rx_bytes is None:
            self.byte_engine_enabled = False
            self.byte_engine_rx_queue = []
            self.byte_status = 0
            self.byte_rx = 0
            if obs is not None:
                obs.invalidate_mmio_slot(self.STATUS, 2)
                obs.invalidate_mmio_slot(self.RX, 2)
            return
        validated: list[int] = []
        for b in rx_bytes:
            if isinstance(b, bool) or not isinstance(b, int) or not 0 <= b <= 0xFF:
                raise ValueError('byte_engine_rx elements must be ints in 0..0xff')
            validated.append(b & 0x7F)
        self.byte_engine_enabled = True
        self.byte_engine_rx_queue = validated
        self.byte_status = 0
        self.byte_rx = 0
        if obs is not None:
            obs.invalidate_mmio_slot(self.STATUS, 2)
            obs.invalidate_mmio_slot(self.RX, 2)

    def drive_input(self, word: int | None) -> None:
        if word is not None and self.endpoint is not None:
            raise ValueError('static panel input and serial endpoint are mutually exclusive')
        if word is not None and (not isinstance(word, int) or not 0 <= word <= 0xFFFF):
            raise ValueError('front-panel input word must be in 0..0xffff')
        self.input_word = word
        obs = self._observer
        if obs is not None and not obs._synchronizing:
            obs.sync_panel()

    def set_strobe(self, level: int | None) -> None:
        if self.endpoint is not None:
            obs = self._observer
            if obs is not None and obs.state.panel_write_count:
                obs.flush_panel()
            self.endpoint.set_strobe(level)
            if obs is not None and not obs._synchronizing:
                obs.sync_panel()

    def set_gpio_led(self, name: str, active: bool) -> None:
        if self.endpoint is not None:
            self.endpoint.set_gpio_led(name, active)

    def read(self, address: int, size: int) -> int:
        obs = self._observer
        if obs is not None and obs.state.panel_write_count:
            obs.flush_panel()
        if address == self.OUTPUT and size == 2:
            return self.output_word
        if address in (self.STATUS, self.RX) and size == 2:
            if not self.byte_engine_enabled:
                raise PanelError('front-panel byte-engine completion/receive behavior is not established')
            if address == self.STATUS:
                return self.byte_status
            rx_val = self.byte_rx
            self.byte_status = 0
            if obs is not None:
                obs.invalidate_mmio_slot(self.STATUS, 2)
                obs.invalidate_mmio_slot(self.RX, 2)
            return rx_val
        if address != self.INPUT or size != 2:
            raise PanelError(f'unmodeled front-panel port read: {address:#010x}, size {size}')
        if self.endpoint is not None:
            return self.endpoint.read_input()
        if self.input_word is None:
            raise PanelError('front-panel input at 0xbd0b1410 is unknown; provide an explicit input word')
        return self.input_word

    def write(self, address: int, size: int, value: int) -> None:
        obs = self._observer
        if obs is not None and obs.state.panel_write_count:
            obs.flush_panel()
        if address == self.TX and size == 2:
            if not self.byte_engine_enabled:
                # D900 polls STATUS bit 0 and consumes RX & 0x7f. It is unclear
                # whether bit 0 requires external reception or just local clocks.
                # Neither a successful byte nor a timeout follows from absence.
                raise PanelError('front-panel byte engine at 0xbd0b1408 needs unresolved '
                                 'completion semantics; no response or timeout is synthesized')
            self.tx_bytes.append(value & 0xFF)
            if self.byte_engine_rx_queue:
                self.byte_rx = self.byte_engine_rx_queue.pop(0) & 0x7F
            else:
                self.byte_rx = value & 0x7F
            self.byte_status = 1
            if obs is not None:
                obs.invalidate_mmio_slot(self.STATUS, 2)
                obs.invalidate_mmio_slot(self.RX, 2)
            return
        if address in self.configuration and size == 2:
            supported = (0, 0x140) if address == self.CONFIG else (0,)
            if value not in supported:
                raise PanelError(f'unmodeled front-panel configuration {address:#010x}={value:#06x}')
            self.configuration[address] = value
            return
        if address != self.OUTPUT or size != 2:
            raise PanelError(f'unmodeled front-panel port write: {address:#010x}, size {size}')
        if value & ~0xF:
            raise PanelError(f'unmodeled front-panel output bits: {value:#06x}')
        if self.endpoint is not None:
            self.endpoint.write_output(value)
        self.output_word = value
        self.output_writes.append(value)
        if obs is not None and not obs._synchronizing:
            obs.sync_panel()
