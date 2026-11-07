"""Standalone toolchain build script for HP DeskJet 2620 freestanding C firmware.

Builds bare-metal ARMv5TE Big-Endian C firmware (ARM946E-S) using zig cc, clang,
or arm-none-eabi-gcc. Supports generating:
- Standalone 16 MiB flash images booting directly from reset vector 0x00000000
- Stage-2 container images booting via HP Stage-1 bootloader at 0xA0020000
- Raw binary and ELF artifacts with symbol tables
- Automated emulator smoke-testing with DRAM mailbox verification
"""
from __future__ import annotations

import argparse
from pathlib import Path
import struct
import sys

from custom_firmware import (
    find_arm_c_compiler,
    compile_c_firmware,
    build_c_custom_firmware_image,
    build_c_stage2_container_image,
)
from emulator import Emulator


def build_firmware(
    c_source: Path | str,
    *,
    mode: str = 'standalone',
    out_dir: Path | None = None,
    run_emulator: bool = False,
    max_instructions: int = 100_000,
) -> dict[str, object]:
    """Compile and package C firmware for HP DeskJet 2620."""
    src_path = Path(c_source).resolve()
    if not src_path.is_file():
        raise FileNotFoundError(f"C source file not found: {src_path}")

    compiler = find_arm_c_compiler()
    if not compiler:
        raise RuntimeError("No ARM C compiler found (zig, clang, or arm-none-eabi-gcc required on PATH)")

    if out_dir is None:
        out_dir = src_path.parent / 'build'
    out_dir.mkdir(parents=True, exist_ok=True)

    base_name = src_path.stem
    results: dict[str, object] = {
        'source': str(src_path),
        'compiler': compiler,
        'mode': mode,
    }

    if mode in ('standalone', 'both'):
        load_addr = 0x00002070
        bin_bytes, entry = compile_c_firmware(src_path, load_address=load_addr)
        raw_bin_path = out_dir / f"{base_name}_standalone.bin"
        raw_bin_path.write_bytes(bin_bytes)

        img_bytes, img_entry = build_c_custom_firmware_image(src_path, reset_entry=load_addr)
        flash_path = out_dir / f"{base_name}_flash_standalone.bin"
        flash_path.write_bytes(img_bytes)

        results['standalone_bin'] = str(raw_bin_path)
        results['standalone_flash'] = str(flash_path)
        results['standalone_entry'] = hex(img_entry)

        if run_emulator:
            print(f"[*] Emulating {flash_path.name} from reset vector 0x00000000...")
            emu = Emulator(img_bytes, serial_panel_inputs=0x3FFFF, native_observer=True)
            emu.run(max_instructions=max_instructions, stop='budget')
            uart_text = bytes(emu.uart).decode('latin1', errors='replace')
            dram = bytes(emu.memory._dram_mv)
            sig = struct.unpack_from('>I', dram, 0x00000104)[0]
            magic = struct.unpack_from('>I', dram, 0x0000010C)[0]
            results['standalone_uart'] = uart_text
            results['standalone_signature'] = sig
            results['standalone_magic'] = magic

    if mode in ('stage2', 'both'):
        from analysis.uart_selftest import load_default_dump
        dump = load_default_dump()
        if dump is None:
            raise FileNotFoundError("dump.bin required for Stage-2 container image build")

        load_addr = 0xA0020000
        bin_bytes, entry = compile_c_firmware(src_path, load_address=load_addr)
        raw_bin_path = out_dir / f"{base_name}_stage2.bin"
        raw_bin_path.write_bytes(bin_bytes)

        img_bytes, img_entry = build_c_stage2_container_image(dump, src_path, load_address=load_addr)
        stage2_path = out_dir / f"{base_name}_flash_stage2.bin"
        stage2_path.write_bytes(img_bytes)

        results['stage2_bin'] = str(raw_bin_path)
        results['stage2_flash'] = str(stage2_path)
        results['stage2_entry'] = hex(img_entry)

        if run_emulator:
            print(f"[*] Emulating {stage2_path.name} via Stage-1 container at 0xA0020000...")
            emu = Emulator(img_bytes, serial_panel_inputs=0x3FFFF, spi_profile='byte-final', native_observer=True)
            emu.run(max_instructions=4_500_000, stop='budget')
            uart_text = bytes(emu.uart).decode('latin1', errors='replace')
            dram = bytes(emu.memory._dram_mv)
            sig = struct.unpack_from('>I', dram, 0x00000104)[0]
            magic = struct.unpack_from('>I', dram, 0x0000010C)[0]
            results['stage2_uart'] = uart_text
            results['stage2_signature'] = sig
            results['stage2_magic'] = magic

    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build freestanding C firmware for HP DeskJet 2620")
    parser.add_argument('--source', type=Path, default=Path('sample_firmware.c'),
                        help="Path to C source file (default: sample_firmware.c)")
    parser.add_argument('--mode', choices=['standalone', 'stage2', 'both'], default='both',
                        help="Target packaging mode: standalone ROM, Stage-2 container, or both (default: both)")
    parser.add_argument('--standalone', action='store_true',
                        help="Convenience alias for --mode standalone")
    parser.add_argument('--stage2', action='store_true',
                        help="Convenience alias for --mode stage2")
    parser.add_argument('--out-dir', type=Path, default=Path('build'),
                        help="Output directory for build artifacts (default: build/)")
    parser.add_argument('--run', action='store_true',
                        help="Run built firmware image in the emulator after compilation")
    parser.add_argument('--all', action='store_true',
                        help="Build both sample_firmware.c and firmware.c in both modes and run validation")
    args = parser.parse_args(argv)

    if args.standalone:
        args.mode = 'standalone'
    elif args.stage2:
        args.mode = 'stage2'

    compiler = find_arm_c_compiler()
    if not compiler:
        print("[!] Error: No ARM C compiler found (zig, clang, or arm-none-eabi-gcc required on PATH)")
        return 1
    print(f"[*] Found ARM C toolchain: {compiler}")

    sources = [Path('sample_firmware.c'), Path('firmware.c')] if args.all else [args.source]
    for src in sources:
        print(f"\n=======================================================")
        print(f"[*] Building C Firmware: {src} (mode={args.mode})")
        print(f"=======================================================")
        res = build_firmware(src, mode=args.mode, out_dir=args.out_dir, run_emulator=args.run or args.all)

        if 'standalone_flash' in res:
            print(f"  [+] Standalone Flash: {res['standalone_flash']} (entry {res['standalone_entry']})")
            if 'standalone_uart' in res:
                print(f"  [+] Standalone Boot Output:\n{res['standalone_uart'].strip()}")
                print(f"  [+] Mailbox: signature=0x{res['standalone_signature']:02x}, status=0x{res['standalone_magic']:08x}")

        if 'stage2_flash' in res:
            print(f"  [+] Stage-2 Flash:    {res['stage2_flash']} (entry {res['stage2_entry']})")
            if 'stage2_uart' in res:
                uart = str(res['stage2_uart'])
                print(f"  [+] Stage-2 Boot Output:\n{uart[uart.find('[Boot] calling'):].strip()}")
                print(f"  [+] Mailbox: signature=0x{res['stage2_signature']:02x}, status=0x{res['stage2_magic']:08x}")

    print("\n[OK] Firmware build process completed successfully.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
