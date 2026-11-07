"""Prepare cached front-panel queries using live interface and bounds evidence."""
import json,hashlib
from datetime import datetime,timezone
from scanner_review_round4 import ROOT,DATA,dis,u32
REF=ROOT/'references'
evidence=[]
for folder in ['physical-60-ui-metadata','physical-64-ui-fields']:
    p=REF/folder/'provider-pointers.json'
    obs=json.loads(p.read_text())['observations']
    for x in obs:
        assert hashlib.sha256((p.parent/x['response_file']).read_bytes()).hexdigest()==x['response_sha256']
        evidence.append(dict(x,artifact=p.relative_to(ROOT).as_posix()))
by={x['label']:x for x in evidence}
assert by['settings_provider_header']['words_be']==['0xa06073ec','0x2084a0c0','0x33']
assert by['cover_provider_method']['words_be']==['0xa0304335']
assert by['cover_cached_descriptor']['words_be']==['0x0','0x2','0x1','0x0','0x1']
assert by['ui_interface_3']['words_be']==['0xa0517544']
inv=json.loads((REF/'static_inventory.json').read_text())
desc={x['name']:x for x in inv['commands']}
rows=[]
def add(name,args,purpose,proof,ranges,**extra):
    d=desc[name]
    row=dict(name=name,command='udws "'+name+(' '+args if args else '')+';"',handler=d['handler'],
        descriptor_address=d['descriptor_address'],classification='reviewed-information-query',argument_syntax=args,
        purpose=purpose,control_flow_proof=proof,timeout=20,hardware_access=False,persistent_writes=False,
        side_effects='Local formatting buffers, UART output and parser state only.',
        prior_invocation_status='not-invoked',runtime_registered=True,image_sha256=hashlib.sha256(DATA).hexdigest(),
        reviewed_ranges=[dict(address=hex(a),bytes=n,disassembly=dis(a,n)) for a,n in ranges])
    row.update(extra);rows.append(row)
for idx in [1,3,4]:
    add('smgr_pe.get_current_setting',str(idx),'Read cached current setting for '+{1:'ColorLok',3:'dry time',4:'eject behavior'}[idx]+'.',
        'Physical60 identifies settings object2084A0B4 and vtableA06073EC. Physical64 proves header(table2084A0C0,count51). '
        'Exact indices1,3,4 are bounded. Slot0 targetA03CE8C8 computes record12*index and reads only current field+4; no setter or storage callback. '
        'ROM record DSIDs at+0 map these positions to0x12BE9 COLORLOK,0x12C28 DRY_TIME_SETTING and0x12C29 EJECT_SETTING. '
        'CLI formats a single scalar. It has a three-byte output buffer; any unexpectedly multi-digit result requires truncation assessment.',
        [(0xa03db900,0x3a),(0xa03ce8c8,12)],
        setting_index=idx,static_descriptor_dsid=u32(0x2084a0c0+idx*12),
        live_bounds_evidence=by['settings_provider_header'])
cover=('Physical64 proves cover descriptor20848C74=[0,2,1,0,1]. A03060F0 findsID0 in its five-entry table. '
       'A0306454 checks descriptor+8==1 then+4==1; actual+4==2 skipsGPIO helperA02555E4 and its cache refresh entirely. '
       'The remaining path reads the override flag20CE33B4[0] and either cached override20CE33BC[0] or cached state20CE33E4[0]. '
       'No hardware read/write or event dispatch on this exact observed descriptor path.')
add('pe.cover_open_query','','Read the cached raw cover state.',cover,
    [(0xa03059f8,0x2e),(0xa0306454,0x3c),(0xa03060f0,0x20)],live_cover_evidence=by['cover_cached_descriptor'])
add('smgr_pe.cover_open_query','','Read the state-manager cover-open Boolean.',
    'Physical60/64 resolve cover provider vtable20848770+0x124 toA0304334. It callsA0306454 forID0 and mapsrawzero/nonzero to1/2. '
    'CLI maps returned2 toBoolean1, else0. '+cover,
    [(0xa03dba4a,0x3e),(0xa0304334,0x18),(0xa0306454,0x3c),(0xa03060f0,0x20)],
    live_method_evidence=by['cover_provider_method'],live_cover_evidence=by['cover_cached_descriptor'])
add('smgr_pe.user_power_on_query','','Read the stored power-state classification exposed by the state manager.',
    'Physical60 resolves20DAF888 to storage objectA0517544, vtableA0517548. Slot8 is previously reviewed scalar getterA005C38C. '
    'Handler passes fixedDSID0x10005 (power state) and mapsvalues0,1,2 toBoolean0, other values to1. No power transition or state write.',
    [(0xa03dba88,0x3c),(0xa005c38c,0x54)],live_provider_evidence=by['ui_interface_3'],
    interpretation='Firmware Boolean derived from stored power state, not a direct voltage or power-button measurement.')
report=dict(schema_version=1,generated_utc=datetime.now(timezone.utc).isoformat(),queries=rows,
 summary=dict(ready_invocations=len(rows),unique_names=len({x['name'] for x in rows}),new_unique_names=len({x['name'] for x in rows})),
 provenance=dict(serial_access_by_reviewer='none'),
 held_paths=[dict(name='smgr_pe.get_default_setting',reason='Requires further validation of separate storage provider20DA30DC; no invocation claimed.')],
 notes=['pe.cover_open_query and smgr_pe.cover_open_query use distinct handlers and return raw state versus wrapper Boolean; uifp aliases deliberately omitted.',
        'Settings IDs are indices, not DSIDs. Only live-bound indices1,3,4 were selected.',
        'No cover disable/enable, power_off, reset, simulated button press or setting setter is called.'])
(REF/'round5_frontpanel_review.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
(REF/'65-ui-commands.json').write_text(json.dumps([{k:v for k,v in x.items() if k!='reviewed_ranges'} for x in rows],indent=2)+'\n',encoding='utf-8')
print(json.dumps(report['summary']))
