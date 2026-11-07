"""Prepare two scanner geometry queries with the live motor callback resolved."""
import hashlib
import json
from datetime import datetime, timezone
from scanner_review_round4 import ROOT, DATA, dis, u32

REF = ROOT / 'references'
def sha(raw):
    return hashlib.sha256(raw).hexdigest()

source = REF / 'physical-75-query-prerequisites' / 'provider-pointers.json'
observations = {}
for item in json.loads(source.read_text())['observations']:
    if not item['label'].startswith('scanner_'):
        continue
    path = source.parent / item['response_file']
    assert sha(path.read_bytes()) == item['response_sha256']
    observations[item['label']] = dict(item, artifact=source.relative_to(ROOT).as_posix(),
                                      response_file=path.relative_to(ROOT).as_posix())
assert observations['scanner_storage_provider']['words_be'] == ['0xa0517544']
assert observations['scanner_motor_provider']['words_be'] == ['0xa060bf74']
assert observations['scanner_motor_object_vtable']['words_be'] == ['0xa060bf7c']
assert observations['scanner_motor_vtable_methods']['words_be'][10] == '0xa03e3bf1'
assert u32(0xa060bf7c + 0x28) == 0xa03e3bf1
assert u32(0xa03e40e4) == 0xbd302010
assert observations['scanner_home_flags']['hex'] == '00000000'
origin_words = [int(x, 16) for x in observations['scanner_origin_offsets_and_overrides']['words_be']]
assert origin_words[1] == 0x8d8 and origin_words[11:13] == [0, 0]

storage_path = REF / 'physical-59-metadata-pointers' / 'provider-pointers.json'
storage_evidence = []
for item in json.loads(storage_path.read_text())['observations']:
    if item['label'] in ['led_interface_1_object_vtable', 'led_interface_1_vtable_methods']:
        path = storage_path.parent / item['response_file']
        assert sha(path.read_bytes()) == item['response_sha256']
        storage_evidence.append(dict(item, artifact=storage_path.relative_to(ROOT).as_posix(),
                                     response_file=path.relative_to(ROOT).as_posix()))
assert u32(0xa0517544) == 0xa0517548 and u32(0xa0517548 + 8) == 0xa005c38d

baseline = json.loads((REF / 'round7-baseline.json').read_text())
inventory = json.loads((REF / 'static_inventory.json').read_text())
descriptors = {x['name']: x for x in inventory['hierarchical_commands_with_static_registration']}
registered = {x['name'] for x in json.loads((REF / 'physical-09-registry/runtime_inventory.json').read_text())['entries']}
programmer_bits = next(x for x in json.loads((REF / 'physical-storage-values.json').read_text())['objects']
                       if x['dsid'] == 0x107a2)
for key in ['nvm_contents_latest', 'ds2_scalar_latest']:
    previous = programmer_bits[key]
    assert previous['value'] == 0
    assert sha((ROOT / previous['source']['response_file']).read_bytes()) == previous['source']['response_sha256']

motor_proof = (
    'Physical75 resolves scanner provider20DA0AEC to objectA060BF74, vtableA060BF7C, slot0x28 A03E3BF0. '
    'A03AD8A8(0) selects that flatbed object. A03E3BF0 recognizes objectA060BF74 and selects motor index0, '
    'then callsA03E01F6 -> A03E3E32. This last helper performs one 32-bit load from position registerBD302010, '
    'callsA03E3E60(0,value) to refresh only volatile cache208593AC, and returns the loaded word. '
    'There are no MMIO stores, motion commands, power changes, delays or persistent writes on this path. '
    'This is a live position-register read, not a purely cached query.')
motor_ranges = [(0xa03ad8a8, 0x26), (0xa03e3bf0, 0x24), (0xa03e01f6, 8),
                (0xa03e3e32, 0x10), (0xa03e3e60, 0x1a)]
rows = []
def add(name, purpose, proof, ranges, **extra):
    assert name not in baseline['invoked_names'] and name in registered
    item = descriptors[name]
    row = dict(name=name, command='udws "' + name + ';"', argument_syntax='',
               handler=item['handler'], descriptor_address=item['descriptor_address'],
               classification='reviewed-information-query', purpose=purpose, control_flow_proof=proof,
               timeout=20, persistent_writes=False, runtime_registered=True,
               hardware_access='Read-only position register BD302010; no actuator writes.',
               side_effects='May refresh volatile position cache208593AC. UART formatting may update its output-throttle counter; no setting or mechanism state is commanded.',
               image_sha256=sha(DATA), prior_invocation_status='not-invoked',
               review_source='references/round7_hardware_review.json',
               live_evidence=[observations[x] for x in ['scanner_motor_provider', 'scanner_motor_object_vtable', 'scanner_motor_vtable_methods']],
               reviewed_ranges=[dict(address=hex(a), bytes=n, disassembly=dis(a,n)) for a,n in ranges])
    row.update(extra)
    rows.append(row)

add('scan orig locget', 'Read the scanner cover, park, white-reference and black-reference locations.',
    'A03AEAE8 calls four getters and prints four signed scalars. A03AE1CC reads cover override208582FC; '
    'if zero, it returns origin208582D4+0x4F6, otherwise override+0x8D8. '
    'A03AE1E0 reads park override20858300; if zero it callsA03A55C8. This tests bits2 and0 of '
    'DSID_PROGRAMMERS_BITS(0x107A2) throughA03A34DC/A03A34D0/A03A32A8 and the verified '
    'storage slot8 A005C38C, then cached home flag20D9CD45. Nonzero predicate returnsorigin+0x59; '
    'zero predicate reads the position getter with index0. Physical75 shows both overrides and home flagzero, '
    'and earlier NVM/DS2 both returnedzero for programmer bits. All branches are reviewed. '
    'A03AE204 selects nonzero white override20D9F280 or cached20D9F218 and addsorigin208582D4. '
    'A03AE216 selects nonzero black override20D9F284 or cached20D9F220 and adds the sameorigin. '
    + motor_proof,
    [(0xa03aeae8, 0x34), (0xa03ae1cc, 0x5c), (0xa03a55c8, 0x1c),
     (0xa03a34d0, 0x18), (0xa03a32a8, 0x10), (0xa005c38c, 0x3e)] + motor_ranges,
    expected_field_labels=['cover location', 'park location', 'white location', 'black location'],
    expected_cover_from_preflight=origin_words[1] + 0x4f6,
    origin_evidence=observations['scanner_origin_offsets_and_overrides'],
    home_evidence=observations['scanner_home_flags'],
    storage_provider_evidence=observations['scanner_storage_provider'],
    storage_method_evidence=storage_evidence,
    prior_programmer_bits=[programmer_bits[x] for x in ['nvm_contents_latest', 'ds2_scalar_latest']],
    interpretation='Firmware location units; no physical distance conversion is assumed. Park may reflect the current position when the scanner has no retained home/override state.')

add('scan mtr getpos', 'Read the current flatbed scanner motor position register.',
    'A03ADF36 callsA03AD8A8 with fixedindex0, prints "Motor Position: %d", and returns. ' + motor_proof,
    [(0xa03adf36, 0x12)] + motor_ranges,
    expected_field_labels=['Motor Position'],
    interpretation='Raw signed position-register value in firmware units. Reading the position does not command a move or establish calibration accuracy.')

report = dict(schema_version=1, generated_utc=datetime.now(timezone.utc).isoformat(),
              provenance=dict(serial_access_by_reviewer='none', baseline='references/round7-baseline.json',
                              baseline_count=703, image_sha256=sha(DATA)),
              summary=dict(ready_invocations=len(rows), new_unique_names=len(rows), new_unique_handlers=len(rows)),
              queries=rows,
              held_paths=[dict(name='scan ifc cabledetect', reason='Live init count is sentinel -1 and this board has no current detector callback; omitted in favor of two commands returning real location data.'),
                          dict(name='pen.is_ready_for_speed_testing', reason='Lazy date/state initialization requires a broader helper review; not admitted in this batch.')],
              notes=['The origin fallback and explicit position query share the same position reader but have distinct handlers and outputs.',
                     'No homing, scanning, motor start, motor disable, ink firing or persistent setter is called.',
                     'Earlier unresolved callback hold for scan orig locget is superseded for this exact physical interface by physical75.'])
(REF / 'round7_hardware_review.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
(REF / '77-hardware-commands.json').write_text(json.dumps(
    [{k:v for k,v in row.items() if k != 'reviewed_ranges'} for row in rows],indent=2)+'\n',encoding='utf-8')
print(json.dumps(report['summary']))
