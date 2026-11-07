"""Offline-only review of one additional allocator metadata command."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
REF=ROOT/'references'
image=(ROOT/'artifacts/ghs/dram.bin').read_bytes()
inventory=json.loads((REF/'static_inventory.json').read_text())
descriptors={x['name']:x for x in inventory['commands']+inventory['hierarchical_commands_with_static_registration']}
coverage={x['name']:x for x in json.loads((REF/'coverage.json').read_text())['commands']}
name='heapmgr log block'
assert coverage[name]['status']=='not-invoked'

header_report=REF/'physical-60-ui-metadata/provider-pointers.json'
header=next(x for x in json.loads(header_report.read_text())['observations'] if x['label']=='named_heap_block_header')
raw=(header_report.parent/header['response_file']).read_bytes()
assert hashlib.sha256(raw).hexdigest()==header['response_sha256']
h=bytes.fromhex(header['hex'])
assert h[:8].hex()=='0100000000000001'
provider_report=REF/'physical-57-provider-pointers/provider-pointers.json'
providers={x['label']:x for x in json.loads(provider_report.read_text())['observations']}
for label in ['heap_output_provider','heap_output_provider_object_vtable','heap_output_provider_vtable_methods']:
    item=providers[label]
    assert hashlib.sha256((provider_report.parent/item['response_file']).read_bytes()).hexdigest()==item['response_sha256']
assert providers['heap_output_provider']['hex']=='a061f594'
assert providers['heap_output_provider_object_vtable']['hex']=='a061f598'
vtable=bytes.fromhex(providers['heap_output_provider_vtable_methods']['hex'])
assert int.from_bytes(vtable[0x10:0x14],'big')==0xa0431979
assert int.from_bytes(vtable[0x24:0x28],'big')==0xa0431a59

proof=[
    'A016098E..09AA reads three decimal uint32 words via A01612B8, then A01608F2 copies them to a 12-byte stack key and calls A0160802 with display_filter=0. Exact words 0,16777216,1 select name lookup of the existing root-directory key 0100000000000001; they are not raw addresses.',
    'A0160802..089A calls A015FB5A six times with selectors1,3,2,5,6,7, each output pointing into its own stack. Selector0/4 and all property setters are unreachable. It formats one bounded metadata line and waits one RTOS tick.',
    'A015FB5A -> A015F884: leading key flag is0, so only name-lookup branch A015FCF4 executes. A015FCF4 hashes eight key bytes with A015FB80 -> A000723C CRC and standard remainder trampoline A0025294, reads one of211 initialized buckets, and traverses existing links. A015FBF2 -> A0160D36 -> A015F3D6 resolves an existing descriptor and A016005A copies eight bytes to a local key buffer; A0010550 compares exactly eight bytes. A015FB92 -> A0160D36 -> A016007E reads the next existing link. No hash insert/remove or allocator path is reachable.',
    'On a key match A0160D48 reads descriptor generation into caller-local storage. A0160D1A -> A0160C60 -> A015F3D6 checks index, non-null header and generation, then reads the existing header pointer. The successful branch was physically exercised for this exact key by ram.block_info in physical61, and the whole hash index was captured in physical58 (8 entries, this block index1 in bucket118).',
    'A0160004 reads selected header fields: selector1=byte26 bit0 purgeable, selector3=byte24 client/major priority field, selector2=byte25 minor priority, selector5=word8 size, selector6=byte27 pin/use count, selector7=byte26 bit1 lock. Its only writes are to each caller-provided stack output.',
    'A0161270 formats into a256-byte stack buffer, gets length, and calls provider vtable+24. Physical57 resolves 20943A40 -> A061F594 -> A061F598; +24=A0431A59 -> A02C2A7A UART writer; parser+10=A0431979 -> A02C3DE2 decimal parser. The fixed line format and integer arguments fit the formatting buffer.',
    'A02F6D70(1) checks scheduler state through A02F7144 and invokes the established RTOS delay A04E6650. This updates volatile current-thread delay/scheduler counters for one tick and returns; it does not access storage or command actuators. This delay path is already used by captured heap tree diagnostics.'
]
row={
    'name':name,'command':'udws "heapmgr log block 0 16777216 1;"',
    'handler':descriptors[name]['handler'],'descriptor_address':descriptors[name]['descriptor_address'],
    'context_word':descriptors[name]['context_word'],
    'argument_syntax':'lookup flags=0, key high word=16777216 (0x01000000), key low word=1',
    'classification':'reviewed-existing-block-metadata-query',
    'purpose':'Read allocator purgeability, lock flag, client/priority fields, pin count and size for the existing root-directory block.',
    'control_flow_proof':proof,
    'side_effects_assessment':'UART output, parser/formatter stack storage and a one-tick RTOS scheduling delay. No allocator mutation, file write, persistent setting or actuator operation.',
    'output_limits':'Exactly one metadata line from6 property reads. Hash walks existing finite chain; prior complete capture has8 entries. Existing-key lookup errors are not checked by the formatter, so raw output must be cross-validated against the known header rather than assuming any prompt means valid data.',
    'timeout':20,'hardware_access':False,'prior_invocation_status':'not-invoked',
    'review_source':'references/round6_system_review.json',
    'expected':{'key_words':['0x00000000','0x01000000','0x00000001'],'purgeable':False,'locked':False,
                'priority_major_raw':h[24],'priority_minor_raw':h[25],'size_bytes':int.from_bytes(h[8:12],'big'),
                'pin_count_at_header_capture':h[27],'pin_count_is_time_dependent':True}}

review={'schema_version':1,'generated_utc':datetime.now(timezone.utc).isoformat(),
        'no_serial_access':True,'image_sha256':hashlib.sha256(image).hexdigest(),
        'baseline_registered_names_invoked':698,'queries':[row],
        'source_header_report':header_report.relative_to(ROOT).as_posix(),'source_header':header,
        'provider_evidence':provider_report.relative_to(ROOT).as_posix(),
        'prior_same_key_semantic_validation':'references/round5_system_extra_results.json',
        'remaining_candidates':[
            {'name':'heapmgr log show','arguments':'0','classification':'conditional-logged-block-reader',
             'proof':'A0160942 -> A016091E -> A0160908 -> A016089C scans the recorded12-byte keys at20943718 with three all-one masks. Each selected key uses the exact property/formatting chain reviewed above. Argument0 disables suppressing certain purgeable/unpinned records. Prints matching-key total.',
             'prerequisite':'Read20943710 count word and confirm1..64 then capture count*12 key bytes beginning20943718. If count0, omit as redundant empty-table evidence. No command prepared until count known.'},
            {'name':'heapmgr log group','classification':'deferred-overlap',
             'reason':'Same log traversal with3 per-word bitmasks and display-filter argument. Not prepared merely to pad distinct-name count when show/block suffice.'},
            {'name':'heapmgr log debug/client','classification':'setter-excluded',
             'reason':'Write logging-enable fields20943A1C/20943714; outside this read-only metadata batch.'}]}
log_report=REF/'physical-69-provider-prerequisites/provider-pointers.json'
if log_report.exists():
    log=next(x for x in json.loads(log_report.read_text())['observations'] if x['label']=='heap_log_count_and_flags')
    assert hashlib.sha256((log_report.parent/log['response_file']).read_bytes()).hexdigest()==log['response_sha256']
    assert log['hex']=='0000000000000000'
    review['log_prerequisite_result']={'source_report':log_report.relative_to(ROOT).as_posix(),
                                     'observation':log,'decision':'Do not invoke heapmgr log show/group; live log contains no entries.'}
    for candidate in review['remaining_candidates'][:2]:candidate['classification']='omitted-empty-live-log'
(REF/'round6_system_review.json').write_text(json.dumps(review,indent=2)+'\n',encoding='utf-8')
(REF/'70-system-commands.json').write_text(json.dumps([row],indent=2)+'\n',encoding='utf-8')
print(json.dumps({'ready_names':[name],'command':row['command'],'expected':row['expected']}))
