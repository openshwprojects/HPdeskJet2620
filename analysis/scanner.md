# Scanner: register map, LED control and a single-line read (2026-11-05)

Goal: light the CIS lamp and read one scan line from the custom firmware.
Status: research only. Nothing below has been run on the printer yet. Every
statement is marked as **proven** (with the HP code address) or **guess**.

## How the scanner is built (proven)

Two chips take part:

1. **"Dasher"**, a front-end ASIC on the scan module side (HP strings
   `DASHER`, `COMET`, `SCANNER DASHER VENDOR`, `DASHER_IC_MANUF_ID_TI`,
   `DASHER_IC_MANUF_ID_WOLFSON` near `0x203a06e0`/`0x203a1acb`). It holds the
   LED current DACs and the LED state machine, the sensor timing generator
   (PHI/RS/VSMP/A2D edges), the ADC/PGA and an LVDS serialiser. It is a
   **MICCI device: controller 0 (`0xBD300800`), sub-bus 1, device 1**, HP
   handle `0x11` (set at `0xa03a110a`/`0xa03a15aa`; the handle-to-bus/device
   mapping is `0xa0171420`: `handle >> 4` = 1 -> controller 0 bus 1,
   `handle & 7` = device). HP expects `REG_CHIP_ID` = `0x800C`
   (`0xa03a189a` compares with `0xa05fa4c8[select]`, the table holds
   `8006 800c 800d`; `select` is hard-wired to 1 by `0xa03a1586`).
2. **The SoC scan block** at `0xBD066C00..0xBD06B4xx`: SCIF (serial receiver
   for the Dasher's LVDS stream), the line timing generator (TG), LED PWM
   timing (ScanIllum), datapath with light-monitor windows, clamp and
   averaging. HP's scan driver (`sc_registers.c`, code `0xa03b1d00..
   0xa03b9b00`) touches no other SoC block except `0xBD041010`
   (`ScanScifRefSelect`, see below) and two motor muxes
   (`0xBD303C00` `ScanMotorDcSignalMux` at `0xa03b4040`, `0xBD303800`
   `ScanMotorTypeMux` at `0xa03b69bc`).

So the LED on/off and LED current are in the Dasher (over MICCI); the SoC
gives the per-line LED timing and receives the pixels.

## HP's scan block state after boot (proven)

The earlier note that HP never touches `0xBD067xxx` is only true for the copy
job. HP's boot **does** initialise the scan block: the Stage-22 snapshot
(`artifacts/snap_stage22.pkl`, key `reg_bytes`) holds the values below, and
HP's Dasher shadow array (`0x20da0b9c`, 0x80 halfwords, updated by every
field write even when the MICCI write is skipped) holds the Dasher values HP
intended (the emulator has no Dasher, so HP logged a failed detection but
continued). Writes go through `0xa03b1dc8` (a logging hook, no side effect).

### SoC register table

HP's table at `0xa05ff6c4` (12-byte entries: address, size byte `02`/`04`,
name pointer; 181 entries). Value = snapshot after HP's init. Registers not in
that table but named in the code are marked with `*`.

| Address | Size | Name | HP value |
| --- | --- | --- | --- |
| `0xBD066C00` | 2 | `ScanScifMergeDataSelect` | `0001` |
| `0xBD066C02` | 2 | `ScanScifMergeNewPixelId` | `0000` |
| `0xBD066C04` | 2 | `ScanScifNewStart` | `0001` |
| `0xBD066C08` | 4 | `ScanScifMergeLineCountRd` | |
| `0xBD066C0C` | 2 | `ScanScifMergeClearLineCount` * | `0001` |
| `0xBD066C0E` | 2 | `ScanScifMergePadControl` (b1 clk pad, b0 data pad) | `0001` |
| `0xBD066C10` | 2 | `ScanScifMergeIntStatusRd` | |
| `0xBD066C12` | 2 | `ScanScifMergeIntMask` | `0001` |
| `0xBD067000` | 4 | `ScanScif0Config` | `026015ca` |
| `0xBD067004` | 2 | `ScanScif0PacketsPerLine` | `0001` |
| `0xBD067006` | 2 | `ScanScif0SensorIdMap` | `4000` |
| `0xBD06700A` | 2 | `ScanScif0ByteSync` | `0e62` |
| `0xBD06700C` | 2 | `ScanScif0PacketErrorControl` | `0000` |
| `0xBD067010` | 4 | `ScanScif0ErrorFifoDataRd` | |
| `0xBD067014` | 4 | `ScanScif0ErrorFifoMask` | `0` |
| `0xBD067018` | 2 | `ScanScif0ErrorFifoFullRd` | |
| `0xBD06701A` | 2 | `ScanScif0ExpectedTestPattern` | `0000` |
| `0xBD06701C` | 2 | `ScanScif0CapturedTestPatternRd` | |
| `0xBD06701E` | 2 | `ScanScif0TestPatternMismatchRd` | |
| `0xBD067020` | 4 | `ScanScif0ClockFilterDelays` | `40000000` |
| `0xBD067024` | 4 | `ScanScif0DataFilterDelays` | `40000000` |
| `0xBD067400` | 2 | `ScanTargetExp` | `0c24` |
| `0xBD067402/404` | 2 | `ScanVsampEdge1/2` | `0024 0040` |
| `0xBD067406..41C` | 2 | `ScanA2DclkEdge1..12` | `0002 0010 001a 0026 002e 003a`, rest `ffff` |
| `0xBD06741E` | 2 | `ScanStartupLat` | `0006` |
| `0xBD067420` | 2 | `ScanShutdownLat` | `0000` |
| `0xBD067422..438` | 2 | `ScanRed/Grn/Blu A..D Edge` | each colour `0015 001e 0002 000c` |
| `0xBD06743A` | 2 | `ScanNewPixelEdge` | `0024` |
| `0xBD06743C..440` | 2 | `ScanRed/Grn/BluCompADGain` | `0` |
| `0xBD067442..446` | 2 | `ScanRed/Grn/BluGain` | `0001` |
| `0xBD067448..452` | 2 | `ScanRed/Grn/BluSlope0/1` | `0` |
| `0xBD067454` | 2 | `ScanAfeSignalInit` | `0000` |
| `0xBD067456` | 2 | `ScanAfeControl` | `008c` |
| `0xBD067458` | 2 | `ScanAfeIoSpeed` | `0003` |
| `0xBD06745A` | 2 | `ScanAfeReadRd` | |
| `0xBD06745C` | 2 | `ScanAfeAdrNdatMask` | `ff00` |
| `0xBD06745E..462` | 2 | `ScanR/G/BAfeGainAddr` | `2800` |
| `0xBD067464..468` | 2 | `ScanR/G/BAfeOfstAddr` | `2000` |
| `0xBD06746A` | 2 | `ScanAfeTxManual` | |
| `0xBD06746C..470` | 2 | `ScanRed/Grn/BluOffset` | `00fd 0 0` |
| `0xBD067472` | 2 | `ScanAfeSamplingStatusRd` | |
| `0xBD067474` | 4 | `ScanAfeDigGainStatusRd` | |
| `0xBD067478` | 4 | `ScanAfeAnalogGainRd` | |
| `0xBD06747C` | 2 | `ScanAfeAnalogGainStatusRd` | |
| `0xBD067480` | 4 | `ScanAfeTestControl` | `0` |
| `0xBD067484` | 4 | `ScanAfeTestChPixelSel` | `0` |
| `0xBD067488..48C` | 2 | `ScanAfeTestPixelData1..3Rd` | |
| `0xBD067800` | 4 | `ScanIllumBulbPwmPeriodOnTime` | `ffff0000` |
| `0xBD067804` | 4 | `ScanIllumLedCalib` | `000000ff` |
| `0xBD067808` | 4 | `ScanIllumControl` | `00110000` |
| `0xBD06780C` | 4 | `ScanIllumRedLedDcOrPwm` | `00500050` |
| `0xBD067810` | 4 | `ScanIllumGrnLedDcOrPwm` | `00500050` |
| `0xBD067814` | 4 | `ScanIllumBluLedDcOrPwm` | `00500050` |
| `0xBD067818` | 2 | `ScanIllumLedControl` | `3f80` |
| `0xBD06781A` | 2 | `ScanIllumLedTimerDelay` | `0002` |
| `0xBD06781C` | 4 | `ScanIllumAux1LedTimerWindow` | `00500af4` |
| `0xBD067820` | 2 | `ScanIllumLedSmCurrentStateRd` | |
| `0xBD068000` | 2 | `ScanDPLm0StartColumn` | `0000` |
| `0xBD068002` | 2 | `ScanDPLm0TotalPixels` (pixels - 1) | `0000` |
| `0xBD068004` | 2 | `ScanDPLm1StartColumn` | `0000` |
| `0xBD068006` | 2 | `ScanDPLm1TotalPixels` | `ffff` |
| `0xBD068008` | 2 | `ScanDPImageStartColumn` | `0016` (22) |
| `0xBD06800A` | 2 | `ScanDPImageTotalPixels` | `0a40` (2624) |
| `0xBD06800C/00E` | 2 | `ScanDPDarkStartColumn/TotalPixels` | `0` |
| `0xBD068010` | 2 | `ScanDPMaxArraysPerColor` | `0001` |
| `0xBD068012` | 2 | `ScanDPChannelCounter` | `0000` |
| `0xBD068014` | 4 | `ScanDPChannelOffset` | `00020100` |
| `0xBD068018..01C` | 2 | `ScanDPChsOffsetRd`, `ScanDPStatus2Rd`, `ScanDPAdjPixelColPosRd` | |
| `0xBD06801E` | 2 | `ScanDPControl` | `0000` |
| `0xBD068020` | 2 | `ScanDPExposureTime` | `0000` |
| `0xBD068400` | 2 | `ScanTgSetup` | `021c` |
| `0xBD068402` | 2 | `ScanTgAssert` | `1c20` |
| `0xBD068404` | 2 | `ScanTgHold` | `021c` |
| `0xBD068406/408` | 2 | `ScanResSel0Setup/Assert` | `03e8 03e8` |
| `0xBD06840C` | 4 | `ScanResSelConfig` | `0` |
| `0xBD068410/412` | 2 | `ScanMinExp/MaxExp` | `0c24 0c24` |
| `0xBD068414` | 2 | `ScanTGControl` | `3501` |
| `0xBD068416` | 2 | `ScanA2DStartEdge` | `0001` |
| `0xBD068418` | 2 | `ScanPixelTime` | `0058` (88) |
| `0xBD06841A..41E` | 2 | `ScanTGMaskEdges`, `ScanPixelDataEnableMask`, `ScanDatapathChEnable` | `0` |
| `0xBD068420` | 2 | `ScanInterruptControl` | `0000` |
| `0xBD068422` | 2 | `ScanTGStatusRd` | |
| `0xBD068424` | 4 | `ScanTGIsrDelayRd` | |
| `0xBD068428` | 2 | `ScanTGTriggerWr` * | `0001` |
| `0xBD06842A` | 2 | `ScanTGExpWindowMode` | `0001` |
| `0xBD06842C` | 2 | `ScanTGNewPixelPeriods` | `0000` |
| `0xBD068800..816` | 2 | `ScanPhi0Edge1..12` | `0002 0025`, rest `ffff` |
| `0xBD068818/81A` | 2 | `ScanPhi0Init/Ctrl` | `0001 0000` |
| `0xBD068C00..C1A` | 2 | `ScanPhi1Edge1..12`, `Init`, `Ctrl` | all edges `ffff`, `0 0` |
| `0xBD069800..806` | 2 | `ScanRSEdge1..4` | `0004 000e ffff ffff` |
| `0xBD069808` | 2 | `ScanRSInit` | `0001` |
| `0xBD06B000..008` | 4 | `ScanClampCh0..2Average` | |
| `0xBD06B00C..010` | 2 | `ScanClampCh0..2Offset` | `0` |
| `0xBD06B012` | 2 | `ScanClampDarkAccShift` | `0` |
| `0xBD06B014` | 2 | `ScanClampControl` | `0001` |
| `0xBD06B016` | 2 | `ScanClampChannelSelect` | `0fff` |
| `0xBD06B018..020` | 4 | `ScanLmw0Red/Grn/BluDataRd` | |
| `0xBD06B024..02C` | 4 | `ScanLmw1Red/Grn/BluDataRd` | |
| `0xBD06B400` | 2 | `ScanAvgControl` | `0088` |
| `0xBD041010` | 2 | `ScanScifRefSelect` * (system block) | `0008` |

(The old guess "`0xBD06781A` = `ScanIllumControl`" was wrong: `ScanIllumControl`
is `0xBD067808`.)

### Bit layouts (proven from HP's register-dump decoder)

HP's dump routine `0xa03b71fc` prints `[%3d] %08x %36s = ...` for a table
entry and then dispatches on the address (`0xa03b7244..0xa03b74a4`) to a
decoder that prints named fields. Field positions below are read from the
shift pairs in those decoders.

`ScanIllumControl` `0x808` (decoder `0xa03b7cca`):

| Bits | Field |
| --- | --- |
| 25 | `reset_led` |
| 24 | `pwms_ld_2nd_xg_en` |
| 23 | `bulb_enable` |
| 22 | `bulb_pwm_enable` |
| 21 | `ord_enable` |
| 20 | `ledoperatenow` |
| 19:18 | `led_on_value` |
| 17:16 | `led_off_value` |
| 15 / 14 | `led_pwm2_always_off` / `led_pwm2_always_on` |
| 13 / 12 | `led_pwm1_always_off` / `led_pwm1_always_on` |
| 11 / 10 | `led_pwm0_always_off` / `led_pwm0_always_on` |
| 9:3 | `pwm_led_enable` |
| 2:0 | `led_mode_select` |

`ScanIllumLedControl` `0x818` (16-bit, decoder `0xa03b7d38`): bits 13:7
`mask_led`, bits 6:0 `ledon_stall`. HP's "LEDs off" is `mask_led = 0x7f`
(value `0x3f80`, `0xa03b67f0(1)` and init `0xa03b1e1e`); its manual LED path
writes 0 (`0xa03b6fa6..0xa03b7088`).

`ScanIllumRed/Grn/BluLedDcOrPwm` `0x80C/0x810/0x814` (decoder `0xa03b7d2c`):
bits 31:16 `led_settime_ontime`, bits 15:0 `led_offtime_period`, i.e. LED on
and off position within the line. HP's init (`0xa03b1de2`) writes Red
`0x004009c4`, Green `0x004007d0`, Blue `0x00400514` (on at 64, off at
2500/2000/1300); the snapshot has `0x00500050` (on = off, no light).
`ScanIllumLedCalib` `0x804`: bit 16 `led_auto_calib`, 15:8 `row_counter`,
7:0 `led_autoonrow`. `ScanIllumBulbPwmPeriodOnTime` `0x800`: 31:16
`pwm_period_count`, 15:0 `pwm_on_count`. `ScanIllumAux1LedTimerWindow`:
31:16 start, 15:0 stop.

`ScanTGControl` `0x8414` (decoder `0xa03b8106`): bit 15 `data_eol_select`,
14 `load_tg_masks_now`, 13 `syncstep_on_tg_rise`, 12 `syncstep_on_tg_blue`,
11 `quad_phi_enable`, 10..6 `phi_3q/2q/1q/tg_rise/tg_fall_toggle_enable`,
5 `tg_wait_for_led_enable`, 4 `tg_active_low`, 3 `scan_init_tgs`,
2 `pos_tgs`, **0 `scanner_enable`**. HP's value `0x3501` = running.

`ScanInterruptControl` `0x8420`: bit 11 `reset_isr_delay_counter`, 10
`interrupt_set`, 9 `interrupt_clear`, 3:0 `isr_select`.
`ScanTGStatusRd` `0x8422`: bit 4 `tg_control_sm_idle`, 3 `tg_pos_max_error`,
2 `tg_pos_min_error`, 1 `tg_pos_late`, 0 `tg_pos_early`.

`ScanScif0Config` `0x7000` (decoder `0xa03b857c`): bit 25
`clock_extraction`, 24 `test_mode`, 23 `native_data_mode`, 22:21
`pixel_data_width`, 19:0 `packet_size`. HP's `0x026015ca`: clock extraction
on, width code 3, packet 0x15ca = 5578 bytes. `ScanScif0SensorIdMap`
`0x7006`: 15:14 `active_sensors`, then three 4-bit sensor ids.

`ScanAfeTestChPixelSel` `0x7484`: 31:29 `ch_sel`, 15:0 `pixel_sel`;
`ScanAfeTestControl` `0x7480`: 17:16 `test_mode`, 15:0
`pixel_data_in_test_mode`. HP's UDW command at `0xa03b9616` writes
`(ch << 29) | pixel`, waits, and prints `ScanAfeTestPixelData1..3Rd`
(`pixel_data[] = %d,%d,%d`).

### HP's SoC sequences (proven)

- **Init** `0xa03b1de2`: Red/Grn/Blu PWM, `LedControl = 0x3f80`,
  `IllumControl` from shadow, `LedTimerDelay = 2`, `AfeIoSpeed = 3`,
  `DPChannelCounter`, `DPChannelOffset`, `TGControl`, `InterruptControl`,
  `StartupLat = 6`, AFE address masks, `ShutdownLat = 0`, then timing
  helpers (`0xa03b4250`, `0xa03b5c26`, `0xa03b5e48`, `0xa03b5da8`,
  `0xa03b6136`). `0xa03b21c6` resets all AFE/edge registers to `ffff`/0.
- **SCIF + Dasher link + start** (function ending `0xa03b56cc`): Dasher
  analog and LVDS setup (see below), `ScanScifMergeDataSelect = 1`,
  `ScanScifRefSelect (0xBD041010) = 8`, `ScanScif0Config`,
  `ScanScif0PacketsPerLine = (pixels * bits) / (packet_size * 8)`, then
  `TGControl |= scan_init_tgs (b3)`, write, clear b3, write, set
  `scanner_enable (b0)`, write, `ScanScifNewStart = 1`,
  `ScanTGTriggerWr (0xBD068428) = 1`.
- **Stop/idle wait** `0xa03b4396..0xa03b4424`: clear `ledoperatenow`, poll
  the TG state for idle (timeout `0x3d090` timer ticks, message
  `Timed-out waiting for idle state`), then clear `scanner_enable`.
- **Manual LED (UDW)** `0xa03b6e96(mask)`: mask 0 -> `LedControl |= 0x3f80`
  (off). Otherwise for R (b0), G (b1), B (b2): `DcOrPwm = (on << 16) | off`
  from the table set by `0xa03b6e4a` (`[R 0x01] %d %d`), others 0;
  `IllumControl` bits 19:16 = `1 | ((mask & 0x40) ? 0xc : 0)`;
  `LedControl = 0`; `IllumControl |= ledoperatenow (b20)`.
- **Light monitor windows** `0xa03b68a6` / `0xa03b68c6`: start columns into
  `ScanDPLm0/1StartColumn`, length - 1 into `ScanDPLm0/1TotalPixels`; sums are
  read from `ScanLmw0/1Red/Grn/BluDataRd` (`0xa03b68e8`, `0xa03b68fc`, UDW
  print `LMW 0: %d %d %d, LMW 1: %d %d %d`).

### Clock, reset, power

- No clock-gate or reset register for the scan block was found in HP's scan
  driver; it writes the scan registers directly. The only write outside the
  block is `ScanScifRefSelect` (`0xBD041010 = 8`, `0xa03b5574`), in the system
  block next to the PLL/timer registers. **Guess**: SCIF reference clock
  select. HP's loader (which also runs before the custom firmware) does the
  PLL setup; whether the scan block is clocked in the custom firmware is
  unknown until a read succeeds on the printer.
- HP's power table has `POWER_SCAN_SENSOR`, `POWER_SCAN_LED`,
  `POWER_SCAN_AFE` but with no function on this printer (entries 32/43/44 at
  `0x20ef7a10` are empty). Scan module power is controlled inside the Dasher
  (`REG_SLEEP_MODE_CNTL` b0, `REG_VLED_CP` b0). `POWER_SCAN_ENCODER` is MCASIC
  `0x3a` b14 (motors.md).

## Dasher register map (proven: HP's field table `0xa05f67ac`)

HP describes the Dasher by fields: table pointer array `0xa05f53f0` (select 0,
1, 2 = `0xa05f53fc`, `0xa05f67ac`, `0xa05f7288`; this printer uses 1). Entry
= 0x14 bytes: id, name, parent id, width, bit position, register, default.
Field access: `0xa03a11d8(id, value, mode)` (mode 0 = shadow only, 1 = shadow
and write, 2 = write whole shadow register), reads `0xa03a114e(id, mode)`,
MICCI transfer `0xa017195e`/`0xa01716c8`. The shadow array is `0x20da0b9c`.

| Reg | Name | Fields | HP shadow after boot |
| --- | --- | --- | --- |
| `0x00` | `REG_CHIP_ID` | | expect `800c` |
| `0x01` | `REG_IC_SUPER_STATUS` | 14 CLR_OP_UV, 13 V3P3_OP_UV_LTH, 12 SUP_FORCE_RST, 11 CLR_SUP_FORCE_RST, 10 CLR_FIFO_OVR_FLW, 9 FIFO_OVR_FLW, 2 WARM, 1 CLR_OTS_LTH, 0 OTS_LTH | |
| `0x02` | `REG_MICCI_STATUS` | 15 EN_VEND_REGS, 3 CLR_LAST_MICCI_ERR, 2:0 LAST_MICCI_ERR | |
| `0x03` | `REG_FORCE_RESET` | HP writes `0x279a` when the chip ID is wrong (`0xa03a161a`) | |
| `0x06` | `REG_SLEEP_MODE_CNTL` | 8 LED_FW_EN, 0 SLEEPMODE | `0100` |
| `0x08` | `REG_DITHER_CNTL` | 4 DITHER_PCT, 0 DITHER_EN | `0001` |
| `0x09` | `REG_OSC_TRAINING` | 15 TRAIN_EN, 8:4 TRAIN_FREQ, 3:0 TRAIN_DIV | `0000` |
| `0x0f` | `REG_PT_LOCKOUT` | 15 SM_LOCKOUT, 7 PT_RESET, 3 N_XG_LOCKOUT | `0008` |
| `0x10` | `REG_PIXEL_CLK_PRESCALER` | 7:0 | `0027` |
| `0x11` | `REG_DUMMY_COUNT` | 7:0 | `0016` (22) |
| `0x12` | `REG_PIXEL_COUNT` | 15:0 | `0399` (921) |
| `0x13` | `REG_TGEN_INIT_VAL` | 12 XG_INV, 8 RES_SEL_INIT, 4 CCD_RS_INIT, 3 A2D_CLK, 2 BSMP, 1 VSMP, 0 PHI | `0011` |
| `0x14` | `REG_XG_HOLD_TIME` | 7:0 | `0005` |
| `0x15` | `REG_PHI_EDGE` | 15:8 edge2, 7:0 edge1 | `2002` |
| `0x1d..0x1f` | `REG_PHI_XG_TOGGLE_1..3` | edge pairs | `403b 4a45 ff00` |
| `0x20` | `REG_CCD_RS_EDGE` | | `0901` |
| `0x21` | `REG_VSMP_EDGE` | | `3b38` |
| `0x22` | `REG_BSMP_EDGE` | | `1d1a` |
| `0x23` | `REG_A2D_CLK_EDGE` | | `1900` |
| `0x2f` | `REG_VLED_CP` | 0 CP_VLED_EN (LED supply charge pump) | `0001` |
| `0x30` | `REG_ILLUM_STATUS` | 12 LED_MONO, 8 LED_RESTART, 5:4 NEXT_STATE, 1:0 CURR_STATE | |
| `0x31` | `REG_LED_STATE_0_CNTL` | 14:12 MUX, 10:8 COARSE_IDAC, 5:0 FINE_IDAC | `0000` |
| `0x32` | `REG_LED_STATE_1_3_CNTL` | same | `0000` |
| `0x33` | `REG_LED_STATE_2_CNTL` | same | `0000` |
| `0x34` | `REG_LED_STATE_SEQ` | next state of state 3..0 in 13:12, 9:8, 5:4, 1:0 | `0021` (0->1->2->0) |
| `0x50` | `REG_GPIO_CONFIG` | modes, output enables/values, pad values | `0320` |
| `0x52` | `REG_RES_SEL` | 8 RES_SEL_EN, 5:0 pulses | |
| `0x60` | `REG_ANALOG_CONFIG` | 13:12 BIT_DEPTH, 10 RLC_OUTPUT_EN, 9 RLC_CLAMP_EN, 8 RLC_CCD, 4 CCD_MODE, 0 VIDEO_EN | `3311` |
| `0x61` | `REG_PGA_DAC` | 13:8 VID_DAC, 2:0 VID_PGA | `3f01` |
| `0x70` | `REG_SZ_N_LOCKOUT` | 15 SZ_N_LOCK | `0000` |
| `0x71` | `REG_LVDS_CONFIG` | 15 CLK_EN, 14 DATA_EN, 11 EN_8B10B, 10 FLIP_8B10B, 9 TEST_EN, 8 TEST_MODE, 4 DIV_ODD, 2:0 DATA_PRESCALE | `4c02` |
| `0x72` | `REG_LVDS_PACKETIZER` | 15 FULL_SCAN_LINE, 14:0 PACKET_SIZE | `8000` |
| `0x73` | `REG_LVDS_TEST_PATTERN` | | `f0f0` |
| `0x7d` | `REG_IC_REVISION` | 15:8 manufacturer, 7:0 revision | |

Detection `0xa03a158a`: read `0x00`; if wrong, write `FORCE_RESET = 0x279a`,
read again after 2 ms and once more after 50 ms; then read `0x7d`. Sleep/wake `0xa03a175c` /
`0xa03a17f4` (`SLEEPMODE` 1/0). LVDS link test `0xa03a1482`: `SZ_N_LOCK = 1`,
pattern into `0x73`, `CLK_EN`, test mode, `SZ_N_LOCK = 0`.

### LED control in the Dasher (proven)

`0xa03b3c70..0xa03b3ed0` programs the SoC Red, Green, Blue PWM registers and,
in the same order, the Dasher LED states: state 0 `MUX = 2` (`0xa03b3df0`),
state 1 `MUX = 1` (`0xa03b3e3c`), state 2 `MUX = 4` (`0xa03b3e86`), each with
coarse/fine IDAC from `0xa03b3644(colour, ...)` (calibrated current; a state
with no current gets the whole register 0). Then `CP_VLED_EN = 1` (unless a
flag at `+0x8f`) and `LED_FW_EN = 1`. Colour mode sequence: `0x34` with
0->1->2->0 (`0xa03b44f8`); mono: all next states 0 (`0xa03b42b4`). HP checks
`ILLUM_CURR_STATE == 0` before reprogramming (`0xa03b3db2`).

So: **Red = MUX 2, Green = MUX 1, Blue = MUX 4** (pairing proven; that
colour 0/1/2 is R/G/B follows from the SoC register names written in the same
function). LED current scale: unknown (guess: start with coarse 1, fine 0x10).

**Guess**: the SoC PWM (`DcOrPwm`, `IllumControl`, `LedControl`) gates the
LED per line through the scanner cable, the Dasher sets which LED and how much
current. Whether `LED_FW_EN = 1` alone lights the LED without the SoC side is
the first thing to test.

## Pixel data (what is known)

- Path: Dasher ADC -> LVDS (8b10b, one packet per line, `FULL_SCAN_LINE`)
  -> SCIF0 -> SCIF merge -> datapath (image window, light-monitor windows,
  clamp, averaging). HP's values: 22 dummy pixels (Dasher `DUMMY_COUNT` and
  `ScanDPImageStartColumn` agree), 2624 image pixels (`ScanDPImageTotalPixels`),
  packet 5578 bytes = 2789 16-bit samples, 16-bit samples
  (`pixel_data_width` 3; Dasher `BIT_DEPTH` 3).
- The snapshot timing is one of HP's timing sets (selected by resolution,
  exposure and colour space, UDW `scan sensor timing`); which resolution it
  is is not known. 2624 pixels would be 222 mm at 300 dpi; an A4 CIS at
  600 dpi would give about 5100, at 1200 dpi about 10200.
- **Line counter**: `ScanScifMergeLineCountRd` (`0xBD066C08`) counts received
  lines (name; guess that it runs continuously).
- **Without DMA**: the two light-monitor windows give per-line sums of a
  column range for R/G/B; 1-pixel windows give two pixel values per line.
  HP's AFE test-pixel registers give one selected pixel.
- **DMA: not found from the scan driver.** The scan code calls no DMA code.
  Candidate (guess, low confidence): a ring-buffer write DMA with channels at
  `0xBD06C800 + n * 0x400` (`+4` start, `+8` DRAM address, `+0xc` size,
  `+0xe` stride, `+0x12` release rows, `+0x16` status), control at
  `0xBD06B800`/`0xBD06B808`, driven by code at `0xa02cf9xx..0xa02d0cxx` next
  to the string `mergeout_holdoff` (`0x202d0cbe`); it is part of HP's
  `lib_tier1` image pipeline (mergein, prnu, vscale, matrix, mergeout).

## Experiment plan for the printer

Run each step only after the previous one gave the expected answer. Power
cycle if the shell stops answering.

1. **Read-only, MICCI** (cannot hang the CPU; the driver times out):
   `micci read 0 1 1 0x00` -> expect `0x800c` (Dasher present and powered).
   Then `0x7d` (manufacturer/revision), `0x01`, `0x06`, `0x2f`, `0x30`.
   A timeout means not powered or not there: stop and look for a power enable
   (MCASIC GPIO `0x3a`, "PASS SW A").
2. **Read-only, SoC, one register at a time** (an unclocked block may hang the
   bus): `md 0xbd066c08 1`, then `md 0xbd067000 1`, `md 0xbd067800 8`,
   `md 0xbd068414 1` (`ScanTGControl` is the upper half), `md 0xbd041010 1`.
   A hang means the block needs a clock enable not found yet. Reset values
   (probably 0) say nothing; to prove the block is clocked, write and read
   back a harmless register: `mw 0xbd068000 0x00050000` (light-monitor
   window 0 start = 5), `md 0xbd068000 1`, `mw 0xbd068000 0`.
3. **One LED, Dasher only** (`scan led g 500` in the draft, by hand with
   `micci write 0 1 1 ...`): `06=0000` (wake), `34=0000` (stay in state 0),
   `32=0000`, `33=0000`, `31=1110` (MUX green, coarse 1, fine 0x10 —
   **uncertain current**), `2f=0001` (LED supply, **uncertain soft-start**),
   10 ms, `06=0100` (`LED_FW_EN`). Hold 0.5–1 s, then `31=0000`,
   `06=0000`, `2f=0000`. Red `31=2110`, blue `31=4110`. Look at the scan
   bar under the glass with the lid open.
4. **Same plus the SoC LED outputs** (`scan led g 500 soc`): additionally
   `ScanIllumControl |= b10|b12|b14 (pwm0..2 always on) | b20`,
   `ScanIllumLedControl = 0` (**uncertain**: which PWM channel reaches the
   module). Off: clear those bits, `ScanIllumLedControl = 0x3f80`.
5. **One line, dark first** (`scan line dark`, then `scan line g` with the
   lid closed, white backing): Dasher configuration from the shadow table in
   HP's order (LVDS, analog, timing; `OSC_TRAINING` and `GPIO_CONFIG` left
   out — **uncertain**), SoC registers with HP's values (including
   `0xBD041010 = 8`, **uncertain**, system block), start (`scan_init_tgs`
   pulse, `scanner_enable`, `ScanScifNewStart = 1`, `ScanTGTriggerWr = 1`),
   check the line counter over 100 ms (line period), then for each pixel pair
   set the two 1-pixel windows and wait two lines (200 ms timeout each, 30 s
   total). Expected output 2646 values (22 dummy + 2624). Dark: low values;
   lit white: high values; a step pattern at the sensor ends. Off:
   `scanner_enable = 0`, LED states 0, `LED_FW_EN = 0`, `VLED_CP = 0`,
   `ScanIllumLedControl = 0x3f80`.
6. If no lines arrive: try HP's oscillator training (`0xa03a13d0..0xa03a145c`:
   `TRAIN_FREQ = 0x16` (default `0xa05f53ec[1]`, overridable by HP setting 2
   via `0xa03a341c`), `TRAIN_DIV = 5` (9 for manufacturer `0x20` with
   revision other than `0x10`), then `TRAIN_EN = 1`)
   and the LVDS test pattern (`0xa03a1482`, SoC `ScanScif0ExpectedTestPattern`
   / `...MismatchRd`).

A draft `scan regs [soc] | led <r|g|b|off> [ms] [soc] | line [r|g|b|dark]
[first] [count]` command implementing steps 1–5 exists outside the tree (not
in `fw/app` yet).

## Open questions

- Is the scan block clocked when the custom firmware runs (step 2)?
- Does `LED_FW_EN` alone light the LED, or is the SoC PWM needed; which PWM
  channel; LED current scale of the IDAC.
- Whether the Dasher oscillator training (step 6) is needed, and the purpose
  of Dasher `GPIO_CONFIG = 0x0320`.
- Which resolution the snapshot timing is; HP's other timing sets
  (`afe_timing`, UDW `scan sensor timing 10`).
- Whether the light-monitor sums need `ScanDPControl`/`ScanDatapathChEnable`
  bits, and whether they are per line or accumulated.
- The real scan-to-DRAM DMA (candidate `0xBD06C800` channels above).
- `ScanMotorDcSignalMux` (`0xBD303C00`, HP sets bit 9 for argument 1 at
  `0xa03b4022`)
  and `ScanMotorTypeMux` (`0xBD303800`): how the scan TG and Motor C are
  coupled.
