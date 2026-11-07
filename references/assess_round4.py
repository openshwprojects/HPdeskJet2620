"""Merge source-bound assessments from the useful-diagnostics capture round."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    result = {'generated_utc': datetime.now(timezone.utc).isoformat(),
              'source_reports': [], 'response_revalidations': [], 'data_quality': []}
    for filename in ['useful_system_round4_results.json',
                     'useful_network_round4_results.json',
                     'useful_scanner_round4_results.json']:
        raw = (ROOT / filename).read_bytes()
        report = json.loads(raw)
        digest = hashlib.sha256(raw).hexdigest()
        result['source_reports'].append({'file': filename, 'sha256': digest})
        for key in ['response_revalidations', 'data_quality']:
            for entry in report[key]:
                result[key].append({**entry, 'analysis_report': filename,
                                    'analysis_report_sha256': digest})
    directory = ROOT / 'physical-51-nvm-mirror'
    session = json.loads((directory / 'session.json').read_bytes())
    dump = next(x for x in session['commands'] if 'nvm2.dump2file ' in x['command'])
    result['data_quality'].append({
        'response_file': directory.name + '/' + dump['response_file'],
        'response_sha256': dump['response_sha256'], 'complete': True,
        'payload_kind': 'bytes-copied-from-nvm-ram-mirror',
        'assessment': 'A full cached image was retrieved and independently replayed.',
        'note': 'Returns65536 copied bytes, not EEPROM content in this reply. The subsequent '
                '128file reads recovered all65536 cached bytes;22direct-byte observations '
                'match, but this does not independently verify every physical EEPROM byte. '
                'The mirror copy is sequential and the firmware ignores its cache-read helper error return.',
        'artifact': directory.name + '/nvm-ram-mirror-65536.bin',
        'artifact_sha256': hashlib.sha256((directory / 'nvm-ram-mirror-65536.bin').read_bytes()).hexdigest(),
        'validation_report': directory.name + '/mirror-validation.json'})
    for key in ['response_revalidations', 'data_quality']:
        seen = set()
        for entry in result[key]:
            source = entry['response_file'].replace('\\', '/').removeprefix('references/')
            assert source not in seen, ('duplicate', key, source)
            assert '..' not in Path(source).parts and not Path(source).is_absolute()
            seen.add(source)
            assert hashlib.sha256((ROOT / source).read_bytes()).hexdigest() == entry['response_sha256']
    (ROOT / 'physical-round4-assessments.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: len(result[key]) for key in ['response_revalidations', 'data_quality']}))


if __name__ == '__main__':
    main()
