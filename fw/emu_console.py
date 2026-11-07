"""Interactive console on an emulated printer: keyboard -> UART RX, UART TX -> stdout.

    python -m fw.emu_console build/hpdj_flash.bin          # type at the app prompt
    python -m fw.emu_console build/hpdj_flash.bin --bl     # stop autoboot, BL prompt

Keys go straight to the firmware (which echoes them, so backspace and the
line editor behave like a serial terminal). A line that starts with "~" is a
console escape handled locally: "~send build/app.img" serves an XMODEM
transfer after you typed "update", "~cycle" power-cycles into the current
flash contents, "~flash out.bin" saves the flash image, "~quit" exits.
"""
from __future__ import annotations

import sys
import time
from collections.abc import Callable
from pathlib import Path

from .harness import HarnessError, VirtualPrinter, boot_to_app, boot_to_bootloader, REBOOT_MARKER

SLICE_INSTRUCTIONS = 400_000   # ~0.1 s of virtual time per idle slice


def _windows_key_reader() -> Callable[[], str | None]:
    import msvcrt

    def read() -> str | None:
        return msvcrt.getwch() if msvcrt.kbhit() else None
    return read


def _posix_key_reader() -> Callable[[], str | None]:
    import os
    import select
    import termios
    import tty
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    tty.setcbreak(fd)

    def read() -> str | None:
        if select.select([fd], [], [], 0)[0]:
            data = os.read(fd, 1)
            return data.decode('latin1') if data else '\x04'
        return None

    read.restore = lambda: termios.tcsetattr(fd, termios.TCSADRAIN, saved)  # type: ignore[attr-defined]
    return read


def default_key_reader() -> Callable[[], str | None]:
    return _windows_key_reader() if sys.platform == 'win32' else _posix_key_reader()


def interactive_loop(vp: VirtualPrinter, prompt: bytes,
                     key_reader: Callable[[], str | None] | None = None) -> VirtualPrinter:
    """Run until "~quit" (or EOF). ``key_reader`` returns one typed character or None."""
    reader = key_reader or default_key_reader()
    print('\n[console] attached. Escapes: ~send <img>, ~cycle, ~flash <out.bin>, ~quit', file=sys.stderr)
    vp.echo = True
    escape: str | None = None      # local line being typed after "~"
    at_line_start = True
    try:
        while True:
            ch = reader()
            if ch is None:
                try:
                    vp.run_steps(SLICE_INSTRUCTIONS)
                except HarnessError as exc:
                    print(f'\n[console] {exc}', file=sys.stderr)
                    return vp
                if vp.uart_bytes()[-200:].rstrip().endswith(REBOOT_MARKER):
                    print('\n[console] firmware requested a reset; power-cycling into current flash',
                          file=sys.stderr)
                    vp = vp.power_cycle()
                    vp.echo = True
                continue
            if ch == '\x04':            # Ctrl-D / EOF
                return vp
            if escape is not None:
                if ch in ('\r', '\n'):
                    sys.stdout.write('\n')
                    sys.stdout.flush()
                    line, escape, at_line_start = escape, None, True
                    vp = _handle_escape(vp, line)
                    if vp is None:
                        return None  # type: ignore[return-value]
                elif ch in ('\x08', '\x7f'):
                    escape = escape[:-1]
                    sys.stdout.write('\b \b')
                    sys.stdout.flush()
                else:
                    escape += ch
                    sys.stdout.write(ch)
                    sys.stdout.flush()
                continue
            if at_line_start and ch == '~':
                escape = ''
                sys.stdout.write('~')
                sys.stdout.flush()
                continue
            if ch in ('\r', '\n'):
                vp.send(b'\r')
                at_line_start = True
            else:
                vp.send(ch.encode('latin1', 'replace'))
                at_line_start = False
    finally:
        restore = getattr(reader, 'restore', None)
        if restore:
            restore()


def _handle_escape(vp: VirtualPrinter, line: str) -> VirtualPrinter | None:
    parts = line.split()
    if not parts or parts[0] == 'quit':
        return None
    if parts[0] == 'send' and len(parts) == 2:
        data = Path(parts[1]).read_bytes()
        try:
            vp.run_until(b'C', max_instructions=8_000_000)
            t0 = time.time()
            sender = vp.xmodem_send(data)
            print(f'\n[console] sent {len(data)} bytes in {len(sender.blocks)} blocks, '
                  f'{time.time() - t0:.1f}s', file=sys.stderr)
        except HarnessError as exc:
            print(f'\n[console] {exc}', file=sys.stderr)
    elif parts[0] == 'cycle':
        vp = vp.power_cycle()
        vp.echo = True
    elif parts[0] == 'flash' and len(parts) == 2:
        Path(parts[1]).write_bytes(vp.flash_image())
        print(f'\n[console] flash saved to {parts[1]}', file=sys.stderr)
    else:
        print('\n[console] escapes: ~send <img>, ~cycle, ~flash <out.bin>, ~quit', file=sys.stderr)
    return vp


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description='Interactive UART console on the emulated printer')
    parser.add_argument('image', type=Path, help='16 MiB flash image')
    parser.add_argument('--bl', action='store_true', help='interrupt autoboot and start at the BL prompt')
    args = parser.parse_args(argv)
    flash = args.image.read_bytes()
    try:
        vp = boot_to_bootloader(flash, echo=True) if args.bl else boot_to_app(flash, echo=True)
    except HarnessError as exc:
        print(f'[console] {exc}', file=sys.stderr)
        return 1
    interactive_loop(vp, b'BL> ' if args.bl else b'app> ')
    return 0


if __name__ == '__main__':
    sys.exit(main())
