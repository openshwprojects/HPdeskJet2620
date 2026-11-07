# Board ID, EEPROM validation, and the boot warning

The supplied flash identifies a supported board family, but it does not establish this printer's individual board ID. The firmware itself falls back to `0x68bf` when the EEPROM ID is unavailable. The `MISSING! Tell the EEs` message is a recoverable diagnostic in this path, not the reason execution stops at the next unmodeled peripheral.

## Read-only inspection

```
python -m analysis.board_id
python -m analysis.board_id --eeprom path/to/actual-64KiB-eeprom.bin
python -m analysis.board_id --eeprom path/to/actual-64KiB-eeprom.bin --json
```

The inspector reads files only. It neither creates EEPROM contents nor repairs them. `analysis/board_id_probe.py` independently records the relevant calls and RAM tables while the original firmware executes from reset with an explicitly erased EEPROM. Its trace is in `analysis/board_id_probe.json`; no instruction, PC, register, or firmware data is replaced by the probe.

## Three distinct representations

The boot routine at `0xa000962a` uses three related values:

| Representation | Value in this dump | Interpretation supported by the code |
| --- | --- | --- |
| Image/ROM support word | `0x064501ff` | M=`06`, F=`45`, supported N range `01` through `1f` |
| Compiled bitpacked board ID | `0x68bf` | M=`06`, F=`45`, N=`1f` |
| Missing NVM item default | `0x001f` | Sentinel that triggers the MISSING message |

The diagnostic at `0xa0009bb6` calls M/F **Mfg/Fam**; diagnostics at `0xa0009bda` and `0xa0009e7e` identify N as the **board number**. The full expansion of Mfg is not established here. For a bitpacked ID, M is bits 12..15, F bits 5..11, and N bits 0..4. The support word uses M in bits 24..27, F in bits 16..22, minimum N in bits 8..12, and maximum N in bits 0..4. The range checks appear at `0xa0009c5a..0xa0009c90`.

The compiled ID resides at RAM `0x200019b0`. A support word is accepted with an adjacent 32-bit bitwise complement; `0xa000963e..0xa0009668` validates that pair, falls back to flash `0x2000` if necessary, and ultimately has the compiled support literal `0x064501ff` available. A static scan of the complete 16 MiB flash found these matching support/complement records:

| Flash offset | Support | Complement |
| --- | --- | --- |
| `0x002000` | `064501ff` | `f9bafe00` |
| `0x02003c` | `064501ff` | `f9bafe00` |
| `0xffff9c` | `064501ff` | `f9bafe00` |

All three describe the same supported range; none provides a more specific N. The support literal also occurs inside the compressed boot data. Searching for the two bytes `68bf` alone is not evidence of an EEPROM backup: there are 250 raw matches, many within executable or compressed data. No separate valid per-device EEPROM image has been identified in this dump. That is narrower than proving no compressed copy could exist anywhere in the larger firmware.

## Where the board-ID EEPROM record lives

The EEPROM is a separate I2C device, wire address `a0/a1` (7-bit address `50`). The boot path reads its first 200 bytes into cache `0x20004b5c` via `0xa000c90e` and the driver at `0xa000c7e2`. The missing-device path leaves the initially zero cache untouched; an explicitly erased EEPROM fills it with `ff`. These are different bus outcomes but both invalidate this board-ID record.

NVM descriptor lookup is `0xa000c95c`; the board-ID request is item **4**, called at `0xa0009876`. Its eight-byte descriptor at `0xa0011934` is:

```
04 09 02 00 a0 01 19 96
```

The fields are item 4, allocation/region 9, payload length 2, validation mode 0, and default-data pointer `0xa0011996`. The two default bytes at that pointer are `00 1f`. The selected allocation table places region 9 at EEPROM offset **0x000c**, with length **5**. This was verified at the actual `0xa000c9f6` call: region `{offset=12, length=5}`, payload length 2, validation mode 0.

| EEPROM offset | Size | Meaning |
| --- | --- | --- |
| `0x000c` | 2 bytes | Board ID, big-endian |
| `0x000e` | 2 bytes | One bitwise-complemented copy of the two ID bytes |
| `0x0010` | 1 byte | `ff XOR ID[0] XOR ID[1]` |

The complement check is `0xa000c9d0`. The checksum routine `0xa000c9a2` starts with `ff`, XORs each decoded data byte, and XORs the validation mode; item 4 uses mode zero. When checking the backup copy, it inverts each stored byte first.

## Validation includes recovery, not just an all-or-nothing checksum

The reader at `0xa000c9f6` computes three predicates: the copies are complements, the primary checksum matches, and the decoded backup checksum matches. At `0xa000caa8..0xa000cab6` it XORs those three predicates. A zero result chooses the default. The nonzero cases select a copy as follows:

| Observed record condition | Firmware result |
| --- | --- |
| Complementary copies, valid checksum | Accept the decoded complemented copy, status 2 |
| Complementary copies, damaged checksum | Recover the primary copy, status 1 |
| Inconsistent copies, only primary checksum valid | Recover primary, status 1 |
| Inconsistent copies, only backup checksum valid | Recover backup, status 1, except stored checksum zero selects default |
| Inconsistent copies, neither or both checksums valid | Select default, status 0 |

The special backup-only zero-checksum rule is visible at `0xa000cad2..0xa000cadc`. The inspector reproduces it rather than simplifying the reader into a generic checksum validator. `0xa000cb8c` converts the selected bytes to an integer and does not propagate the reader's status to the caller; therefore the actual warning depends on the selected ID, not merely whether a validation check failed.

An all-`ff` record has non-complementary copies and neither checksum matches. An all-zero record has the same failure. Both select default `001f`, and `0xa0009880..0xa0009882` compares the result directly against `0x1f`. The warning string is at `0xa0009a80`.

## How firmware continues after the warning

For a missing NVM ID, the routine retains M/F from the ROM/compiled ID. If N is `1f`, it substitutes the ROM support word's low five bits at `0xa0009918..0xa000992e`. In this image those bits are also `1f`, so the reported “ROM num” remains `68bf`.

The remapper at `0xa00095c2` first accepts any ID whose `(id | 0x1f)` equals `0x68bf`, preserving its N. The table at `0xa0011704` contains three source/destination pairs: `68a0 -> 68c0`, `68c0 -> 68a0`, and `0000 -> 0000`. The routine returns a remapped N, and `0xa0009984..0xa000999c` combines it with the stored M/F. The current `68bf` takes the first fast path and remains unchanged.

The missing-ID branch does not itself set an incompatibility flag. The compatibility/remapping calls succeed for the fallback ID. Firmware prints the final remapped ID and continues initialization. Thus a real EEPROM dump could supply the physical ID and other per-device state; inventing a valid board record merely to silence this warning would discard information about the missing hardware input.

The record tests in `tests/test_board_id.py` use small format examples around the firmware's unknown-ID placeholder. They cover erased/zero records, complement and checksum damage, recovery, ambiguity, and the zero-checksum exception. They do not constitute an EEPROM image for the printer.
