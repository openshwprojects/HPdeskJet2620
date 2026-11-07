"""Correct two historical review claims using raw UART evidence and disassembly."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import re

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT/'references'
DATA = (ROOT/'artifacts/ghs/dram.bin').read_bytes()

def sha(data):
    return hashlib.sha256(data).hexdigest()

def read_record(directory, name):
    session = json.loads((directory/'session.json').read_text())
    row = next(r for r in session['commands'] if r.get('name') == name)
    path = directory/row['response_file']
    raw = path.read_bytes()
    assert sha(raw) == row['response_sha256'] and len(raw) == row['response_bytes']
    assert row['prompt_returned'] and row['status'] == 'prompt-returned'
    text = raw.decode('ascii').replace('\r','').strip()
    prefix = '-> '+row['command']
    assert text.startswith(prefix)
    rest = text[len(prefix):].strip()
    assert re.search(r';?udws\(\) returns 0 \(0x0\)\s*->$', rest)
    payload = re.sub(r';?udws\(\) returns 0 \(0x0\)\s*->$', '', rest).strip()
    return row, payload, dict(name=name, command=row['command'],
        response_file=path.relative_to(ROOT).as_posix(), response_sha256=sha(raw),
        response_bytes=len(raw), prompt_returned=True, wrapper_return=0)

def evidence(a,b):
    return dict(start=f'0x{a:08x}', end_exclusive=f'0x{b:08x}',
                sha256=sha(DATA[a&0x3ffffff:b&0x3ffffff]))

def main():
    results=[]
    validations=[]
    quality=[]
    cap=REF/'physical-63-misc-queries'
    _,payload,rng=read_record(cap,'crypto_svc.get_rand_num')
    assert re.fullmatch(r'---\s*writing to file \[\]\s*---\s*just wrote 16 bytes to',payload)
    rng.update(valid=True, complete=False, assessment='unexpected-empty-filename-file-branch',
        note='Historical proof was false: the inner semicolon is parsed as an empty second string, scanf returns2, and the handler attempts unlink/open of the empty pathname. No random bytes were captured. The write message is unconditional after open and cannot establish successful creation or a16-byte write.',
        parser_format='%d %79s', parser_assignments=2, parsed_filename='', random_bytes_captured=0,
        attempted_file_operations=['unlink("")','open("",0x242,0x1f8)'],
        conditional_file_operations='write(fd,2089E424,16) and close(fd) only if open returns nonnegative',
        actual_file_mutation_status='Not established by original UART response; requires mount-prefix evidence.',
        persistent_writes_original_claim='Unsupported for the executed branch; original metadata is preserved and superseded by this annotation.',
        corrected_command='udws "crypto_svc.get_rand_num 16"',
        corrected_path_proof='A03D0224 input returns EOF at quoted-string NUL, without appending a semicolon. A02C3BFC..3C04 takes EOF before second conversion; A02C3C90..3D62 returns assignment count1. A0044A70 skips file flag initialization, A0044ABE takes UART path A0044AFC..4B1C, emitting16 bytes via %02x.',
        code_evidence=[evidence(0xa0044a44,0xa0044b20),evidence(0xa02c3bc0,0xa02c3c9c),evidence(0xa02c3d60,0xa02c3dae),evidence(0xa03d0224,0xa03d023c),evidence(0xa046ad60,0xa046adc4),evidence(0xa046b74a,0xa046b7fa),evidence(0xa046beca,0xa046bf10)])
    mountcap=REF/'physical-67-mount-metadata'
    mountfile=mountcap/'provider-pointers.json'
    mounts=json.loads(mountfile.read_text())
    assert mounts['cleanup_complete'] and mounts['source_memory_written'] is False
    mountsession=json.loads((mountcap/'session.json').read_text())
    mountrows={r['response_file']:r for r in mountsession['commands']}
    assert mountsession['stop_reason']=='batch-complete'
    mountdata=b''
    for index,observation in enumerate(mounts['observations']):
        assert int(observation['address'],16)==0x20dfe054+index*448
        assert observation['count']==448
        raw=(mountcap/observation['response_file']).read_bytes()
        record=mountrows[observation['response_file']]
        assert sha(raw)==observation['response_sha256']==record['response_sha256']
        assert len(raw)==record['response_bytes'] and record['prompt_returned']
        snapshot=bytes.fromhex(observation['hex'])
        assert len(snapshot)==448 and observation['hex'] in raw.decode('ascii')
        mountdata+=snapshot
    assert len(mountdata)==896
    active=[]
    for index in range(32):
        record=mountdata[index*28:(index+1)*28]
        if int.from_bytes(record[20:24],'big')!=1:
            continue
        pointer=int.from_bytes(record[4:8],'big')
        assert 0xa0000000<=pointer<0xa4000000
        prefix=DATA[pointer&0x3ffffff:(pointer&0x3ffffff)+288].split(b'\0',1)[0]
        assert prefix and prefix.startswith(b'/')
        active.append(dict(slot=index,prefix_pointer=f'0x{pointer:08x}',prefix=prefix.decode('ascii')))
    assert len(active)==12
    rng.update(actual_file_mutation_status='The captured32-entry mount table has12 active nonempty ROM prefixes and no empty-prefix mount. A046AD60 returns NULL/errno14 for empty paths under this configuration. Both unlink and open return-1 without dispatching a backend; the guarded write/close are skipped.',
               persistent_writes_original_claim='Original no-file-branch proof was false. Follow-up mount evidence supports no file mutation despite attempted unlink/open; original metadata is retained unchanged.',
               actual_file_mutation_supported=False,
               mount_evidence_timing='Mount table was captured after the original call, not atomically during it; the conclusion assumes no unobserved mount reconfiguration between those captures.',
               mount_evidence_file=mountfile.relative_to(ROOT).as_posix(),
               mount_evidence_sha256=sha(mountfile.read_bytes()))
    rng['note']+=' Follow-up mount snapshot has no empty prefix: empty-path calls cannot reach a filesystem backend under the captured configuration, so no actual file mutation is supported. Snapshot timing is retained as a limitation.'
    results.append(rng)
    _,payload,nvm=read_record(cap,'nos_nvm_sirius.get')
    assert payload=='-1'
    entry=DATA[0x84801c:0x848034]
    assert entry.hex()=='000000120000000c00000000200000000000001200000000'
    assert DATA[0x11984:0x1198c].hex()=='0c140401a00119b4'
    assert DATA[0x119b4:0x119b8] == bytes(4)
    nvm.update(valid=True,complete=True,assessment='complete-signed-full-width-nos-nvm-scalar',
        note='The payload -1 is signed formatting of the full32-bit data0xffffffff. It is not dispatch failure: A02EC152..162 uses %d%c only when lookup/get returns0; dispatch error emits a semicolon alone. Entry18 maps to item12, full-width32 mask, shift0, DSID0, and the special path calls A000CB8C(12). Default bytes are00000000, so this output is not that default. Lower NVM validation status is discarded by A000CB8C; no claim of independent EEPROM integrity follows.',
        signed_value=-1,unsigned_value=0xffffffff,hex_value='ffffffff',dispatch_status=0,
        mapped_item=12,dsid=0,bit_width=32,bit_shift=0,rom_default='00000000',
        persistent_writes=False,
        code_evidence=[evidence(0xa02ebfae,0xa02ebfca),evidence(0xa02ec032,0xa02ec07a),evidence(0xa02ec11c,0xa02ec16c),evidence(0x2084801c,0x20848034),evidence(0xa000cb8c,0xa000cbd6),evidence(0xa0011984,0xa001198c)])
    results.append(nvm)
    correctedcap=REF/'physical-66-rng-uart'
    _,payload,corrected=read_record(correctedcap,'crypto_svc.get_rand_num')
    assert corrected['command']=='udws "crypto_svc.get_rand_num 16"'
    assert re.fullmatch('[0-9a-f]{32}',payload)
    corrected.update(valid=True,complete=True,assessment='complete-random-byte-sample-uart-path',
        note='Exact EOF syntax without an inner semicolon selected the reviewed UART branch. Exactly32 lowercase hexadecimal characters encode16 bytes; no file-output message appeared. This validates output format and execution, not randomness quality.',
        sample_hex=payload,sample_bytes=len(bytes.fromhex(payload)),
        persistent_writes=False,side_effects='Volatile processed PRNG state/counter and mutex bookkeeping advance.',
        corrected_original_response_file=rng['response_file'],
        source_session_sha256=sha((correctedcap/'session.json').read_bytes()))
    results.append(corrected)
    for obj in results:
        common={key:obj[key] for key in ['response_file','response_sha256','assessment','note']}
        validations.append(dict(common,valid=obj['valid'],complete=obj['complete']))
        quality.append(dict(common,complete=obj['complete']))
    report=dict(schema_version=1,generated_utc=datetime.now(timezone.utc).isoformat(),
        provenance=dict(serial_access_by_reviewer='none',image_sha256=sha(DATA),
                        source_session='references/physical-63-misc-queries/session.json',
                        source_session_sha256=sha((cap/'session.json').read_bytes())),
        summary=dict(original_responses_reviewed=2,corrected_responses_reviewed=1,complete_data_responses=2,
                     corrected_random_sample_bytes=16,active_mounts=12,empty_prefix_mounts=0,
                     random_byte_capture_in_original_response=False,nvm_value_is_dispatch_error=False),
        mount_evidence=dict(file=mountfile.relative_to(ROOT).as_posix(),sha256=sha(mountfile.read_bytes()),
                            session_sha256=sha((mountcap/'session.json').read_bytes()),
                            records=32,active_prefixes=active,observations=mounts['observations']),
        corrections=['The original filename-omission proof ignored custom scanf semantics at a semicolon delimiter.',
                     'The RNG handler prints its16-byte write claim even after a failed open.',
                     'A negative signed NVM scalar is not the function return status.'],
        response_revalidations=validations,data_quality=quality,results=results)
    (REF/'round5_misc_followup_results.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report['summary']))

if __name__=='__main__':
    main()
