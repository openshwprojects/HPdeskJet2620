"""Validate physical scanner captures using exact binary metadata and field counts."""
import hashlib
import json
import re
from datetime import datetime, timezone
from scanner_review_round4 import ROOT, DATA, u32, string

REF=ROOT/'references'
CAP=REF/'physical-50-scanner-capabilities'
session=json.loads((CAP/'session.json').read_text())
results=[]
revalidations=[]
quality=[]
def digest(data):return hashlib.sha256(data).hexdigest()
def ws(text):return ' '.join(text.split())
def model_resolution_list(table_index,adf):
    ptr=u32(0x20857d38+table_index*4)
    words=[int.from_bytes(DATA[(ptr&0x3ffffff)+32+i*2:(ptr&0x3ffffff)+34+i*2],'big',signed=True) for i in range(14)]
    sentinel=next(i for i,v in enumerate(words[:12]) if v == -1)
    limit=words[sentinel+1] if adf else words[sentinel-1]
    scratch=[0]*14
    i=0
    while i<13:
        val=words[i]
        if val>limit:break
        if val==-1:
            scratch[i-1]=0
            break
        scratch[i]=val
        i+=1
    scratch[i]=0
    answer=scratch[:scratch.index(0)]
    return {'table_index':table_index,'pointer':hex(ptr),'source_halfwords':words,
            'sentinel_index':sentinel,'selected_limit':limit,'result':answer,
            'scratch_terminator_included_in_printed_output':0}

assert session['stop_reason']=='batch-complete'
assert len(session['commands'])==12
for rec in session['commands']:
    path=CAP/rec['response_file']
    raw=path.read_bytes()
    sha=digest(raw)
    assert sha==rec['response_sha256']
    assert len(raw)==rec['response_bytes']
    assert rec['prompt_returned'] and rec['status']=='prompt-returned'
    text=raw.decode('ascii').replace('\r','').strip()
    expected_start='-> '+rec['command']
    assert text.startswith(expected_start)
    rest=text[len(expected_start):]
    assert rest.endswith(';udws() returns 0 (0x0)\n\n->') or re.search(r';udws\(\) returns 0 \(0x0\)\s*->$',rest)
    payload=re.sub(r';udws\(\) returns 0 \(0x0\)\s*->$','',rest).strip()
    obj={'index':rec['index'],'name':rec['name'],'command':rec['command'],
         'response_file':path.relative_to(ROOT).as_posix(),'response_sha256':sha,
         'response_bytes':len(raw),'prompt_returned':True,'wrapper_return':0}
    if rec['index']==1:
        assert ws(payload)=='SCANNER SENSOR: (CIS_MODULE_TEMPO_TOS), SCANNER SUPPLIER: (CIS_SUPPLIER_CSI)'
        obj.update(sensor='CIS_MODULE_TEMPO_TOS',sensor_enum_from_binary=2,supplier='CIS_SUPPLIER_CSI',field_count=2)
        assessment='complete-cached-scanner-identity'
        note='Both sensor and supplier fields are present and match the required TEMPO identity.'
    elif rec['index']==2:
        m=re.fullmatch(r'LED ONTIMEs: \((\d+), (\d+), (\d+)\)',payload)
        assert m
        obj.update(rgb_on_times=[int(x) for x in m.groups()],field_count=3)
        assessment='complete-cached-led-on-times'
        note='Exactly three cached RGB on-time values. All are zero; this does not measure LED electrical behavior.'
    elif rec['index']==3:
        m=re.fullmatch(r'_dasher_detected\[0\]\[0\] = (\d+)\s+_sc_dasher_ic_chip_id\[0\]\[\] = (0x[\da-fA-F]+)',payload)
        assert m
        obj.update(detected=int(m[1]),chip_id=int(m[2],16),field_count=2)
        assessment='complete-cached-bolt-detection-state'
        note='Both cached fields are present and zero. These fields do not identify the main AXON SoC or disprove the separately identified COMET scanner AFE.'
    elif rec['index']==4:
        fields={k:int(v) for k,v in re.findall(r'^\s*(start_pix|npixels_keep|v0|vn|_db_udw)\s*=\s*(-?\d+)',payload,re.M)}
        assert len(fields)==5 and all(v==0 for v in fields.values())
        assert len(re.findall('keep_region\\[0\\]',payload))==1
        assert len(re.findall(r'\(\+0\.0000\)',payload))==2
        obj.update(fields=fields,field_count=5)
        assessment='complete-cached-keep-configuration'
        note='One KEEP region has all five expected fields; numeric and decimal forms agree at zero.'
    elif rec['index']==5:
        rows=re.findall(r'^ADF = (No|Yes), Color Space =\s+(RGB|K)\s+: yres\[(\d)\]\[(\d)\] :\s*([\d, ]+)$',payload,re.M)
        assert len(rows)==4
        parsed=[]
        for adf,color,a,c,values in rows:
            a,c=int(a),int(c)
            assert a==(adf=='Yes') and c==(color=='K')
            physical=[int(x.strip()) for x in values.split(',')]
            model=model_resolution_list(84 if c==0 else 72,a)
            assert physical==model['result']+[0]
            parsed.append({'adf_label':adf,'color_space':color,'indices':[a,c],
                           'printed_values':physical,'model':model})
        assert [p['indices'] for p in parsed]==[[0,0],[0,1],[1,0],[1,1]]
        obj.update(rows=parsed,row_count=4,all_rows_match_binary_model=True,
                   upper_listed_non_adf_dpi=2400,upper_listed_adf_dpi=600)
        assessment='complete-resolution-lists-matching-binary-model'
        note='All four ordered rows and every integer, including each zero terminator, match A03BD61C applied to the TEMPO tables. The helper clears the value immediately before the -1 marker, so 4800 in the underlying table is omitted. This does not establish optical resolution or installed ADF hardware.'
    elif 6<=rec['index']<=11:
        assert payload==''
        obj.update(resolution_argument=int(rec['argument_syntax']),payload_empty=True,
                   inferred_null_cached_record=True,calibration_fields_returned=0)
        assessment='complete-empty-led-record-query'
        note='The full wrapper and prompt returned with no payload. A03A9A9C only prints an empty line when the selected cached record pointer is null. Null-pointer interpretation is inferred from the reviewed control flow; no calibration values were returned.'
    else:
        addresses=[0xa03bde76,0xa03bde7c,0xa03bde82,0xa03bde88,0xa03bde8e,0xa03bde94,0xa03bde9a]
        # All seven are Thumb ADR r0 instructions; immediate is imm8 times four.
        chunks=[]
        for address in addresses:
            insn=int.from_bytes(DATA[address&0x3ffffff:(address&0x3ffffff)+2],'big')
            assert insn&0xff00==0xa000
            target=((address+4)&~3)+(insn&255)*4
            chunks.append(string(target))
        assert ws(payload)==ws(''.join(chunks))
        selectors=[int(x) for x in re.findall(r'^\s*(\d+)\s*(?:;|<)',payload,re.M)]
        assert selectors==[0,1,2,10,11,12,20,30,99]
        obj.update(help_only=True,selector_count=9,selectors=selectors,
                   help_matches_all_seven_firmware_literals=True,
                   selector_meaning={'0':'Usage','1':'Select timing (sets volatile override)',
                   '2':'Duplex selection documented, but this build immediately returns',
                   '10':'Read selected timing parameters','11':'Read with resolution/exposure/color details',
                   '12':'Read with field offsets','20':'Set selected parameter','30':'Set all gain parameters',
                   '99':'Set debug mode'})
        assessment='complete-built-in-help-only'
        note='Help matches all seven firmware literals and lists nine selectors. Only selector 0 ran; no timing parameters were queried or changed. Help advertises selector 2, but binary A03BDE50 immediately enters the common return epilogue for it.'
    obj.update(valid=True,complete=True,assessment=assessment,note=note)
    results.append(obj)
    common={k:obj[k] for k in ['response_file','response_sha256','assessment','note']}
    revalidations.append(dict(common,valid=True,complete=True))
    quality.append(dict(common,complete=True))

report={'schema_version':1,'generated_utc':datetime.now(timezone.utc).isoformat(),
 'provenance':{'source':'Physical UART capture independently validated offline against firmware',
 'capture':'references/physical-50-scanner-capabilities/session.json',
 'capture_sha256':digest((CAP/'session.json').read_bytes()),'image':'artifacts/ghs/dram.bin',
 'image_sha256':digest(DATA),'serial_access_by_reviewer':'none',
 'review':'references/useful_scanner_round4_review.json',
 'review_sha256':digest((REF/'useful_scanner_round4_review.json').read_bytes())},
 'summary':{'physical_commands':12,'prompt_returned':12,'validated_responses':12,
 'unique_names':7,'new_unique_names_at_plan_creation':6,'resolution_lists':4,
 'resolution_lists_match_binary_model':True,'empty_led_record_queries':6,'help_only_queries':1},
 'response_revalidations':revalidations,'data_quality':quality,'results':results,
 'limitations':['These commands read cached configuration and capability lists; none measured hardware operation.',
 'All six per-resolution LED records were absent at query time; no LED calibration was established.',
 'Resolution lists are firmware-reported values, not verified optical scanner capability.',
 'Raw captures and original capture metadata were not modified.']}
(REF/'useful_scanner_round4_results.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
print(json.dumps(report['summary']))
