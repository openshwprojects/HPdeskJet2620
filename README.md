# HP DeskJet 2620 firmware emulator

Two things live in this repository: a custom firmware that runs on the real
printer (`fw/`, summary just below) and the emulator that runs HP's original
firmware and ours (everything after that).

## Custom firmware on the real printer: what works (2026-11-05)

HP's stage-1 boot ROM starts our bootloader (flash `0x022000`, never written
by software), which boots the newer of two application slots (A/B, 512 KiB
each). Everything below was checked on the physical DeskJet 2620; details,
commands and the open questions are in [fw/README.md](fw/README.md).

| Area | Status |
| --- | --- |
| Consoles | UART shell (115200), the printer's USB port as a serial port (high speed, started at power-on), a web shell |
| Updates | into the inactive slot, verified: XMODEM over the UART or USB (`fw.tool update`), HTTP over Wi-Fi (`fw.tool ota`, `POST /api/update`) |
| Wi-Fi | BCM43143: scan, WPA2 join with DHCP (default network joined at power-on, rejoined after a lost link), own access point with DHCP server, mDNS `deskjet.local` |
| Web page | port 80: motors, sensors, panel LCD, log, shell, application upload |
| Motors | A (paper feed), B (carriage), C (scanner) driven through the MCASIC; encoder positions and end-stop detection for B and C |
| Panel, sensors | front-panel display and buttons, panel menu; cartridge door and tray sensors |
| Interrupts | first hardware IRQ: timer tick through HP's interrupt controller (`irq demo`) |
| Not yet | printing (printhead carriage chip not woken up), scanner image (front end answers, no line read yet), motor A encoder, automatic rollback |

Recovery: the bootloader stays in its shell after three failed starts and can
run an image from RAM; [dump_helloWorld_v3.bin](dump_helloWorld_v3.bin) is a
full flash image for an external programmer. Analysis notes are in
[analysis/](analysis/), the original firmware's UART captures in
[references/](references/README.md).

## Emulator

Python + Unicorn execution of `dump.bin`, starting at its reset vector. The
firmware initializes memory, decompresses its code, talks to modeled peripherals,
and writes every output byte to the UART. Firmware instructions, return values,
checksums, and output strings are not patched to advance boot.

With explicit panel and experimental SPI profiles, reset execution now emits
**933 UART bytes**, identifies the flash, completes the original application
decompressor, and enters the main application at `0xa04e2e05`. The next explicit
boundary is a programmable-timer group-mask write to `0xbd040804`, PC
`0xa0177664`, after **94,177,440 instructions**. The native observer completed
this run in 158.5 seconds on the development machine. The flash is unchanged.

Both SPI timing candidates reach the same application boundary with identical
UART and flash command bytes. `wide-final` takes three more instructions because
of its different transfer timing. Its loaded `.text` (5,123,676 bytes) and
`.rodata` (1,677,772 bytes) match independent extraction byte for byte. Earlier
ten-million-instruction, 901-byte captures end inside the decompressor at their
requested budget, without a hardware fault.

The default run emits **578 UART bytes**, including:

```text
[Boot] Firmware build timestamp: Sat Jan 14, 2017 03:32:35PM
[Boot] ASIC ID (AXON RevA .0 fab0 Marvel)
[Boot] Entering NOS_HW_MCASIC_init
[Boot] Board Support (ROM)           : 0x064501ff (bytepacked)
[Boot] Unmapped Board ID (Compiled in): 0x68bf (bitpacked) M:0x06 F:0x45 N:0x1f
[Boot] Unmapped Board ID (from NVM)  : MISSING!  Tell the EEs
[Boot] Unmapped Board ID (ROM num)   : 0x68bf (bitpacked) M:0x06 F:0x45 N:0x1f
[Boot] Remapped Board ID (table)     : 0x68bf (bitpacked) M:0x06 F:0x45 N:0x1f
```

An explicit serial-panel profile now passes the firmware's original detection
and produces **754 UART bytes**, including these additional lines:

```text
[Boot] NVM powerdown state = 0x00000003
[nos_ui] _powerup_buttons: 0x20002000
[Boot] Load mode = 0x00000004
[Boot] read NOS_NVM_FWUPDATE_STATUS = 0xaa
[Boot] SPI Device 0 :
```

Run it with `--serial-panel-inputs 0x3ffff`, an explicit assumption that all 18
active-low inputs are released. The endpoint models strobe/clock edges, input
sampling, the protocol signature, and feedback from actual transmitted bits.
It is inferred from matching boot and main-firmware drivers; it is not a capture
of the physical printer. After **2,126,053 instructions**, boot reaches the actual
SPI JEDEC-ID command store at `0xbd063814`, PC `0xa000cee4`. With the default
strict SPI model, the transfer stops explicitly because control bit 5
and final-byte/chip-select timing remain unresolved. The last UART line is an
unfinished prefix, not a reported flash-identification failure.
The following space has been written to UART but is still transmitting when
the CPU and virtual clock stop; it is not counted among the 754 completed bytes.

Adding `--spi-profile byte-final` or `--spi-profile wide-final` selects an
explicit candidate serializer, one attached Winbond flash, high undriven MISO,
and an empty-slot memory window. Each candidate clocks actual bytes through the
flash model and adds these lines:

```text
[Boot] SPI Device 0 : W25Q128BV (16 Megabytes) (ID:0x00ef4018)
[Boot] SPI Device 1 : No ROM found (ID:0x00ffffff)
[Boot] SPI Device 2 : No ROM found (ID:0x00ffffff)
```

The longer runs then print `[Boot] calling (*0xa04e2e05)()` and
executes the application until the timer boundary. Timer/IRQ driver evidence
is recorded in [main-entry notes](analysis/main_entry.md); active interrupt
delivery remains unimplemented.

The part name is the firmware's table label for `ef4018`; it does not supersede
the photographed chip marking. Neither candidate is a confirmed AXON register
specification. See the [SPI review](analysis/spi_review.md) and
[storage-path analysis](analysis/storage_path.md) for the unresolved details.

The new status values are explainable: `03` and `aa` are compiled NVM fallbacks,
`20002000` holds two unused button identifiers, and load mode `04` is the default
selection path. They are not additional error messages. The missing board-ID
warning still reflects unavailable EEPROM settings. See the
[UART and boot-path analysis](analysis/boot_progress.md).

The default run keeps the external panel unspecified and stops on its input
read at `0xbd0b1410`, PC `0xa000e2e0`, after **1,633,229 instructions**. With an
explicitly static input, firmware rejects the panel handshake and reaches its
alternate byte engine at `0xbd0b1408`, where completion semantics remain undecoded
(2,083,465 instructions for static-low). Those runs still emit 578 UART bytes.

This remains a partial hardware emulator. Verified dump SHA-256:
`b791436cf9152785fb5d3f6954aa5949d4ae7c7458af0e2031b9a41f5534c496`.

## Run

```powershell
python -m pip install -r requirements.txt
python emulator.py
```

Python 3.14.3 / Unicorn 2.1.4 are verified. The default dump path is beside the
script; an alternate image can be supplied as the first argument.

```powershell
New-Item -ItemType Directory -Force artifacts | Out-Null
python emulator.py --uart-out artifacts/further-default.uart --report artifacts/further-default.json --trace artifacts/further-default.jsonl
python emulator.py --serial-panel-inputs 0x3ffff --uart-out artifacts/serial-spi.uart --report artifacts/serial-spi.json
python emulator.py --serial-panel-inputs 0x3ffff --spi-profile byte-final --max-instructions 10000000 --uart-out artifacts/spi-byte.uart --report artifacts/spi-byte.json
python -m analysis.run_progress --spi-profile byte-final --max-instructions 300000000 --prefix artifacts/deep-boot
python -m analysis.run_progress --native-observer --spi-profile byte-final --max-instructions 300000000 --prefix artifacts/deep-native
python -m analysis.run_progress --native-observer --spi-profile wide-final --max-instructions 100000000 --prefix artifacts/deep-wide --verify-section .text --verify-section .rodata
python emulator.py --blank-eeprom --uart-out artifacts/blank-eeprom-uart.txt
python emulator.py --eeprom path/to/24512-dump.bin
python -m analysis.board_id
python -m analysis.board_id --eeprom path/to/24512-dump.bin
```

The board photo shows a separate ST `24512WP` chip next to the NVM label. Its
64 KiB contents are **not** supplied by the 16 MiB SPI flash dump. By default,
the simulated I2C bus has no EEPROM attached. `--blank-eeprom` explicitly attaches
an erased device filled with `0xff`; `--eeprom` attaches an exact 65,536-byte image.
These options are mutually exclusive. EEPROM writes affect a private in-memory
copy, never the input file. No valid board settings are invented.

The `MISSING!` message is a recoverable firmware diagnostic. Board-ID item 4 lives
at EEPROM offsets `0x0c..0x10`: two big-endian ID bytes, their bitwise-inverted
bytes, and an XOR checksum. Invalid or unavailable data selects the firmware's
`0x001f` default. The boot code then retains the ROM family and uses its fallback
board ID `0x68bf` (`M=6, F=0x45, N=0x1f`). All three support records in this flash
agree on `0x064501ff`; they do not reveal the missing physical unit's board number.
This warning does not itself stop execution. The inspector also implements the
firmware's recovery rules: some damaged checksums remain recoverable from the
complementary copies, so a checksum mismatch alone is not proof of a missing ID.

Other options:

- `--stop first-byte` or `--stop banner`: retain the original UART milestones.
- `--stop budget`: run until an unsupported operation or the budget; the default.
- `--max-instructions 5000000`: bound execution, including polling loops.
- `--timer-step 1`: virtual timer ticks per instruction, not ticks per read.
- `--supply-mv 24000`: simulated external supply voltage, **not a measurement**.
- `--gpio-input PIN=LEVEL`: explicitly drive a GPIO input to 0 or 1; repeat as needed.
- `--panel-input-word 0` (or `0x100`): test a static panel return line; neither represents a responding panel.
- `--serial-panel-inputs 0x3ffff`: attach the inferred serial panel with explicit raw input levels; mutually exclusive with a static input word.
- `--spi-profile byte-final` or `wide-final`: select a candidate serializer and explicit one-flash board wiring; omitted by default.
- `--native-observer`: use optional C accounting and block-validation cache to reduce Python callback overhead; GCC is required on the first run.
- `--no-mcasic`: omit the inferred secondary-controller profile.

UART text goes to stdout; diagnostics go to stderr. Use `--uart-out` for exact
bytes, including LF/CR ordering. Exit status is 0 for an explicitly requested UART
milestone, 1 for a budget/fault/unsupported stop, and 2 for input errors. The current
default front-panel boundary therefore exits with status 1. JSON reports include hashes,
registers, clock, analog/EEPROM profiles, bus transactions, UART text, and the stop
reason. JSONL traces record CP15/MMIO operations and completed UART frames.

The boot UART is configured for **115200 baud**, established by matching its
control word with the main driver's explicit numeric baud selection. For a
physical capture, start with **115200, 8N1**; the framing remains an assumption.
One bit is approximately 8.68 microseconds. This does not identify a physical
TX pad or establish the signal voltage. See [UART baud evidence](analysis/uart_baud.md).

## Hardware model

| Module | Responsibility |
| --- | --- |
| `emulator.py` | ARM/Thumb execution, MPU integration, IRQ/FIQ delivery, bounded runs, trace/report CLI |
| `cpu.py` | CP15 identity/control/TCM/MPU state and cached permission checks |
| `memory.py` | Immutable flash, shared physical DDR, persistent TCM overlays |
| `hardware.py` | Virtual time, PLLs, calibration, UART, timers/IRQs, decoded configuration registers |
| `micci.py` | Timed MICCI2 transactions, broadcast probes, MCASIC (`0x8014`), and Carriage/Printhead ASIC (`0x0801`) |
| `i2c.py` | Timed I2C transfers, negative-address recovery, optional EEPROM |
| `gpio.py` | Output latches, directions, explicit inputs, retained interrupt routing |
| `panel.py` | Clocked serial-panel endpoint (`press_button`/`release_button`/`last_output`) and signal/configuration registers; strict byte-engine boundary |
| `spi.py` | Strict AXON boundary, optional scheduled serializer candidates, and read-only W25Q128JV endpoint |
| `sdio.py` | AXON SDHCI controller (`0xbd0b0000`, IRQ 63) and Broadcom `BCM43143` SDIO 2.0 Wi-Fi card (CCCR/CIS, AXI EROM, 512 KiB SOCRAM, `SDPCM`+`BCDC` Function 2 ioctls & bidirectional Ethernet II RX/TX frame queues) |
| `usb.py` | AXON USB 2.0 High-Speed Device controller (`0xbd09bc00..0xbd09fc3e`, IRQs 23 & 24, 16 endpoints + global SIE/DMA, standard/class `SETUP` & Bulk-OUT/IN helpers) |
| `panel_gui.py` | Interactive Tkinter front-panel GUI, headless widget controller (`FrontPanelUIController`), and `--talk-all` full-system self-test harness |
| `custom_firmware.py` | Pure-Python ARMv5 big-endian assembler (`ArmBeAssembler`), bare-metal ROM builder (`build_custom_firmware_rom`), and Stage-2 container packager (`build_stage2_container_image`) |

Time advances with instructions, independently of register reads. Counter preload
changes its offset without moving device deadlines. PLL lock follows reset and
configuration; SDRAM calibration completes after a delay. UART writes occupy a
transmit slot and clear readiness; bytes are captured after frame completion.
Bus and ADC operations likewise stay busy until scheduled completion. Timing
parameters are deterministic approximations, **not cycle-accurate or calibrated
physical timing**.

The optional native observer still lets Unicorn execute every instruction.
It counts instructions, tracks deadlines, and caches successful block checks in an
8,192-entry (2,048-set, 4-way set-associative) block metadata cache with C-side
CP15 (`MRC`/`MCR p15`) and unprivileged transfer (`LDRT`/`STRT`) decoding.
Every cache hit rechecks current code bytes and CPU privilege mode; MPU/TCM
changes (`c1`/`c5`/`c6`/`c9`) invalidate cached decisions without flushing the
cache on no-op `c7` write-buffer drains. Together with cached MPU region and
access evaluations in `CP15`, cold boot runs at ~1.20 MIPS (3.5x faster) and
main-application + SDIO execution runs at 1.77–2.34 MIPS (up to 26x faster than
the unoptimized 0.09 MIPS baseline). Python handles devices and architectural
checks at their normal observable boundaries. The default Python observer is
retained as the reference. A 10-million-instruction reset comparison produced
identical UART, CPU register values, stop position, and report contents with
both observers. Differential tests also cover alias-written code changes,
privilege changes, partial blocks, faults, and exact UART completion timing.

The helper compiles local `native_observer.c` into ignored `artifacts/native`.
Set `NATIVE_OBSERVER_CC` to the GCC executable if it is not on PATH. It currently
requires a 64-bit host and uses Unicorn 2.1.4's internal Python binding API.
Custom observation hooks must synchronize accounting before reading Python
clock/counter fields; `analysis/run_progress.py` instead reads the native
counters from its separate observation thread without touching the CPU.

CP15 state controls TCM overlays and MPU page permissions. MPU overlap priority
and privileged/user rights are enforced before peripheral side effects. Original
instructions perform decompression and data initialization. Unknown operations
stop explicitly instead of receiving generic zero or successful responses.

### Memory and CPU profile

| Physical storage | CPU-visible mapping |
| --- | --- |
| Original 16 MiB SPI flash | `0x00000000..0x00ffffff`, read-only, shadowed by enabled ITCM |
| One 64 MiB DDR bank | Shared through `0x20000000`, `0x60000000`, `0xa0000000` apertures |
| 8 KiB ITCM | Separate bank at zero, controlled by CP15 enable/load mode |
| 4 KiB DTCM | Same persistent bank follows relocation from `0x10000000` to `0x60000000` |

The photographed Etron EM6AB160 supports 64 MiB capacity. Shared DDR apertures and
64 MiB repetition within their 128 MiB windows are inferences from physical memory
and MPU setup. Aliasing is tested, but the ASIC address decoder is not measured.

The CPU profile is ARM946, big-endian, with configurable TCM capacities and no
caches. Exact core revision, caches, TCM capacity, and reset endian strap are
not established from hardware. The no-cache descriptor matches Unicorn's ARM946
profile; maintenance operations therefore have no dirty cache state to process.
The one exception is the instruction-cache invalidate (`c7, c5, 0`), which
flushes Unicorn's translated blocks so that code rewritten through another DRAM
alias and re-executed at the same address is retranslated.
Byte-order switching is explicitly unsupported. TCM load stores preserve flash
and underlying RAM. Unicorn cannot select separate instruction/data backing;
fetches through DTCM or a TCM load-mode aperture stop explicitly instead of using
the wrong memory.

### Secondary controller and EEPROM

The dump's descriptor selects MCASIC identity `0x8014`, MICCI controller zero,
sub-bus zero, device six. This is the default modeled board profile. Its reset
status/configuration values are documented assumptions; complete bit definitions
are unavailable. ADC selector 15 is an ideal half-scale reference inferred from
calibration code; selector zero measures the configurable supply. Other analog
inputs and unknown registers remain unsupported.

The I2C driver deliberately probes an unconnected address and expects timeout
and reset recovery. The controller model reproduces that behavior for unanswered
addresses. With no EEPROM, firmware follows its failure path. With an erased
EEPROM, it reads actual `0xff` bytes through the controller and independently
finds missing settings. The EEPROM model implements 16-bit addressing, 128-byte
page wrap, delayed programming, write protection, and busy rejection. GPIO 7 is
connected to active-high EEPROM write protection, inferred from the NVM driver's
low/write/high sequence. Protected data bytes NACK and leave the image unchanged;
reads remain available. Programming delay conversion
to timer units remains uncertain and is not shortened to make writes succeed.

### GPIO, front panel, and flash command mode

GPIO output latches and direction changes execute through the original accessors.
Input pins require explicit levels; unknown pull resistors are not assumed.
Interrupt masks and pending-status clears retain state, but signal transitions
do not generate speculative edge events.

The front-panel driver shifts a 64-bit frame through output clock/data bits and
samples a return line. Both firmware stages agree on 18 active-low input bits,
then signature byte `0x35`, then feedback of the first outgoing byte, all LSB
first. The optional endpoint captures actual output bits and latches configured
inputs at GPIO 26's strobe. Reads do not advance the serial position. Main code
also establishes output-latch readback. Reset polarity and edge timing are
explicit model assumptions; the physical implementation is still unidentified.

Static-low and static-high experiments reject the handshake. Its alternate byte
controller polls a completion bit whose relationship to external reception is
unknown. That path still stops explicitly. The serial protocol does not imply
that the alternate byte engine or its MCU commands have been decoded.

`spi.py` implements the photographed Winbond flash's byte protocol: JEDEC
`ef4018`, status registers, read/fast-read, legacy ID, and write-enable state.
The array is immutable. Main-array data does not contain the chip's nonvolatile
status bits, so their factory profile is explicit. This endpoint is tested
independently and connected to reset execution only with an explicit SPI profile.
In `byte-final`, every request clocks one MSB byte, and a cleared hold bit releases
CS afterward. In `wide-final`, a held request with bit 5 clear clocks two MSB
bytes, while the final request releases CS without clocking a byte. Both produce
the same wire bytes for the firmware's observed ID sequences. Completion is
scheduled; status reads do not advance transfers. Unsupported operations stop.

The optional one-flash board profile sets the slot windows to `03c0/0fff/0fff`
and provides a read-only, high-pulled absent-device aperture at
`0x03f00000..0x03ffffff`. Firmware scans this space before deciding that the
other slots contain no ROM. The intervening gap stays unmapped. These reset
windows and the pull are assumptions; unsupported window remapping stops
instead of silently using a stale mapping.

### Carriage ASIC, USB 2.0 device controller, and Broadcom BCM43143 SDIO Wi-Fi

In the main application (`0xa04e2e05` onwards), active IRQ/FIQ delivery, timer
channels, and additional peripherals are modeled:

- **Carriage / Printhead MICCI ASIC (`CarriageAsic`, `0xbd300c00`, bus 1, device 4)**:
  Responds with chip identity `0x0801` (register `0x7d`), Neo status `0x800e`
  (register `0x00`), and pen ADC ready readings (register `0x09`), completing
  1,633 MICCI transactions during main-application initialization alongside
  broadcast/presence probes (`config[0] & 0x4000`).
- **AXON USB 2.0 High-Speed Device Controller (`usb.py`, `0xbd09bc00..0xbd09fc3e`, IRQs 23 & 24)**:
  Implements the 16 endpoint blocks (`0xbd09bc00 + 0x400 * ep`) and global SIE
  block (`0xbd09fc00`), supporting VBUS attach, High-Speed (480 Mbps) bus reset,
  EP0 `SETUP` packets (`GET_DESCRIPTOR`, `SET_ADDRESS`, `SET_CONFIGURATION`,
  IEEE 1284.4 `GET_DEVICE_ID`), and Bulk-OUT / Bulk-IN endpoint DMA channels.
- **AXON SDHCI Controller, Broadcom `BCM43143` SDIO 2.0 Wi-Fi (`sdio.py`, `0xbd0b0000`, IRQ 63), & DMA Checksum Offload (`0xbd0b1000..0xbd0b1010`)**:
  Models `CMD0`, `CMD5`, `CMD3`, `CMD7`, `CMD52`, and `CMD53` (byte and 64-/512-byte
  block DMA modes), Function 0 CCCR/FBR/CIS tuples (`0x02d0:0xa887`), Function 1
  AMBA AXI backplane sliding window (`0x1000a..0x1000c`), the 5-core EROM table
  at `0x18001000` (`ChipCommon`, `SDIOD`, `ARM Cortex-M3`, `SOCRAM`, `D11 MAC`),
  512 KiB of on-chip `SOCRAM`, and the AXON hardware 16-bit Internet checksum
  offload engine at `0xbd0b1000..0xbd0b1010`. During main-application boot, the
  unmodified firmware executes 3,847 SDIO commands, decompresses and uploads the
  374,076-byte Broadcom Cortex-M3 Wi-Fi firmware (`rtecdc_43143.bin`) and 829-byte
  NVRAM table into `SOCRAM`, releases the Cortex-M3 core reset, enables Function 2
  (`0x06`), completes 247 `SDPCM` + `BCDC` Wi-Fi ioctls — configuring and bringing up
  the `HP-Setup<17>DeskJet 2600 series` SoftAP (`cur_etheraddr = 00:04:ea:85:0b:17`,
  `apsta = 1`, `bss = up`, and HP vendor IE `OUI 08:00:09`) — and transmits **61
  live Ethernet II / IPv4 / IPv6 network frames** over SDIO Function 2 (`SDPCM`
  Channel 2), including Gratuitous ARP for `192.168.223.1`, IGMPv2 Membership
  Reports (`224.0.1.60` `hp-device-disc` and `239.255.255.250`), NetBIOS Name
  Service registration (`HP850B17`), IPv6 MLDv1/Router Solicitation/Neighbor
  Solicitation (`fe80::204:eaff:fe82:50b`), DHCPv6 Solicit, LLMNR, WS-Discovery
  SOAP-over-UDP XML `Hello`/`Bye` broadcasts (`http://192.168.223.1:3911/`), and
  mDNS / AirPrint / DNS-SD service announcements (`HP0004EA850B17.local`,
  `_ipps._tcp.local`, `_http._tcp.local`). Bidirectional Function 2 RX injection
  (`inject_ethernet_frame`) also drives live ARP replies, ICMP Echo replies, TCP
  3-way handshakes, and HTTP `GET /DevMgmt/ProductConfigDyn.xml` LEDM XML responses
  from the unmodified Treck TCP/IP + embedded web server stack.

MPU violations and User-mode CP15 accesses stop with diagnostics; ARM exception
delivery remains unimplemented. Aligned LDRT/STRT transfers enforce User
permissions even in a privileged CPU mode; unaligned forms stop explicitly.

## Verification and evidence

The flash's application header describes 133 sections and 27 nonempty scatter
operations. A read-only inspector recovers the main executable and data using
those tables, including all 13 compressed sections. This is an analysis tool;
the emulator still runs the original firmware loader.

```powershell
python -m analysis.firmware_image
python -m analysis.firmware_image --extract-dir artifacts/firmware
python -m analysis.panel_deep_disasm 0xa00e8644 0xa00e8664
```

The [container notes](analysis/firmware_image.md) document offsets, destinations,
hashes, and the application entry `0xa04e2e05`.

The flash also contains a GHS update stream. The offline
[GHS inspector](analysis/ghs_update.md) reconstructs the updated image and verifies
its encoded, base-runtime, and final-runtime checksums independently. Its entry
is `0xa04e3dd9`. This inspector does not populate emulator RAM or replace any
loader instructions. This update path would require substantially more work
than the base image. The observed load-mode-4 run selects the base application's
entry and does not apply the update stream; it reaches that application in
about 94 million instructions.

```powershell
python -m analysis.ghs_update --verify
python -m unittest analysis.ghs_selftest -v
python -m analysis.uart_selftest
```

### UART Shell & UDW Command Self-Tests
All 54 top-level UART0 shell functions and variables (21 functions + 33 variables) are cataloged and tested in [analysis/uart_selftest.py](analysis/uart_selftest.py). In addition, HP's 62-alphabet monoalphabetic substitution cipher (`0xA04EDFB4`) was completely broken, decrypting all 289 cipher-obfuscated commands across 206 subsystem tables (1,520 total UDW commands). Interactive self-testing executes 64 diagnostic commands (including `js.jobs_status`, `js.job_list`, `jm_reports.supp_report`, `pe_seq.show_seq_tree`, `bio.*`, `bdid.*`, `ui_lite.*`, `shm.msecs`, `mem.list_pools`, `hw.get_clk_rate`, and `micci2.get_errors`) in ~2.7s across 23.8M instructions with 0 faults:
```powershell
python -m analysis.uart_selftest
```

### Custom bootloader + self-updating application (`fw/`)
A two-level replacement firmware that keeps HP stage-1 and boots through its container header: a **base bootloader** at flash `0x022000` that software can never overwrite (the SPI driver refuses writes below `0x040000`) and an **application** in two 512 KiB A/B slots with CRC-checked headers, selected by sequence number. Both levels have a UART shell (115200 8N1) with timestamped logging, memory/flash inspection, and `update`, which receives a new application image over XMODEM-1K, verifies it and programs the inactive slot. `python -m fw.build` produces `build/hpdj_flash.bin` for the CH341 plus `bl.img`/`app.img`; `fw/harness.py` and `fw/emu_console.py` drive the result in the emulator (which now mirrors SPI programs/erases into the memory-mapped flash), and `fw/tool.py` does the serial update on the real printer. Design, layout, flashing procedure and the open hardware questions are in [fw/README.md](fw/README.md).
```powershell
python -m fw.build
python -m unittest tests.test_fw_bootloader -v
python -m fw.emu_console build/hpdj_flash.bin
python -m fw.tool update --port COM5 build/app.img
```

### Freestanding C Compiler Setup for ARMv5 Big-Endian
A full freestanding C compilation pipeline targeting `ARM946E-S` big-endian (`-target armeb-freestanding-eabi -mcpu=arm946e_s`) is provided via `zig cc` (or `clang`/`arm-none-eabi-gcc`).
- [hardware.h](hardware.h): Bare-metal MMIO register definitions for UART0 (`0xBD0B3C00`), Pad-Mux (`0xBD060800`), UART Baud/FIFO (`0xBD0B4C00`), GPIO (`0xBD0B2000`), 64-bit shift-register front panel (`0xBD0B1400`), dual-stage watchdog (`0xBD040400`), and DDR DRAM mailbox (`0x20000100`), with `uart_init()` and `uart_puthex()` inline helpers.
- [sample_firmware.c](sample_firmware.c): Bare-metal freestanding sample C firmware that prints "Hello World from Freestanding C Sample Firmware!", drives GPIO LEDs, clocks the front-panel shift register to sample the `0x35` hardware signature, writes mailbox status `0xC0DE0035`, transmits formatted diagnostic banners over UART0, and hosts an interactive UART CLI (`help`, `ping`, `buttons`, `leds`, `mailbox`, `echo <msg>`, `reset`).
- [firmware.c](firmware.c): Freestanding bare-metal C payload supporting both Stage-2 container execution and standalone reset vector booting.
- [build_firmware.py](build_firmware.py): Unified build toolchain script that compiles freestanding C source to ELF/binary, generates standalone flash and Stage-2 container images, and executes automated emulator smoke tests:
```powershell
python build_firmware.py --all
python build_firmware.py --source sample_firmware.c --standalone --run
python build_firmware.py --source sample_firmware.c --stage2 --run
python custom_firmware.py --compile-c-standalone --c-source sample_firmware.c
python custom_firmware.py --compile-c --c-source sample_firmware.c
python -m unittest tests.test_firmware_image.CFirmwareCompilationTests -v
```

### RAM Upload & Hello World Command Self-Tests
Uploading and running Hello World in RAM is supported and verified via two complementary mechanisms:
1. **Interactive Machine Code RAM Upload & Execution**:
   - Bootstrap developer memory-write commands over UART (`0x{addr} = 0x{word}`).
   - Upload Thumb machine code stub and string payload directly into DDR DRAM (`0x20010000` / `0xA0010001`).
   - Verify RAM contents via memory display (`d 0x20010000 8 4`) and UDW hex dump (`udws "udw.dm 0x20010000 4 4;"`).
   - Register dynamic shell command `hello` via `func_call 0xa03cdb07`.
   - Execute uploaded routine via shell command `hello` and `func_call 0xa0010001`, returning `42 (0x2a)`.
2. **RAM Filesystem (`/ram`) Upload & Verification**:
   - Upload raw ASCII stream: `udws "ram.file_write2 /ram/hello.txt 12 Hello World!;"` or `panel_gui.py --upload-ram /ram/hello.txt "Hello World!"`.
   - Upload hex-encoded byte stream: `udws "ram.file_write /ram/hello_hex.txt 12 48656c6c6f20576f726c6421;"`.
   - Verify directory listing with size: `ls /ram` or `panel_gui.py --list-ram`.
   - Read back file over UART console: `udws "ustd.cat /ram/hello.txt;"` or `panel_gui.py --cat-ram /ram/hello.txt` -> prints `Hello World!`.
   - Copy file within RAM filesystem: `cp /ram/hello.txt /tmp/hello_copied.txt` and verify with `ll /tmp`.
   - Verify physical backing in guest DDR DRAM (`0x20000000..0x22000000`).
```powershell
python -m analysis.ram_selftest
python panel_gui.py --run-ram-hello
python panel_gui.py --upload-ram /ram/test.txt "Hello from CLI"
python panel_gui.py --list-ram /ram
python -m unittest tests.test_ram_upload -v
python -m unittest analysis.uart_selftest.UartCommandSelfTest.test_upload_hello_world_to_ram -v
```

### Printing Pipeline & Emulation ("Does it print? YES!")
The emulator executes the real, unpatched firmware print pipeline:
1. Triggering `jm_reports.print_report 974;` (Diagnostic Print Report 974) schedules task `VP03` (`JM_REPORTS`, TCB `0x21273524`).
2. `VP03` runs through `pe->start_job` (`0xa021ffb0` -> `0xa0303440`) and `pe->start_page` (`0xa0220054` -> `0xa03035f0`).
3. It renders swaths at 600 DPI (page dimensions 4800x6404) and feeds raster strips into the print engine via `pe->send_strip` (`0xa02200f8..0xa022012c` -> `0xa0303830`).
4. Strips are transferred to `tSweepMgr` (`0x212e76ac`), which queues print sweep passes and motor move commands to `tMech` (`0x212df60c`).
5. `tMech` sweeps carriage Motor 0 and advances line-feed Motor 1 across thousands of execution runs.
6. The entire pipeline is exposed in `FrontPanelUIController.trigger_print_report()`, CLI option `python panel_gui.py --print-report 974`, Tk GUI button `Print Report 974`, and tested under both pure Python and C native observers with 100% parity:
```powershell
python panel_gui.py --print-report 974
python -m unittest tests.test_printing -v
```

### Ink Test Check & Pen Diagnostics
The printer's ink subsystem is thoroughly emulated and tested:
- `CarriageAsic` companion hardware chip on MICCI controller 1, bus 1, device 4 (`0x0801`), modeling pen presence (register 0x09 bit 8 `0x8100` vs `0x8000`), ink level percentages, and bar gauges.
- Firmware IDS (Ink Delivery System) status table at `0x20DF37B4` synchronized with `apply_to_firmware()`. Missing cartridges trigger `ui_lite` status `Error: 0x10, Pen: 0x03` and ink alert indicators. Installed cartridges update gauge bars `[C:###] [K:###]`.
- Live UDW pen diagnostics: `pen.etest` executes electronic nozzle checks, `pen.get_ambient_temp` reads thermal sensors (25.0°C), `pen.get_dotcount` tracks droplet usage, and `pen.get_pen_family_name_string` confirms cartridge family `Dolmen` (HP 304/65 family).
- Live servicing: Clean Test (Report 926) triggers the real firmware printhead purge routine.
```powershell
python -m unittest tests.test_ink -v
```

### Key Press Emulation & Self-Tests
Full physical key press and chord emulation across all 7 physical keys and 8 chords:
- Shift-register active-low input bits for `power` (bit 1), `resume` (bit 2), `wireless` (bit 3), `cancel` (bit 6), `info` (bit 7), `copy_color` (bit 8), and `copy_black` (bit 9).
- Multi-button chords (`wireless_cancel`, `wireless_resume`, `copy_color_resume`, etc.) mapped to hardware bit patterns and firmware chord words.
- Live firmware key recorder: entering `fp.test_enter;` and dumping keys via `fp.test_dump_keys;` verifies the real firmware captures keys and chords (`cancel`, `copy_color`, `power`, `wireless_cancel`).
- Service combos: holding `power` while tapping `copy_color` (+100), `resume` (+10), and `copy_black` (+1) updates service registers at `0x2093C060` in firmware DRAM.
- Interactive copy button reactions: tapping `copy_color` button increments the 7-segment display counter.
```powershell
python -m unittest tests.test_key_presses -v
```

### USB Computer Visibility & Plug-and-Play Emulation
Computer hosts (Windows Plug and Play, Linux CUPS, macOS) can fully see and communicate with the emulated printer over USB 2.0 High-Speed:
- USB Bus Reset negotiation and address assignment (`SET_ADDRESS 5`).
- Standard Device Descriptor query returning Vendor ID `0x03F0` (HP Inc.) and Product ID `0x0053` (HP DeskJet 2600 Series).
- Configuration Descriptor configuring 4 interfaces: Interface 1 (Printer Class `07/01/02`), Interface 3 (LEDM REST HTTP), Interface 2 (Scanner/Imaging), and Interface 0 (Vendor).
- Product string query returning `"DeskJet 2600 series"`.
- IEEE 1284 Device ID query over USB class request `GET_DEVICE_ID` returning the exact identification string Windows Plug-and-Play uses to match and install the printer driver:
  `MFG:HP;MDL:DeskJet 2600 series;CMD:PCL3GUI,PJL,Automatic,JPEG,PCLM,AppleRaster,PWGRaster,DW-PCL,802.11,DESKJET,DYN;CLS:PRINTER;DES:V1N01A;CID:HPDeskjet_P976D;...`
- Bulk-OUT DMA transfer on Endpoint 8 streaming PJL/PCL print jobs (`@PJL INFO ID`).
- Exposed via CLI `python panel_gui.py --usb-query`, GUI button `USB 2.0 Query`, and `--talk-all`:
```powershell
python panel_gui.py --usb-query
python -m unittest tests.test_usb -v
```

```powershell
python -m unittest discover -s tests -v
python analysis/uart_decode.py
python -m analysis.next_disasm 0xa0009310 0xa0009340
```

All **240 regression tests passed** (including all 5 GHS update self-tests, all 54/54 UART0 shell functions & variables, decryption of all 289/289 cipher-obfuscated UDW commands across 206 tables / 1,520 UDW commands, live execution of 118 interactive shell/UDW commands, RAM upload self-tests covering raw and hex `/ram` file uploads and interactive Thumb machine code DDR RAM upload & execution with physical DRAM memory backing, C compiler setup & bare-metal standalone ROM + Stage-2 container custom ARMv5 C sample firmware boots with interactive CLI, end-to-end print pipeline emulation with diagnostic report 974 generation, dedicated ink level and pen electronic tests, full physical key press & chord emulation with live firmware key dumps, and complete USB 2.0 Plug-and-Play computer device discovery with IEEE 1284 Device ID queries). They cover real reset execution and exact UART bytes, transmit timing, independent
decompression, unchanged flash, DDR aliases, TCM relocation/load mode, MPU rights,
peripheral transitions, MICCI/ADC/CarriageAsic, I2C/EEPROM write protection, GPIO, clocked panel
frames and button helpers, strict and candidate SPI transfers, flash commands, USB 2.0 device enumeration/DMA,
SDIO host and Broadcom BCM43143 backplane/Wi-Fi ioctls/bidirectional Ethernet RX/TX frames, DMA Internet checksum offload,
board-ID corruption/recovery rules, and bounded extraction of every declared compressed section.

Reverse engineering: [first UART](analysis/uart_notes.md) and
[later boot hardware](analysis/boot_hardware.md), [board ID](analysis/board_id.md),
[GPIO](analysis/gpio.md), [early front panel](analysis/front_panel.md), and
[panel protocol from both firmware stages](analysis/panel_deep.md). These distinguish observations,
inferred register meanings, and unverified board-profile assumptions.

Primary references:

- [ARM946E-S manual](https://documentation-service.arm.com/static/5e8e3ee588295d1e18d3aa82): CP15, TCM, MPU.
- [ARM architecture manual](https://documentation-service.arm.com/static/5f8dacc8f86e16515cdb865a): endian startup and instruction behavior.
- [Etron EM6AB160 family](https://etron.com/wp-content/uploads/2022/04/EM6AB160TSHWKE_Rev-1.1.pdf): DDR geometry; newer family revision than the pictured part.
- [ST M24512-W](https://www.st.com/en/memories/m24512-w.html): capacity, 128-byte pages, programming specifications.
- [Winbond W25Q128JV](https://www.winbond.com/resource-files/w25q128jv%20revg%2004082019%20plus.pdf): flash byte protocol and ordering/ID profile.
- [Unicorn API](https://github.com/unicorn-engine/unicorn/blob/2.1.4/include/unicorn/unicorn.h).
