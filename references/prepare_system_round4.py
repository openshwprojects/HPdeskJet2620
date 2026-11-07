"""Offline review artifact builder. Does not import serial or access hardware."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / 'references'
inventory = json.loads((REF / 'static_inventory.json').read_text())
descriptors = {x['name']: x for x in inventory['commands'] + inventory['hierarchical_commands_with_static_registration']}
coverage = {x['name']: x for x in json.loads((REF / 'coverage.json').read_text())['commands']}
image = (ROOT / 'artifacts/ghs/dram.bin').read_bytes()
image_hash = hashlib.sha256(image).hexdigest()
rows = []

def add(name, purpose, proof, effects, timeout=45, limits=None):
    desc = descriptors[name]
    rows.append({
        'name': name, 'command': f'udws "{name};"',
        'handler': desc['handler'], 'descriptor_address': desc['descriptor_address'],
        'context_word': desc['context_word'], 'argument_syntax': 'no arguments',
        'classification': 'reviewed-cached-system-metadata-query',
        'purpose': purpose, 'control_flow_proof': proof,
        'side_effects_assessment': effects, 'timeout': timeout,
        'output_limits': limits or 'Finite traversal of initialized module metadata; raw response and final prompt must be checked for completeness.',
        'hardware_access': False, 'prior_invocation_status': coverage[name]['status'],
        'review_source': 'references/useful_system_round4_review.json',
        'image_sha256': image_hash,
    })

add('sm_module stat', 'Read module manager initialization/shutdown state.',
    'A03D72D6..A03D7644 switches on cached state at20DAA4B4, prints state text and, for transitional states, current module/connection metadata. Helpers A03D64D0/A03D6442 resolve names in existing arrays. All invoked helper paths only read metadata and format text.',
    'UART formatting and stack temporaries only.', 20,
    'Small state-dependent diagnostic, typically one line after startup.')
add('sm_module task', 'List module task IDs, priorities, stack sizes, entry points and task names for emulator comparison.',
    'A03D7646..A03D76CE loops over module count at20DAA488 and each descriptor task count at+8; 16-byte task descriptors at+4. A03D5994 scans603 fixed entries atA0607D54. Prints header atA03D78E8 and row format atA03D796C; delays10ticks every30 rows. No task is created or resumed.',
    'UART formatting, bounded scheduling delays; if a task ID cannot be resolved, helper A03D5994 stores the unresolved ID at20DA5460 before returning fallback240.', 45,
    'Finite task-descriptor traversal. Header labels the fields; values are descriptors, not a live scheduler-state or CPU-utilization report.')
add('sm_module mem', 'Read per-module initialization/task/start memory accounting and overall free-memory totals.',
    'A03D6670..A03D67A4 reads44-byte module records at20DAA480; fields+14/+18/+1C are accumulated into stack counters. A03D4512 calls A042BE56 for selectors0..3; that helper only returns cached free memory20DEC590 for selector2 and0 otherwise. No allocator is called.',
    'UART formatting, stack totals and10tick delays every30rows.', 45,
    'Cached allocation accounting plus a current cached free-memory sample; not a destructive RAM test or physical installed-RAM measurement.')
add('sm_module tim', 'Read per-module startup timing and phase timing totals.',
    'A03D6A6A..A03D6BA6, final formatter atA03D6E20, reads module fields+20/+24/+28 and cached phase counters. A03D20D0 is an ARM veneer toA0010578 for zeroing three local words. A03D67A6 maps phase IDs to constant text. No clock registers are accessed.',
    'UART formatting, stack sums and10tick delays every30rows.', 45,
    'Historical boot/startup timing counters; units must follow returned labels and are not CPU-load measurements.')
add('sm_module mod', 'List all registered modules, ranks, types and component IDs.',
    'A03D64E6..A03D666E reads44-byte module records and prints cached rank/type/CID/name. Descriptor context7 selects all three type bits. A03D64D0 resolves names. Reading method-pointer presence at object vtable+38 does not invoke that method.',
    'UART formatting and10tick delays every30rows.', 45,
    'Finite module list. Context7 is descriptor metadata passed by dispatcher, not a user argument.')

interfaces = [
    ('init', 0x00), ('start', 0x04), ('sdc', 0x08), ('sdi', 0x0c),
    ('query', 0x10), ('svrC', 0x14), ('spl', 0x18), ('swut', 0x1c),
    ('msdt', 0x20), ('scom', 0x24), ('ipcc', 0x28),
]
for label, offset in interfaces:
    add(f'sm_module sysifc {label}', f'List modules implementing system-interface slot {label}.',
        f'A03D7B86 entry prints count, branches across literal data toA03D7D98, then traverses module records in reverse. Context{offset // 4 + 1} selects vtable slot+{offset:X}; A03D7DA2..A03D7EA2 only checks the method pointer for nonzero. A03D7EA4 calls name resolver and formatter; the inspected method is NEVER called. EndA03D7EC0.',
        'UART formatting only; does not execute the named system operation.', 45,
        'Finite module names filtered by method-pointer presence. Header total is traversal population, not filtered match count.')

review = {
    'schema_version': 1, 'generated_utc': datetime.now(timezone.utc).isoformat(),
    'provenance': {'kind': 'offline_static_review_for_physical_capture', 'serial_access': 'none',
        'image': 'artifacts/ghs/dram.bin', 'image_sha256': image_hash,
        'coverage_sha256': hashlib.sha256((REF/'coverage.json').read_bytes()).hexdigest(),
        'runtime_registry': 'references/physical-09-registry/runtime_inventory.json'},
    'dispatch_proof': 'Hierarchy dispatcherA02C2D0E loads handler from descriptor+8 and context intor0 from+0C, thenBLXhandler. Context is not an argument count. Exact no-argument invocations below preserve this dispatch.',
    'summary': {'ready_invocations': len(rows), 'new_registered_names': sum(x['prior_invocation_status']=='not-invoked' for x in rows),
        'claims': 'Static review only; only physical captures establish responses and completion. No serial access by this reviewer.'},
    'queries': rows,
    'held_for_followup': [
        {'name': 'heapmgr client prt / free prt / hash load / hash prt / name prt',
         'reason': 'Queries traverse existing allocation structures, but common output helperA0161270 dereferences provider20943A40. Need establish initialized heap output-provider connection before capture; no guessed pointers or heap mutations.'},
        {'name': 'heapmgr heap prt / heap ls', 'reason': 'Metadata traversal starts descriptor context0 and is bounded4096slots, but additionally confirm provider initialization; printing uses small volatile diagnostic globals.'},
        {'name': 'sm_module conn / tabl / prov / req', 'reason': 'Promising metadata lists, withheld from first batch to prioritize compact task/module diagnostics and avoid another very large output tail.'},
        {'name': 'sm_module th', 'reason': 'Exact same handler/context as task; excluded because it adds no new information.'},
        {'name': 'rev.get_aasic / hw_mcasic.read_all_registers', 'reason': 'No demonstrated prerequisite resolving earlier assertion; broad hardware register sweep not included.'},
    ],
}
(REF/'49-system-commands.json').write_text(json.dumps(rows, indent=2)+'\n', encoding='utf-8')
(REF/'useful_system_round4_review.json').write_text(json.dumps(review, indent=2)+'\n', encoding='utf-8')
print(json.dumps(review['summary']))
