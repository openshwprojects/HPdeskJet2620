# Continuing reset through the MICCI2 bridge

Addresses refer to the supplied image, expanded at 0xa0005720 using the firmware's own decoder. `next_disasm.py` annotates PC-relative literal loads for analysis. The production implementation is in `emulator.py`, `hardware.py`, `micci.py`, and `i2c.py`.

## Second clock/PLL block

ARM routine 0x284c writes 16-bit 0xbd042800=0, toggles bit 12 of 32-bit 0xbd061c08, then writes 0xbd042c04=0xca21a3c3 and 0xbd042c08=0x322. It writes 0xbd042c4c=0x8124, updates bits 4..7 of 0xbd042c48 to 0xc, and writes 0xbd060c34=0x20. After waiting, it polls bit 23 of 0xbd042c04. Finally it writes 16-bit 0xbd041000=1. This is consistent with clock setup and lock, but the exact clock's destination is unknown.

The firmware accesses 0xbd061c08 as both a halfword and a word. These views need coherent big-endian byte lanes.

## System counter and interrupt controller

At 0xa0009e3e the firmware writes 0xee1e5cff to counter 0xbd040814. Counter writes must change its offset/preload while preserving forward virtual time.

The interrupt controller starts at 0xbd040c00. Initialization at 0xa0005b58 clears words +0..+0xc, writes all ones to +0x40..+0x4c, clears words +0xe0..+0xfc and writes halfword +0xb0=2. IRQ configuration at 0xa0005c4c selects word +0xc-4*(irq>>5), mask word +0x4c-4*(irq>>5), and priority word +0xfc-4*(irq>>4). Priorities occupy two bits each. Halfword +0xc8 is the current interrupt priority threshold: relocated routine 0x130 reads it, writes a new level and returns the old value. The routine executes CP15 drain-write-buffer c7,c10,4.

0xbd062800 is a bus-error monitor enable halfword. Its status halfword +2 is checked before enable; a nonzero value causes inspection of the captured address word +0xc. Reset status zero represents no captured error.

## Watchdog and platform pin configuration

0xbd04083c is a retained reset/scratch halfword. Values 0xf001 or 0xf002 select special boot paths; other values take ordinary initialization.

The watchdog block is 0xbd040400. Routine 0xa00067e0 writes control=0xc, writes word +4 = timeout*180+12000000 (saturating), word +8=12000000, then control=3. A feeding routine reads control's low four bits, writes 0xc, and restores those bits. This is consistent with two enable bits and two stop/clear/load bits, but interrupt/reset effects still require verification. Initial cold boot writes 0xc and configures IRQs 5 and 6.

The routine at 0xa0009568 writes pin-mux configuration halfwords under 0xbd060800. Its nearby source filename is `d_pad_mux_cfg.h`: do not label the entire bank clock dividers without further evidence. Writes: +0xe,+0x10,+0x4a,+0x4c,+0x42,+0x44,+0x1c,+0x1e,+0x20,+0x24,+0x26 = 1; +0x1a=0; +0x22=2.

## MICCI2 serial controller

Source filename at 0xa0010330 is `hw_micci2_NOS.c`. Controllers are at 0xbd300800 and 0xbd300c00. Init at 0xa0010194 writes word +4 = 0x001e0018 / 0x002c0018, word +8=0x00b40018 and halfword +0=0x2400. The next UART line is `[Boot] Entering NOS_HW_MCASIC_init`.

Read routine 0xa00101bc and write routine 0xa001025a establish:

| Offset | Width | Meaning supported by code |
| --- | --- | --- |
| +0xa6 | 16 | Status/error; bit 1 is an error condition. |
| +0xac | 16 | Transaction command. |
| +0xb0 | 32 | Data; bit 31 is busy, low 16 bits are result/payload. |

Command = (device&7)<<13 | (register&0x7f)<<6 | subbus<<4 | 0x0f; reads also set bit 5. Read setup writes zero to the data register before issuing the command. Completion is polled by busy clearing, independently of data. The firmware times out using the system counter after approximately 1000 timer ticks (plus one retry).

The implemented bridge samples endpoint read data when it accepts the command and exposes that sample after the scheduled transfer delay. Writes take effect on completion. This is a coarse timing model: the exact serial sampling instant and bus rate remain unmeasured. Firmware ADC polling still observes its independently scheduled conversion completion.

The selected board configuration word at 0xa00108e0 is 0x0406: descriptor index four, device six, controller zero/subbus zero. Its descriptor pointer is 0x200014d4 and the first halfword is expected MCASIC identity 0x8014. The alternative identity 0x8017 is also accepted by code, but is not this dump's selected identity. A modeled identity must be an explicit board profile, not a return value keyed to a firmware address.

After reading register zero, init writes register 6=0, register 12=0x8000, and register 2=0. It then reads register 1 (status bits), register 7 (status/config), and later register 0x20 and power-control registers 8, 9, 10. Their physical reset values are not contained in the flash dump and should remain explicit assumptions or strict boundaries.

## Further MCASIC ADC evidence

The descriptor at +0xda identifies ADC command register 0x10, result register 0x11 and 10-bit precision. Routine 0xa000602c writes command bit 15 as start, bits 8 onward as a sample/averaging mode and low bits as a channel selector. It polls result bit 15 as valid and reads the low ten bits.

The boot calibration channel 0x10 maps through the descriptor to mux selector 15. Firmware checks its sampled value is between 0x1d1 and 0x21f (465..543), consistent with an internal half-scale reference. This supports a possible ideal reference of 512, but a datasheet or physical measurements would confirm it.

The next logical channel 0x11 maps to selector 0. Firmware scales its ADC count by 850*33/1023 into supply millivolts, then compares the result to board thresholds. The supply voltage is an external input and must be an explicit configurable board assumption. A value chosen solely to avoid the failure branch would not establish faithful hardware behavior.

## Implemented MCASIC profile and limits

`Mcasic8014` models the firmware-selected identity and command transport. Its virtual board has a configurable supply (default 24,000 mV, not measured from the physical unit), ideal half-scale reference, and no latched external status events at reset. Only specifically decoded configuration registers accept writes. ADC conversions begin on command bit 15 and set result-valid bit 15 after scheduled virtual time; reads themselves do not advance conversion. Unknown registers and analog selectors remain strict errors. The ADC completion delay is a simulation parameter rather than a measured conversion duration.

**Corrected mux indices verified by transaction trace:** the calibration channel is hardware selector **15**, command 0x830f. Supply voltage is hardware selector **0**, command 0x8400. The descriptor's channel mapping starts at offset +0xe1; logical channel 0x10 indexes descriptor +0xf1=0x0f and logical channel 0x11 indexes +0xf2=0. Earlier scratch analysis suggesting selectors 2 and 1 was an indexing error, not an alternate hardware profile.

## I2C controller and EEPROM

Controller base is 0xbd065800. All observed accesses are halfwords. Initialization 0xa0009340 sets +0xc to 180,000,000/(2*200,000)-1 = 449 and +0xe to 28. The 180 MHz value is a firmware constant, not measured clock speed. The clock's conversion to emulator ticks remains a simulation parameter.

| Offset | Supported interpretation | Evidence |
| --- | --- | --- |
| +0 | Control/status: bit 0 enable, bit 7 busy, bit 8 reset, bit 1 read continuation, bit 2 write continuation, bit 9 16-bit word address | Routines 0xa0009370, 0xa00093ca, 0xa0009476 |
| +2 | Wire-format slave address including read/write bit 0; writing starts transfer | 0xa0009424 / 0xa00094d0 |
| +4 | Word address high byte | 0xa0009416 / 0xa00094c2 |
| +6 | Word address low byte | 0xa000941c / 0xa00094c8 |
| +8 | Data byte | 0xa0009420 / 0xa00094ea |
| +0xc | SCL divider | 0xa0009506 |
| +0xe | Timing parameter | 0xa0009340 |
| +0x12,+0x14,+0x1a,+0x1c | Configuration cleared during disable | 0xa0009524 |

### Unconnected target behavior is established by the negative probe

Routine 0xa000c7ac deliberately reads one byte from reserved wire address 0xfe/0xff (7-bit address 0x7f), word offset zero. It requires the driver to return error 0x81; any other result prints `*** I2C SDA stuck low ***` and repeats. The underlying driver produces 0x81 only when the busy wait at 0xa0009370 reaches its timeout, writes control 0x100 to reset, re-enables with control 1, and observes busy clear. Therefore the controller's failed/unanswered transfer remains busy until reset. Modeling this sequence for an unconnected device is supported by the firmware's explicit negative test; returning immediate successful completion would be incorrect.

The real EEPROM interface at 0xa000c7e2 reads wire address 0xa0/0xa1 (7-bit address 0x50), 16-bit word addresses. Its contents are not included in the 16 MiB flash dump. A supplied EEPROM image or an explicitly selected blank device is independent backing storage; no valid NVM records should be fabricated.

### Sequential reads have a one-byte pipeline

At 0xa0009476, continuation bit 1 is set initially if length is greater than one. When two bytes remain, firmware clears it **before** reading the penultimate byte. Consequently the continuation decision is latched when a byte starts: consuming a byte whose continuation was latched starts the next byte, and that next byte latches the current control setting. This delivers the final byte after continuation is cleared, without fetching an extra byte. A one-byte read starts without continuation and finishes on its first data read.

Sequential writes set bit 2 for more than one byte, preload the first data byte before writing the slave address, and clear bit 2 before writing the final byte.

## Physical storage and prospective SPI work

The board photo `photos/20261023_192108305_iOS.jpg` shows the Winbond 25Q128JV-family flash at U5 and a separate ST 24512WP-marked EEPROM next to the NVM silkscreen. The 512-Kbit EEPROM holds 64 KiB, consistent with the firmware's 16-bit I2C addressing; it is a separate device from the supplied 16 MiB flash. [ST's M24512 datasheet](https://www.st.com/resource/en/datasheet/m24512-df.pdf) supports the EEPROM capacity and interface. The flash capacity also matches the W25Q128JV's 128-Mbit density in [Winbond's product selection guide](https://www.winbond.com/export/sites/winbond/product-selection-guide/file/180117-2018-PSG.pdf).

The SPI driver at 0xa000ceba establishes the controller at 0xbd063800: status word +0, receive word +4, control word +8, and transmit word +0x14. It sets control bit 0 from argument r2 and bit 5 from the inverse of argument r1, then writes r0's low byte into transmit bits 31:24. It polls status bits 15:11 for 13 when r2 is nonzero, otherwise zero. Routine 0xa000cefa places the device index in control bits 11:10. Routine 0xa000ce98 reads a halfword at +0x18 + device*2 and returns its low six bits shifted left by 20; writes at 0xa000d21e additionally encode the inclusive final MiB in bits 11:6.

**The serializer's control semantics remain unresolved.** Interpreting bit 0 as holding CS and bit 5 as receive/capture control is plausible, but not established by a controller datasheet or bus capture. Routine 0xa000cf12 obtains a 24-bit JEDEC identity using arguments `(9f,0,1), (00,0,1), (00,1,1), (00,0,0)`. Both an all-eight-bit transfer model with CS release after the final byte and an eight/sixteen-bit model with a final CS-only operation fit the known identification sequences. Choosing between them solely to return the desired ID would conceal missing hardware knowledge.

`spi.py` therefore contains a standalone byte-level `W25Q128JV` endpoint and a strict register skeleton. The board marking `25Q128JVSQ` maps to W25Q128JVSIQ in [Winbond's Rev G datasheet](https://www.winbond.com/resource-files/w25q128jv%20revg%2004082019%20plus.pdf), supporting JEDEC ID EF4018 and 16 MiB capacity. The endpoint consumes explicit CS edges and clocked bytes, reads the supplied immutable image, and implements JEDEC/status/ordinary/fast/legacy-ID reads plus the volatile write-enable latch. Nonvolatile status defaults are an explicit factory profile because the array dump does not contain those registers. Program/erase, uncaptured metadata, and other unsupported commands fault. The controller skeleton is now routed through `BootHardware`: it validates register widths and mapping encodings, stores configuration, and exposes the explicitly selected idle/SPI startup profile. Transmit requests still fault until the unresolved serializer behavior is decoded; the flash endpoint is not connected to those requests. Its 12 tests exercise protocol framing, actual backing bytes, status state, immutability, board routing, and that strict boundary.

### Full application image review

The application scatter records expose the later 0x4e2e5c-byte code section and its initialized data, not just the early boot block. `python -m analysis.spi_probe` extracts these sections in memory and inventories direct SPI register literals and Thumb calls. The later application has neither direct `0xbd0638xx` register references nor direct calls into the boot SPI routines. This is a direct-reference result, not a proof excluding every computed pointer or indirect call. The apparent raw-image SPI strings near flash 0x58d300 instead belong to the separate Broadcom Wi-Fi GSPI firmware.

The early flash profile table does provide an independent match for the photographed chip: record **0xa0011f60** has ID **0x00ef4018**, label **W25Q128BV (16 Megabytes)**, and geometry **256 blocks of 65,536 bytes**. The newer JV device shares that ID; no firmware-table edit is required to identify it.

The probe helper also compares the two candidate serializer interpretations for all four observed identification sequences. Every sequence contains one r1=1 transfer with zero TX data and ends with one r2=0 transfer with zero TX data. Consequently both interpretations put exactly the same bytes on the wire before the final receive-register read. Additional final-ID tests cannot distinguish them. A useful physical observation would read RX after the third held-CS operation of the JEDEC sequence, before its final call: the candidate models have clocked different numbers of response bytes at that point. A clock/CS capture would resolve the same ambiguity without relying on the receive-register packing.

## GPIO boundary, now resolved

Both the absent-EEPROM and erased-EEPROM board profiles reach 578 UART bytes, ending with the firmware's remapped board-ID report. The former stop was a halfword store to **0xbd0b203a** at PC **0xa0005b1a**, GPIO pin 7's output register. `gpio.py` now executes that store and subsequent direction/latch operations. See [GPIO evidence](gpio.md), [board-ID validation](board_id.md), and [the front-panel boundary](front_panel.md).

The GPIO accessors establish base 0xbd0b2000 + pin*8: halfword +0 input (returned low eight bits by 0xa0005b08), halfword +2 output (written by 0xa0005b14), and halfword +4 direction/configuration (written by 0xa0005a90). Pin descriptors are selected from the table at 0xa00129bc by the wrapper at 0xa0010348. The model retains event-clear and interrupt-mask banks at 0xbd0b2400 and 0xbd0b2500; edge detection and interrupt delivery remain unimplemented. Input pins require an explicit external level.

The NVM write helper drives logical GPIO 0 / physical pin 7 low around EEPROM
writes and high afterward (`0xa000c81a`/`0xa000c838`). The emulated board connects
this pin to active-high EEPROM write protection as a wiring inference. ST's
[M24512-W datasheet](https://www.st.com/resource/en/datasheet/m24512-w.pdf), section
5.1, specifies address acknowledgement but data NACK while protected. The model
enforces this, leaves reads available, and allows an already-started programming
cycle to finish after protection is asserted. Sub-byte setup/hold timing is not
modeled.
