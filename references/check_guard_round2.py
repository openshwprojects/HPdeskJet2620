"""Conservative offline continuation of empty-path review; no serial access.

Unlike the first sweep, direct Thumb helpers are executed instruction by
instruction under the same unknown-value, stack-only-write restrictions.
Formatter-only handlers are also eligible. This is analysis, never UART data.
"""
from pathlib import Path
import hashlib
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_empty_argument_paths as base
from capstone.arm import ARM_OP_IMM
import unicorn.arm_const as arm

ROOT = Path(__file__).resolve().parents[1]


class ExpandedChecker(base.Checker):
    def call_return(self, address, size, value=None):
        self.known.difference_update({'r0', 'r1', 'r2', 'r3', 'r12'})
        if value is not None:
            self.uc.reg_write(arm.UC_ARM_REG_R0, value & 0xffffffff)
            self.known.add('r0')
        self.flags_known = False
        self.known.update({'pc', 'lr'})
        self.uc.reg_write(arm.UC_ARM_REG_LR, (address + size) | 1)
        self.uc.reg_write(arm.UC_ARM_REG_PC, (address + size) | 1)

    def code(self, uc, address, size, extra):
        if not 0xA0000000 <= address < 0xA4000000:
            return super().code(uc, address, size, extra)
        ins = next(self.md.disasm(bytes(uc.mem_read(address, size)), address), None)
        if ins is None:
            return super().code(uc, address, size, extra)
        if ins.mnemonic == 'bl' and ins.operands[0].type == ARM_OP_IMM:
            target = ins.operands[0].imm & 0xfffffffe
            if target not in {base.PARSER, base.FORMATTER}:
                self.current = ins
                self.before_known = set(self.known)
                self.before_sp = self.reg('sp')
                self.result['instructions'].append(f'{base.HEX(address)}: {ins.mnemonic} {ins.op_str}')
                self.result['calls'].append({'at': base.HEX(address), 'target': base.HEX(target),
                                             'method': 'execute helper instructions with conservative tracking'})
                self.known.update({'pc', 'lr'})
                return
            if target == base.FORMATTER and not self.result['parser_count']:
                self.result['instructions'].append(f'{base.HEX(address)}: {ins.mnemonic} {ins.op_str}')
                self.result['calls'].append({'at': base.HEX(address), 'target': base.HEX(target)})
                fmt = self.string(self.reg('r1')) if 'r1' in self.known else None
                if fmt is None or not self.output_args_known(fmt):
                    self.fail('standalone output could consume unknown data or pointers')
                    return
                self.result['outputs'].append({'at': base.HEX(address), 'format_address': base.HEX(self.reg('r1')), 'text': fmt})
                self.call_return(address, size)
                return
        return super().code(uc, address, size, extra)


def main():
    r = ROOT / 'references'
    raw = (ROOT / 'artifacts/ghs/dram.bin').read_bytes()
    inventory = json.loads((r / 'static_inventory.json').read_bytes())
    assert hashlib.sha256(raw).hexdigest() == inventory['provenance']['image_sha256']
    coverage_bytes = (r / 'coverage.json').read_bytes()
    pending = {x['name'] for x in json.loads(coverage_bytes)['commands'] if x['status'] == 'not-invoked'}
    descriptors = {x['name']: x for x in inventory['commands'] + inventory['hierarchical_commands_with_static_registration']}
    checker = ExpandedChecker(raw)
    cases, accepted, held = {}, [], []
    for name in sorted(pending):
        desc = descriptors[name]
        handler = int(desc['handler'], 16)
        if handler not in cases:
            cases[handler] = [checker.check(handler, value) for value in (0, -1)]
        paths = cases[handler]
        passed = all(x['returned'] and not x['failure'] for x in paths)
        if not passed:
            held.append({'name': name, 'handler': desc['handler'],
                         'reasons': sorted({x['failure'] or 'return not proved' for x in paths if x['failure'] or not x['returned']}),
                         'path_evidence': paths})
            continue
        accepted.append({'name': name, 'command': f'udws "{name};"', 'handler': desc['handler'],
                         'descriptor_address': desc['descriptor_address'],
                         'classification': 'reviewed-empty-argument-path' if any(x['parser_count'] for x in paths) else 'reviewed-software-only-empty-path',
                         'purpose': 'Capture the proved empty-argument path only; no operational argument coverage claimed.',
                         'side_effects_assessment': 'Only stack-local computation, parser/output bookkeeping, and known-argument output. No global/peripheral reads or writes, unknown calls, or unknown-state branch decisions were admitted.',
                         'evidence': {'checker': 'references/check_guard_round2.py', 'paths': paths},
                         'timeout': 20, 'hardware_access': False, 'physically_tested_by_this_review': False})
    result = {'schema_version': 1, 'provenance': {
        'image_sha256': hashlib.sha256(raw).hexdigest(),
        'coverage_sha256': hashlib.sha256(coverage_bytes).hexdigest(),
        'checker_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'base_checker_sha256': hashlib.sha256((r / 'check_empty_argument_paths.py').read_bytes()).hexdigest(),
        'kind': 'offline proof paths, not physical responses'},
        'summary': {'uninvoked_names_swept': len(pending), 'unique_handlers': len(cases),
                    'accepted_names': len(accepted), 'held_names': len(held)},
        'queries': accepted, 'held_for_further_review': held}
    (r / 'guard_round2_review.json').write_text(json.dumps(result, indent=2) + '\n')
    (r / '32-guard-commands.json').write_text(json.dumps(accepted, indent=2) + '\n')
    print(json.dumps(result['summary']))
    print('\n'.join(x['name'] for x in accepted))


if __name__ == '__main__':
    main()
