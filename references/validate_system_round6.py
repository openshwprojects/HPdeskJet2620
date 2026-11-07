"""Validate actual captured allocator block metadata, offline only."""
import argparse
import hashlib
import json
import re
from pathlib import Path

REF=Path(__file__).resolve().parent
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('capture',help='Existing physical capture directory, relative to references or absolute')
args=parser.parse_args()
cap=Path(args.capture)
if not cap.is_absolute():cap=REF/cap
review=json.loads((REF/'round6_system_review.json').read_text())
query=review['queries'][0]
session=json.loads((cap/'session.json').read_text())
assert session['stop_reason']=='batch-complete' and session.get('ended_utc')
rows=[x for x in session['commands'] if x['command']==query['command']]
assert len(rows)==1
row=rows[0]
path=cap/row['response_file']
raw=path.read_bytes()
digest=hashlib.sha256(raw).hexdigest()
assert row['prompt_returned'] and row['response_sha256']==digest and row['response_bytes']==len(raw)
text=raw.decode('ascii').replace('\r','').lstrip('\n')
prefix='-> '+query['command']+'\n'
assert text.startswith(prefix)
body=text[len(prefix):]
end=re.search(r';udws\(\) returns 0 \(0x0\)\n+-> $',body)
assert end
body=body[:end.start()].strip('\n')
m=re.fullmatch(r'(0x[0-9a-fA-F]{8}) (0x[0-9a-fA-F]{8}) (0x[0-9a-fA-F]{8})  (NOT PURGEABLE|purgeable) +(L)? +priority (\d+): *(\d+)  pin=(\d+)  size = (\d+)',body)
assert m,repr(body)
actual={'key_words':[m[1].lower(),m[2].lower(),m[3].lower()],
        'purgeable':m[4]=='purgeable','locked':bool(m[5]),
        'priority_major_raw':int(m[6]),'priority_minor_raw':int(m[7]),
        'pin_count':int(m[8]),'size_bytes':int(m[9])}
expected=query['expected']
for field in ['key_words','purgeable','locked','priority_major_raw','priority_minor_raw','size_bytes']:
    assert actual[field]==expected[field],(field,actual[field],expected[field])
assert 0<=actual['pin_count']<=255
relative=path.relative_to(REF).as_posix()
report={'source':cap.name,'serial_access':'none',
        'review_source':'round6_system_review.json',
        'review_sha256':hashlib.sha256((REF/'round6_system_review.json').read_bytes()).hexdigest(),
        'responses':[{'response_file':relative,'response_sha256':digest,
                      'result':{'valid':True,'errors':[],'fields':actual,'header_fixed_fields_match':True,
                                'pin_count_matches_prior_snapshot':actual['pin_count']==expected['pin_count_at_header_capture']}}],
        'response_revalidations':[{'response_file':relative,'response_sha256':digest,'valid':True,
                                  'assessment':'validated-live-allocator-block-metadata',
                                  'note':'Real root-directory block key. Identity, purgeability, lock, both priority bytes and size match independently captured live header; pin count is a time-dependent field. Complete single-line payload and normal return verified.'}],
        'data_quality':[]}
(REF/'round6_system_results.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
print(json.dumps(actual,indent=2))
