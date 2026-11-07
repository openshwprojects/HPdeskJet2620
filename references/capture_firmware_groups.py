"""Capture and validate all physically enumerated numeric firmware groups."""
import hashlib
import json
from pathlib import Path
import re

from capture_uart import Capture


def parse_group(item, text):
    normalized = text.replace('\r', '')
    echo = item['command'] + '\n'
    if echo not in normalized or 'udws() returns 0 (0x0)' not in normalized:
        raise ValueError('Missing command echo or successful wrapper return')
    payload = normalized.split(echo, 1)[1].split('udws() returns', 1)[0]
    lines = [line.strip() for line in payload.splitlines() if line.strip() and line.strip() != ';']
    expected = item['expected']
    header = re.fullmatch(r'0x([0-9a-fA-F]+) = (\S+);', lines[0])
    if not header or int(header[1], 16) != expected['suid'] or header[2] != expected['group_name']:
        raise ValueError('Wrong or malformed group header')
    rows = []
    for line in lines[1:]:
        match = re.fullmatch(r'0x([0-9a-fA-F]+) = (-?\d+) (-?\d+) (\S+);', line)
        if not match:
            raise ValueError('Malformed group row: ' + line[:180])
        rows.append({'packed_id': int(match[1], 16), 'current': int(match[2]),
                     'compiled': int(match[3]), 'name': expected['group_name'] + '.' + match[4]})
    wanted = [((expected['suid'] << 8) | row['index'], row['name']) for row in expected['items']]
    actual = [(row['packed_id'], row['name']) for row in rows]
    if actual != wanted or len(rows) != expected['item_count']:
        raise ValueError('Group rows differ from the complete physical name registry')
    return rows


def validate(item, text):
    try:
        rows = parse_group(item, text)
        return {'valid': True, 'rows': len(rows), 'expected_rows': item['expected']['item_count'],
                'checks': ['group identity', 'complete ordered item IDs/names', 'integer fields', 'row delimiters']}
    except (ValueError, IndexError) as error:
        return {'valid': False, 'errors': [str(error)]}


def main():
    plan = Path('references/45-firm-groups-commands.json')
    items = json.loads(plan.read_text())
    capture = Capture('references/physical-45-firmware-groups', 'COM16', 115200, validator=validate)
    try:
        capture.open()
        if not capture.synchronize():
            raise RuntimeError('No initial prompt')
        for number, item in enumerate(items, 1):
            assert len(item['command']) <= 120 and '\n' not in item['command']
            if not capture.execute(item, number, 25):
                raise RuntimeError('Firmware group did not return a complete validated response')
        capture.metadata['stop_reason'] = 'batch-complete'
    except Exception as error:
        capture.metadata['stop_reason'] = str(error)
        raise
    finally:
        capture.close()
    values = []
    for item, result in zip(items, capture.results, strict=True):
        path = capture.directory / result['response_file']
        raw = path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == result['response_sha256']
        for row in parse_group(item, raw.decode('latin-1')):
            values.append({**row, 'suid': item['expected']['suid'],
                           'source_response': str(path).replace('\\', '/'),
                           'source_sha256': result['response_sha256']})
    assert len(values) == 577 and len({x['packed_id'] for x in values}) == 577
    output = {'source': 'physical UART, sequential group queries', 'item_count': len(values),
              'group_count': len(items), 'all_groups_validated': True, 'atomic_snapshot': False,
              'plan_sha256': hashlib.sha256(plan.read_bytes()).hexdigest(), 'values': values}
    Path('references/physical-firmware-values.json').write_text(json.dumps(output, indent=2)+'\n')
    print(json.dumps({'complete_numeric_firmware_items': len(values), 'groups': len(items)}))


if __name__ == '__main__':
    main()
