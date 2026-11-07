"""Capture the firmware's 64-KiB NVM RAM mirror via an owned temporary RAM file.

This is not a direct full EEPROM read. Each bounded UART payload is validated;
selected byte offsets are independently compared with nvm_read_byte afterwards.
"""
import hashlib
import json
import re
from pathlib import Path

from capture_uart import Capture

DEST = '/ram/codex_nvm_20261029b.bin'
OUT = Path('references/physical-51-nvm-mirror')
SIZE = 65536
CHUNK = 512


def payload(text):
    m = re.fullmatch(r'\s*-> udws "[^\r\n]*"\s*([\s\S]*?);?udws\(\) returns 0 \(0x0\)\s*-> ?', text)
    if not m:
        raise ValueError('Unexpected echo/wrapper/prompt framing')
    return m[1].strip()


def validate(item, text):
    value = payload(text)
    errors = []
    if 'expected' in item and value != str(item['expected']):
        errors.append(f'Expected {item["expected"]!r}; got {value!r}')
    if 'hex_bytes' in item and not re.fullmatch('[0-9a-fA-F]{' + str(2 * item['hex_bytes']) + '}', value):
        errors.append('Hex payload has wrong length or nonhex characters')
    return {'valid': not errors, 'errors': errors}


def main():
    c = Capture(OUT, 'COM16', 115200, validator=validate)
    created = opened = responsive = False
    manifest = {'kind': 'physical firmware NVM RAM-mirror snapshot',
                'size': SIZE, 'chunk_size': CHUNK, 'atomic_snapshot': False,
                'direct_full_eeprom_read': False, 'chunks': [], 'spot_checks': [],
                'review': 'nvm_mirror_round4_review.json'}

    def save():
        (OUT / 'mirror-validation.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')

    def run(expr, purpose, **expect):
        nonlocal responsive
        item = {'command': f'udws "{expr};"', 'purpose': purpose,
                'classification': 'reviewed-nvm-mirror-capture',
                'review_source': 'nvm_mirror_round4_review.json', 'timeout': 30, **expect}
        assert len(item['command']) <= 120
        ok = c.execute(item, len(c.results) + 1, 30)
        responsive = c.results[-1].get('prompt_returned', False)
        if not ok:
            raise RuntimeError('Unexpected reply: ' + expr)
        raw = OUT / c.results[-1]['response_file']
        return payload(raw.read_bytes().decode('latin-1'))

    try:
        c.open()
        responsive = c.synchronize()
        if not responsive:
            raise RuntimeError('No prompt')
        run(f'ustd.sizeof {DEST}', 'Require the exact temporary path to be absent before creation.', expected=-1)
        created = True  # A partial file still belongs to this capture if the next command fails.
        run(f'nvm2.dump2file {DEST}', 'Copy the firmware NVM RAM mirror into a new volatile file.', expected=SIZE)
        run(f'ustd.sizeof {DEST}', 'Verify mirror-file size.', expected=SIZE)
        opened = True
        run(f'ustd.open_file {DEST}', 'Open our file read-only using the dedicated debug reader.', expected=SIZE)
        data = bytearray()
        for offset in range(0, SIZE, CHUNK):
            block = bytes.fromhex(run(f'ustd.read_file {offset} {CHUNK}',
                                     'Read one bounded mirror-file block.', hex_bytes=CHUNK))
            data.extend(block)
            source = c.results[-1]
            manifest['chunks'].append({'offset': offset, 'length': len(block),
                'response_file': source['response_file'], 'response_sha256': source['response_sha256'],
                'block_sha256': hashlib.sha256(block).hexdigest()})
            (OUT / 'nvm-ram-mirror.partial.bin').write_bytes(data)
            save()
        assert len(data) == SIZE
        (OUT / 'nvm-ram-mirror-65536.bin').write_bytes(data)
        manifest.update(complete=True, sha256=hashlib.sha256(data).hexdigest())
        # Check the boot-critical header and one location at each 4-KiB boundary,
        # then the final byte, using the direct hardware-backed read-byte path.
        offsets = sorted(set([0, 1, 12, 13, 255, 256, 65535] + list(range(4096, SIZE, 4096))))
        for offset in offsets:
            value = int(run(f'nvm2.nvm_read_byte {offset}', 'Compare one direct EEPROM byte against the cached mirror.'))
            source = c.results[-1]
            same = value == data[offset]
            manifest['spot_checks'].append({'offset': offset, 'direct_value': value,
                'mirror_value': data[offset], 'equal': same,
                'response_file': source['response_file'], 'response_sha256': source['response_sha256']})
            save()
        manifest['spot_checks_all_equal'] = all(x['equal'] for x in manifest['spot_checks'])
        c.metadata['stop_reason'] = 'batch-complete'
    except Exception as error:
        c.metadata['stop_reason'] = type(error).__name__ + ': ' + str(error)
        manifest['error'] = str(error)
        raise
    finally:
        try:
            if responsive and opened:
                run('ustd.close_file', 'Release the debug-reader handle opened by this capture.', expected='')
                opened = False
            if responsive and created and not opened:
                run(f'ram.unlink {DEST}', 'Delete only our temporary mirror file.', expected=0)
                run(f'ustd.sizeof {DEST}', 'Verify the temporary file is absent after cleanup.', expected=-1)
                created = False
        finally:
            manifest['cleanup_complete'] = not created and not opened
            save()
            c.close()


if __name__ == '__main__':
    main()
