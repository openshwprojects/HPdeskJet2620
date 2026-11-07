"""Offline review artifact builder. Does not import serial or access a device."""
from pathlib import Path
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / 'references'
image = (ROOT / 'artifacts/ghs/dram.bin').read_bytes()
inventory = {x['name']: x for x in json.loads((REF / 'static_inventory.json').read_text())['commands']}

def region(start, end, description):
    raw = image[start & 0x3ffffff:end & 0x3ffffff]
    return {'start': hex(start), 'end_exclusive': hex(end), 'sha256': hashlib.sha256(raw).hexdigest(), 'description': description}

binding = {
    'consumer': 'ultra_spectra binding index 1 stores its SDIO interface at 0x20defe38; module table 0xa0622bfc identifies interface SUID 0x1216c.',
    'provider': 'sdio module request table 0xa06058ec uses the same SUID. Interface getter 0xa03c07b6 returns [0x208584c0] = 0xa0605968; that ROM object points to vtable 0xa060596c.',
    'vtable': {'offset_0x08': '0xa03c080d (request submission)', 'offset_0x14': '0xa03c0883 (copy counters)', 'offset_0x20': '0xa03c08a3 (read/dump function-0 status)'},
    'runtime_limit': 'Binding is resolved from this image; a physical run must still establish that the live module is initialized. Each handler checks A045EA8C before accessing this interface.'
}
assert int.from_bytes(image[0x8584c0:0x8584c4], 'big') == 0xa0605968
assert int.from_bytes(image[0x605968:0x60596c], 'big') == 0xa060596c
for offset, target in ((8, 0xa03c080d), (0x14, 0xa03c0883), (0x20, 0xa03c08a3)):
    assert int.from_bytes(image[0x60596c + offset:0x605970 + offset], 'big') == target

queries = []
def query(name, args, purpose, evidence, effects, ranges, timeout=20, caveat=None):
    entry = inventory[name]
    item = {'name': name, 'command': 'udws "' + name + (' ' + args if args else '') + ';"',
            'arguments': args.split() if args else [], 'handler': entry['handler'],
            'descriptor_address': entry['descriptor_address'],
            'classification': 'reviewed-bounded-diagnostic', 'purpose': purpose,
            'argument_syntax': args if args else 'No arguments are consumed.',
            'review_evidence': evidence, 'side_effects_assessment': effects,
            'code_evidence': [region(*r) for r in ranges],
            'review_source': 'references/useful_network_round4_review.json', 'timeout': timeout,
            'physically_tested_by_this_review': False}
    if caveat:
        item['result_limitations'] = caveat
    queries.append(item)
    return item

counters = query('ultra_spectra.get_sdio_counters', '', 'Read the WiFi SDIO command, transfer, timeout and error counters without clearing them.',
    'A0454F76 checks card availability, calls vtable +0x14 with port 0 and a 40-byte stack buffer, and prints only on interface result 1. Resolved A03C0882 -> A03C67C0 -> A03C129A copies ten words from driver object +0x3c4 into that buffer; no counter reset or card transaction.',
    'Cached RAM reads, stack writes and UART output. Counters remain untouched by the getter.',
    [(0xa0454f76,0xa0454fdc,'handler'), (0xa03c0882,0xa03c08a2,'resolved interface'), (0xa03c67c0,0xa03c67dc,'counter helper'), (0xa03c129a,0xa03c12d6,'ten-word copy')])
caps = query('ultra_spectra.get_sdio_caps', '', 'Read the SDIO manufacturer/device IDs, function capabilities, block sizes, and speed support needed by the emulator.',
    'A0454796 sends request 0x65 into a 0xac-byte stack structure, then request 0x64 into the same request output. Resolved A03C668E queues to A03C69C4; 0x65 selects A03C6D14, which copies cached CCCR/FBR and manufacturer/device fields via A03C130E/A03C1322/A03C12D6; 0x64 selects A03C6A66, which copies cached function count and function sizes via A03C138E/A03C13A0/A03C13A4. Formatting runs only after successful request completion.',
    'Two serialized driver requests, semaphore/queue bookkeeping, cached RAM reads, caller-buffer writes and UART output. These selectors do not issue discovery, configuration, radio connection, reset or nonvolatile writes.',
    [(0xa0454796,0xa04547ce,'first request'), (0xa0454a28,0xa0454b98,'format and second request'), (0xa03c668e,0xa03c67c0,'submission'), (0xa03c69a0,0xa03c69f8,'selector dispatch'), (0xa03c6a66,0xa03c6bda,'selector 100'), (0xa03c6d14,0xa03c6eb2,'selector 101'), (0xa03c12d6,0xa03c1336,'cached capability copies')],
    30, 'The helper waits for request completion internally; host timeout is a capture bound, not a guarantee that every firmware fault is bounded. These fields are cached capability values, not a new card enumeration.')
status = query('ultra_spectra.get_sdio_status', '', 'Read and dump the current 32 bytes at SDIO function 0 offsets 0x00..0x1f.',
    'A04546C4 -> resolved vtable +0x20 A03C08A2 -> A03C6816 -> A03C4BD8. Last helper invokes A03C506C with direction 0, function 0, address 0, length 0x20 and a stack output buffer. A03C52E4 loops over exactly 32 bytes using A03C13DC. A045EE36 prints the successful read as hex.',
    '32 SDIO read transactions and normal controller/request bookkeeping; this will increment the SDIO counters. No write request to card configuration, connection, scan, or persistent settings is selected.',
    [(0xa04546c4,0xa045470c,'handler'), (0xa03c08a2,0xa03c08c0,'resolved interface'), (0xa03c6816,0xa03c682c,'status helper'), (0xa03c4bd8,0xa03c4bfc,'read 32 bytes'), (0xa03c52e4,0xa03c5326,'bounded per-byte read loop'), (0xa03c13dc,0xa03c148e,'CMD52 bit 31 is set only when direction equals 1; supplied direction 0 leaves write bit clear and stores response byte in caller output')],
    30, 'CCCR registers are live, so compare to the cached capability dump without assuming atomicity. Handler success alone is weak: A03C6816 returns 1 after its helper even if that helper prints a read failure; require the 32-byte dump.')

for name in ('mpc', 'nmode'):
    query('ultra_spectra.get_iovar', name, 'Read the WiFi driver integer variable ' + name + '.',
        'A0453F3A consumes one bounded string (100-byte input buffer). A0459322 checks the driver pointer, calls A0459414 with no input payload and 4-byte output, and prints only on success. A0459414 passes ioctl 0x106 through getter A0459568 (vtable +0x0c), while setters use 0x107 through A045955C. The selected variable names occur in this firmware: mpc at A04585B8, nmode at A062303C. The corresponding getter output is interpreted as a little-endian 32-bit integer by A0459656.',
        'A driver GET_VAR request with a variable name only, stack buffers, transaction bookkeeping and UART output. No setter payload or nonvolatile configuration path is selected.',
        [(0xa0453f3a,0xa0453f80,'bounded string argument and successful-only output'), (0xa0459322,0xa0459356,'integer getter and null guard'), (0xa0459414,0xa0459498,'GET_VAR 0x106 request'), (0xa045955c,0xa0459580,'separate set/get virtual paths')],
        30, 'The radio firmware can reject unsupported variables. An empty result with a normal prompt is not a successful value read. Do not use this integer handler for strings such as ver or for secret material.')

query('ultra_spectra.test_endian', '', 'Record the compiled 16/32/64-bit endian conversion test vectors for emulator comparison.',
    'A045520A..A045527E prints twelve literal vectors through A0453EEE and returns; all values are fixed immediates/literals (0x0102, 0x01020304, 0x0102030405060708 and their reversed-byte counterparts). There is no hardware access.',
    'UART output and stack/formatting bookkeeping only.', [(0xa045520a,0xa0455280,'all twelve fixed-vector prints')],
    15, 'These are compiled fixed-vector outputs. They are not evidence that the physical SDIO bus exercised all endian cases.')

query('webfwupdate.get_base_md', '', 'Read the 16-byte base firmware digest and any matching base_md entry in local secure configuration.',
    'A0470EEA reads 16 bytes from the existing base-MD pointer at 20E09780 and formats 32 hex characters via A046ED8A. It then requests section web_product_update/key base_md with a 65-byte output buffer through A03C8882/A03C8696. The secure-configuration helper seeks and reads existing cached file descriptors; it never selects a file write. Missing entry is skipped because caller passed a NULL default.',
    'Local metadata reads and UART output. Secure-config lookup uses its mutex and changes file positions; on bad/stale file reads it can close those cached handles and compact the in-memory descriptor list. No firmware update, download, or nonvolatile write.',
    [(0xa0470eea,0xa0470f32,'digest and optional configuration query'), (0xa046ed8a,0xa046edb8,'bounded hex formatter'), (0xa03c8696,0xa03c8780,'section search and file seek'), (0xa03c8792,0xa03c8882,'bounded read, copy and cached-handle cleanup'), (0xa03c8882,0xa03c88a0,'NULL default on missing entry')],
    30, 'The base digest pointer is initialized by the firmware lifecycle; unlike the formatted optional configuration entry it is not null-checked by this handler. Established running firmware is the precondition. This command does not recompute the digest.')

held = [
    {'names': ['ultra_spectra.get_wifi_ant'], 'reason': 'Read-only ioctl 0x3f is selected, but A0458240 dereferences the WLC driver without a null check and ignores driver return status before converting uninitialized output. A live successful GET_VAR is a useful prerequisite, but the antenna value still needs an error check outside this handler. Not in this batch.'},
    {'names': ['nca.get_wired_state'], 'reason': 'Required numeric adaptor selector is real, but absent hardware can return before setting the stack output flag; this WiFi/USB printer is not a useful target for the unguarded wired formatter.'},
    {'names': ['nca.get_mfg_ssid','nca.get_mfg_wireless_mode','nca.get_mfg_wireless_locale','nca.get_mfg_wireless_5ghz_config'], 'reason': 'Prior cache-refresh hold strengthened: A02DDA8A clears/rebuilds manufacturing cache AND normalizes an NVM enum via setter vtable +0x0c at A02DDB34 when its value exceeds 2. These names are not passive read-only getters.'},
    {'names': ['nca.get_wireless_5ghz_config'], 'reason': 'A02E0140 leaves output length r4 uninitialized if enum is outside 1..4, including 0. Live enum precondition remains unproved.'},
    {'names': ['nca.get_wireless_scan','nca.get_directed_wireless_scan','nca.get_num_aps_max_rssi','wifi_conman.scan','wifi_conman.find','wifi_conman.search'], 'reason': 'Actual scans/discovery; not necessary to obtain the SDIO diagnostics in this batch. No claim these are intrinsically forbidden.'},
    {'names': ['usb.convert_to_test_printer','dot4.convert_to_test_printer','usb_hs.set_cfg_desc_type','usb_hs.set_scan_hw_autonull'], 'reason': 'USB behavior/configuration mutations. Available passive USB configuration queries already captured; no newly proved useful passive USB command remains in this reviewed set.'},
]

report = {'schema_version': 1, 'kind': 'offline-static-review-not-a-physical-log',
    'image': 'artifacts/ghs/dram.bin', 'image_sha256': hashlib.sha256(image).hexdigest(),
    'method': 'Direct Thumb big-endian disassembly of handlers and named backend paths. SDIO virtual dispatch resolved against module tables and the initialized ROM object. UART/framework and general OS helpers are not formally proven transitively.',
    'sdio_binding': binding, 'queries': queries, 'held_for_further_review': held,
    'prior_hold_correction': 'Manufacturing network getters may also normalize an out-of-range NVM field; earlier description as only a RAM cache refresh was incomplete.'}
(REF/'useful_network_round4_review.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
plan = [counters, caps, status, dict(counters, purpose='Read counters after the 32 status-byte reads to measure actual SDIO activity.')] + queries[3:]
for entry in plan:
    entry.pop('code_evidence', None)
(REF/'48-network-commands.json').write_text(json.dumps(plan, indent=2)+'\n', encoding='utf-8')
print(json.dumps({'invocations': len(plan), 'distinct_names': len({x['name'] for x in plan})}))
