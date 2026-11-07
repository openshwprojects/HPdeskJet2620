# CPU, memory map and system peripherals (2026-11-06)

Collected from the printer (`sysinfo`, `md`, tests of the custom firmware) and
from HP's code. "Measured" = read or observed on the physical printer;
"HP code" = derived from the firmware dump at the given address.

## CPU

| Item | Value | Source |
| --- | --- | --- |
| Core | ARM946E-S, ARMv5TE, CPU ID `0x41059461` (ARM, part `0x946`, revision 1) | measured |
| Byte order | big-endian (SCTLR B bit), ARM and Thumb code | measured |
| SCTLR | `0x000510fd`: MPU, D-cache, I-cache on, big-endian, low vectors (V = 0), DTCM and ITCM enabled (bits 16, 18) | measured |
| Caches | cache type `0x0f112152`: 16 KiB instruction + 8 KiB data, 4-way, 32-byte lines | measured |
| ITCM | 8 KiB at `0x00000000` (MPU region 1 `0x0..0x1FFF`): exception vectors and HP's low code (dispatchers at `0x130`, `0x16C`, `0x1F8`) | measured, HP code |
| DTCM | HP keeps data at `0x60000000..` (interrupt-controller pointers at `0x60000000..0C`, `.fast_dmem1_data`); size not measured | HP code (`analysis/main_entry.md`) |
| Clock | HP's firmware uses 180 MHz as the peripheral reference (I2C divider, watchdog counts); core clock not measured | HP code |

The custom firmware runs in SVC mode with IRQ and FIQ masked and polls
everything; the interrupt path itself works (`analysis/interrupts.md`).

## Memory map

| Range | What | Access (MPU, set by HP stage-1, table `0x31F0`) |
| --- | --- | --- |
| `0x00000000..0x00FFFFFF` | SPI flash, memory-mapped read window (16 MiB, W25Q128JV); the first 8 KiB are covered by the ITCM | region 0 (0..64 MiB) read/write, execute; region 1 (`0..0x1FFF`) data read-only |
| `0x20000000..0x23FFFFFF` | DRAM, 64 MiB (HP's `.endram` = `0x24000000`), cached, for data | region 3: read/write, no execute |
| `0x60000000..` | the same DRAM, uncached (DMA buffers: SDIO `0x60800000`, USB `0x60810000`); DTCM at its start | region 4: read/write, no execute |
| `0xA0000000..` | the same DRAM, cached, for code | region 2: read-only, execute |
| `0xBC000000..0xBFFFFFFF` | peripherals (`0xBD......`) | region 5: read/write, execute |
| `0x10000000` (64 KiB) | unidentified RAM | region 6: read/write, execute |

Code written through `0x20...` must be cleaned from the D-cache and the
I-cache invalidated before it runs at `0xA0...` (`fw/common/cache.h`). HP's
firmware occupies DRAM up to `0x20EFD560`; the rest is its heap. The custom
firmware: bootloader `0xA0020000`, application `0xA0100000` (data
`0x20180000`, stack top `0x20200000`), staging buffer `0x20400000`.

## System block `0xBD040000`

| Address | Register | Notes |
| --- | --- | --- |
| `0xBD040000` | ASIC ID | `0x00400000` on this printer (`sysinfo`, measured) |
| `0xBD040010..1C` | SoC ID words | device-unique; HP derives the NVM key from them (`analysis/nvm.md`); this printer: `references/physical-eeprom/chip-id.txt` |
| `0xBD040400` | watchdog control | `0xC` = stop/clear (reloads the count), `0x3` = both stages on. Feed = remember the on bits, write `0xC`, write them back (HP's routine; `watchdog_kick()`). Measured: with `0x3` and no feed the SoC resets (about 8 s for the custom firmware's setting); fed from the idle and wait loops it runs indefinitely |
| `0xBD040404` | watchdog first-stage count | 180 MHz counts (`12,000,000 + 180,000,000 * s`) |
| `0xBD040408` | watchdog second stage | HP uses `12,000,000` (about 67 ms) |
| `0xBD060002` | watchdog routing | set by stage-1 |
| `0xBD040800..` | timer channels | prescaler `0x80E` = 180 (1 us counts), reload `0x81C + 4*ch`, groups `0x804..0x80C` -> interrupt sources 18..22, pending `0x800`, write-1-to-clear `0x802`, stop `0x812` (measured with the `irq` test) |
| `0xBD040814` | free-running counter | 1 MHz on the printer (measured against the UART by the bootloader); the custom firmware's only time base |
| `0xBD04083C` | reset-retained halfword | `0xF001`/`0xF002` select special boot paths in stage-1 |
| `0xBD040C00` | interrupt controller | masks, priorities, threshold `0xCC8`, claim `0xCB8` (`analysis/interrupts.md`) |

## Other peripheral blocks

| Base | Block | Notes |
| --- | --- | --- |
| `0xBD041010` | scan SCIF reference select | `analysis/scanner.md` |
| `0xBD060800` | pad mux | `analysis/gpio.md` |
| `0xBD062800` | bus-error monitor | `analysis/boot_hardware.md` |
| `0xBD063800` | SPI flash controller | `fw/common/spi_flash.c`, `analysis/spi_review.md` |
| `0xBD065800` | I2C controller | `analysis/i2c.md` |
| `0xBD066C00..0xBD06B4xx` | scan block (SCIF, TG, datapath, LED PWM) | `analysis/scanner.md` |
| `0xBD09BC00`, `0xBD09FC00` | USB 2.0 device controller (endpoints, global) | `analysis/usb_console.md` |
| `0xBD0B0000` | SD host controller (Wi-Fi BCM43143 on SDIO) | `analysis/wifi.md` |
| `0xBD0B1410` | front-panel serial shift interface | `analysis/front_panel.md` |
| `0xBD0B2000` | GPIO (8 bytes per pin) | `analysis/gpio.md` |
| `0xBD0B3C00`, `0xBD0B4C00` | UART0 and its FIFO/baud block | `fw/common/uart.c` |
| `0xBD300038`, `0xBD302010`, `0xBD302410` | encoder counters (channels 10, 7, 8) | motor B, motor C, unused (`analysis/motors.md`) |
| `0xBD300800`, `0xBD300C00` | MICCI controllers 0 and 1 | MCASIC (motors), scan front end (`analysis/motors.md`, `analysis/scanner.md`) |

Not identified: the SoC's exact part name (HP's boot banner calls it "AXON
RevA .0 fab0 Marvel"), the core clock, the DTCM size, and region 6.
