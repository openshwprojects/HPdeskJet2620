"""Index captured physical responses without fabricating unexecuted results."""
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent


def dump(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


def command_names(command):
    match = re.fullmatch(r'(?:udws|ludw) "(.*)"', command)
    if match:
        return [part.strip() for part in match.group(1).split(';') if part.strip()]
    return [command]


def is_registry_enumeration(command):
    return bool(re.fullmatch(r'(?:udws|ludw) "(.*)"', command)) and all(
        name in {'udw.iter_reset', 'udw.iter_get_next'} for name in command_names(command))


def shell_returns(text, command):
    name = command.split(' ', 1)[0]
    return [int(value) for value in re.findall(
        re.escape(name) + r'\(\) returns (-?\d+) \(0x[0-9a-fA-F]+\)', text)]


def response_body(text, command):
    text = text.replace('\r', '')
    echo = command + '\n'
    if echo in text:
        text = text.split(echo, 1)[1]
    text = re.sub(re.escape(command.split(' ', 1)[0]) +
                  r'\(\) returns -?\d+ \(0x[0-9a-fA-F]+\)', '', text)
    text = re.sub(r'\n->\s*$', '', text)
    return text.strip(' \r\n;')


def registered_expressions(command, names, complete):
    """Resolve only UDW expressions against the UDW registry, not native shell calls."""
    if not re.fullmatch(r'(?:udws|ludw) "(.*)"', command):
        return []
    expressions = []
    for expression in command_names(command):
        candidates = [name for name in names if expression == name or expression.startswith(name + ' ')]
        name = max(candidates, key=len) if candidates else expression.split(' ', 1)[0]
        expressions.append({'expression': expression, 'name': name,
                            'runtime_registered': True if candidates else (False if complete else None)})
    return expressions


def bulk_payload_validation(command, text):
    """Check the fixed-width format established for this firmware's full NVM read."""
    if command_names(command) != ['nvm2.nvm_read_all']:
        return None
    marker = '65536,'
    start = text.find(marker)
    end = text.find(';', start + len(marker)) if start >= 0 else -1
    body = text[start + len(marker):end] if start >= 0 and end >= 0 else ''
    fields = body.split(',') if body else []
    invalid = []
    for index, field in enumerate(fields):
        try:
            valid = 0 <= int(field) <= 255 and field == f'{int(field):3d}'
        except ValueError:
            valid = False
        if not valid:
            invalid.append(index)
    valid = start >= 0 and end >= 0 and len(fields) == 65536 and not invalid
    return {'kind': 'fixed-width-full-nvm-response', 'expected_fields': 65536,
            'header_seen': start >= 0, 'final_semicolon_seen': end >= 0,
            'actual_fields': len(fields), 'invalid_field_count': len(invalid),
            'first_invalid_field_index': invalid[0] if invalid else None,
            'payload_bytes_including_final_semicolon': len(body) + 1 if end >= 0 else None,
            'expected_payload_bytes': 262144, 'payload_valid': valid,
            'note': 'Structural validation only; prompt/return status and independent data comparison are separate.'}


def outcome(result, text, body, validation=None, expressions=()):
    if '*** ASSERT:' in text:
        return 'firmware-assertion'
    if result['status'] == 'started':
        return 'in-progress-or-interrupted'
    if result['status'] == 'response-validation-failed' or result.get('validation', {}).get('valid') is False:
        return 'response-validation-failed'
    if validation is not None and not validation['payload_valid']:
        return 'malformed-bulk-response'
    if not result.get('prompt_returned'):
        return 'timeout-no-prompt'
    # Anchor diagnostics so counters such as "Peak RAM usage" and "Error: 0"
    # in normal status reports do not become usage/error responses.
    if re.search(r"(?im)^\s*(?:error(?:\s*$|\s*[-:]|\s+while\b)|"
                 r"invalid\s+(?:file\s+)?arguments\b|unknown\s+kind\s*:|"
                 r"name conversion failed|unknown command|command not found|undefined symbol)", body):
        return 'reported-error'
    if is_registry_enumeration(result['command']):
        return 'registry-enumeration'
    if expressions and any(x['runtime_registered'] is False for x in expressions):
        return 'unregistered-command-response' if body else 'silent-unregistered-command'
    if re.search(r'(?im)^\s*usage\s*:', body):
        return 'usage-response'
    if any(value != 0 for value in shell_returns(text, result['command'])):
        return ('argument-error-response' if result.get('classification') == 'reviewed-empty-argument-path'
                else 'reported-error')
    if not body:
        return 'returned-without-payload'
    return 'output-returned'


def command_family(item, native_kinds):
    if re.fullmatch(r'(?:udws|ludw) "(.*)"', item['command']):
        return 'udw-expression-batch'
    name = item['command'].split(' ', 1)[0]
    kind = item.get('kind') or native_kinds.get(name)
    if kind in {'shell_variable_read', 'variable'}:
        return 'native-variable-read'
    if kind in {'shell_function', 'function'}:
        return 'native-function-call'
    return 'native-shell-attempt'


def exercise_scope(item, result_outcome):
    if is_registry_enumeration(item['command']):
        return 'registry-enumeration-only'
    if item.get('classification') == 'proven-compiled-out-handler':
        return 'compiled-handler-only'
    if item.get('classification') == 'reviewed-empty-argument-path':
        return 'empty-argument-path-only'
    if item.get('classification') == 'reviewed-software-only-empty-path':
        return 'software-only-empty-path'
    if item.get('classification') == 'reviewed-read-and-clear-volatile-counters':
        return 'read-and-clear-volatile-counters'
    if item.get('classification') in {'explicit-help', 'reviewed-help'} or result_outcome == 'usage-response':
        return 'help-or-usage-output-only'
    if result_outcome in {'silent-unregistered-command', 'unregistered-command-response'}:
        return 'unregistered-name-attempt'
    return 'supplied-invocation-only'


def reference_path(path):
    """Normalize catalog paths to the references directory used by this index."""
    return path.replace('\\', '/').removeprefix('references/')


def load_response_annotations():
    quality, revalidations = {}, {}
    catalog_path = ROOT / 'physical-nvm-catalog.json'
    if catalog_path.exists():
        catalog_bytes = catalog_path.read_bytes()
        catalog = json.loads(catalog_bytes)
        validation = catalog.get('validation', {})
        anomalies = defaultdict(list)
        for anomaly in validation.get('capture_anomalies', []):
            anomalies[anomaly['source']['response']].append(anomaly)
        objects = {item['dsid']: item for item in catalog.get('objects', [])}
        for result in validation.get('reset_capture_counts', []):
            source = result['source']
            # Filtered reports deliberately omit objects. Only sources with a
            # documented malformed row are labeled incomplete here.
            if source not in anomalies:
                continue
            missing = result['missing_from_full_catalog']
            recovery = [{'dsid': dsid, 'sources': objects[dsid]['sources']['reset_membership']}
                        for dsid in missing]
            quality[reference_path(source)] = {
                'assessment': 'known-incomplete-response', 'complete': False,
                'source_report': catalog_path.name,
                'source_report_sha256': hashlib.sha256(catalog_bytes).hexdigest(),
                'response_sha256': catalog['provenance']['sources'][source]['sha256'],
                'observed_unique_rows': result['unique_rows'],
                'expected_unique_rows': catalog['counts']['nvm_objects'],
                'missing_dsids': missing, 'capture_anomalies': anomalies[source],
                'recovery_evidence': recovery,
                'catalog_union_covers_all_objects': validation.get('reset_membership_union_covers_all_objects'),
                'note': 'Other physical responses recover the missing catalog rows; this individual response remains incomplete.'}
    values_path = ROOT / 'physical-storage-values.json'
    if values_path.exists():
        values_bytes = values_path.read_bytes()
        values = json.loads(values_bytes)
        for entry in values.get('response_revalidations', []):
            source = reference_path(entry['response_file'])
            if source in revalidations:
                raise ValueError(f'Duplicate response revalidation: {source}')
            revalidations[source] = {**entry, 'source_report': values_path.name,
                                    'source_report_sha256': hashlib.sha256(values_bytes).hexdigest(),
                                    'preserves_original_capture_validation': True}
    for supplemental_path in sorted(ROOT.glob('physical-round*-assessments.json')):
        supplemental_bytes = supplemental_path.read_bytes()
        supplemental = json.loads(supplemental_bytes)
        for collection, annotations in [('data_quality', quality), ('response_revalidations', revalidations)]:
            entries = supplemental.get(collection, [])
            if not isinstance(entries, list):
                raise ValueError(f'Supplemental {collection} must be a list')
            for entry in entries:
                source = reference_path(entry['response_file'])
                if Path(source).is_absolute() or '..' in Path(source).parts:
                    raise ValueError(f'Supplemental response path escapes references: {source}')
                if not re.fullmatch(r'[0-9a-fA-F]{64}', entry['response_sha256']):
                    raise ValueError(f'Supplemental response hash is not SHA-256: {source}')
                response_path = ROOT / source
                if not response_path.is_file() or hashlib.sha256(response_path.read_bytes()).hexdigest() != entry['response_sha256'].lower():
                    raise ValueError(f'Supplemental response missing or hash mismatch: {source}')
                if source in annotations:
                    raise ValueError(f'Duplicate {collection} annotation: {source}')
                if collection == 'response_revalidations' and not isinstance(entry.get('valid'), bool):
                    raise ValueError(f'Supplemental response revalidation needs a boolean valid: {source}')
                annotations[source] = {
                    **entry, 'response_sha256': entry['response_sha256'].lower(), 'source_report': supplemental_path.name,
                    'source_report_sha256': hashlib.sha256(supplemental_bytes).hexdigest(),
                    'preserves_original_capture_validation': True}
    return quality, revalidations


def checked_annotation(annotations, path, raw):
    annotation = annotations.get(path)
    if annotation is not None and annotation['response_sha256'] != hashlib.sha256(raw).hexdigest():
        raise ValueError(f'Stale response annotation/hash mismatch: {path}')
    return annotation


def load_review_notes():
    """Index exact named decisions; never infer a hold from a name prefix."""
    notes = {'udw': defaultdict(list), 'native': defaultdict(list)}
    reviewed = defaultdict(list)
    collections = {
        'additional_query_review.json': [('held', 'udw', 'information-query batch')],
        'help_review.json': [('held_candidates', 'udw', 'empty-argument path'),
                             ('systematic_sweep.held_descriptor_names', 'udw', 'empty-argument path')],
        'network_query_review.json': [('excluded_from_this_query_batch', 'udw', 'network metadata batch')],
        'software_query_review.json': [('held_for_further_review', 'udw', 'software query path')],
        'shell_query_review.json': [('excluded_from_metadata_batch', 'native', 'native metadata batch')],
    }
    for path in sorted(ROOT.glob('*round*review.json')):
        data = json.loads(path.read_bytes())
        if not isinstance(data, dict):
            continue
        scope = ('offline empty-argument checker path' if path.name.startswith('guard_')
                 else 'invocation path described in the cited review')
        paths = [(key, 'udw', scope) for key in
                 ['held', 'held_candidates', 'held_for_further_review', 'held_paths',
                  'excluded_from_this_query_batch', 'excluded_from_metadata_batch']
                 if isinstance(data.get(key), list)]
        if isinstance(data.get('reviews'), list):
            paths.append(('reviews', 'udw', scope))
        collections[path.name] = paths
    for filename, paths in collections.items():
        path = ROOT / filename
        if not path.exists():
            continue
        raw = path.read_bytes()
        data = json.loads(raw)
        review_sha256 = hashlib.sha256(raw).hexdigest()
        for collection, namespace, scope in paths:
            entries = data
            for part in collection.split('.'):
                entries = entries.get(part, {})
            for index, entry in enumerate(entries):
                if collection == 'reviews' and entry.get('status') not in {'held-for-further-review', 'held', 'unreviewed'}:
                    continue
                for name in entry.get('names', [entry.get('name')]):
                    if not name:
                        continue
                    reasons = entry.get('reasons', [entry.get('reason', entry.get('evidence'))])
                    if not all(isinstance(reason, str) for reason in reasons):
                        raise ValueError(f'Missing review reason: {filename} {collection} {index}')
                    notes[namespace][name].append({
                        'name': name, 'source_review': filename,
                        'source_review_sha256': review_sha256,
                        'source_entry': f'{collection}[{index}]', 'invocation_scope': entry.get('invocation_scope', scope),
                        'kind': ('not-repeated-in-review' if entry.get('category') == 'already_covered'
                                 else 'unproven-checker-path' if filename.startswith('guard_round')
                                 else 'held-in-review'),
                        'category': entry.get('category'), 'reasons': reasons,
                        'superseded': False})
    # Accepted named review entries establish only the particular described path.
    # Do not interpret free-form prose, wildcards, or command prefixes as names.
    accepted_collections = {'queries', 'parameterized_queries', 'no_output_queries', 'commands',
                            'read_and_clear_queries', 'guard_queries', 'additional_reviewed_queries',
                            'hardware_queries', 'reviews'}
    for path in sorted(ROOT.glob('*review.json')):
        data = json.loads(path.read_bytes())
        if not isinstance(data, dict):
            continue
        for collection in sorted(accepted_collections):
            for entry in data.get(collection, []):
                if not isinstance(entry, dict) or not isinstance(entry.get('name'), str):
                    continue
                if collection == 'reviews' and entry.get('status') != 'ready':
                    continue
                name = entry['name']
                source = {'source_review': path.name, 'source_collection': collection}
                if source not in reviewed[name]:
                    reviewed[name].append(source)
    corrections_path = ROOT / 'native_guard_review.json'
    if corrections_path.exists():
        for index, correction in enumerate(json.loads(corrections_path.read_bytes()).get('corrections_to_prior_review', [])):
            for name in correction.get('names', [correction.get('name')]):
                for namespace in notes.values():
                    for note in namespace.get(name, []):
                        if note['source_review'] in correction['supersedes']:
                            note['superseded'] = True
                            note['correction'] = {**correction, 'source_review': corrections_path.name,
                                                  'source_entry': f'corrections_to_prior_review[{index}]'}
    prerequisites_path = ROOT / 'status-prerequisite-results.json'
    if prerequisites_path.exists():
        raw = prerequisites_path.read_bytes()
        for index, decision in enumerate(json.loads(raw).get('decisions', [])):
            name = decision['name']
            notes['udw'][name].append({
                'name': name, 'source_review': prerequisites_path.name,
                'source_review_sha256': hashlib.sha256(raw).hexdigest(),
                'source_entry': f'decisions[{index}]', 'invocation_scope': 'live prerequisite bounds',
                'kind': 'prerequisites-passed' if decision['eligible'] else 'held-by-live-prerequisites',
                'reasons': [decision['reason']], 'superseded': False, 'decision': decision})
    return notes, reviewed


def render_review_note(note):
    label = {'held-in-review': 'Held in the cited review',
             'unproven-checker-path': 'Not proved by the offline checker',
             'not-repeated-in-review': 'Already covered in the cited review',
             'prerequisites-passed': 'Later live prerequisites passed',
             'held-by-live-prerequisites': 'Held by live prerequisites'}[note['kind']]
    if note['superseded']:
        label = 'Superseded review decision'
    # Preserve verbatim reasons in JSON; escape only Markdown/HTML presentation.
    reason = '<br>'.join(html.escape(x).replace('|', '&#124;').replace('\n', '<br>') for x in note['reasons'])
    result = (f'{label} ({note["invocation_scope"]}): {reason} '
              f'[{note["source_review"]}: {note["source_entry"]}]({note["source_review"]})')
    if note.get('correction'):
        correction = note['correction']
        result += f' Correction: {html.escape(correction["finding"])} [{correction["source_review"]}]({correction["source_review"]})'
    return result


def render_test(item):
    result = f'[{item["outcome"]}]({item["text_file"]}) — `{item["command"]}`'
    quality = item.get('data_quality')
    if quality is not None and quality.get('complete') is False:
        if all(key in quality for key in ['missing_dsids', 'observed_unique_rows', 'expected_unique_rows']):
            missing = ', '.join(map(str, quality['missing_dsids']))
            result += (f' **Incomplete: {quality["observed_unique_rows"]}/{quality["expected_unique_rows"]} rows; '
                       f'missing DSIDs {missing}.**')
        else:
            result += ' **Incomplete output.**'
    if quality is not None:
        result += f' [Quality evidence]({quality["source_report"]})'
        if quality.get('note'):
            result += ' ' + html.escape(str(quality['note'])).replace('|', '&#124;').replace('\n', '<br>')
    revalidation = item.get('post_capture_validation')
    if revalidation is not None:
        status = 'passed' if revalidation['valid'] else 'failed'
        result += (f' [Later revalidation {status}]({revalidation["source_report"]}) '
                   '(original capture status and validation retained).')
        if revalidation.get('assessment'):
            result += ' ' + html.escape(str(revalidation['assessment'])).replace('|', '&#124;').replace('\n', '<br>')
        if revalidation.get('note'):
            result += ' ' + html.escape(str(revalidation['note'])).replace('|', '&#124;').replace('\n', '<br>')
    if item.get('side_effect'):
        effect = item['side_effect']
        if effect.get('kind') == 'clear_volatile_timing_counters':
            result += (f' **Read and clear:** cleared {len(effect["counter_labels"])} volatile timing counters '
                       f'({effect["byte_count"]} RAM bytes) after emitting their values.')
        else:
            result += f' Side effect: `{effect["kind"]}`.'
    return result


def main():
    records, sessions, joined = [], [], []
    integrity = []
    session_sources = [(path, json.loads(path.read_text())) for path in ROOT.glob('physical-*/session.json')]
    session_sources.sort(key=lambda pair: (datetime.fromisoformat(pair[1]['started_utc']), pair[0].parent.name))
    inventories = []
    for session_path, _ in session_sources:
        path = session_path.parent / 'runtime_inventory.json'
        if path.exists():
            inventories.append(json.loads(path.read_text()) |
                               {'file': str(path.relative_to(ROOT)).replace('\\', '/')})
    complete = [x for x in inventories if x.get('complete')]
    inventory = complete[-1] if complete else (inventories[-1] if inventories else None)
    registry_complete = bool(inventory and inventory.get('complete'))
    names = sorted({x['name'] for x in inventory['entries']}) if inventory else []
    native_review_path = ROOT / 'shell_query_review.json'
    native_review = json.loads(native_review_path.read_text()) if native_review_path.exists() else {}
    native_kinds = {entry['name']: entry['kind'] for entry in native_review.get('static_registration_calls', [])}
    quality_annotations, revalidations = load_response_annotations()
    review_notes, reviewed_paths = load_review_notes()
    for session_path, session in session_sources:
        folder = session_path.parent
        finalized = bool(session.get('ended_utc') or session.get('stop_reason'))
        sessions.append({'directory': folder.name, 'started_utc': session['started_utc'],
                         'ended_utc': session.get('ended_utc'), 'stop_reason': session.get('stop_reason'),
                         'command_count': len(session['commands']), 'responses_indexed': finalized})
        # Live writers update RX, wire events, and metadata separately. Do not
        # present a mixture of those moments as a verified capture snapshot.
        if not finalized:
            continue
        wire_path = folder / 'wire.jsonl'
        tx_events = []
        if wire_path.exists():
            events = [json.loads(line) for line in wire_path.read_text().splitlines() if line]
            offset = 0
            for event in events:
                data = bytes.fromhex(event['hex'])
                if event['bytes'] != len(data):
                    raise ValueError(f'Wire event length mismatch: {folder}')
                if event['direction'] == 'rx':
                    if event.get('offset') != offset:
                        raise ValueError(f'Wire RX offset mismatch: {folder}')
                    offset += len(data)
                elif event['direction'] == 'tx':
                    tx_events.append(data)
            wire_rx = b''.join(bytes.fromhex(e['hex']) for e in events if e['direction'] == 'rx')
            raw = (folder / 'rx.bin').read_bytes()
            integrity.append({'session': folder.name, 'wire_matches_rx_bin': wire_rx == raw,
                              'rx_bytes': len(raw), 'rx_sha256': hashlib.sha256(raw).hexdigest()})
            if wire_rx != raw:
                raise ValueError(f'Wire/RX mismatch: {folder}')
        transcript = folder / 'transcript.txt'
        if transcript.exists():
            joined.append(f'\n===== CAPTURE {folder.name} | {session["started_utc"]} =====\n')
            joined.append(transcript.read_text(encoding='utf-8'))
        for item in session['commands']:
            file = folder / item['response_file']
            raw = file.read_bytes() if file.exists() else b''
            if item['status'] != 'started' and not file.exists():
                raise ValueError(f'Finalized response file missing: {file}')
            if item.get('response_bytes') is not None and len(raw) != item['response_bytes']:
                raise ValueError(f'Response length mismatch: {file}')
            if item.get('response_sha256') and hashlib.sha256(raw).hexdigest() != item['response_sha256']:
                raise ValueError(f'Response hash mismatch: {file}')
            tx_file = file.with_suffix('.tx.bin')
            if tx_file.exists():
                tx = tx_file.read_bytes()
                ending = {'CR': b'\r', 'CRLF': b'\r\n'}.get(session.get('line_ending'))
                if ending is not None and tx != item['command'].encode('ascii') + ending:
                    raise ValueError(f'Command TX file mismatch: {tx_file}')
                if wire_path.exists() and tx not in tx_events:
                    raise ValueError(f'Command TX missing from wire log: {tx_file}')
            text = raw.decode('latin-1')
            body = response_body(text, item['command'])
            expressions = registered_expressions(item['command'], names, registry_complete)
            registered = (all(x['runtime_registered'] for x in expressions)
                          if expressions and registry_complete else
                          (True if expressions and all(x['runtime_registered'] is True for x in expressions) else None))
            validation = bulk_payload_validation(item['command'], text)
            result_outcome = outcome(item, text, body, validation, expressions)
            response_path = str(file.relative_to(ROOT)).replace('\\', '/')
            data_quality = checked_annotation(quality_annotations, response_path, raw)
            post_capture_validation = checked_annotation(revalidations, response_path, raw)
            if data_quality is not None and data_quality.get('complete') is False and result_outcome == 'output-returned':
                result_outcome = 'output-returned-incomplete'
            family = command_family(item, native_kinds)
            returns = shell_returns(text, item['command'])
            native_name = item['command'].split(' ', 1)[0] if family != 'udw-expression-batch' else None
            variable_value_seen = bool(native_name and re.search(
                r'(?m)^' + re.escape(native_name) + r' = -?\d+ \(0x[0-9a-fA-F]+\)', body))
            records.append({**item, 'session': folder.name,
                            'response_file': response_path,
                            'text_file': str(file.with_suffix('.txt').relative_to(ROOT)).replace('\\', '/'),
                            'outcome': result_outcome, 'body': body,
                            'command_family': family, 'native_name': native_name,
                            'exercise_scope': exercise_scope(item, result_outcome),
                            'native_kind_source': ('capture metadata or shell_query_review.json static call sites'
                                                   if native_name else None),
                            'native_function_return_seen': bool(returns) if native_name else None,
                            'native_variable_value_seen': variable_value_seen if family == 'native-variable-read' else None,
                            'native_review_notes': review_notes['native'].get(native_name, []),
                            'command_expressions': command_names(item['command']),
                            'udw_expressions': expressions, 'runtime_registered': registered,
                            'registration_inventory_file': inventory['file'] if expressions and inventory else None,
                            'transmission_confirmed_by_tx_file': tx_file.exists(),
                            'wrapper_returns': [int(x) for x in re.findall(r'(?:udws|ludw)\(\) returns (-?\d+)', text)],
                            'shell_returns': returns,
                            'payload_validation': validation,
                            'data_quality': data_quality,
                            'post_capture_validation': post_capture_validation})
    dump(ROOT / 'command_results.json', records)
    dump(ROOT / 'capture_integrity.json', integrity)
    (ROOT / 'all_uart_output.txt').write_text(''.join(joined), encoding='utf-8')

    tested = defaultdict(list)
    for item in records:
        if not item['transmission_confirmed_by_tx_file']:
            continue
        for expression in item['udw_expressions']:
            if expression['runtime_registered'] is not True:
                continue
            name = expression['name']
            tested[name].append({'command': item['command'], 'outcome': item['outcome'],
                                 'text_file': item['text_file'], 'data_quality': item['data_quality'],
                                 'post_capture_validation': item['post_capture_validation'],
                                 'exercise_scope': item['exercise_scope'], 'side_effect': item.get('side_effect')})
    held_names = {name for name in names if any(note['kind'] in {'held-in-review', 'held-by-live-prerequisites'}
                                               and not note['superseded'] for note in review_notes['udw'].get(name, []))}
    unproven_names = {name for name in names if any(note['kind'] == 'unproven-checker-path'
                                                  for note in review_notes['udw'].get(name, []))}
    coverage = {'source': 'physical registry and physical command responses',
                'registry_complete': registry_complete,
                'inventory_file': inventory['file'] if inventory else None,
                'unique_registered_names': len(names),
                'registered_names_invoked': sum(name in tested for name in names),
                'registered_names_invoked_non_enumeration': sum(any(
                    test['exercise_scope'] != 'registry-enumeration-only' for test in tested.get(name, [])) for name in names),
                'registered_names_with_hold_history': len(held_names),
                'uninvoked_registered_names_with_hold_history': sum(name not in tested for name in held_names),
                'registered_names_with_unproven_checker_path_history': len(unproven_names),
                'review_note': 'Hold notes describe exact invocation paths and historical batch decisions, not blanket command bans. '
                               'An invocation or a later prerequisite decision can coexist with an earlier hold. '
                               'No indexed review is not evidence that a command is dangerous. Native names and invocation patterns are separate.',
                'native_review_notes': dict(review_notes['native']),
                'review_notes_for_unregistered_udw_names': {name: notes for name, notes in review_notes['udw'].items()
                                                           if name not in names},
                'commands': [{'name': name, 'status': 'invoked' if name in tested else 'not-invoked',
                              'review_state': ('specific-hold-history-indexed' if name in held_names else
                                               ('completed-invocation-review-indexed' if name in reviewed_paths else
                                                'offline-checker-path-unproven' if name in unproven_names else
                                                'no-completed-invocation-review-indexed')),
                              'reviewed_paths': reviewed_paths.get(name, []),
                              'review_notes': review_notes['udw'].get(name, []),
                              'tests': tested.get(name, [])} for name in names]}
    dump(ROOT / 'coverage.json', coverage)
    (ROOT / 'runtime_commands.txt').write_text(''.join(name + '\n' for name in names), encoding='utf-8')
    counts = Counter(item['outcome'] for item in records)
    families = Counter(item['command_family'] for item in records)
    native_functions = [item for item in records if item['command_family'] == 'native-function-call']
    native_variables = [item for item in records if item['command_family'] == 'native-variable-read']
    scopes = Counter(item['exercise_scope'] for item in records)
    summary = {'generated_utc': datetime.now(timezone.utc).isoformat(), 'sessions': sessions,
               'transmissions_with_command_records': len(records), 'outcomes': dict(counts),
               'transmissions_confirmed_by_tx_files': sum(x['transmission_confirmed_by_tx_file'] for x in records),
               'registry_enumeration_records': sum(is_registry_enumeration(x['command']) for x in records),
               'non_enumeration_command_records': sum(not is_registry_enumeration(x['command']) for x in records),
               'command_records_by_family': dict(families),
               'exercise_scope_records': dict(scopes),
               'native_function_names_attempted': sorted({x['native_name'] for x in native_functions}),
               'native_function_names_with_return_marker': sorted({x['native_name'] for x in native_functions
                                                                   if x['native_function_return_seen']}),
               'native_variable_names_read': sorted({x['native_name'] for x in native_variables
                                                     if x['native_variable_value_seen']}),
               'unique_native_function_names_attempted': len({x['native_name'] for x in native_functions}),
               'unique_native_function_names_with_return_marker': len({x['native_name'] for x in native_functions
                                                                       if x['native_function_return_seen']}),
               'unique_native_variable_names_read': len({x['native_name'] for x in native_variables
                                                         if x['native_variable_value_seen']}),
               'semantic_capture_validation': {
                   'passed_records': sum(x.get('validation', {}).get('valid') is True for x in records),
                   'failed_records': sum(x.get('validation', {}).get('valid') is False for x in records),
                   'not_applied_records': sum('validation' not in x for x in records),
                   'note': 'Typed capture validation is separate from byte-integrity checks and full-NVM structural checks.'},
               'data_quality': {
                   'known_incomplete_response_records': sum(x['data_quality'] is not None and
                                                            x['data_quality'].get('complete') is False for x in records),
                   'note': 'Recovered catalog coverage does not make a damaged individual response complete.'},
               'post_capture_validation': {
                   'passed_records': sum(x['post_capture_validation'] is not None and
                                         x['post_capture_validation'].get('valid') is True for x in records),
                   'failed_records': sum(x['post_capture_validation'] is not None and
                                         x['post_capture_validation'].get('valid') is False for x in records),
                   'capture_failures_later_revalidated': sum(x.get('validation', {}).get('valid') is False and
                                                             x['post_capture_validation'] is not None and
                                                             x['post_capture_validation'].get('valid') is True for x in records),
                   'note': 'Later source-hash-bound assessments never replace historical status, validation, or outcome.'},
               'registry_complete': coverage['registry_complete'],
               'unique_registered_names': len(names),
               'registered_names_invoked': coverage['registered_names_invoked'],
               'registered_names_invoked_non_enumeration': coverage['registered_names_invoked_non_enumeration'],
               'registered_names_with_hold_history': coverage['registered_names_with_hold_history'],
               'uninvoked_registered_names_with_hold_history': coverage['uninvoked_registered_names_with_hold_history'],
               'registered_names_with_unproven_checker_path_history': coverage['registered_names_with_unproven_checker_path_history'],
               'coverage_note': 'Enumeration proves a name is registered, not that its command was tested. '
                                'Usage responses test argument validation, not the underlying operation. '
                                'Invocation means the expression was transmitted, not that every operation succeeded. '
                                'A zero wrapper return can also accompany an unregistered command. '
                                'Native functions and variable reads are counted separately from UDW batches. '
                                'Native function attempts do not prove registration; return markers identify resolved function calls. '
                                'Empty-argument, help, and compiled-out paths do not test the corresponding operational behavior. '
                                'Active sessions are listed but indexed only after closing or stopping.'}
    dump(ROOT / 'summary.json', summary)

    rows = ['# Physical command coverage\n',
            'Generated from raw COM16 captures. A command listed by the iterator has not necessarily been invoked. '
            'See summary.json for native functions, variable reads, and help/guard/stub path counts. '
            'Review holds apply to the cited path and batch; they can coexist with a later guard/stub test or '
            'a reviewed invocation. Absence of an indexed review is not a danger assessment.\n',
            '| Registered command | Actual invocation / response | Exact review notes |\n|---|---|---|\n']
    for entry in coverage['commands']:
        responses = '<br>'.join(render_test(item) for item in entry['tests']) or 'Not invoked'
        notes = '<br>'.join(render_review_note(note) for note in entry['review_notes'])
        if not notes and not entry['tests']:
            notes = ('Specific invocation review indexed: ' + ', '.join(
                f'[{source["source_review"]}]({source["source_review"]})' for source in entry['reviewed_paths'])
                if entry['reviewed_paths'] else 'No completed invocation review indexed')
        rows.append(f'| `{entry["name"]}` | {responses} | {notes} |\n')
    rows.extend(['\n## Native shell review decisions\n\n',
                 'These exact native names and explicitly written invocation patterns are separate from the UDW registry. '
                 'A missing-argument test does not exercise an operational path held in an earlier review.\n\n',
                 '| Native name / reviewed pattern | Actual invocation / response | Exact review notes |\n|---|---|---|\n'])
    for name, notes in sorted(review_notes['native'].items()):
        responses = '<br>'.join(render_test(item) for item in records if item['native_name'] == name) or 'No exact-name invocation indexed'
        rows.append(f'| `{html.escape(name)}` | {responses} | {"<br>".join(render_review_note(note) for note in notes)} |\n')
    (ROOT / 'command_index.md').write_text(''.join(rows), encoding='utf-8')
    print(json.dumps({key: value for key, value in summary.items() if key != 'sessions'}, indent=2))


if __name__ == '__main__':
    main()
