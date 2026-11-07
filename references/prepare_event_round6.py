"""Prepare one bounded event query from physically captured ring entries."""
import hashlib
import json
from pathlib import Path

REF = Path(__file__).resolve().parent
ROOT = REF.parent
observations = {}
sources = []
for directory in ['physical-68-query-prerequisites', 'physical-74-event-prerequisite']:
    p = REF / directory / 'provider-pointers.json'
    report = json.loads(p.read_bytes())
    assert report['cleanup_complete']
    for row in report['observations']:
        assert hashlib.sha256((p.parent / row['response_file']).read_bytes()).hexdigest() == row['response_sha256']
        observations[row['label']] = row
    sources.append({'path':str(p.relative_to(ROOT)).replace('\\','/'), 'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
state = bytes.fromhex(observations['event_ring_metadata_and_providers']['hex'])
first = bytes.fromhex(observations['event_at_oldest_ring_index']['hex'])
second = bytes.fromhex(observations['next_event_ring_record']['hex'])
assert state[:4] == bytes([7,7,0,0])
assert state[12:20].hex() == 'a061f594a0517544'
sequence = int.from_bytes(first[:4], 'big')
assert sequence == 1266 and int.from_bytes(second[:4], 'big') == 1271
assert first[8] == 9 and first[9:18] == b'FROM_NVM\0'
assert int.from_bytes(first[4:8], 'big') == 72234
row = {
    'name':'event_log.get_event', 'command':f'udws "event_log.get_event {sequence};"',
    'handler':'0xa00c3d71', 'descriptor_address':'0xa0522b40',
    'classification':'reviewed-information-query', 'timeout':20,
    'purpose':'Read an existing event by its observed sequence number.',
    'control_flow_proof':
        'A00C3D70 zeros240 bytes of caller stack and reads one integer. Physical68 proves CLI providerA061F594 '
        'and scalar-storage providerA0517544, both resolved in earlier captures. A00C3946 initializes output '
        'and callsA00C39A8. Ring count7, end7, oldest0 are valid for15 slots. Query1266 lies in the observed '
        'slot0 interval[1266,1271), so the first iteration selects slot0. A0025294 is a veneer to ITCM304 '
        'integer remainder; divisor15 is nonzero. The scalar callback only reads DSID10C7D current sequence. '
        'A00C3946 copies at most228 bytes; this selected record has9 bytes including a verified NUL, '
        'so the subsequent %s print is bounded. No event add, clear, purge or NVM-write helper is called.',
    'side_effects':'UART output, local output struct and shared300-byte formatting scratch only.',
    'hardware_access':False, 'persistent_writes':False,
    'interpretation':'Stored event sequence number, not a timestamp. Occurrences field is the getter presence flag1. '
        'The initial diagnostic prints0 from the zeroed output field before lookup; it does not echo the requested sequence.',
    'expected':{'sequence':1266, 'event_id':72234, 'payload':'FROM_NVM', 'presence_flag':1},
    'image_sha256':hashlib.sha256((ROOT/'artifacts/ghs/dram.bin').read_bytes()).hexdigest(),
    'itcm_sha256':hashlib.sha256((ROOT/'artifacts/ghs/itcm.bin').read_bytes()).hexdigest(),
    'prior_invocation_status':'not-invoked', 'runtime_registered':True,
    'review_source':'references/round6_event_review.json'}
(REF/'73-event-commands.json').write_text(json.dumps([row],indent=2)+'\n',encoding='utf-8')
(REF/'round6_event_review.json').write_text(json.dumps({'queries':[row], 'physical_evidence':sources},indent=2)+'\n',encoding='utf-8')
print(row['command'])
