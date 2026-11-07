"""Validate real captures of IP override and basic-job memory metadata."""
import argparse
import hashlib
import json
import re
from pathlib import Path

REF=Path(__file__).resolve().parent
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('capture',help='Existing physical capture directory relative to references or absolute')
args=parser.parse_args()
cap=Path(args.capture)
if not cap.is_absolute():cap=REF/cap
review_path=REF/'round7_system_review.json'
review=json.loads(review_path.read_text())
session=json.loads((cap/'session.json').read_text())
assert session.get('ended_utc') and session['stop_reason']=='batch-complete'
queries={q['command']:q for q in review['queries']}
rows=session['commands']
assert len(rows)==len(queries) and {x['command'] for x in rows}==set(queries)
responses=[]
revalidations=[]
fields=[]
profile=None
for row in rows:
    q=queries[row['command']]
    path=cap/row['response_file']
    raw=path.read_bytes()
    digest=hashlib.sha256(raw).hexdigest()
    assert row['prompt_returned'] and row['response_sha256']==digest and row['response_bytes']==len(raw)
    text=raw.decode('ascii').replace('\r','').lstrip('\n')
    prefix='-> '+row['command']+'\n'
    assert text.startswith(prefix)
    body=text[len(prefix):]
    end=re.search(r';udws\(\) returns 0 \(0x0\)\n+-> $',body)
    assert end
    body=body[:end.start()].strip('\n')
    if q['name']=='ip.get':
        m=re.fullmatch(r'IP override setting:(\d+) is (-?\d+)\.',body)
        assert m,repr(body)
        assert int(m[1])==q['expected']['field_id']
        value=int(m[2])
        assert -2**31<=value<2**31
        if 'printed_value_at_prerequisite_capture' in q['expected']:
            assert value==q['expected']['printed_value_at_prerequisite_capture'],(row['command'],value,q['expected'])
        result={'field_id':int(m[1]),'field_name':q['expected']['name'],'printed_value':value,
                'matches_prerequisite_snapshot':value==q['expected'].get('printed_value_at_prerequisite_capture') if 'printed_value_at_prerequisite_capture' in q['expected'] else None}
        fields.append(result)
        assessment='validated-ip-override-value'
        note='Exact valid descriptor ID and complete labeled integer payload. This is stored override data; enablement is not established by ip.get.'
        if result['matches_prerequisite_snapshot']:note+=' Value exactly matches independent prerequisite RAM snapshot.'
    else:
        m=re.fullmatch(r'\((\d+)\) (\S+) :  memory : min (\d+), max (\d+), bimodal : 0x([0-9a-fA-F]+)',body)
        assert m,repr(body)
        result={'ordinal':int(m[1]),'profile':m[2],'minimum_bytes':int(m[3]),'maximum_bytes':int(m[4]),'bimodal':int(m[5],16)}
        assert result==q['expected']
        profile=result
        assessment='validated-basic-job-memory-requirement'
        note='Complete one-row resource table matches independently decoded constant descriptor: basic, minimum and maximum1MiB, bimodal0. Declared requirements, not current allocation.'
    relative=path.relative_to(REF).as_posix()
    responses.append({'response_file':relative,'response_sha256':digest,'result':{'valid':True,'errors':[],'fields':result}})
    revalidations.append({'response_file':relative,'response_sha256':digest,'valid':True,'assessment':assessment,'note':note})
assert len(fields)==38 and profile is not None
report={'source':cap.name,'serial_access':'none','review_source':review_path.name,
        'review_sha256':hashlib.sha256(review_path.read_bytes()).hexdigest(),
        'summary':{'new_registered_names':2,'validated_responses':len(responses),'ip_override_fields':len(fields),
                   'all_available_prerequisite_values_matched':all(x['matches_prerequisite_snapshot'] is not False for x in fields)},
        'ip_override_values':fields,'basic_job_memory_requirement':profile,
        'responses':responses,'response_revalidations':revalidations,'data_quality':[]}
(REF/'round7_system_results.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
print(json.dumps(report['summary'],indent=2))
print(json.dumps(profile))
