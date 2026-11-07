"""Offline LED metadata query plan using physical provider-pointer evidence."""
import json,hashlib
from datetime import datetime,timezone
from scanner_review_round4 import ROOT,DATA,dis,u32
REF=ROOT/'references'
source=REF/'physical-59-metadata-pointers/provider-pointers.json'
p=json.loads(source.read_text())
by={x['label']:x for x in p['observations']}
for obs in p['observations']:
    raw=(source.parent/obs['response_file']).read_bytes()
    assert hashlib.sha256(raw).hexdigest()==obs['response_sha256']
assert by['led_interface_3']['words_be']==['0xa05c35a8']
assert by['led_output']['words_be']==['0xa0567abc']
assert by['led_interface_4']['words_be']==['0xa06053c0']
inv=json.loads((REF/'static_inventory.json').read_text())
desc={x['name']:x for x in inv['commands']}
cov={x['name']:x for x in json.loads((REF/'coverage.json').read_text())['commands']}
rows=[]
def add(name,args,proof,ranges,**extra):
    d=desc[name]
    row=dict(name=name,command=f'udws "{name} {args};"',handler=d['handler'],
      descriptor_address=d['descriptor_address'],classification='reviewed-information-query',argument_syntax=args,
      purpose='Read LED color calibration metadata without enabling illumination or performing calibration.',
      control_flow_proof=proof,hardware_access=False,persistent_writes=False,timeout=20,
      side_effects='Local output buffers, UART output and parser state only.',
      prior_invocation_status=cov[name]['status'],runtime_registered=True,
      image_sha256=hashlib.sha256(DATA).hexdigest(),
      provider_evidence=str(source.relative_to(ROOT)).replace('\\','/'),provider_evidence_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
      reviewed_ranges=[dict(address=hex(a),bytes=n,disassembly=dis(a,n)) for a,n in ranges])
    row.update(extra);rows.append(row)
for idx in [0,1]:
    a=0xa05c439c if idx==0 else 0xa05c43f4
    expected=[int.from_bytes(DATA[(a&0x3ffffff)+i*2:(a&0x3ffffff)+i*2+2],'big',signed=True) for i in range(9)]
    add('led_color_cal.get_default_matrix',str(idx),
        'Physical provider20AB0154 isA05C35A8 with vtableA05C35AC. Its slot0x10 isA02CDDBC, forwarding toA02CD9E6. '
        'For index0/1 this returns fixed ROM addressA05C439C/A05C43F4, with nine valid signed halfwords. '
        'HandlerA024EA74 prints exactly three rows of three; only parser/formatting and pure table-selection helpers execute.',
        [(0xa024ea74,0x58),(0xa024eed8,0x10),(0xa02cddbc,10),(0xa02cd9e6,0x10)],
        expected_values=expected,interpretation='Compiled default matrix, not measured or current calibration.')
    a=0xa05c4390 if idx==0 else 0xa05c43e8
    expected=[int.from_bytes(DATA[(a&0x3ffffff)+i*2:(a&0x3ffffff)+i*2+2],'big',signed=True) for i in range(6)]
    add('led_color_cal.get_golden_peaks_widths',str(idx),
        'Same physically resolved provider slot0x0C isA02CDDB2, forwarding toA02CD9D6. '
        'Exact index0/1 returns constantA05C4390/A05C43E8; handler prints exactly six signed halfwords as RGB peak/width pairs. '
        'No calibration, lighting, scanner transaction, or storage write occurs.',
        [(0xa024eb50,0x4a),(0xa024eee8,0x10),(0xa02cddb2,10),(0xa02cd9d6,0x10)],
        expected_values=expected,interpretation='Compiled golden reference values. Index1 table is all zero; this is not a physical light measurement.')
add('led_color_cal.get_barcode','0',
    'Physical scanner providerA06053C0 vtableA06054B4 slot0x80 resolvesA03BF716. Exact index0 andsize11 pass its guards. '
    'It callsA03A3714, a10-byte storage record read DSID0x12E85,index0 via storage slot0x20, then forces output[10]=NUL. '
    'The CLI initializes all11bytes tozero before the read. DecoderA024E026 requiresstrlen10 and performs exactlythree bounded groups '
    'throughA024DFBE, writing only its caller-owned output arrays. The result is cached barcode plus decoded RGB metadata or a validation error.',
    [(0xa024eacc,0x84),(0xa03bf716,0x1e),(0xa03a3714,0x1c),(0xa024e026,0x48),(0xa024dfbe,0x4e)],
    interpretation='Stored scanner LED barcode. Validation failure means no valid ten-character barcode decoded; no calibration is performed.')
out=dict(schema_version=1,generated_utc=datetime.now(timezone.utc).isoformat(),queries=rows,
 summary=dict(ready_invocations=len(rows),unique_names=len({x['name'] for x in rows}),new_unique_names=len({x['name'] for x in rows if x['prior_invocation_status']=='not-invoked'})),
 provenance=dict(serial_access_by_reviewer='none',provider_evidence=str(source.relative_to(ROOT)).replace('\\','/')),
 held_paths=[dict(name='led_color_cal.get_matrix',reason='Await live2083E244 count to ensure getter fills every stack byte; handler ignores failure.')])
(REF/'round5_led_review.json').write_text(json.dumps(out,indent=2)+'\n',encoding='utf-8')
(REF/'56b-led-commands.json').write_text(json.dumps([{k:v for k,v in x.items() if k!='reviewed_ranges'} for x in rows],indent=2)+'\n',encoding='utf-8')
print(json.dumps(out['summary']))
