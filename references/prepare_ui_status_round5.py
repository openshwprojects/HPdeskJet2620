"""Offline front-panel display-state inspection plan; no serial access."""
import json,hashlib
from datetime import datetime,timezone
from capstone import Cs,CS_ARCH_ARM,CS_MODE_ARM,CS_MODE_BIG_ENDIAN
from scanner_review_round4 import ROOT,DATA,dis
REF=ROOT/'references'
inv=json.loads((REF/'static_inventory.json').read_text())
d=next(x for x in inv['commands'] if x['name']=='ui_lite.display_status')
itcm=(ROOT/'artifacts/ghs/itcm.bin').read_bytes()
md=Cs(CS_ARCH_ARM,CS_MODE_ARM|CS_MODE_BIG_ENDIAN)
arm='\n'.join(f'{x.address:#x}: {x.mnemonic} {x.op_str}' for x in md.disasm(itcm[0x6bc:0x78c],0x6bc))
r=dict(name=d['name'],command='udws "ui_lite.display_status;"',handler=d['handler'],
 descriptor_address=d['descriptor_address'],classification='reviewed-information-query',argument_syntax='',timeout=20,
 purpose='Read the cached front-panel status, error selections, digit values, wireless/button/LED flags.',
 control_flow_proof='A044DDDA copies48bytes of status20DEFE04 throughA0453960, and optionally formats up to16active error slots. '
 'A04539C6 tests a bit in the cached word20DEFE2C. A0450338 reads cached displaydigits20DEF73C/740. '
 'A044FE6A copies12bytes20DEF044. TailA044E08C copies28bytes20DEFDE8 viaA0453326 and prints byteflags. '
 'All three copy helpers tail throughA03E47C4, an ARM veneer to ITCM000006BC, the reviewed bounded memmove. '
 'No UI refresh/setter, GPIO, illumination or state-machine dispatch is called. All loop bounds and stack destinations are fixed.',
 side_effects='UART output, local stack buffers and parser state only.',hardware_access=False,persistent_writes=False,
 interpretation='Cached UI state; no physical panel changes or refresh request. Concurrent firmware tasks may change fields during the query.',
 image_sha256=hashlib.sha256(DATA).hexdigest(),itcm_sha256=hashlib.sha256(itcm).hexdigest(),
 reviewed_ranges=[dict(address=hex(a),bytes=n,disassembly=dis(a,n)) for a,n in [(0xa044ddda,0xa2),(0xa044e08c,0x52),
 (0xa0453960,12),(0xa04539c6,16),(0xa0450338,22),(0xa044fe6a,12),(0xa0453326,12)]],
 itcm_memmove_disassembly=arm,runtime_registered=True,prior_invocation_status='not-invoked')
report=dict(schema_version=1,generated_utc=datetime.now(timezone.utc).isoformat(),queries=[r],
 summary=dict(ready_invocations=1,unique_names=1,new_unique_names=1),provenance=dict(serial_access_by_reviewer='none'))
(REF/'round5_ui_status_review.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
(REF/'56c-ui-status-commands.json').write_text(json.dumps([{k:v for k,v in r.items() if k not in ['reviewed_ranges','itcm_memmove_disassembly']}],indent=2)+'\n',encoding='utf-8')
print(json.dumps(report['summary']))
