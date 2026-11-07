"""Read-only trace of board-ID initialization using normal firmware execution."""
from pathlib import Path
import json
from unicorn import UC_HOOK_CODE
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_SP
from emulator import Emulator


def main():
    dump = Path('dump.bin').read_bytes()
    emulator = Emulator(dump, eeprom=bytes([0xff]) * 0x10000)
    snapshots = []
    registers = (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)

    def trace(uc, address, size, _):
        regs = [uc.reg_read(reg) for reg in registers]
        event = {'pc': hex(address), 'r0_r3': [hex(value) for value in regs]}
        if address == 0xa000c9f6:
            event['region'] = bytes(uc.mem_read(regs[0], 8)).hex()
            event['default'] = bytes(uc.mem_read(regs[1], regs[3])).hex()
            event['mode'] = int.from_bytes(uc.mem_read(uc.reg_read(UC_ARM_REG_SP), 4), 'big')
        snapshots.append(event)

    for address in (0xa000c9f6, 0xa000cbbc, 0xa0009880, 0xa00099da):
        emulator.uc.hook_add(UC_HOOK_CODE, trace, begin=address, end=address)
    result = emulator.run(max_instructions=3_000_000, stop='budget')
    ranges = ((0xa0011704, 12), (0xa0011924, 128), (0x20001780, 256),
              (0x200019b0, 48), (0x20004170, 32), (0x20004b58, 208))
    print(json.dumps({'reason': result.reason, 'error': result.error,
                      'uart': result.uart.decode('latin1'), 'snapshots': snapshots,
                      'memory': {hex(address): bytes(emulator.uc.mem_read(address, size)).hex()
                                 for address, size in ranges}}, indent=2))


if __name__ == '__main__':
    main()
