# Next steps (updated 2026-11-05 evening)

The checklist prepared the night before (panel menu, sensors, Wi-Fi join, access
point, motor init, USB console, scanner block read) has been worked through on
the printer; see `fw/README.md` for what works. Open items, roughly in the
order they make later experiments safer:

| # | Item | Notes |
| --- | --- | --- |
| 1 | Automatic rollback after an update | The bootloader in flash cannot be updated and does not fall back to the other slot by itself (it stops in its shell after 3 failed starts). So the application has to do it: a freshly written image runs "on trial" and gives itself up if it does not reach the network or USB within about a minute. |
| 2 | Motor safety | Scanner (C) homing ran its full 10 s: its encoder keeps counting at the end of travel, so end-stop detection does not work there. Shorter limit for C, home/range only where the encoder stops, and run stall checks from the timer interrupt so a move no longer blocks the network. Motor A is not to be moved; B and C only gently and after asking (owner's rule). |
| 3 | Scanner single line | Front end ("Dasher", MICCI ctrl0 bus 1 dev 1) answers `0x800c`. Next per `analysis/scanner.md`: read-only SoC scan-block reads (may need a power cycle if unclocked), one LED for under a second, then `scan line`. |
| 4 | Printhead carriage ASIC | Does not answer on MICCI yet, probably unpowered. Research first (HP's power sequence for the carriage), the first step towards anything print-related. |
| 5 | Motor A encoder | Not on encoder channels 7, 8 or 10. Find where HP reads the paper-feed position. Research only. |
| 6 | Wi-Fi comfort | Access-point fallback when the router is not found at boot, WPA2 on the access point by default, a web form to set the network. Settings that survive a reboot need a flash sector chosen with care. |
| 7 | Interrupts beyond the demo | `irq` proves the path (`analysis/interrupts.md`); permanent use needs every polled driver checked for re-entrancy. |
| 8 | Small | measure the CPU core clock against the 1 MHz counter; Device Manager screenshot of the USB port for `photos/`. |
