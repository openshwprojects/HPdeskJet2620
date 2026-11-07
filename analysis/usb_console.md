# USB serial console (experiment, 2026-11-05)

`usb on` makes the printer's USB device port a CDC-ACM serial port (USB IDs
`1209:0001`, the pid.codes test ID) that shows the same shell as the UART:
everything the shell prints is copied to the bulk IN endpoint, and what the
host types on bulk OUT goes into the shell. **It works on the printer**
(2026-11-05): Windows 10 binds its own CDC driver (COM42 here, high speed)
and the shell answers over USB in both directions, with the UART still
showing everything. Emulator test: `tests/test_fw_usb_console.py`.

## What the real controller needed (found on the printer, 2026-11-05)

Enumeration worked at once; the bulk endpoints needed HP's own sequence,
taken from the boot ROM USB stack (`.load_text`) and `udw20_drv.c`:

- At SET_CONFIGURATION each endpoint word gets the enable bit 7, bit 20 and
  the packet-size code in bits 4:0 (7 = 512 bytes at high speed, 4 = 64):
  `(w & 0xFF7FFFE0) | 0x00100080 | code` (ROM `0xA000EE0C`, udw20 `0xA0196DF6`).
  The words copied from HP's snapshot lacked bit 7 because that snapshot had
  no host attached. Words on the printer: EP1 `0x011000C7`, EP2 `0x051005C7`,
  EP3 `0x051005E4`.
- `0xBD09FC08` is the configuration value (set to 1), not the address: the
  hardware answers SET_ADDRESS itself.
- Bulk IN: before each DMA the endpoint's bit in `0xBD09FC1E` must be set (ROM
  `0xA000F324`); without it the data stays in the endpoint FIFO (`+0x10`
  counted 0xA7 bytes waiting). Then `+0x24` address, `+0x28` length,
  `+0x2E` = 1; completion in `0xBD09FC24` (status; `0xBD09FC26` is its
  enable), acknowledged with `+0x20` = 1.
- Bulk OUT: a DMA armed before the data arrives does not end on a short
  packet; the bytes land as they come (`+0x24` advances, `+0x28` counts down,
  e.g. 512 -> 500 after 12 typed bytes), so the driver takes them as the
  count moves and re-arms once the 512-byte buffer is full.
- Still unknown: the meaning of endpoint word bits 22, 23, 26, and of
  `+0x08` bit 8 (the ROM waits on it before an IN send).

## What it is built on

- The emulator's controller model `usb.py`, reverse-engineered from HP's
  `udw20_drv.c` (`0xA0196000..`) and the boot ROM's USB recovery stack:
  endpoint blocks at `0xBD09BC00 + 0x400 * ep`, global block at `0xBD09FC00`
  (SETUP packet at `0x10/0x14`, EP0 handshake `0x1A`: 1 = acknowledge,
  3 = stall, RX-ready/enable `0x1C/0x1E`, DMA-done/enable `0x24/0x26`,
  events `0x28`: bus reset bit 3, SETUP bit 8; pull-up `0x0E` bit 0).
- HP's own register state after its USB start-up (Stage-22 snapshot): global
  control `0x0060130F`; endpoint words `0x01008044` (bulk OUT), `0x05100544`
  (bulk IN), `0x05100564` (EP3, probably interrupt IN), and per-endpoint maps
  such as `0x0001 0080`, `0x0002 0280`, `0x0003 0480` (endpoint number, then
  what looks like a FIFO offset; the high byte is taken to be the interface).
  The custom firmware copies these for its EP1/EP2/EP3.

## Behaviour

Polled from the idle loop: bus reset, GET_DESCRIPTOR (device, configuration
with 512-byte bulk packets at high speed / 64 at full speed, strings),
SET_ADDRESS (applied after the status stage), SET/GET_CONFIGURATION,
GET_STATUS, CDC SET/GET_LINE_CODING and SET_CONTROL_LINE_STATE; anything else
is stalled. DMA buffers are in the uncached DRAM alias at `0x60810000`, like
the SDIO buffer that works on the printer. An 8 KiB ring holds output until
the host reads it.

## Unknown before the printer test (answered above)

- Clock and PHY start-up: HP's driver may enable clocks or the PHY before the
  registers above respond; the model needs none of that.
- The meaning of the endpoint words beyond what HP uses.
- Whether the controller answers SET_ADDRESS by itself (the model does that
  only when the SETUP event interrupt is enabled; the custom firmware keeps
  interrupts off and handles it).

On the printer: `usb on`, then the PC shows a new serial port; `usb` shows
bus resets, SETUP count, bytes in/out and the configuration register.
