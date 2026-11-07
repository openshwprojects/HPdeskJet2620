"""Offline review of heap and partition queries after provider-pointer capture."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / 'references'
data = (ROOT / 'artifacts/ghs/dram.bin').read_bytes()
inv = json.loads((REF / 'static_inventory.json').read_text())
desc = {x['name']: x for x in inv['commands'] + inv['hierarchical_commands_with_static_registration']}
coverage = {x['name']: x for x in json.loads((REF / 'coverage.json').read_text())['commands']}
provider_path = REF / 'physical-57-provider-pointers/provider-pointers.json'
providers = json.loads(provider_path.read_text())
observed = {x['label']: x for x in providers['observations']}
for label in ['heap_output_provider_object_vtable', 'heap_output_provider_vtable_methods', 'ram_partition_provider_object_vtable', 'ram_partition_provider_vtable_methods']:
    row = observed[label]
    raw = (provider_path.parent / row['response_file']).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == row['response_sha256']
    offset = int(row['address'], 16) & 0x3ffffff
    assert bytes.fromhex(row['hex']) == data[offset:offset + row['count']]
assert observed['heap_output_provider']['hex'] == 'a061f594'
assert observed['ram_partition_provider']['hex'] == 'a053bb24'
rows = []


def add(name, purpose, proof, args='', timeout=45, effects='UART formatting and stack temporaries only.', limits='Finite initialized metadata traversal, but no atomic-snapshot guarantee against concurrent allocator activity.'):
    d = desc[name]
    rows.append({'name': name, 'command': f'udws "{name}{(" " + args) if args else ""};"',
                 'handler': d['handler'], 'descriptor_address': d['descriptor_address'], 'context_word': d['context_word'],
                 'argument_syntax': args or 'no arguments', 'classification': 'reviewed-heap-metadata-query',
                 'purpose': purpose, 'control_flow_proof': proof, 'side_effects_assessment': effects,
                 'timeout': timeout, 'output_limits': limits, 'hardware_access': False,
                 'prior_invocation_status': coverage[name]['status'], 'review_source': 'references/round5_heap_review.json',
                 'image_sha256': hashlib.sha256(data).hexdigest()})


add('heapmgr client prt', 'Read heap client IDs and their memory-compaction priority metadata.',
    'A015F4F6..F516 reads 2-byte client descriptors at2093F6C0, indices0 through cached max at+4 inclusive. Calls only common outputA0161270. No client registration or priority mutation.')
add('heapmgr free prt', 'Read free-memory start/size and free-block list, with total reserved size.',
    'A015F77A..F7BE reads globals208381BC/2093F6C8 and traverses existing free list. A01606AE/A01606D6 call A016068C sentinel comparison and read links only; no coalescing, allocation, compaction or free operation.', timeout=60)
add('heapmgr hash load', 'Read hash bucket count and number of stored memory-block identities.',
    'A015FD62..FD9A loops211 hash buckets at2093F6D4, walks each existing link withA015FB92, and prints two totals. A015FB92 callsA0160D36 -> A015F3D6 bounded descriptor lookup and A016007E field getter; writes only stack outputs.')
add('heapmgr hash prt', 'Capture hash buckets and stored block identities for heap-layout reference.',
    'A015FD9C..FDE0 loops211 buckets, prints only nonempty bucket contents, follows existing links withA015FB92. Same reviewed getter chain as hash load; no writes to hash table.', timeout=60)
add('heapmgr name prt', 'Map heap logical names to live block addresses and reference counts.',
    'A0160D6E..0DA2 scans names starting1, bounded byA015F3D6 cached maximum2093F6BC. Each lookup computes an existing descriptor slot; reads two words and prints only active entries. No name allocation/free.', timeout=60)
add('heapmgr heap prt', 'Capture heap binary-tree structure, block IDs and client metadata.',
    'A01601AC..0252 starts descriptor context0, recursively visits indices2*i+1/2*i+2 only when <=4095 and populated. A0160106 -> A0160D36 -> A015F3D6 returns descriptor, A016008E reads its halfword. Direct formatterA02C37BE; no output-provider call or allocator mutation.',
    effects='UART output; diagnostic indentation/depth globals209436E0/E4 are changed and depth restored;3tick delay per populated node.', timeout=90)
add('heapmgr heap ls', 'Read detailed live heap-block allocation flags, sizes, logical names and addresses.',
    'A0160254..0318 starts context0 and visits populated binary-tree indices<=4095. A0160D36 resolves descriptor, A016008E/00A6/00D2/00E0/00DA only read fields, A016005A copies8 name bytes to209436E8 diagnostic scratch. Direct formatter and3tick per node delay; no allocator mutation.',
    effects='UART output, volatile8-byte diagnostic scratch209436E8 and bounded delays.', timeout=90)
add('heapmgr heap mem', 'Read sum of allocated block sizes with client priority above100.',
    'A0160494..04C8 parses one decimal16-bit threshold via proven provider+10=A0431979; explicit100. CallsA0160368 tree traversal<=4095, A0160106 halfword getter andA016031A which reads metadata through reviewed getter chain. Adds matching sizes into local sum, no allocation.', args='100',
    effects='UART output and temporary traversal-depth global209436F0 increment/decrement; no allocator changes.')
add('ram.part_info', 'Read partition0 capacity, free/used blocks, allocator and file metadata.',
    'A0356C84..6CB2 parses partition selector0, callsA0352F34 and returns status. A0352F34 bounds index<32, rejects inactive slots and only prints cached descriptor fields. Accounting A0355E52 resolves physically captured provider+20=A0161075, which takes heap mutex, reads totalsA015F860, resolves clientA015F4D6, sumsA01604CA and free listA015F7C0, then releases. Optional type2 pool helpersA02CE408/3A4/30C/430/456 take pool mutex and read existing list/count metadata only. No storage or allocator mutation.', args='0', timeout=60,
    effects='UART output, heap/pool mutex acquisition/release, volatile lock accounting counters and traversal-depth scratch. No persistent writes.')

review = {'schema_version':1, 'generated_utc':datetime.now(timezone.utc).isoformat(),
          'image_sha256':hashlib.sha256(data).hexdigest(),
          'provider_evidence':{'path':str(provider_path.relative_to(ROOT)).replace('\\','/'), 'sha256':hashlib.sha256(provider_path.read_bytes()).hexdigest(),
            'all_four_object_or_vtable_captures_match_offline_image':True,
            'heap_output_chain':'20943A40 -> A061F594 -> A061F598; vtable+24=A0431A59 -> A02C2A7A UART byte writer. vtable+10=A0431979 -> A02C3DE2 decimal argument parser.',
            'ram_partition_chain':'20D23B24 -> A053BB24 -> A053BB48; vtable+20=A0161075 accounting getter. Captured words after+20 belong to adjacent data and are not methods.'},
          'summary':{'ready_invocations':len(rows),'new_registered_names':sum(x['prior_invocation_status']=='not-invoked' for x in rows)},
          'queries':rows,
          'held_for_followup':[{'name':'heapmgr log show/group/block','reason':'Helpers must be reviewed further; log command names alone do not establish read-only behavior.'},
                               {'name':'heapmgr log client/debug and other debug toggles','reason':'They write diagnostic enable fields; omitted from useful metadata query round.'},
                               {'name':'ram.block_info','reason':'Requires an existing block descriptor key, omitted until partition metadata yields a valid one.'}]}
(REF/'58-heap-commands.json').write_text(json.dumps(rows,indent=2)+'\n',encoding='utf-8')
(REF/'round5_heap_review.json').write_text(json.dumps(review,indent=2)+'\n',encoding='utf-8')
print(json.dumps(review['summary']))
