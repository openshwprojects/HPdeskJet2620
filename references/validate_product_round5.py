"""Verify physical product capability responses against the ROM, offline only."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import re

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / 'references'
DATA = (ROOT / 'artifacts/ghs/dram.bin').read_bytes()

def sha(raw):
    return hashlib.sha256(raw).hexdigest()

def u32(address):
    off = address & 0x3ffffff
    return int.from_bytes(DATA[off:off + 4], 'big')

def main():
    cap = REF / 'physical-55-media-capabilities'
    session_raw = (cap / 'session.json').read_bytes()
    session = json.loads(session_raw)
    assert session['stop_reason'] == 'batch-complete'
    results = []
    validations = []
    quality = []
    for rec in session['commands']:
        path = cap / rec['response_file']
        raw = path.read_bytes()
        assert sha(raw) == rec['response_sha256']
        assert len(raw) == rec['response_bytes']
        assert rec['prompt_returned'] and rec['status'] == 'prompt-returned'
        text = raw.decode('ascii').replace('\r', '').strip()
        prefix = '-> ' + rec['command']
        assert text.startswith(prefix)
        rest = text[len(prefix):].strip()
        assert re.search(r';?udws\(\) returns 0 \(0x0\)\s*->$', rest)
        payload = re.sub(r';?udws\(\) returns 0 \(0x0\)\s*->$', '', rest).strip()
        match = re.fullmatch(r'200\s*[,;]\s*(\d+)(?:\s*[,;]\s*(.*?))?\s*', payload, re.S)
        assert match, (rec['name'], payload)
        count, body = int(match[1]), (match[2] or '').rstrip(';').strip()
        name = rec['name']
        if name == 'mhdlg.get_mhdlgcap_supported_bins':
            assert re.fullmatch(r'\(\d+\)(?:\s*,\s*\(\d+\))*', body)
            values = [int(x) for x in re.findall(r'\((\d+)\)', body)]
            expected, source, labels = [0], 'A030437A writes bin0 and returns count1', []
            assessment = 'complete-output-bin-capability'
        elif name == 'mhdlg.get_supported_df_groups':
            values, labels, expected = [], [], []
            assert body == '' and count == u32(0xa05cfbb8) == 0
            source = 'ROM A05CFBB8'
            assessment = 'complete-zero-group-capability'
        else:
            pairs = re.findall(r'([^,;()]+)\((-?\d+)\)', body)
            assert ','.join(label + '(' + number + ')' for label, number in pairs) == body
            labels = [label for label, _ in pairs]
            values = [int(number) for _, number in pairs]
            if name == 'mhdlg.get_mhdlgcap_supported_trays':
                expected = [u32(0xa05cfbd0 + 0x54 * i) for i in range(u32(0xa05cfbb0))]
                source = 'ROM tray records A05CFBD0, count A05CFBB0'
                assessment = 'complete-input-tray-capability'
            else:
                assert name == 'mhdlg.get_mhdlgcap_media_type_size_list'
                selector = int(re.search(r'media_type_size_list (\d+) 1;', rec['command'])[1])
                address, n = ((0xa05cfc24, 32) if selector == 0 else (0xa05cfcc4, 4))
                expected = list(dict.fromkeys(u32(address + 4 * i) for i in range(n)))
                source = f'ROM 0x{address:08x}, {n} table entries'
                assessment = 'complete-media-size-capability' if selector == 0 else 'complete-media-type-capability'
        assert count == len(values) and values == expected, (name, count, values, expected)
        note = 'Physical status200, full declared count and all IDs agree with the reviewed ROM capability implementation.'
        if name == 'mhdlg.get_supported_df_groups':
            note += ' This is a successful empty-capability response, not an invalid-group/404 guard.'
        obj = dict(name=name, command=rec['command'], index=rec['index'],
                   response_file=path.relative_to(ROOT).as_posix(), response_sha256=sha(raw),
                   response_bytes=len(raw), prompt_returned=True, wrapper_return=0,
                   status_code=200, declared_count=count, values=values, labels=labels,
                   rom_source=source, matches_reviewed_rom=True, valid=True, complete=True,
                   assessment=assessment, note=note)
        results.append(obj)
        common = {key: obj[key] for key in ['response_file', 'response_sha256', 'assessment', 'note']}
        validations.append(dict(common, valid=True, complete=True))
        quality.append(dict(common, complete=True))
    assert len(results) == 5 and len({r['name'] for r in results}) == 4
    report = dict(schema_version=1, generated_utc=datetime.now(timezone.utc).isoformat(),
                  provenance=dict(serial_access_by_reviewer='none', image_sha256=sha(DATA),
                                  source_session=(cap/'session.json').relative_to(ROOT).as_posix(),
                                  source_session_sha256=sha(session_raw)),
                  summary=dict(physical_commands=len(results), validated_responses=len(results),
                               unique_names=4, input_trays=1, output_bins=1,
                               media_size_ids=32, media_type_ids=4, df_groups=0),
                  response_revalidations=validations, data_quality=quality, results=results,
                  limitations=['Capabilities describe configured firmware support, not currently loaded media or a mechanical test.',
                               'Two media-list selectors are one registered command name.',
                               'Original raw capture bytes and session records remain unchanged.'])
    (REF/'round5_product_results.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report['summary']))

if __name__ == '__main__':
    main()
