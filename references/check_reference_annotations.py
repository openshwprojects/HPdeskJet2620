"""Offline checks for supplemental annotations; never opens UART or rebuilds reports."""
import hashlib
import fnmatch
import json
from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parent


def main():
    module = runpy.run_path(str(ROOT / 'build_reference_index.py'), run_name='annotation_checks')
    load = module['load_response_annotations']
    files = {}
    class FixturePath:
        def __init__(self, key=''):
            self.key = key
        def __truediv__(self, name):
            return FixturePath('/'.join(part for part in [self.key, str(name)] if part))
        def __lt__(self, other):
            return self.key < other.key
        def glob(self, pattern):
            prefix = self.key + '/' if self.key else ''
            return [FixturePath(key) for key in files
                    if key.startswith(prefix) and '/' not in key[len(prefix):]
                    and fnmatch.fnmatchcase(key[len(prefix):], pattern)]
        @property
        def name(self):
            return self.key.rsplit('/', 1)[-1]
        def exists(self):
            return self.key in files
        def is_file(self):
            return self.exists()
        def read_bytes(self):
            return files[self.key]
        def write_bytes(self, data):
            files[self.key] = data
        def write_text(self, text, **kwargs):
            self.write_bytes(text.encode('utf-8'))
    # In-memory files keep the checks independent of platform temporary-directory ACLs.
    def check_fixture():
        fixture = FixturePath()
        load.__globals__['ROOT'] = fixture
        raw = b'cp() returns 8 (0x8)\r\n-> '
        response = fixture / 'physical-fixture' / '0001.bin'
        response.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        entry = {'response_file': 'references/physical-fixture/0001.bin', 'response_sha256': digest,
                 'valid': True, 'assessment': 'Expected successful copy byte count.',
                 'note': 'The observed return 8 equals the requested eight bytes.'}
        quality = {'response_file': entry['response_file'], 'response_sha256': digest,
                   'complete': False, 'assessment': 'Fixture incomplete output', 'note': 'A row is missing.'}
        manifest = fixture / 'physical-round2-assessments.json'
        def save(values):
            manifest.write_text(json.dumps(values), encoding='utf-8')
        save({'response_revalidations': [entry], 'data_quality': [quality]})
        q, v = load()
        key = 'physical-fixture/0001.bin'
        assert module['checked_annotation'](q, key, raw)['complete'] is False
        assert module['checked_annotation'](v, key, raw)['valid'] is True
        original = {'command': 'cp /ram/owned/a /ram/owned/b', 'outcome': 'reported-error',
                    'status': 'prompt-returned', 'text_file': 'fixture.txt',
                    'data_quality': q[key], 'post_capture_validation': v[key]}
        rendered = module['render_test'](original)
        assert '[reported-error]' in rendered and 'Later revalidation passed' in rendered
        assert 'requested eight bytes' in rendered and '**Incomplete output.**' in rendered
        assert original['outcome'] == 'reported-error'
        for bad in [{**entry, 'response_sha256': '0' * 64},
                    {**entry, 'response_file': '../outside.bin'},
                    {**entry, 'valid': 'true'}]:
            save({'response_revalidations': [bad]})
            try:
                load()
            except ValueError:
                pass
            else:
                raise AssertionError(f'Invalid annotation accepted: {bad}')
        save({'response_revalidations': [entry, entry]})
        try:
            load()
        except ValueError:
            pass
        else:
            raise AssertionError('Duplicate annotations accepted')
        save({'response_revalidations': [entry]})
        another = fixture / 'physical-round4-assessments.json'
        another.write_text(json.dumps({'data_quality': [quality]}))
        q, v = load()
        assert q[key]['source_report'] == another.name
        assert v[key]['source_report'] == manifest.name
        another.write_text(json.dumps({'response_revalidations': [entry]}))
        try:
            load()
        except ValueError:
            pass
        else:
            raise AssertionError('Duplicate annotations across rounds accepted')
        assert response.read_bytes() == raw
    check_fixture()
    print('Supplemental annotation checks passed: source hash/path, duplicate rejection, generic quality rendering, historical outcome preserved. No capture/report changed.')


if __name__ == '__main__':
    main()
