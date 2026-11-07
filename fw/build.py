"""Build the HPDJ bootloader and application with arm-none-eabi-gcc.

    python -m fw.build                      # build/ : bl.img, app.img, hpdj_flash.bin
    python -m fw.build --app-version 1.0.1 --no-flash --out build/v2
    python -m fw.build --no-flash --app-src fw/examples/hello --out build/hello
    python -m fw.build --emulate            # also boot the result in the emulator

The toolchain is located with custom_firmware.find_arm_c_compiler() (ARM_CC
environment variable, PATH, or the Arduino arm-none-eabi-gcc install). zig cc
and clang are accepted as well, but only gcc is exercised regularly.

Compiles run in parallel and objects are cached by content hash under
build/.objcache (compiler version, flags, source and all headers hashed), so
rebuilding an application after an edit recompiles only what changed.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import image as fwimage
from .image import (
    APP_CODE_MAX, APP_DATA_ADDR, APP_DATA_SIZE, APP_LOAD_ADDR, APP_STACK_TOP,
    BL_CODE_MAX, BL_DATA_ADDR, BL_DATA_SIZE, BL_LOAD_ADDR, BL_STACK_TOP,
    IMAGE_TYPE_APP, IMAGE_TYPE_BL, FW_DIR, ROOT_DIR, make_image, version_str, version_u32,
)

sys.path.insert(0, str(ROOT_DIR))
from custom_firmware import find_arm_c_compiler  # noqa: E402

COMMON_DIR = FW_DIR / 'common'
BL_DIR = FW_DIR / 'bl'
APP_DIR = FW_DIR / 'app'
DEFAULT_BASE_DUMP = ROOT_DIR / 'dump.bin'
DEFAULT_OBJ_CACHE = ROOT_DIR / 'build' / '.objcache'
DEFAULT_JOBS = max(1, min(8, os.cpu_count() or 1))

LINKER_TEMPLATE = """\
ENTRY(_start)
MEMORY
{{
    CODE (rx) : ORIGIN = {code_origin:#x}, LENGTH = {code_len:#x}
    DATA (rw) : ORIGIN = {data_origin:#x}, LENGTH = {data_len:#x}
}}
__stack_top = {stack_top:#x};
SECTIONS
{{
    .text : {{ KEEP(*(.text.entry)) *(.text .text.*) . = ALIGN(4); }} > CODE
    .rodata : {{ *(.rodata .rodata.*) . = ALIGN(4); }} > CODE
    .data : {{ __data_start = .; *(.data .data.*) . = ALIGN(4); __data_end = .; }} > DATA AT> CODE
    __data_lma = LOADADDR(.data);
    .bss (NOLOAD) : {{ __bss_start = .; *(.bss .bss.*) *(COMMON) . = ALIGN(4); __bss_end = .; }} > DATA
    /DISCARD/ : {{ *(.ARM.exidx*) *(.ARM.extab*) *(.comment) *(.note*) }}
}}
"""


@dataclass(frozen=True)
class Target:
    name: str
    src_dir: Path
    image_type: int
    code_origin: int
    code_len: int
    data_origin: int
    data_len: int
    stack_top: int
    image_name: str


TARGETS = {
    'bl': Target('bl', BL_DIR, IMAGE_TYPE_BL, BL_LOAD_ADDR, BL_CODE_MAX, BL_DATA_ADDR, BL_DATA_SIZE,
                 BL_STACK_TOP, 'hpdj-bl'),
    'app': Target('app', APP_DIR, IMAGE_TYPE_APP, APP_LOAD_ADDR, APP_CODE_MAX, APP_DATA_ADDR, APP_DATA_SIZE,
                  APP_STACK_TOP, 'hpdj-app'),
}


class Toolchain:
    def __init__(self, compiler: str | None = None):
        cc = compiler or find_arm_c_compiler()
        if not cc:
            raise RuntimeError('no ARM C compiler found: set ARM_CC or install arm-none-eabi-gcc / zig / clang')
        self.cc = cc
        name = Path(cc).name.lower()
        self.kind = 'zig' if 'zig' in name else 'clang' if 'clang' in name else 'gcc'
        bindir = str(Path(cc).parent)

        def tool(*candidates: str) -> str | None:
            for c in candidates:
                p = shutil.which(c, path=bindir) or shutil.which(c)
                if p:
                    return p
            return None

        if self.kind == 'zig':
            self.objcopy = [cc, 'objcopy']
        else:
            oc = tool('arm-none-eabi-objcopy', 'llvm-objcopy', 'objcopy')
            if not oc:
                raise RuntimeError('objcopy not found next to the compiler')
            self.objcopy = [oc]
        self.objdump = tool('arm-none-eabi-objdump', 'llvm-objdump')
        self.size = tool('arm-none-eabi-size', 'llvm-size')
        self._version: str | None = None

    def target_flags(self) -> list[str]:
        if self.kind == 'zig':
            return ['cc', '-target', 'armeb-freestanding-eabi', '-mcpu=arm946e_s']
        if self.kind == 'clang':
            return ['-target', 'armeb-none-eabi', '-mcpu=arm946e-s']
        return ['-mbig-endian', '-mcpu=arm946e-s', '-marm']

    def version(self) -> str:
        if self._version is None:
            try:
                out = subprocess.run([self.cc, *(['cc'] if self.kind == 'zig' else []), '--version'],
                                     capture_output=True, text=True, check=False).stdout
                self._version = out.splitlines()[0] if out else self.cc
            except OSError:
                self._version = self.cc
        return self._version


def _run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f'command failed ({p.returncode}): {" ".join(cmd)}\n{p.stdout}\n{p.stderr}')
    return p.stderr.strip()


def git_short_hash() -> str:
    try:
        out = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=ROOT_DIR, capture_output=True,
                             text=True, check=False)
        return out.stdout.strip() or 'nogit'
    except OSError:
        return 'nogit'


@dataclass
class BuildResult:
    target: str
    elf: Path
    binary: Path
    image: Path
    map: Path
    entry: int
    payload_size: int
    version: int
    sequence: int
    image_bytes: bytes
    compiled: int = 0      # objects compiled (the rest came from the cache)


def _headers_digest(dirs: list[Path]) -> str:
    h = hashlib.sha256()
    for d in dirs:
        for hdr in sorted(d.glob('*.h')):
            h.update(hdr.name.encode())
            h.update(hdr.read_bytes())
    return h.hexdigest()


def _compile_objects(tc: Toolchain, jobs: list[tuple[Path, list[str], str]], obj_cache: Path,
                     max_workers: int) -> tuple[list[Path], int, list[str]]:
    """Compile (source, flags, headers digest) jobs in parallel through the object cache.

    Returns the object paths in job order, how many were actually compiled,
    and any compiler diagnostics.
    """
    obj_cache.mkdir(parents=True, exist_ok=True)
    cc_version = tc.version()
    plan: list[tuple[Path, Path, list[str]]] = []
    objects: list[Path] = []
    for src, flags, headers_digest in jobs:
        key = hashlib.sha256()
        key.update(cc_version.encode())
        key.update('\0'.join(flags).encode())
        key.update(headers_digest.encode())
        key.update(src.name.encode())
        key.update(src.read_bytes())
        obj = obj_cache / f'{src.stem}-{key.hexdigest()[:24]}.o'
        objects.append(obj)
        if not obj.exists():
            plan.append((src, obj, flags))

    diagnostics: list[str] = []

    def compile_one(item: tuple[Path, Path, list[str]]) -> str:
        src, obj, flags = item
        tmp = obj.with_suffix(f'.{os.getpid()}.tmp.o')
        err = _run([tc.cc, *flags, '-c', str(src), '-o', str(tmp)])
        os.replace(tmp, obj)
        return err

    if plan:
        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
            for err in pool.map(compile_one, plan):
                if err:
                    diagnostics.append(err)
    return objects, len(plan), diagnostics


def build_target(target: Target, out_dir: Path, tc: Toolchain, *, version: int, sequence: int,
                 build_time: int, build_str: str, extra_cflags: list[str] | None = None,
                 obj_cache: Path = DEFAULT_OBJ_CACHE, jobs: int = DEFAULT_JOBS) -> BuildResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    common_sources = sorted(COMMON_DIR.glob('*.c')) + sorted(COMMON_DIR.glob('*.S'))
    target_sources = sorted(target.src_dir.glob('*.c')) + sorted(target.src_dir.glob('*.S'))
    base_cflags = [
        *tc.target_flags(), '-O2', '-g', '-std=gnu11', '-ffreestanding', '-nostdlib', '-nostartfiles',
        '-fno-builtin', '-fno-common', '-ffunction-sections', '-fdata-sections', '-fno-strict-aliasing',
        '-Wall', '-Wextra', '-Wno-unused-parameter', '-Wno-main',
        f'-I{COMMON_DIR}', *(extra_cflags or []),
    ]
    # Version defines only reach the target's own sources, so fw/common objects
    # are shared between builds through the cache.
    defines = [
        f'-DFW_VERSION_U32={version:#x}U',
        f'-DFW_VERSION_STR="{version_str(version)}"',
        f'-DFW_SEQUENCE={sequence}U',
        f'-DFW_BUILD_TIME={build_time}U',
        f'-DFW_BUILD_STR="{build_str}"',
        f'-DFW_TARGET_{target.name.upper()}=1',
    ]
    target_cflags = [*base_cflags, f'-I{target.src_dir}', *defines]
    # Common sources only see fw/common headers, so their cache key ignores the
    # target directory and the bootloader and application share those objects.
    common_digest = _headers_digest([COMMON_DIR])
    target_digest = _headers_digest([COMMON_DIR, target.src_dir])
    jobs_list = [(src, base_cflags, common_digest) for src in common_sources] + \
        [(src, target_cflags, target_digest) for src in target_sources]
    objects, compiled, diagnostics = _compile_objects(tc, jobs_list, obj_cache, jobs)
    for diag in diagnostics:
        print(diag, file=sys.stderr)

    ld_path = out_dir / f'{target.name}.ld'
    ld_path.write_text(LINKER_TEMPLATE.format(
        code_origin=target.code_origin, code_len=target.code_len,
        data_origin=target.data_origin, data_len=target.data_len, stack_top=target.stack_top))
    elf = out_dir / f'{target.name}.elf'
    map_path = out_dir / f'{target.name}.map'
    ldflags = [*tc.target_flags(), '-nostdlib', '-nostartfiles', '-Wl,--gc-sections', '-Wl,--nmagic',
               f'-Wl,-Map,{map_path}', f'-Wl,-T,{ld_path}']
    err = _run([tc.cc, *ldflags, *map(str, objects), '-o', str(elf)])
    if err:
        print(err, file=sys.stderr)

    binary = out_dir / f'{target.name}.bin'
    _run([*tc.objcopy, '-O', 'binary', str(elf), str(binary)])
    elf_bytes = elf.read_bytes()
    if elf_bytes[:4] != b'\x7fELF':
        raise RuntimeError('linker did not produce an ELF file')
    entry = int.from_bytes(elf_bytes[24:28], 'big' if elf_bytes[5] == 2 else 'little')
    payload = binary.read_bytes()
    img = make_image(payload, image_type=target.image_type, version=version, sequence=sequence,
                     load_address=target.code_origin, entry_address=entry, build_time=build_time,
                     name=target.image_name)
    image_path = out_dir / f'{target.name}.img'
    image_path.write_bytes(img)
    return BuildResult(target.name, elf, binary, image_path, map_path, entry, len(payload) + (-len(payload) % 4),
                       version, sequence, img, compiled)


def build_all(out_dir: Path, *, base_dump: Path | None = DEFAULT_BASE_DUMP, bl_version: str = '1.0.0',
              app_version: str = '1.0.0', app_sequence: int | None = None, bl_sequence: int = 1, slot: int = 0,
              neutralize_hp_update: bool = True, compiler: str | None = None,
              extra_cflags: list[str] | None = None, build_time: int | None = None,
              write_flash: bool = True, app_src: Path | None = None, build_bl: bool = True,
              obj_cache: Path = DEFAULT_OBJ_CACHE, jobs: int = DEFAULT_JOBS,
              flash_out: Path | None = None, slot_b_image: Path | None = None) -> dict:
    """Build the application (and the bootloader unless ``build_bl`` is False).

    ``app_sequence`` defaults to the build time so every new build outranks the
    previous one in the bootloader's slot selection. ``app_src`` builds the
    application from another source directory. The flash image needs both.
    ``flash_out`` names the 16 MiB image (default <out_dir>/hpdj_flash.bin);
    ``slot_b_image`` is an existing application .img placed in the other slot
    (the bootloader boots whichever of the two has the higher sequence).
    """
    tc = Toolchain(compiler)
    build_time = int(time.time()) if build_time is None else build_time
    if app_sequence is None:
        app_sequence = build_time
    if write_flash and base_dump is not None and not build_bl:
        raise ValueError('the flash image needs the bootloader: use build_bl=True or write_flash=False')
    build_str = f'{time.strftime("%Y-%m-%d %H:%M", time.gmtime(build_time))} UTC {git_short_hash()}'
    app_target = TARGETS['app']
    if app_src is not None:
        app_src = Path(app_src).resolve()
        if not app_src.is_dir() or not list(app_src.glob('*.c')):
            raise FileNotFoundError(f'application source directory with .c files expected: {app_src}')
        app_target = dataclasses.replace(app_target, src_dir=app_src, image_name=app_src.name[:20])
    common = dict(build_time=build_time, build_str=build_str, extra_cflags=extra_cflags, obj_cache=obj_cache,
                  jobs=jobs)
    results: dict[str, BuildResult] = {}
    if build_bl:
        results['bl'] = build_target(TARGETS['bl'], out_dir, tc, version=version_u32(bl_version),
                                     sequence=bl_sequence, **common)
    results['app'] = build_target(app_target, out_dir, tc, version=version_u32(app_version), sequence=app_sequence,
                                  **common)
    manifest: dict = {
        'compiler': tc.version(),
        'build_time': build_time,
        'build_str': build_str,
        'targets': {},
    }
    for name, res in results.items():
        manifest['targets'][name] = {
            'image': str(res.image), 'elf': str(res.elf), 'entry': f'{res.entry:#010x}',
            'payload_size': res.payload_size, 'version': version_str(res.version), 'sequence': res.sequence,
            'image_sha256': hashlib.sha256(res.image_bytes).hexdigest(),
            'description': fwimage.describe_image(res.image_bytes),
            'objects_compiled': res.compiled,
        }
    flash_path = None
    if write_flash and base_dump is not None:
        base = Path(base_dump).read_bytes()
        flash = bytearray(fwimage.build_flash_image(base, results['bl'].image_bytes, results['app'].image_bytes,
                                                    slot=slot, neutralize_hp_update=neutralize_hp_update))
        other_desc = None
        if slot_b_image is not None:
            other = Path(slot_b_image).read_bytes()
            fwimage.write_slot(flash, 1 - slot, other)
            other_desc = fwimage.describe_image(other)
        flash = bytes(flash)
        flash_path = Path(flash_out) if flash_out is not None else out_dir / 'hpdj_flash.bin'
        flash_path.write_bytes(flash)
        manifest['flash'] = {
            'path': str(flash_path), 'size': len(flash), 'sha256': hashlib.sha256(flash).hexdigest(),
            'base_dump_sha256': hashlib.sha256(base).hexdigest(), 'app_slot': 'AB'[slot],
            'other_slot_image': other_desc,
            'neutralize_hp_update': neutralize_hp_update,
        }
    (out_dir / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    manifest['_results'] = results
    manifest['_flash_path'] = flash_path
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Build the HPDJ bootloader + application')
    parser.add_argument('--out', type=Path, default=ROOT_DIR / 'build', help='output directory (default: build/)')
    parser.add_argument('--base', type=Path, default=DEFAULT_BASE_DUMP,
                        help='stock 16 MiB dump providing HP stage-1 (default: dump.bin)')
    parser.add_argument('--bl-version', default='1.0.0')
    parser.add_argument('--app-version', default='1.0.0')
    parser.add_argument('--app-seq', type=int, default=None,
                        help='application sequence number (higher wins; default: build time, so each build wins)')
    parser.add_argument('--app-src', type=Path, default=None,
                        help='build the application from this directory instead of fw/app')
    parser.add_argument('--slot', choices=['a', 'b'], default='a', help='slot that receives the application')
    parser.add_argument('--slot-b', type=Path, default=None, metavar='IMG',
                        help='existing application .img to place in the other slot (e.g. build/app.img)')
    parser.add_argument('--flash-out', type=Path, default=None, metavar='PATH',
                        help='file name for the 16 MiB flash image (default: <out>/hpdj_flash.bin)')
    parser.add_argument('--keep-hp-update', action='store_true',
                        help='do not erase the HP footer/GHS sectors (stage-1 may then overlay HP code)')
    parser.add_argument('--no-flash', action='store_true', help='only build the .img files')
    parser.add_argument('--no-bl', action='store_true', help='skip the bootloader (implies --no-flash)')
    parser.add_argument('--jobs', type=int, default=DEFAULT_JOBS, help='parallel compiler processes')
    parser.add_argument('--cflag', action='append', default=[], help='extra compiler flag (repeatable)')
    parser.add_argument('--no-watchdog', action='store_true',
                        help='do not arm the 8 s hardware watchdog at power-on (printer builds do by default)')
    parser.add_argument('--no-usb-console', action='store_true',
                        help='do not start the USB serial console at power-on (printer builds do by default)')
    parser.add_argument('--no-wifi-join', action='store_true',
                        help='do not join the default Wi-Fi network after power-on (printer builds do by default; '
                             'build_all() and the tests leave it off)')
    parser.add_argument('--emulate', action='store_true', help='boot the flash image in the emulator afterwards')
    parser.add_argument('--emulate-instructions', type=int, default=12_000_000)
    args = parser.parse_args(argv)
    no_flash = args.no_flash or args.no_bl
    cflags = list(args.cflag)
    if not args.no_wifi_join:
        cflags.append('-DWIFI_BOOT_JOIN=1')
    if not args.no_usb_console:
        cflags.append('-DUSB_BOOT_ON=1')
    if not args.no_watchdog:
        cflags.append('-DWDT_BOOT_S=8')

    t0 = time.time()
    try:
        manifest = build_all(args.out, base_dump=None if no_flash else args.base, bl_version=args.bl_version,
                             app_version=args.app_version, app_sequence=args.app_seq,
                             slot='ab'.index(args.slot), neutralize_hp_update=not args.keep_hp_update,
                             extra_cflags=cflags or None, write_flash=not no_flash,
                             app_src=args.app_src, build_bl=not args.no_bl, jobs=args.jobs,
                             flash_out=args.flash_out, slot_b_image=args.slot_b)
    except (RuntimeError, FileNotFoundError, ValueError) as exc:
        print(f'[!] {exc}', file=sys.stderr)
        return 1
    print(f'[*] compiler: {manifest["compiler"]}')
    for name, info in manifest['targets'].items():
        print(f'[+] {name:3s}: {info["description"]} ({info["objects_compiled"]} objects compiled)')
        print(f'         {info["image"]}')
    if manifest.get('flash'):
        f = manifest['flash']
        print(f'[+] flash image for the CH341: {f["path"]} ({f["size"]} bytes, sha256 {f["sha256"][:16]}..., '
              f'app in slot {f["app_slot"]})')
        if f.get('other_slot_image'):
            print(f'         other slot: {f["other_slot_image"]}')
    print(f'[*] build time {time.time() - t0:.1f} s')
    if args.emulate:
        if not manifest['_flash_path']:
            print('[!] --emulate needs the flash image (do not combine with --no-flash/--no-bl)', file=sys.stderr)
            return 1
        from .harness import VirtualPrinter
        vp = VirtualPrinter(manifest['_flash_path'].read_bytes())
        vp.run_until(b'app> ', max_instructions=args.emulate_instructions)
        print(vp.uart_text())
    return 0


if __name__ == '__main__':
    sys.exit(main())
