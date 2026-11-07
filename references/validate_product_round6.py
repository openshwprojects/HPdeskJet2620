"""Validate the two physical consumable responses without serial access."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import re

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / 'references'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def main():
    cap = REF / 'physical-72-consumable-status'
    source = (cap / 'session.json').read_bytes()
    session = json.loads(source)
    assert session['stop_reason'] == 'batch-complete' and session['ended_utc']
    rx = (cap / 'rx.bin').read_bytes()
    image = (ROOT / 'artifacts/ghs/dram.bin').read_bytes()
    review_raw = (REF / 'round6_product_review.json').read_bytes()
    review = json.loads(review_raw)
    assert sha(image) == review['image_sha256']
    for row in review['queries']:
        for ev in row['code_evidence']:
            a, b = int(ev['start'], 16), int(ev['end_exclusive'], 16)
            assert sha(image[a & 0x3ffffff:b & 0x3ffffff]) == ev['sha256']
    cstr = lambda address: image[address & 0x3ffffff:].split(b'\0',1)[0].decode('ascii')
    status_label = cstr(0xa003b748)
    consumable_labels = {'BLACK':cstr(0xa003b718),'CMY':cstr(0xa003b724)}
    channel_labels = [cstr(a) for a in [0xa003b774,0xa003b77c,0xa003b784,0xa003b78c]]
    results, validations, quality = [], [], []
    assert len(session['commands']) == 2
    for rec in session['commands']:
        raw_path = cap / rec['response_file']
        raw = raw_path.read_bytes()
        assert len(raw) == rec['response_bytes'] and sha(raw) == rec['response_sha256']
        assert rec['status'] == 'prompt-returned' and rec['prompt_returned']
        assert rec['name'] == 'constat.get_consumable_info'
        assert raw_path.with_suffix('.tx.bin').read_bytes() == (rec['command']+'\r\n').encode()
        offset = rx.find(raw)
        assert offset >= 0 and rx.find(raw, offset+1) == -1
        clean = raw.decode('ascii').replace('\r','').strip()
        prefix = '-> '+rec['command']
        assert clean.startswith(prefix)
        body = clean[len(prefix):].strip()
        match = re.fullmatch(r'(.+);udws\(\) returns 0 \(0x0\)\s*->', body, re.S)
        assert match
        payload = match[1]
        parts = [x.strip() for x in payload.split(',')]
        token = rec['arguments'][0]
        assert token in ['BLACK','CMY']
        assert parts[:2] == [status_label,consumable_labels[token]]
        assert len(parts) >= 8
        presence, flag10, flag14, chute, count = map(int, parts[2:7])
        channels = parts[7:]
        expected_chute = 1 if token == 'BLACK' else 0
        expected_channels = channel_labels[:1] if token == 'BLACK' else channel_labels[1:]
        assert presence in [0,1] and flag10 == flag14 == 1
        assert chute == expected_chute and count == len(channels)
        assert channels == expected_channels
        dsid_address = 0xa05d7084+4*(2*0x14+chute)
        dsid = int.from_bytes(image[dsid_address & 0x3ffffff:(dsid_address & 0x3ffffff)+4], 'big')
        assessment = 'complete-cached-consumable-info'
        note = ('Physical statusOK and wrapper0. All declared channels and scalar fields are present; '
                'chute, fixed flags and channel labels match the reviewed implementation. '
                'The first scalar is cached presence: 1-((prioritized PEN status>>1)&1). '
                'It does not establish cartridge health or ink remaining.')
        obj = dict(name=rec['name'],command=rec['command'],index=rec['index'],
                   response_file=raw_path.relative_to(ROOT).as_posix(),response_sha256=sha(raw),
                   response_bytes=len(raw), rx_offset=offset,rx_end_exclusive=offset+len(raw),
                   prompt_returned=True,wrapper_return=0,status_label=parts[0],
                   consumable=token,consumable_label=parts[1],cached_presence=presence,
                   constant_fields={'output+0x10':flag10,'output+0x14':flag14},
                   chute=chute,channel_count=count,channels=channels,
                   cached_pen_status_dsid=dsid,cached_pen_status_dsid_hex=hex(dsid),
                   cached_pen_status_source_table=hex(dsid_address),
                   payload=payload, valid=True,complete=True,assessment=assessment,note=note)
        results.append(obj)
        common = {k:obj[k] for k in ['response_file','response_sha256','assessment','note']}
        validations.append(dict(common,valid=True,complete=True))
        quality.append(dict(common,complete=True))
    assert {r['consumable'] for r in results} == {'BLACK','CMY'}
    report = dict(schema_version=1,generated_utc=datetime.now(timezone.utc).isoformat(),
                  provenance=dict(serial_access_by_reviewer='none',
                                  source_session=(cap/'session.json').relative_to(ROOT).as_posix(),
                                  source_session_sha256=sha(source),
                                  rx_file=(cap/'rx.bin').relative_to(ROOT).as_posix(),rx_sha256=sha(rx),
                                  image_sha256=sha(image),
                                  review_source='references/round6_product_review.json',review_sha256=sha(review_raw)),
                  summary=dict(physical_commands=2,validated_responses=2,unique_names=1,
                               cached_presence={r['consumable']:r['cached_presence'] for r in results},
                               chute_mapping={r['consumable']:r['chute'] for r in results}),
                  response_revalidations=validations,data_quality=quality,results=results,
                  limitations=['Cached presence is a firmware status predicate, not a cartridge or ink-level test.',
                               'The two valid cartridge selections are one registered command name.',
                               'DSIDs0x100DF/0x100E0 identify the cache sources in ROM; no additional direct DS2 capture was made here.',
                               'Original raw session and response records remain unchanged.'])
    (REF/'round6_product_results.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report['summary']))


if __name__ == '__main__':
    main()
