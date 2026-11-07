# DC motors: how HP drives them, and physical confirmation (2026-11-05)

Status: **all three MCASIC motors work on the physical printer** (confirmed by
the owner 2026-11-05 evening, driven from the Wi-Fi web page
`http://<printer>/`): Motor A (`0x31`, paper feed / pick rollers), Motor B
(`0x33`, printhead carriage) and Motor C (`0x35`, scanner). The working
sequence is `motor init` (MCASIC `0x20/0x21/0x30/0x38..0x3a`, HP's order) and
then a plain DC control word `(mode << 12) | pwm` on the motor's register, the
same for all three. Drivers: `motor pulse`, `motorsAndButtonsTest` (front-panel
button holds) and the web API `/api/motor` (see "Web control" below).

## Motor drivers live in the MCASIC

The MCASIC (ID `0x8014`, MICCI controller `0xBD300800`, bus 0, device 6, HP
handle `0x0406`) contains the H-bridges. HP's health check
(`hw_mcasic_health_check.c`, strings at `0xa01701b0`) reports Motor A,
Motor B, Motor C (A and B side) and Motor D faults, plus a "PASS SW A" switch,
and decodes reset codes `0x102000` MtrA_Flt, `0x104000` MtrB_Flt,
`0x108000` MtrCA_Flt, `0x110000` MtrCB_Flt. The chip family names in that code
are BARACUS, HANNIBAL and TEMPLETON.

## Control word and registers

HP's servo PWM function `0xa02bdbf0(servo, pwm, mode)` handles output types
1..7 through `0xa016edfa`: control word = `(mode << 12) | (pwm & 0x3ff)`,
written to `descriptor[0x116 + index]` of the MCASIC board descriptor at
`0x200014d4` (index from the table at `0xa05b518b`: type 1 -> 0, type 2 -> 1).
Modes 1 and 2 are the two directions (HP swaps them when a servo is flipped),
0 is off. HP's own reports print "max pwm = 511".

| Motor | Control | Status (health check) | Fault mask | HP servo |
| --- | --- | --- | --- | --- |
| A | `0x31` | `0x32` | `0x0001` | servo 0 (kind 5001, type 1) |
| B | `0x33` | `0x34` | `0x0001` | servo 1 (kind 5000, type 2) |
| C | `0x35` | `0x37` | `0x0002` / `0x0001` | not configured |

The servo table is `0xa05b5408` (copied to RAM `0x20849954`, 0x20 bytes per
servo). Servo 1's position counter is SoC channel 10 (`0xBD300038`); channels
7 and 8 read `0xBD302010` / `0xBD302410` (16-bit, extended in software),
preset at `+8`, enable bits `0x40` / `0x80` in `0xBD303C00` (driver
`0xa017740c`..`0xa01774ac`).

Other output types: 12/13 write an 8-bit duty and its complement to MCASIC
registers (`0xa016eda8`); 14..23 use SoC PWM channels (`0xa017758e`, duty
written as `0xfff - pwm * 8`) with a GPIO for direction.

## MCASIC power rails and power domains

The descriptor holds three rail records of 0x22 bytes from offset `0x52`:
`main` (register `0x08`), `aux` (`0x09`), `core` (`0x0a`). HP's enable routine
`0xa016ebf8` sets the record's enable bit (`main` 0x8000, `aux` and `core`
0x0001), waits 50 ms, writes the record's clear bits (`main`/`aux` 0x0900,
`core` 0x0440) and checks fault bits up to three times. HP stage-1 writes
`09=009f, 08=0900, 09=099f, 0a=0440` (seen in the emulator, also when it boots
the custom image). The printer reads back `08=801a 09=009f 0a=0801`: the
registers read as status, not as the written value.

HP's power device table (names at `0x20869cf4`, entries at `0x20ef7a10`, 0x1c
bytes) configures on this printer:

| Index | Name | What it does |
| --- | --- | --- |
| 0 | `POWER_CARRIAGE_SERVO` | `0xa04e2b28`: MCASIC wake (`06=0`, 10 ms, `0c &= ~0x0cf0`, restore registers) or sleep (save, `0c |= 0x0cf0`, `06=0x000b`) |
| 7 | `POWER_PICK_ENCODER` | MCASIC register `0x3a` bit `0x8000` |
| 8 | `POWER_SCAN_ENCODER` | MCASIC register `0x3a` bit `0x4000` |
| 16 | `POWER_PEN_VPP_LOW` | rail `aux` enable |
| 17 | `POWER_PEN_VPP_32V_LOW` | `0xa02564f4` / `0xa02564fc` |

`POWER_PAPER_SERVO` has no entry. Registers `0x39`/`0x3a`/`0x3b` are the
MCASIC's GPIO direction, output and input registers (HP GPIO type 3); `0x38`
is listed next to them in the descriptor.

## Tests on the printer

The application's `motor` command (`fw/app/periph_cmds.c`) writes one control
word, waits, and switches every motor off; `motor show` reads the registers.

| Pulse | Result |
| --- | --- |
| A fwd, pwm 150, 40 ms | nothing |
| A fwd and B fwd, pwm 300, 80 ms | nothing |
| A fwd and B fwd, pwm 500, 200 ms | nothing, control word read back during the pulse, status 0 |

Before and after: `06=0000 08=801a 09=009f 0a=0801 0c=8000`, `0x20..0x3b` all
zero, supply 21.5 V. The MCASIC is awake (register `06` is not `0x000b`) and
the wake mask bits in `0c` are clear.

## Registers HP's running firmware had set and ours never wrote (2026-11-05 night)

Comparing the MCASIC registers in the Stage-22 snapshot (HP's firmware after
its boot, emulator) with the custom application's state after boot:

| Register | HP snapshot | Custom firmware | Meaning (from HP's board descriptor and code) |
| --- | --- | --- | --- |
| `0x20` | `0xc070` | 0 | first of a per-motor list `0x20..0x23` stored right before the motor control registers in the descriptor (`+0x110`); HP clears bit 15 around an ADC reading (`0xa016ecf0`) and sets it again |
| `0x21` | `0x0101` | 0 | second entry of that list |
| `0x30` | `0x8000` | 0 | stored just before `0x31/0x33/0x35` in the descriptor (`+0x115`) |
| `0x38` | `0x0030` | 0 | MCASIC GPIO block (function?) |
| `0x39` | `0x0030` | 0 | MCASIC GPIO direction |
| `0x3a` | `0xc000` | 0 | MCASIC GPIO output: bits 15/14 = pick and scan encoder power |

The rails (`0x08..0x0a`), `0x0c` and the ADC registers match, and so do the
MICCI controller settings and the SoC GPIOs apart from the Wi-Fi pins.

HP's MCASIC wake routine (`0xa0170ef6`, the `POWER_CARRIAGE_SERVO` wake)
shows how this group belongs together. It writes 0 to the sleep register
(descriptor `+0x2a` = `0x06`), waits 10 ms, clears the wake mask in `0x0c`
(`0xa0170eda`), and, if registers were saved before sleeping (struct at
`0x2096d32e`, flag byte first), restores them. With the descriptor bytes at
`+0x10e` (`01 01 20 21 22 23`): `0x20 &= 0xffff7fff` (enable bit 15 off),
`0x21`, `0x22`, `0x23`, then `0x20` with its full value (bit 15 on), a read of
`0x24`. Then a loop over blocks of 16 registers selected by the descriptor
masks at `+0x2e` (`5400 0000 3c40 036b`), which restores `0x0a 0x0c 0x0e`,
`0x26 0x2a..0x2d` and `0x30 0x31 0x33 0x35 0x36 0x38 0x39`, plus register
`0x10` (descriptor `+0xda`). Elsewhere (`0xa02bd63a`) HP resets the four
encoder peak detectors `0x2a..0x2d` with `0x80`.

In the snapshot the save struct is all zeros (HP never put the MCASIC to sleep
there), so the values above were set by HP's original initialisation, which
was not found tonight. A motor-driver configuration (`0x20..0x23`) whose
enable bit was never set fits "registers latch, nothing moves". Not proven:
the snapshot came from an older emulation run, and today's cold boot asserts
in `hw_watchdog.c` before HP's main firmware gets there.

Experiment (custom firmware, `motor init`): switch every motor off, then in
HP's order `20 &= 7fff`, `21=0101 22=0000 23=0000`, `20=c070`, `30=8000`,
`38=0030 39=0030 3a=c000`, each read back. Then a short pulse as before, only
after the owner says "go":

```
motor init
motor pulse a fwd 150 40
```

## Confirmed on the physical printer (2026-11-05)

Running `motor init` on the printer gave:

- `0x20 <- 0x0000`, `0x21 <- 0x0101` (reads back `0x0001` — bit 8 self-clears),
  `0x22 <- 0x0000`, `0x23 <- 0x0000`, `0x20 <- 0xc070` (reads `0xc070`),
  `0x30 <- 0x8000` (reads `0x8000`), `0x38 <- 0x0030` (reads `0x0030`),
  `0x39 <- 0x0030` (reads `0x0000` — write-only), `0x3a <- 0xc000` (reads `0xc000`).
- MCASIC GPIO input register `0x3b` changed from `0x0000` to `0x0430`.
- Immediately afterwards, `motor pulse a fwd 150 40` and `250 60` moved
  **Motor A**.
- With `motorsAndButtonsTest` (default PWM 350, front-panel button holds mapped
  to Motor A/B/C fwd/rev):
  - **Motor A (`0x31`, `Cancel` / `Resume`)** drives the **paper feed / pick
    rollers** in both directions (in one direction spinning the rollers freely,
    in the other swinging the pendulum gear to engage the pick clutch, which
    needs ~350 PWM to overcome the mechanical load).
  - **Motor B (`0x33`, `Wireless` / `Info`)** drives the **printhead carriage**
    left and right.
  - **Motor C (`0x35`, `Black Copy` / `Color Copy`)** was silent in this first
    `motorsAndButtonsTest` session. The guess then was that the flatbed scanner
    is a two-coil stepper (`MtrCA_Flt` / `MtrCB_Flt` across `0x35` and `0x36`),
    or that `0x22` in the `0x20..0x23` block still `0x0000` had to be set. Both
    turned out unnecessary: see the next section.

## Web control and Motor C (2026-11-05 evening)

With the printer on Wi-Fi (`wifi join`, DHCP), the app's HTTP server
(`fw/app/net.c`) serves a control page at `/` and a JSON API:

| Request | Action |
| --- | --- |
| `/api/status` | `inited`, active motor/mode/PWM, door, sensors, RSSI, MCASIC `00/20/30/3b`, control `0x31/0x33/0x35`, status, encoders, counters |
| `/api/motor?act=init` | `motor_api_init()` = `motor init` |
| `/api/motor?act=move&m=a\|b\|c&dir=fwd\|rev&pwm=N&ms=N` | `motor_api_drive()`: one control word, auto-off lease (`MOTOR_WEB_MAX_MS`), hold limit `MOTOR_HOLD_MAX_MS` (reply `hold limit reached`), PWM capped at `MOTOR_MAX_PWM` |
| `/api/motor?act=stop` | all three control registers to 0 |
| `/api/lcd?ch=X[&wifi=&color=&black=]` | panel digit / icons |

The page's HOLD buttons repeat `act=move ... ms=280` every 130 ms while held
and send `act=stop` on release. Space/Escape stop everything.

From this page the owner confirmed **all three motors move in both directions,
Motor C (scanner carriage) included**. The control word and init are the same
as for A and B; no extra write to `0x22`/`0x36` was needed. So Motor C behaves as
a DC motor on `0x35`, like A and B. Status after the session: `inited 1`,
`3b=0x0430`, `enc=007f/0000/007f/0000`, counters `000097d8/00000000/000011e1`.
Open question: why Motor C stayed still in the first `motorsAndButtonsTest`
run. Possibly the PWM was too low for the scanner's load, or the button mapping
for `Black`/`Color Copy` was wrong. Not investigated.

## Why the emulator cannot show the missing step

HP's firmware never writes a motor register in the emulator: not during cold
boot (only stage-1's 16 MCASIC writes), not during report 974 (print) and not
during report 926 (clean). The servo table entries for lib_servo outputs stay
unassigned there, so the mechanism initialisation does not run.

## Next evidence to collect

The missing-enable question is answered: `motor init` is enough for A, B and C.
What remains open is closed-loop control (encoder feedback, homing, end stops),
which needs the position counters mapped to motors.

Parked on 2026-11-05: the owner chose not to do the CH341 round trip for now.

HP's shell has `hw_mcasic.read_all_registers`. Running it under HP's own
firmware on the printer, after the power-on carriage move, would show the
register state in which the motors work; comparing it with the table above
should expose the missing enable.
