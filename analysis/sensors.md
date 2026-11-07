# Door and input-tray sensors (2026-11-05, emulator)

## How the mapping was found

HP's firmware names its digital sensors in `lib_digital_sensor`; the name
table at `0x2056d190` (Stage-22 snapshot) lists them in ID order:

| ID | HP name |
| --- | --- |
| 0 | `DOOR_SENSOR` |
| 1 | `TRAY_SENSOR` |
| 2 | `TRAY_IR_SENSOR_S1` |
| 3 | `TRAY_IR_SENSOR_S2` |
| 4 | `TRAY_IR_SENSOR_S3` |
| 5 | `TOF_OOPS` |

HP's shell answers `udws "pe_digital_sensor.get <id>;"`. In the emulator
(Stage-22 snapshot of HP's own firmware), each GPIO input that HP reads (pins
1, 2, 3, 8, 11 and 15, the same inputs the harness supplies) was flipped once
and all six sensors were asked again:

| Flipped input | Sensor that changed |
| --- | --- |
| GPIO 1: 0 -> 1 | `TRAY_IR_SENSOR_S1`: 0 -> 1 |
| GPIO 2: 0 -> 1 | `TRAY_IR_SENSOR_S2`: 0 -> 1 |
| GPIO 3: 0 -> 1 | `TRAY_IR_SENSOR_S3`: 0 -> 1 |
| GPIO 8: 1 -> 0 | `DOOR_SENSOR`: 0 -> 1 |
| GPIO 11, GPIO 15 | none of the six |

`TRAY_SENSOR` follows none of these pins and `TOF_OOPS` answers -1 (not
fitted on this model). HP's reports call the three IR sensors small, medium
and large ("Tap 916 - Input tray IR sensors related information"), so they
probably see paper of different widths in the input tray.

## In the custom firmware

`sensors` prints every input with its level and HP's meaning, and the MCASIC
GPIO input register `0x3b`; `sensors watch [s]` reports changes as they
happen (open the door, slide paper in). The panel menu lights the `!` icon and
logs `ui: door open` while `DOOR_SENSOR` is active.

## Unconfirmed on the printer

- Which door GPIO 8 belongs to (the cartridge access door is the obvious
  candidate) and that "active" means open: the emulator's default level
  (high) is the one HP's firmware boots with, which suggests closed.
- Which IR sensor level means "paper present".
- What GPIO 11 and 15 are.

Check on the printer with `sensors watch 60` while opening the door and
sliding paper into the tray.
