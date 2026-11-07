"""Validate the physical WiFi/SDIO capture. Never accesses serial."""
from pathlib import Path
import hashlib
import json
import re
from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_BIG_ENDIAN

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / 'references'
SESSION = REF / 'physical-48-wifi-sdio'
sha = lambda b: hashlib.sha256(b).hexdigest()
results = []
bodies = {}
sources = {}
for path in sorted(SESSION.glob('[0-9][0-9][0-9][0-9]-*.json')):
    record = json.loads(path.read_text())
    raw_path = SESSION / record['response_file']
    raw = raw_path.read_bytes()
    assert len(raw) == record['response_bytes']
    assert sha(raw) == record['response_sha256']
    assert record['prompt_returned'] and record['status'] == 'prompt-returned'
    text = raw.decode('ascii').replace('\r', '')
    expected_start = '\n-> ' + record['command'] + '\n'
    assert text.startswith(expected_start)
    suffix = '\n;udws() returns 0 (0x0)\n-> '
    assert text.endswith(suffix)
    body = text[len(expected_start):-len(suffix)] + '\n'
    index = record['index']
    bodies[index] = body
    source = {'response_file': raw_path.relative_to(ROOT).as_posix(), 'response_sha256': sha(raw)}
    sources[index] = source
    results.append(dict(source, command=record['command'], valid=True,
                        assessment='exact-command-echo-and-response-body-validated',
                        preserves_original_capture_validation=True))
assert len(results) == 8

counter_keys = ('master', 'overcurrent', 'autocmd12', 'command_index', 'command_endbit',
                'command_crc', 'command_timeout', 'data_endbit', 'data_crc', 'data_timeout')
counter_re = (r'SD MISC Counters: Master: (\d+), OverCurrent: (\d+), Autocmd12: (\d+)\n'
              r'SD CMD Counters: Index: (\d+), EndBit: (\d+), CRC: (\d+), Timeout: (\d+)\n'
              r'SD DAT Counters: EndBit: (\d+), CRC: (\d+), Timeout: (\d+)\n')
counter_values = {}
for index in (1, 4):
    match = re.fullmatch(counter_re, bodies[index])
    assert match, repr(bodies[index])
    counter_values[index] = dict(zip(counter_keys, map(int, match.groups())))
delta = {key: counter_values[4][key] - counter_values[1][key] for key in counter_keys}
assert counter_values[1] == counter_values[4]

caps = bodies[2]
identity = re.search(r'SDIO Card Vendor: 0x([0-9A-F]+), Product: 0x([0-9A-F]+)', caps)
assert identity
identity = {'vendor_id': '0x' + identity[1], 'product_id': '0x' + identity[2]}
def number(s):
    return int(s, 16) if s.startswith('0x') else int(s)
cccr = {k: number(v) for k, v in re.findall(r'cccr\.(\w+) = (0x[0-9A-F]+|\d+)', caps)}
assert len(cccr) == 10
fbr = {}
for function, field, value in re.findall(r'fbr\[(\d+)\]\.(\w+) = (0x[0-9A-F]+|\d+)', caps):
    function = int(function)
    assert field not in fbr.setdefault(function, {})
    fbr[function][field] = number(value)
assert set(fbr) == {1, 2} and all(len(x) == 7 for x in fbr.values())
size = {int(i): int(v) for i, v in re.findall(r'blkSizeMax\[(\d+)\] = (\d+)', caps)}
count = {int(i): int(v) for i, v in re.findall(r'blkCountMax\[(\d+)\] = (\d+)', caps)}
assert set(size) == set(count) == {0, 1, 2}
assert 'Num Funcs (in addition to F0): 2\n' in caps
assert '..Block Mode Support: Y\n' in caps

assert bodies[3] == 'SDIO status - Display device CCCR info:\n'
image = (ROOT/'artifacts/ghs/dram.bin').read_bytes()
md = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_BIG_ENDIAN)
helper = image[0x45ee36:0x45eeec]
insns = list(md.disasm(helper, 0xa045ee36))
assert sum(i.size for i in insns) == len(helper)
calls = [{'address': hex(i.address), 'mnemonic': i.mnemonic, 'target': i.op_str}
         for i in insns if i.mnemonic in ('bl', 'blx')]
assert len(calls) == 3 and all(x['target'] == '#0xa04f0b50' for x in calls)
assert insns[-1].mnemonic == 'pop' and 'pc' in insns[-1].op_str

iovar = {}
for index, name in ((5, 'mpc'), (6, 'nmode')):
    match = re.fullmatch(r' (-?\d+)\n', bodies[index])
    assert match
    iovar[name] = int(match[1])

vectors = re.findall(r'u(16|32|64) = 0x([0-9A-Fa-f]+)\n', bodies[7])
expected = [('16', '0102')] * 2 + [('32', '01020304')] * 2 + [('64', '0102030405060708')] * 2
expected += [('16', '0201')] * 2 + [('32', '04030201')] * 2 + [('64', '0807060504030201')] * 2
assert vectors == expected
assert bodies[7] == ''.join('u'+bits+' = 0x'+value+'\n' for bits, value in expected)

match = re.fullmatch(r'base-md :([0-9a-f]{32});\n', bodies[8])
assert match
base_md = match[1]
old_path = REF/'physical-11-software-network/0020-udws_webfwupdate_get_opaque_rev.bin'
old_raw = old_path.read_bytes()
assert base_md.encode() in old_raw

quality = [
    dict(sources[1], complete=True, payload_kind='ten-error-counters', note='These count errors, not ordinary commands/transfers. Master=2 and Data CRC=2; eight other fields are zero.'),
    dict(sources[4], complete=True, payload_kind='ten-error-counters', note='All ten error counters equal the earlier sample. This establishes no newly counted errors; it cannot measure the number of successful SDIO reads.'),
    dict(sources[2], complete=True, payload_kind='cached-capabilities', note='Complete formatter payload: ten CCCR fields, seven FBR fields for each of two functions, and six maximum-size/count fields. FBR1 CIS=0xff0100 and FBR2 cached block size=8192 are preserved as reported; their current-register meaning is unverified. No formatter endian/offset defect was found.'),
    dict(sources[3], complete=True, payload_kind='heading-only', recovered_cccr_bytes=0,
         note='A045EE36..A045EEEA formats into stack buffers but contains no UART/output call. Its only three calls are snprintf A04F0B50. This is expected heading-only output, not captured CCCR contents or evidence of transport loss. Static read path exists, but UART success cannot verify its underlying result.'),
    dict(sources[7], complete=True, payload_kind='compiled-endian-test-vectors', note='All twelve fixed literal vectors match the compiled handler. This is not an exercise of SDIO hardware endian transfers.'),
    dict(sources[8], complete=True, payload_kind='cached-firmware-digest', note='32 hex characters match the base digest in earlier physical opaque-revision output. Optional local secure-configuration base_md entry was not printed. No hash recomputation was performed.')
]
result = {
    'schema_version': 1, 'kind': 'validated-physical-capture-with-offline-interpretation',
    'session': SESSION.relative_to(ROOT).as_posix(), 'validated_responses': len(results),
    'response_revalidations': results, 'data_quality': quality,
    'findings': {
        'sdio_identity': dict(identity, interpretation='Matches the emulator model BCM43143 (0x02d0:0xa887). This identity is observed; the commercial chip-name mapping is already in sdio.py, not freshly verified from an external source.'),
        'cached_cccr': cccr, 'cached_fbr': fbr, 'number_functions_excluding_f0': 2,
        'block_mode_supported': True, 'block_size_max': size, 'block_count_max': count,
        'error_counters_before': counter_values[1], 'error_counters_after': counter_values[4], 'error_counter_delta': delta,
        'integer_iovar_values': iovar,
        'base_firmware_digest': base_md,
        'base_digest_cross_check': {'response_file': old_path.relative_to(ROOT).as_posix(), 'response_sha256': sha(old_raw)},
        'cccr_bytes_recovered_from_status': 0,
    },
    'corrections_to_initial_review': [
        {'source_review': 'references/useful_network_round4_review.json',
         'source_review_sha256': sha((REF/'useful_network_round4_review.json').read_bytes()),
         'names': ['ultra_spectra.get_sdio_status', 'ultra_spectra.get_sdio_counters'],
         'correction': 'Initial review incorrectly predicted that A045EE36 prints bytes and that status reads increment the displayed counters. A045EE36 only formats local buffers; counters are errors only. Preserve original capture/plan metadata and use this source-bound correction.'},
    ],
    'additional_code_evidence': {
        'silent_format_helper': {'start': '0xa045ee36', 'end_exclusive': '0xa045eeec', 'sha256': sha(helper), 'all_calls': calls},
        'fbr_layout': {
            'getter_copy': 'A03C1322 copies 18 cached bytes from driver +0xbc+18*function. A03C6DD6..A03C6E3E copies them into returned output at +0x13+18*function.',
            'formatter': 'A045471C..A0454784 reads identical offsets. CIS pointer is byte[9] | byte[10]<<8 | byte[11]<<16; block size is byte[16] | byte[17]<<8. Both are little-endian as expected.',
            'conclusion': 'No proven formatting bug. Cached FBR values and separately cached maximum block-size capabilities differ. No live-register payload is available to decide whether the cache is stale, corrupted, or represents an unusual earlier state; do not silently repair 8192 to 512.'
        }
    },
    'emulator_use': 'Observed IDs, function count, maximum block sizes and integer GET_VAR replies are useful comparison points. Preserve cached-FBR anomalies separately rather than treating them as direct live CCCR/FBR register reads.',
}
(REF/'useful_network_round4_results.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
print(json.dumps({'validated_responses': len(results), 'data_quality_annotations': len(quality), 'identity': identity, 'iovar': iovar, 'cccr_bytes_recovered': 0}))
