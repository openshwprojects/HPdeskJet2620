"""Rebuild the captured NVM mirror from raw, hashed UART replies without serial I/O."""
import hashlib
import json
from pathlib import Path

from capture_nvm_mirror import payload, validate


def main():
    root = Path(__file__).resolve().parent
    directory = root / 'physical-51-nvm-mirror'
    report = json.loads((directory / 'mirror-validation.json').read_text())
    session = json.loads((directory / 'session.json').read_text())
    assert session['ended_utc'] and session['stop_reason'] == 'batch-complete'
    assert len(report['chunks']) == 128
    rebuilt = bytearray()
    for entry in report['chunks']:
        assert entry['offset'] == len(rebuilt)
        raw = (directory / entry['response_file']).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == entry['response_sha256']
        block = bytes.fromhex(payload(raw.decode('latin-1')))
        assert len(block) == entry['length'] == 512
        assert hashlib.sha256(block).hexdigest() == entry['block_sha256']
        rebuilt.extend(block)
    assert len(rebuilt) == report['size'] == 65536
    assert bytes(rebuilt) == (directory / 'nvm-ram-mirror-65536.bin').read_bytes()
    assert hashlib.sha256(rebuilt).hexdigest() == report['sha256']
    for record in session['commands']:
        raw = (directory / record['response_file']).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == record['response_sha256']
        assert validate(record, raw.decode('latin-1'))['valid']
    for entry in report['spot_checks']:
        raw = (directory / entry['response_file']).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == entry['response_sha256']
        value = int(payload(raw.decode('latin-1')))
        assert 0 <= value <= 255
        assert value == entry['direct_value']
        assert rebuilt[entry['offset']] == entry['mirror_value']
        assert entry['equal'] == (value == entry['mirror_value'])
    assert len(report['spot_checks']) == 22
    assert report['spot_checks_all_equal'] == all(x['equal'] for x in report['spot_checks'])
    assert report['cleanup_complete']
    old_header = (root / 'physical-15-nvm-header/nvm-header-0000-00ff.bin').read_bytes()
    assert rebuilt[:256] == old_header
    print('Verified 65,536 captured mirror bytes, 22 agreeing direct-byte observations, '
          'the historical 256-byte header, and temporary-file cleanup.')


if __name__ == '__main__':
    main()
