"""Check complete scanner origin/position UART replies without opening a port."""
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from scanner_review_round4 import ROOT, DATA

REF = ROOT / 'references'
def sha(raw):
    return hashlib.sha256(raw).hexdigest()

folder = sys.argv[1] if len(sys.argv) > 1 else 'physical-77-scanner-locations'
capture = REF / folder
session_raw = (capture / 'session.json').read_bytes()
session = json.loads(session_raw)
plan = json.loads((REF / '77-hardware-commands.json').read_text())
baseline = json.loads((REF / 'round7-baseline.json').read_text())
assert session['stop_reason'] == 'batch-complete'
assert [r['command'] for r in session['commands']] == [r['command'] for r in plan]
assert all(r['name'] not in baseline['invoked_names'] for r in plan)
results = []
annotations = []
quality = []
for rec in session['commands']:
    path = capture / rec['response_file']
    raw = path.read_bytes()
    assert sha(raw) == rec['response_sha256'] and len(raw) == rec['response_bytes']
    assert rec['status'] == 'prompt-returned' and rec['prompt_returned']
    text = raw.decode('ascii').replace('\r', '').strip()
    prefix = '-> ' + rec['command']
    assert text.startswith(prefix)
    rest = text[len(prefix):].strip()
    ending = re.search(r';?udws\(\) returns 0 \(0x0\)\s*->$', rest)
    assert ending, repr(rest)
    payload = rest[:ending.start()].strip()
    obj = dict(name=rec['name'], command=rec['command'], handler=rec['handler'],
               response_file=path.relative_to(ROOT).as_posix(), response_sha256=sha(raw),
               response_bytes=len(raw), prompt_returned=True, wrapper_return=0,
               complete=True, valid=True,
               hardware_access='Read-only position register BD302010, with volatile cache refresh only.',
               physical_distance_unit='unassigned; retained as firmware location units')
    if rec['name'] == 'scan orig locget':
        pattern = (r'cover location:\s*(-?\d+)\s+park location:\s*(-?\d+)\s+'
                   r'white location:\s*(-?\d+)\s+black location:\s*(-?\d+)')
        match = re.fullmatch(pattern, payload)
        assert match, repr(payload)
        fields = dict(zip(['cover', 'park', 'white', 'black'], map(int, match.groups())))
        assert all(-2**31 <= value < 2**31 for value in fields.values())
        assert fields['cover'] == rec['expected_cover_from_preflight']
        origin = rec['origin_evidence']
        assert sha((ROOT / origin['response_file']).read_bytes()) == origin['response_sha256']
        obj.update(fields=fields, field_count=4, matches_preflight_cover=True,
                   origin_source=origin,
                   assessment='complete-scanner-origin-locations',
                   note='All four labelled signed locations are present. Cover matches the preflight origin '
                        'plus compiled offset. The reviewed park fallback can read the current position '
                        'register; white and black are cached reference locations. No homing or scanning occurred.')
    else:
        match = re.fullmatch(r'Motor Position:\s*(-?\d+)', payload)
        assert match, repr(payload)
        value = int(match.group(1))
        assert -2**31 <= value < 2**31
        obj.update(value=value, field_count=1,
                   assessment='complete-read-only-scanner-position',
                   note='One complete signed position value was returned through the physically resolved '
                        'flatbed interface. The helper reads BD302010 and refreshes only volatile cache208593AC; '
                        'it does not command movement or establish calibration accuracy.')
    results.append(obj)
    common = {k: obj[k] for k in ['response_file', 'response_sha256', 'assessment', 'note']}
    annotations.append(dict(common, valid=True, complete=True))
    quality.append(dict(common, complete=True))

origin_result = next(r for r in results if r['name'] == 'scan orig locget')
motor_result = next(r for r in results if r['name'] == 'scan mtr getpos')
park_matches = origin_result['fields']['park'] == motor_result['value']
origin_result['park_matches_subsequent_position'] = park_matches
report = dict(schema_version=1, generated_utc=datetime.now(timezone.utc).isoformat(),
              provenance=dict(serial_access_by_reviewer='none', image_sha256=sha(DATA),
                              baseline='references/round7-baseline.json', baseline_count=703,
                              source_session=dict(path=(capture/'session.json').relative_to(ROOT).as_posix(),
                                                  sha256=sha(session_raw))),
              summary=dict(physical_commands=len(results), validated_responses=len(results),
                           new_unique_names=len({r['name'] for r in results}),
                           new_unique_handlers=len({r['handler'] for r in results}),
                           origin_locations=origin_result['fields'], motor_position=motor_result['value'],
                           park_matches_subsequent_position=park_matches),
              response_revalidations=annotations, data_quality=quality, results=results,
              limitations=['The reads are sequential snapshots; matching values do not establish physical position accuracy.',
                           'Raw firmware location units have not been converted to distance.',
                           'The two handlers share a position-reading helper but return different data; this is two command names, not two hardware devices.'])
(REF/'round7_hardware_results.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
print(json.dumps(report['summary']))
