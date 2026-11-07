"""Prepare two additional metadata queries, requiring real provider evidence."""
import argparse
import hashlib
import json
import struct
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
REF=ROOT/'references'
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('provider_report',help='Actual provider-pointers JSON file')
args=parser.parse_args()
source=Path(args.provider_report)
if not source.is_absolute():source=ROOT/source
observations=json.loads(source.read_text())['observations']
image=(ROOT/'artifacts/ghs/dram.bin').read_bytes()
inventory=json.loads((REF/'static_inventory.json').read_text())
descriptors={x['name']:x for x in inventory['commands']+inventory['hierarchical_commands_with_static_registration']}
coverage={x['name']:x for x in json.loads((REF/'coverage.json').read_text())['commands']}
required={0x209e3e6c:4,0x20a3133c:4}
proven=[]
for address,count in required.items():
    item=next(x for x in observations if int(x['address'],16)==address and x['count']>=count)
    raw=(source.parent/item['response_file']).read_bytes()
    assert hashlib.sha256(raw).hexdigest()==item['response_sha256']
    assert bytes.fromhex(item['hex'])[:4].hex()=='a061f594'
    proven.append(item)

def string(address):
    offset=address&0x3ffffff
    return image[offset:offset+256].split(b'\0',1)[0].decode('ascii')

fields=[]
for field_id in range(40):
    at=0x54bf88+16*field_id
    name_ptr,flag_ptr,value_ptr=struct.unpack_from('>III',image,at)
    kind=image[at+12]
    if not kind:continue
    assert kind in range(1,8) and 0x209e4860<=value_ptr<=0x209e49e0
    fields.append({'field_id':field_id,'name':string(name_ptr),'type':kind,'enable_flag_address':f'0x{flag_ptr:08x}',
                   'value_address':f'0x{value_ptr:08x}','width_bytes':{1:1,2:1,3:2,4:2,5:4,6:4,7:1}[kind],
                   'read_signed':kind in [2,4], 'print_signed':True})
assert len(fields)==38
snapshot=next((x for x in observations if int(x['address'],16)==0x209e4860 and x['count']>=388),None)
if snapshot:
    assert hashlib.sha256((source.parent/snapshot['response_file']).read_bytes()).hexdigest()==snapshot['response_sha256']
    snapshot_bytes=bytes.fromhex(snapshot['hex'])
    for field in fields:
        offset=int(field['value_address'],16)-0x209e4860
        raw=snapshot_bytes[offset:offset+field['width_bytes']]
        value=int.from_bytes(raw,'big',signed=field['read_signed'])
        if value>=2**31:value-=2**32
        field['printed_value_at_prerequisite_capture']=value

common='Physical provider globals resolve to A061F594. Physical57 proved object -> vtable A061F598, parser+10=A0431979 -> A02C3DE2 decimal scanner and output+24=A0431A59 -> A02C2A7A UART writer. Formatting helpers A04F0BC4/A04F0B50 and length helper A0010538 only operate on bounded local buffers.'
ip_proof=[
    'A01D26BE..2752 reads two decimal arguments. Explicit index1..39 excluding18 stays within the40-entry table at A054BF88 and selects a nonzero type byte. The second argument0 selects labeled decimal output; no configuration write is executed.',
    'The16-byte descriptor contains name, enable-flag address, value address, type byte. This handler reads only value address+8, then exactly1,2 or4 bytes according to type1..7. Types2/4 sign-extend; others zero-extend or load a word. Actual integer formatting uses%d, so32-bit values are displayed signed. The handler has no callback or storage-read path beyond parser/UART helpers.',
    'A01D25D0 allocates256 bytes on the stack, formats the fixed short string "IP override setting:%d is %d.\\n", computes length, and writes through the captured output provider. No direct writes target descriptor data or override values.',
    common]
jmis_proof=[
    'A020C368..C3D6 initializes index0. A020C1D8 accepts only this first index, writes the constant name basic to caller-local output, then A020C17C returns fixed resource descriptor A055A780. Every later index returns null, ending the traversal after one row.',
    'The descriptor header is bimodal0,count1. The handler sums count1 minimum words at+8 and maximum words at+30: each1048576 bytes. It reads no dynamic resource provider and invokes no allocation/job-start path. A020C1F0 is an adjacent job-start implementation and is unreachable from this query.',
    'A04F0B50 formats the fixed profile line into a128-byte local buffer. It obtains provider20A3133C and calls only its UART method+24. Output is exactly one row with ordinal1, profile basic, minimum1048576, maximum1048576, bimodal0.',
    common]
rows=[]
def add(name,argument,purpose,proof,expected):
    assert coverage[name]['status']=='not-invoked'
    descriptor=descriptors[name]
    rows.append({'name':name,'command':f'udws "{name}{(" "+argument) if argument else ""};"',
                 'handler':descriptor['handler'],'descriptor_address':descriptor['descriptor_address'],
                 'context_word':descriptor['context_word'],'argument_syntax':argument or 'no arguments',
                 'classification':'reviewed-system-metadata-query','purpose':purpose,'control_flow_proof':proof,
                 'side_effects_assessment':'UART output and local parsing/formatting storage only; no settings writes, job scheduling or actuator operations.',
                 'hardware_access':False,'timeout':20,'prior_invocation_status':'not-invoked',
                 'review_source':'references/round7_system_review.json','expected':expected})

for field in fields:
    add('ip.get',f"{field['field_id']} 0",f"Read the current stored IP override value for {field['name']}.",ip_proof,field)
resource=struct.unpack_from('>26I',image,0x55a780)
assert resource[0]==0 and resource[1]==1 and resource[2]==resource[12]==1048576
add('jmis.mem_show','','Read the firmware basic-job minimum/maximum memory request.',jmis_proof,
    {'ordinal':1,'profile':'basic','minimum_bytes':1048576,'maximum_bytes':1048576,'bimodal':0})
review={'schema_version':1,'generated_utc':datetime.now(timezone.utc).isoformat(),'serial_access':'none',
        'baseline_registered_names_invoked':703,'image_sha256':hashlib.sha256(image).hexdigest(),
        'provider_report':source.relative_to(ROOT).as_posix(),'provider_report_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
        'provider_observations':proven,'ip_override_value_snapshot':snapshot,
        'provider_object_vtable_evidence':'references/physical-57-provider-pointers/provider-pointers.json',
        'queries':rows,'summary':{'new_registered_names':2,'invocations':len(rows),'ip_valid_fields':len(fields)},
        'interpretation':'ip.get reports override-value storage, not necessarily active image-processing parameters; enable flags are separate and are not inferred. jmis.mem_show reports declared job requirements, not current heap use.',
        'excluded':[{'name':'smjob pri','reason':'A03D3006 writes job priority fields at+14/+18.'},
                    {'name':'smjob can','reason':'A03D33D4 dispatches job state6, a cancel operation.'},
                    {'name':'smjob show','reason':'Repeats prior job/types/blk captures.'},
                    {'name':'heapmgr log show/group','reason':'Known empty live log; omitted redundant output.'}]}
(REF/'round7_system_review.json').write_text(json.dumps(review,indent=2)+'\n',encoding='utf-8')
(REF/'78-system-commands.json').write_text(json.dumps(rows,indent=2)+'\n',encoding='utf-8')
print(json.dumps(review['summary']))
