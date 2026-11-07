# Storage discovery after the front panel

This note follows the unchanged boot code in `.load_text` and its profile table
in `.load_rodata`. Addresses below are runtime addresses unless identified as
flash offsets. It separates firmware behavior from selectable board-model
assumptions; it does not establish unknown AXON serializer timing or reset
register values.

## Device identification and memory windows

`A0005970` calls SPI discovery at `A000D07A`, then builds four 16-byte storage
interface records at `20001ED4`. The first three describe SPI slots 0, 1 and 2;
the fourth is an enabled zero-length terminator at base `03000000`. Discovery
stores each SPI slot's `{size, protocol, profile_pointer}` in a 12-byte record
at `20004C44 + slot * 12`.

The table entry at `A0011F60` recognizes JEDEC ID `00EF4018` and names it
`W25Q128BV (16 Megabytes)`. Its geometry is 256 blocks of 65,536 bytes. The
photographed newer JV-family chip uses this ID too, so no table alteration is
needed. The table's older product name does not mean identification failed.

`A000D21E..D244` writes the controller's window register at
`BD063818 + slot * 2`. With nonzero geometry it allocates windows consecutively,
encoding the start MiB in bits 5:0 and inclusive last MiB in bits 11:6. For a
single recognized 16 MiB slot-zero chip, firmware writes `03C0`: start 0,
last 15. It has not read slot zero's original window in this path.

An unknown ID is treated differently. The scan at `A000D174` does not special
case `FFFFFF` as absent. If the profile table has no match, the code calls
`A000CE98`, which reads the slot's window and returns
`(window_register & 0x3f) << 20`. It then uses CPU memory reads to distinguish
an empty aperture from an unknown ROM:

1. `A000D18E` calls `A000D01E(base + 2000, E000)`. This reads every byte from
   `base + 2000` through `base + FFFF`, inclusive. Any mixture of `00` and `FF`
   counts as empty. The first other value makes the routine return nonzero.
2. An empty aperture selects the `No ROM found` profile at `A0011A74`, whose
   geometry is zero.
3. Otherwise, `A000D04A` compares the same `E000` bytes against addresses
   offset by 1, 2, 4 and 8 MiB. The first matching alias selects an unknown-ROM
   profile with that capacity; no match selects the unknown 16 MiB profile.
4. For zero geometry, `A000D242..D244` writes `0FFF` to the window register.

Thus `0FFF` is demonstrably the firmware's zero-size/no-ROM encoding. It is
not independently proven to be a hardware disable flag: ordinary field
decoding gives start and last MiB both equal to 63. Setting it as the emulator's
reset value remains an explicit profile assumption rather than a measured
strap or captured reset register.

An optional absent-device profile can coherently choose reset windows
`(03C0, 0FFF, 0FFF)` and an undriven serial input pulled high. The narrow CPU
aperture implied by the latter two values is `03F00000..03FFFFFF`. The exact
probe reads are then `03F02000..03F0FFFF`. Returning `FF` there represents that
declared open-bus/pull-up profile, not another ROM or RAM allocation. Mapping
all addresses between 16 and 64 MiB as readable `FF` is unnecessary for this
evidence and would hide unrelated unimplemented mappings. Reads outside the
selected windows should remain strict.

## Main image and the RFIX footer

The actual reset run in `artifacts/deep-native.json` reaches the **base**
entry `A04E2E05`, then stops at `A0177664` on a halfword store to the unmodeled
timer mask `BD040804`. It executes 94,177,440 instructions and emits 933 UART
bytes, ending with `[Boot] calling (*0xa04e2e05)()`. RFIX/GHS are not selected.
The run uses explicit SPI profile `byte-final`, MISO pull one, and window
reset values `(03C0, 0FFF, 0FFF)`; the flash remains unchanged.

After storage discovery, the image loader `A000A9D0` has already validated the
main image magic at flash `00020034` and its board-support complement pair.
It conditionally searches for update headers using `A000A60E`, including the last `6C` bytes
of each eligible storage device. On the 16 MiB slot-zero chip that is flash
`00FFFF94`. A valid footer is insufficient: NVM status gates the search.

### Why this run selects the base image

The main header's `+68` field is `00B40000`. At `A000ABDC..AC06`, the loader
tries that aligned nonzero candidate and fallback `00C40000`; both locations
begin with `FFFFFFFF` in this dump, so neither selects an embedded update.

For external footers, `A000AC68..AC7A` checks NVM item 3 per device `n`:

- `A000A5CE(n)` tests bit `2*n`.
- If that bit is zero, `A000A58C(n)` tests bit `2*n + 1`.
- Even bit zero **and** odd bit one skips the slot before footer validation.

UART reports item 3 as `AA`. Its per-slot bit pairs all have even zero and
odd one, including slot zero, so the valid `00FFFF94` footer is skipped.
Both selected-header locals remain zero; `A000AD40` and `A000AD9C` skip
their GHS calls, preserving the base scatter loader's entry.

Load mode four itself does **not** prohibit updates. Mode three separately
skips the entire external-footer scan at `A000AC3E/AC40`. Mode four preserves
the fallback `AA`, which triggers the per-slot gate. The odd bit is set before
applying an external update (`A000ADAA/ADAC`) and cleared after success
(`A000ADDE/ADE0`); the even bit may then be set. These transitions suggest
update state, but do not establish official symbolic names. Changing those
bits just to force more UART would change the boot scenario.

### Conditional footer path

The supplied dump's footer has magic `3CA55A7D`, length `6C`, support
`064501FF/F9BAFE00`, and a 16-byte base-image signature matching the main
image. `A000A60E` checks those fields and the current board-number range;
the successful diagnostic is `[Boot] Got a valid header`. No checksum is
computed in this particular validator.

The footer's field `+58` contains `00C93BD1`, an actual Thumb callback. If
selected, loader instructions `A000AC9E..ACB6` call it. Its code at `00C93BD0`
uses the existing UART at `BD0B3C00` and prints:

- the footer address;
- `taiji_pp_usr_hf`, `narel`, and build `TJP1FN1809AR`;
- timestamp `Thu Mar 01, 2018 11:28:03PM`;
- two 16-byte signatures;
- `[RFIX] No Boot changes - Returning`.

This optional callback needs no additional peripheral beyond UART. Its first
string is `[RFIX] Called entry from header @ `. It can add output during an
update-selected boot, but was not called in the recorded reset run.

The next footer field, `+5C = 00C93E98`, is a descriptor consumed by
`A000B42A`, not another executable entry. Treating it as Thumb code produces
data disassembly.

`A000A85C` obtains the application's entry pointer from the header, then calls
`A000A868` to process zero-fill, raw-copy and compressed scatter records. This
path uses the already-modeled timer `BD040814`, RAM stores and the ARM
decompressor at `A000A748`; it must run these original instructions rather
than loading the offline extracted artifacts into memory. The recorded reset
run executes this base loading path and enters `A04E2E05` without processing
the optional update descriptor.

## The optional 2018 update stream

The descriptor at `00C93E98` begins with bytes `7F 47 48 53` (`\x7fGHS`),
which `A000B42A` checks byte by byte. Its size word at `+18` is `000C411B`;
the parser computes the stream end as `descriptor + 14 + size = 00D57FC7`.
The expected checksum at `+1C` is `8F9F727E`. The software checksum routine
at `A000AE88` rotates a 32-bit accumulator left seven bits, adds the next
byte, and repeats. An independent calculation over
`00C93EB8..00D57FC6` produces the exact expected checksum `8F9F727E`.

After checking the stream, the parser skips its first body word
(`A04E2E05` at `+20`) and retains the second (`A04E3DD9` at `+24`) as its
return value. It then dispatches numbered load commands through routines
`A000AFD2`, `A000B00A`, `A000B042`, `A000B07A`, `A000AF0A`, `A000B0CC`,
and `A000B3F6`. The caller can replace the base application's entry pointer
with this returned value. Consequently, seeing a jump to `A04E3DD9` after
successful update processing would be consistent with the supplied dump,
rather than evidence of an erroneous branch to the base image's entry.

This checksum alone would process roughly 803 KiB if the update were selected.
It is absent from the observed 94.18-million-instruction base boot, whose
scatter loader already expands several MiB. Offline verification establishes
the update's format and integrity, not that the current NVM state selects it.
