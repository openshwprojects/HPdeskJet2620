"""Offline system query review; never imports serial or accesses the printer."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / 'references'
inv = json.loads((REF / 'static_inventory.json').read_text())
desc = {x['name']: x for x in inv['commands'] + inv['hierarchical_commands_with_static_registration']}
coverage = {x['name']: x for x in json.loads((REF / 'coverage.json').read_text())['commands']}
image_hash = hashlib.sha256((ROOT / 'artifacts/ghs/dram.bin').read_bytes()).hexdigest()
rows = []


def add(name, purpose, proof, args='', timeout=30, effects='UART formatting, stack temporaries, and bounded scheduling delays only.', limits='Finite cached metadata traversal; verify returned prompt and complete row grammar.'):
    d = desc[name]
    rows.append({
        'name': name, 'command': f'udws "{name}{(" " + args) if args else ""};"',
        'handler': d['handler'], 'descriptor_address': d['descriptor_address'],
        'context_word': d['context_word'], 'argument_syntax': args or 'no arguments',
        'classification': 'reviewed-cached-system-metadata-query', 'purpose': purpose,
        'control_flow_proof': proof, 'side_effects_assessment': effects,
        'hardware_access': False, 'timeout': timeout, 'output_limits': limits,
        'prior_invocation_status': coverage[name]['status'],
        'review_source': 'references/round5_system_review.json', 'image_sha256': image_hash,
    })


add('sm_module conn', 'Map live resolved module interface connections and endpoint addresses for emulator initialization.',
    'A03D6E28..A03D6EAC iterates the cached 24-byte connection table at20DAA478, bounded by20DAA47C. Reads requester/provider indices and interface IDs/addresses; A03D64D0 only resolves module names. Format/direct-vtable label and every30row scheduling delay are the only calls besides output. No interface method is invoked.', timeout=90)
add('sm_module prov', 'List provider interface IDs, types, names and addresses.',
    'A03D6F34..A03D6FC8 iterates20DAA474 module descriptors, count20DAA488. Per-module+0C provider table has24-byte descriptors and+10 count. Reads fields+8,+C,+10 and module name+24, never calls provider methods. UART formatter and delay every30rows only.', timeout=60)
add('sm_module req', 'List required interfaces, requested provider component and endpoint locations.',
    'A03D6FCA branches across literal strings toA03D7238..72D2. Traverses20DAA474 module descriptors; per-module+14 requirement table,+18 count;24-byte descriptors. A03D6442 maps component ID to static name, and formatter plus bounded delay are remaining calls. No interface method invoked.', timeout=60)
add('sm_module tabl', 'Capture dependency adjacency list, including modules without dependencies.',
    'A03D6EAE..6F32 traverses each module and each cached connection; emits requester/provider names only. A03D64D0 name lookup, A03D7F08 arithmetic veneer (mod30), A02F6D70 delay and formatter only; no graph mutation.', timeout=90)
add('sm_module pevent', 'Read advertised event IDs and names from module metadata.',
    'A03D7AC6..7AFE traverses module descriptor+38 event table and+3C count,8-byte rows; prints16-bit ID and string pointer+4. Only output called. No event emitted.', effects='UART formatting and stack temporaries only.')
add('sm_module revent', 'Read required/subscribed event IDs and names from module metadata.',
    'A03D7B00..7B38 traverses module descriptor+40 event table and+44 count,16-byte rows; prints16-bit ID and string pointer+4. Only output called. No subscriptions changed.', effects='UART formatting and stack temporaries only.')
add('smjob job', 'Read active job IDs, states, job types and control addresses.',
    'A03D36F6..375C takes job-manager mutex20DA51B8, scans exactly50 12-byte records, skips state1 empty records, reads active job fields, then releases mutex. A03D36D4 is a bounded76-entry type-name lookup; A03D36A0 selects constant state text. No job creation/cancellation/dispatch method called.', effects='Job-manager mutex acquisition/release and UART output; no job state mutation.')
add('smjob types', 'Read all registered job types, priority and resource/interface descriptors.',
    'A03D3CD8..3D2E takes the same mutex, scans exactly76 40-byte records at20DA4218, reads ID, fields+4/+8 and name throughA03D36D4; prints and releases mutex. No callback invoked.', effects='Job-manager mutex acquisition/release and UART output; no job state mutation.')
add('smjob blk', 'Read current blocker components, levels, bitmasks and blocked job types.',
    'A03D3D30..3E2E takes job-manager mutex then scans exactly3 316-byte blocker records at20DA4E00, each up to76 job-type words. Name helpers A03D6442/A03D36D4 read metadata; A03D375E renders bitmask to local32-char buffer plus NUL. Releases mutex. No block/allow action called.', effects='Job-manager mutex acquisition/release and UART output; no blocker state mutation.')
add('smjob filter show', 'Read per-job-type filter-enabled status.',
    'A03D4126..41AA reads numeric selector viaA03D3688 (default0), takes job-manager mutex and scans76 type entries. Explicit0 selects all nonzero IDs; reads enabled byte+19 and names, then releases mutex. No filter change routine called.', args='0', effects='Job-manager mutex acquisition/release and UART output; no filter mutation.')

review = {
    'schema_version': 1, 'generated_utc': datetime.now(timezone.utc).isoformat(),
    'provenance': {'kind': 'offline_static_review_for_physical_capture', 'serial_access': 'none',
                   'image': 'artifacts/ghs/dram.bin', 'image_sha256': image_hash},
    'dispatch_proof': 'Hierarchy dispatcherA02C2D0E passes descriptor+0C as opaque context to+8 handler. User argument parsing remains handler-specific.',
    'summary': {'ready_invocations': len(rows), 'new_registered_names': sum(x['prior_invocation_status']=='not-invoked' for x in rows)},
    'queries': rows,
    'held_for_followup': [
        {'name':'heapmgr queries', 'reason':'Awaiting read-only resolution of output provider20943A40 before approving indirect output calls.'},
        {'name':'ram.part_info', 'reason':'Awaiting read-only resolution of provider20D23B24 and vtable helper.'},
        {'name':'sm_module tree', 'reason':'Triple nested364*364*connection-count traversal; disproportionate work for equivalent conn data.'},
        {'name':'smjob show', 'reason':'Aggregator repeats job/types/blk; omitted to prioritize useful independent command behavior.'},
        {'name':'is.info / is.orientation', 'reason':'Image-service provider/payload preconditions unresolved; info failure path may call fatal helper.'},
        {'name':'immd.get / immd.query', 'reason':'Image-metadata provider20838964 and valid IDs require resolution.'},
    ],
}
(REF / '54-system-commands.json').write_text(json.dumps(rows, indent=2)+'\n', encoding='utf-8')
(REF / 'round5_system_review.json').write_text(json.dumps(review, indent=2)+'\n', encoding='utf-8')
print(json.dumps(review['summary']))
