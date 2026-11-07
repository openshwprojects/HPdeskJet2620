"""Run the reviewed scanner queries only after checking their live prerequisite."""
import json
from pathlib import Path
from capture_uart import Capture


def main():
    items = json.loads(Path('references/50-scanner-commands.json').read_text())
    capture = Capture('references/physical-50-scanner-capabilities', 'COM16', 115200)
    try:
        capture.open()
        if not capture.synchronize():
            raise RuntimeError('No initial prompt')
        if not capture.execute(items[0], 1, 25):
            raise RuntimeError('Identity query did not return prompt')
        source = capture.results[-1]
        raw = (capture.directory / source['response_file']).read_bytes()
        expected = items[0]['required_observation_for_later_resolution_list']
        valid = expected.encode('ascii') in raw
        capture.metadata['resolution_list_prerequisite'] = {
            'expected': expected, 'observed': valid,
            'response_file': source['response_file'], 'response_sha256': source['response_sha256']}
        capture.save()
        if not valid:
            raise RuntimeError('Sensor identity differs from reviewed resolution-table selection')
        for number, item in enumerate(items[1:], 2):
            if not capture.execute(item, number, 25):
                raise RuntimeError('Query did not return prompt: ' + item['command'])
        capture.metadata['stop_reason'] = 'batch-complete'
    except Exception as error:
        capture.metadata['stop_reason'] = str(error)
        raise
    finally:
        capture.close()


if __name__ == '__main__':
    main()
