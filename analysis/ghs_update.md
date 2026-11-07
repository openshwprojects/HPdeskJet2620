# GHS firmware update inspector

`python -m analysis.ghs_update --verify` reconstructs the supplied dump's
base scatter image in private analysis memory, applies its GHS update stream,
and independently checks both runtime checksums stored in that stream. It
does not alter the dump or connect to the emulator. Production execution must
run only the path selected by the original loader and current device state.
The inspector deliberately applies the optional update offline; it does not
prove the current boot selects it.

The actual reset run in `artifacts/deep-native.json` selects base entry
`A04E2E05`, emits 933 UART bytes, and stops at timer MMIO PC `A0177664` after
94,177,440 instructions. NVM status `AA` causes the per-device footer gate
at `A000AC68..AC7A` to skip the valid footer. RFIX/GHS are not executed.
See `analysis/storage_path.md` for exact conditions; mode four alone does
not prohibit the update.

The analysis banks follow the emulator's current 64 MiB DDR alias map, 8 KiB
instruction TCM and 4 KiB data TCM, initially zeroed. Matching the embedded
checksums provides a much stronger check of the decoded update operations
than comparing the entry instruction alone. This is not a claim that all
physical RAM powers on as zero.

Commands:

```
python -m analysis.ghs_update
python -m analysis.ghs_update --verify
python -m analysis.ghs_update --verify --json
python -m analysis.ghs_selftest
```

The first command reports stream structure only. `--verify --json` reports
runtime checksums, the updated entry bytes, relocation statistics and SHA-256
digests for all 21 final checksum ranges. Without `--verify`, `--json` instead
prints the full command/record inventory. The inspector writes no files.

## Verified results for this dump

| Check | Actual and stored value | Bytes checked |
| --- | --- | --- |
| Encoded update stream | `8F9F727E` | `803087` |
| Base runtime image | `9820510E` | `15636128` |
| Updated runtime image | `06EDF545` | `15619562` |

The encoded stream begins at flash `00C93E98` and ends at `00D57FC7`.
Its base entry is `A04E2E05`; the updated entry is `A04E3DD9`. The first
32 bytes at the latter entry, excluding the Thumb flag, are:

```
b570b0851c041c0e1c15f002e810f000f8cf4b112b00d001f002e838480f2800
```

If an external footer is selected and the loader's verification flag is zero,
the call at `A000ADD2` passes skip counter one to `A000B42A`. On that path,
the first runtime checksum is skipped and the last runs. The inspector checks
both independently. A nonzero flag selects zero skips; the separate embedded
update call at `A000AD80` selects two or zero. These are conditional paths,
not observations from the current base boot. Skips apply only to runtime
checksums, not the encoded stream checksum.

## Command decoding

All byte-aligned integers are big-endian. The header magic is `7F474853`.
The parser reads the extent at header `+18`, sets its end to
`header + 14 + extent`, and compares the checksum at `+1C` against the bytes
from `+20` through that end. The two body entry words occupy `+20` and `+24`;
commands begin at `+28`. Offsets in this paragraph are hexadecimal.

The rolling checksum at `A000AE88` rotates the accumulator left seven bits
and adds each input byte, modulo 2^32. Checksum commands preserve the
accumulator between their listed ranges.

| Opcode | Decoder | Meaning | Records in supplied stream |
| --- | --- | --- | --- |
| 4 | `A000B0CC` | Checksum over address ranges, then compare stored word | 21 ranges before and after updates |
| 3 | `A000AF0A` / `A000AEDE` | Memory move | 53 |
| 1 | `A000AFD2` | Raw bytes | 80708 |
| 5 | `A000B00A` | LZSS bytes | 470 |
| 2 | `A000B042` | Individual byte replacements | 20435 |
| 6 | `A000B07A` | Fill a range with a repeated byte | 254 |
| 7 | `A000B3F6` | Packed word relocations | 6032 word writes from 5254 encoded bytes |

Opcodes 1, 2 and 5 contain a 16-bit group count. Each group begins with a
16-bit record count. Group zero records use full 32-bit destination addresses
and, for opcodes 1/5, 32-bit stored lengths. Later groups begin with a 32-bit
address accumulator and use one-byte unsigned address deltas and lengths.
Opcode 2's length is always one byte. Each record is immediately followed by
its stored bytes. Opcode 5 uses the same 4 KiB-window LZSS decoder as the
base image; its expanded length is independently derived from the tokens
and bounded before allocation.

Opcode 3 contains a 16-bit count and triples `{source, length, delta}`.
Its destination is `source + delta` modulo 2^32. The original helper calls
the overlap-aware `memmove` at `000006BC`, so the inspector copies source
bytes before writing each destination. The order of records matters.

Opcode 4 contains a 32-bit count, pairs `{start, exclusive_end}`, and the
expected checksum. Opcode 6 contains a 16-bit count followed by records
`{32-bit destination, 16-bit length, byte value}`.

Opcode 7 prefixes its packed payload with a 32-bit byte length. The decoder
at `A000B27C` is an LSB-first bit reader with a 32-bit lookahead word. Prefixes
1 through 7 select one of seven configurable delta encodings; prefixes 8 and
9 update the relocation addend and reset the running address; prefix 10
terminates this stream. Each ordinary relocation advances the address by a
positive word count and adds the current addend to the addressed big-endian
word, modulo 2^32. The inspector follows the observed default/custom table
selection in `A000B2BE` and rejects unsupported code 7.

The lookahead is significant: the firmware reads 27 bits beyond this
payload's declared end into the following checksum command. The inspector
uses those actual following source bytes rather than inventing padding.
There are 42027 logical consumed bits in the 42032-bit payload, plus the
32-bit lookahead. The terminator and bounds are checked separately.

Five focused checks cover incremental checksumming, grouped delta records,
corruption/truncation/unknown commands, LZSS token sizes, and relocation
prefixes, overflow and lookahead. The full `--verify` run additionally
reproduces both stored runtime checksums exactly.

## Observed base work and optional-update estimate

The main non-loader LZSS streams contain 2682870 literals and 796603
back-references generating 4549746 bytes. Counting instructions through the
actual `A000A748` loop gives approximately 89279679 instructions, excluding
progress callbacks and the extra initial-zero back-reference path. The main
`.text` stream alone accounts for approximately 67443770 instructions.

The recorded base boot reaches its first new timer stop in 94,177,440
instructions, consistent with the decompression estimate plus setup and
zero/copy operations. The earlier roughly 220-million estimate incorrectly
treated the optional update as selected in that run.

For an update-selected boot, the encoded checksum would add about 5.6 million
instructions. The final
runtime checksum processes 15619562 bytes at seven loop instructions per
byte: 109336934 instructions, before diagnostic output and function overhead.
Moving 15.6 MiB, applying roughly 100000 patches, relocation decoding and
zero/raw scatter operations add further work. Around 220 million or more
instructions remains an estimate for the **update-selected alternative**
before later application initialization, not the observed base boot.

Base milestones are `A000A868` (scatter loader), `A000A748` (LZSS), and
`A0007910` (branch to selected entry). Only update-selected paths additionally
reach `A000B42A` (GHS), `A000AF0A` (memory moves), `A000B2E8` (relocations),
and `A000B0CC` / `A000AE88` (runtime checksum). Repeated execution inside those
loops with advancing pointers is progress rather than evidence of a hung
hardware poll.
