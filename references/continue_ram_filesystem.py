"""Continue the physical-34 copy test, exercise owned handles, then clean up.

Physical-34 stopped because our expectation was incorrect: ustd.copy returns
the byte count, not zero. Its raw reply remains unchanged. This continuation
verifies both files before further operations.
"""
import re
from capture_uart import Capture
from exercise_ram_filesystem import ROOT, A, B, C, validate


def main():
    capture = Capture('references/physical-35-ram-handles-and-cleanup', 'COM16', 115200,
                      validator=validate)
    fds, mapping, directory = [], None, None
    zero = r'[\r\n]\s*0;udws\(\)'
    eight = r'[\r\n]\s*8;udws\(\)'

    def run(command, purpose, expected=None):
        assert len(command) + 9 <= 120
        item = {'command': f'udws "{command};"',
                'classification': 'reviewed-owned-ram-file-operation',
                'scope': ROOT, 'purpose': purpose, 'expected_regex': expected,
                'review_source': '2018 handler disassembly via references/review_disasm.py',
                'timeout': 20}
        if not capture.execute(item, len(capture.results)+1, 20):
            raise RuntimeError('Unexpected physical response: '+command)
        return (capture.directory / capture.results[-1]['response_file']).read_bytes().decode('latin-1')

    def number(command, purpose, expected=None):
        text = run(command, purpose, expected)
        match = re.search(r'(?:^|[\r\n])\s*(-?\d+);udws\(\)', text)
        if not match:
            raise RuntimeError('Numeric handler result missing: '+command)
        return int(match.group(1))

    def open_file(path, flags):
        fd = number(f'ram.open {path} {flags}', 'Open our known file and use only its returned descriptor.')
        if fd < 0:
            raise RuntimeError('Could not open owned RAM file')
        fds.append(fd)
        return fd

    def close_file(fd):
        number(f'ram.close {fd}', 'Release only our allocated descriptor.', zero)
        fds.remove(fd)

    try:
        capture.open()
        if not capture.synchronize():
            raise RuntimeError('No initial prompt')
        for path in (A, B):
            number(f'ustd.sizeof {path}', 'Verify prior test file is eight bytes.', eight)
            run(f'ustd.cat {path} 0', 'Verify actual eight-byte test content.', r'01234567;udws')
        fd_a = open_file(A, 2)
        text = run(f'ram.mmap {fd_a} 0 8 3 1 0', 'Map eight bytes of our own RAM file writable; kernel chooses address.')
        match = re.search(r'prot = 3 : 0x([0-9a-fA-F]+)', text)
        if not match:
            raise RuntimeError('Missing returned mmap address')
        mapping = int(match.group(1), 16)
        if not (0x20000000 <= mapping < 0x24000000 or 0xa0000000 <= mapping < 0xa4000000):
            mapping = None
            raise RuntimeError('mmap did not return a DRAM address')
        fd_b = open_file(B, 0)
        number(f'ram.read {fd_b} {hex(mapping)} 8', 'Read our copied file into the eight-byte mapping of our original file.', eight)
        close_file(fd_b)
        number(f'ram.munmap {hex(mapping)} 8', 'Release only the mapping returned by this test.', zero)
        mapping = None
        close_file(fd_a)
        run(f'ustd.cat {A} 0', 'Verify RAM descriptor read preserved exact content.', r'01234567;udws')
        text = run(f'ram.opendir {ROOT}', 'Open only our test directory for bounded enumeration.')
        match = re.search(r'opendir\([^\r\n]+\) : 0x([0-9a-fA-F]+)', text)
        if not match:
            raise RuntimeError('Missing returned directory handle')
        directory = int(match.group(1), 16)
        if not directory or directory == 0xffffffff:
            directory = None
            raise RuntimeError('opendir failed')
        run(f'ram.dirfd {hex(directory)}', 'Query the descriptor of our actual directory handle.')
        for _ in range(3):
            run(f'ram.readdir {hex(directory)}', 'Read one bounded entry from our two-file directory.')
        run(f'ram.closedir {hex(directory)}', 'Release only our allocated directory handle.', r'closedir\(0x[0-9a-fA-F]+\) : 0')
        directory = None
        run(f'ustd.open_file {A}', 'Use the dedicated debug reader with our eight-byte test file.', eight)
        run('ustd.read_file 0 8', 'Read exact eight bytes through the debug reader.', r'30.?31.?32.?33.?34.?35.?36.?37')
        run('ustd.close_file', 'Close the debug reader opened by this test.')
        run(f'ustd.rename {B} {C}', 'Rename only our copied file.', r'result 0, errno 0')
        run(f'ustd.cat {C} 0', 'Verify renamed data.', r'01234567;udws')
        run(f'ustd.ll {ROOT}', 'Record final test-file listing.')
        number(f'ustd.del {C}', 'Delete only our renamed copy.', zero)
        number(f'ram.unlink {A}', 'Delete only our original test file.', zero)
        run(f'ustd.rmdir {ROOT}', 'Remove our empty directory.', r'result 0')
        text = run('ustd.ls /ram', 'Verify cleanup in the actual RAM root listing.')
        if 'codex_uart_20261029a' in text:
            raise RuntimeError('Temporary directory still listed')
        capture.metadata['temporary_files_removed'] = True
        capture.metadata['stop_reason'] = 'batch-complete'
    except Exception as error:
        capture.metadata['stop_reason'] = str(error)
        capture.metadata['manual_follow_up_required'] = {
            'directory': ROOT, 'known_open_fds': fds, 'mapping': mapping,
            'directory_handle': directory}
        raise
    finally:
        capture.close()


if __name__ == '__main__':
    main()
