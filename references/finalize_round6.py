"""Validate, index and summarize the five additional physical command names."""
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from capture_nvm_mirror import payload

REF = Path(__file__).resolve().parent
ROOT = REF.parent
directory = REF/'physical-73-event-query'
session = json.loads((directory/'session.json').read_bytes())
assert session['stop_reason'] == 'batch-complete' and len(session['commands']) == 1
row = session['commands'][0]
raw = (directory/row['response_file']).read_bytes()
assert len(raw) == row['response_bytes'] and hashlib.sha256(raw).hexdigest() == row['response_sha256']
value = re.sub(r'\s+', ' ', payload(raw.decode('latin-1'))).strip()
assert value == 'get_event for seq_num: 0 got event for seq_num: 1266 got event occurences: 1 got event: 72234 with buf: FROM_NVM'
source = {'response_file':directory.name+'/'+row['response_file'], 'response_sha256':row['response_sha256']}
note = ('Existing event sequence1266, event ID72234, string FROM_NVM match the independently captured RAM record. '
        'The first printed sequence0 is a handler diagnostic bug: it prints the zeroed output before lookup. '
        'The occurrences field1 is the getter presence flag, not a measured occurrence count. No event was added or cleared.')
event = {'response_revalidations':[{**source, 'valid':True, 'complete':True, 'assessment':'existing-event-matches-independent-ring-snapshot', 'note':note}],
         'data_quality':[{**source, 'complete':True, 'assessment':note}]}
(REF/'round6_event_results.json').write_text(json.dumps(event,indent=2)+'\n',encoding='utf-8')

reports = ['round6_system_results.json','round6_hardware_results.json','round6_product_results.json',
           'round6_event_results.json','round6_metadata_results.json']
merged = {'generated_utc':datetime.now(timezone.utc).isoformat(), 'source_reports':[],
          'response_revalidations':[], 'data_quality':[]}
for filename in reports:
    data = (REF/filename).read_bytes()
    report = json.loads(data)
    digest = hashlib.sha256(data).hexdigest()
    merged['source_reports'].append({'file':filename, 'sha256':digest})
    for key in ['response_revalidations','data_quality']:
        for entry in report[key]:
            merged[key].append({**entry, 'analysis_report':filename, 'analysis_report_sha256':digest})
for key in ['response_revalidations','data_quality']:
    seen = set()
    for entry in merged[key]:
        relative = entry['response_file'].replace('\\','/').removeprefix('references/')
        assert relative not in seen and '..' not in Path(relative).parts and not Path(relative).is_absolute()
        seen.add(relative)
        assert hashlib.sha256((REF/relative).read_bytes()).hexdigest() == entry['response_sha256']
(REF/'physical-round6-assessments.json').write_text(json.dumps(merged,indent=2)+'\n',encoding='utf-8')

baseline = json.loads((REF/'round6-baseline.json').read_bytes())
captures = sorted(p for p in REF.glob('physical-*') if p.is_dir() and
    (m:=re.match(r'physical-(\d+)',p.name)) and 68<=int(m[1])<=74)
assert len(captures) == 7
commands = []
for cap in captures:
    sess = json.loads((cap/'session.json').read_bytes())
    assert sess['ended_utc'] and sess['stop_reason'] == 'batch-complete'
    for entry in sess['commands']:
        raw = (cap/entry['response_file']).read_bytes()
        assert entry['prompt_returned'] and entry['status'] == 'prompt-returned'
        assert len(raw) == entry['response_bytes'] and hashlib.sha256(raw).hexdigest() == entry['response_sha256']
        commands.append((cap,entry))
prior = set(baseline['invoked_names'])
new_names = sorted({entry['name'] for _,entry in commands if 'name' in entry} - prior)
assert len(commands) == 98 and len(new_names) == 5
with (REF/'last_index_build.txt').open('w',encoding='utf-8') as out:
    subprocess.run([sys.executable,str(REF/'build_reference_index.py')],cwd=ROOT,stdout=out,check=True)
summary = json.loads((REF/'summary.json').read_bytes())
assert summary['registered_names_invoked'] == baseline['registered_names_invoked'] + 5 == 703
assert summary['transmissions_with_command_records'] == baseline['transmissions'] + 98 == 3226
assert all(x['wire_matches_rx_bin'] for x in json.loads((REF/'capture_integrity.json').read_bytes()))
annotations = {x['response_file'].replace('\\','/').removeprefix('references/'):x for x in merged['response_revalidations']}
lines = ['HP DeskJet 2620 — next five commands, physically tested',
    'COM16, 115200 8N1. Registered names invoked: 698 -> 703 of 2,235.',
    'Eight focused invocations plus 90 metadata snapshot operations. All 98 returned prompts.', '',
    'Results:',
    '- Stored printer build date: 20180624 (2018-06-24), matching earlier NVM/DS2.',
    '- Default-setting interface: ColorLok/dry-time/eject = 1/2/1. These are DS2 values, not compiled factory defaults.',
    '- Root-directory heap block: 16,928 bytes, non-purgeable, unlocked, priority 1:0, sampled pin count 0.',
    '- Stored event sequence 1266: event ID 72234, payload FROM_NVM. No event was created or cleared.',
    '- Consumables: cached presence BLACK=1, CMY=0; chute mapping BLACK=1, CMY=0; channels BLACK and CYAN/MAGENTA/YELLOW.',
    '  This is cached firmware state, not a fresh electrical cartridge test or ink-volume measurement.', '',
    'Exact commands and complete replies follow. All UART output: all_uart_output.txt',
    'Hash-bound validation: physical-round6-assessments.json', '']
records = []
for name in new_names:
    lines.append(name)
    for cap, entry in commands:
        if entry.get('name') != name:
            continue
        relative = cap.name+'/'+entry['response_file']
        check = annotations[relative]
        assert check['valid'] is True and check.get('complete') is not False
        text = payload((cap/entry['response_file']).read_bytes().decode('latin-1'))
        lines.extend(['  '+entry['command'], *['  '+s for s in text.splitlines()],
                      '  Source: '+relative, ''])
        records.append({'name':name,'command':entry['command'],'response_file':relative,'response_sha256':entry['response_sha256']})
lines.extend(['Event diagnostic caveat: its first sequence0 is printed before lookup; the actual result is1266.',
              'Its occurrences1 field is a presence flag, not an occurrence counter. Original bytes are unchanged.'])
(REF/'round6-results.txt').write_text('\n'.join(lines)+'\n',encoding='utf-8')
audit = {'sessions':len(captures),'transmissions':len(commands),'all_prompts_returned':True,
         'all_hashes_lengths_and_wire_streams_match':True,'new_registered_names':new_names,
         'baseline_invoked':698,'total_invoked':703,'focused_invocations':records,
         'source_assessment_sha256':hashlib.sha256((REF/'physical-round6-assessments.json').read_bytes()).hexdigest()}
(REF/'round6-audit.json').write_text(json.dumps(audit,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'new_names':5,'total_names':703,'transmissions_this_round':98,'focused_invocations':len(records)}))
