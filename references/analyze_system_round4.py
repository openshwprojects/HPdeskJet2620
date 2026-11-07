"""Validate captured module diagnostics against each other and static descriptors."""
import hashlib
import json
import re
import struct
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / 'references'
CAP = REF / 'physical-49-system-metadata'
image = (ROOT / 'artifacts/ghs/dram.bin').read_bytes()
session = json.loads((CAP / 'session.json').read_text())
assert session['ended_utc'] and len(session['commands']) == 16
responses = []
texts = {}
for row in session['commands']:
    path = CAP / row['response_file']
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    assert digest == row['response_sha256']
    assert len(raw) == row['response_bytes'] and row['prompt_returned']
    text = raw.decode('latin1').replace('\r', '')
    lines = [x.rstrip() for x in text.splitlines() if x.strip()]
    assert lines[0] == '-> ' + row['command']
    assert lines[-2:] == [';udws() returns 0 (0x0)', '->']
    texts[row['name']] = lines[1:-2]
    responses.append({'command': row['command'], 'name': row['name'],
        'response_file': path.relative_to(REF).as_posix(), 'response_sha256': digest,
        'response_bytes': len(raw), 'prompt_returned': True,
        'valid': True, 'complete': True})

modules = []
for line in texts['sm_module mod'][2:]:
    m = re.fullmatch(r'\s*(\d+)\s+(\d+)\s+(\w+)\s+([0-9a-f]{4})\s+(.+?)\s*', line)
    assert m, repr(line)
    idx, rank, typ, cid, name = m.groups()
    early = name.endswith('early_start')
    if early:
        name = name[:-len('early_start')].rstrip()
    modules.append({'index': int(idx), 'rank': int(rank), 'type': typ,
                    'cid': '0x'+cid, 'name': name, 'early_start': early})
assert [x['index'] for x in modules] == list(range(364))
names = [x['name'] for x in modules]
assert len(set(names)) == len(names)

tasks = []
priorities = {}
for tid, priority in struct.iter_unpack('>II', image[0x607d54:0x607d54 + 603*8]):
    priorities.setdefault(tid, priority)  # firmware returns FIRST match, not last
for line in texts['sm_module task'][2:]:
    m = re.fullmatch(r'\s*(\d+)\s+(.+?)\s+([0-9a-f]{8})\s+(\d+)\s+(\d+)\s+([0-9a-f]{8})\s+(\S+)\s*', line)
    assert m, repr(line)
    idx, module, tid, priority, stack, entry, task_name = m.groups()
    tid_int = int(tid, 16)
    assert module in names
    assert priorities.get(tid_int, 240) == int(priority)
    occurrences = []
    start = 0
    while True:
        pos = image.find(tid_int.to_bytes(4, 'big'), start)
        if pos < 0:
            break
        start = pos + 1
        if pos + 16 > len(image):
            continue
        _, name_ptr, descriptor_stack, descriptor_entry = struct.unpack('>IIII', image[pos:pos+16])
        name_offset = name_ptr & 0x3ffffff
        if (0xa0000000 <= name_ptr < 0xa4000000
                and descriptor_stack == int(stack) and descriptor_entry == int(entry, 16)
                and image[name_offset:name_offset+128].split(b'\0')[0].decode('latin1') == task_name):
            occurrences.append(f'0x{0xa0000000+pos:08x}')
    assert occurrences, (tid, task_name)
    tasks.append({'index': int(idx), 'module': module, 'tid': '0x'+tid,
                  'priority': int(priority), 'stack_bytes': int(stack), 'entry': '0x'+entry,
                  'task_name': task_name, 'matching_static_descriptors': occurrences})
assert [x['index'] for x in tasks] == list(range(137))

tables = {}
for command, tag in [('sm_module mem', 'alloc'), ('sm_module tim', 'time')]:
    rows = []
    for line in texts[command]:
        m = re.fullmatch(r'\s*(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(.+?)\s*', line)
        if m and not m[4].startswith('('):
            rows.append({'name': m[4], 'values': [int(m[i]) for i in range(1, 4)]})
    assert [x['name'] for x in rows] == names
    pattern = rf'\s*(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+\({tag} total: (-?\d+)\)'
    totals = [re.fullmatch(pattern, line) for line in texts[command]]
    totals = [x for x in totals if x]
    assert len(totals) == 1
    columns = [int(totals[0][i]) for i in range(1, 4)]
    assert [sum(x['values'][i] for x in rows) for i in range(3)] == columns
    total = int(totals[0][4])
    assert sum(columns) == total
    tables[command] = {'rows': rows, 'column_totals': columns, 'grand_total': total}

interfaces = {}
for key, lines in texts.items():
    if not key.startswith('sm_module sysifc '):
        continue
    assert lines[0].strip() == '#  Total Modules under traversal'
    assert lines[2] == '364 (Decimal)'
    assert lines[3].strip().startswith('#  Filtered Modules providing specified handler')
    filtered = lines[5:]
    assert all(x in names for x in filtered)
    order = [names.index(x) for x in filtered]
    assert order == sorted(set(order), reverse=True)
    interfaces[key.rsplit(' ', 1)[1]] = {'total_modules_traversed': 364,
        'filtered_count': len(filtered), 'module_names': filtered,
        'completeness_basis': 'Final prompt and wrapper; complete lines form unique reverse-order subset of independent364-row module list. Exact callback membership not separately reconstructed from runtime vtables.'}

assert texts['sm_module stat'] == ['Sirius system_start_complete done']
mem_text = '\n'.join(texts['sm_module mem'])
current_available = int(re.search(r'Current available:\s+(\d+)', mem_text)[1])
data = {
    'schema_version': 1, 'generated_utc': datetime.now(timezone.utc).isoformat(),
    'source': 'physical-49-system-metadata',
    'image_sha256': hashlib.sha256(image).hexdigest(),
    'session_sha256': hashlib.sha256((CAP/'session.json').read_bytes()).hexdigest(),
    'summary': {'responses': len(responses), 'all_prompts_returned': True,
        'module_count': len(modules), 'task_count': len(tasks),
        'all_task_fields_match_static_descriptors': True,
        'all_task_priorities_match_first_static_lookup': True,
        'memory_and_timing_tables_match_all_modules_in_order': True,
        'all_table_column_sums_match_printed_totals': True,
        'system_state': texts['sm_module stat'][0],
        'configured_task_stack_total_bytes': sum(x['stack_bytes'] for x in tasks),
        'current_available_counter_bytes': current_available,
        'current_available_counter_scope': 'A042BE56 selector2 cached allocator counter at20DEC590; not all installedRAM or total free memory across every pool.',
        'init_task_start_allocation_totals_bytes': tables['sm_module mem']['column_totals'],
        'startup_timing_totals_raw': tables['sm_module tim']['column_totals'],
        'timing_units': 'Retained as raw firmware counters; no independent scale calibration in this capture.',
        'interface_counts': {k:v['filtered_count'] for k,v in interfaces.items()},
        'no_serial_access_by_analyzer': True},
    'responses': responses, 'modules': modules, 'tasks': tasks,
    'memory': tables['sm_module mem'], 'startup_timing': tables['sm_module tim'],
    'system_interfaces': interfaces,
    'notable_firmware_detail': 'Task priority table contains duplicateTID0x11dbd: first priority232, later240. The physical netdiag row reports232, consistent with first-match lookup.',
    'response_revalidations': [dict(response_file=x['response_file'], response_sha256=x['response_sha256'],
        valid=True, assessment='validated-cached-system-metadata',
        note=('Complete framing and contiguous module/task indices; task static descriptor/priority matches, ordered cross-table364-module consistency and exact arithmetic totals verified where applicable. Interface filters checked as unique reverse-order module subsets; independent runtime-vtable membership remains unverified.')) for x in responses],
    'data_quality': [],
}
(REF/'useful_system_round4_results.json').write_text(json.dumps(data, indent=2)+'\n', encoding='utf-8')
print(json.dumps(data['summary'], indent=2))
