"""Derive scanner status reads from exact, completed physical prerequisites."""
import hashlib
import json
import re

from prepare_storage_reads import Captures, REF, relative, require


def main():
    review_path = REF / 'status_prerequisite_review.json'
    review = json.loads(review_path.read_text(encoding='utf-8'))
    captures = Captures()
    session = 'physical-27-status-prerequisites'
    decisions = []
    plan = []

    def payload(command):
        body, source = captures.exact(session, command)
        require(body.endswith(';'), f'Missing result separator: {source}')
        return body[:-1].strip(), source

    def numeric(command):
        value, source = payload(command)
        require(re.fullmatch(r'\d+', value) is not None, f'Not one nonnegative integer: {source}')
        return int(value), source

    rules = review['plan28_generation_rules']
    for rule in rules['record_getters']:
        count, count_source = numeric(rule['prerequisite_commands'][0])
        size, size_source = numeric(rule['prerequisite_commands'][1])
        limits = rule['accept_if']
        passed = (limits['record_count_min'] <= count <= limits['record_count_max'] and
                  limits['record_size_bytes_min'] <= size <= limits['record_size_bytes_max'])
        observation = {'record_count': count, 'record_bytes': size,
                       'count_source': count_source, 'size_source': size_source}
        decisions.append({'name': rule['name'], 'eligible': passed, **observation,
                          'reason': 'All live bounds passed.' if passed else 'Live record count or size does not satisfy the reviewed bounds.'})
        if passed:
            plan.append({**rule, 'physical_prerequisites': observation,
                         'review_source': relative(review_path)})

    rule = rules['remote_scan_state_getter']
    name, name_source = payload(rule['prerequisite_commands'][0])
    count, count_source = numeric(rule['prerequisite_commands'][1])
    size, size_source = numeric(rule['prerequisite_commands'][2])
    value, value_source = numeric(rule['prerequisite_commands'][3])
    limits = rule['accept_if']
    passed = (name == limits['name_exact'] and count == limits['element_count_equals'] and
              size == limits['record_size_bytes_equals'] and value in limits['value_in'])
    observation = {'observed_name': name, 'element_count': count, 'record_bytes': size,
                   'value': value, 'name_source': name_source, 'count_source': count_source,
                   'size_source': size_source, 'value_source': value_source}
    decisions.append({'name': rule['name'], 'eligible': passed, **observation,
                      'reason': 'All live identity and state checks passed.' if passed else 'Live scalar identity or state failed reviewed requirements.'})
    if passed:
        plan.append({**rule, 'classification': 'reviewed-information-query-after-live-metadata',
                     'physical_prerequisites': observation, 'review_source': relative(review_path)})
    for item in plan:
        require(item['command'].isascii() and len(item['command']) <= 120, 'Command exceeds input limit')
    assessment = {'source': 'physical UART prerequisites, independently hash-checked',
                  'review_source': relative(review_path),
                  'review_sha256': hashlib.sha256(review_path.read_bytes()).hexdigest(),
                  'sources': captures.sources, 'decisions': decisions,
                  'eligible_count': len(plan), 'held_count': len(decisions) - len(plan)}
    for name, data in [('28-status-commands.json', plan), ('status-prerequisite-results.json', assessment)]:
        (REF / name).write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'eligible': [x['name'] for x in plan],
                      'held': [x['name'] for x in decisions if not x['eligible']]}))


if __name__ == '__main__':
    main()
