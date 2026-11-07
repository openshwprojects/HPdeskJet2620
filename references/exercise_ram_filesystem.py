"""Capture a bounded create/read/write/copy/rename/delete test in printer RAM.

Uses only the dedicated, preflight-checked path. File descriptor arguments come
from the live create/open responses. No device address is used as a write target;
the 16-byte pattern is read from the verified firmware's constant hex alphabet.
"""
import json
import re
from pathlib import Path

from capture_uart import Capture

ROOT = '/ram/codex_uart_20261029a'
A, B, C = (ROOT + '/' + name for name in ('a', 'b', 'c'))


def validate(item, text):
    pattern = item.get('expected_regex')
    ok = pattern is None or re.search(pattern, text) is not None
    return {'valid': ok, 'expected_regex': pattern,
            'errors': [] if ok else ['Expected live result not observed.']}


def main():
    source = Path('artifacts/ghs/dram.bin').read_bytes()
    assert source[0x11910:0x11920] == b'0123456789ABCDEF'
    capture = Capture('references/physical-34-ram-file-operations', 'COM16', 115200,
                      validator=validate)
    fd = None
    prompt = False

    def run(command, purpose, expected=None):
        nonlocal prompt
        assert len(command) + 9 <= 120 and '\n' not in command
        item = {'command': f'udws "{command};"',
                'classification': 'reviewed-owned-ram-file-operation',
                'purpose': purpose, 'expected_regex': expected,
                'scope': ROOT,
                'review_source': '2018 handler disassembly via references/review_disasm.py',
                'timeout': 20}
        ok = capture.execute(item, len(capture.results) + 1, 20)
        prompt = capture.results[-1]['prompt_returned']
        text = (capture.directory / capture.results[-1]['response_file']).read_bytes().decode('latin-1')
        if not ok:
            raise RuntimeError('Stopped on unexpected physical reply: ' + command)
        return text

    def number(command, purpose, expected=None):
        result = run(command, purpose, expected)
        match = re.search(r'(?:^|[\r\n])\s*(-?\d+);udws\(\)', result)
        if match is None:
            raise RuntimeError('No numeric handler result: ' + command)
        return int(match.group(1))

    zero = r'[\r\n]\s*0;udws\(\)'
    try:
        capture.open()
        if not capture.synchronize():
            raise RuntimeError('No initial UART prompt')
        prompt = True
        run(f'ustd.mkdir {ROOT}', 'Create unique test directory; absence verified in physical-33.',
            r'mkdir\([^\r\n]+\).*result\s+0')
        fd = number(f'ram.creat {A}', 'Create only our RAM file; retain actual returned descriptor.')
        if fd < 0:
            fd = None
            raise RuntimeError('RAM file creation failed')
        number(f'ram.write {fd} 0xa0011910 16', 'Copy 16 bytes of the ROM hex alphabet into our RAM file.', r'[\r\n]\s*16;udws\(\)')
        run(f'ram.fstat {fd}', 'Inspect the descriptor allocated for our file.')
        number(f'ram.lseek {fd} 0 0', 'Seek our descriptor to the start.', zero)
        number(f'ram.close {fd}', 'Close our created descriptor.', zero)
        fd = None
        run(f'ustd.cat {A} 0', 'Read back all 16 physical bytes.', r'0123456789ABCDEF')
        number(f'ustd.sizeof {A}', 'Verify physical file size.', r'[\r\n]\s*16;udws\(\)')
        run(f'ustd.crc {A} 0', 'Calculate checksum of the 16-byte test file.')
        run('ram.crc 0xa0011910 16', 'Checksum the same 16-byte read-only firmware alphabet.')
        fd = number(f'ram.open {A} 2', 'Reopen our existing file read/write.')
        if fd < 0:
            fd = None
            raise RuntimeError('RAM open failed')
        number(f'ustd.ftruncate {fd} 8', 'Truncate only our file to eight bytes.', zero)
        number(f'ram.close {fd}', 'Release our descriptor after truncation.', zero)
        fd = None
        number(f'ustd.sizeof {A}', 'Verify truncation changed the actual size to eight.', r'[\r\n]\s*8;udws\(\)')
        run(f'ustd.cat {A} 0', 'Verify the eight retained bytes.', r'01234567')
        number(f'ustd.copy {A} {B}', 'Copy eight bytes; the handler returns bytes copied.', r'[\r\n]\s*8;udws\(\)')
        run(f'ustd.rename {B} {C}', 'Rename only the copied test file.', r'result\s+0')
        run(f'ustd.cat {C} 0', 'Verify copied and renamed data.', r'01234567')
        run(f'ustd.ll {ROOT}', 'Record the two final test-file entries.')
        number(f'ustd.del {C}', 'Remove only the copied test file.', zero)
        number(f'ram.unlink {A}', 'Remove only the original test file.', zero)
        run(f'ustd.rmdir {ROOT}', 'Remove our now-empty test directory.', r'result\s+0')
        run('ustd.ls /ram', 'Verify the test directory is absent after cleanup.')
        capture.metadata['temporary_files_removed'] = True
        capture.metadata['stop_reason'] = 'batch-complete'
    except Exception as error:
        capture.metadata['stop_reason'] = str(error)
        capture.metadata['manual_follow_up_required'] = {'directory': ROOT, 'known_open_fd': fd,
                                                       'prompt_returned': prompt}
        # Close only a descriptor allocated by this script, if shell remains responsive.
        if fd is not None and prompt:
            try:
                run(f'ram.close {fd}', 'Failure cleanup: close our known test descriptor.', zero)
                capture.metadata['manual_follow_up_required']['known_open_fd'] = None
            except Exception:
                pass
        raise
    finally:
        capture.close()


if __name__ == '__main__':
    main()
