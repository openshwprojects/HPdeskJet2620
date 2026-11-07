"""Merge hash-bound round-five assessments; preserve all original capture outcomes."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

REF = Path(__file__).resolve().parent
reports = ['round5_system_results.json', 'round5_heap_results.json',
           'round5_system_extra_results.json', 'round5_product_results.json',
           'round5_hardware_results.json', 'round5_misc_results.json',
           'round5_misc_followup_results.json', 'round5_metadata_results.json']
result = {'generated_utc':datetime.now(timezone.utc).isoformat(), 'source_reports':[],
          'response_revalidations':[], 'data_quality':[]}
for name in reports:
    raw = (REF / name).read_bytes()
    data = json.loads(raw)
    digest = hashlib.sha256(raw).hexdigest()
    result['source_reports'].append({'file':name, 'sha256':digest})
    for key in ('response_revalidations', 'data_quality'):
        for row in data[key]:
            result[key].append({**row, 'analysis_report':name, 'analysis_report_sha256':digest})
for key in ('response_revalidations', 'data_quality'):
    seen = set()
    for row in result[key]:
        source = row['response_file'].replace('\\','/').removeprefix('references/')
        assert source not in seen, (key, source)
        seen.add(source)
        assert '..' not in Path(source).parts and not Path(source).is_absolute()
        assert hashlib.sha256((REF / source).read_bytes()).hexdigest() == row['response_sha256']
(REF / 'physical-round5-assessments.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
print(json.dumps({key:len(result[key]) for key in ('response_revalidations','data_quality')}))
