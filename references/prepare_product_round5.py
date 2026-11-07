"""Offline bounded product-query plan; never opens a serial port."""
from pathlib import Path
import hashlib
import json

ROOT=Path(__file__).resolve().parents[1]
REF=ROOT/'references'
image=(ROOT/'artifacts/ghs/dram.bin').read_bytes()
inventory={x['name']:x for x in json.loads((REF/'static_inventory.json').read_text())['commands']}
coverage={x['name']:x for x in json.loads((REF/'coverage.json').read_text())['commands']}
rows=[]
def add(name,args,purpose,proof,ranges,limits='',effects='Reads initialized metadata, writes caller buffers and formats UART output.'):
    d=inventory[name]
    row={'name':name,'command':'udws "'+name+(' '+args if args else '')+';"','handler':d['handler'],'descriptor_address':d['descriptor_address'],'arguments':args.split(),'purpose':purpose,'classification':'reviewed-bounded-product-query','review_source':'references/round5_product_review.json','review_evidence':proof,'side_effects_assessment':effects,'result_limitations':limits,'timeout':30,'prior_invocation_status':coverage[name]['status'],'code_evidence':[{'start':hex(a),'end_exclusive':hex(b),'sha256':hashlib.sha256(image[a&0x3ffffff:b&0x3ffffff]).hexdigest()} for a,b in ranges]}
    rows.append(row)

media_binding='MHDLG consumer slot4 is interface SUID0x1154a in A0518754..A051876C. PE provider table A05CC93C has matching SUID, slot1; interface getter A0305068 returns initialized object A05CC04C from20848514. Object points to vtable20848770. Relevant methods +8/+10/+60/+64/+6C/+EC resolve to A0303E9C/A0303EBA/A030408C/A030409A/A03040B0/A030437A.'
add('mhdlg.get_mhdlgcap_supported_trays','', 'Enumerate supported paper input trays and their numeric IDs.',
    media_binding+' A0062F7A uses +10 -> A0303EBA -> A030DC0C, a bounded ROM table traversal. ROM A05CFBB0 has1 record, ID1, so count fits14slots. A006345E formats count and name/ID using A02541FC.',
    [(0xa006345e,0xa0063562),(0xa0062f7a,0xa0063052),(0xa030dc0c,0xa030dc3c)],
    'Finite capability metadata. No tray activation or paper movement.')
add('mhdlg.get_mhdlgcap_supported_bins','','Enumerate the configured paper output bins.',
    media_binding+' A0063052 uses +EC -> A030437A, which writes bin0 and returns count1. Handler A0063562 prints status200,count1,ID0; this is the model capability implementation, not a disabled command handler.',
    [(0xa0063562,0xa0063628),(0xa0063052,0xa00630a4),(0xa030437a,0xa0304382)])
for selector in [0,1]:
    add('mhdlg.get_mhdlgcap_media_type_size_list',str(selector)+' 1',
        'Enumerate '+('media sizes' if selector==0 else 'media types')+' for verified tray1.',
        media_binding+' First parsed integer0 selects sizes,1 selects types; second is trayID. A00630A4 routes sizes through +8 -> A030DADE and types through +60 -> A030E04C. Both deduplicate bounded ROM arrays into caller buffers; tray1 table has32 sizes and4 types. Handler prints symbolic and numeric identifiers.',
        [(0xa0063628,0xa0063798),(0xa00630a4,0xa006315a),(0xa030dade,0xa030db7a),(0xa030e04c,0xa030e0ba)],
        'The two selectors share one registered command name and count as one name.')
add('mhdlg.get_supported_df_groups','','Determine whether the firmware exposes additional media feed groups.',
    media_binding+' A0063212 calls +64 -> A030409A -> A030E0BA. It reads group count from ROM A05CFBB8 (0 in this image), writes only bounded caller array, and returns status200 pluscount0. A0063896 outputs both status and count.',
    [(0xa0063896,0xa006390c),(0xa0063212,0xa0063252),(0xa030e0ba,0xa030e0e0)],
    'A successful zero-count capability result is expected, not a media-motion test.')

held=[
 {'names':['dsd_ols_prdcfg.get_time_stamp'],'reason':'Misleading getter name: handler A0074F86 parses a date string, then A0072AD0 validates and converts it. Its success path calls A00727F6 and A04306EE to update date/clock state. Not a passive timestamp read.'},
 {'names':['ultra_spectra.get_vendor_ie','ultra_spectra.get_vendor_ie_json'],'reason':'Local GET_VAR path A0459A3C checks driver, zeroes 2048-byte output and checks result, but both formatters trust returned item count and IE lengths without checking the end of that buffer. Deferred instead of claiming a fully bounded parse.'},
 {'names':['mhdlg.get_supported_types_by_group'],'reason':'No DF groups in ROM A05CFBB8. A supplied group would only test404, adding no useful successful capability read.'},
 {'names':['mhdlg.get_mhdlgdyn','rest_eccl.get_copy_caps','rest_eccl.get_copy_default_settings','rest_eccl.get_copy_dependent_caps_list','cloud_data.get_current_config','cloud_data.get_printer_config','cloud_data.get_reg_id','gcp_privet.get_email_addr','jm_copy.get_supported_media_sizes','jm_copy.get_mcm_page_count'],'reason':'Output paths are removed or do not publish the fetched values in this build; excluded from useful-command target instead of padding names.'},
 {'names':['escl.get_config_state'],'reason':'Misleading getter: A0373DF2 calls A0378624 with callback A0373F19, which stores callback at20D8E258 and returns1. It reports callback-registration success rather than querying current configuration.'},
 {'names':['cloud_reg.get_claim_info','cloud_reg.get_claim_status','cloud_reg.get_email'],'reason':'Callbacks are queued to cloud worker; may initiate external service requests. Not needed for local state capture.'},
 {'names':['reports_mgr.reports_list'],'reason':'Handler loops all IDs1..999 and unconditionally counts each lookup; it ignores backend success before strlen on stack output. Cannot establish meaningful bounded report rows without further backend proof.'},
]
report={'schema_version':1,'kind':'offline-review-not-physical-results','image':'artifacts/ghs/dram.bin','image_sha256':hashlib.sha256(image).hexdigest(),'serial_access':'none','method':'Direct big-endian Thumb disassembly, concrete argument-path review and provider resolution. Generic UART and OS helpers not formally proven transitively.','queries':rows,'held_for_further_review':held,'summary':{'invocations':len(rows),'new_names':len({x['name'] for x in rows})}}
(REF/'round5_product_review.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
(REF/'55-product-commands.json').write_text(json.dumps(rows,indent=2)+'\n',encoding='utf-8')
print(json.dumps(report['summary']))
