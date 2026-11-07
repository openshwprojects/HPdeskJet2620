"""Build an explicitly reviewed scanner capture plan; no serial access."""
import json, hashlib
from datetime import datetime, timezone
from scanner_review_round4 import ROOT, DATA, dis, u32

REF=ROOT/'references'
inv=json.loads((REF/'static_inventory.json').read_text())
descs={x['name']:x for x in inv['commands']+inv['hierarchical_commands_with_static_registration']}
coverage={x['name']:x for x in json.loads((REF/'coverage.json').read_text())['commands']}
digest=hashlib.sha256(DATA).hexdigest()
entries=[]
def add(name,args,purpose,proof,ranges,classification='reviewed-information-query',extra=None):
    d=descs[name]
    item=dict(name=name,command='udws "'+name+(' '+args if args else '')+';"',
              handler=d['handler'],descriptor_address=d['descriptor_address'],
              classification=classification,argument_syntax=args,purpose=purpose,
              control_flow_proof=proof,hardware_access=False,timeout=25,
              side_effects='UART output and parser state. The scanner formatter can update its volatile output-throttle counter.',
              runtime_registered=True,prior_invocation_status=coverage[name]['status'],
              image_sha256=digest,review_scope='Exact invocation only; offline binary review, no serial access.',
              reviewed_ranges=[{'address':hex(a),'bytes':n,'disassembly':dis(a,n)} for a,n in ranges])
    if extra:item.update(extra)
    entries.append(item)

add('scan sensor CCD','0','Refresh sensor identity before table-dependent queries.',
    'A03BDC4C ignores parsed byte and reads sensor ID2085841C and supplier2085843C, mapping them to constant strings. Previous physical capture identifies TEMPO_TOS (ID2).',
    [(0xa03bdc4c,0x26),(0xa03bdbd0,0x5a)],extra={'required_observation_for_later_resolution_list':'SCANNER SENSOR: (CIS_MODULE_TEMPO_TOS)'})
add('scan led LEDONTIMEGET','','Read cached RGB LED on-times without enabling LEDs.',
    'A03A9CEE reads three halfwords20D9DF84,86,88 and formats. No other call except scanner output.',
    [(0xa03a9cee,0x12)])
add('scan bolt yy','','Read cached scanner ASIC/controller mode and revision.',
    'A03A2EA4 reads byte20D9CBDC and halfword20D9CBD8; two output calls only.',
    [(0xa03a2ea4,0x16)])
add('scan keep yy','0','Print cached scanner KEEP configuration.',
    'A03A7E3C parses signed byte then helperA03A7CA2 ignores value and reads fixed20D9DE28..38 fields, formatting only. No setter or device access.',
    [(0xa03a7e3c,0xc),(0xa03a7ca2,0x6a)])
add('scan ifc yreslist','','Print color and mono Y-resolution lists labelled with and without ADF; listed ADF modes do not prove an ADF is installed.',
    'HandlerA03BFD1A/A03BFF5C callsA03BD61C four times with(xres300,selector0/1,mode0/1). Helper scans max12entries for sentinel-1, filters at most13halfwords into20DA08E4 and always appends0. A03BD564 reads fixedsensor table, A03BD470 maps300toindex0. TEMPO_TOS(ID2) chooses entries72/84 of20857D38, both statically nonnull with valid-1sentinel. No callbacks, hardware access or persistent writes.',
    [(0xa03bfd1a,0x50),(0xa03bff5c,0x5e),(0xa03bd61c,0x70),(0xa03bd564,0x38),(0xa03bd470,0x40)],
    extra={'side_effects':'Writes bounded28-byte volatile resolution-list scratch at20DA08E4; UART output and parser state. No persistent setting changes.',
           'prerequisite':'Earlier CCD query in this plan must confirm TEMPO_TOS; otherwise stop before this invocation.',
           'static_sensor_table_evidence':[{'table_index':i,'pointer':hex(u32(0x20857d38+i*4)),
           'halfwords_at_offset32':[int.from_bytes(DATA[(u32(0x20857d38+i*4)&0x3ffffff)+32+j*2:(u32(0x20857d38+i*4)&0x3ffffff)+34+j*2],'big',signed=True) for j in range(14)]} for i in [72,84]]})
for res in [300,600,1200,2400,4800,9600]:
    add('scan led LEDONTIMEINFO',str(res),'Read cached LED calibration/timing information at '+str(res)+'DPI.',
        'A03A9A9C parsesunsigned16resolution; pureA03A8230 maps chosen arguments toindex0..5. Top-level20D9DF38[index] is null-checked before dereferencing its configuration. All non-output helper calls after nullguard areA03A247E(constant1) and integer division with fixed511denominator. Reads selected configuration and cached current/on-time fields; no hardware/setter/allocation calls. Nullslots print only trailing newline.',
        [(0xa03a9a9c,0x94),(0xa03a9c18,0xd6),(0xa03a8230,0x4c),(0xa03a247e,4)],
        extra={'response_limit_note':'Null resolution slots are expected to have empty payload; that establishes no calibration record at this resolution, not a calibrated zero value.'})
add('scan sensor timing','0','Capture built-in timing command usage, exposing future supported selectors and argument syntax.',
    'Selector 0 at A03BDE3A bypasses selector 1 writes, 10..12/20 table paths, 30 setter and 99 debug setter. Executes six constant format calls, a seventh at A03BE234, then the common A03BEC98 epilogue. This exact help path does not read current selection, alter timing, or touch hardware.',
    [(0xa03bde3a,0x64),(0xa03be234,8),(0xa03bec98,4)],classification='reviewed-help')

holds=[
 {'name':'scan ifc maxyres','reason':'A03BFD0E unconditionallywrites20DA0AB8 resolutionoverride; not a getter.'},
 {'name':'scan sensor id','reason':'A03BDE14 with0..2writes sensorID and reconfigures;255would onlyguard and adds no usefulinformation.'},
 {'name':'scan reg info','reason':'A03B9D4A parsespairs and callsA03B80F4 configuration-write path fornonzeroargs.'},
 {'name':'scan sensor timing','invocation_scope':'selectors other than0','reason':'Selector1 writes selectionoverride;20maycallA03BD704 setter;30writes multiple timingtablefields;99writesdebugmode. Onlyselector0isapprovedhere.'},
 {'name':'scan sensor ylist','reason':'A03BED94 invokes initializedresolutioncallback but then loads samehalfword atmost14times and prints nothing. Skip lowinformation duplicatepath.'},
 {'name':'scan gain histstatus','reason':'Generic history constructorA03A1718 bindsstatusslot0toBXLR A03A17A6. Potentiallyonlystuboutput; initializationchainnotfullyreconstructed.'},
 {'name':'scan timing status','reason':'A03BEE9C initializes20DA0910 throughA03A1718; slot0bindsBXLR A03A17A6. No usefulhistorypayload.'},
 {'name':'scan illum hist status','reason':'Indirecthistoryobjectstatus islikelysame genericstub; no usefuloutput anticipated, initializationchainnotfullyreconstructed.'},
 {'name':'scan mem show','reason':'A03AB2EE callsA03AB27A tofill fourlocalstackwords, runs20iterationemptyloop, thenreturnswithoutprinting. Does not expose actualmemoryusage.'},
 {'name':'scan orig locget','reason':'OnefallbackentersmotorpositionvirtualcallbackA03AD8A8; callbackimplementationnotresolvedinthisbatch.'},
 {'name':'scan mtr getpos','reason':'A03ADF36 alwaysentersmotorpositionvirtualcallbackA03AD8A8; targetnotresolvedinthisbatch.'},
 {'name':'scan cissm prnutblprt','reason':'A03A6BE0 writesdebugflag20D9DC79; not tabledump.'},
 {'name':'scan reg gget','reason':'A03B6A88 usescachedgain asunboundedindexintoromA06011C4 forconstantASICtype1; gainboundnotestablished.'},
 {'name':'scan prnu yy / scan sensor gaintarg / scan sensor whitetarg / scan sensor blacktarg / scan sensor SCANRES','reason':'Read-onlyvariantsidentified, but dependoncurrentselectionindices20858418..28 throughA03BD796. Currentselectioninitializationnotestablishedfromphysicalcapture; deferredratherthanassumingready.'},
 {'name':'scan isr parms / scan firms print','reason':'GetterA03A4314 uses scanner firmware-valuevirtualinterface20DA0AFC; futureboundedreviewcanresolveexactvtable. Existing577firmvalueartifactalreadycoversnumerics.'},
]
for h in holds:
    h.update(decision='defer-or-skip-this-batch',not_a_blanket_command_ban=True)
report=dict(schema_version=1,generated_utc=datetime.now(timezone.utc).isoformat(),
    provenance={'kind':'offline_review_for_physical_capture','serial_access':'none','image':'artifacts/ghs/dram.bin','image_sha256':digest,
    'runtime_registry':'references/physical-09-registry/runtime_inventory.json','coverage':'references/coverage.json'},
    summary={'ready_invocations':len(entries),'unique_names':len({x['name'] for x in entries}),
             'new_unique_names':len({x['name'] for x in entries if x['prior_invocation_status']=='not-invoked'}),
             'claims':'Proposed exact invocations only. No physical outputs are claimed by this offline report.'},
    queries=entries,held_paths=holds,
    common_notes=['No command in this plan starts scanning, moves a motor, enables an LED, writes persistent NVM or writes an ASIC register.',
                  'Root alone executes COM16 captures. Keep prior raw artifacts immutable.',
                  'Pure table/cached-state access does not prove physicalsensororLEDhealth.',
                  'All nonnull LED records use existing firmware-owned configuration pointers. The command checks top-levelpointer but not every nested pointer; output depends on normallyinitializedconfiguration.'])
(REF/'useful_scanner_round4_review.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
plan=[{k:v for k,v in x.items() if k!='reviewed_ranges'} for x in entries]
(REF/'50-scanner-commands.json').write_text(json.dumps(plan,indent=2)+'\n',encoding='utf-8')
print(json.dumps(report['summary']))
