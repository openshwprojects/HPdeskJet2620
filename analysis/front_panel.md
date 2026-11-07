# Front-panel initialization and the next emulation boundary

The GPIO stop at `0xbd0b203a` is resolved. The firmware continues after the
missing EEPROM board-ID warning and enters front-panel initialization. Neither
that warning nor board compatibility checking causes the new stop.

## Signal-level interface

`0xa000e3dc` probes a serial front panel through halfword registers:

| Address | Observed role |
| --- | --- |
| `0xbd0b1418` | Output word: bit 1 clock, bit 2 serial output; bit 3 polarity is tried both ways |
| `0xbd0b1410` | Sampled input word; bit 8 is serial input |
| GPIO 26 | Logical GPIO 12; pulses around the 64-bit frame, consistent with a strobe |
| GPIO 28 | Logical GPIO 13; active-low dedicated indicator, independently driven by UI commands |

The driver at `0xa000e0f6` shifts 64 bits, samples input, and checks a portion of
the received stream against its transmitted pattern. It retries with the other
polarity. A constant input is not a valid device identity or acknowledgement.

`panel.py` retains the established output and configuration registers. By
default it stops when the input is sampled: there is no captured electrical
level or attached panel model. `--panel-input-word 0` or `0x100` explicitly
provides a static test input. These are controlled experiments, not measured
states of the printer. Both make the original firmware reject the serial
handshake and try its alternate panel driver. No firmware instruction, PC,
return value, or NVM record is changed.

## Alternate byte interface

The nearby source marker is `nos_ui_fp_mcu_simp.c` at `0xa000de8c`.
Initialization at `0xa000d89e` writes output zero, configuration `0x140` at
`0xbd0b140e`, then output `0xb`, `3`, and `8` separated by delays, and zero at
`0xbd0b140a`. These configurations are retained; their complete bit meanings
are not established.

The exchange routine at `0xa000d900`:

1. Writes a halfword command to `0xbd0b1408`.
2. Waits up to 100,000 timer ticks for bit 0 of `0xbd0b1414`.
3. Reads `0xbd0b1416` and returns its low seven bits.

The reader consumes that receive register even after timeout and reset. Its
empty/stale/reset value is not established. Frame sequences include
`[opcode, payload_length, XOR checksum, 1]`, followed by `0x7f` exchanges to
retrieve response data. Observed opcodes are 1 (version), `0x30` (startup keys),
and `0x31` (events). The returned opcode is checked after the fourth exchange,
so status bit 0 does not mean a valid protocol-level acknowledgement.

Two hardware interpretations remain possible: a locally clocked serializer
finishes even without a panel, or completion depends on receiving an external
frame. The code alone does not establish which interpretation is correct.
An absent panel therefore does not justify forcing either completion or a
timeout. `panel.py` rejects a byte-engine transmit with this diagnostic instead
of generating an invented response. Its status and receive reads are also
explicitly unsupported.

## Evidence needed to continue

A register description or a captured front-panel clock/data exchange would
resolve the byte engine and panel identity/response format. The next model can
then drive real pin transitions and received bytes, retaining the firmware's
own detection and validation. A separate 64 KiB EEPROM dump would recover
per-unit settings; it would not by itself resolve the front-panel controller.

## Physical confirmation (2026-11-04)

On the owner's printer HP stage-1's panel driver state, read back from DRAM
after boot (`md 0x60001040 10` in the custom application), is identical to
the emulator's:

| Address | Value | Meaning (from `0xa000e3dc` / `0xa000e0f6`) |
| --- | --- | --- |
| `0x60001040` | `3` | delay unit; the delay routine `0xa0009310` busy-waits on the 1 MHz counter, so one unit is 3 us |
| `0x60001052` | `0x0009` | clock-low output template (bit 0 set, bit 3 high) |
| `0x60001054` | `0x000b` | clock-high output template |
| `0x60001058` | `1` | detection result: serial shift panel found (2 would select the MCU byte engine) |
| `0x6000105c`/`5d` | `01`/`00` | found, polarity 0 |

So the physical panel is the serial shift-register panel, and the `0x35`
signature protocol is real. The custom firmware's first driver failed on the
printer (signature read `ff`) because it cleared output bit 0 and toggled the
clock without delays. Reproducing stage-1's waveform exactly fixed it: hold
bit 0, put data on bit 2, wait 75 units (225 us) after every edge, sample the
input (bit 8) while the clock is high, read the input only for the 34 defined
positions, pulse the GPIO 26 strobe low before and after the 64 bits, and
start with a bit-3 reset pulse followed by a 66 ms wait. Every frame is then
acknowledged with the `0x35` signature and the echoed output byte
(`fw/common/periph.c`, `panel_shift`), the display shows the encoded frame,
and button presses decode on the printer (`info`, `wireless` and others were
pressed and reported live with `buttons watch`). The `No CGD, skipping
backlight steps` line printed on the printer comes from a stub at
`0xa000e4de` (no backlit graphic display on this board), not from a failed
panel detection.

## Display map seen on the printer (2026-11-04)

The bit numbers in `panel.py` come from HP's `FP_HWDEFS` table, so they are
the ones HP's firmware drives. The element names attached to them were
guesses, and several were wrong. The custom application's `lcd walk`
command lit one logical bit at a time (the digit blinked the bit number) while
the owner watched the panel and named what lit. "Logical" means after the
`0x0F00` polarity mask, as everywhere in `panel.py` and `fw/common/periph.c`.

| Bit | HP ID | What lights on the printer | Old guessed name |
| --- | --- | --- | --- |
| 8 | `0x03FD` | orange LED DS1 at the Cancel button, marked "INK" | `attention_icon` |
| 9 | `0x03BD` | orange LED at the Resume button | `wireless_status_icon` |
| 10 | none | nothing visible | none |
| 11 | `0x03F9` | orange LED at the Info button | `wifi_direct_icon` |
| 12 | `0x03E6` | Wi-Fi antenna icon, left of the five bars | `web_services_icon` |
| 13 | `0x03AD` | sheet of paper icon | `auto_wireless_connect_icon` |
| 14 | `0x0374` | exclamation mark | `paper_error_icon` |
| 15 | `0x0373` | small letter E | `resume_led` |
| 16 | `0x03CE` | first ink gauge, bottom bar | `color_ink_bar_3` |
| 17 | `0x03CC` | first ink gauge, top bar | `color_ink_bar_1` |
| 18 | `0x03CD` | first ink gauge, middle bar | `color_ink_bar_2` |
| 19 | `0x03FF` | first ink gauge, U-shaped outline | `color_ink_icon` |
| 20 | `0x040C` | Wi-Fi Direct icon: printer, phone and radio waves | `paper_alert_led` |
| 21 | `0x040B` | ink droplet between the two gauges | `ink_alert_led` |
| 22 | `0x03D1` | second ink gauge, middle bar | `black_ink_bar_2` |
| 23 | `0x03FE` | second ink gauge, U-shaped outline | `black_ink_icon` |
| 24 | `0x03E7` | Wi-Fi signal bar 1 (smallest) | `wifi_icon` |
| 25 | `0x040A` | droplet on a sheet of paper | `counter_icon` |
| 26 | `0x03D0` | second ink gauge, top bar | `black_ink_bar_1` |
| 27 | `0x03D2` | second ink gauge, bottom bar | `black_ink_bar_3` |
| 28 | `0x03E8` | Wi-Fi signal bar 2 | `wifi_bar_1` |
| 29 | `0x03E9` | Wi-Fi signal bar 3 | `wifi_bar_2` |
| 30 | `0x03EA` | Wi-Fi signal bar 4 | `wifi_bar_3` |
| 31 | `0x03EB` | Wi-Fi signal bar 5 (biggest) | `wifi_bar_4` |
| 32 | `0x03BE` | small exclamation mark over the Wi-Fi bars | `wireless_attention_icon` (correct) |
| 33..39 | `0x0402`..`0x0408` | the digit; `SEVEN_SEG_BITS` is right (bit 39 lit the top segment alone, and 1, 2, 3 and every number during the walk read correctly) | `a`..`g` (correct) |
| 40..44, 46, 47 | none | nothing visible | none |
| 45 | `0x03C3` | nothing visible; the display has no backlight | `panel_backlight` |

Consequences:

- Every element has a bit. With all 64 logical bits set, every display
  element and the three orange LEDs light, which the owner's photos confirm.
  Bits 0..7, 10, 40..47 and 48..63 light nothing; this was checked with a
  frame that set all of them together.
- Bits 8, 9 and 11 are discrete LEDs, not display segments. LEDs are
  active-low, which is why HP's `FP_HWDEFS` property `0x0e` XORs `0x0F00`
  into every frame. Bit 10 would be a fourth LED position; nothing is
  fitted for it.
- The Wi-Fi gauge has five bars, smallest to biggest on bits 24, 28, 29, 30,
  31. Bit 24 is a bar. The antenna icon is bit 12, and HP's ID for it
  (`0x03E6`) directly precedes the bar IDs.
- Within each ink gauge HP's IDs run top to bottom. A level is drawn from the
  bottom: bits 16, 18, 17 for the first gauge and 27, 22, 26 for the second.
  The outlines are bits 19 and 23. The old encoder lit the top bar for
  level 1.
- Which gauge holds colour and which black is not yet confirmed on the
  printer. The names follow HP's ID grouping: `0x03CC`..`0x03CE` with `0x03FF`
  as colour, `0x03D0`..`0x03D2` with `0x03FE` as black.
- Two LEDs sit outside the shift register. GPIO 0 (`wireless_led`, HP ID
  `0x03C2`, `led wifi on` in the custom application) lights the blue LED at
  the Wireless button. GPIO 28 (`power_led`) lights the power button LED.
- The panel board is HP part `V1N01-60002` (silkscreen `2016-HPDC`), with
  buttons S1 Cancel, S2 Info, S3 Wireless, S5 Resume, S6 Black Copy, Colour
  Copy and Power, and the display as U21.
- Names for the icons whose meaning is uncertain describe their look
  (`paper_icon`, `exclamation_icon`, `letter_e_icon`, `ink_drop_icon`,
  `drop_on_paper_icon`). Bit 45 is named `unused_bit45`.
