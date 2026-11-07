"""Record physical UART bytes without transmitting anything (e.g. a reboot)."""
import argparse
from pathlib import Path
import time

from capture_uart import Capture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=120)
    parser.add_argument('--port', default='COM16')
    args = parser.parse_args()
    capture = Capture(args.directory, args.port, 115200)
    capture.metadata['mode'] = 'passive-only; no transmitted bytes'
    try:
        capture.open()
        deadline = time.monotonic() + args.seconds
        next_update = time.monotonic() + 20
        while time.monotonic() < deadline:
            data = capture.device.read(min(max(capture.device.in_waiting, 4096), 16384))
            if data:
                capture.record('rx', data)
                capture.transcript.write(data.decode('latin-1'))
                capture.transcript.flush()
            if time.monotonic() >= next_update:
                print(f'Passive capture: {capture.offset} bytes', flush=True)
                next_update = time.monotonic() + 20
        capture.metadata['stop_reason'] = 'passive-recording-duration-complete'
    finally:
        capture.close()


if __name__ == '__main__':
    main()
