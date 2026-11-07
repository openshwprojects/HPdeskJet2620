"""Observe an unmodified reset run while long software loader loops execute.

This analysis runner selects explicit panel/SPI board profiles. Its observer
thread only reads Python counters; it never touches CPU registers or memory.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import threading
import time

from emulator import Emulator, REGISTERS
from unicorn.arm_const import UC_ARM_REG_CPSR


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--max-instructions', type=int, default=300_000_000)
    parser.add_argument('--spi-profile', choices=('byte-final', 'wide-final'), required=True)
    parser.add_argument('--native-observer', action='store_true',
                        help='use optional C instruction accounting; CPU and devices are unchanged')
    parser.add_argument('--prefix', type=Path, default=Path('artifacts/deep-boot'))
    parser.add_argument('--verify-section', action='append', default=[], metavar='NAME',
                        help='after stopping, compare a loaded section to independent offline extraction')
    args = parser.parse_args()
    dump_path = Path(__file__).resolve().parents[1] / 'dump.bin'
    dump = dump_path.read_bytes()
    section_image = None
    if args.verify_section:
        from analysis.firmware_image import FirmwareImage
        section_image = FirmwareImage(dump)
        for name in args.verify_section:
            if name not in section_image.by_name:
                parser.error(f'unknown section {name!r}')
    paths = {suffix: args.prefix.with_suffix(suffix)
             for suffix in ('.uart', '.txt', '.json', '.progress.json', '.jsonl')}
    if any(path.resolve() == dump_path.resolve() or
           path.exists() and path.samefile(dump_path) for path in paths.values()):
        parser.error('output must not overwrite the source dump')
    args.prefix.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    done = threading.Event()
    with paths['.uart'].open('wb') as uart, paths['.jsonl'].open('w', encoding='utf-8') as trace:
        def output(value):
            uart.write(bytes((value,)))
            uart.flush()
            print(chr(value), end='', flush=True)

        def event(record):
            # Retain every write, CP15 transition and completed UART frame.
            # High-frequency status/timer reads are omitted from this log.
            if record['kind'] != 'mmio-read':
                trace.write(json.dumps(record) + '\n')

        emulator = Emulator(dump, serial_panel_inputs=0x3ffff,
                            spi_profile=args.spi_profile, native_observer=args.native_observer,
                            on_uart=output, on_event=event)

        def observe():
            while not done.wait(10):
                # Native counters can be newer than the last Python device
                # observation. Read only the shared accounting state; never
                # call Unicorn or synchronize devices from this thread.
                native = emulator._native_observer
                if native is None:
                    count = emulator.instructions
                    pc = emulator.recent_pcs[-1] if emulator.recent_pcs else None
                else:
                    count = native.state.instructions
                    recent = native.state.recent_total
                    pc = native.state.recent[(recent - 1) & 31] if recent else None
                paths['.progress.json'].write_text(json.dumps({
                    'running': True,
                    'instructions': count,
                    'last_pc': hex(pc) if pc is not None else None,
                    'uart_bytes': len(emulator.uart),
                    'elapsed_seconds': round(time.monotonic() - started, 1),
                }), encoding='utf-8')

        observer = threading.Thread(target=observe, daemon=True)
        observer.start()
        try:
            result = emulator.run(args.max_instructions, stop='budget')
        finally:
            done.set()
            observer.join()
    report = asdict(result)
    report['pc'] = hex(result.pc)
    report['uart'] = result.uart.decode('latin-1')
    report.update({
        'dump_sha256': emulator.dump_sha256,
        'flash_unchanged': emulator.memory.check_flash(),
        'elapsed_seconds': round(time.monotonic() - started, 1),
        'uart_bytes': len(result.uart),
        'virtual_ticks': emulator.board.ticks,
        'recent_pcs': [hex(pc) for pc in emulator.recent_pcs],
        'spi_profile': args.spi_profile, 'spi_miso_pull': 1,
        'native_observer': args.native_observer,
        'spi_window_registers': emulator.board.spi.window_registers,
        'spi_transfers': emulator.board.spi.transfers,
        'spi_flash_transactions': emulator.spi_flash.transactions,
        'serial_panel_inputs': 0x3ffff,
        'panel_frames': emulator.board.panel.endpoint.frames,
        'uart_tx_pending': emulator.board.uart_tx_pending,
        'registers': {name: hex(emulator.uc.reg_read(reg)) for name, reg in
                      zip([f'r{i}' for i in range(13)] + ['sp', 'lr', 'pc'], REGISTERS)},
        'cpsr': hex(emulator.uc.reg_read(UC_ARM_REG_CPSR)),
    })
    if args.verify_section:
        comparisons = []
        for name in args.verify_section:
            section = section_image.by_name[name]
            expected = section_image.extract(name)
            actual = bytes(emulator.uc.mem_read(section.address, section.size))
            comparisons.append({'section': name, 'address': hex(section.address),
                                'bytes': section.size, 'matches': actual == expected,
                                'expected_sha256': hashlib.sha256(expected).hexdigest(),
                                'actual_sha256': hashlib.sha256(actual).hexdigest()})
        report['section_comparisons'] = comparisons
    paths['.json'].write_text(json.dumps(report, indent=2), encoding='utf-8')
    paths['.progress.json'].write_text(json.dumps({
        'running': False, 'reason': result.reason,
        'instructions': result.instructions, 'last_pc': hex(result.pc),
        'uart_bytes': len(result.uart), 'elapsed_seconds': report['elapsed_seconds'],
    }), encoding='utf-8')
    paths['.txt'].write_bytes((report['uart'].replace('\n\r', '\n').rstrip('\n') + '\n').encode('utf-8'))
    print(f'\n[{result.reason}] {result.instructions:,} instructions, '
          f'{len(result.uart)} UART bytes, PC={result.pc:#x}; {result.error or "no fault"}')


if __name__ == '__main__':
    main()
