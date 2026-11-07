# Interrupts: first hardware IRQ on the printer (2026-11-05)

The custom firmware runs with IRQ and FIQ masked and polls everything. The
`irq` command (`fw/app/irq.c`, `fw/app/irq.S`) proves the interrupt path on
the real printer: a timer tick through HP's interrupt controller, for a
bounded time, everything put back afterwards. RAM only: no flash writes.

## Result on the printer

`irq tick 1000 2` (image run from RAM with `fw.tool run`): 2000 ticks in 2 s,
period 999..1001 us, 0 spurious, 0 foreign, claim `0x0112` (priority 1,
source 18). Afterwards the vector literal is back, all sources are masked and
the CPU has I and F set again. Emulator: `tests/test_fw_irq.py`.

`irq demo 10`: the tick blinks the power LED (1 Hz) and the blue Wi-Fi LED
(4 Hz) from the interrupt; the main loop counts seconds on the panel digit and
at 5 s spins for 2 s without polling anything. On the printer 2048 ticks
happened during that blind stretch, 10048 ticks in 10048 ms overall.

## How it works

- Vectors: 8 KiB ITCM at address 0 (SCTLR.V clear). Every vector is
  `E59FF018` (`ldr pc,[pc,#0x18]`), so the IRQ vector jumps to the literal at
  `0x38`. On the printer (and in the emulator) that literal is `0x168`, HP's
  ITCM dispatcher; the FIQ literal `0x3C` is `0x1F8`. The firmware never uses
  HP's dispatcher: it swaps only the literal at `0x38` to its own `irq_entry`
  and restores the value it found.
- MPU region 1 (`0x0..0x1FFF`) is privileged read-only for data (AP 6). The
  swap masks I and F, sets region 1's data AP to 1 (c5,c0,2 bits 7:4) for the
  store, reads it back, restores c5 exactly and drains the write buffer. HP
  does the same store with the MPU off (`A000FCC0`).
- Interrupt controller `0xBD040C00`: mask words `C4C - 4*(src>>5)` (1 =
  masked; `C40`/`C44` do not take writes on the printer: sources 64..127 are
  apparently absent), priority bit 2 at `C0C - 4*(src>>5)`, 2-bit priorities at
  `CFC - 4*(src>>4)`, threshold `CC8`, IRQ claim `CB8` (high byte priority,
  low byte source; priority 0 or >= 4 = nothing). No end-of-interrupt write:
  the device is acknowledged.
- Timer channels `0xBD040800`: prescaler `80E` = 180 (1 us counts), reload
  `81C + 4*ch` (period - 1 us), group masks `804 + 2*g` route channels to
  INTC sources 18..22, pending `800`, write-1-to-clear acknowledge `802`,
  stop `812`. The test uses channel 7 in group 0 (source 18), which HP leaves
  idle.
- Handler (ARM, IRQ mode, own 1 KiB stack): read the claim, raise the
  threshold to the claimed priority while handling (as HP does), acknowledge
  the channel, restore, return with `ldmfd ..., pc}^`. A storm guard and a
  foreign-source guard mask everything if something unexpected fires.

## Before relying on interrupts

FIQ stays masked: its use is not explored. Interrupts are only enabled inside
the bounded `irq tick` command; using them permanently (a tick for the motor
loop or input buffering) needs every polled driver checked for re-entrancy,
and the vector literal must then stay installed, so a crash path would no
longer be a plain power cycle away from the old state.
