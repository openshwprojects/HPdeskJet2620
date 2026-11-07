"""Prepare two new information queries using bounded static and live evidence."""
import hashlib
import json
from datetime import datetime, timezone
from scanner_review_round4 import ROOT, DATA, dis, u32

REF = ROOT / 'references'
def sha(raw):
    return hashlib.sha256(raw).hexdigest()

evidence = {}
for folder in ['physical-59-metadata-pointers', 'physical-60-ui-metadata',
               'physical-64-ui-fields', 'physical-68-query-prerequisites']:
    source = REF / folder / 'provider-pointers.json'
    for obs in json.loads(source.read_text())['observations']:
        raw = (source.parent / obs['response_file']).read_bytes()
        assert sha(raw) == obs['response_sha256']
        evidence[(folder, obs['label'])] = dict(
            obs, artifact=source.relative_to(ROOT).as_posix(),
            response_file=(source.parent / obs['response_file']).relative_to(ROOT).as_posix())

def ev(folder, label):
    return evidence[(folder, label)]

settings = ev('physical-64-ui-fields', 'settings_provider_header')
storage = ev('physical-68-query-prerequisites', 'default_settings_storage_provider')
date = ev('physical-68-query-prerequisites', 'cached_printer_build_date')
gate = ev('physical-60-ui-metadata', 'security_status_cache')
assert settings['words_be'] == ['0xa06073ec', '0x2084a0c0', '0x33']
assert storage['words_be'] == ['0xa0517544']
assert date['words_be'] == ['0x133ee90']
assert gate['words_be'] == ['0x0', '0x1']
assert u32(0xa06073ec + 8) == 0xa03ce8e1
assert u32(0xa0517544) == 0xa0517548
assert u32(0xa0517548 + 8) == 0xa005c38d

baseline = json.loads((REF / 'round6-baseline.json').read_text())
inventory = json.loads((REF / 'static_inventory.json').read_text())
descriptors = {x['name']: x for x in inventory['commands']}
scalars = {x['dsid']: x for x in json.loads((REF / 'physical-storage-values.json').read_text())['objects']}
rows = []

def old_scalar(dsid):
    source = scalars[dsid]['ds2_scalar_latest']
    assert sha((ROOT / source['source']['response_file']).read_bytes()) == source['source']['response_sha256']
    return dict(dsid=dsid, name=scalars[dsid]['name'], **{k: source[k] for k in ['value', 'source']})

def add(name, args, purpose, proof, ranges, **extra):
    assert name not in baseline['invoked_names']
    item = descriptors[name]
    row = dict(name=name, command='udws "' + name + (' ' + args if args else '') + ';"',
               argument_syntax=args, handler=item['handler'], descriptor_address=item['descriptor_address'],
               classification='reviewed-information-query', purpose=purpose, control_flow_proof=proof,
               timeout=20, hardware_access=False, persistent_writes=False,
               side_effects='Parser bookkeeping, local output buffers and UART output.',
               prior_invocation_status='not-invoked', runtime_registered=True,
               image_sha256=sha(DATA), review_source='references/round6_hardware_review.json',
               reviewed_ranges=[dict(address=hex(a), bytes=n, disassembly=dis(a, n)) for a, n in ranges])
    row.update(extra)
    rows.append(row)

for idx, dsid, label in [(1, 0x12be9, 'ColorLok'), (3, 0x12c28, 'dry time'), (4, 0x12c29, 'eject behavior')]:
    assert u32(0x2084a0c0 + idx * 12) == dsid
    prior = old_scalar(dsid)
    assert prior['value'] in [1, 2]
    add('smgr_pe.get_default_setting', str(idx), 'Read the stored ' + label + ' setting through the default-setting interface.',
        'A03DB960 parses one integer and calls settings-interface slot8. Physical60 identifies object2084A0B4, '
        'and physical64 proves its header: vtableA06073EC, table2084A0C0, count51. Exact indices1/3/4 are bounded. '
        'Slot8 A03CE8E0 reads the record DSID at table+12*index and calls storage-interface slot8. Physical68 '
        'resolves20DA30DC toA0517544; physical59 proves its vtableA0517548 and slot8 A005C38C. That helper searches '
        'the in-memory DS2 hash chain and returns the scalar; no setter or physical peripheral call. '
        'Earlier direct NVM and DS2 observations establish valid nonzero values for these three DSIDs. '
        'The CLI has only a three-byte buffer for "%01d;"; the expected single-digit values fit and '
        'the captured response must be checked for a complete delimiter and agreement.',
        [(0xa03db960, 0x3a), (0xa03ce8e0, 0x18), (0xa005c38c, 0x3e)],
        setting_index=idx, dsid=dsid, expected_value=prior['value'], prior_storage=prior,
        live_evidence=[settings, storage, ev('physical-60-ui-metadata', 'ui_interface_1'),
                       ev('physical-59-metadata-pointers', 'led_interface_1_object_vtable'),
                       ev('physical-59-metadata-pointers', 'led_interface_1_vtable_methods')],
        interpretation='This method reads stored DS2 values. It does not read the compiled default at record offset8, and it does not change a setting.')

prior_date = old_scalar(0x13042)
prior_override = old_scalar(0x13fc3)
assert prior_date['value'] == 20180624
add('pen.get_printer_build_date', '', 'Read the printer build-date scalar used by the pen subsystem.',
    'A032B2B8 callsA03237B6 and prints its scalar through the standard CLI integer formatter. '
    'A0323776 first callsA00089DE. Its gateA0008924 reads20004894; physical60 proves initialized flag1, '
    'so it skips initialization. A0008938 performs the previously reviewed software hash/compare on boot RAM. '
    'If overrides are permitted, only the fixed scalar DSID0x13FC3 is read usingA03211B4 '
    '(bounded current-task cache, otherwise storage getter); there is no transaction unlock or write path. '
    'Earlier DS2 returned0 for that override, which is retained as raw evidence because zero alone is ambiguous. '
    'When no nonzero override is returned, A03237B6 reads20D02D94. Physical68 proves it is nonzero20180624, '
    'so the fallback-provider call and cache write are skipped on the observed state. '
    'For completeness the fallback objectA05C5154 has slot12 A02D16AE, a fixedDSID0x13042 scalar read. '
    'Earlier direct NVM and DS2 returned the same20180624 for DSID_MFG_DATE_EPOCH. No motors, ink, '
    'scanner lighting, setting changes or persistent writes are invoked.',
    [(0xa032b2b8, 0x18), (0xa03237b6, 0x26), (0xa0323776, 0x18),
     (0xa00089de, 0x14), (0xa00089a0, 0x1e), (0xa0008924, 0x14),
     (0xa0008938, 0x68), (0xa03211b4, 0x3c), (0xa02d16ae, 0x10)],
    expected_value=20180624, expected_date='2018-06-24', dsid=0x13042,
    prior_storage=prior_date, prior_override=prior_override,
    live_evidence=[date, gate, ev('physical-68-query-prerequisites', 'build_date_fallback_provider')],
    shared_gate_review='references/round5_misc_review_corrected.json: crypto_svc.is_secure',
    interpretation='Stored printer manufacture/build date used by firmware, not the firmware compilation date. Calendar interpretation follows decimal YYYYMMDD. The command permits a nonzero diagnostic override if enabled.',
    side_effects='Local buffers and UART output. The observed nonzero cache bypasses the fallback cache write; initialized security gate bypasses its volatile initialization.')

report = dict(schema_version=1, generated_utc=datetime.now(timezone.utc).isoformat(),
              provenance=dict(serial_access_by_reviewer='none', image_sha256=sha(DATA),
                              baseline='references/round6-baseline.json', baseline_count=698),
              summary=dict(ready_invocations=len(rows), unique_names=len({x['name'] for x in rows}),
                           new_unique_names=len({x['name'] for x in rows})), queries=rows,
              notes=['Physical captures are required before claiming either new command tested.',
                     'Historical captures and earlier round files are unchanged.',
                     'Live evidence remains from the same uninterrupted printer boot according to root.'])
(REF / 'round6_hardware_review.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
(REF / '71-hardware-commands.json').write_text(json.dumps(
    [{k: v for k, v in x.items() if k != 'reviewed_ranges'} for x in rows], indent=2) + '\n', encoding='utf-8')
print(json.dumps(report['summary']))
