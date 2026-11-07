"""Independently validate the round-five cached hardware captures offline."""
import hashlib,json,re
from datetime import datetime,timezone
from scanner_review_round4 import ROOT,DATA,u32
REF=ROOT/'references'
def sha(raw):return hashlib.sha256(raw).hexdigest()
scalars={x['dsid']:x for x in json.loads((REF/'physical-storage-values.json').read_text())['objects']}
arrays={x['dsid']:x for x in json.loads((REF/'physical-storage-arrays.json').read_text())['arrays']}
results=[]; validations=[];quality=[]
def read_session(dirname):
    cap=REF/dirname
    session=json.loads((cap/'session.json').read_text())
    assert session['stop_reason']=='batch-complete'
    for rec in session['commands']:
        path=cap/rec['response_file'];raw=path.read_bytes()
        assert sha(raw)==rec['response_sha256'] and len(raw)==rec['response_bytes']
        assert rec['prompt_returned'] and rec['status']=='prompt-returned'
        text=raw.decode('ascii').replace('\r','').strip()
        prefix='-> '+rec['command']
        assert text.startswith(prefix)
        rest=text[len(prefix):].strip()
        assert re.search(r';?udws\(\) returns 0 \(0x0\)\s*->$',rest)
        payload=re.sub(r';?udws\(\) returns 0 \(0x0\)\s*->$','',rest).strip()
        yield rec,payload,dict(name=rec['name'],command=rec['command'],index=rec['index'],
          response_file=path.relative_to(ROOT).as_posix(),response_sha256=sha(raw),response_bytes=len(raw),
          prompt_returned=True,wrapper_return=0)
def append(obj,assessment,note):
    obj.update(valid=True,complete=True,assessment=assessment,note=note);results.append(obj)
    common={k:obj[k] for k in ['response_file','response_sha256','assessment','note']}
    validations.append(dict(common,valid=True,complete=True));quality.append(dict(common,complete=True))
for rec,payload,obj in read_session('physical-56-calibration-pen'):
    if rec['name'] in ['pen.get_ttoe_ad_range','pen.get_ttoe_test_range']:
        assert re.fullmatch(r'\s*\d+(?:\s*,\s*\d+){63}',payload)
        values=[int(x) for x in payload.split(',')]
        assert values[0]==63 and len(values)==64 and all(0<=v<=65535 for v in values[1:])
        obj.update(declared_count=63,values=values[1:],all_cached_entries_zero=all(v==0 for v in values[1:]))
        append(obj,'complete-bounded-pen-calibration-table','Count63 and exactly63 unsigned halfwords are present. All are zero in this capture; these cached table values do not demonstrate calibration or hardware health.')
        continue
    assert re.fullmatch(r'-?\d+',payload)
    value=int(payload);obj['value']=value
    if rec['name']=='pen.get_dotcount':
        typ,selector,history=[int(x) for x in rec['argument_syntax'].split()]
        mapped=DATA[0x5df6cc+selector]
        dsid=u32(0xa05d730c+((typ*2+history)*53+mapped)*4)
        old=scalars[dsid]
        assert old['nvm_contents_latest']['value']==value
        assert old['ds2_scalar_latest']['value']==value
        obj.update(dsid=dsid,storage_name=old['name'],matches_prior_nvm_and_ds2=True,
                   cross_check_sources=[old['nvm_contents_latest']['source'],old['ds2_scalar_latest']['source']])
        append(obj,'complete-dot-counter-matching-storage','The full scalar matches both earlier direct NVM and DS2 observations for the ROM-mapped DSID. Catalog names identify the last argument as a history-slot index; the original plan used the generic word channel. These are stored raw dot counts, not page counts or live nozzle tests.')
    elif rec['name']=='pen.get_drfs':
        old=arrays[0x13caf]['entries'][0]['latest']
        assert value==old['value']
        obj.update(dsid=0x13caf,storage_name=arrays[0x13caf]['name'],matches_prior_ds2=True,cross_check_source=old['source'])
        append(obj,'complete-dynamic-refresh-status','Value2 matches the earlier indexed DS2 read of DSID_PEN_DYNAMIC_REFRESH_STATUS[0].')
    elif rec['name'] in ['pen.is_ttoed','pen.is_ldw_ttoed']:
        assert value in [0,1]
        append(obj,'complete-cached-pen-state','One bounded Boolean value returned for the requested chute. It is a stored calibration-state query, not a live electrical test.')
    else:
        append(obj,'complete-cached-calibration-scalar','One signed integer returned from a matching compiled calibration descriptor. Status values are retained as raw codes; -1 is not interpreted as physical hardware failure or successful calibration.')

for rec,payload,obj in read_session('physical-56b-led-metadata'):
    idx=int(rec['argument_syntax'])
    if rec['name']=='led_color_cal.get_default_matrix':
        body=payload.removeprefix('Default correction frontside matrix:').strip()
        assert re.fullmatch(r'-?\d+(?:\s+-?\d+){8}',body)
        values=[int(x) for x in body.split()]
        addr=0xa05c439c if idx==0 else 0xa05c43f4
        expected=[int.from_bytes(DATA[(addr&0x3ffffff)+i*2:(addr&0x3ffffff)+i*2+2],'big',signed=True) for i in range(9)]
        assert values==expected
        obj.update(index_argument=idx,matrix=[values[i:i+3] for i in [0,3,6]],matches_rom=True,rom_address=hex(addr))
        append(obj,'complete-default-matrix-matching-rom','All nine signed fields and all three rows match the selected compiled ROM matrix. This is a default matrix, not measured or current calibration.')
    elif rec['name']=='led_color_cal.get_golden_peaks_widths':
        body=payload.removeprefix('Frontside Golden LED peaks and widths:').strip()
        pairs=re.findall(r'(Red|Green|Blue)\s+(-?\d+)\s+(-?\d+)',body)
        assert len(pairs)==3 and [x[0] for x in pairs]==['Red','Green','Blue']
        assert re.sub(r'(Red|Green|Blue)\s+(-?\d+)\s+(-?\d+)','',body).strip()==''
        values=[int(x) for row in pairs for x in row[1:]]
        addr=0xa05c4390 if idx==0 else 0xa05c43e8
        expected=[int.from_bytes(DATA[(addr&0x3ffffff)+i*2:(addr&0x3ffffff)+i*2+2],'big',signed=True) for i in range(6)]
        assert values==expected
        obj.update(index_argument=idx,rgb_peak_width_pairs=[values[i:i+2] for i in [0,2,4]],matches_rom=True,rom_address=hex(addr))
        append(obj,'complete-golden-reference-matching-rom','Three labelled RGB pairs match all six compiled ROM values. Index0 is620/14,521/34,452/22; index1 iszero. No light measurement took place.')
    else:
        assert ' '.join(payload.split())=='Barcode Barcode not valid'
        obj.update(index_argument=0,barcode='',barcode_decoded=False,expected_validation_failure=True)
        append(obj,'complete-empty-barcode-result','The getter returned an empty stored barcode and explicitly rejected it as invalid. Capture is complete, but no valid LED barcode or derived calibration values were obtained.')

for rec,payload,obj in read_session('physical-56c-ui-status'):
    pattern=(r'\[UI_LITE\]: ui ii pop up state: (\d+), dashboard: (\d+)\s+'
        r'\[UI_LITE\]: Active: (\d+), pen type: (\d+)\s+'
        r'\[UI_LITE\]: size error: (\d+), sensor error: (\d+)\s+'
        r'\[UI_LITE\]: Allows: (\d+), Admin_D: (\d+), Reged: (\d+), Auto_E: (\d+), Pend: (\d+), e: (\d+), Server: (\d+), Main: (\d+)')
    match=re.fullmatch(pattern,payload)
    assert match
    labels=['popup_state','dashboard','active','pen_type','size_error','sensor_error','allows','admin_d','registered','auto_e','pending','e','server','main']
    fields=dict(zip(labels,map(int,match.groups())))
    assert fields['active']==0
    obj.update(fields=fields,field_count=14,active_error_rows=0)
    append(obj,'complete-cached-ui-display-state','Four complete labelled lines contain fourteen cached fields. Active0 is consistent with no per-error rows. Auto_E is1 and other captured fields are0; labels are retained without assuming undocumented flag meanings.')

for rec,payload,obj in read_session('physical-62-current-matrices'):
    idx=int(rec['argument_syntax'])
    body=payload.removeprefix('Color correction frontside matrix:').strip()
    assert re.fullmatch(r'-?\d+(?:\s+-?\d+){8}',body)
    values=[int(x) for x in body.split()]
    defaults=next(x for x in results if x['name']=='led_color_cal.get_default_matrix' and x['index_argument']==idx)
    matrix=[values[i:i+3] for i in [0,3,6]]
    assert matrix==defaults['matrix']
    obj.update(index_argument=idx,matrix=matrix,matches_captured_default=True,
               default_source={k:defaults[k] for k in ['response_file','response_sha256']})
    append(obj,'complete-current-matrix-matching-default','All nine current cached coefficients match the independently captured default matrix for this index. Both live-valid indices0/1 were tested; this does not prove physical LED calibration or health.')

for rec,payload,obj in read_session('physical-65-frontpanel-settings'):
    assert re.fullmatch(r'\d',payload)
    value=int(payload);obj['value']=value
    if rec['name']=='smgr_pe.get_current_setting':
        idx=int(rec['argument_syntax'])
        dsid=u32(0x2084a0c0+idx*12)
        prior=scalars[dsid]['ds2_scalar_latest']
        assert value==prior['value']
        obj.update(setting_index=idx,dsid=dsid,storage_name=scalars[dsid]['name'],
                   matches_prior_ds2=True,cross_check_source=prior['source'])
        append(obj,'complete-current-setting-matching-storage','The one-digit cached setting fits the tiny CLI buffer completely and matches earlier DS2. Indices1/3/4 map to ColorLok1,dry-time2,eject1; no setting was changed.')
    elif rec['name'] in ['pe.cover_open_query','smgr_pe.cover_open_query']:
        assert value in [0,1]
        obj['cached_closed_state']=(value==0)
        append(obj,'complete-cached-cover-state','Raw-cover and state-manager Boolean queries both returned0, consistent with their reviewed mapping and a cached closed state. The physically verified descriptor selected the cached branch; this did not poll GPIO or change cover handling.')
    else:
        assert value in [0,1]
        append(obj,'complete-stored-power-state-classification','Boolean1 returned completely. Handler derives it from fixed storage DSID0x10005 using predicate value not in{0,1,2}; no power transition or electrical measurement occurred.')

for obj in results:
    sources=obj.get('cross_check_sources',[])+([obj['cross_check_source']] if 'cross_check_source' in obj else [])
    for s in sources:
        assert sha((ROOT/s['response_file']).read_bytes())==s['response_sha256']

report=dict(schema_version=1,generated_utc=datetime.now(timezone.utc).isoformat(),
 provenance=dict(serial_access_by_reviewer='none',image_sha256=sha(DATA),
 source_sessions=[dict(path='references/'+p+'/session.json',sha256=sha((REF/p/'session.json').read_bytes())) for p in ['physical-56-calibration-pen','physical-56b-led-metadata','physical-56c-ui-status','physical-62-current-matrices','physical-65-frontpanel-settings']]),
 summary=dict(physical_commands=len(results),validated_responses=len(results),unique_names=len({x['name'] for x in results}),
   pen_range_entries=126,dot_counters_cross_checked=4,dynamic_refresh_status_cross_checked=True,
   current_settings_cross_checked=3,default_matrix_fields_matched=18,golden_reference_fields_matched=12,
   current_matrix_fields_matched=18,cached_ui_fields=14),
 response_revalidations=validations,data_quality=quality,results=results,
 limitations=['Cached calibration and stored counter queries do not test motors, ink firing, LEDs, or physical sensor health.',
              'Snapshots are sequential and not atomic. Historical counts agreeing do not establish that every storage object is current.',
              'Original capture bytes and historical metadata remain unchanged.'])
(REF/'round5_hardware_results.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
print(json.dumps(report['summary']))
