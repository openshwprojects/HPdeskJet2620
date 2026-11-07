"""Small offline disassembly aid; never accesses the printer."""
import argparse
import json
import re
from pathlib import Path

from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_ARM, CS_MODE_BIG_ENDIAN

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('targets', nargs='+')
    parser.add_argument('--bytes', type=lambda s: int(s, 0), default=256)
    parser.add_argument('--arm', action='store_true')
    args = parser.parse_args()
    data = (ROOT / 'artifacts/ghs/dram.bin').read_bytes()
    inventory = json.loads((ROOT / 'references/static_inventory.json').read_text())
    handlers = {x['name']: int(x['handler'], 16) for x in inventory['commands']}
    md = Cs(CS_ARCH_ARM, CS_MODE_BIG_ENDIAN | (CS_MODE_ARM if args.arm else CS_MODE_THUMB))
    for target in args.targets:
        address = int(target, 0) if target.startswith('0x') else handlers[target]
        address &= ~1
        offset = address & 0x3ffffff
        print(f'\n{target} @ {address:#010x}')
        for insn in md.disasm(data[offset:offset + args.bytes], address):
            annotation = ''
            match = re.search(r'\[pc, #(0x[0-9a-f]+|\d+)\]', insn.op_str)
            if match:
                literal = ((insn.address + (8 if args.arm else 4)) & ~3) + int(match[1], 0)
                at = literal & 0x3ffffff
                value = int.from_bytes(data[at:at + 4], 'big')
                annotation = f' ; [{literal:#010x}]={value:#010x}'
                if 0xa0000000 <= value < 0xa4000000:
                    raw = data[value & 0x3ffffff:(value & 0x3ffffff) + 240].split(b'\0', 1)[0]
                    if raw and all(32 <= x <= 126 or x in (9, 10, 13) for x in raw):
                        annotation += ' ' + repr(raw.decode('ascii'))
            print(f'{insn.address:#010x}: {insn.mnemonic:8} {insn.op_str}{annotation}')


if __name__ == '__main__':
    main()
