"""Validate captured heap and RAM-partition diagnostics without serial access."""
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
REF=ROOT/'references'
CAP=REF/'physical-58-heap-diagnostics'
s=json.loads((CAP/'session.json').read_text())
assert s['stop_reason']=='batch-complete' and len(s['commands'])==9
texts={}
responses=[]
for row in s['commands']:
    path=CAP/row['response_file'];raw=path.read_bytes();digest=hashlib.sha256(raw).hexdigest()
    assert row['prompt_returned'] and row['response_sha256']==digest and row['response_bytes']==len(raw)
    text=raw.decode('ascii').replace('\r','').lstrip('\n')
    prefix='-> '+row['command']+'\n';assert text.startswith(prefix)
    text=text[len(prefix):]
    end=re.search(r';udws\(\) returns 0 \(0x0\)\n+-> $',text);assert end
    texts[row['name']]=text[:end.start()].strip('\n')
    responses.append(dict(name=row['name'],response_file=path.relative_to(REF).as_posix(),response_sha256=digest,response_bytes=len(raw),prompt_returned=True))

clients=[]
for line in texts['heapmgr client prt'].splitlines():
    m=re.fullmatch(r'client_id: (\d+) major_number: (\d+)',line);assert m
    clients.append(dict(client_id=int(m[1]),major_number=int(m[2])))
assert clients==[{'client_id':1,'major_number':0},{'client_id':2,'major_number':1},{'client_id':0,'major_number':0}]
load=re.fullmatch(r'Hash table size: (\d+)\nNumber of entries: (\d+)',texts['heapmgr hash load']);assert load
assert int(load[1])==211
hash_lines=texts['heapmgr hash prt'].splitlines();assert hash_lines.pop(0)=='hash_index\tsys_indexs'
buckets=[]
for line in hash_lines:
    assert re.fullmatch(r'\d+\t\d+(?:\t\d+)*',line)
    values=list(map(int,line.split('\t')));buckets.append(dict(bucket=values[0],system_indices=values[1:]))
assert len(set(x['bucket'] for x in buckets))==len(buckets)
assert all(0<=x['bucket']<211 for x in buckets)
indices=[i for x in buckets for i in x['system_indices']]
assert len(indices)==int(load[2])==8 and len(set(indices))==8
name_lines=texts['heapmgr name prt'].splitlines();assert name_lines.pop(0)=='index\tcount\thdr_ptr'
names=[]
for line in name_lines:
    m=re.fullmatch(r'(\d+)\t(\d+)\t(0x[0-9a-f]+)',line);assert m
    names.append(dict(index=int(m[1]),count=int(m[2]),header=m[3]))
assert sorted(x['index'] for x in names)==sorted(indices)
free_text=texts['heapmgr free prt']
free=re.fullmatch(r'Nonreloc Mem Start: \((0x[0-9a-f]+)\)\nFree List Start: \((0x[0-9a-f]+)\)\nFree Mem - Start:\((0x[0-9a-f]+)\) Size:\((0x[0-9a-f]+) (\d+)\) End:\((0x[0-9a-f]+)\)\nFree List End: \((0x[0-9a-f]+)\)',free_text);assert free
free_block=dict(start=free[3],size_hex=free[4],size_bytes=int(free[5]),end=free[6])
assert int(free[4],16)==int(free[5]) and int(free[3],16)+int(free[5])==int(free[6],16)
assert texts['heapmgr heap prt']==texts['heapmgr heap ls']=='The heap is empty.'
assert texts['heapmgr heap mem']=='sizeof blocks with priority > 100 : 0'
part=re.fullmatch(r'partition # : (\d+)  name : ([^,]+),  type : ([^,]+),  block_size : (\d+)\n\s+curr_size = (\d+), available_mem = (\d+)\n 0',texts['ram.part_info']);assert part
partition=dict(index=int(part[1]),name=part[2],type=part[3],block_size=int(part[4]),current_size_raw=int(part[5]),available_memory_bytes=int(part[6]),return_code=0)
assert partition['index']==0 and partition['name']=='/' and partition['block_size']==16384
notes={
 'heapmgr client prt':'Two client-to-major-number mappings plus one zero sentinel were returned. This command prints major_number, not compaction priority; prior purpose wording is corrected here.',
 'heapmgr free prt':'Single reported free block; hexadecimal and decimal sizes match, and start+size=end exactly. Other pool/free counters are distinct in scope and sampled at different times.',
 'heapmgr hash load':'211 buckets and8entries; count independently agrees with hash prt and name prt.',
 'heapmgr hash prt':'Eight unique indices in eight nonempty valid buckets; same eight indices appear in name prt.',
 'heapmgr name prt':'Eight named block entries with reference counts and header addresses; matches hash-table enumeration.',
 'heapmgr heap prt':'Relocation-tree root is empty. Existing named blocks/free memory are reported separately; this does not mean the global allocator has no allocations.',
 'heapmgr heap ls':'Relocation-tree root is empty. This exercises a valid empty query state; block-row formatting is unexercised.',
 'heapmgr heap mem':'Priority-filtered sum0 agrees with empty relocation-tree state; does not measure total installed or used RAM.',
 'ram.part_info':'Partition0 root /,heap type,16KiBblocksize,current_size105404,available_memory46556576; successful0return. current_size retains firmware-native units until independently established.'}
out={'schema_version':1,'generated_utc':datetime.now(timezone.utc).isoformat(),'source':'physical-58-heap-diagnostics',
 'summary':{'responses':9,'all_prompts_returned':True,'registered_names_invoked':9,'client_mappings':2,'hash_buckets':211,'named_blocks':8,'free_block_bytes':free_block['size_bytes'],'relocation_tree_empty':True,'root_partition_block_size':16384},
 'session_sha256':hashlib.sha256((CAP/'session.json').read_bytes()).hexdigest(),
 'clients':clients,'hash_buckets':buckets,'named_blocks':names,'free_block':free_block,'partition':partition,'responses':responses,
 'response_revalidations':[dict(response_file=x['response_file'],response_sha256=x['response_sha256'],valid=True,assessment='validated-heap-or-partition-diagnostic',note=notes[x['name']]) for x in responses],
 'data_quality':[],'serial_access':'none'}
(REF/'round5_heap_results.json').write_text(json.dumps(out,indent=2)+'\n',encoding='utf-8')
print(json.dumps(out['summary'],indent=2))
