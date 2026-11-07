"""Prove initialized current-matrix output against captured live index count."""
import json,hashlib
from datetime import datetime,timezone
from scanner_review_round4 import ROOT,DATA,dis
REF=ROOT/'references'
p=REF/'physical-60-ui-metadata/provider-pointers.json'
observations=json.loads(p.read_text())['observations']
count=next(x for x in observations if x['label']=='current_matrix_count')
assert count['words_be']==['0x2']
assert hashlib.sha256((p.parent/count['response_file']).read_bytes()).hexdigest()==count['response_sha256']
inv=json.loads((REF/'static_inventory.json').read_text())
d=next(x for x in inv['commands'] if x['name']=='led_color_cal.get_matrix')
rows=[]
for idx in [0,1]:
    rows.append(dict(name=d['name'],command=f'udws "led_color_cal.get_matrix {idx};"',handler=d['handler'],
      descriptor_address=d['descriptor_address'],classification='reviewed-information-query',argument_syntax=str(idx),timeout=20,
      purpose='Read all nine current LED color-correction coefficients for the selected configured side.',
      control_flow_proof='Physical59 resolves output object2083E248 toA0567ABC, vtableA0567AC0, slot8 targetA024E56E. '
      'Physical60 reads live index-count2083E244 as2. Exact index0/1 passes both target guards, so all18output bytes are initialized. '
      'Target chooses cached normal matrix20AB00B4+18*index or override20AB00FC+18*index based on cached flag20AB00A0[index], '
      'and copies18bytes to caller stack. Handler prints nine signed halfwords. No setter, light, calibration or motor operation.',
      hardware_access=False,persistent_writes=False,side_effects='Local stack output buffer, UART output and parser state only.',
      runtime_registered=True,prior_invocation_status='not-invoked',
      live_count_evidence=dict(count,artifact=str(p.relative_to(ROOT)).replace('\\','/')),
      image_sha256=hashlib.sha256(DATA).hexdigest(),
      reviewed_ranges=[dict(address=hex(a),bytes=n,disassembly=dis(a,n)) for a,n in [(0xa024ea04,0x70),(0xa024e56e,0x34)]],
      interpretation='Current cached coefficients; this query does not run or validate physical calibration. Side labels follow firmware index conventions.'))
report=dict(schema_version=1,generated_utc=datetime.now(timezone.utc).isoformat(),queries=rows,
 summary=dict(ready_invocations=2,unique_names=1,new_unique_names=1),provenance=dict(serial_access_by_reviewer='none'),
 caution='Static dump count was4; live configured count is2. Only live-proven indices0/1 are used. The handler ignores getter failure, so out-of-range arguments could print uninitialized stack values.')
(REF/'round5_current_matrix_review.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
(REF/'62-ui-commands.json').write_text(json.dumps([{k:v for k,v in x.items() if k!='reviewed_ranges'} for x in rows],indent=2)+'\n',encoding='utf-8')
print(json.dumps(report['summary']))
