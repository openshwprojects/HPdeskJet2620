"""Read-only disassembly helper for the main image's front-panel driver.

Uses the bounded container parser directly, without depending on a particular
extraction-directory filename. No firmware execution or patching is performed.
"""
from pathlib import Path
import re
import sys

from capstone import Cs, CS_ARCH_ARM, CS_MODE_BIG_ENDIAN, CS_MODE_THUMB
from analysis.firmware_image import FirmwareImage


IMAGE = Path(__file__).resolve().parents[1] / 'dump.bin'


if __name__ == '__main__':
    start, end = (int(value, 0) for value in sys.argv[1:3])
    firmware = FirmwareImage(IMAGE.read_bytes())
    base = firmware.by_name['.text'].address
    data = firmware.extract('.text')
    if not base <= start < end <= base + len(data):
        raise SystemExit('requested disassembly range lies outside .text')
    decoder = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_BIG_ENDIAN)
    decoder.skipdata = True
    for instruction in decoder.disasm(data[start-base:end-base], start):
        annotation = ''
        match = re.search(r'\[pc, #(0x[0-9a-f]+|\d+)\]', instruction.op_str)
        if match:
            target = ((instruction.address + 4) & ~3) + int(match[1], 0)
            if base <= target <= base + len(data) - 4:
                value = int.from_bytes(data[target-base:target-base+4], 'big')
                annotation = f' ; [{target:#x}]={value:#010x}'
        print(f'{instruction.address:#010x}: {instruction.mnemonic:8} '
              f'{instruction.op_str}{annotation}')
