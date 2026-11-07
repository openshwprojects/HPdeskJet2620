"""Record the exact reviewed miscellaneous queries for physical round five."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / 'references'
image = (ROOT / 'artifacts/ghs/dram.bin').read_bytes()
inventory = json.loads((REF / 'static_inventory.json').read_text())
descriptors = {r['name']: r for r in inventory['commands'] + inventory['hierarchical_commands_with_static_registration']}
coverage = {r['name']: r for r in json.loads((REF / 'coverage.json').read_text())['commands']}
evidence_path = REF / 'physical-59-metadata-pointers/provider-pointers.json'
evidence = json.loads(evidence_path.read_text())
observations = {r['label']: r for r in evidence['observations']}
for label in ['rng_initialized_flag', 'assert_ring_metadata']:
    r = observations[label]
    assert hashlib.sha256((evidence_path.parent / r['response_file']).read_bytes()).hexdigest() == r['response_sha256']
assert observations['rng_initialized_flag']['hex'] == '00000001'
assert bytes.fromhex(observations['assert_ring_metadata']['hex'])[4] == 0
rows = []


def add(name, arguments, purpose, proof, interpretation, **extra):
    d = descriptors[name]
    rows.append(dict(name=name, command=f'udws "{name}{(" " + arguments) if arguments else ""};"',
        handler=d['handler'], descriptor_address=d['descriptor_address'],
        classification='reviewed-information-query', argument_syntax=arguments,
        purpose=purpose, control_flow_proof=proof, interpretation=interpretation,
        timeout=25, persistent_writes=False,
        prior_invocation_status=coverage[name]['status'], runtime_registered=True,
        image_sha256=hashlib.sha256(image).hexdigest(),
        review_source='references/round5_misc_review_corrected.json', **extra))


add('undercroft.get_rev', '', 'Read cached undercroft revision nibble.',
    'A0469D0C calls A046A180, which reads the high nibble of byte20DFDE39 and returns; then formats the integer. No hardware call or mutation.',
    'Cached component revision, not identification of a physical chip.', hardware_access=False)
add('crypto_svc.is_secure', '', 'Read the firmware security-mode predicate.',
    'A00449D4 calls A00089DE -> A00089A0(0). A0008924 reads cached20004894; if uninitialized A0008914 populates its two volatile words using NVM item1 via A000CB8C. A0008938 checks boot RAM fields and hashes16bytes from2000489C with A0007B8A/A0007BAC/A0007C1C, compares32bytes and returns a boolean. NVM item1 exists in A0011924; A000CB8C/A000C9F6 only decode defaults or the RAM NVM mirror. No flash/EEPROM write or security-setting operation.',
    'An internal firmware predicate only; does not establish secure boot enforcement or overall device security.', hardware_access=False)
random_proof = ('Physical59 confirms initialized flag2089E40C=1. Processed mode0 calls A00448B6 -> A0043564 -> A0044688 -> A004460C, holding the crypto mutex. A004460C reads free-running timerBD040814, hashes32-byte PRNG state2089E3EC plus timer/counter using the software SHA helpers, and advances counter2089E3E8/state. The raw AASIC entropy path is not selected.')
add('crypto_svc.get_rand_num', '16', 'Capture one sixteen-byte processed random sample.',
    'A0044D44 calls A0044A44(mode0); exact length16 fits the cleared16-byte buffer2089E424. ' + random_proof,
    'Sample format and execution coverage only; no randomness or security-quality claim.',
    hardware_access='Read-only free-running timer', side_effects='Volatile PRNG state advances; mutex bookkeeping and UART output.')
rows[-1]['command'] = 'udws "crypto_svc.get_rand_num 16"'
rows[-1]['control_flow_proof'] += (' Critical syntax: omit the semicolon inside the quoted UDW string. The wrapper input callback A03D0224 returns EOF at the string NUL without appending punctuation. Custom scanf A02C39F4 using format %d %79s reaches EOF before the second assignment and returns1. A0044A70 skips file flag setup; A0044ABE branches to bytewise UART formatting A0044AFC..A0044B1C. A trailing semicolon instead yields an empty second string and assignment count2, selecting unlink/open(file) code. Historical capture63 demonstrated that parser edge case; its success-looking write message is unconditional and is not proof a file was written.')
add('crypto_svc.get_rand_seq', '4', 'Capture four processed random words.',
    'A0044A02 parses bounded exact count4 and calls A0044890 for one4-byte sample per iteration. ' + random_proof,
    'Four samples, not a statistical assessment.',
    hardware_access='Read-only free-running timer', side_effects='Volatile PRNG state advances; mutex bookkeeping and UART output.')
for serial, year in [('AA711', 2017), ('AA7CV', 2017), ('AA811', 2018)]:
    add('mfg_info.calc_mfg_date', f'{serial} {year}', 'Exercise serial-code date decoding with explicit synthetic fixtures.',
        'A02D15C0 parses %15s %d into bounded volatile parser buffer and local year, validates length>4 and encoded year/month/day characters, then calls A02D1460 and A02D156C. Explicit year2017/2018 bypasses firmware-version year inference. Remaining code performs bounded character lookup and integer date arithmetic, then UART formatting; no hardware or NVM writes.',
        'Synthetic input: the returned date is not this printer manufacture date.', hardware_access=False)
add('nos_nvm_sirius.get', '18', 'Read mapped NOS NVM item12 through the Sirius query interface.',
    'A02EC134 parses ID18 and calls A02EC032. Lookup A02EBFAE finds entry18 in19-entry table20847EB4: item12, DSID0. Special ID18 branch calls A000CB8C(12). ROM NVM descriptor A0011924 contains item12: 0C140401 A00119B4 (4bytes). A000CB8C resolves partition metadata via A0007696 and reads default bytes or mirror20004B5C through A000C9F6, validates cached copies/checksum, and formats the scalar. No NVM-write helper A000C8AA is called.',
    'Item12 scalar; semantic meaning remains unassigned. This is not raw EEPROM offset12. Observed -1 is the signed format of0xffffffff: A02EC152..162 prints the scalar only on dispatch status0; failure prints just a semicolon. The lower NVM reader does not expose its validation status through this command, so this is not independent proof of EEPROM integrity.', hardware_access=False)
add('errlog.get_assert_entry', '0', 'Decode the most recent retained assertion-ring entry.',
    'A00C2DD8 zeros caller storage and parses index0, calls A0007460 -> A00073F6 -> A0006F3C. Physical59 ring metadata20004158 has count5,index1,recordsize14,base92, and crucial dirty/reset flag byte4=0; therefore A00073C6 reset branch is not taken. Valid index0 reads bounded record106..119 through A000C888, then decodes only local packed fields and emits strings. Captured flag is the precondition for this exact invocation.',
    'Historical retained assertion, not evidence of a new error caused by this command.', hardware_access='NVM read', precondition_evidence='references/physical-59-metadata-pointers/provider-pointers.json')

review = dict(image_sha256=hashlib.sha256(image).hexdigest(),
    historical_correction='The original63 plan incorrectly assumed an omitted filename bypassed file output even with an inner semicolon. Preserve63-misc-commands.json, round5_misc_review.json and physical63 as historical records. This generator now writes distinct corrected artifacts and uses exact EOF syntax for the RNG byte query.',
    evidence_path=str(evidence_path.relative_to(ROOT)).replace('\\', '/'),
    evidence_sha256=hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
    new_registered_names=len({r['name'] for r in rows if r['prior_invocation_status']=='not-invoked'}),
    queries=rows,
    held=[{'name':'crypto_svc.get_rand_num_raw', 'reason':'Raw AASIC entropy path not admitted.'},
          {'name':'firm.get_str', 'reason':'Valid live registered string SUID has not been established; no guessed lookup.'}])
(REF / 'round5_misc_review_corrected.json').write_text(json.dumps(review, indent=2), encoding='utf-8')
(REF / '63-misc-commands-corrected.json').write_text(json.dumps(rows, indent=2), encoding='utf-8')
(REF / '66-rng-corrected-commands.json').write_text(json.dumps([r for r in rows if r['name']=='crypto_svc.get_rand_num'], indent=2), encoding='utf-8')
print(json.dumps({'invocations':len(rows), 'new_names':review['new_registered_names']}))
