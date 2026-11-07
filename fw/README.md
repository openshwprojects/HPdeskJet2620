# HPDJ bootloader + application firmware

Custom two-level firmware for the owner's HP DeskJet 2620 (AXON ASIC,
ARM946E-S, big-endian, 16 MiB Winbond W25Q128JV SPI flash). It is meant to be
programmed once with an external SPI programmer (CH341) and then maintained
over the UART: a **base bootloader** that software can never overwrite, and an
**application** in two A/B flash slots that updates itself over XMODEM.

Everything here has been verified only in the emulator (`emulator.py`), which
runs HP's real stage-1 boot ROM. The hardware unknowns are listed at the end;
read them before flashing.

```
python -m fw.build                                   # build/bl.img, build/app.img, build/hpdj_flash.bin
python -m unittest tests.test_fw_bootloader -v       # image format, XMODEM, 8 emulator scenarios (~2 min)
python -m fw.emu_console build/hpdj_flash.bin        # type at the emulated printer's UART shell
python -m fw.harness build/hpdj_flash.bin --cmd slots --cmd update --send build/v2/app.img --cmd slots
python -m fw.tool update --port COM5 build/app.img   # real printer: XMODEM update over the serial port
python -m fw.tool ota build/app.img --host deskjet.local   # real printer: update over Wi-Fi (HTTP), reboot
python -m fw.tool update --port COM42 build/app.img --reboot   # over the printer's own USB port
```

`python -m fw.build` (the command line) makes printer builds: the application
joins the default Wi-Fi network a few seconds after power-on, so the web page
and `fw.tool ota` work without the serial port, and starts the USB serial
console. `--no-wifi-join` / `--no-usb-console` leave those out; `build_all()`
and the tests leave both out by default.

Toolchain: `arm-none-eabi-gcc` (found automatically from `ARM_CC`, `PATH`,
or the Arduino `arm-none-eabi-gcc` package; GCC 7.2.1 is what was used).
`-mbig-endian -mcpu=arm946e-s -ffreestanding -nostdlib`; the toolchain's
libgcc is little-endian only, so `fw/common/libmini.c` and `aeabi.S` supply
`memcpy`/`memset`/division helpers. Compiles run in parallel with a
content-hashed object cache in `build/.objcache`: a cold build of both images
takes ~10 s on this machine, an application rebuild after an edit ~3 s
(`--no-bl` skips the bootloader, which an app-only `.img` does not need).

## Boot chain

```
power on
  └─ HP stage-1 ROM (flash 0x000000..0x021FFF, untouched)
       PLLs, 64 MiB DDR, UART0 115200 8N1, SPI discovery, MPU + caches on
       reads the container header at 0x020030 (patched by fw/image.py)
       raw-copies BL payload flash 0x022040 -> DRAM 0xA0020000, calls it
  └─ HPDJ bootloader (fw/bl)
       validates slot A and slot B headers + CRC-32
       picks the valid slot with the highest sequence number
       1 s "press any key" window, then copies the app to 0xA0100000 and jumps
       any key / no valid slot / request from the app -> recovery shell "BL> "
  └─ HPDJ application (fw/app)  shell "app> ", UART log with timestamps
```

Stage-1 hands over in SVC/ARM state with interrupts *enabled* and three FIQ
sources unmasked (bus-error monitor, both watchdog stages); the first
instruction of `start.S` masks IRQ/FIQ. The MPU programmed by stage-1 is kept:
the `0xA0xxxxxx` DRAM alias is read-only for data and used for code,
`0x20xxxxxx` is the writable alias used for `.data`/`.bss`/stacks. Stage-1's
own state (exception handlers, their stacks, the relocated 4 KiB DTCM) lives
in the first 128 KiB of DRAM, which the shells refuse to write. Do not move
`BL_LOAD_ADDR`: stage-1 silently skips a scatter copy whose destination
overlaps its protect table (26 ranges at flash 0x200A0); 0xA0020000 is clear. Code copied into DRAM is written through the
`0x20` alias and made executable with ARM946 cache maintenance
(`fw/common/cache.h`). The first 8 KiB of the flash window are shadowed by the
ITCM, which is why the flash self-test probes start at 0x010000.

## Flash layout (`fw/common/layout.h`, parsed by `fw/image.py`)

| Offset     | Size    | Content | Written by |
|------------|---------|---------|------------|
| 0x000000   | 128 KiB | HP stage-1 boot ROM | CH341 only (kept from dump.bin) |
| 0x020030   | 0x6C    | HP container header, repointed at the BL | CH341 only |
| 0x022000   | 120 KiB | BL image: 64 B header + payload | CH341 only |
| 0x040000   | 512 KiB | application slot A: header + payload | app/BL `update`, CH341 |
| 0x0C0000   | 512 KiB | application slot B | app/BL `update` |
| 0x140000.. |         | unused, original dump bytes kept | — |
| 0xC93000   | 4 KiB   | erased: GHS update stream header (was `7F474853` at 0xC93E98) | builder |
| 0xFFF000   | 4 KiB   | erased: HP footer (was `3CA55A7D` at 0xFFFF94) | builder |

`FLASH_PROTECT_END = 0x040000`: the SPI driver refuses every erase/program
below it, in both the bootloader and the application. That is what makes the
base level non-updatable from software; only the external programmer can
change stage-1 or the BL. (The W25Q128's block-protect bits are not touched;
they could add a hardware lock later.)

The two erased sectors matter on the real unit: its EEPROM says "update
applied", so stage-1 would otherwise locate the HP footer, overlay HP's GHS
patch stream onto RAM and jump to HP's entry instead of ours. With the footer
gone stage-1 prints `Hdr is no good-CS0x00; 0x00ffff94.` and falls back to the
container header, which is what the emulator shows with the factory EEPROM
profile. `--keep-hp-update` disables the erasure.

### Image header (64 bytes, big-endian, `fw/common/image.h`)

`magic "HPDJ"`, header version, type (1 BL / 2 APP), version
`major.minor.patch`, **sequence** (the BL boots the valid slot with the
highest sequence; ties prefer A), payload size, load address, entry address,
payload CRC-32, build time, 20-byte name, header CRC-32. `fw/image.py`
creates and validates these; the C side (`image.c`) applies the same rules plus
load-address/entry constraints.

### RAM map

| Region | BL | App |
|--------|----|-----|
| code + rodata + `.data` load image | 0xA0020000 (max 120 KiB − 64) | 0xA0100000 (max 512 KiB − 64) |
| `.data`/`.bss` | 0x20040000 (128 KiB) | 0x20180000 (256 KiB) |
| stack top | 0x20080000 | 0x20200000 |
| XMODEM staging buffer | 0x20400000 (512 KiB), shared | |
| boot-info block (BL → app) | 0x2001F000 | |

## Shells

Both shells share `help`, `slots`, `update [a|b]`, `verify [a|b]`,
`erase a|b`, `flash id|status|read <off> <len>|selftest|mode [byte|wide]`,
`md`, `mw`, `crc`, `sysinfo`, `uptime`, `reboot`. The BL adds `info`,
`boot [a|b]` and `run` (receive an image over XMODEM and execute it from RAM
without writing flash: the fastest edit-build-try cycle, `python -m fw.tool
run --port COMx app.img`). The app adds `version`, `log level 0..3 | heartbeat
<ms> | test`, `led power|wifi on|off`, `gpio dump|get|set`, `panel`, `echo`,
`sleep`, `bl` (reloads the BL from flash and re-enters its shell without a
reset), and the peripheral test commands prepared from the hardware analysis:

| Command | Peripheral | What it exercises |
|---------|------------|-------------------|
| `i2c scan`, `i2c read <addr7> <sub> [len] [16]` | I2C master 0xBD065800 | address probing with HP's timeout/reset recovery; 8/16-bit sub-addresses |
| `eeprom [off] [len]` | ST 24512 NVM at 0x50 | read-only dump (board ID at 0x0c..0x10) |
| `asic` | MICCI buses | MCASIC identity 0x8014 + status, carriage ASIC 0x0801, Neo status, pen status |
| `micci read/write <ctrl> <bus> <dev> <reg> [val]` | MICCI 0xBD300800/0xBD300C00 | raw register access (writes can power the engine down: careful) |
| `adc <sel> [mode]` | MCASIC ADC | selector 15 = internal half-scale reference, 0 = 24 V supply in mV |
| `lcd <char> [wifi 0-5] [color 0-3] [black 0-3] [icon...]` | front-panel shift register | 7-segment digit, Wi-Fi/ink bars, named icons; samples buttons |
| `lcd walk [first last]` | front-panel shift register | lights one bit at a time while the digit blinks its number; any button = next, cancel = back |
| `buttons [wait [ms] \| watch [s]]` | front-panel inputs | decodes power/resume/wireless/cancel/info/copy buttons by name |
| `motor [show] \| off \| init \| pulse <a\|b\|c> <fwd\|rev> <pwm> <ms>` | MCASIC DC-motor bridges | reads the motor registers or sends one limited pulse (pwm <= 600 of 1023, <= 250 ms), then switches all motors off; `init` writes the MCASIC registers HP's running firmware had set (`0x20/0x21/0x30/0x38..0x3a`), which unlocks all three motors: A (paper feed), B (printhead carriage) and C (scanner), each confirmed on the printer; see `analysis/motors.md` |
| `motor pos \| home <b\|c> [fwd\|rev] [pwm] \| range <b\|c> ... \| goto <b\|c> <pos> [pwm] \| zero <b\|c>` | encoders of motors B and C | positions in encoder counts (B: SoC channel 10, C: channel 7; A has no encoder found); every drive stops itself when the encoder shows no movement (end stop, blocked) and the same direction is refused until another direction or a stop; `home` runs to an end stop and calls it 0, `range` measures the travel to the other end, `goto` moves closed-loop to a position |
| `motorsAndButtonsTest [pwm]` | MCASIC motors + front-panel buttons | interactive sub-shell (`mbt> `, `exit` to quit) that initializes the MCASIC bridges and maps button holds directly to Motor A (`Cancel`/`Resume` = paper rollers fwd/rev), Motor B (`Wireless`/`Info` = printhead carriage fwd/rev) and Motor C (`Black`/`Color Copy`), with `Power` cycling PWM presets |
| `wifi [status] \| probe \| fw \| scan \| off` | BCM43143 SDIO Wi-Fi module | `probe`: power cycle, SD host, SDIO enumeration, chip ID (`0x1702a887`); `fw`: loads HP's chip firmware and NVRAM from flash, boots the chip, prints its version and MAC; `scan`: lists nearby networks; see `analysis/wifi.md` |
| `wifi join <ssid> [pass] \| ap [ssid] [pass\|-] [ch] \| leave \| auto [on\|off] \| rssi` | Wi-Fi link | a lost link is rejoined in the background (3 s, then the wait doubles up to 60 s; the shell stays usable); `wifi auto off` stops that, `leave`/`ap`/`off` end it; joins an open or WPA2-PSK network (handshake by the chip; WPA2 join + DHCP confirmed on the printer; bare `join` uses the built-in default network) and asks DHCP for an address; or runs the printer's own access point (192.168.4.1, DHCP server); names or passphrases with spaces go in double quotes (the shell keeps quoted arguments together); `scan` marks networks `[secured]`/`[open]`; see `analysis/wifi.md` |
| `net [status] \| ip <addr> <mask> [gw] \| dhcp \| name <host>`, `ping <ip> [count]` | IPv4 over Wi-Fi | address, ARP table, DHCP state, static address; ICMP echo (the printer answers pings too); mDNS: the printer answers for `deskjet.local` (`net name` changes it) |
| `server [status] \| on \| off \| port <p> \| init \| stop \| token <t>` | HTTP server, port 80 | page `/`: motors (hold/pulse, home, range, go to position), sensors, panel LCD, web shell, log, application upload; JSON API: `/api/status`, `/api/motor?act=init\|stop\|move\|home\|range\|goto`, `/api/lcd`, `/api/log?from=N`, `/api/cmd?token=..&c=<command>` (no interactive commands), `/api/reboot?token=..`, `POST /api/update?token=..[&reboot=1]` (image into the other slot, `fw.tool ota`); the token defaults to `hpdj2620` (`server token` changes it until the next boot, `-DWEB_TOKEN` at build time) |
| `ui [status] \| on \| off` | front-panel menu | Wireless = scan (digit = networks, bars = best signal), hold 2 s = access point on/off, Info = IP address digit by digit, Cancel = Wi-Fi off, Resume = display test, Power = soft off/on; `!` while the door is open; `lcd`/`panel`/`buttons` pause it |
| `sensors [watch [s]]` | GPIO inputs | door (GPIO 8, low = open) and input-tray IR sensors (GPIO 1..3) as HP's firmware maps them; see `analysis/sensors.md` |
| `usb [status] \| on \| off \| uart on\|off` | USB device port | the shell as a USB serial port (CDC-ACM, 1209:0001; Windows 10 binds its own driver), working on the printer at high speed and started at power-on in printer builds (`--no-usb-console` leaves that out); everything the UART reads also takes USB keys, so `python -m fw.tool update --port <USB COM> img --reboot` works over it (127 KB in ~7 s); `usb uart off` stops the copy to the 115200 UART (output ~6x faster); see `analysis/usb_console.md` |
| `irq [status] \| tick <hz> [s] \| off` | interrupt controller + timer channel | first hardware interrupt: a timer tick (channel 7, INTC source 18) for a bounded time, own IRQ handler via the vector literal at 0x38, everything restored afterwards; 2000 ticks in 2 s at 1000 Hz on the printer; `irq demo [s]` blinks the power and Wi-Fi LEDs from the interrupt and counts seconds on the panel while the main loop goes blind for 2 s; see `analysis/interrupts.md` |
| `memtest [addr] [len]` | DDR | pattern test of a DRAM range (writable aliases only) |

All of them run against the emulator's models (`tests/test_fw_bootloader.py`,
`test_peripheral_test_commands`; the Wi-Fi link in `tests/test_fw_network.py`
with the virtual network `wifi_net.py`, the panel menu, sensors and
`motor init` in `tests/test_fw_panel_menu.py`, the USB console in
`tests/test_fw_usb_console.py`), so the register sequences match what HP's own
drivers do; what they return on the real printer is the interesting part.

Upload speed: XMODEM-1K at 115200 8N1 moves about 11 KB/s, so a 20 KiB app
takes ~2 s plus ~0.3 s of erase/program, a full 512 KiB slot ~50 s + ~7 s.
HP's UART driver only has baud codes up to 115200, so this is the ceiling over
the serial console; `run` skips the flash step entirely.

`update` receives a `*.img` over XMODEM-CRC (128 B or 1 KiB blocks) into the
staging buffer, checks header and payload CRC, then erases, programs and
verifies the target slot (the inactive slot by default). Any terminal with
XMODEM-1K works; `python -m fw.tool update --port COMx build/app.img` does the
whole exchange, and `--reboot` types `reboot` afterwards.

`reboot` prints `[reboot] arming watchdog, resetting...`, arms the hardware
watchdog the way HP's boot code does, and spins. The emulator does not model
the watchdog reset (its expiry only raises an IRQ, which the firmware keeps
masked), so the emulator harness treats the marker as a power cycle.

## Writing your own application (DIY / educational use)

The bootloader is the stable part; the application is yours to replace as
often as you like. The development loop, after the one-time CH341 flash:

```
# 1. edit fw/app/*.c (or your own directory, see fw/examples/hello)
python -m fw.build --no-flash --out build/dev --app-version 0.2.0
# 2. push it over the UART and start it, no reset or power cycle needed
python -m fw.tool update --port COM5 build/dev/app.img --run
# 3. or try it in the emulator first (your real slot layout, real bootloader)
python -m fw.harness build/hpdj_flash.bin --cmd update --send build/dev/app.img --cmd bl --cmd boot --cmd help
python -m fw.emu_console build/hpdj_flash.bin      # then type: update  and  ~send build/dev/app.img
```

How it fits together:

- **Sequence numbers.** The bootloader boots the valid slot with the highest
  sequence. The builder uses the build time as the sequence by default, so a
  rebuilt app always wins; `--app-seq N` overrides it. Uploading an older
  image prints a warning and stays selectable with `boot a|b` in the BL shell.
- **Slots alternate.** `update` always writes the slot you are *not* running
  from, so the previous build stays available: `bl`, then `boot a` or `boot b`.
- **Your sources.** Every `.c`/`.S` file in the app directory and in
  `fw/common` is compiled; `--app-src DIR` builds from another directory
  (`fw/examples/hello` is a 100-line starting point with its own `hello` and
  `blink` commands). Add commands to the `cli_cmd_t` table, keep
  `COMMON_CMD_ENTRIES` to retain `update`, `slots`, `flash`, `md` and friends.
- **What you can use.** `uart_printf`, `LOGI/LOGW/LOGE/LOGD` with timestamps,
  `timer_*` (ms/us delays, uptime), the register macros in `hw.h`
  (GPIO, panel, SPI, timers), `spi_flash.h` (reads anywhere, writes above
  0x040000), `cli.h`, `crc.h`, `fmt.h` (`snprintf`, `parse_u32`). No heap, no
  interrupts, no libc beyond `libmini.c`. Code size limit 512 KiB − 64, data
  256 KiB, stack 256 KiB (see `layout.h`).
- **Common code changes** take effect in the next app build; the flashed
  bootloader keeps its own copy, so you never need the CH341 again unless you
  change the bootloader itself (`fw/bl`, then `python -m fw.build` and reflash
  `hpdj_flash.bin`).
- **If your app crashes before its shell comes up:** power on and press a key
  during the 1 s countdown (after HP's `[Boot] ASIC ID` line), then `update`,
  `run` or `erase a|b` from the BL shell. There are no exception handlers of
  our own; a fault lands in HP's stage-1 vectors, which print `*** ASSERT` and
  arm the watchdog. If that reset works, a valid-but-crashing app would loop;
  the bootloader counts automatic starts that never reported themselves alive
  (`boot_attempts_mark_alive()`, called by `fw/app` and the hello example once
  their shell is up) and after 3 of them stops autobooting and waits in the
  shell. Keep that call in your own apps.
- **Flash writes** are only ever accepted inside the two slots
  (0x040000..0x13FFFF) and only once the SPI serializer mode has proven
  itself by reading the chip's JEDEC id (or was forced with `flash mode`).
  Ctrl-C aborts a waiting XMODEM receiver.
- **Inspecting a build:** `build/<dir>/app.elf` has symbols (`-g`),
  `app.map` the final layout; `arm-none-eabi-objdump -d build/dev/app.elf`
  shows the generated code.

## Emulator testing

`fw/harness.py` (`VirtualPrinter`) boots an image with the writable SPI flash
model (`spi_writable=True`), feeds UART input reactively, serves XMODEM
transfers from `fw/xmodem.py`, and can `power_cycle()` into whatever the
firmware wrote. `spi_profile='wide-final'` runs the other controller
hypothesis (the firmware must then be switched with `flash mode wide`);
`uart_frame_ticks=347` gives real 115200 frame timing so the BL's timer
calibration can be checked. The emulator now treats the guest's
"invalidate I-cache" operation (`mcr p15,0,rX,c7,c5,0`) as a flush of
Unicorn's translated-code cache: without it, code replaced in DRAM through
the 0x20 alias and re-executed at the same 0xA0 address (exactly what
`update` + `bl` + `boot` does) could keep running stale instruction words. Reading an unconfigured GPIO input (`gpio get`)
faults in the strict emulator model; on hardware it simply reads the pin. `emulator.py` now mirrors committed SPI programs/erases into
the CPU's memory-mapped flash window (one physical chip), so the BL sees a
slot the app just wrote within the same run. `tests/test_fw_bootloader.py`
covers: stage-1 → BL → app autoboot (through the real 1 s delay in virtual
time), all shell commands, the protect boundary, self-update → `bl` → `boot`
→ new version → `reboot` marker → power cycle, BL break-in and recovery
update with 128-byte blocks, missing/corrupt slots, and a corrupted XMODEM
block being NAKed and retransmitted.

## Flashing with the CH341

`dump_helloWorld_v3.bin` (2026-11-05, SHA-256 `5e29ffbd…`) is the current
recovery image: byte for byte `dump_helloWorld.bin` (HP stage-1, the same
bootloader that has booted on the printer since 2026-11-04, slot B with the
first full application), except slot A, which holds the application of
2026-11-05 evening (seq 1793914304: Wi-Fi join at power-on, web page and
HTTP update, USB serial console, `irq demo`). It boots slot A. It was made by
writing `build/irq/app.img` into slot A of `dump_helloWorld.bin` with
`fw.image.write_slot`, deliberately keeping the proven bootloader rather than
building a new one; checked in the emulator through HP's stage-1. On
2026-11-07 the default Wi-Fi name and passphrase in slot A were replaced with
`your-network` / `your-passphrase` (same lengths, header and payload CRCs
recomputed), so this image no longer joins a network on its own; set the
real ones in `fw/app/wifi_secrets.h` (copy `wifi_secrets.example.h`) for
your own builds.

The ready-made image `dump_helloWorld.bin` in the repository root is HP's
stage-1 from `dump.bin`, our bootloader, the hello-world example in slot A
(boots by default: `hello> `) and the full application in slot B (`bl`, then
`boot b` gives `app> ` with every test command). It was made with

```
python -m fw.build --app-src fw/examples/hello --app-version 0.1.0 --out build/helloWorld --slot-b build/app.img --flash-out dump_helloWorld.bin
```

(`--slot-b` places an existing `.img` in the other slot, `--flash-out` names
the 16 MiB file). `python -m fw.build` alone gives `build/hpdj_flash.bin` with
only the full application. Either file is flashed the same way:

1. `python -m fw.build` — read `build/manifest.json` (SHA-256 of the image).
2. Keep `dump.bin`; it is the full restore image.
3. Program `build/hpdj_flash.bin` (exactly 16 MiB) to the W25Q128JV, then
   read the chip back and compare it with the file before powering the printer.
4. Connect the UART (115200 8N1) and power on. Expected output: HP's
   `[Boot] ...` lines ending in `[Boot] calling (*0xa0020000)()`, then
   `==== HPDJ bootloader v1.0.0 ...` with the project credit line
   (`HP DeskJet 2620 custom firmware by Elektroda (OpenSHWProjects) - <github url>`,
   from `fw/common/branding.h`), a `timer: N.NN ticks/us measured against
   UART` line, `flash id ef4018`, the slot table, and after 1 s the
   application banner and `app> `.
5. First checks at `app> ` (all read-only): `flash selftest` (must print
   `passed`; if it reports mismatches or a wrong JEDEC id, type `flash mode
   wide` and repeat), `version`, `uptime` (the measured ticks/ms), `sysinfo`,
   `log test`, `led wifi off` / `on`.
6. Bootloader shell: `bl` from the app, or a key during the 1 s countdown.
   Press it after HP's `[Boot] ASIC ID` line (stage-1 flushes the UART input
   twice before that; holding a key down also works). A shell entered by a key
   press resumes autoboot after 60 s without input (`BL_SHELL_IDLE_AUTOBOOT_MS`),
   so a line glitch cannot park an unattended printer in the bootloader.
   `info`, `slots`, `boot`.
7. First real write: `python -m fw.build --app-version 1.0.1 --no-flash --out build/v2`
   (no `--app-seq`: the build time is the sequence, so a later build always
   outranks the flashed one), then
   `python -m fw.tool update --port COMx build/v2/app.img --run`. Before any
   erase the driver checks that the status register reads sanely and that the
   write-enable latch sets, so a wrong SPI hypothesis fails with a message and
   nothing written. `reboot` arms the watchdog and, if the printer is still
   running 3 s later, says so and returns to the shell (the reset is
   unverified); `bl` + `boot` or a power cycle starts the new slot B.

If the bootloader banner never appears: the UART is unchanged from HP's
configuration, so no output at all points at the stage-1 handoff (compare the
`[Boot]` lines with the emulator's). Restoring `dump.bin` with the CH341 brings
the stock firmware back in every case; stage-1 is never modified.

## First boot on the real printer (2026-11-04)

`dump_helloWorld.bin` (SHA-256 `01fb7699…`, artifacts with symbols frozen in
`build/flashed-dump_helloWorld/`) booted on the physical DeskJet 2620:

- HP stage-1 took the footer path (`Hdr is no good-CS0x00; 0x00ffff94.`) with
  the unit's NVM (`NOS_NVM_FWUPDATE_STATUS = 0xa9`) and called `0xa0020000`.
- The bootloader ran, measured the system counter at **0.99 ticks/µs** (the
  emulator's 4 was wrong; the UART calibration corrected it), validated both
  slots through the memory-mapped flash window, copied slot A into DRAM and
  started it: cache maintenance and the 0x20/0xA0 aliasing work on silicon.
- The SPI **command** path read the JEDEC id as `de8030`, i.e. `ef4018`
  shifted left by one bit, with the old per-byte receive method (bit 5 always
  set, RX read after every byte). HP's own stage-1 reads `ef4018` on the same
  board with its identification pattern. Booting does not use the command
  path, and the old driver's writes refused safely (the write-enable check
  read WEL in the wrong bit position, so no erase or program was ever sent).

The driver now uses HP's pattern for every read and calibrates against the
memory-mapped window before allowing writes (see `fw/common/spi_flash.c`).
The flashed bootloader still has the old driver, so its own `update`/`flash`
commands cannot write; applications built from the current source can, and
the bootloader's `run` command loads them into RAM without any flash write:

```
python -m fw.build --out build/next
python -m fw.tool run --port COM41 build/next/app.img --then "flash diag" --then "flash selftest"
python -m fw.tool update --port COM41 build/next/app.img --run
```

`VirtualPrinter(..., spi_rx_read_shift=True)` makes the emulator reproduce
the observation (the flashed bootloader then prints exactly the line seen on
the printer). The `flash diag` measurements from the printer and their
interpretation (bit 5 set samples MISO one bit late; a bit-5-clear transfer
realigns) are in `analysis/spi_review.md`, "Physical observations".

### Verified on the printer over COM41 the same evening

| Function | Result |
|---|---|
| UART receive (shell input, XMODEM at 115200) | works; 34 KiB in about 3 s |
| `run` (app from RAM via the bootloader) | works |
| new SPI driver: `flash selftest`, calibration | passed; status register `00`, no block protection |
| self-update: `update` erase + program + verify of slot B | works, twice |
| boot selection by sequence after an update | works |
| `bl` → `BL>` → `boot` round trips | work repeatedly |
| `reboot` (watchdog) | resets the SoC: stage-1, bootloader, autoboot |
| system counter | exactly 1 MHz (30.04 s of PC time = 30,041,795 ticks); UART calibration reads 0.99, now rounded to 1.00 |
| caches | present (`cache type 0f112152`); copy/clean/invalidate path works |
| `i2c scan`, `eeprom` | 0x1F and 0x50 answer; NVM readable |
| `asic` | MCASIC `0x8014`; carriage ASIC does not answer (its MICCI bus is not set up by stage-1) |
| `adc 15`, `adc 0` | reference 510/1023; supply 783/1023 = 21.5 V |
| `memtest` | 1 MiB at 0x20800000, 0 errors |
| `lcd`, `buttons` | first driver read signature `ff`; after matching HP stage-1's waveform exactly (bit 0 held, 225 µs per edge, reset pulse; see `analysis/front_panel.md`) every frame is acknowledged and button presses decode live (`buttons watch [s]`). The display map was then checked element by element with `lcd walk`: Wi-Fi antenna icon plus five bars, ink bars drawn from the bottom, orange Info/Resume/Ink LEDs on frame bits 11/9/8, no backlight (table in `analysis/front_panel.md`) |

State left on the printer after that first session: slot A = hello example
(`dump_helloWorld.bin`), slot B = `fw/app` built into `build/next/`. Since
2026-11-05 evening both slots hold the full application (updated over
Wi-Fi and USB); `slots` on the printer shows the current pair.

## Hardware unknowns (written before the first boot)

Status after the first session on the printer: the SPI command path, the
timer rate, caches, UART receive, the watchdog reset and the front panel
(display and buttons) are resolved (see above). Still open: the carriage ASIC
bus, which button-map entries correspond to which physical keys beyond those
confirmed, and what HP's exception handlers do on a crash. The original notes
follow for reference.

- **AXON SPI serializer.** Two register interpretations fit all of HP's
  traffic (`analysis/spi_review.md`). At start the BL and the app read the
  JEDEC ID in `byte-final` mode and, only if it is not `EF4018`, retry in
  `wide-final` and adopt whichever returns the known ID (read-only probe,
  reported in the boot log and `sysinfo`); `flash mode` can still override.
  The driver read-modify-writes the control register like HP's driver (so
  boot-strap bits it may have set survive), waits for the same controller
  states (13 with CS held, 0 after release) with a bounded poll, and before
  any erase requires a sane status byte (not 0xFF, WIP and WEL clear) plus a
  WEL that sets after WREN. Verification after erase/program reads the
  memory-mapped window and, if that disagrees, re-reads through the SPI
  command: a lagging window is counted (`stale window sectors`), wrong chip
  content is an error.
- **Timer rate.** `TIMER_TICKS_PER_US = 4` is only the compiled default. At
  boot the BL times 256 UART bytes (115200 8N1 is established) against the
  counter and adopts the measured rate for the autoboot delay, XMODEM timeouts
  and `uptime`; the app inherits it through the boot-info block. An implausible
  measurement (outside 0.25..1000 ticks/us) keeps the default and logs a warning.
- **Watchdog reset.** Arming the two-stage watchdog is HP's own sequence, but
  that the second stage resets the SoC is inferred, not observed. `reboot`
  therefore waits 3 s and reports if nothing happened; `bl` + `boot` restarts
  the application without any reset, and a power cycle always works.
- **Exceptions.** The BL installs no vector table; a data abort (for example
  from a stray pointer in your own app) lands in HP's stage-1 handlers. The
  shell commands that take addresses (`md`, `mw`, `crc`) refuse anything
  outside the flash window, the DRAM aliases and the 0xBD peripheral block
  for that reason.
- **Caches.** The emulator has no cache model; the cache maintenance before
  jumping to copied code and after SPI writes follows the ARM946E-S TRM and
  has not run on silicon.
- **Stage-1 behaviour with the real EEPROM.** The emulator's factory profile
  takes the footer path and falls back cleanly; the unit's actual NVM may
  choose a different load mode. Stage-1 itself is never modified, so the stock
  `dump.bin` always restores the printer.
