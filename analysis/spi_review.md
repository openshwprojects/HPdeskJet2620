# Independent review of the SPI candidates

The two optional serializer profiles are useful experiments, but the supplied
firmware does not select between them. Successful boot under both profiles
would establish that the later boot path is insensitive to this ambiguity. It
would not establish the actual meaning of AXON control bit 5.

## Constraints from the firmware

The complete direct call inventory in `analysis/spi_probe.py` finds the
transfer helper `A000CEBA` called only from identification routine `A000CF12`.
The inventory covers all initialized scatter sections. It does not prove that
indirect calls or computed peripheral addresses are absent.

`A000CEBA` preserves control bits other than bit 0 and bit 5, writes its byte
argument into TX bits 31:24, then polls status bits 15:11. It expects 13 when
bit 0 is set and zero when bit 0 is clear. This establishes two terminal states
of the driver's handshake. It does not establish intermediate state codes,
clock speed, when CS changes relative to the control write, or the duration
of a final CS-only request.

`A000CEFA` places the device argument in control bits 11:10. The initialization
loop visits slots 0, 1, and 2. The byte returned through RX is shifted into the
low end of the word: the ordinary JEDEC path keeps RX bits 23:0, the legacy
`85` path keeps bits 15:0, and the `AB` path keeps bits 7:0. The extended JEDEC
path compares the complete last four bytes with `20180300` or `20180301`.
This supports a 32-bit rolling receive register; it does not reveal its reset
value or the voltage of an undriven MISO pin.

The clear-bit-5 transfer is not consistently the penultimate request:

| Identification sequence | Number of requests | Request with bit 5 clear |
| --- | ---: | ---: |
| `9F` ordinary JEDEC | 4 | 3 |
| `AB` legacy device ID | 5 | 4 |
| `85` legacy Macronix ID | 4 | 2 |
| `9F` extended JEDEC | 6 | 3 |

Every such request has zero TX data, and every final bit-0-clear request also
has zero TX data. Consequently, one extra zero byte on the bit-5-clear request
and no byte on the final request has the same wire data as one byte per
request. This is why the two profiles are observationally equivalent on these
callers. A simple "clear RX before this byte" interpretation for bit 5 is
inconsistent with ordinary JEDEC: it would discard the manufacturer byte
already received, although the firmware retains that byte in the low 24-bit
ID. Other pipelined or conditional interpretations remain possible.

## The `85` fallback identifies a different flash protocol

The manufacturer's Macronix MX25L6402 datasheet, PM0943 Rev. 1.0, describes
`85` as READ ID: command byte, one dummy byte, manufacturer byte, device byte.
It also describes data output synchronized to the rising clock edge. This
matches the four-byte `A000CF2E..A000CF58` sequence and the legacy Macronix
entries in the firmware's device table. The manufacturer document is available
as a [mirrored transcript](https://dtsheet.com/doc/766163/mcnix-mx25l6402mc-40).

The firmware toggles control bit `12 + device` when the first identification
attempt returns zero, and changes the software selector between the `9F`/`AB`
route and the `85` route (`A000D130..A000D16C`). Per-device protocol or timing
selection is therefore a better-supported interpretation than CS polarity.
The exact register meaning remains unknown; the current candidate models
correctly stop if these bits are used.

For the attached Winbond part, the [W25Q128JV Rev. G datasheet](https://www.winbond.com/resource-files/w25q128jv%20revg%2004082019%20plus.pdf)
documents `9F` with three response bytes and `AB` with three dummy bytes then
device ID. Its `9F` diagram ends after the third ID byte; it does not specify
the subsequent response. The endpoint therefore keeps its strict boundary
after those three bytes. The firmware's longer JEDEC path is conditional on
ID `012018`, so it does not require inventing extra Winbond ID bytes.

An unattached slot has no endpoint to acknowledge an opcode or supply an ID.
Its receive level depends on board wiring. Explicit `miso_pull=0` or `1` is an
assumption about that wiring, not an identity response. The zero-pull case can
exercise the alternate-protocol branch; the high-pull case can reach unknown
device/memory-window probing. Neither result by itself identifies the real
pull configuration.

## Implementation review

The optional implementation in `spi.py` retains the strict default. With a
candidate selected, it schedules byte completion independently of register
reads and derives received bytes through the flash endpoint. Device changes
while selected, active-transfer collisions, unsupported control bits, and
unknown receive bits are explicit errors. Endpoint errors latch for the next
status poll and do not mark the transfer complete or silently release CS.
No concrete state-transition or CS-framing bug was found in this review.

The following remain declared modeling choices, rather than recovered AXON
facts: intermediate status value 1, one simulation tick per bit, the delay of
a CS-only release, when CS assertion occurs relative to the TX request,
control bit 1's role, and bit 5's role in the byte-final profile. These profiles
must remain identified as hypotheses in run reports. Matching UART under both
does not remove these limits.

The window register decoding is consistent with `A000D21E`: low six bits are
the start MiB, next six the inclusive end MiB. Firmware writes `0FFF` when a
slot's detected capacity is zero. This is evidence of a special empty-slot
configuration but does not alone prove whether AXON treats that value as a
disabled window or an otherwise valid last-MiB window.

## Physical observations (2026-11-04, custom firmware on the owner's printer)

Measured from the custom application's `flash diag` command running on the
physical DeskJet 2620 (`fw/common/spi_flash.c`, `flash_diag`). Values are
32-bit RX register reads; the chip is the W25Q128JV (JEDEC `ef4018`); flash
`0x022000` holds `48 50 44 4a`.

| Register / sequence | Value on the printer |
| --- | --- |
| control `0xbd063808` after HP stage-1 | `0x000000ea` (bits 7, 6, 5, 3, 1; no strap bits 15:12) |
| status `0xbd063800`, idle | `0x00ff0000` (state field 0; bits 23:16 read `ff`) |
| window registers `+0x18/+0x1a/+0x1c` | `03c0 0fff 0fff` (the emulator's assumed values) |
| E1: HP pattern `9F` (bit 5 set, set, clear, set; RX once at end) | `00ef4018` (correct) |
| E2: same, RX after each transfer | `ef401801 401801de 0c00ef40 00ef4018` |
| E3: `9F`, bit 5 set on every transfer, RX at end | `01de8030` |
| E4: same, RX after each transfer (first driver) | `de803001 803001de 3001de80 01de8030` |
| E5: `03 022000` with HP's data pattern | `485044` (correct) |
| E6: `03 022000`, bit 5 clear on all three data transfers | `00121411` |
| E7: `03 022000`, bit 5 set on data transfers, RX after each | `00000090 000090a0 0090a088` |
| E8: `05` with HP's pattern | `000000` |

Interpretation, limited to what these numbers show:

- With control bit 5 **set**, received data is shifted left by one bit
  relative to the chip's byte boundaries (E3, E4, E7: `ef4018` reads as
  `de8030`, `485044` as `90a088`). The bit after each byte (the next byte's
  MSB) appears in bit 0: E2's first read already holds `01`, the first ID bit,
  captured during the opcode transfer. This is consistent with MISO being
  sampled one SCK edge late in that mode.
- A bit-5-**clear** transfer that follows bit-5-set transfers in the same CS
  cycle shifts the register by only seven bits (E2: `401801de` becomes
  `0c00ef40` = (`401801de` << 7) | `40`), which realigns the stream; the
  following bit-5-set transfer then captures the next byte aligned (E2's last
  value, E1). HP's identification pattern relies on exactly this.
- Several consecutive bit-5-clear transfers (E6) return each byte shifted
  right by two (`48 50 44` read as `12 14 11`); that mode is not understood
  and is not used.
- Every byte-final transfer clocks eight bits, including the CS-releasing one
  (E4's fourth read brings new data), so the byte-final framing hypothesis
  holds for transmit; the wide-final hypothesis does not describe this board.
- The custom driver therefore reads only with HP's pattern (three data bytes
  per CS cycle: bit 5 set, clear, set; RX read once at the end), transmits
  commands with bit-5-set transfers, and verifies reads against the memory
  window before enabling writes. Erase, program, verification and booting
  the written image were confirmed on the printer.

The emulator's `SPIController.rx_read_shift` flag reproduces the first-driver
symptom for regression tests; it does not implement the sampling mechanism.
