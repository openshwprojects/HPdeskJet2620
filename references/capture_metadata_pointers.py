"""Read named LED interfaces and firmware metadata through an owned RAM file."""
import json
from pathlib import Path
from capture_uart import Capture
from capture_nvm_mirror import payload, validate


def main():
    path = '/ram/codex_metadata_r5.bin'
    capture = Capture('references/physical-59-metadata-pointers', 'COM16', 115200,
                      validator=validate)
    fd = None
    reader = created = responsive = False
    observations = []

    def run(expr, purpose, **expect):
        nonlocal responsive
        item = {'command': f'udws "{expr};"', 'purpose': purpose,
                'classification': 'reviewed-owned-ram-file-operation',
                'review_source': 'ram_filesystem_review.json; A035612A..6162 source buffer -> file write',
                **expect}
        assert len(item['command']) <= 120
        ok = capture.execute(item, len(capture.results) + 1, 20)
        responsive = capture.results[-1]['prompt_returned']
        if not ok:
            raise RuntimeError('Unexpected response: ' + expr)
        return payload((capture.directory / capture.results[-1]['response_file']).read_bytes().decode('latin-1'))

    def read(address, count, label):
        nonlocal fd, reader, created
        assert 0 < count <= 512 and address % 4 == 0
        assert any(start <= address and address + count <= end
                   for start, end in [(0x20000000, 0x24000000), (0xa0000000, 0xa4000000)])
        run(f'ustd.sizeof {path}', 'Require our exact temporary file to be absent.', expected=-1)
        created = True
        fd = int(run(f'ram.creat {path}', 'Create only our temporary file and retain returned descriptor.'))
        if fd < 0:
            fd = None
            raise RuntimeError('File creation failed')
        run(f'ram.write {fd} {hex(address)} {count}',
            f'Copy {count} bytes from known DRAM source {label} into our file; source memory is read-only.', expected=count)
        run(f'ram.close {fd}', 'Close our writer.', expected=0)
        fd = None
        reader = True
        run(f'ustd.open_file {path}', 'Open our snapshot file read-only.', expected=count)
        data = bytes.fromhex(run(f'ustd.read_file 0 {count}', 'Retrieve the exact snapshot bytes.', hex_bytes=count))
        source = capture.results[-1]
        observations.append({'label': label, 'address': hex(address), 'count': count,
            'hex': data.hex(), 'words_be': [hex(int.from_bytes(data[i:i+4], 'big')) for i in range(0,count,4)],
            'response_file': source['response_file'], 'response_sha256': source['response_sha256']})
        run('ustd.close_file', 'Close only our debug reader.', expected='')
        reader = False
        run(f'ram.unlink {path}', 'Remove only our temporary snapshot file.', expected=0)
        created = False
        run(f'ustd.sizeof {path}', 'Verify removal.', expected=-1)
        return data

    try:
        capture.open()
        responsive = capture.synchronize()
        if not responsive:
            raise RuntimeError('No initial prompt')
        for label, address in [(f'led_interface_{i}', 0x20ab0148 + 4*i) for i in range(5)] + [('led_output', 0x2083e248)]:
            pointer = int.from_bytes(read(address, 4, label), 'big')
            if pointer and pointer != 0xffffffff:
                vtable = int.from_bytes(read(pointer, 4, label + '_object_vtable'), 'big')
                if vtable and vtable != 0xffffffff:
                    read(vtable, 64, label + '_vtable_methods')
        read(0x2089e40c, 4, 'rng_initialized_flag')
        read(0x2091f7a4, 420, 'firm_string_registry_21_rows')
        read(0x20004158, 24, 'assert_ring_metadata')
        capture.metadata['stop_reason'] = 'batch-complete'
    except Exception as error:
        capture.metadata['stop_reason'] = str(error)
        raise
    finally:
        try:
            if responsive and fd is not None:
                run(f'ram.close {fd}', 'Failure cleanup of our writer.', expected=0)
                fd = None
            if responsive and reader:
                run('ustd.close_file', 'Failure cleanup of our reader.', expected='')
                reader = False
            if responsive and created:
                run(f'ram.unlink {path}', 'Failure cleanup of our temporary file.', expected=0)
                created = False
        finally:
            report = {'observations': observations, 'cleanup_complete': fd is None and not reader and not created,
                      'source_memory_written': False, 'scope': 'Named LED interfaces/vtables, RNG initialization flag, 21 string registry rows, and assertion-ring metadata only.'}
            (capture.directory / 'provider-pointers.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
            capture.close()


if __name__ == '__main__':
    main()
