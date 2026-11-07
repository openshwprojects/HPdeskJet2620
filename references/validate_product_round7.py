"""Validate system-consumable output and raw source boundaries. Offline only."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import re

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT/'references'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def main():
    cap = REF/'physical-76-consumable-system'
    session_raw = (cap/'session.json').read_bytes()
    session = json.loads(session_raw)
    assert session['stop_reason'] == 'batch-complete' and session['ended_utc']
    assert len(session['commands']) == 1
    rec = session['commands'][0]
    assert rec['name'] == 'constat.get_system_info'
    assert rec['command'] == 'udws "constat.get_system_info;"'
    assert rec['status'] == 'prompt-returned' and rec['prompt_returned']
    path = cap/rec['response_file']
    raw = path.read_bytes()
    assert len(raw) == rec['response_bytes'] and sha(raw) == rec['response_sha256']
    assert path.with_suffix('.tx.bin').read_bytes() == (rec['command']+'\r\n').encode()
    rx = (cap/'rx.bin').read_bytes()
    offset = rx.find(raw)
    assert offset >= 0 and rx.find(raw,offset+1) == -1
    text = raw.decode('ascii').replace('\r','').strip()
    prefix = '-> '+rec['command']
    assert text.startswith(prefix)
    match = re.fullmatch(r'(.+);udws\(\) returns 0 \(0x0\)\s*->',text[len(prefix):].strip(),re.S)
    assert match
    payload = match[1]
    fields = [x.strip() for x in payload.split(',')]
    assert fields[0] == 'OK'
    offsets = [0x0c,0x14,0x10,0x40,0x8c,0x90,0x98,0x9c,0xa0,0xa4,0x94]
    scalars = dict(zip([hex(x) for x in offsets],map(int,fields[1:12])))
    assert [scalars[hex(x)] for x in offsets[:4]] == [2,2,2,4]
    for off in [0x8c,0x90,0x98,0xa0,0x94]:
        assert scalars[hex(off)] in [0,1]
    position = 12
    lists = {}
    for key, limit in [('configured_consumables',2),('supported_channels',4),('usable_channels',4)]:
        count = int(fields[position])
        position += 1
        assert 0 <= count <= limit
        values = fields[position:position+count]
        assert len(values) == count
        position += count
        lists[key] = dict(count=count,values=values)
    assert lists['configured_consumables']['values'] == ['CMY IPH','BLACK IPH']
    assert lists['supported_channels']['values'] == ['CYAN','MAGENTA','YELLOW','BLACK']
    assert lists['usable_channels']['values'] in [[],['BLACK'],['CYAN','MAGENTA','YELLOW'],['BLACK','CYAN','MAGENTA','YELLOW']]
    assert len(fields)-position == 4
    trailing = dict(zip(['0xb0','0xa8','0xb8','0xc0'],map(int,fields[position:])))
    assert list(trailing.values()) == [0,0,0,0]
    review_raw = (REF/'round7_product_review.json').read_bytes()
    review = json.loads(review_raw)
    image = (ROOT/'artifacts/ghs/dram.bin').read_bytes()
    assert sha(image) == review['image_sha256']
    for ev in review['queries'][0]['code_evidence']:
        start,end = int(ev['start'],16),int(ev['end_exclusive'],16)
        assert sha(image[start&0x3ffffff:end&0x3ffffff]) == ev['sha256']
    note = ('Physical OK status and wrapper0; all11 leading scalars,3 count-prefixed lists and4 trailing scalars are present. '
            'Configured cartridges and supported channels agree with the reviewed ROM implementation. '
            'The usable-channel list and readiness-related scalars describe cached state; they do not measure ink volume or test mechanics. '
            'Names are retained by output-structure offset for scalars whose exact public meaning is unestablished.')
    result = dict(name=rec['name'],command=rec['command'],index=rec['index'],
                  response_file=path.relative_to(ROOT).as_posix(),response_sha256=sha(raw),response_bytes=len(raw),
                  rx_offset=offset,rx_end_exclusive=offset+len(raw),prompt_returned=True,
                  wrapper_return=0,status_label='OK',payload=payload,
                  leading_scalars_by_structure_offset=scalars,lists=lists,
                  trailing_scalars_by_structure_offset=trailing,
                  valid=True,complete=True,assessment='complete-cached-consumable-system-info',note=note)
    annotation = {key:result[key] for key in ['response_file','response_sha256','assessment','note']}
    report = dict(schema_version=1,generated_utc=datetime.now(timezone.utc).isoformat(),
                  provenance=dict(serial_access_by_reviewer='none',
                                  source_session=(cap/'session.json').relative_to(ROOT).as_posix(),source_session_sha256=sha(session_raw),
                                  rx_file=(cap/'rx.bin').relative_to(ROOT).as_posix(),rx_sha256=sha(rx),
                                  image_sha256=sha(image),review_source='references/round7_product_review.json',review_sha256=sha(review_raw)),
                  summary=dict(physical_commands=1,validated_responses=1,unique_names=1,
                               configured_consumables=lists['configured_consumables']['values'],
                               supported_channels=lists['supported_channels']['values'],
                               usable_channels=lists['usable_channels']['values']),
                  response_revalidations=[dict(annotation,valid=True,complete=True)],
                  data_quality=[dict(annotation,complete=True)],results=[result],
                  limitations=['The reviewed simple ink-cache path relies on the guard observed in physical79 before this query.',
                               'No direct ink quantity, cartridge electrical or mechanical test was performed.',
                               'Original capture files and records remain unchanged.'])
    (REF/'round7_product_results.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report['summary']))


if __name__ == '__main__':
    main()
