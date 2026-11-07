"""Capture real printer replies, one explicit UART command at a time.

This tool never imports the emulator or synthesizes firmware responses.
It stops a batch if the shell prompt does not return. Commands must be supplied
explicitly; this script does not generate arguments or enumerate by execution.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import time
from datetime import datetime, timezone

import serial


def utc():
    return datetime.now(timezone.utc).isoformat()


class Capture:
    def __init__(self, directory, port, baud, character_delay=0.003, validator=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.events = (self.directory / 'wire.jsonl').open('w', encoding='utf-8')
        self.raw = (self.directory / 'rx.bin').open('wb')
        self.transcript = (self.directory / 'transcript.txt').open('w', encoding='utf-8')
        self.offset = 0
        self.results = []
        self.character_delay = character_delay
        self.validator = validator
        self.device = serial.Serial(port=None, baudrate=baud, bytesize=8,
                                    parity='N', stopbits=1, timeout=0.05,
                                    write_timeout=3, xonxoff=False,
                                    rtscts=False, dsrdtr=False)
        self.device.dtr = False
        self.device.rts = False
        self.device.port = port
        self.metadata = {'started_utc': utc(), 'port': port, 'baud': baud,
                         'data_bits': 8, 'parity': 'N', 'stop_bits': 1,
                         'line_ending': 'CRLF', 'source': 'physical UART',
                         'rts': False, 'dtr': False,
                         'tx_character_delay_seconds': character_delay}
        self.metadata['capture_program_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        if validator is not None:
            validator_path = Path(validator.__code__.co_filename)
            self.metadata['response_validator'] = {
                'name': validator.__name__, 'file': validator_path.name,
                'sha256': hashlib.sha256(validator_path.read_bytes()).hexdigest()}
        self.save()

    def open(self):
        self.device.open()
        if hasattr(self.device, 'set_buffer_size'):
            self.device.set_buffer_size(rx_size=1048576, tx_size=65536)
            self.metadata['requested_rx_buffer_bytes'] = 1048576
            self.metadata['requested_tx_buffer_bytes'] = 65536
        self.save()

    def save(self):
        temporary = self.directory / 'session.json.tmp'
        temporary.write_text(json.dumps(
            {**self.metadata, 'commands': self.results}, indent=2), encoding='utf-8')
        temporary.replace(self.directory / 'session.json')

    def record(self, direction, data):
        event = {'utc': utc(), 'monotonic': time.monotonic(),
                 'direction': direction, 'bytes': len(data), 'hex': data.hex()}
        if direction == 'rx':
            event['offset'] = self.offset
            self.offset += len(data)
            self.raw.write(data)
            self.raw.flush()
        self.events.write(json.dumps(event) + '\n')
        self.events.flush()

    def send(self, command):
        data = command.encode('ascii') + b'\r\n'
        self.record('tx', data)
        written = 0
        for byte in data:
            written += self.device.write(bytes([byte]))
            if self.character_delay:
                time.sleep(self.character_delay)
        self.device.flush()
        if written != len(data):
            raise RuntimeError(f'short UART write: {written}/{len(data)}')
        return data

    def receive(self, timeout, quiet=0.25, *, require_prompt=True):
        data = bytearray()
        deadline = time.monotonic() + timeout
        last_rx = time.monotonic()
        prompt = False
        while time.monotonic() < deadline:
            # A fixed minimum coalesces UART bytes across the 50 ms timeout.
            # Reading one byte and flushing two files for each arrival can
            # overrun the Windows serial receive queue on sustained dumps.
            chunk = self.device.read(min(max(self.device.in_waiting, 4096), 16384))
            if chunk:
                self.record('rx', chunk)
                data.extend(chunk)
                last_rx = time.monotonic()
                prompt = bool(re.search(rb'(?:^|[\r\n])-> ?$', data))
            elif time.monotonic() - last_rx >= quiet and (prompt or not require_prompt):
                break
        return bytes(data), prompt

    def synchronize(self):
        initial, prompt = self.receive(1, require_prompt=False)
        if not prompt:
            self.send('')
            reply, prompt = self.receive(10)
            initial += reply
        (self.directory / 'initial.bin').write_bytes(initial)
        self.transcript.write('=== INITIAL / PROMPT SYNCHRONIZATION ===\n')
        self.transcript.write(initial.decode('latin-1').replace('\n\r', '\n'))
        self.transcript.flush()
        self.metadata['initial_prompt_seen'] = prompt
        self.save()
        return prompt

    def execute(self, item, number, default_timeout):
        command = item['command']
        slug = re.sub(r'[^a-zA-Z0-9_-]+', '_', command).strip('_')[:65] or 'enter'
        base = self.directory / f'{number:04d}-{slug}'
        result = {**item, 'index': number, 'started_utc': utc(),
                  'response_file': base.name + '.bin', 'status': 'started'}
        self.results.append(result)
        self.save()
        self.transcript.write(f'\n\n=== COMMAND {number}: {command} ===\n')
        self.transcript.flush()
        started = time.monotonic()
        sent = self.send(command)
        base.with_suffix('.tx.bin').write_bytes(sent)
        received, prompt = self.receive(float(item.get('timeout', default_timeout)))
        base.with_suffix('.bin').write_bytes(received)
        text = received.decode('latin-1').replace('\n\r', '\n')
        base.with_suffix('.txt').write_text(text, encoding='utf-8')
        self.transcript.write(text)
        self.transcript.flush()
        result.update(ended_utc=utc(), elapsed_seconds=round(time.monotonic()-started, 3),
                      response_bytes=len(received), response_sha256=hashlib.sha256(received).hexdigest(),
                      prompt_returned=prompt,
                      status='prompt-returned' if prompt else 'timeout-no-prompt')
        if prompt and self.validator is not None:
            try:
                result['validation'] = self.validator(item, text)
            except (ValueError, KeyError, TypeError) as error:
                result['validation'] = {'valid': False, 'errors': [str(error)]}
            if not result['validation'].get('valid'):
                result['status'] = 'response-validation-failed'
        base.with_suffix('.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        self.save()
        print(json.dumps({'index': number, 'command': command,
                          'bytes': len(received), 'prompt': prompt,
                          'validated': result.get('validation', {}).get('valid'),
                          'preview': text[:1100]}), flush=True)
        return result['status'] == 'prompt-returned'

    def close(self):
        self.device.close()
        self.metadata['ended_utc'] = utc()
        self.save()
        self.events.close()
        self.raw.close()
        self.transcript.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', default='COM16')
    parser.add_argument('--baud', default=115200, type=int)
    parser.add_argument('--commands', type=Path, required=True)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--timeout', default=20, type=float)
    parser.add_argument('--validate-storage', action='store_true',
                        help='Validate typed storage batches against their explicit expected metadata.')
    args = parser.parse_args()
    items = json.loads(args.commands.read_text(encoding='utf-8'))
    if not isinstance(items, list):
        parser.error('commands must be a JSON array')
    for item in items:
        command = item.get('command')
        if not isinstance(command, str) or not command or any(c in command for c in '\r\n\0'):
            parser.error('each command must be one nonempty line')
        if not command.isascii() or len(command) > 120:
            parser.error(f'command exceeds conservative 120-byte limit: {command!r}')
    validator = None
    if args.validate_storage:
        from storage_response import validate_storage_response
        validator = validate_storage_response
    capture = Capture(args.directory, args.port, args.baud, validator=validator)
    try:
        capture.open()
        if not capture.synchronize():
            capture.metadata['stop_reason'] = 'initial-prompt-not-observed'
            print('No prompt: batch stopped without sending test commands.', flush=True)
            return 2
        for number, item in enumerate(items, 1):
            if not capture.execute(item, number, args.timeout):
                capture.metadata['stop_reason'] = (
                    'response-validation-failed' if capture.results[-1]['status'] == 'response-validation-failed'
                    else 'command-did-not-return-prompt')
                return 1
        capture.metadata['stop_reason'] = 'batch-complete'
        return 0
    except (serial.SerialException, OSError, KeyboardInterrupt) as error:
        capture.metadata['stop_reason'] = type(error).__name__
        capture.metadata['error'] = str(error)
        print(f'Capture stopped: {error}', flush=True)
        return 2
    finally:
        capture.close()


if __name__ == '__main__':
    raise SystemExit(main())
