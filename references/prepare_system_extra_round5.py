"""Prepare one meaningful RAM block query from a captured live header."""
import hashlib
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
REF=ROOT/'references'
source=REF/'physical-60-ui-metadata/provider-pointers.json'
capture=json.loads(source.read_text())
header=next(x for x in capture['observations'] if x['label']=='named_heap_block_header')
raw=(source.parent/header['response_file']).read_bytes()
assert hashlib.sha256(raw).hexdigest()==header['response_sha256']
h=bytes.fromhex(header['hex']);assert len(h)==32
kind=h[0];block=int.from_bytes(h[1:4],'big');inode=int.from_bytes(h[4:8],'big')
assert (kind,inode,block)==(1,1,0)
expected={'kind':'DIR','inode':inode,'block':block,'priority':h[25],
          'size':int.from_bytes(h[8:12],'big'),'purgeable':bool(h[26]&1),'use_count_at_header_capture':h[27]}
query={'name':'ram.block_info','command':f'udws "ram.block_info {kind} {inode} {block};"',
       'handler':'0xa0356983','argument_syntax':'type=1 DIR, inode=1, block=0 from captured header key0100000000000001',
       'classification':'reviewed-existing-block-metadata-query','purpose':'Read metadata of real root-directory block and cross-check known live header fields.',
       'control_flow_proof':'A0356982..6A02 parses type,inode,block. A03559E6 builds12-byte key in locals: key+4=(type<<24)|block, key+8=inode. A0355DD8 uses physically resolved providerA053BB48+1C=A0161055; takes/releases heap mutex throughA0160E84/ED4, callsA015FB5A. A015F884/A015FCF4 use existing hash chain and8-byte name compare; A0160D1A resolves existing name descriptor, thenA0160004 selectors2,1,6,5 read priority,purgeable,use_count,size. Only caller-local output fields and lock accounting modified.',
       'side_effects_assessment':'UART output and volatile mutex accounting. Does not write the target block, allocate, free or change a file.',
       'timeout':20,'hardware_access':False,'prior_invocation_status':'not-invoked','expected':expected,
       'review_source':'references/round5_system_extra_review.json'}
review={'schema_version':1,'query':query,'source_header':header,'source_header_report':str(source.relative_to(ROOT)).replace('\\','/'),
        'provider_evidence':'references/physical-57-provider-pointers/provider-pointers.json',
        'interpretation':'This is a DIR block key, not a raw pointer argument. Size is payload16928bytes from header+8; use_count is sampled and may change between captures.',
        'no_serial_access':True}
(REF/'61-system-extra-commands.json').write_text(json.dumps([query],indent=2)+'\n',encoding='utf-8')
(REF/'round5_system_extra_review.json').write_text(json.dumps(review,indent=2)+'\n',encoding='utf-8')
print(json.dumps(query))
