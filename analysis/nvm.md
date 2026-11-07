# Settings EEPROM (NVM) format

The settings EEPROM is an ST M24512: 64 KiB at I2C address `0x50`, with 16-bit word
addresses. `python -m analysis.nvm_decode <image> [--objects] [--json] [--crosscheck]`
decodes an image of it. The decoder only reads files. It accepts a raw dump, HP's
64 KiB RAM mirror, or a shorter prefix such as the 256-byte header capture.

Confidence labels used below:

- **proven**: taken from HP firmware code or tables, or reproduced exactly from HP's own command output.
- **strong**: consistent with every available capture, but not traced fully through the code.
- **inferred** / **unknown**: as stated.

## Source images

- `references/physical-51-nvm-mirror/nvm-ram-mirror-65536.bin` is HP's RAM cache of the part (`nvm2.dump2file`). It uses the same byte addressing as the EEPROM (**proven**):
  - its first 256 bytes equal `references/physical-15-nvm-header/nvm-header-0000-00ff.bin`, which was read with the hardware reader `nvm2.nvm_read_byte`;
  - 22 more direct reads matched it;
  - every structure below decodes from it at EEPROM offsets.
- The mirror is not an atomic snapshot. It may differ from the chip in writes that were still pending.

## Top-level map (map revision 4)

| Range | Contents | Confidence |
| --- | --- | --- |
| `0x0000-0x00b1` | Boot "NOS" region: 21 regions, 13 items with copy, complement and checksum | proven (boot ROM tables) |
| `0x00b2-0x02fe` | NVM3 metadata, 589 bytes. HP reports "MetaData and wastage: 589" | proven |
| `0x02ff-0xfffe` | 253 pages × 256 bytes. Pages 0-59 are the journal; the other 193 are data pages | proven |
| `0xffff` | unused | proven (arithmetic) |

## Boot region

Byte `0x0000` is the layout byte (`0x40` here). Its high nibble selects a region table through `0xa0007590`; the selector table is at `0xa0010f8c`:

- `0x30` and `0x33` use `0xa0010fa4` and `0xa0010ff4`;
- revision 4 (high nibble 4) uses `0x20001804`.

For revision 4, `0xa00075f8` sets region 8 to `(low nibble + 3) * 4 + 2` bytes. HP's `nvm2.get_map_details` confirms the result: "Error Log `0x5c`/70, Eagle Bits `0xa4`/14, Clock Offset `0x52`/4, NVM3 `0xb2`/65358".

Item descriptors are 8 bytes each at `0xa0011924`: item, region, payload length, validation mode, default pointer. The reader is `0xa000c9f6`. The validation modes are:

- mode 0: copy, complement, then checksum `ff ^ mode ^ XOR(data)`;
- mode 1: raw bytes;
- mode 2: copy and complement only;
- mode 3: like mode 0, with the payload capped at `(len-1)/2`.

The reader recovers from damaged copies the same way as the board-ID record; see `analysis/board_id.md`.

| Offset | Region | Item | Value in the mirror | Meaning | Confidence |
| --- | --- | --- | --- | --- | --- |
| `0x0000` | 0x0 | 0 | `0x40` | layout byte, map revision 4 | proven |
| `0x0001` | 0xb | 3 | `0xa9` (`a9 56`) | boot update-gate bits | strong (`analysis/storage_path.md`) |
| `0x0003` | 0xc | 2 | `ff ff` is invalid, so the default `0x08` applies | unknown | — |
| `0x0005` | 0x1 | 8 | `0x01` | unknown | — |
| `0x0006` | 0xa | 1 | `0x02` | unknown flags | proven mapping, unknown meaning (a) |
| `0x0008`, `0x000a` | 0x2, 0x3 | – | `b3 00`, `c6 22` | no boot item | unknown |
| `0x000c` | 0x9 | 4 | `0x68a4` | board ID (`analysis/board_id.md`) | proven |
| `0x0011` | 0x8 | – | `01 0d f5 03 0a bb ff…` (14 bytes) | no boot item | unknown |
| `0x001f` | 0xd | 5 | 14 ASCII chars, last four `06MD` | serial number | strong (b) |
| `0x0048`-`0x0051` | 0xe, 0xf, 0x10, 0x12, 0x13 | 6, 7, 9, 10, 11 | all `ff ff`, so defaults apply | unknown; item 6 has the same mapping as item 1 | — |
| `0x0052` | 0x14 | 12 | `ffffffff` (-1) | clock offset | proven (HP `nos_nvm_sirius.get 18` = -1) |
| `0x0056` | 0x11 | – | `ff`×6 | no boot item | unknown |
| `0x005c` | 0x4 | – | 5 × 14-byte assert ring | hardware error log | proven |
| `0x00a2` | 0x5 | – | `01 71` | ring index 1, XOR checksum of the ring (valid) | proven (`0xa0007314`, `0xa0007540`) |
| `0x00a4` | 0x6 | – | `ff`×14 | "Eagle bits"; meaning unknown | name proven |

Notes:

- (a) The `nos_nvm_sirius` key table at `0x20847eb4` maps keys 0-7 to the bits of item 1 and key 15 to the whole byte, with DS2 mirror DSID 65570. Keys 8-14 and 19 are the bits of item 6, key 16 is its whole byte (mirror DSID 75032), and key 18 is item 12.
- (b) The firmware feeds the first 15 bytes of item 5 into an NVM key derivation (`0xa0044e54`, key id `0x0089`). The text has the shape of an HP serial number. HP's `DSID_SERIAL_NUMBER` (66038) was never read, so the two cannot be compared.

Each error-log entry is 14 bytes, written by `0xa00074b0` and read by `0xa00073f6` with entry 0 being the newest: assert ID (u32), line (u16), file-name pointer (u32), page count (u32). The mirror holds one entry: `0xc4eb99f7`, `hw_micci2.c` line 1733, page count 775. HP's `errlog.get_assert_entry 0` printed the same values.

## NVM3 store

### Metadata

| Offset | Bytes | Contents | Confidence |
| --- | --- | --- | --- |
| `0x00b2` | 13 | `c7 00000100 00 fd0005 000002 4c`. The word at `0x00b3` is 256, the page size | unknown fields |
| `0x00bf` | 253 | logical-to-physical data page map; `ff` marks an unused entry. The mirror uses 43 pages, which with the 60 journal pages gives 103 = 253 − 150 free ("Free Pages: 150") | strong |
| `0x01bc` | 32 | one bit per page, LSB first: the page ends with a hole chain. A clear bit means the page is full | strong |
| `0x01dc` | 60 | journal page map. It is the identity map 0..59 here | strong |

### Pages and holes

Physical page *n* occupies `0x02ff + 256*n` to `0x03fe + 256*n`. Freed space ("holes") is described from the last byte of the page backwards (`0xa02f138a`, `0xa02f1346`, `0xa02f1370`). The final byte of each descriptor determines the hole:

- `c0`: a 1-byte hole; the chain ends.
- `c1`: a 2-byte hole; the chain continues at the preceding byte.
- `c9`: a 2-byte hole; the chain ends.
- `[size][prev][c2]`: a hole of `size` bytes; the chain continues at offset `prev`.
- `[size][ff][ca]`: a hole of `size` bytes; the chain ends.

The trailer writer is `0xa02efdfc`. The holes in the mirror total 2102 bytes, which equals HP's "Combined Holes: 2102".

The journal pages are all written (ring), but their record format was **not decoded**. The data-page stream does not need the journal to be complete: it accounts for every object, as shown below.

### Object stream

The data pages are concatenated in page-map order with holes removed. The result is one stream of 8906 bytes, which equals HP's "Current Usage: 8906" (**proven** by reproduction). The reader is `0xa02f261c`. The stream is:

- a raw big-endian DSID (`00 01 00 0e` = 65550);
- then, for each DSID, its value tokens;
- each following DSID is given by a delta token;
- DSIDs are strictly ascending. The mirror has 1003 of them, equal to HP's "Total # of objects: 1003" and to the 1003 objects of the reference catalog.

There is no explicit file or record header. The DS2 file and table grouping exists only in the firmware's object tables; `references/physical-nvm-catalog.json` supplies names and file IDs.

The prefix code is **proven** from decoder `0xa02f3030..0xa02f336a` and encoder `0xa02f2d7c..0xa02f2fec`. Its tables are at `0xa05c913c` and `0xa05c915c`.

| First byte | Token | Payload |
| --- | --- | --- |
| `40-7f` | DSID delta: 6-bit value + 1 | – |
| `e0-ef` | DSID delta: 12-bit value + 1 | +1 byte |
| `fc-fd` | DSID delta: 17-bit value + 1 | +2 bytes |
| `80-bf` | integer, 6 bits | – |
| `d0-df` | integer, 12 bits | +1 byte |
| `c8-cf` | integer, 19 bits | +2 bytes |
| `c7` | integer, 32 bits | +4 bytes |
| `c0-c6` | byte string, 0-6 bytes | n |
| `f0-f7` | byte string, 5-12 bytes | n |
| `f8-fb` | byte string, 10-bit length | +1 length byte, n |
| `fe` | byte string, 16-bit length | +2 length bytes, n |
| `ff` | end / erased | – |

Token types by object kind:

- **Scalars** use one integer token. Signed types sign-extend it by the token width.
- **Simple arrays** use one integer token per element.
- **Record arrays** use one byte string per record. The reader pads a short record to full size by repeating its last byte (`0xa02f329e`, **proven**). The writer's trimming rule is empirical: the stored length is `max(length without trailing repeats of the last byte, min(record size, 8))`. It holds for 84 of 86 HP records; the two exceptions are `DSID_NVM2_UT_2`, a unit-test object.

The encoder uses a single byte for unsigned values 0-31 and signed values -8..31. It moves to 12, 19 and then 32 bits at 0x7ff and 0x3ffff (`0xa02f2d7c`).

### Value encryption

Every byte of an integer token after the first, and every byte-string payload, is XORed with a per-DSID keystream. The keystream is filled at start-up by `0xa02f34ee`:

```text
ks = AES-128-ECB_K( BE32(DSID) || 00×10 || BE16(block) ), block = 0, 1, ...
K  = SHA-256( 0a 94 01 00 || BE32[0xbd04001c] BE32[0xbd040018] BE32[0xbd040014] BE32[0xbd040010] )[:16]
```

Offsets into the keystream:

- scalars start at offset 0;
- simple-array element *i* starts at *i × element size*;
- record *i* starts at *i × record size* (`0xa02ed01c`, `0xa02f1fc0`, `0xa02f25be`).

The evidence for each part:

- **Key setup** (`0xa02f348c`): the KDF at `0xa0044e54` with key id `0x0a94` and parameter `0x100`. A parameter above `0xff` selects the chip-ID words (`0xa0044de4` reading `0xbd04001c`..`0xbd040010`).
- **Hash**: SHA-256. The IV at `0x20001860` is SHA-256's.
- **Block cipher**: AES. Key lengths are 16, 24 or 32 bytes with 10, 12 or 14 rounds, in the rijndael-fst code at `0xa0043578` and `0xa0043964`.

A second key (id `0x0089`) is derived from the serial number. It is set up in the same function, but its role (objects with a flag at descriptor `+0x16`) was not traced.

Confidence: the XOR and its byte alignment are **proven**. HP's 887 scalar values give 863 consistent first-byte classes, and every one of the 667 single-byte values matches.

The KDF inputs and the keystream layout come from static RE (**strong**). They have **not** been verified end-to-end, because the 16-byte chip ID is not in any capture. With the custom firmware, `md 0xbd040010 4` would read it. The decoder then takes `--chip-id W10,W14,W18,W1C` and checks the result against HP's values with `--crosscheck`. A wrong key gives 0 of 220 equal; it is reported as rejected.

Consequences for a dump without the chip ID:

- single-byte values are exact: 896 tokens, including all 667 single-byte scalars;
- wider values show only their token class and top 3 or 4 bits as a range. For example, `DSID_TOTAL_ENGINE_PAGE_COUNT` is `[768..1023]`, and HP printed 775;
- record contents stay hidden, though their lengths are visible.

## Cross-check against HP's own commands (mirror)

- **Single-byte scalars**: all 667 match `physical-storage-scalars.json`.
- **Multi-byte scalars**: 196 have a clear header byte consistent with HP's value. 24 differ, and their encrypted values cannot be compared directly. Most of them are pen IDs and acknowledgements, stall codes and timers. HP's values were read about 75 minutes before the mirror.
- **Arrays**: all 101 element counts match `physical-storage-arrays.json`.
- **Records**: 84 of 86 lengths match the length rule.
- **Exact values (examples)**:

  | Object | Value |
  | --- | --- |
  | `DSID_PENS_INITIALIZED` | 3 |
  | `DSID_TOTAL_PEN_COUNT_K` / `DSID_TOTAL_PEN_COUNT_CMY` | 10 / 1 |
  | `DSID_MECH_CARRIAGE_STALL_COUNT` | 7 |
  | `DSID_MECH_PAPER_JAM_COUNT` | 1 |
  | `DSID_MECH_PAPER_STALL_COUNT` | 10 |
  | `DSID_PEN_TOTAL_INK_USAGE_ML` | 28 |
  | `DSID_PAPER_CAL_A/B_OFFSET_SETTING` | -1 / -3 |
  | `DSID_PEN_CHUTE_DS_LEFT_PEN_TYPE` | 5 |

## Unknown

- The fields of the NVM3 header (`0x00b2-0x00be`).
- Boot regions 0x2, 0x3, 0x8 and 0x11, and items 1, 2, 6-11.
- The journal record format.
- The role of the serial-derived key.
- The two `DSID_NVM2_UT_2` record lengths.
- The end-to-end decryption check, which needs the chip ID.

## Privacy

The following fields identify the device:

- **Serial number** (`0x001f`, item 5): stored in **clear text**. The decoder masks it (`**********06MD`) unless `--show-identifiers` is given.
- **Network identity**, stored encrypted in NVM3:
  - MAC addresses `DSID_NCA_MAC_ADDRESS` (HP read two, ending `…ea6b` and `…ea6c`);
  - host names `DSID_NCA_HOST_NAME`, `DSID_NCA_USR_HOST_NAME(_16)`, of the form `HP` + the last 6 MAC digits;
  - mDNS name `DSID_SAND_MDNS_DOMAIN_NAME`, which contains the full MAC;
  - UPnP UDN `DSID_UDN_DEV_UDN`;
  - display name `DSID_GCP_DISPLAY_NAME`;
  - adaptor and protocol configuration `DSID_NCA_*_CONFIG`, which include IP settings.
- **Credentials and keys**: `DSID_CERT_SVC_CERT` and `DSID_CERT_SVC_REQ_CERT`, plus an unnamed 1680-byte record in the same file (1398), which is likely key material. `DSID_ACCESS_LIST_DB` and `DSID_PINK_SUPPLY_UID` are also sensitive.
- **The 16-byte chip ID** (`0xbd040010`) is device-unique. Anyone holding the board can derive the NVM key from it, so the encryption protects a removed EEPROM, not the device.

With a key, the decoder redacts decrypted records whose names match these patterns, and every unnamed object. The board ID (`0x68a4`) and the error log identify the model, not the unit.
