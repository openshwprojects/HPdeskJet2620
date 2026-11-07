"""Freeze the reviewed consumable metadata queries. Offline; no serial access."""
from pathlib import Path
import hashlib
import json
import re

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / 'references'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def main():
    image = (ROOT / 'artifacts/ghs/dram.bin').read_bytes()
    inventory = {x['name']: x for x in json.loads((REF / 'static_inventory.json').read_text())['commands']}
    coverage = {x['name']: x for x in json.loads((REF / 'coverage.json').read_text())['commands']}
    cap = REF / 'physical-69-provider-prerequisites'
    evidence_raw = (cap / 'provider-pointers.json').read_bytes()
    evidence = json.loads(evidence_raw)
    live = {}
    bindings = []
    for obs in evidence['observations']:
        if not obs['label'].startswith('constat_'):
            continue
        path = cap / obs['response_file']
        raw = path.read_bytes()
        assert sha(raw) == obs['response_sha256']
        matches = re.findall(rb'\r\n([0-9a-f]{128});udws\(\) returns 0 \(0x0\)', raw)
        assert len(matches) == 1 and matches[0].decode() == obs['hex']
        data = bytes.fromhex(obs['hex'])
        assert len(data) == obs['count'] == 64
        address = int(obs['address'], 16)
        live.update({address + i: b for i, b in enumerate(data)})
        bindings.append(dict(label=obs['label'], address=obs['address'], count=64,
                             response_file=path.relative_to(ROOT).as_posix(),
                             response_sha256=sha(raw), response_bytes=len(raw), payload_hex=obs['hex']))
    u32 = lambda addr: int.from_bytes(bytes(live[addr+i] for i in range(4)), 'big')
    assert u32(0x20899398) == 0x20896e10
    assert u32(0x2089939c) == 0
    assert u32(0x20896e10) == 0xa050fe84
    assert live[0x20896e1c] == 1
    assert u32(0x20896df0) == 0xa05de248
    assert u32(0x20896dec) == 0xa0517544
    assert evidence['cleanup_complete'] and not evidence['source_memory_written']

    proof = [
        'A0040918 reads one string into a 20-byte caller buffer. A003B90A searches the 14-entry table A050CCA4; BLACK maps to ID108 and CMY to109. Only these two valid IDs are selected.',
        'A050C790 vtable method+4 is A003AB82. Its selector A003DEFE uses normal object20896E10 when secondary selector2089939C isNULL. Physical69 proves exactly this branch, normal vtableA050FE84, initialized flag+0C=1, DS2A0517544 and PEN objectA05DE248.',
        'Normal vtable+14C is A003E2CC. A003A2BC initializes the full64-byte caller output through template20866274 and fills five channel slots with-1. A003DF9C/A003DFB6 accept exactly IDs108/109 and map BLACK to chute1, CMY to chute0.',
        'A003E2CC stores constants1 at output+10/+14, calls presence predicateA003DFD8 for output+0C and stores chute at+18. A003DFD8 calls PEN vtable method0 and computes 1-((status>>1)&1). This is cached firmware presence status, not a new electrical cartridge probe.',
        'PEN interface SUID11422 maps to provider slot68. A03193DA resolves slot68 through A05D3568 to objectA05DE248, vtableA05DE24C; method0 isA0321B2E. The live connection20896DF0 matches this object.',
        'A0321B2E acquires/releases paired PEN locks and reads A03204AC(chute,0x14). That helper reads the fixed DSID table A05D7084 at4*(2*0x14+chute), then A03211B4 retrieves the cache value or DS2 vtable+8. A03211B4 has no setter call; the separate setter startsA03211F0. Cache population bounds its record count to400 atA0321220..A0321236.',
        'Lock wrappers A0322AA4/A0322AC4 -> A0314846/A0314864 update volatile owner/depth bookkeeping around A0308208/A0308218. Those acquire/release semaphore20CE7F7C through A02F5FA2/A02F5F56. No writeback, calibration, transport or mechanical routine occurs on this reviewed path.',
        'A003B64C writes channel count1 and BLACK channel0 for ID108; count3 and channels1/2/3 for ID109, with a-1 terminator. Handler loops only this constructed count, prints status label, consumable label, four scalars, count and channel labels. A003B576/A003B5CC/A003B60A bound the label copies to15/20/15 bytes.'
    ]
    ranges = [(0xa0040918,0xa0040a5e),(0xa003ab82,0xa003aba8),
              (0xa003defe,0xa003df22),(0xa003a2bc,0xa003a302),
              (0xa003e2cc,0xa003e352),(0xa003df9c,0xa003e00e),
              (0xa003b576,0xa003b6b6),(0xa003b90a,0xa003b96c),
              (0xa0321b2e,0xa0321bae),(0xa03204ac,0xa03204c2),
              (0xa03211b4,0xa03211f0),(0xa0322aa4,0xa0322ae8),
              (0xa0314846,0xa0314898),(0xa0308208,0xa0308228),
              (0xa050fe84,0xa0510130),(0xa05de248,0xa05de260)]
    code = [dict(start=hex(a), end_exclusive=hex(b), sha256=sha(image[a&0x3ffffff:b&0x3ffffff])) for a,b in ranges]
    name = 'constat.get_consumable_info'
    desc = inventory[name]
    rows = []
    for token, chute, channels in [('BLACK',1,['BLACK']),('CMY',0,['CYAN','MAGENTA','YELLOW'])]:
        rows.append(dict(name=name, command=f'udws "{name} {token};"',
                         handler=desc['handler'], descriptor_address=desc['descriptor_address'],
                         arguments=[token], purpose=f'Read cached {token} consumable presence, chute mapping and channel metadata.',
                         classification='reviewed-bounded-cached-consumable-query',
                         review_source='references/round6_product_review.json', review_evidence=proof,
                         side_effects_assessment='Caller output and paired volatile lock bookkeeping only on the reviewed valid-ID path. No persistent setting, hardware actuation or external service request.',
                         result_limitations='Reports firmware cached presence and configured channel mapping. Does not measure ink volume or test electrical/mechanical cartridge operation. Generic UART, OS and DS2 getter internals are not a formal transitive proof.',
                         timeout=30, prior_invocation_status=coverage[name]['status'], code_evidence=code,
                         prerequisites=bindings, expected_chute=chute, expected_channels=channels,
                         expected_constant_fields={'output+0x10':1,'output+0x14':1}))
    assert coverage[name]['status'] == 'not-invoked', coverage[name]['status']
    report = dict(schema_version=1, kind='offline-review-not-physical-results',
                  image='artifacts/ghs/dram.bin', image_sha256=sha(image), serial_access='none',
                  method='Concrete Thumb big-endian handler/helper review plus source-bound live provider metadata.',
                  prerequisites=dict(source=(cap/'provider-pointers.json').relative_to(ROOT).as_posix(),
                                     sha256=sha(evidence_raw), validated_sources=bindings,
                                     observation='Normal provider selected; secondary selectorNULL; PEN interface bound to reviewed ROM object.'),
                  queries=rows, summary=dict(invocations=2,new_names=1),
                  deferred=[dict(name='constat.get_system_info', reason='Its extra cached ink-level path needs further nested-helper review. Not included in this five-name batch.')])
    (REF/'round6_product_review.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    (REF/'72-product-commands.json').write_text(json.dumps(rows,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report['summary']))


if __name__ == '__main__':
    main()
