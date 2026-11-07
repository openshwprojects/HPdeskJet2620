"""Prepare a bounded system-consumable snapshot query. Offline only."""
from pathlib import Path
import hashlib
import json
import re

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / 'references'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def observation(folder, label):
    directory = REF / folder
    report = json.loads((directory/'provider-pointers.json').read_text())
    assert report['cleanup_complete'] and not report['source_memory_written']
    item = next(x for x in report['observations'] if x['label'] == label)
    raw = (directory/item['response_file']).read_bytes()
    assert sha(raw) == item['response_sha256']
    match = re.search(rb'\r\n([0-9a-f]+);udws\(\) returns 0 \(0x0\)', raw)
    assert match and match[1].decode() == item['hex']
    data = bytes.fromhex(item['hex'])
    assert len(data) == item['count']
    return data, dict(item, response_file=(directory/item['response_file']).relative_to(ROOT).as_posix(),
                      response_bytes=len(raw))


def main():
    image = (ROOT/'artifacts/ghs/dram.bin').read_bytes()
    inv = {x['name']:x for x in json.loads((REF/'static_inventory.json').read_text())['commands']}
    baseline = json.loads((REF/'round7-baseline.json').read_text())
    name = 'constat.get_system_info'
    assert name not in baseline['invoked_names']
    selectors, a = observation('physical-69-provider-prerequisites','constat_provider_selectors')
    normal, b = observation('physical-69-provider-prerequisites','constat_normal_provider')
    storage, c = observation('physical-75-query-prerequisites','manufacturing_storage_provider')
    state, d = observation('physical-79-ink-cache-prerequisite','pen_ink_cache_state_and_flags')
    assert selectors[8:16] == bytes.fromhex('20896e1000000000')
    assert normal[8:16] == bytes.fromhex('a0517544a05de248')
    assert normal[20:24] == bytes.fromhex('a05c5154')
    assert normal[44:48] == bytes.fromhex('a050fe84') and normal[56] == 1
    assert storage == bytes.fromhex('a0517544')
    words = [int.from_bytes(state[i*4:i*4+4],'big') for i in range(2)]
    flags = list(state[8:10])
    guard = [bool((words[i] & 2) or flags[i] or not words[i]) for i in range(2)]
    assert guard == [True, True]
    cap = REF/'physical-75-query-prerequisites'
    session = json.loads((cap/'session.json').read_text())
    status = next(x for x in session['commands'] if x['command'] == 'udws "ds2.get_list 65759 65760;"')
    raw = (cap/status['response_file']).read_bytes()
    assert sha(raw) == status['response_sha256'] and len(raw) == status['response_bytes']
    assert re.search(rb'\r\n\s*2,\s*2,\s*0;udws\(\) returns 0 \(0x0\)',raw)
    status_evidence = dict(response_file=(cap/status['response_file']).relative_to(ROOT).as_posix(),
                           response_sha256=sha(raw),response_bytes=len(raw),
                           values={'0x100df':2,'0x100e0':0})
    proof = [
        'HandlerA0040A5E takes no arguments and uses a196-byte stack output. A050C790 method0=A003AB60 calls selectorA003DEFE. Physical69 proves normal20896E10 is selected, alternate selectorNULL, normal vtableA050FE84, enabled flag1, DS2objectA0517544, PENobjectA05DE248, serial-number providerA05C5154.',
        'Normal vtable+144=A003E10E initializes output viaA003A284 -> ARMveneerA00414F4 -> A04E416C, copying196-byte template208661B0. Relevant count fields+24/+50/+74 are zero in the initialized template. Initializer fills5 slots in each color list with-1 and callsA003B7A2 for output+A4.',
        'A003B7A2 calls serial-provider size methodA02D16AA (constant15), then methodA02D162C. Physical75 proves that provider storage pointer20BA3B2C=A0517544. DS2vtable+20=A005B8FA -> A005B860 copies at most14 bytes of DSID0x101F6 into15-byte scratch20896DFC, using a semaphore; A02D1666 writes finalNUL. No DS2 setter is called. A003B87A maps selected characters modulo10, then a bounded3x3 neighborhood of ROM tableA050CAF8 computes output+A4. All100 table values are positive, so the sum divisor is nonzero. This updates only the existing serial scratch RAM and caller output.',
        'A003E10E constructs configured consumables[109,108] count2 and supported channels[1,2,3,0] count4. It reads DS2ID0x129EE through getter+8 for output+A0, and PENvtable+8=A0321BC8 reads cached DS2ID0x10A71 for output+9C. It loops exactly2 chutes, obtaining cached status/type through PENmethods0/+4=A0321B2E/A0321BAE -> A03204AC fixed fields0x14/0x15 -> A03211B4. All operations are reads with paired volatile lock bookkeeping reviewed in round6.',
        'Only a zero status with type0 or1 reaches PENmethod+10=A0321CBC. Physical75 cached statuses are color2/black0. A0321CBC calls predicateA0315AE6, which reads words20CFB4A0+4*chute and flags+8+chute. Physical79 proves words[2,0], flags[0,0]; each chute therefore returnsfalse from this predicate. The selected branchA0321CEC calls A03204AC(chute,0x28), reads a cached DS2 value and returns its low byte. The deeper lazy ink-cache/transaction branchA0321C0E is excluded by this observed guard; no such helper is claimed generally safe.',
        'After those reads the backend combines cached states into caller fields only, constructs at most4 usable channels, and returns0. The handler prints OK,11 scalar fields, configured consumable count/list, supported channel count/list, usable channel count/list, then4 final scalars. All formatter loops are bounded by locally constructed counts2/4/at-most4; string helpers bound copies to15/10/20 bytes.'
    ]
    ranges = [(0xa0040a5e,0xa0040cb2),(0xa003ab60,0xa003ab82),
              (0xa003defe,0xa003df22),(0xa003a284,0xa003a2bc),
              (0xa04e416c,0xa04e4178),(0xa003b7a2,0xa003b8a2),
              (0xa02d162c,0xa02d166e),(0xa02d16aa,0xa02d16ae),
              (0xa005b860,0xa005b912),(0xa003e10e,0xa003e2b4),
              (0xa0321b2e,0xa0321bde),(0xa0321cbc,0xa0321d02),
              (0xa0315ae6,0xa0315b0e),(0xa0315a10,0xa0315a1c),
              (0xa03204ac,0xa03204c2),(0xa03211b4,0xa03211f0),
              (0xa050caf8,0xa050cc88),(0x208661b0,0x20866274)]
    code = [dict(start=hex(x),end_exclusive=hex(y),sha256=sha(image[x&0x3ffffff:y&0x3ffffff])) for x,y in ranges]
    row = dict(name=name,command=f'udws "{name};"',arguments=[],handler=inv[name]['handler'],
               descriptor_address=inv[name]['descriptor_address'],
               classification='reviewed-bounded-cached-consumable-system-query',
               purpose='Read configured cartridge/channel capabilities and currently usable channels with cached overall flags.',
               review_source='references/round7_product_review.json',review_evidence=proof,
               prerequisites=[a,b,c,d],cached_status_prerequisite=status_evidence,
               side_effects_assessment='Caller output,15-byte serial-number scratch RAM, parser state and paired semaphore/lock bookkeeping. No persistent setting write, mechanical operation or external request on the branch selected by physical79.',
               result_limitations='Readiness and usable-channel flags reflect cached firmware state. Raw scalar semantics are retained by structure offset where no source name is established. The review assumes the observed PEN cache guard remains unchanged between prerequisite79 and execution; do not generalize to hotplug/transaction states.',
               timeout=30,prior_invocation_status='not-invoked',code_evidence=code)
    report = dict(schema_version=1,kind='offline-review-not-physical-results',serial_access='none',
                  image='artifacts/ghs/dram.bin',image_sha256=sha(image),queries=[row],
                  summary=dict(invocations=1,new_names=1),
                  selected_ink_path=dict(state_words=words,flag_bytes=flags,uses_simple_cached_value=guard),
                  held=[dict(name='constat.is_fake_mode',reason='A0040DE0 loads2089939C, no-opA00415BC returns it unchanged, then dereferences. Physical69 proves this pointerNULL. Not a safe alternative getter on the observed state.'),
                        dict(name='constat.get_system_info in a different PEN cache state',reason='The alternate lazy ink path includesA0315A62 -> A0316076, which can process pending state. Not approved by this state-specific review.')])
    (REF/'round7_product_review.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    (REF/'76-product-commands.json').write_text(json.dumps([row],indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report['summary']))


if __name__ == '__main__':
    main()
