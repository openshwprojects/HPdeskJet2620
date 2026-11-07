"""Replay every round-five temporary-file metadata read from immutable UART bytes."""
import hashlib
import json
from pathlib import Path
from capture_nvm_mirror import payload, validate

REF = Path(__file__).resolve().parent
directories = ['physical-57-provider-pointers', 'physical-59-metadata-pointers',
               'physical-60-ui-metadata', 'physical-64-ui-fields', 'physical-67-mount-metadata']
report = {'serial_access':False, 'response_revalidations':[], 'data_quality':[], 'sessions':[]}
for name in directories:
    directory = REF / name
    session = json.loads((directory / 'session.json').read_bytes())
    snapshots = json.loads((directory / 'provider-pointers.json').read_bytes())
    assert session['ended_utc'] and session['stop_reason'] == 'batch-complete'
    assert snapshots['cleanup_complete'] and snapshots['source_memory_written'] is False
    by_file = {}
    for row in session['commands']:
        raw = (directory / row['response_file']).read_bytes()
        assert len(raw) == row['response_bytes']
        assert hashlib.sha256(raw).hexdigest() == row['response_sha256']
        assert row['prompt_returned'] and row['status'] == 'prompt-returned'
        assert validate(row, raw.decode('latin-1'))['valid']
        by_file[row['response_file']] = row
    assert len(session['commands']) == 9 * len(snapshots['observations'])
    for observation in snapshots['observations']:
        row = by_file[observation['response_file']]
        assert row['response_sha256'] == observation['response_sha256']
        raw = (directory / row['response_file']).read_bytes()
        data = bytes.fromhex(payload(raw.decode('latin-1')))
        assert data == bytes.fromhex(observation['hex']) and len(data) == observation['count']
        assert observation['words_be'] == [hex(int.from_bytes(data[i:i+4], 'big')) for i in range(0,len(data),4)]
        index = session['commands'].index(row)
        window = session['commands'][index-5:index+4]
        assert len(window) == 9 and 'ustd.sizeof ' in window[0]['command'] and 'ustd.sizeof ' in window[-1]['command']
        assert window[0]['expected'] == window[-1]['expected'] == -1
        assert f"{observation['address']} {observation['count']};" in window[2]['command']
        source = {'response_file':name+'/'+row['response_file'], 'response_sha256':row['response_sha256']}
        note = ('Exact bounded RAM bytes recovered through an owned temporary file, whose absence was verified before creation and after removal. '
                'Pointer/data interpretation is separate from byte completeness; memory may change between snapshots.')
        if observation['label'] == 'firm_string_registry_21_rows':
            note += ' Historical label describes requested420-byte span only:21 registered string groups have NOT been established; much of this span is zero and later words may be adjacent data.'
        report['response_revalidations'].append({**source, 'valid':True, 'complete':True,
            'assessment':'exact-metadata-span-replayed', 'note':note})
        report['data_quality'].append({**source, 'complete':True, 'payload_kind':'bounded-RAM-snapshot',
            'assessment':note, 'address':observation['address'], 'bytes':len(data)})
    report['sessions'].append({'directory':name, 'validated_commands':len(session['commands']),
        'validated_spans':len(snapshots['observations']), 'cleanup_complete':True,
        'report_sha256':hashlib.sha256((directory / 'provider-pointers.json').read_bytes()).hexdigest()})
(REF / 'round5_metadata_results.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
print(json.dumps({'spans':len(report['response_revalidations']), 'commands':sum(x['validated_commands'] for x in report['sessions'])}))
