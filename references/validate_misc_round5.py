"""Validate the miscellaneous scalar/date/assertion replies without serial access."""
import hashlib
import json
import re
from pathlib import Path
from capture_nvm_mirror import payload

REF = Path(__file__).resolve().parent
directory = REF / 'physical-63-misc-queries'
session = json.loads((directory / 'session.json').read_bytes())
assert session['stop_reason'] == 'batch-complete' and session['ended_utc']
result = {'response_revalidations': [], 'data_quality': [], 'observations': []}
for row in session['commands']:
    if row['name'] in ('crypto_svc.get_rand_num', 'nos_nvm_sirius.get'):
        continue  # Separately reviewed unexpected paths; never silently pass these.
    raw = (directory / row['response_file']).read_bytes()
    assert len(raw) == row['response_bytes']
    assert hashlib.sha256(raw).hexdigest() == row['response_sha256']
    assert row['prompt_returned']
    value = payload(raw.decode('latin-1')).strip()
    if row['name'] == 'undercroft.get_rev':
        assert value == 'SUCCESS: undercroft chip rev is: 2'
        detail = 'Cached revision nibble2; not a chip-model identification.'
    elif row['name'] == 'crypto_svc.is_secure':
        assert value == '1'
        detail = 'Firmware security predicate1; does not establish secure boot enforcement.'
    elif row['name'] == 'crypto_svc.get_rand_seq':
        assert re.fullmatch(r'(\d+,\s*){4}', value)
        assert all(0 <= int(x.strip()) <= 0xffffffff for x in value.split(',') if x.strip())
        detail = 'Exactly four processed32-bit random samples. PRNG state advances; no randomness-quality claim.'
    elif row['name'] == 'mfg_info.calc_mfg_date':
        expected = {'AA711 2017':'20170101', 'AA7CV 2017':'20171231', 'AA811 2018':'20180101'}
        assert value == expected[row['argument_syntax']]
        detail = 'Synthetic serial-code fixture decoded exactly; not the printer manufacturing date.'
    elif row['name'] == 'errlog.get_assert_entry':
        assert re.fullmatch(r'1, ASSERT: c4eb99f7 type: ASSERT_TYPE_HW line: 1733 file: hw_micci2\.c page count: 775 in component \(CID\): 4eb comp error info: 99f7', value)
        detail = 'Retained historical carriage-companion assertion, matching the prior fault code/source/line; record reports page count775. Not a new assertion.'
    else:
        raise AssertionError(row['name'])
    source = {'response_file':directory.name+'/'+row['response_file'], 'response_sha256':row['response_sha256']}
    result['response_revalidations'].append({**source, 'valid':True, 'complete':True,
        'assessment':'validated-miscellaneous-query-payload', 'note':detail})
    result['data_quality'].append({**source, 'complete':True, 'assessment':detail})
    result['observations'].append({'name':row['name'], 'command':row['command'], 'payload':value, 'interpretation':detail})
(REF / 'round5_misc_results.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
print(json.dumps({'validated':len(result['response_revalidations']), 'names':len({x['name'] for x in result['observations']})}))
