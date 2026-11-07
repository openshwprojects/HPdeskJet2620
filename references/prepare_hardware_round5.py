"""Offline bounded calibration and pen query review. Never opens a serial port."""
import json, hashlib
from datetime import datetime, timezone
from scanner_review_round4 import ROOT, DATA, dis, u32, string
REF = ROOT/'references'
inv=json.loads((REF/'static_inventory.json').read_text())
desc={x['name']:x for x in inv['commands']+inv['hierarchical_commands_with_static_registration']}
cov={x['name']:x for x in json.loads((REF/'coverage.json').read_text())['commands']}
entries=[]
def add(name,args,purpose,proof,ranges,**extra):
    d=desc[name]
    row=dict(name=name,command='udws "'+name+(' '+args if args else '')+';"',handler=d['handler'],
      descriptor_address=d['descriptor_address'],classification='reviewed-information-query',
      argument_syntax=args,purpose=purpose,control_flow_proof=proof,
      hardware_access=False,persistent_writes=False,timeout=20,
      side_effects='UART output, parser state and bounded local stack writes only.',
      runtime_registered=True,prior_invocation_status=cov[name]['status'],
      image_sha256=hashlib.sha256(DATA).hexdigest(),review_scope='Exact arguments only; offline review.',
      reviewed_ranges=[dict(address=hex(a),bytes=n,disassembly=dis(a,n)) for a,n in ranges])
    row.update(extra);entries.append(row)

pa_proof=('The five arguments are calibration enum, pass enum, pen enum, integer speed/selector, direction enum. '
 'A0027308 reads the enum dictionaries. A0025EE4 searches firmware-owned tables, stopping at initialized counts or -1 sentinel, '
 'and returns a bounded index or -1. Getter helpers guard negative results then read one cached value/status field. '
 'No setter, commit, scan, motor, or pen transaction helper is called. Configuration counts at A0568368..7C are 1,1,6,2,8,1; '
 'initialization allocates fixed RAM tables and copies those descriptors. Chosen first-pass combinations match real ROM descriptors, '
 'including P2PX/P2PY CMY and BIDI/THETAZ K. The speed argument zero is below the first BIDI/THETAZ threshold.')
for name,helper in [('cal_val.get_pa_value',0xa00261a0),('cal_val.get_pa_value_status',0xa002620a),
                    ('cal_val.get_unused_pa_value',0xa0026b4c),('cal_val.get_unused_pa_status',0xa0026bb2)]:
    for args in ['P2PX FIRST CMY 0 FWD','P2PY FIRST CMY 0 FWD','BIDI FIRST K 0 FWD','THETAZ FIRST K 0 FWD']:
        add(name,args,'Read active/unused alignment calibration value or validity status for a real configured descriptor.',pa_proof,
            [(int(desc[name]['handler'],16)&~1,0x64),(helper,0x78),(0xa0025ee4,0x1a8),(0xa0027308,0x22)],
            response_limit_note='A zero is a cached value or status, not evidence of successful physical calibration.')
for name,helper in [('cal_val.get_linefeed_err',0xa00262c2),('cal_val.get_linefeed_err_status',0xa00262de)]:
    add(name,'1 1 K','Read linefeed calibration error or validity status for the actual configured descriptor.',
        'Parses two integers and K enum. A002608C searches count-bounded RAM descriptors initialized from A056909C; '
        'its only descriptor starts with (1,1,0), matching these arguments. A00262C2/62DE guard negative index then '
        'read a signed halfword at +6 or status word at +40 in the selected 44-byte record. No commit or hardware call.',
        [(int(desc[name]['handler'],16)&~1,0x4c),(helper,0x1c),(0xa002608c,0x40),(0xa0027308,0x22)])
for name,helper in [('cal_val.get_edge_err',0xa00263a0),('cal_val.get_edge_err_status',0xa00263c8)]:
    add(name,'1','Read the configured edge calibration error or status.',
        'Parses ID1. Helpers search 24-byte records at2086FAA8 with count2086DAB4, read offset18 or20, and return0 if not found. '
        'A0025AE0 initializes the count from constant1 atA0568380 and descriptor ID1 atA05683A4. No callbacks except parser/output.',
        [(int(desc[name]['handler'],16)&~1,0x32),(helper,0x28),(0xa0025ae0,0x44),(0xa0250ade,6)])

for name,helper,base in [('pen.get_ttoe_ad_range',0xa031bb70,0x20cff3b0),('pen.get_ttoe_test_range',0xa031bb78,0x20cff330)]:
    add(name,'','Read all 63 cached pen calibration range entries.',
        f'Handler outputs count63 then loops exactly indices0..62. Helper{helper:#x} reads one unsigned halfword from fixed{base:#x}+2*index and returns. '
        'No hardware access or pen transaction lock is called. Output is count plus63 semicolon-delimited values.',
        [(int(desc[name]['handler'],16)&~1,0x62),(helper,8)],expected_value_count=64)
for name,selector in [('pen.is_ttoed',26),('pen.is_ldw_ttoed',29)]:
    for chute in [0,1]:
        add(name,str(chute),'Read cached pen calibration state for cartridge chute '+str(chute)+'.',
            f'Handler callsA0318210 with fixedmode selecting field{selector}. A032053A reads chute type viaA03159F8/A03204AC and guards type<4. '
            'Exact chute0/1 bounds the type lookup; A0320558 bounds its table implicitly by type0..3, channel0 and fixedfield. '
            'Nonzero descriptor is resolved byA03211B4 through a current-task volatile cache or storage read interface slot8. '
            'No transaction lock/unlock or setters are called; these are storage/cache state queries, not live pen health measurements.',
            [(int(desc[name]['handler'],16)&~1,0x3e),(0xa0318210,0x28),(0xa032053a,0x3a),
             (0xa03159f8,10),(0xa03204ac,0x16),(0xa03211b4,0x3c),(0xa0314546,0x22)])
for typ,mode in [(0,0),(1,0),(0,1),(1,1)]:
    add('pen.get_dotcount',f'{typ} 0 {mode}','Read a cached dot counter for pen type '+str(typ)+' and channel '+str(mode)+'.',
        'A032AAC6 initializes a local type object, reads three integers, bounds selector0..10, maps selector0 tofield17 viaA05DF6CC, '
        'then reads A05D730C[((type*2+channel)*53+17)] throughA0320558/A03211B4. Exact type0/1 andchannel0/1 keep all accesses bounded. '
        'A03211B4 reads volatile current-task cache or storage interface slot8; no setter or transaction lock.',
        [(0xa032aac6,0x70),(0xa0320558,0x1c),(0xa03211b4,0x3c),(0xa03299bc,0x34)],
        response_limit_note='First argument is the pen-type table index, not necessarily a physical chute number.')
add('pen.get_drfs','','Read the stored DRFS value.',
    'HandlerA032B26E calls storage interface20CFD320 slot0x14 with constantDSID0x13CAF,index0, then prints the scalar through UART interface. '
    'This is the same indexed storage-read contract used by the previous validated physical storage dumps; no storage write slot is used.',
    [(0xa032b26e,0x22)])

holds=[dict(name='cal_val.get_sensor_err / get_sensor_err_status',reason='Compiled sensor calibration record count A0568384 is zero. These would only exercise empty-table fallback; skipped as low information.'),
 dict(name='pen.get_eureka_dot_count_status / get_trigger* / ambient and voltage measurements',reason='Calls pen transaction lock/unlock, whose unlock can flush queued storage/hardware work. Requires separate side-effect review.'),
 dict(name='led_color_cal.get_matrix/default_matrix/golden_peaks_widths/barcode',reason='Await exact runtime interface pointers to prove virtual getter target and returned pointer initialization.'),
 dict(name='smgr_pe.get_current_setting/get_default_setting/cover_open_query/user_power_on_query',reason='Await interface-target review; no calls claimed here.')]

report=dict(schema_version=1,generated_utc=datetime.now(timezone.utc).isoformat(),
 provenance=dict(serial_access='none',image='artifacts/ghs/dram.bin',image_sha256=hashlib.sha256(DATA).hexdigest()),
 summary=dict(ready_invocations=len(entries),unique_names=len({x['name'] for x in entries}),
              new_unique_names=len({x['name'] for x in entries if x['prior_invocation_status']=='not-invoked'})),
 queries=entries,held_paths=holds,
 notes=['Pure cached queries cannot establish that a cartridge or scanner is physically healthy.',
        'No scan, motor, pen firing, power transition, persistent write, or firmware reset is requested.'])
(REF/'round5_hardware_review.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
(REF/'56-hardware-commands.json').write_text(json.dumps([{k:v for k,v in x.items() if k!='reviewed_ranges'} for x in entries],indent=2)+'\n',encoding='utf-8')
print(json.dumps(report['summary']))
