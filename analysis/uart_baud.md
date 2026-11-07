# UART baud identified from the firmware

The boot console is configured for **115200 baud**. This follows from matching
the boot register values with the main application's explicit numeric baud
selection, rather than assuming a clock-divider formula.

## Boot configuration

The original boot initializer starts at `A000FFD8`. The reset trace in
`artifacts/serial-spi.jsonl` confirms these halfword writes:

| Instruction address | Register | Value |
| --- | --- | --- |
| `A0010026` | `BD0B4C00` | `2111` |
| `A0010028` | `BD0B4C02` | `2111` |
| `A001002E` | `BD0B3C04` | `03FF` |
| `A0010036` | `BD0B3C00` | `1000` |
| `A001003C` | `BD0B3C08` | `0000` |

TX characters are written to `BD0B3C06`. The initializer then reads the RX
register `BD0B3C0C` six times. These operations occur before the first banner
character is transmitted.

`03FF` is **not established as a baud divider**. The main driver modifies bits
0 and 6 of register `+4` to control RX and TX servicing. Its behavior supports
an interrupt-mask interpretation for those bits. The boot value masks these
sources while the bootloader uses polled output.

## Independent baud mapping in the main application

The application stores two UART MMIO bases at `2083D13C`: port 0 is
`BD0B3C00`, and port 1 is `BD0B4000`. Initialization at `A0177C38` selects the
corresponding base and passes configuration to `A0177CEC`.

`A0177CEC` first writes `1000` to UART register `+0`, `2111` to both
`BD0B4C00` and `BD0B4C02`, and zero to UART `+8`. It then compares the actual
numeric baud argument at `A0177D84..A0177DA2`:

| Numeric baud argument | Constant construction | UART `+0` after selection |
| ---: | --- | --- |
| 19200 | `4B << 8` | `1030` |
| 38400 | `4B << 9` | `1020` |
| 57600 | `E1 << 8` | `1010` |
| **115200** | **`E1 << 9`** | **`1000`, preserved by the branch to `A0177DB8`** |
| Any other value | Default branch | `1040` |

The other-value branch does not identify its actual supported rate, so the
last row should not be labeled 9600 without further evidence. These values
are baud-selection codes, not a proven linear divider.

The default configuration at `A053D8CC` begins with `0001C200`, which is
115200. Routine `A0177A8A` copies that configuration into both port records,
then changes port 1's baud to 19200 at `A0177AAA`. Port 0, whose MMIO address
matches the boot UART, retains 115200. Thus both the explicit selection code
and the default console configuration corroborate the boot setting.

These addresses can be inspected without executing or changing firmware:

```powershell
python -m analysis.next_disasm 0xa000ffd8 0xa001004c
python -m analysis.panel_deep_disasm 0xa0177a8a 0xa0177ab6
python -m analysis.panel_deep_disasm 0xa0177cec 0xa0177db8
```

The second helper has a historical panel-oriented name but disassembles any
requested range in the extracted application's `.text` section.

## Practical capture settings and limits

Start a logic-analyzer UART decoder at **115200, 8N1**, with hardware flow
control disabled. The baud is directly supported by the firmware above.
**8N1 remains an initial framing assumption**: the inspected driver does not
decode or document the parity, data-length, and stop-bit fields independently.
No matching AXON UART register manual was found during the bounded check.

At the intended baud, one bit lasts `1 / 115200 = 8.68056 microseconds`.
An 8N1 frame would therefore take about `86.806 microseconds`. Physical timing
and voltage have not been measured; the oscillator tolerance and full PLL to
UART-clock path are not yet decoded. The emulator's 160 virtual ticks per
frame are an independent scheduling parameter, not a measured baud rate.

The expected first bytes from this dump are `0A 0D 5B 42 6F 6F 74 5D`, or
newline, carriage return, and `[Boot]`. Recognizing that prefix after reset is
a stronger TX-pad check than finding activity at approximately the right rate.

Early initialization also clears pin-multiplexer bits 5 through 9 at
`BD061C08`, using logical pin identifiers `35`, `36`, `37`, `38`, and `39`
(hexadecimal) stored at `A00100D0..A00100E0`. This identifies the group touched
by the UART initializer. It does **not** identify which logical pin is TX or
map any of them to a package ball, connector position, or board test pad.
