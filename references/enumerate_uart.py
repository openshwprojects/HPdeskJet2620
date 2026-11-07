"""Enumerate the real firmware registry through its read-only UDW iterator."""
import argparse
from contextlib import redirect_stdout
import json
from pathlib import Path
import re

from capture_uart import Capture


def payload(data, command):
    text = data.decode('latin-1').replace('\r', '')
    echo = command + '\n'
    at = text.find(echo)
    if at >= 0 and re.fullmatch(r'[\n >-]*', text[:at]):
        text = text[at:]
    if not text.startswith(command + '\n'):
        raise ValueError('Missing expected command echo; refusing to infer registry names')
    text = text[len(command) + 1:]
    if 'udws() returns 0 (0x0)' not in text:
        raise ValueError('Iterator wrapper did not report zero return')
    return text.split('udws() returns 0 (0x0)', 1)[0].strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--port', default='COM16')
    parser.add_argument('--per-line', type=int, choices=(1, 2, 3, 4), default=1)
    args = parser.parse_args()
    capture = Capture(args.directory, args.port, 115200)
    inventory = {'source': 'physical UART registry iterator', 'complete': False,
                 'completion_marker': None, 'entries': []}
    output = args.directory / 'runtime_inventory.json'

    def save():
        inventory['entry_count'] = len(inventory['entries'])
        inventory['unique_name_count'] = len({x['name'] for x in inventory['entries']})
        output.write_text(json.dumps(inventory, indent=2), encoding='utf-8')

    try:
        capture.open()
        if not capture.synchronize():
            raise RuntimeError('No shell prompt; iterator not started')
        capture.execute({'command': 'udws "udw.iter_reset;"',
                         'purpose': 'Reset registry enumeration cursor',
                         'classification': 'command-discovery'}, 1, 20)
        if not capture.results[-1]['prompt_returned']:
            raise RuntimeError('Iterator reset did not return a prompt')
        with (args.directory / 'execution.jsonl').open('w', encoding='utf-8') as console:
            for index in range(2, 10002):
                command = 'udws "' + 'udw.iter_get_next;' * args.per_line + '"'
                with redirect_stdout(console):
                    prompt = capture.execute({'command': command,
                        'purpose': f'Read next {args.per_line} registry names; does not execute listed commands',
                        'classification': 'command-discovery'}, index, 20)
                console.flush()
                if not prompt:
                    raise RuntimeError('Iterator did not return prompt')
                response = capture.results[-1]['response_file']
                body = payload((args.directory / response).read_bytes(), command)
                names = [name.strip() for name in body.split(';') if name.strip()]
                if not names:
                    raise RuntimeError('Empty iterator output is not a completion marker')
                for name in names:
                    if name == 'iter_done':
                        inventory['complete'] = True
                        inventory['completion_marker'] = name
                        break
                    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_. ]*', name):
                        raise RuntimeError(f'Unrecognized registry item: {name!r}')
                    inventory['entries'].append({'index': len(inventory['entries']) + 1,
                        'name': name, 'response_file': response, 'enumeration_command_index': index})
                if index % 20 == 0 or inventory['complete']:
                    save()
                    print(f"Real registry: {len(inventory['entries'])} entries; complete={inventory['complete']}", flush=True)
                if inventory['complete']:
                    capture.metadata['stop_reason'] = 'iterator-reported-iter_done'
                    break
            else:
                raise RuntimeError('Enumeration safety bound reached without iter_done')
    except (Exception, KeyboardInterrupt) as error:
        inventory['error'] = str(error)
        capture.metadata['stop_reason'] = type(error).__name__ + ': ' + str(error)
        print(capture.metadata['stop_reason'], flush=True)
        return 1
    finally:
        save()
        capture.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
