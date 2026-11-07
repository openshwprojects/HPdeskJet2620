"""Build the readable new-command checklist from audited physical captures."""
import hashlib
import json
import re
from pathlib import Path
from capture_nvm_mirror import payload

REF = Path(__file__).resolve().parent
baseline = json.loads((REF / 'round5-baseline.json').read_bytes())
summary = json.loads((REF / 'summary.json').read_bytes())
audit = json.loads((REF / 'round5_audit.json').read_bytes())
coverage = json.loads((REF / 'coverage.json').read_bytes())
prior_names = set(baseline['invoked_names'])
# Keep this historical report reproducible after later rounds extend coverage.
new = sorted((r for r in coverage['commands'] if r['name'] in audit['new_names']), key=lambda r:r['name'])
for row in new:
    sources = set(audit['new_names'][row['name']])
    row['tests'] = [test for test in row['tests']
                    if str(Path(test['text_file']).with_suffix('.bin')).replace('\\','/') in sources]
summary['registered_names_invoked'] = audit['summary']['registered_names_invoked_after_round5']
assert len(new) == audit['summary']['new_registered_names'] == 54
assert summary['registered_names_invoked'] - baseline['registered_names_invoked'] == len(new)
assert all(x['wire_matches_rx_bin'] for x in json.loads((REF / 'capture_integrity.json').read_bytes()))
lines = [
    'HP DeskJet 2620 — 54 additional commands tested on the physical printer',
    'COM16, 115200 8N1. Firmware TJP1FN1809AR; board ID 0x68a4.',
    '',
    f"Registered names invoked: {baseline['registered_names_invoked']} -> {summary['registered_names_invoked']} / {summary['unique_registered_names']}.",
    'This round: 17 sessions, 540 transmissions, 540 returned prompts.',
    '80 focused command invocations, 459 operations for 51 RAM metadata snapshots, 1 identity check.',
    'Every new name has a validated query reply. This is not coverage of all arguments or physical operations.',
    '',
    'Useful results:',
    '- 1,900 module connections, 443 providers, 1,900 requirements, 94 event records, 29 job types.',
    '- Eight named heap blocks; 46,414,880-byte free block; 16,928-byte root-directory block.',
    '- BIDI alignment -5 and THETAZ 3; four cartridge dot counters match earlier NVM/DS2.',
    '- 32 media sizes, four media types, one tray and one output bin.',
    '- Current LED matrices match defaults; ColorLok/dry-time/eject settings 1/2/1.',
    '- Retained assertion 0xc4eb99f7: hw_micci2.c line 1733, page count 775.',
    '',
    'Caveats recorded in the evidence:',
    '- 20 connection rows have unknown providers/unreliable enums; do not use them as emulator bindings.',
    '- Empty job/relocation lists and empty/invalid stored barcode are recorded as those results.',
    '- Cover/UI/calibration queries read cached state; they do not verify physical sensors or mechanics.',
    '- The random-byte query needs no inner semicolon: udws "crypto_svc.get_rand_num 16".',
    ' The first semicolon form selected an empty-filename branch and printed an unconditional write claim;',
    ' its response is incomplete. Corrected syntax returned exactly 16 UART bytes.',
    '- NOS item 12 returned signed -1 (0xffffffff data), not a dispatch error; integrity is not independently proven.',
    '- Manufacturing dates below use synthetic fixtures, not this printer serial number.',
    '',
    'Below: all 54 new names with exact commands and real response excerpts.',
    'Excerpts collapse whitespace and stop at 240 characters; referenced TXT/BIN files retain full output.',
    'Full combined UART log: all_uart_output.txt',
    'Evidence: physical-round5-assessments.json; round5_audit.json; capture_integrity.json',
    '',
]
entries = []
for number, row in enumerate(new, 1):
    tests = row['tests']
    complete = [t for t in tests if (t.get('post_capture_validation') or {}).get('valid') is True
                and (t.get('post_capture_validation') or {}).get('complete') is not False
                and (t.get('data_quality') or {}).get('complete') is not False]
    assert complete, ('no validated complete reply',row['name'])
    lines.append(f"{number:02d}. {row['name']}")
    entry = {'name':row['name'], 'invocations':[]}
    for test in tests:
        path = REF / test['text_file']
        rawpath = path.with_suffix('.bin')
        raw = rawpath.read_bytes()
        value = payload(raw.decode('latin-1'))
        excerpt = re.sub(r'\s+', ' ', value).strip()
        if len(excerpt) > 240:
            excerpt = excerpt[:240] + ' [excerpt ends; full response in linked file]'
        proof = test['post_capture_validation']
        assert hashlib.sha256(raw).hexdigest() == proof['response_sha256']
        lines.extend(['    '+test['command'], '    Reply: '+excerpt,
                      '    Assessment: '+proof['assessment'], '    File: '+test['text_file']])
        entry['invocations'].append({'command':test['command'], 'response_file':str(rawpath.relative_to(REF)).replace('\\','/'),
            'response_sha256':proof['response_sha256'], 'complete':proof.get('complete',True),
            'assessment':proof['assessment'], 'excerpt':excerpt})
    lines.append('')
    entries.append(entry)
(REF / 'round5-results.txt').write_text('\n'.join(lines)+'\n',encoding='utf-8')
(REF / 'round5-new-commands.json').write_text(json.dumps({'baseline_invoked':baseline['registered_names_invoked'],
    'total_invoked':summary['registered_names_invoked'], 'new_names':len(entries),
    'commands':entries},indent=2)+'\n',encoding='utf-8')
print(json.dumps({'new_names':len(entries),'listed_invocations':sum(len(x['invocations']) for x in entries)}))
