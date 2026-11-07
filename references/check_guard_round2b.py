"""Offline continuation with actual ARM helper instructions, not helper stubs."""
from pathlib import Path
import hashlib
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_empty_argument_paths as base
from check_guard_round2 import ExpandedChecker
from capstone import Cs, CS_ARCH_ARM, CS_MODE_ARM, CS_MODE_THUMB, CS_MODE_BIG_ENDIAN
from capstone.arm import ARM_OP_IMM, ARM_CC_AL, ARM_CC_INVALID
import unicorn.arm_const as arm

ROOT = Path(__file__).resolve().parents[1]


class InterworkingChecker(ExpandedChecker):
    def __init__(self, raw):
        super().__init__(raw)
        self.thumb_decoder = self.md
        self.arm_decoder = Cs(CS_ARCH_ARM, CS_MODE_ARM | CS_MODE_BIG_ENDIAN)
        self.arm_decoder.detail = True

    def code(self, uc, address, size, extra):
        cpsr = uc.reg_read(arm.UC_ARM_REG_CPSR)
        thumb = bool(cpsr & 32)
        self.md = self.thumb_decoder if thumb else self.arm_decoder
        if 0xA0000000 <= address < 0xA4000000:
            ins = next(self.md.disasm(bytes(uc.mem_read(address, size)), address), None)
            if ins and not thumb and ins.cc not in (ARM_CC_AL, ARM_CC_INVALID):
                if not self.flags_known:
                    self.fail('conditional ARM instruction depends on unknown flags')
                    return
                n, z, c, v = (bool(cpsr & (1 << bit)) for bit in (31, 30, 29, 28))
                conditions = {1:z, 2:not z, 3:c, 4:not c, 5:n, 6:not n,
                              7:v, 8:not v, 9:c and not z, 10:not c or z,
                              11:n == v, 12:n != v, 13:not z and n == v, 14:z or n != v}
                if ins.cc not in conditions:
                    self.fail('unsupported ARM condition code')
                    return
                if not conditions[ins.cc]:
                    self.result['instructions'].append(f'{base.HEX(address)}: {ins.mnemonic} {ins.op_str} [condition false; no state change]')
                    return
            if (ins and ins.mnemonic == 'bl' and ins.operands[0].type == ARM_OP_IMM
                    and (ins.operands[0].imm & 0xfffffffe) == base.PARSER and 'r1' in self.known
                    and self.string(self.reg('r1')) in {'%*;', '*;'}):
                # Custom UDW scan conversion, not libc scanf: '*' sets flag4
                # at A02C3ABE; ';' selects the string-to-semicolon conversion
                # at A02C3B1C. A02C3BC4..3BD2 suppress the destination and
                # A02C3C58..3C6E skip all destination stores when it is null.
                self.result['instructions'].append(f'{base.HEX(address)}: {ins.mnemonic} {ins.op_str}')
                fmt = self.string(self.reg('r1'))
                self.result['calls'].append({'at': base.HEX(address), 'target': base.HEX(base.PARSER),
                                             'method': 'reviewed no-destination suppressed conversion or literal match'})
                self.result['parser_count'] += 1
                self.result['parser_formats'].append(fmt)
                self.result.setdefault('suppressed_conversion_evidence', []).append({
                    'at': base.HEX(address), 'format_address': base.HEX(self.reg('r1')),
                    'format': fmt, 'literal_only_branch': ['0xa02c3a30', '0xa02c3a34', '0xa02c3a36', '0xa02c3a58'],
                    'note': '*; has no percent sign: it is literal matching, not a conversion. No destination is consumed or written.',
                    'suppression_flag_set': '0xa02c3abe',
                    'conversion_selector': '0xa02c3b1c',
                    'destination_suppressed': ['0xa02c3bc4', '0xa02c3bc8', '0xa02c3bd2'],
                    'destination_stores_skipped': ['0xa02c3c58', '0xa02c3c5c', '0xa02c3c68', '0xa02c3c6e']})
                self.call_return(address, size, self.result['parser_return'])
                return
            if ins and ins.mnemonic == 'blx' and ins.operands[0].type == ARM_OP_IMM:
                if ins.cc not in (ARM_CC_AL, ARM_CC_INVALID) and not self.flags_known:
                    self.fail('conditional interworking call depends on unknown flags')
                    return
                self.current = ins
                self.before_known = set(self.known)
                self.before_sp = self.reg('sp')
                self.result['instructions'].append(f'{base.HEX(address)}: {ins.mnemonic} {ins.op_str}')
                self.result['calls'].append({'at': base.HEX(address), 'target': base.HEX(ins.operands[0].imm & 0xffffffff),
                                             'method': 'execute ARM/Thumb helper instructions with conservative tracking'})
                self.known.update({'pc', 'lr'})
                return
        return super().code(uc, address, size, extra)


def main():
    r = ROOT / 'references'
    raw = (ROOT / 'artifacts/ghs/dram.bin').read_bytes()
    inventory = json.loads((r / 'static_inventory.json').read_bytes())
    assert hashlib.sha256(raw).hexdigest() == inventory['provenance']['image_sha256']
    baseline = json.loads((r / 'guard_round2_review.json').read_bytes())
    descriptors = {x['name']: x for x in inventory['commands'] + inventory['hierarchical_commands_with_static_registration']}
    checker = InterworkingChecker(raw)
    cases, accepted, held = {}, [], []
    for old in baseline['held_for_further_review']:
        name = old['name']; desc = descriptors[name]; handler = int(desc['handler'], 16)
        if handler not in cases:
            cases[handler] = [checker.check(handler, value) for value in (0, -1)]
        paths = cases[handler]
        if not all(x['returned'] and not x['failure'] for x in paths):
            held.append({'name': name, 'handler': desc['handler'],
                         'reasons': sorted({x['failure'] or 'return not proved' for x in paths if x['failure'] or not x['returned']}),
                         'path_evidence': paths})
            continue
        accepted.append({'name': name, 'command': f'udws "{name};"', 'handler': desc['handler'],
                         'descriptor_address': desc['descriptor_address'],
                         'classification': 'reviewed-empty-argument-path' if any(x['parser_count'] for x in paths) else 'reviewed-software-only-empty-path',
                         'purpose': 'Capture the proved empty-argument path only; no operational argument coverage claimed.',
                         'side_effects_assessment': 'Only stack-local computation, parser/output bookkeeping, and known-argument output. ARM/Thumb helper instructions executed with conservative tracking; no global/peripheral reads or writes, unknown calls, or unknown-state decisions admitted.',
                         'evidence': {'checker': 'references/check_guard_round2b.py', 'paths': paths},
                         'timeout': 20, 'hardware_access': False, 'physically_tested_by_this_review': False})
    result = {'schema_version': 1, 'provenance': {
        'image_sha256': hashlib.sha256(raw).hexdigest(),
        'baseline_review_sha256': hashlib.sha256((r/'guard_round2_review.json').read_bytes()).hexdigest(),
        'checker_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'dependencies': {name: hashlib.sha256((r/name).read_bytes()).hexdigest() for name in ['check_guard_round2.py','check_empty_argument_paths.py']},
        'kind': 'offline instruction proofs, not physical responses'},
        'summary': {'remaining_names_swept': len(baseline['held_for_further_review']), 'unique_handlers': len(cases),
                    'accepted_names': len(accepted), 'held_names': len(held)},
        'queries': accepted, 'held_for_further_review': held}
    (r / 'guard_round2b_review.json').write_text(json.dumps(result, indent=2) + '\n')
    (r / '32b-guard-commands.json').write_text(json.dumps(accepted, indent=2) + '\n')
    print(json.dumps(result['summary']))
    print('\n'.join(x['name'] for x in accepted))


if __name__ == '__main__':
    main()
