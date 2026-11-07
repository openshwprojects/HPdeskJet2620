# Ink cartridges and the carriage ASIC (2026-11-05, research only)

Goal: read which cartridges are installed and show it on the LCD ink gauges.
Blocked by the carriage ASIC staying silent on the printer.

## Known

- The carriage ASIC sits on MICCI controller 1 (`0xBD300C00`), bus 1,
  device 4. The custom firmware's `asic` command reads its ID (register
  `0x7d`) and status (`0x00`); the emulator's `CarriageAsic` model answers
  `0x0801` / `0x800e` (pen presence in register `0x09` bit 8). On the printer
  the same reads got no answer.
- The MICCI controllers are configured by HP's boot ROM, before the custom
  bootloader runs: controller 1 gets `+0 = 0x2400`, `+4 = 0x2c0018`,
  `+8 = 0xb40018` (code at `0xa00101a8..0xa00101b2`), the same values HP's main
  firmware has in the Stage-22 snapshot. So the bus set-up is probably not
  the difference.
- The addressing matches too: the last command HP's firmware left in
  controller 1 is `0x943F`, which the custom firmware's encoding
  (`dev << 13 | reg << 6 | read << 5 | bus << 4 | 0xF`) reads as device 4,
  register `0x50`, read, bus 1 - the same device the `asic` command asks.
- HP's own pen test (`udws "pen.etest;"`) passes in the emulator against the
  model, and HP names the cartridge family (Dolmen: HP 304/65).
- HP's power devices include `POWER_PEN_VPP_LOW` (MCASIC aux rail) and
  `POWER_PEN_VPP_32V_LOW` (`0xa02564f4` / `0xa02564fc`); the rails read the
  same in HP's snapshot and in the custom firmware.
- The MCASIC GPIO block differs: HP has `0x38 = 0x0030`, `0x39 = 0x0030`
  (bits 4 and 5 as outputs, driven low) and `0x3a = 0xc000`; the custom
  firmware leaves all three at 0. Whether bits 4/5 power or release the
  carriage electronics is unknown, but it is the cheapest thing to try.

## Experiment for the printer

```
asic
motor init
asic
md 0xbd300c00 4
```

`motor init` writes HP's MCASIC state (including `0x38..0x3a`) without moving
anything. If the second `asic` answers, the ink gauges can follow: register
`0x09` bit 8 is the pen-presence bit in the model, and HP's `pen.*` commands
show which registers its pen code reads.
