"""Host-side serial tool for the HPDJ firmware (needs pyserial for the port commands).

    python -m fw.tool info build/app.img                # describe an image file
    python -m fw.tool console --port COM5               # serial terminal, 115200 8N1
    python -m fw.tool update --port COM5 build/app.img --run   # XMODEM update, then run it
    python -m fw.tool update --port COM5 build/app.img --slot b --reboot
    python -m fw.tool run --port COM5 build/app.img            # execute from RAM, no flash write
    python -m fw.tool ota build/app.img --host 192.168.0.123   # over Wi-Fi: write the other slot, reboot
    python -m fw.tool ota build/app.img --host 192.168.0.123 --ram   # over Wi-Fi: run from RAM only
    python -m fw.tool run --app --port COM42 build/app.img      # from RAM via the app (UART or USB)
    python -m fw.tool nvm --host 192.168.0.123                  # read and decode the settings EEPROM

"update" types "update [slot]" at whichever prompt answers (app> or BL>),
serves the XMODEM-1K transfer and prints the firmware's verification result.
"--run" then re-enters the bootloader ("bl") and types "boot", so the new
image starts without any reset; "--reboot" uses the watchdog instead. The
firmware never accepts writes below the bootloader protect boundary, so this
tool cannot replace the bootloader.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

from . import image as fwimage
from .xmodem import XmodemSender

PROMPTS = (b'app> ', b'BL> ')
# Any shell prompt: the bootloader ("BL> "), the full app ("app> "), the hello
# example ("hello> ") or your own application ("<name>> ").
PROMPT_RE = re.compile(rb'\n([A-Za-z][\w-]{0,15})> $')


def _open_port(port: str, baud: int):
    try:
        import serial  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on the host environment
        raise SystemExit('pyserial is required: python -m pip install pyserial') from exc
    return serial.Serial(port, baud, timeout=0.01, write_timeout=2)


def _read_until(ser, patterns: tuple[bytes, ...], timeout: float, echo: bool = True) -> tuple[bytes, bytes | None]:
    """Read until one of ``patterns`` terminates the stream. Returns (data, pattern)."""
    deadline = time.time() + timeout
    buf = bytearray()
    while time.time() < deadline:
        chunk = ser.read(256)
        if chunk:
            buf += chunk
            if echo:
                sys.stdout.write(chunk.decode('latin1'))
                sys.stdout.flush()
            for pat in patterns:
                if buf.endswith(pat) or pat in buf[-max(64, len(pat) + 8):]:
                    return bytes(buf), pat
    return bytes(buf), None


def _read_until_prompt(ser, timeout: float, echo: bool = True) -> tuple[bytes, bytes | None]:
    """Read until the output ends with any shell prompt. Returns (data, prompt)."""
    deadline = time.time() + timeout
    buf = bytearray()
    while time.time() < deadline:
        chunk = ser.read(256)
        if chunk:
            buf += chunk
            if echo:
                sys.stdout.write(chunk.decode('latin1'))
                sys.stdout.flush()
            m = PROMPT_RE.search(bytes(buf[-40:]))
            if m:
                return bytes(buf), m.group(1) + b'> '
    return bytes(buf), None


def _get_prompt(ser, echo: bool) -> bytes:
    for attempt in range(6):
        ser.write(b'\r')
        _, prompt = _read_until_prompt(ser, 1.0, echo=echo)
        if prompt:
            return prompt
        if attempt == 2:
            # Maybe the bootloader is counting down: any key stops autoboot.
            ser.write(b' ')
    raise SystemExit('no shell prompt from the printer (expected e.g. "app> ", "hello> " or "BL> " at 115200 8N1);'
                     ' close other terminal programs that hold the port')


def cmd_info(args) -> int:
    data = Path(args.image).read_bytes()
    print(fwimage.describe_image(data))
    return 0


def cmd_cmd(args) -> int:
    """Type shell commands one by one, printing everything until each next prompt."""
    ser = _open_port(args.port, args.baud)
    _get_prompt(ser, echo=True)
    for c in args.commands:
        ser.write(c.encode('latin1') + b'\r')
        _, prompt = _read_until_prompt(ser, args.timeout, echo=True)
        if prompt is None:
            print(f'\n[tool] no prompt after {c!r} within {args.timeout:.0f} s', file=sys.stderr)
            return 1
    print()
    return 0


def cmd_console(args) -> int:
    ser = _open_port(args.port, args.baud)
    print(f'[tool] connected to {args.port} @ {args.baud}; Ctrl-C to exit', file=sys.stderr)
    import threading

    def reader():
        while True:
            chunk = ser.read(256)
            if chunk:
                sys.stdout.write(chunk.decode('latin1'))
                sys.stdout.flush()

    threading.Thread(target=reader, daemon=True).start()
    try:
        for line in sys.stdin:
            ser.write(line.rstrip('\r\n').encode('latin1') + b'\r')
    except KeyboardInterrupt:
        pass
    return 0


def send_xmodem(ser, data: bytes, *, block_size: int = 1024, timeout: float = 120.0, echo: bool = False,
                primed: bool = False) -> XmodemSender:
    """Serve an XMODEM transfer. ``primed`` means the receiver's first 'C' was
    already consumed by the caller, so the first block is sent right away."""
    sender = XmodemSender(data, block_size=block_size)
    if primed:
        sender.feed(ord('C'))
        ser.write(sender.pending())
    deadline = time.time() + timeout
    last_report = 0.0
    while not sender.done and time.time() < deadline:
        chunk = ser.read(64)
        for b in chunk:
            sender.feed(b)
            out = sender.pending()
            if out:
                ser.write(out)
                deadline = time.time() + timeout
        if time.time() - last_report > 1.0:
            last_report = time.time()
            print(f'\r[tool] xmodem {sender.progress * 100:5.1f}% ({sender.index}/{len(sender.blocks)} blocks)',
                  end='', file=sys.stderr)
    print(file=sys.stderr)
    if not sender.done:
        raise SystemExit('xmodem transfer timed out')
    if sender.error:
        raise SystemExit(f'xmodem transfer failed: {sender.error}')
    return sender


def _enter_bootloader(ser, prompt: bytes, echo: bool) -> bool:
    """From the app prompt, type "bl" and wait for the bootloader shell."""
    if prompt == b'BL> ':
        return True
    ser.write(b'bl\r')
    _, pat = _read_until(ser, (b'BL> ',), 15.0, echo=echo)
    return pat is not None


def cmd_run(args) -> int:
    """Upload an image and execute it from RAM: through the bootloader's "run",
    or with --app through the running application's "ramrun" (no bootloader, so
    it also works over the USB console)."""
    data = Path(args.image).read_bytes()
    hdr = fwimage.validate_image(data, expect_type=fwimage.IMAGE_TYPE_APP)
    print(f'[tool] image: {fwimage.describe_image(data)}', file=sys.stderr)
    ser = _open_port(args.port, args.baud)
    prompt = _get_prompt(ser, echo=not args.quiet)
    if args.app:
        ser.write(b'ramrun\r')
    else:
        if not _enter_bootloader(ser, prompt, not args.quiet):
            print('[tool] bootloader shell did not answer after "bl"', file=sys.stderr)
            return 1
        ser.write(b'run\r')
    _, pat = _read_until(ser, (b'\r\nC',), 10.0, echo=not args.quiet)
    if not pat:
        raise SystemExit('the firmware did not start the XMODEM receiver')
    send_xmodem(ser, data, block_size=args.block_size, echo=not args.quiet, primed=True)
    if args.app:
        try:                                         # the USB console goes away and comes back
            out, _ = _read_until(ser, (b'from RAM',), 30.0, echo=not args.quiet)
        except Exception:
            out = b''
        started = b'ramrun: starting' in out
        print(f'\n[tool] {"handed over to the image in RAM" if started else "image was refused"}: '
              f'v{hdr.version_text} seq {hdr.sequence}', file=sys.stderr)
        return 0 if started else 1
    out, prompt = _read_until_prompt(ser, 60.0, echo=not args.quiet)
    started = b'starting application from RAM' in out and prompt is not None and prompt != b'BL> '
    print(f'\n[tool] {"image running from RAM" if started else "image did not start"}: '
          f'v{hdr.version_text} seq {hdr.sequence}'
          + (f', prompt {prompt.decode().strip()}' if started else ''), file=sys.stderr)
    for cmd in (args.then or []):
        ser.write(cmd.encode('latin1') + b'\r')
        _read_until_prompt(ser, 120.0, echo=not args.quiet)
    if args.then:
        print(file=sys.stderr)
    return 0 if started else 1


def cmd_update(args) -> int:
    data = Path(args.image).read_bytes()
    hdr = fwimage.validate_image(data, expect_type=fwimage.IMAGE_TYPE_APP)
    print(f'[tool] image: {fwimage.describe_image(data)}', file=sys.stderr)
    ser = _open_port(args.port, args.baud)
    prompt = _get_prompt(ser, echo=not args.quiet)
    print(f'[tool] shell prompt {prompt!r}', file=sys.stderr)
    cmd = b'update' + (b' ' + args.slot.encode() if args.slot else b'') + b'\r'
    ser.write(cmd)
    _, pat = _read_until(ser, (b'\r\nC',), 10.0, echo=not args.quiet)
    if not pat:
        raise SystemExit('firmware did not start the XMODEM receiver')
    sender = send_xmodem(ser, data, block_size=args.block_size, echo=not args.quiet, primed=True)
    out, _ = _read_until_prompt(ser, 180.0, echo=not args.quiet)
    ok = b'updated and verified' in out
    print(f'[tool] {"update verified" if ok else "UPDATE FAILED"}: v{hdr.version_text} seq {hdr.sequence} '
          f'({len(sender.blocks)} blocks)', file=sys.stderr)
    if b'warning: seq' in out:
        print('[tool] the firmware warned that this image will not be selected by the bootloader', file=sys.stderr)
    if ok and args.run:
        # Start the new image without a reset: re-enter the bootloader from
        # the app ("bl"), then "boot" the highest-sequence slot.
        if not _enter_bootloader(ser, prompt, not args.quiet):
            print('[tool] bootloader shell did not answer after "bl"', file=sys.stderr)
            return 1
        ser.write(b'boot\r')
        out, new_prompt = _read_until_prompt(ser, 60.0, echo=not args.quiet)
        started = new_prompt not in (None, b'BL> ') and b'starting application' in out
        print(f'[tool] {"new application is running" if started else "application did not start"}', file=sys.stderr)
        return 0 if started else 1
    if ok and args.reboot:
        ser.write(b'reboot\r')
        try:
            _read_until(ser, (b'[reboot]',), 5.0, echo=not args.quiet)
        except Exception as exc:                     # serial.SerialException: the USB port went away
            print(f'[tool] the port closed during the reboot ({type(exc).__name__}); '
                  'on the USB console that is expected', file=sys.stderr)
    return 0 if ok else 1


def cmd_ota(args) -> int:
    """POST the image to the printer's web server; it writes the inactive slot
    and, unless --no-reboot, restarts into it. Then wait for the new sequence."""
    import http.client
    import json
    import urllib.parse
    import urllib.request
    data = Path(args.image).read_bytes()
    hdr = fwimage.validate_image(data, expect_type=fwimage.IMAGE_TYPE_APP)
    print(f'[tool] image: {fwimage.describe_image(data)}', file=sys.stderr)
    query = urllib.parse.urlencode({'token': args.token, 'reboot': '0' if args.no_reboot else '1'})
    t0 = time.time()
    conn = http.client.HTTPConnection(args.host, args.http_port, timeout=120)
    path = f'/api/run?{query}' if args.ram else f'/api/update?{query}'
    conn.request('POST', path, body=data, headers={'Content-Type': 'application/octet-stream'})
    resp = conn.getresponse()
    body = resp.read().decode('utf-8', 'replace')
    conn.close()
    print(f'[tool] {resp.status} after {time.time() - t0:.1f} s: {body}', file=sys.stderr)
    try:
        res = json.loads(body)
    except ValueError:
        return 1
    if not res.get('ok'):
        return 1
    if args.ram:
        print('[tool] running from RAM; a power cycle returns to the flash slots', file=sys.stderr)
        return 0
    if args.no_reboot or not res.get('reboot'):
        return 0
    print('[tool] waiting for the printer to come back ...', file=sys.stderr)
    deadline = time.time() + args.wait
    while time.time() < deadline:
        time.sleep(2)
        try:
            with urllib.request.urlopen(f'http://{args.host}:{args.http_port}/api/status', timeout=3) as r:
                st = json.loads(r.read())
        except (OSError, ValueError):
            continue
        if st.get('seq') == hdr.sequence:
            print(f'[tool] running v{st["ver"]} seq {st["seq"]} from slot {st["slot"]} '
                  f'({time.time() - t0:.0f} s)', file=sys.stderr)
            return 0
    print('[tool] the printer did not answer with the new sequence in time', file=sys.stderr)
    return 1


def cmd_nvm(args) -> int:
    """Download the settings EEPROM (64 KiB, read-only) and the SoC ID words over
    Wi-Fi, save them, and decode the EEPROM with analysis/nvm_decode.py."""
    import json
    import subprocess
    import urllib.parse
    import urllib.request
    base = f'http://{args.host}:{args.http_port}'
    tok = urllib.parse.quote(args.token)
    out = Path(args.out)
    data = bytearray()
    for off in range(0, 0x10000, 2048):
        with urllib.request.urlopen(f'{base}/api/eeprom?token={tok}&off={off}&len=2048', timeout=20) as r:
            res = json.loads(r.read())
        if not res.get('ok'):
            print(f'[tool] eeprom read failed at {off:#06x}: {res}', file=sys.stderr)
            return 1
        data += bytes.fromhex(res['hex'])
        print(f'\r[tool] eeprom {len(data) * 100 // 0x10000:3d}%', end='', file=sys.stderr)
    print(file=sys.stderr)
    out.write_bytes(data)
    with urllib.request.urlopen(f'{base}/api/chipid?token={tok}', timeout=10) as r:
        words = json.loads(r.read())['words']
    chip = ','.join(words)
    Path(str(out) + '.chipid').write_text(chip + '\n')
    print(f'[tool] saved {out} ({len(data)} bytes) and {out}.chipid; both identify this printer, keep them private',
          file=sys.stderr)
    cmd = [sys.executable, '-m', 'analysis.nvm_decode', str(out), '--chip-id', chip]
    if args.show_identifiers:
        cmd.append('--show-identifiers')
    return subprocess.call(cmd, cwd=Path(__file__).resolve().parent.parent)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='HPDJ firmware host tool')
    sub = parser.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('info', help='describe an .img file')
    p.add_argument('image')
    p.set_defaults(fn=cmd_info)
    p = sub.add_parser('cmd', help='type shell commands and print the replies')
    p.add_argument('commands', nargs='+')
    p.add_argument('--port', required=True)
    p.add_argument('--baud', type=int, default=115200)
    p.add_argument('--timeout', type=float, default=30.0, help='seconds to wait for each prompt')
    p.set_defaults(fn=cmd_cmd)
    p = sub.add_parser('console', help='serial terminal')
    p.add_argument('--port', required=True)
    p.add_argument('--baud', type=int, default=115200)
    p.set_defaults(fn=cmd_console)
    p = sub.add_parser('update', help='send an application image over XMODEM')
    p.add_argument('image')
    p.add_argument('--port', required=True)
    p.add_argument('--baud', type=int, default=115200)
    p.add_argument('--slot', choices=['a', 'b'], help='target slot (default: the inactive one)')
    p.add_argument('--block-size', type=int, choices=[128, 1024], default=1024)
    p.add_argument('--run', action='store_true',
                   help='after a verified update start it via the bootloader shell (bl + boot), no reset needed')
    p.add_argument('--reboot', action='store_true', help='type "reboot" after a verified update (watchdog reset)')
    p.add_argument('--quiet', action='store_true', help='do not echo the printer output')
    p.set_defaults(fn=cmd_update)
    p = sub.add_parser('run', help='upload an app image and execute it from RAM (nothing written to flash)')
    p.add_argument('image')
    p.add_argument('--port', required=True)
    p.add_argument('--baud', type=int, default=115200)
    p.add_argument('--block-size', type=int, choices=[128, 1024], default=1024)
    p.add_argument('--app', action='store_true',
                   help='use the running application\'s "ramrun" instead of the bootloader (works over USB)')
    p.add_argument('--then', action='append', metavar='CMD',
                   help='command to type once the image is running (repeatable), e.g. --then "flash diag"')
    p.add_argument('--quiet', action='store_true', help='do not echo the printer output')
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser('ota', help='send an application image over Wi-Fi (HTTP) and reboot into it')
    p.add_argument('image')
    p.add_argument('--host', default='deskjet.local', help='printer address or mDNS name (default: deskjet.local)')
    p.add_argument('--http-port', type=int, default=80)
    p.add_argument('--token', default='hpdj2620', help="the firmware's web token ('server token' sets it)")
    p.add_argument('--no-reboot', action='store_true', help='only write the slot')
    p.add_argument('--ram', action='store_true', help='run the image from RAM (POST /api/run), write no flash')
    p.add_argument('--wait', type=float, default=60.0, help='seconds to wait for the new image to answer')
    p.set_defaults(fn=cmd_ota)
    p = sub.add_parser('nvm', help='download the settings EEPROM over Wi-Fi and decode it (read-only)')
    p.add_argument('--host', default='deskjet.local')
    p.add_argument('--http-port', type=int, default=80)
    p.add_argument('--token', default='hpdj2620')
    p.add_argument('--out', default='build/nvm.bin', help='where to save the 64 KiB image (default build/nvm.bin)')
    p.add_argument('--show-identifiers', action='store_true', help='do not mask the serial number and the like')
    p.set_defaults(fn=cmd_nvm)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == '__main__':
    sys.exit(main())
