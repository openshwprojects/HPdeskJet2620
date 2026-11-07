"""Validate root-directory block diagnostic against earlier captured header."""
import json
import re
from pathlib import Path


def validate(item,text):
    text=text.replace('\r','')
    if item['command']+'\n' not in text or not re.search(r';udws\(\) returns 0 \(0x0\)\s*->\s*$',text):
        return {'valid':False,'errors':['Incomplete echo, wrapper or prompt']}
    payload=text.split(item['command']+'\n',1)[1].split(';udws()',1)[0].strip()
    m=re.fullmatch(r'(DIR|FILE) inode (\d+) : block # = (\d+), priority = (\d+), size = (\d+), use_count = (\d+), (Non Purgeable|Purgeable)',payload)
    if not m:return {'valid':False,'errors':['Missing or malformed successful block-info row']}
    got={'kind':m[1],'inode':int(m[2]),'block':int(m[3]),'priority':int(m[4]),'size':int(m[5]),'use_count':int(m[6]),'purgeable':m[7]=='Purgeable'}
    expected=item['expected']
    mismatch=[k for k in ['kind','inode','block','priority','size','purgeable'] if got[k]!=expected[k]]
    return {'valid':not mismatch,'errors':mismatch,'fields':got,'header_fields_match':not mismatch,
            'use_count_matches_prior_snapshot':got['use_count']==expected['use_count_at_header_capture']}


if __name__=='__main__':
    import hashlib
    root=Path(__file__).resolve().parents[1];ref=root/'references'
    cap=ref/'physical-61-block-metadata'
    session=json.loads((cap/'session.json').read_text())
    rows=[]
    for item in session['commands']:
        path=cap/item['response_file'];raw=path.read_bytes();sha=hashlib.sha256(raw).hexdigest()
        assert sha==item['response_sha256'] and len(raw)==item['response_bytes'] and item['prompt_returned']
        result=validate(item,raw.decode('ascii'));assert result['valid'],result
        rows.append({'response_file':path.relative_to(ref).as_posix(),'response_sha256':sha,'result':result})
    out={'source':'physical-61-block-metadata','responses':rows,
         'response_revalidations':[dict(response_file=x['response_file'],response_sha256=x['response_sha256'],valid=True,
            assessment='validated-live-directory-block-metadata',note='Actual DIR inode1 block0 query; identity,size,priority,purgeable fields exactly match independently captured live32-byte header. Reference count is separately compared as a time-dependent field.') for x in rows],
         'data_quality':[],'serial_access':'none'}
    (ref/'round5_system_extra_results.json').write_text(json.dumps(out,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(rows))
