# GPIO register and board-descriptor evidence

This analysis applies to the supplied dump after its original scatter loader
and decompressor execute. The production GPIO model is `gpio.py`. Addresses
below are firmware runtime addresses, except where a flash offset is named.

## Pin register bank

The literal at `0xa0005b2c` is `0xbd0b2000`. Three small Thumb accessors establish
the stride-eight layout:

| Offset within pin | Width | Firmware evidence | Model |
| --- | --- | --- | --- |
| `+0` | 16 bits | `0xa0005b08` reads `base + pin*8` and returns its low byte | Sample the driven level; input-only pins require an external input |
| `+2` | 16 bits | `0xa0005b14` stores a halfword at `base + pin*8 + 2` | Output latch, retained independently of direction |
| `+4` | 16 bits | `0xa0005a90` stores a halfword at `base + pin*8 + 4` | Direction 0=input, 1=output |
| `+6` | Unknown | No decoded accessor | Unsupported |

The direction interpretation is supported by `0xa0010348`: during initialization
phase one it preloads the output only when descriptor direction equals one
(`0xa00103c4..0xa00103d0`), then passes that direction to `0xa00103e6`. The latter
calls the direct AXON direction accessor for bus type zero. Other direction
values remain unsupported rather than being silently reduced to one bit.

The hardware model implements 64 pins. This count is inferred from the IRQ
accessor's four groups of 16 pins, using halfword index `3 - (pin >> 4)`. It is
not a pin-count ID read from the chip. Input/output halfwords only accept the
decoded logical values zero and one. Unknown external inputs are errors; no
pull-up, pull-down, button state, or board strap is supplied automatically.

An output-mode input read returns the output latch unless an explicit external
drive conflicts, in which case the model reports contention. Changing a pin to
input mode does not erase its output latch. Static external levels do not
generate guessed interrupt edges.

## Firmware board descriptors

`0xa0010348` selects four 24-byte descriptors starting at `0xa00129bc`. A reset
run with the original dump was stopped after GPIO initialization, and the
following values were read directly from the firmware-initialized RAM table:

| Descriptor | Logical ID | Physical AXON pin | Direction | Polarity byte | Initial logical output | Initial physical output |
| --- | --- | --- | --- | --- | --- | --- |
| `0xa00129bc` | 0 | 7 | Output | 0 | 0 | 1 |
| `0xa00129d4` | 12 (`0x0c`) | 26 | Output | 1 | 1 | 1 |
| `0xa00129ec` | 9 | 11 | Input | 1 | Ignored | Externally supplied |
| `0xa0012a04` | 13 (`0x0d`) | 28 | Output | 0 | 0 | 1 |

All four entries apply to board revisions 0..31 and have AXON bus type zero.
The descriptor fields used here are: revision limits at bytes 0 and 1, logical
ID word at +4, bus-type word at +8, physical pin byte at +12, direction word at
+16, polarity byte at +20, and initial logical output byte at +21. The wrapper
copies twenty bytes beginning at descriptor +4 into its logical-pin table.

`0xa0010496` converts a logical output to its physical value: polarity zero
inverts it, while nonzero polarity passes it through. `0xa0010430` applies the
same polarity conversion to reads. Consequently the first observed MMIO store,
`0xbd0b203a = 1` from `0xa0005b1a`, is physical pin 7's output preload, followed
by its output direction. Logical IDs are not physical pin numbers.

The NVM byte-write helper `0xa000c804` drives logical ID zero to one before an
EEPROM write (`0xa000c816..0xa000c81a`), and back to zero afterward
(`0xa000c834..0xa000c838`). This is consistent with pin 7 controlling the separate
EEPROM's active-high write-protect line: the helper drives physical low while
writing, then high. The emulated board now connects this sampled GPIO level to
the EEPROM write-protection input as an explicit wiring inference. A protected
data byte is NACKed and cannot alter the image; reads remain available.

## Event and routing banks

The literals at `0xa0005b38` and `0xa0005b3c` identify the related banks:

| Bank | Evidence | Implemented behavior |
| --- | --- | --- |
| `0xbd0b2400 + 2*pin` | `0xa0005b1e` reads its low byte; `0xa0005ab2` writes zero | Explicit pending status, zero clears; other writes unsupported |
| `0xbd0b2500 + 16*channel + 2*(3-(pin>>4))` | `0xa0005abe` writes `1 << (pin&15)`; `0xa0005ae6` writes zero | Retained 16-bit routing/mask words |

`0xa0005a9c` records a channel in the per-pin table at `0x20001f1c` and marks a
byte in the enable array at `0x20001f14`. The model provides eight channels,
inferred from the eight-byte gap before the pin table; a silicon datasheet
would be needed to establish the exact implemented channel count. The four
mask words per channel are ordered from pins 48..63 at +0 down to pins 0..15
at +6. Unknown gaps and alternate access widths remain errors.

The firmware seen so far does not establish rising/falling/level detection
encoding or interrupt delivery. Therefore `set_event(pin, raw_status)` can
explicitly supply a pending event for tests, but no edge type or CPU interrupt
is generated by assumption.

## Relation to the front-panel serial port

The next boot boundary after GPIO setup is the separate halfword port
`0xbd0b1418`, first written with 9 at `0xa000e412`. Its paired input port is
`0xbd0b1410`. The serial routine `0xa000e0f6` supplies stronger bit evidence:

- Output bit 1 toggles once per serial bit: idle pattern 9 (`1001`) to active
  pattern 11 (`1011`), then back. This is consistent with the serial clock.
- Output bit 2 carries the outgoing bit. At `0xa000e292..0xa000e29e`, the low bit
  of the outgoing word selects zero or four before being combined with the
  port's idle/active pattern.
- Input bit 8 is sampled at `0xa000e2de..0xa000e2ea` into successive bits of the
  received word. Later samples are compared against an expected shifted
  sequence at `0xa000e2f4..0xa000e316`.
- Output bit 3 is toggled for an initial synchronization pulse
  (`0xa000e22c..0xa000e244`) and inverted for the second detection attempt
  (`0xa000e3fa..0xa000e40c`). Its exact electrical role is unresolved.
- Output bit 0 stays set in the observed clock patterns; its independent
  meaning is not established by these routines.

Logical GPIO 12, physical pin 26, is configured as an output at
`0xa000e414..0xa000e420` and pulsed low/high around each 64-bit transfer
(`0xa000e26a..0xa000e280` and `0xa000e332..0xa000e348`). This suggests a frame
strobe/latch signal; naming it chip select would require additional evidence.
It is not physical GPIO pin 12.

Logical GPIO 13 is physical pin 28, active low. `0xa000e5ee` treats command ID
`0x372` specially: mode zero writes logical zero, mode one writes logical one,
and modes two/three alternate the logical output using timer thresholds
660000 and 330000 (`0xa000e624..0xa000e674`). This is consistent with a dedicated
LED control. Physical GPIO pin 11 is logical ID 9 and remains a separately
configured input; the decoded serial-input samples come from `0xbd0b1410`
bit 8, not from its GPIO input register.

## Verification

`tests/test_gpio.py` checks preload-before-enable, direction changes, explicit
input profiles, conflicting drives, event clearing, independent route words,
and strict rejection of unknown addresses, widths and encodings. All eight
tests pass. An original reset run advances through these GPIO writes and
reaches the next front-panel write at `0xbd0b1418` without input substitutions.
