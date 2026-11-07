"""Validate new default-setting and printer-date captures without serial access."""
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from scanner_review_round4 import ROOT, DATA

REF = ROOT / 'references'
def sha(raw):
    return hashlib.sha256(raw).hexdigest()

folder = sys.argv[1] if len(sys.argv) > 1 else 'physical-71-hardware-queries'
capture = REF / folder
session_raw = (capture / 'session.json').read_bytes()
session = json.loads(session_raw)
plan = json.loads((REF / '71-hardware-commands.json').read_text())
baseline = json.loads((REF / 'round6-baseline.json').read_text())
scalars = {x['dsid']: x for x in json.loads((REF / 'physical-storage-values.json').read_text())['objects']}
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
    match = re.fullmatch(r'(\d+);udws\(\) returns 0 \(0x0\)\s*->', rest)
    assert match, repr(rest)
    value = int(match.group(1))
    assert value == rec['expected_value']
    obj = dict(name=rec['name'], command=rec['command'], handler=rec['handler'],
               argument_syntax=rec['argument_syntax'], value=value, dsid=rec['dsid'],
               storage_name=scalars[rec['dsid']]['name'],
               response_file=path.relative_to(ROOT).as_posix(), response_sha256=sha(raw),
               response_bytes=len(raw), prompt_returned=True, wrapper_return=0,
               complete=True, valid=True, matches_prior_nvm_and_ds2=True,
               cross_check_sources=[])
    old = scalars[rec['dsid']]
    for key in ['nvm_contents_latest', 'ds2_scalar_latest']:
        prior = old[key]
        assert prior['value'] == value
        source = prior['source']
        assert sha((ROOT / source['response_file']).read_bytes()) == source['response_sha256']
        obj['cross_check_sources'].append(source)
    if rec['name'] == 'smgr_pe.get_default_setting':
        assert len(match.group(1)) == 1
        obj.update(setting_index=int(rec['argument_syntax']),
                   assessment='complete-stored-default-setting',
                   note='One digit and its semicolon fit the three-byte CLI buffer completely. '
                        'The stored setting matches both earlier NVM and DS2. The default-setting '
                        'interface reads DS2; this is not a new measurement of compiled defaults.')
    else:
        assert value == 20180624
        obj.update(calendar_date='2018-06-24',
                   assessment='complete-cached-printer-build-date',
                   note='Complete scalar 20180624 matches the independently read live cache and '
                        'both prior NVM and DS2 observations for DSID_MFG_DATE_EPOCH. This is the '
                        'stored printer build/manufacture date used by the pen subsystem, not '
                        'the firmware compilation date. No date override was observed.')
        cached = next(x for x in rec['live_evidence'] if x['label'] == 'cached_printer_build_date')
        assert int(cached['words_be'][0], 16) == value
        assert sha((ROOT / cached['response_file']).read_bytes()) == cached['response_sha256']
        obj['matches_live_cache'] = True
        obj['live_cache_source'] = cached
    results.append(obj)
    common = {k: obj[k] for k in ['response_file', 'response_sha256', 'assessment', 'note']}
    annotations.append(dict(common, valid=True, complete=True))
    quality.append(dict(common, complete=True))

report = dict(schema_version=1, generated_utc=datetime.now(timezone.utc).isoformat(),
              provenance=dict(serial_access_by_reviewer='none', image_sha256=sha(DATA),
                              baseline='references/round6-baseline.json', baseline_count=698,
                              source_session=dict(path=(capture / 'session.json').relative_to(ROOT).as_posix(),
                                                  sha256=sha(session_raw))),
              summary=dict(physical_commands=len(results), validated_responses=len(results),
                           new_unique_names=len({x['name'] for x in results}),
                           new_unique_handlers=len({x['handler'] for x in results}),
                           default_setting_values=[r['value'] for r in results if r['name'] == 'smgr_pe.get_default_setting'],
                           printer_build_date=20180624),
              response_revalidations=annotations, data_quality=quality, results=results,
              limitations=['Stored values and cached metadata do not establish physical hardware health.',
                           'Every raw response and prior evidence file is hash-verified; historical files are unchanged.'])
(REF / 'round6_hardware_results.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
print(json.dumps(report['summary']))
