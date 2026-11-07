# Base boot observation and optional updated-image analysis

## Observed base-image handoff

`artifacts/deep-native.json` records the actual reset run reaching base entry
`A04E2E05`. Its last UART line is `[Boot] calling (*0xa04e2e05)()`.
At 94,177,440 instructions and 933 UART bytes it stops at **`A0177664`**,
a halfword store of zero to `BD040804`. This is the base timer initializer,
not the updated image's `A0177924`. The flash remains unchanged.

NVM update status `AA` skips the external footer before validation: the test
at `A000AC68..AC7A` finds slot zero's even bit zero and odd bit one. Embedded
candidates `00B40000` and `00C40000` also lack valid headers. Neither RFIX
nor GHS is selected. Load mode four alone does not cause the skip. Exact
branch evidence is in `analysis/storage_path.md`.

Direct disassembly of the extracted **base** `.text` confirms the following
correspondences. The timer methods use the same MMIO algorithm in both images.

| Function/data | Observed base image | Optional updated image |
| --- | --- | --- |
| Entry (Thumb flag) | `A04E2E05` | `A04E3DD9` |
| First runtime initializer, ARM | `A04E4E30` | `A04E5E04` |
| Standard I/O initialization | `A04E2FB4` | `A04E3F88` |
| Application main | `A03D133C` | `A03D22AC` |
| Kernel initialization | `A04E4F54` | `A04E5F28` |
| IRQ/timer setup chain, ARM | `A04E727C` | `A04E8250` |
| Timer group-mask initialization | `A0177628` | `A01778E8` |
| First mask store PC | `A0177664` | `A0177924` |
| Timer allocation | `A0177670` | `A0177930` |
| Timer enable / disable | `A01776BA` / `A01776DE` | `A017797A` / `A017799E` |
| Group lookup | `A0177734` | `A01779F4` |
| Group ISR | `A01777A0` | `A0177A60` |
| Callback registration | `A01777D8` | `A0177A98` |
| Kernel tick ISR / setup / stop | `A02F66F0` / `A02F6704` / `A02F6744` | `A02F6A34` / `A02F6A48` / `A02F6A88` |
| Allocation state | `2083D134` | `20838854` |
| Channel-to-group bytes | `20975750` | `209715D0` |
| Group records | `2097575C` | `209715DC` |
| Callback table | `20975768` | `209715E8` |

Address differences vary across the image; do not apply one relocation delta
to all methods. The remaining detailed analysis uses the **optional updated
image**, except unchanged boot addresses. It is supporting static evidence,
not the recorded execution path.

## Optional updated-image evidence

This is static analysis of the private image produced by `analysis/ghs_update.py`.
Both stored runtime checksums match; the updated checksum is `06EDF545` over
15,619,562 bytes. The production emulator must execute whichever path the
original loader selects; the current run does not select this update. The
private image is only a disassembly source.
Addresses below are updated runtime addresses, and code is big-endian ARM/Thumb.

## Immediate entry path

The GHS stream changes the entry from `A04E2E05` to `A04E3DD9` (Thumb flag set).
The following chain executes before the main application thread:

| Address | Observed action |
| --- | --- |
| `A04E3DD8` Thumb | Saves entry arguments, calls `A04E5E04`, then `A04E3F88`, then `A03D22AC`. |
| `A04E5E04` ARM | Calls heap bookkeeping `A042BE7C`, kernel initialization `A04E5F28`, heap setup `A042BDAC`, and a mutex initializer. |
| `A04E5F28` ARM | Writes initialization marker `F0F0F0F0` at `60000AE0`, calls `A04E83D0` and `A04E8FF4`, then writes `F0F0F0F1`. |
| `A04E83D0` / `A04E8250` ARM | Saves stack state, installs IRQ entry/exit hooks `00000B94` / `00000A48` through boot function `A0005EB0`, records mode stack limits, installs low-vector target `00001440`, then calls `A042BC00`. |
| `A042BC00` Thumb | Calls `A02F6A48(1)`, which allocates and configures the initial kernel timer. |
| `A04E3F88` Thumb | Initializes standard-I/O RAM structures and mutex references. |
| `A03D22AC` Thumb | Sets a constructor flag, executes the constructor list beginning at `A0694C58`, clears the flag, and calls kernel start `A04E5ECC`. Constructor contents are not exhaustively analyzed here. |
| `A04E5ECC` ARM | Calls `A03D22C2` to create the startup thread with entry `A03D20D9`; eventually enters the low-ITCM scheduler `00000C10`. |

The earliest predicted **new MMIO stop** is `A0177924`, a halfword store of zero
to `BD040804`, inside the first timer allocation. This is a static prediction,
not an observed successful loader handoff. Interrupt-priority accesses through
`BD040CC8` also occur on this path, but the boot code already uses that address.

There is no direct new UART write in this small entry chain. The startup thread
at `A03D20D8` creates queues, iterates a module initialization catalog around
`A04F8F00..A04F94B0`, and then waits for queue commands. Reaching it requires
faithful CPU mode/state behavior and eventually live interrupts.

## Programmable timer driver

`A0177930` allocates channels. Its initialized counter at `20838854` is ten;
the first allocation decrements it to nine and returns `9 - counter = 0`.
The capability functions `A0250FD0` and `A0250FDE` really are compiled stubs
returning `FFFFFFFF` in this updated image. Their default branches therefore
apply; no emulator substitution is needed.

The first allocation calls `A01778E8`, which stores zero to five halfword
group masks at `BD040804`, `806`, `808`, `80A`, and `80C`. This initialization
does **not** clear status, reloads or counters, so it does not establish their
physical power-on values.

`A0177A98(channel, priority, callback)` assigns a group matching the priority,
stores its callback at `209715E8 + 4*channel`, and registers group interrupt
`18 + group` through boot function `A0005C04`. Group bookkeeping consists of
five two-byte records at `209715DC`. The initial request is priority one and
selects group zero. Channel-to-group bytes are stored at `209715D0`.

| Register | Width | Evidence and interpretation |
| --- | --- | --- |
| `BD040800` | 16 | Pending-channel bitmap, read at `A0177A74`. Dispatcher ANDs this with its group mask and walks set bits. High confidence. |
| `BD040802` | 16 | Acknowledge bitmap: callbacks write `1 << channel`, including kernel tick at `A02F6A40` and periodic software timer at `A042ECEC`. Clearing those pending bits is strongly supported; readback behavior is not established. |
| `BD040804 + 2*group` | 16 | Group enable/routing mask. `A0177992/96` reads, ORs channel bit and writes; `A01779B8/BC` clears it. Five groups. High confidence. |
| `BD04080E` | 16 | Clock scaling value. A short-delay loop at `A042EE3E..52` multiplies the requested interval by this value and compares elapsed `BD040818`. Exact divider rule is unverified. |
| `BD040812` | 16 | Per-channel clear/stop command written by channel-disable `A01779D6`, in addition to acknowledging `BD040802`. Whether it clears a second pending bank or resets the countdown needs more evidence. |
| `BD040814` | 32 | Free-running upward time counter; differences are used extensively for delays/deadlines. The timer API treats one million of these units as one second. |
| `BD040818` | 32 | Faster upward counter used by short-delay loops with `80E` scaling. No direct reset or exact clock relationship established here. |
| `BD04081C + 4*channel` | 16 | Periodic reload. Initial tick writes 16665; another periodic user writes 1999 and advances its software epoch by 2000. A period of reload + 1 is strongly supported. |
| `BD04081E + 4*channel` | 16 | Writable current countdown / one-shot start value. Zero cancels an active one-shot; positive values schedule its next expiry. Strong behavioral inference; exact readback and one-shot off-by-one remain unverified. |

The allocator's ten-channel software capacity is not by itself a hardware
register map. In particular `BD04083C`, algebraically channel-eight reload,
also has an existing boot reset/scratch use. Do not blindly widen the complete
timer bank from this formula without reconciling that overlap and actual accesses.

### Initial tick: exact writes and ISR

`A02F6A48(1)` calls the allocator, stores channel zero at `20BDD8D8`, and calls
`A0177A98(0, 1, A02F6A35)`. It computes `1000000 / (60 * 1) = 16666`, subtracts
one when greater than one, and at `A02F6A7C` stores `16665` (`4119` hex) to
`BD04081C`. `A017797A(0)` then sets bit zero in `BD040804`.

The callback `A02F6A34` writes bit zero to `BD040802` and calls `A04E5CA0`,
which increments kernel tick count `20EF4E88`, updates timeout counters and
flags pending timer work. It does not reload or rewrite the timer. Thus this
timer must produce recurring expirations from the reload value. Disabling the
initial tick at `A02F6A88` writes zero to both reload and countdown; merely
clearing the group mask is a different operation.

### Independent reload/countdown corroboration

`A042EDB8` sets one timer's reload to `1999` (`07CF` hex), registers callback
`A042EC91`, and enables its group bit. That callback reads `BD040814`, advances
its software timestamp by `2000`, and acknowledges `BD040802`; it does not
rearm. This independently supports periodic reload + 1 semantics.

The same initializer sets another timer's reload and countdown to zero,
registers callback `A042ED39`, and enables its group bit. The callback:

1. Writes zero to its countdown at `A042ED48`.
2. Compares queued absolute deadlines against `BD040814` and services due work.
3. Acknowledges its pending bit at `A042ED94`.
4. If work remains, computes deadline minus current time, clamps to at least
   two, and writes that value to the countdown at `A042EDB2`.

When a new earliest event enters the queue, `A042EF80` and `A042F26E` write
one to this countdown to request prompt service. This behavior distinguishes
the countdown from a simple enable/control register. Zero reload permits a
one-shot triggered by a positive countdown write. Whether writing a nonzero
reload also starts/restarts an existing countdown, or start occurs on group
enable, cannot be distinguished by the initial periodic setup alone.

An emulator model should advance these timers from the same virtual-time basis
as the free-running counter, latch pending bits on expiry, preserve them until
acknowledged, and derive the group interrupt line from pending AND group mask.
Those are behavioral inferences from the callers. Physical clock calibration,
power-on counts, exact phase on reload writes, expiry/ack races and undocumented
register readback remain unresolved and should be labeled as such.

## Interrupt controller and CPU requirements

Boot initialization `A0005B58` creates these DTCM pointers dynamically:

| DTCM location | Value | Use |
| --- | --- | --- |
| `60000000` | `BD040C00` | Interrupt-controller base. |
| `60000004` | `BD040CC8` | Halfword current priority threshold. |
| `60000008` | `BD040CC0` | Halfword FIQ claim/status read through banked `r8`. |
| `6000000C` | `BD040CB8` | Halfword IRQ claim/status. |

The private GHS reconstruction does not execute boot and therefore its DTCM
pointer words are initially zero. Their runtime values above come from the
actual boot instructions, not from the offline snapshot.

Low-ITCM function `00000130`, reached through veneers `A00107D4` and
`A00292A8`, saves CPSR, masks IRQ/FIQ while modifying the controller, reads the
old threshold at `BD040CC8`, writes the requested threshold, executes CP15
drain-write-buffer (`c7,c10,4`), restores CPSR and returns the old threshold.
Consequently those veneers are not merely CPU interrupt-mask operations.
The timer driver temporarily uses threshold six; boot registration uses seven.

For initial timer group source 18 and priority one, `A0005C04`:

- Stores callback `A0177A61` into `60000010 + 18*4 = 60000058`.
- Through `A0005C4C`, reads/writes `BD040C0C`, clearing bit 18 (priority's bit 2).
- Reads/writes `BD040C4C`, clearing bit 18 (unmask source).
- Reads/writes `BD040CF8`, replacing bits 5:4 with one (priority's low two bits).
- Uses `BD040CC8` save/raise/restore around the changes.

General source addressing is `C0C - 4*(source>>5)` for priority bit 2,
`C4C - 4*(source>>5)` for mask, and `CFC - 4*(source>>4)` for two-bit priority
fields at `2*(source&15)`. The initializer writes all ones to mask words,
zeros to priorities, and halfword `BD040CB0 = 2`. Meaning of the latter
control bits beyond this observed initialization is not established.

IRQ dispatch at low ITCM `0000016C` reads `BD040CB8` at PC `00000178`. It
interprets the high byte as priority and the low byte as source. Priority zero
or >=4 exits without calling a handler; priorities one through three call
the handler table entry and temporarily raise the current threshold to that
priority. For initial timer group zero, a claim value `0112` is therefore
consistent with the firmware encoding, **only when a real pending unmasked
source 18 of priority one exists**. Returning a constant claim would be wrong.

FIQ dispatch `000001F8` reads `BD040CC0` at `0000020C`, checks a nonzero high
byte, and similarly dispatches the low-byte source. Priority bit 2 naturally
selects the upper priority class/FIQ. Priority eligibility above the threshold
is a strong inference from masking with six/seven and allowing nested higher
priorities; tie breaking, claim side effects and precise IRQ/FIQ arbitration
need validation. No controller end-of-interrupt write is visible in either
common dispatcher; the device callback acknowledges the device's pending bit.

The main runtime installs IRQ pre/post hooks `B94` and `A48` in the common
dispatch path. These save SPSR, switch between IRQ/SVC/system modes, adjust
banked stack pointers, and return with `MOVS pc,lr`; the boot wrapper instead
returns with `SUBS pc,lr,#4`. FIQ setup `A0005E5C` initializes banked r8-r10.
Correct ARM banked-register, SPSR/CPSR, exception LR and state-restoring return
behavior is necessary. Interrupts must enter through actual vectors and allow
these original handlers to run, rather than calling timer callbacks directly.

The scheduler at `00000C10` enables IRQs while checking the runnable-thread
pointer at `20EF490C`; its idle loop is `00000C2C..34`. A future stop there
can mean no runnable thread until a timer/device interrupt arrives. A stop in
main constructors or startup-module initialization may expose other devices
first; no claim is made that the timer alone completes application startup.
