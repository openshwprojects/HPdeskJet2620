"""Independent offline capture-integrity and coverage audit for rounds53..67."""
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
REF=ROOT/'references'
baseline=json.loads((REF/'round5-baseline.json').read_text())
registry=json.loads((REF/'physical-09-registry/runtime_inventory.json').read_text())
registered={x['name'] for x in registry['entries']}
before=set(baseline['invoked_names'])
assert len(before)==baseline['registered_names_invoked']==644
annotations=defaultdict(list)
for path in REF.glob('*round5*results*.json'):
    report=json.loads(path.read_text())
    for collection in ['response_revalidations','data_quality']:
        for item in report.get(collection,[]):
            relative=item['response_file'].replace('\\','/')
            if relative.startswith('references/'):relative=relative[len('references/'):]
            response_path=REF/relative
            raw=response_path.read_bytes()
            assert hashlib.sha256(raw).hexdigest()==item['response_sha256'],(path,item['response_file'])
            annotations[relative].append({'report':path.name,'collection':collection,'valid':item.get('valid'),'complete':item.get('complete')})


def names_in(command):
    m=re.fullmatch(r'(?:udws|ludw) "(.*)"',command)
    if not m:return []
    out=[]
    for expression in m[1].split(';'):
        expression=expression.strip()
        if not expression:continue
        candidates=[name for name in registered if expression==name or expression.startswith(name+' ')]
        if candidates:out.append(max(candidates,key=len))
    return out


sessions=[]
records=[]
new=defaultdict(list)
flags=[]
capture_paths=[]
for path in REF.glob('physical-*'):
    m=re.match(r'physical-(\d+)',path.name)
    if path.is_dir() and m and 53<=int(m[1])<=67:capture_paths.append(path)
assert {int(re.match(r'physical-(\d+)',p.name)[1]) for p in capture_paths}==set(range(53,68))
for cap in sorted(capture_paths):
    session_path=cap/'session.json'
    session=json.loads(session_path.read_text())
    assert session.get('ended_utc') and session['stop_reason']=='batch-complete',cap
    events=[json.loads(x) for x in (cap/'wire.jsonl').read_text().splitlines() if x]
    wire_rx=bytearray();tx=[]
    for event in events:
        data=bytes.fromhex(event['hex'])
        assert len(data)==event['bytes'],(cap,event)
        if event['direction']=='rx':
            assert event['offset']==len(wire_rx),(cap,event)
            wire_rx.extend(data)
        else:
            assert event['direction']=='tx'
            if data!=b'\r\n':tx.append(data)
    assert bytes(wire_rx)==(cap/'rx.bin').read_bytes(),cap
    expected_tx=[(x['command']+'\r\n').encode('ascii') for x in session['commands']]
    assert tx==expected_tx,(cap,'TX sequence mismatch')
    assert [x['index'] for x in session['commands']]==list(range(1,len(session['commands'])+1))
    last_rx_position=0
    for row in session['commands']:
        path=cap/row['response_file'];raw=path.read_bytes();digest=hashlib.sha256(raw).hexdigest()
        assert digest==row['response_sha256'] and len(raw)==row['response_bytes'],path
        assert row['prompt_returned'] and row['status']=='prompt-returned',path
        text=raw.decode('latin1').replace('\r','')
        assert row['command']+'\n' in text and re.search(r'\n->\s*$',text),path
        if row.get('validation') is not None:assert row['validation']['valid'],path
        position=bytes(wire_rx).find(raw,last_rx_position)
        assert position>=last_rx_position,(path,'response not located in wire RX stream')
        last_rx_position=position+len(raw)
        relative=path.relative_to(REF).as_posix()
        resolved=names_in(row['command'])
        for name in resolved:
            if name not in before:new[name].append(relative)
        annotation=annotations.get(relative,[])
        has_revalidation=any(x['collection']=='response_revalidations' for x in annotation)
        has_inline=row.get('validation') is not None
        payload=text.split(row['command']+'\n',1)[1]
        suspect=[]
        if re.search(r'(?i)\b(?:assert(?:ion)?|error|failed|invalid|not supported|unknown|empty)\b',payload):
            suspect=[x.strip() for x in payload.splitlines() if re.search(r'(?i)\b(?:assert(?:ion)?|error|failed|invalid|not supported|unknown|empty)\b',x)][:8]
        record={'response_file':relative,'response_sha256':digest,'registered_names':resolved,'inline_validation':has_inline,
                'supplemental_revalidation':has_revalidation,'annotations':annotation}
        records.append(record)
        if suspect:flags.append({'response_file':relative,'lines':suspect,'covered_by_inline_or_report':has_revalidation or has_inline})
    sessions.append({'directory':cap.name,'session_sha256':hashlib.sha256(session_path.read_bytes()).hexdigest(),
                     'commands':len(session['commands']),'closed':True,'stop_reason':session['stop_reason'],
                     'all_prompts_returned':True,'response_hashes_and_lengths_valid':True,
                     'wire_rx_exact':True,'wire_tx_exact':True,'responses_in_wire_order':True})

missing=[x for x in records if not x['inline_validation'] and not x['supplemental_revalidation']]
out={'schema_version':1,'generated_utc':datetime.now(timezone.utc).isoformat(),
     'scope':'Independent offline audit of physical53..67; no serial access, no index mutation.',
     'baseline_sha256':hashlib.sha256((REF/'round5-baseline.json').read_bytes()).hexdigest(),
     'summary':{'sessions':len(sessions),'transmissions':len(records),'baseline_transmissions':baseline['transmissions'],
                'total_transmissions_after_round5':baseline['transmissions']+len(records),
                'baseline_registered_names_invoked':len(before),'new_registered_names':len(new),
                'registered_names_invoked_after_round5':len(before|set(new)),
                'registered_names_invoked_non_enumeration_after_round5':baseline['registered_names_invoked_non_enumeration']+len(new),
                'all_sessions_closed':True,'all_prompt_hash_length_and_wire_checks_passed':True,
                'responses_with_inline_validation':sum(x['inline_validation'] for x in records),
                'responses_with_supplemental_revalidation':sum(x['supplemental_revalidation'] for x in records),
                'responses_without_either_validation':len(missing)},
     'sessions':sessions,'new_names':dict(sorted(new.items())),
     'missing_annotations':missing,'suspicious_result_lines':flags,
     'known_semantic_limitations':['Prompt/hash integrity does not establish functional success. Supplemental reports remain authoritative for known guard/empty/error cases.',
                                  'System connection table includes20unknown-provider enums; flagged and covered by system report.',
                                  'Original crypto random-output empty-filename invocation and corrected66 invocation are analyzed separately by product reviewer.',
                                  'Snapshot-copy commands have inline validation; root audits extracted metadata separately.']}
(REF/'round5_audit.json').write_text(json.dumps(out,indent=2)+'\n',encoding='utf-8')
print(json.dumps(out['summary'],indent=2))
print('Missing annotation commands: '+json.dumps([(x['response_file'],x['registered_names']) for x in missing]))
print('Uncovered suspicious results: '+json.dumps([x for x in flags if not x['covered_by_inline_or_report']]))
