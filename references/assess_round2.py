"""Build hash-bound follow-up assessments without changing raw UART captures."""
import hashlib
import json
from pathlib import Path

from build_reference_index import response_body

ROOT = Path(__file__).resolve().parent


def source(session, index):
    folder = ROOT / session
    capture = json.loads((folder / 'session.json').read_text())
    command = next(x for x in capture['commands'] if x['index'] == index)
    path = folder / command['response_file']
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == command['response_sha256']
    return ({'response_file': 'references/' + path.relative_to(ROOT).as_posix(),
             'response_sha256': command['response_sha256']},
            response_body(raw.decode('latin-1'), command['command']))


def main():
    result = {'scope': 'Supplemental assessments of exact physical responses; original captures remain unchanged.',
              'response_revalidations': [], 'data_quality': [], 'supporting_reports': []}

    def assess(session, index, predicate, valid, assessment, note, **extra):
        entry, body = source(session, index)
        assert predicate(body), (session, index, repr(body))
        result['response_revalidations'].append({**entry, 'valid': valid,
                                                 'assessment': assessment, 'note': note, **extra})

    assess('physical-34-ram-file-operations', 16, lambda s: s == '8', True,
           'copy-return-is-byte-count',
           'The initial host validator wrongly expected zero. The physical handler returned eight bytes copied. '
           'Session 35 independently read back both eight-byte files as 01234567.',
           original_expected_value=0, corrected_expected_value=8)
    assess('physical-35-ram-handles-and-cleanup', 27, lambda s: 'result -1' in s, False,
           'directory-removal-rejected',
           'This is an actual operation failure, not a lost response. Session 36 confirmed an empty visible directory; '
           'session 38 native diagnostic returned errno 158. The exact owned directory was removed in session 39.')
    entry, body = source('physical-33-ram-preflight', 1)
    assert 'flush result : OK' in body
    result['data_quality'].append({**entry, 'assessment': 'side-effect-classification-corrected', 'complete': None,
                                  'corrected_classification': 'ram-directory-flush-and-diagnostic',
                                  'note': 'The original plan called ram.ls a listing query. Handler disassembly shows '
                                  'directory ioctl 24 can reclaim deleted RAM file records. Its actual reply says '
                                  'flush result : OK. This side effect is retained explicitly; it is not a passive listing.'})
    assess('physical-38-ram-upload-cleanup', 1, lambda s: s == '16', True,
           'inline-upload-return-count', 'ram.file_write2 consumed all sixteen inline payload bytes.')
    assess('physical-38-ram-upload-cleanup', 2, lambda s: s == '0123456789ABCDEF', True,
           'inline-upload-exact-readback', 'The physical file contains the exact sixteen uploaded ASCII bytes.')
    assess('physical-38-ram-upload-cleanup', 3, lambda s: s == '16', True,
           'inline-upload-size', 'The physical file size is sixteen bytes.')
    assess('physical-38-ram-upload-cleanup', 5, lambda s: 'ERROR (158)' in s, True,
           'guarded-cleanup-error-observed',
           'The native guarded directory-removal diagnostic returned errno 158. Its firmware format string '
           'incorrectly labels the operation ram_drv_mkdir; the dispatched handler is ram.rmdir.')
    assess('physical-39-ram-cleanup', 1, lambda s: 'result OK (0)' in s, True,
           'owned-empty-directory-removed',
           'Flag 1 bypassed stale counters only for the newly created, verified empty test directory. '
           'No recursive removal or removal of pre-existing printer data was performed.')
    assess('physical-39-ram-cleanup', 2,
           lambda s: 'total files in /ram : 8' in s and 'codex_uart_20261029a' not in s,
           True, 'temporary-directory-absence-verified',
           'The final physical root listing contains the same eight named directories as preflight and no test directory.')
    assess('physical-41-ram-hex-upload', 1, lambda s: s == '16', True,
           'hex-upload-consumed-character-count',
           'ram.file_write returns sixteen input hexadecimal characters consumed, representing eight output bytes.')
    assess('physical-41-ram-hex-upload', 2, lambda s: s == '01234567', True,
           'hex-upload-exact-readback', 'The physical file contains the eight decoded ASCII bytes.')
    assess('physical-41-ram-hex-upload', 3, lambda s: s == '8', True,
           'hex-upload-size', 'The physical file size is eight bytes.')
    assess('physical-41-ram-hex-upload', 6, lambda s: s == '-1', True,
           'hex-upload-scratch-file-removed',
           'The fixed /ram/file_write.tmp scratch path, absent before the test, is absent after the firmware handler returns.')
    assess('physical-41-ram-hex-upload', 8,
           lambda s: 'total files in /ram : 8' in s and 'codex_hex_test' not in s and 'file_write.tmp' not in s,
           True, 'hex-upload-cleanup-verified', 'The final physical root listing contains only its original eight entries.')
    rom_path = ROOT.parent / 'dump.bin'
    rom = rom_path.read_bytes()
    version_bytes = rom[0x2040:0x2050]
    for offset, width in enumerate([1, 2, 4]):
        expected = sum(int.from_bytes(version_bytes[i:i+width], 'big')
                       for i in range(0, len(version_bytes), width)) & 0xffffffff
        for index in [16+offset, 19+offset]:
            assess('physical-37-bounded-software', index,
                   lambda s, expected=expected: int(s, 16) == expected, True,
                   'physical-rom-sum-matches-original-dump',
                   'Sum of the same sixteen base-firmware-version bytes, using big-endian units of the indicated width.',
                   unit_width=width, expected_sum=f'0x{expected:08x}',
                   dump_offset=0x2040, byte_count=16,
                   dump_sha256=hashlib.sha256(rom).hexdigest())
    assess('physical-37-bounded-software', 4, lambda s: 'FAILED(0)' in s, False,
           'clock-self-test-rejected-unsupported-clock-id',
           'The wrapper received clock ID 1, which this firmware clock_gettime helper does not support. '
           'It rejects the call before the timing test. This response does not establish a broken clock; '
           'see posix_clock_test_review.json for the supported ID and subsequent test.')
    assess('physical-44-supported-clock-test', 1, lambda s: 'PASSED(1)' in s, True,
           'supported-clock-id-passes-built-in-test',
           'The same built-in test passes with supported clock ID 3. Its weak compiled timing criteria '
           'do not independently establish clock accuracy or a positive elapsed-time lower bound.')
    for index in range(39, 43):
        assess('physical-37-bounded-software', index, lambda s: 'invalid firm id' in s, False,
               'simple-firmware-getter-rejected-identifiers',
               'These supplied SUID/item arguments were rejected. The diagnostic packs them into a printed identifier, '
               'but that formatting alone does not establish the lookup argument encoding. The same groups and items '
               'are accepted by get_by_name and query_value_suid. get_simple requires a different simple-table '
               'representation at registry entry +4; named numeric registration sets that field to zero. '
               'The original SUID/item syntax was correct; repacking the arguments would not fix this representation mismatch.')
    for filename in ['hardware_round2_results.json', 'software_round2_results.json']:
        path = ROOT / filename
        if not path.exists():
            continue
        raw = path.read_bytes()
        report = json.loads(raw)
        result['supporting_reports'].append({'file': filename, 'sha256': hashlib.sha256(raw).hexdigest()})
        for key in ['response_revalidations', 'data_quality']:
            result[key].extend(report.get(key, []))
    for key in ['response_revalidations', 'data_quality']:
        paths = [x['response_file'] for x in result[key]]
        assert len(paths) == len(set(paths)), (key, 'duplicate annotation')
    (ROOT / 'physical-round2-assessments.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({key: len(result[key]) for key in ['response_revalidations', 'data_quality']}))


if __name__ == '__main__':
    main()
