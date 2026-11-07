# UART after serial-panel discovery

## Current result: main application reached

The optional `--serial-panel-inputs 0x3ffff --spi-profile byte-final` board
profile now reaches **933 completed UART bytes** and executes the main
application. The original dump is unchanged. `artifacts/deep-native.json`
records **94,177,440 instructions**, with a strict stop at PC `A0177664` on a
halfword write to programmable-timer group mask `BD040804`. Its final lines are:

```text
[Boot] SPI Device 0 : W25Q128BV (16 Megabytes) (ID:0x00ef4018)
[Boot] SPI Device 1 : No ROM found (ID:0x00ffffff)
[Boot] SPI Device 2 : No ROM found (ID:0x00ffffff)
[Boot] calling (*0xa04e2e05)()
```

The SPI profiles remain explicit hardware hypotheses, not a recovered complete
AXON serializer specification. Both candidates produce identical flash wire
transactions and UART through the application handoff. `artifacts/deep-wide.json`
records the same timer boundary at **94,177,443 instructions**, three more than
`byte-final`, and exact byte matches against independent extraction for loaded
`.text` (5,123,676 bytes) and `.rodata` (1,677,772 bytes).
See [spi_review.md](spi_review.md) for the remaining uncertainty.
The optional native instruction observer matches the reference Python backend's
entire report at the ten-million-instruction budget and has separate full-state differential tests.
It executes the original firmware loader; it does not substitute decompression,
patch memory from the offline extractor, or jump over checks.

This boot selects the **base** application. The separate RFIX/GHS update in the
dump is not applied: the observed NVM update-status fallback `AA` causes the
external-footer scan to skip the flash slot. Embedded-update locations also
contain erased headers. See [storage_path.md](storage_path.md) for the branches.
Thus missing EEPROM settings affect which installed image is selected even
though their warnings do not prevent booting the base application.

The new boundary is a missing peripheral model, not a firmware-reported timer
failure. The application initializes five timer group masks, allocates a
periodic kernel tick, and configures its interrupt. Actual timer expiration,
pending bits, interrupt arbitration, and ARM exception entry still require
implementation. Driver evidence is in [main_entry.md](main_entry.md).

## Earlier strict-profile boundary

The explicit `--serial-panel-inputs 0x3ffff` profile reaches 754 UART bytes in `artifacts/serial-spi.json`: the last line is `[Boot] SPI Device 0 :`, followed by an explicit unsupported SPI transaction at `A000CEE4`. The four preceding values are firmware defaults and selection results. They are not four additional error reports. Their original symbolic enum names have not been recovered; the descriptions below follow data and branches rather than assigning names to numeric states.

## NVM defaults

The NVM item descriptors begin at `A0011924`. Each eight-byte descriptor contains `{item_id, region_id, byte_length, validation_mode, default_pointer}`. The selected region layout is at `20001804`; `A000CB8C` resolves a descriptor and calls the validating reader `A000C9F6`.

| Printed value | Descriptor | Default byte | EEPROM record in this layout | Interpretation established by code |
| --- | --- | --- | --- | --- |
| `NVM powerdown state = 00000003` | `A001192C`: item 1, region `0A`, length 1, mode 2 | `03` at `A0011995` | Offset `0006`, two bytes | Compiled fallback for an unavailable or invalid complemented-byte record |
| `NOS_NVM_FWUPDATE_STATUS = aa` | `A0011944`: item 3, region `0B`, length 1, mode 2 | `AA` at `A0011999` | Offset `0001`, two bytes | Compiled fallback for the firmware-update status item |

Mode 2 stores a byte followed by its inverted byte. Neither `00 00` nor `FF FF` is a valid pair, so absent NVM and an erased EEPROM both reach these defaults. This differs from the five-byte board-ID record described in [board_id.md](board_id.md). The dumped SPI flash does not supply the missing board EEPROM contents.

The powerdown value is used as a bit field. For example, bit 2 can change boot-mode selection (`A000D782`), and bit 3 is set for load mode 2 and cleared for load mode 3 (`A000AA34`, `A000AA70`). The code does not establish a single named enum meaning for the whole value `03`.

`AA` is deliberately written as well as used as a default: load mode 3 writes `AA` to item 3 at `A000AA7C`, while load mode 2 writes zero at `A000AA40`. The observed load mode 4 preserves the fallback. The label alone does not establish whether `AA` should be called "idle", "complete", or another symbolic state, so those names are intentionally left unresolved. The diagnostic is printed unconditionally at `A000AA9E` after these branches.

## Button sentinels and load mode

`A000E70E` initializes two button identifiers to `1 << 13`, or `2000`. It replaces the first identifier if one accepted button is found, and both if at least two are found. `A000E788` prints `(first << 16) | second` as `_powerup_buttons`. Thus `20002000` is **two unfilled button slots**, not an error number or a bit mask of pressed buttons.

The current input profile explains why both slots remain unfilled. `A000E0F6` samples 18 input bits during discovery; at `A000E35C` it XORs them with `0003FFFF` and returns the result with a validity bit. A successful all-high sample becomes zero after that inversion. `A000E46C` removes the validity bit and stores the resulting zero button mask. Later `A000E394` enumerates set bits in that mask, producing no button identifiers. This establishes the firmware's interpretation of the explicit all-high profile; it does not measure the physical panel's current state.

There is another reason to keep `2000` described as a sentinel: the button-code lookup at `A000E028` also returns it for an unmapped raw button index. The observed all-high case needs no such failed lookup. `A000E394` additionally filters seven mapped identifiers using the table at `A00125C8` when its third argument is nonzero.

The two identifiers are compared, in either order, with eight combination records at `A0011044` by `A00076F8`. Neither sentinel occurs in those records. `A000E7AC` maps the resulting combination/state code to a load mode:

| Combination/state code | Load mode |
| --- | --- |
| `06` | `08` |
| `10` | `01` |
| `11` | `02` |
| `12` | `03`, with an assertion if either identifier is the sentinel |
| `13` | `05` |
| `15` | `09` |
| Other values | `04` |

Mode 4 is also returned if serial-panel discovery fails (`A000E744`). It therefore identifies the default selection path, rather than proving any particular panel or failure condition. The wrapper at `A000D74C` can replace mode 4 with mode `0A` when NVM item 6 bit 0 is set, or mode `07` when powerdown bit 2 is set. Item 6 defaults to zero and powerdown defaults to `03`, so neither override applies here. The mode is printed at `A000783A`.

## Route to SPI and the next boundary

The boot routine at `A00077EE` calls the image loader at `A000A9D0`. The loader validates the main header magic `3CA55A3C` at flash `00020034` and its board-support pair, processes the NVM mode flags, and prints the firmware-update byte. At `A000AADE` it calls `A0005970`, which builds the available storage-interface list. That routine calls SPI initialization at `A0005996` -> `A000D07A`.

The earlier 732-byte run stopped at the first SPI control read, `A000D082` reading `BD063808`. With the decoded SPI register model, firmware now prints the device-zero prefix, selects the device, and submits its identification command. `artifacts/serial-spi.json` records 2,126,053 instructions and 754 UART bytes. The unsupported operation is the store at `A000CEE4` to TX register `BD063814`, with `9F000000`; control is `00000023`. The register model stops at this transaction because its receive/completion behavior is not established. No identity response has been fabricated.

After SPI initialization returns, `A0005970` translates detected-device records at `20004C44` into 16-byte interface records starting at `20001ED4`; the image loader then inspects these interfaces and candidate image headers. Main-image section metadata and its offline extraction are documented in [firmware_image.md](firmware_image.md). Reaching identification is still before the loader has completed that selection and scatter-loading route, so it does not yet demonstrate execution of the extracted main application.
