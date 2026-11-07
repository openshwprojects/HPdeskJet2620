# Firmware container and offline extraction

The complete dump contains a linked section catalog and scatter-loading tables, so the main executable can be recovered without guessing offsets or changing execution. `analysis/firmware_image.py` parses those records and implements the same 4 KiB-window LZSS format as the boot decoder at flash `0x3100`. Production emulation still executes the original loader.

```
python -m analysis.firmware_image
python -m analysis.firmware_image --extract-dir artifacts/firmware
python -m analysis.firmware_image --section .text --section .rodata --extract-dir artifacts/main
python -m analysis.firmware_image --json
```

The default inspection writes nothing. Extraction writes separately named `.bin` files plus a manifest containing addresses, sizes, source mappings and SHA-256 digests. Zero-fill regions are omitted unless `--include-zero` is selected. The CLI checks every proposed output before writing: source-file collisions, including existing hard links, and duplicate output paths are rejected. The dump is never opened for writing. Malformed ranges, cyclic section lists, missing linked destinations, truncated LZSS tokens, and wrong expansion lengths fail explicitly.

## Header and catalog

The application header begins at flash `0x20030`. All table words are big-endian. The fields used by the extractor are:

| Header offset | Observed value | Interpretation |
| --- | --- | --- |
| `+04` | `3ca55a3c` | Application-header magic |
| `+08` | `0000006c` | Header size |
| `+0c/+10` | `064501ff/f9bafe00` | Board support and its complement |
| `+38` | `a04e2e05` | Thumb entry address |
| `+3c` | `00020030` | Header self-pointer |
| `+44` | `000203a8` | First linked section descriptor |
| `+48/+50` | `20180/201ec` | Zero-fill triple range |
| `+54/+58` | `201ec/202d0` | Raw-copy triple range |
| `+5c` | `20294` | Code-copy subset begins here |
| `+60/+64` | `202d0/203a8` | LZSS triple range |
| `+68` | `20390` | Compressed-code subset begins here |

There are **133 section descriptors** and **27 nonempty scatter operations**: five zero-fill, nine raw-copy, and thirteen LZSS operations. Each scatter entry contains `{destination, source, stored_length}`. Each section node contains six words `{next_node, name_pointer, address, size, flags, related_section_pointer}`. For a stored `.crom*` section, the related pointer identifies its expanded runtime section and therefore the required expansion size. The parser checks that relationship rather than accepting an arbitrary decompressed length.

For example, `.cromtext` at descriptor `0x20a2c` points to runtime `.text` descriptor `0x2125c`. Its source is flash `0x791160`, packed length `0x39faf7`, runtime base `0xa00150d0`, and expanded length `0x4e2e5c`. The expanded end is exactly the next section's address, `0xa04f7f2c`.

## Important extracted sections

| Runtime section | Flash source | Packed bytes | Runtime base | Expanded bytes |
| --- | --- | --- | --- | --- |
| `.load_text` | `39ec` | `8fef` | `a0005720` | `b10c` |
| `.load_rodata` | `c9dc` | `1611` | `a001082c` | `21f0` |
| `.load_data` | `dff0` | `5e7` | `200014c0` | `9a4` |
| `.load_ncdata` | `e5d8` | `4` | `60001040` | `4` |
| `.text` | `791160` | `39faf7` | `a00150d0` | `4e2e5c` |
| `.rodata` | `6c00f0` | `b0f0d` | `a04f9568` | `1999cc` |
| `.fixcon` | `6b3ec0` | `c22f` | `a0692f60` | `142d0` |
| `.data` | `771000` | `1fda7` | `2081e0a0` | `52750` |
| `.ncdata` | `790da8` | `30d` | `606b15a0` | `125c` |
| `.srdata` | `7910b8` | `a8` | `60000ae8` | `188` |

All values in this table are hexadecimal. Other compressed sections are `.module`, `.romfs`, and `.romfs_objs`; the complete manifest includes them and all raw-copy operations. In particular, `.nosi_text` and the fast-IMEM sections are raw relocations rather than compressed streams.

SHA-256 digests of the major recovered files:

```
.load_text   2cfea07c78ba6d97325352971fdff4aa80a6abadcc48d96145f9fb298f1c8725
.load_rodata 0839fa2e9536843f334ff18329aabb7272dc858e76bd7e56c95a20281a0d167e
.text        ea48cad6bf6c98a75e1012037da50928a7029963cc1f3cc3507ca269602a2da5
.rodata      cb2b6494c72e700b2eb7cb7b1b00f03e801f302c7f41682a433783e7ce32c451
.fixcon      063ca6a6b93ae712ac360050866c40ef0f67cf0e2292d23ec22e070903bd6586
.data        364bb7d67d2a5e330657161ef04bb45ec52937d22a3813f1faab89f558ef2e05
```

The input dump SHA-256 remains `b791436cf9152785fb5d3f6954aa5949d4ae7c7458af0e2031b9a41f5534c496`. Eleven focused tests validate all declared expansion sizes, known code hashes, literal/overlapping/initial-zero decoding, raw relocations, malformed metadata, and output-collision protection.

## Regions outside these scatter operations

Large uncompressed resource sections include `.fixcon_fonts` at `0x22000`, JPEG demo data at `0x192b68`, and `.hpromobj` at `0x210940`. A raw SPI-related string around `0x58d300` belongs to an embedded Wi-Fi firmware resource, so it should not automatically be attributed to the printer's AXON flash controller.

The catalog's `.usrincr_data` points at `0xc80000` with declared length zero; actual bytes there begin `GNDT` and include data/certificates. The extractor does not invent a length for that dynamic region. A small uncompressed Thumb component at `0xc93bd0` has RFIX diagnostics and an adjacent `TJP1FN1809AR` build label dated March 2018. The final footer contains `0x00c93bd1` and `0x00c93e98`; these are plausible Thumb entry/end values, but their full loader semantics remain to be traced. There is only one `3ca55a3c` application header in the raw dump.
