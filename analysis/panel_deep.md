# Panel evidence from both firmware stages

The main firmware independently confirms the signal-level serial panel
protocol. It does **not** resolve the fallback byte engine's completion flag
or configuration encoding. The resulting opt-in `SerialShiftPanel` endpoint
implements the confirmed framing without changing firmware bytes.

## Image coverage and reproducibility

The original early code is LZSS-packed flash `0x39ec..0xc9db`, expanded at
`0xa0005720`. Its constants and initialized data also occupy independently
packed ranges `0xc9dc..0xdfed` (destination `0xa001082c`) and
`0xdff0..0xe5d7` (destination `0x200014c0`).

The later container extraction provides the following sections. The canonical
filenames below are generated with:

```text
python -m analysis.firmware_image --extract-dir artifacts/main
```

| Artifact | Runtime base | Expanded length |
| --- | --- | --- |
| `artifacts/main/text-a00150d0.bin` | `0xa00150d0` | `0x4e2e5c` |
| `artifacts/main/rodata-a04f9568.bin` | `0xa04f9568` | `0x1999cc` |
| `artifacts/main/fixcon-a0692f60.bin` | `0xa0692f60` | `0x142d0` |
| `artifacts/main/data-2081e0a0.bin` | `0x2081e0a0` | `0x52750` |
| `artifacts/main/ncdata-606b15a0.bin` | `0x606b15a0` | `0x125c` |
| `artifacts/main/srdata-60000ae8.bin` | `0x60000ae8` | `0x188` |

The main code's packed source is flash `0x791160`, length `0x39faf7`.
Expanded code and data extents match their container destination boundaries.
`python -m analysis.panel_deep_disasm START END` obtains `.text` directly from
`FirmwareImage`, displays main Thumb code, and annotates PC-relative literal
loads. It never executes or modifies firmware and requires no extracted files.

A search of all these expanded artifacts found five `bd 0b 14` literal
prefixes, all in main code:

| Literal location | Value |
| --- | --- |
| `0xa00e86d4` | `0xbd0b1418` |
| `0xa00e8a00` | `0xbd0b1418` |
| `0xa00e8a04` | `0xbd0b1410` |
| `0xa00e9598` | `0xbd0b1418` |
| `0xa00e95a4` | `0xbd0b1410` |

No direct literal for `0xbd0b1400`, `1408`, `140a`, `140e`, `1414`, or `1416`
occurs in these main artifacts. This is a bounded literal-search result, not
proof that an address can never be calculated at runtime. The sole aligned
`0xbd0b0000` main constant is associated with the separate SDIO area at
`0xa0603c3c`, not the decoded panel code.

Names include `fp_hwdefs` at `0xa0531e08`, `fp_mcu_simp` at `0xa0532008`, and
`Front-Panel UI` at `0xa05cfb80`. The `fp_mcu_simp` module name is insufficient
to identify a physical MCU: this build's relevant implementation drives the
clock and data bits directly. The `FP_MCU_I2C` string at `0xa05e4c40` is in a
generic power-client name list, not a register description.

## Independently corroborated framing

Early routine `0xa000e0f6` and main routine `0xa00e8576` both implement:

- Halfword output `0xbd0b1418`: bit 1 is clock and bit 2 is outgoing serial data.
- Halfword input `0xbd0b1410`: bit 8 is the incoming serial level.
- Logical GPIO 12, physical pin 26 in this board table, strobes low then high
  around each serial frame.
- Outgoing data is least-significant-bit first. The selected profile clocks
  64 bits, even though the input verification consumes only 34 bits.
- The first 18 incoming bits are active-low input state. They are followed by
  16 verification bits: byte `0x35`, then the first eight transmitted data bits,
  both least-significant-bit first.

The early code forms the verification value as
`(physical_output_low << 8) | 0x35` at `0xa000e218..0xa000e220`.
It samples input state for bit positions 0..17 at `0xa000e2d6..0xa000e2ea`,
compares positions 18..33 at `0xa000e2ee..0xa000e316`, and clocks through
position 63 at `0xa000e32c..0xa000e330`. A mismatch invalidates the frame.
Successful input conversion is `(raw_inputs ^ 0x3ffff)`; the returned function
value carries a separate success bit in bit zero.

The main routine independently constructs `((physical_output & 0xff) << 8)
| 0x35` at `0xa00e8644..0xa00e8660`. It records low input levels as asserted
input bits at `0xa00e86f8..0xa00e8710`, then verifies the same 16-bit sequence
at `0xa00e8714..0xa00e873e`. Main loop length is taken from the panel table,
not hardcoded to make detection pass. Main code also performs an additional
input read per clock at `0xa00e8740..0xa00e8742`, discarding its value. A device
must advance on clock edges, never on input-register reads.

Main code reads `0xbd0b1418` before changing data or clock, for example
`0xa00e8678..0xa00e8686` and `0xa00e94d8..0xa00e950c`. This establishes that
the output latch must be readable. The initial strict `PanelPort` interface
only supported output writes; latch readback is now implemented with this evidence.

Clock rises before sampling and falls after sampling. Presenting the selected
input bit on the rising edge is a suitable discrete-event approximation;
the code does not establish the external chip's internal launch edge or exact
setup/hold requirements. Repeated reads while clock is unchanged must return
the same sampled bit.

## Matching board tables

Early `FP_HWDEFS` starts at `0x200019ec`, with property pairs at `0x20001c88`.
Main `FP_HWDEFS` starts at `0x2083a164`, with property pairs at `0x2083a400`.
Their 16 property pairs are byte-for-byte identical. The selected project is
`0xfff7`, with revision range 0..`0xffffffff`.

| Property ID | Value | Established use |
| --- | --- | --- |
| `0x0d` | `0x40` | 64 output clocks per main frame |
| `0x0e` | `0x0f00` | XOR applied to outgoing logical output state |
| `0x0f` | `0x12` | 18 incoming input bits |
| `0x0b` | `0x294` | Main slow blink timing parameter |
| `0x0c` | `0x14a` | Main fast blink timing parameter |

Main initialization at `0xa00e895a..0xa00e89aa` queries these properties and
stores them in the driver state. The early detection routine has a fallback
26-input-bit format when its provisional mode byte is clear; normal detection
sets that byte and uses the 18-input-bit form. The table agrees with the latter.

Input mapping is also identical in both stages. The eight populated entries
map physical input indices `0xff, 2, 3, 7, 6, 8, 9, 1` to firmware key IDs
`0x2000, 0x2028, 0x209b, 0x20ab, 0x2004, 0x202d, 0x202c, 0x2001` respectively.
Index `0xff` is a sentinel. The remaining raw input bits must not be assigned
button names or electrical levels without an explicit board profile.

## Bounded device-model contract

The evidence supports an explicit signal-level serial-panel profile with:

1. A configured raw input word and input width, latched on the selected frame
   strobe. No automatic guess about pressed buttons or disconnected line levels.
2. Output bit 2 captured on each rising clock edge; data retained independently
   from whether the guest reads the input register.
3. First 18 incoming bits from that latched input, then eight signature bits
   from `0x35`, then eight feedback bits from the first outgoing byte.
4. All 64 outgoing bits retained as a frame. Feedback is derived from actual
   MOSI history, not from the expected guest state or a program counter.
5. Explicit state reset for the observed bit-3 synchronization pulse and GPIO
   strobe; reject or diagnose unsupported framing rather than silently repair it.

This models a firmware-established device protocol. The fixed `0x35` is a
wire signature independently required by two drivers, not an invented MCU
identity/version value. A configurable wrong signature, wrong frame length,
wrong polarity, or broken feedback must fail the firmware's original check.
The physical device type (discrete shift registers, custom logic, or MCU),
precise bit-3 reset polarity, and strobe electrical behavior remain inferences
until a schematic or logic-analyzer capture is available. Such a model should
be labeled as an explicit inferred panel profile, not a captured physical unit.

`panel.py` now implements this endpoint through
`PanelPort(endpoint=SerialShiftPanel(raw_inputs=...))` and `set_strobe(level)`.
The raw input argument is mandatory. Defaults are 18 input bits, signature
`0x35`, 64 frame clocks and active-low bit-3 reset. `set_inputs()` changes the
external inputs for the next strobe; it does not modify a frame in progress.
Completed frames are recorded in `frames` at the next low-to-high strobe.
Reset, strobe disconnection and incomplete frames preserve diagnostic records
in `aborted_frames`. Input reads beyond the confirmed 34-bit sequence remain
errors unless an explicit `tail_bit` is supplied; early boot does not read
those trailing positions. The fourteen panel tests cover framing, causal
feedback, input latching, repeated reads, output readback, broken signatures,
reset/disconnection, frame lengths, unknown tails, GPIO26 routing through
`BootHardware`, and the strict byte engine.

## Byte-engine boundary remains unresolved

`0xbd0b1400` is the **base** used by early routine `0xa000d900`. Its status
poll is at `base+0x14 = 0xbd0b1414`, bit 0, not at base itself. The transmit
store is `base+8`; receive read is `base+0x16`, masked to seven bits.

Only early initialization writes configuration `0xbd0b140e=0x0140` and
control `0xbd0b140a=0`. Main code provides no second implementation of those
accesses to compare. Searching primary HP/Marvell documentation and exact
register/source strings did not yield a register description for this ASIC.
The public HP DeskJet 2600 guide describes physical user controls, not these
controller registers; generic Marvell printer-SoC or older HP instrument
manuals cannot establish this ASIC's bit meanings.

There is still no evidence to choose between local serializer completion and
completion gated by external reception. Nor is there evidence for the empty
receive value after a failed transaction/reset. A fabricated completion bit,
fabricated timeout, or selected MCU identity would therefore remain unjustified.
The serial-panel protocol above offers a separately evidenced implementation
path while keeping this alternate controller boundary strict.
