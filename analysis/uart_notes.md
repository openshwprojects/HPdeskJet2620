# Earliest UART path

This analysis applies to the supplied `dump.bin`. Addresses are firmware runtime
addresses unless identified as flash offsets.

## Loading the code

- Reset vectors load ARM entry `0x2070`.
- ARM scatter-loader `0x2238` calls the Thumb LZSS decoder at `0x3100`.
- At call site `0x2434`, the arguments are destination `0xa0005720`, source flash
  offset `0x39ec`, and packed length `0x8fef` (end offset `0xc9db`).
- That block expands to `0xb10c` bytes, ending at `0xa001082c`.
- `uart_decode.py` independently translates the firmware decoder for inspection.
  It does not modify the dump or serve as an emulator boot shortcut.

## Call chain

After relocation, ARM `0x244c` calls Thumb `0x2180`:

1. `0x2182` calls an empty platform hook at `0x2020`.
2. `0x2186` calls veneer `0x31ac`, which loads `0xa000ffd9` (Thumb UART init).
3. `0x218c` calls veneer `0x31b4`, which loads `0xa000fef5` (Thumb put string),
   with the banner string at flash offset `0x21c8`.
4. The same wrapper prints the build-time label at `0x220c`, timestamp at
   `0x2050`, and newline/carriage return at `0x2234`.
5. `0x21a2` calls veneer `0x31bc`, which loads ARM `0xa0005720`.

`0xa000fef4` checks the console-enable byte at `0x20005524`, then calls the
character loop at `0xa000fed2`. The actual transmit store is at `0xa000fee0`.

## UART MMIO

| Address | Access | Observed behavior |
| --- | --- | --- |
| `0xbd0b3c00` | 16-bit | Init writes `0x1000`. |
| `0xbd0b3c04` | 16-bit | Init writes `0x03ff`. |
| `0xbd0b3c06` | 16-bit | Transmit data; low eight bits contain one character. |
| `0xbd0b3c08` | 16-bit | Init writes zero. |
| `0xbd0b3c0c` | 16-bit | Init reads six times. |
| `0xbd0b3c0e` | 16-bit | Status. Bit 8 (`0x0100`) must be set for transmit. |

The status evidence is the sequence `ldrh r2, [r4]`, `lsls r3, r2, #23`,
`bpl` at `0xa000fed8..0xa000fedc`. The shift puts original bit 8 into the
sign flag. The following `strh r1, [r2]` at `0xa000fee0` writes the character.

## Initialization dependencies

UART init at `0xa000ffd8`:

- Sets `0x20005524` to one.
- Sets bit 0 in 16-bit registers `0xbd06080e` and `0xbd060810`.
- Calls the pin-configuration function `0xa000ce42` for pins `0x35..0x39`.
  With zero-default MMIO, these touch `0xbd061c08` and return normally.
- Waits for the 32-bit counter at `0xbd040814` to advance at least `0xc9`.
- Writes `0x2111` to both `0xbd0b4c00` and `0xbd0b4c02`.
- Initializes the UART registers in the table above.

## Execution checks

The actual decoded Thumb functions were executed with Unicorn's ARM926 model,
big-endian mode, and CPSR `0x213`:

- Init returned normally with zero-default MMIO and an advancing counter.
- The original put-string function, with enabled byte one and status `0x0100`,
  emitted through real UART stores:

```text
\n\r[Boot] ******************************************************\n\r
```

The targeted checks load the decoded function directly for independent analysis;
they are not evidence of a complete reset-to-banner emulator run.
