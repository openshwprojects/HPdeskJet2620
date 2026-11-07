#!/usr/bin/env python3
"""
Compile OpenBeken (OpenBK7231T_App) with PLATFORM_88PAX01 for the
Marvell 88PAAX01-BPK2 (AXON, ARM946E-S big-endian) SoC in the HP DeskJet 2620.

Produces:
  - build/obk/obk_app.elf
  - build/obk/obk_app.bin
  - build/obk/obk_app.img  (HPDJ 64-byte header + payload, ready for ramrun/update/OTA)
"""
from __future__ import annotations

import argparse
import concurrent.futures
import glob
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from custom_firmware import find_arm_c_compiler
from fw.image import (
    APP_CODE_MAX,
    APP_DATA_ADDR,
    APP_LOAD_ADDR,
    IMAGE_TYPE_APP,
    make_image,
    version_u32,
)

DEFAULT_OBK_DIR = Path(r"W:\GIT\OpenBK7231T\apps\OpenBK7231T_App")

COMMON_CFLAGS = [
    "-mbig-endian",
    "-mcpu=arm946e-s",
    "-marm",
    "-Os",
    "-ffreestanding",
    "-fno-builtin",
    "-fno-common",
    "-fno-strict-aliasing",
    "-ffunction-sections",
    "-fdata-sections",
    "-nostdlib",
    "-std=gnu99",
    "-Wno-unused-parameter",
    "-Wno-unused-variable",
    "-Wno-unused-function",
    "-Wno-sign-compare",
    "-Wno-pointer-sign",
    "-Wno-implicit-function-declaration",
    "-Wno-int-conversion",
    "-Wno-incompatible-pointer-types",
    "-Wno-format",
    "-DPLATFORM_88PAX01=1",
    "-DLFS_NO_MALLOC",
    "-DUSB_BOOT_ON=1",
    "-DWIFI_BOOT_JOIN=0",
    "-DFW_VERSION_STR=\"Open88PAX01_1.0\"",
    "-DFW_BUILD_STR=\"88PAX01\"",
]


def collect_obk_sources(obk_dir: Path) -> list[Path]:
    src = obk_dir / "src"
    files: list[str] = [
        str(src / "new_ping.c"),
        str(src / "rgb2hsv.c"),
        str(src / "tiny_crc8.c"),
        str(src / "new_common.c"),
        str(src / "new_pins.c"),
        str(src / "new_cfg.c"),
        str(src / "user_main.c"),
        str(src / "libraries" / "obktime" / "obktime.c"),
        str(src / "hal" / "generic" / "hal_adc_generic.c"),
        str(src / "hal" / "generic" / "hal_bt_proxy_generic.c"),
        str(src / "hal" / "generic" / "hal_hwtimer_generic.c"),
    ]

    patterns = [
        "base64/*.c",
        "bitmessage/*.c",
        "cJSON/*.c",
        "cmnds/*.c",
        "devicegroups/*.c",
        "httpclient/*.c",
        "httpserver/*.c",
        "jsmn/*.c",
        "littlefs/*.c",
        "logging/*.c",
        "mqtt/*.c",
        "hal/generic/*.c",
        "hal/88pax01/*.c",
    ]
    for pat in patterns:
        for f in sorted(glob.glob(str(src / pat))):
            if f not in files:
                files.append(f)

    driver_files = [
        "drv_main.c",
        "drv_deviceclock.c",
        "drv_timed_events.c",
        "drv_ntp.c",
        "drv_ntp_events.c",
        "drv_httpButtons.c",
        "drv_ssdp.c",
        "drv_mdns.c",
        "drv_wemo.c",
        "drv_hue.c",
        "drv_charts.c",
        "drv_ddp.c",
        "drv_tasmotaDeviceGroups.c",
        "drv_test_drivers.c",
        "drv_tuyaMCU.c",
        "drv_uart.c",
        "drv_ds1820_common.c",
        "drv_hp2620_panel.c",
        "drv_hp2620_motors.c",
    ]
    for df in driver_files:
        p = src / "driver" / df
        if p.exists():
            files.append(str(p))

    return [Path(f) for f in files]


def write_obk_linker_script(path: Path, layout: dict[str, int]) -> None:
    code_origin = layout["APP_LOAD_ADDR"]
    code_len = layout["APP_CODE_MAX"]
    data_origin = layout["APP_DATA_ADDR"]
    data_len = 0x00180000  # 1.5 MiB for .data + .bss (0x20180000..0x20300000)
    stack_top = 0x20380000

    path.write_text(
        f"""ENTRY(_start)
MEMORY {{
    CODE (rx)  : ORIGIN = 0x{code_origin:08X}, LENGTH = 0x{code_len:08X}
    DATA (rw)  : ORIGIN = 0x{data_origin:08X}, LENGTH = 0x{data_len:08X}
}}
SECTIONS {{
    .text : {{
        KEEP(*(.text.entry))
        *(.text .text.*)
        *(.rodata .rodata.*)
        . = ALIGN(4);
    }} > CODE

    __data_lma = LOADADDR(.data);
    __data_load = LOADADDR(.data);
    .data : {{
        __data_start = .;
        *(.data .data.*)
        . = ALIGN(4);
        __data_end = .;
    }} > DATA AT > CODE

    .bss (NOLOAD) : {{
        __bss_start = .;
        *(.bss .bss.* COMMON)
        . = ALIGN(4);
        __bss_end = .;
    }} > DATA

    __stack_top = 0x{stack_top:08X};
    /DISCARD/ : {{ *(.ARM.exidx* .ARM.extab* .comment .note*) }}
}}
""",
        encoding="ascii",
    )


def compile_one(args: tuple[str, Path, Path, list[str]]) -> tuple[Path, int, str]:
    cc, src_path, obj_path, flags = args
    obj_path.parent.mkdir(parents=True, exist_ok=True)
    if obj_path.exists() and obj_path.stat().st_mtime >= src_path.stat().st_mtime:
        return (src_path, 0, "")
    cmd = [cc, *flags, "-c", str(src_path), "-o", str(obj_path)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    return (src_path, res.returncode, res.stdout + res.stderr)


def build_obk(obk_dir: Path, clean: bool = False) -> Path:
    cc = find_arm_c_compiler()
    if not cc:
        raise SystemExit("[build_obk] No ARM C compiler found.")
    cc_path = Path(cc)
    tc = {
        "gcc": cc,
        "objcopy": str(cc_path.with_name(cc_path.name.replace("gcc", "objcopy"))),
        "size": str(cc_path.with_name(cc_path.name.replace("gcc", "size"))),
    }
    layout = {
        "APP_LOAD_ADDR": APP_LOAD_ADDR,
        "APP_CODE_MAX": APP_CODE_MAX,
        "APP_DATA_ADDR": APP_DATA_ADDR,
    }

    build_dir = ROOT / "build" / "obk"
    obj_dir = build_dir / "obj"
    if clean and obj_dir.exists():
        for p in obj_dir.rglob("*.o"):
            p.unlink()
    obj_dir.mkdir(parents=True, exist_ok=True)

    obk_fw_dir = ROOT / "fw" / "obk"
    fw_common = ROOT / "fw" / "common"
    fw_app = ROOT / "fw" / "app"

    includes = [
        f"-I{obk_fw_dir / 'include' / 'obk_shim'}",
        f"-I{obk_fw_dir / 'include'}",
        f"-I{fw_common}",
        f"-I{fw_app}",
        f"-I{obk_dir / 'src'}",
    ]

    hpdj_sources = [
        fw_common / "start.S",
        fw_common / "uart.c",
        fw_common / "timer.c",
        fw_common / "log.c",
        fw_common / "crc.c",
        fw_common / "spi_flash.c",
        fw_common / "image.c",
        fw_common / "slots.c",
        fw_common / "xmodem.c",
        fw_common / "common_cmds.c",
        fw_common / "periph.c",
        fw_app / "periph_cmds.c",
        fw_app / "wifi.c",
        fw_app / "ui.c",
        fw_app / "usbcon.c",
        fw_app / "scan.c",
        fw_app / "ramrun.c",
        fw_app / "ramrun.S",
    ]

    sdk_sources = [
        obk_fw_dir / "src" / "rtos_switch.S",
        obk_fw_dir / "src" / "rtos.c",
        obk_fw_dir / "src" / "heap.c",
        obk_fw_dir / "src" / "libc.c",
        obk_fw_dir / "src" / "printf.c",
        obk_fw_dir / "src" / "softfp.c",
        obk_fw_dir / "src" / "net.c",
        obk_fw_dir / "src" / "tcp.c",
        obk_fw_dir / "src" / "dhcp.c",
        obk_fw_dir / "src" / "dhcpd.c",
        obk_fw_dir / "src" / "dnsd.c",
        obk_fw_dir / "src" / "lwip_sockets.c",
        obk_fw_dir / "src" / "lwip_mqtt.c",
        obk_fw_dir / "src" / "lwip_mdns.c",
    ]

    obk_sources = collect_obk_sources(obk_dir)
    all_sources = hpdj_sources + sdk_sources + obk_sources

    flags = COMMON_CFLAGS + includes
    tasks = []
    obj_files: list[Path] = []
    for src_path in all_sources:
        h = hashlib.md5(str(src_path).encode("utf-8")).hexdigest()[:8]
        obj_path = obj_dir / f"{src_path.stem}_{h}.o"
        obj_files.append(obj_path)
        tasks.append((tc["gcc"], src_path, obj_path, flags))

    print(f"[build_obk] Compiling {len(tasks)} source files for PLATFORM_88PAX01 (ARM946E-S big-endian)...")
    t0 = time.time()
    failed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as ex:
        for src_path, rc, out in ex.map(compile_one, tasks):
            if rc != 0:
                print(f"\n[FAIL] {src_path}:\n{out}")
                failed += 1
    if failed:
        raise SystemExit(f"[build_obk] {failed} file(s) failed to compile.")
    print(f"[build_obk] Compilation finished in {time.time() - t0:.1f}s")

    ld_script = build_dir / "obk.ld"
    write_obk_linker_script(ld_script, layout)

    elf_path = build_dir / "obk_app.elf"
    bin_path = build_dir / "obk_app.bin"
    img_path = build_dir / "obk_app.img"

    link_cmd = [
        tc["gcc"],
        "-mbig-endian",
        "-mcpu=arm946e-s",
        "-marm",
        "-nostdlib",
        f"-T{ld_script}",
        "-Wl,--gc-sections",
        f"-Wl,-Map={build_dir / 'obk_app.map'}",
        "-o",
        str(elf_path),
        *[str(o) for o in obj_files],
    ]
    print("[build_obk] Linking obk_app.elf...")
    res = subprocess.run(link_cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(res.stdout + res.stderr)
        raise SystemExit("[build_obk] Link failed.")

    subprocess.run([tc["objcopy"], "-O", "binary", str(elf_path), str(bin_path)], check=True)
    subprocess.run([tc["size"], str(elf_path)], check=False)

    payload = bin_path.read_bytes()
    seq = int(time.time())
    img = make_image(
        payload=payload,
        image_type=IMAGE_TYPE_APP,
        version=version_u32("1.0.0"),
        sequence=seq,
        load_address=layout["APP_LOAD_ADDR"],
        entry_address=layout["APP_LOAD_ADDR"],
        build_time=seq,
        name="Open88PAX01",
    )
    img_path.write_bytes(img)
    print(
        f"[build_obk] Built {img_path} ({len(img):,} bytes, payload {len(payload):,} / {layout['APP_CODE_MAX']:,} bytes, seq={seq})"
    )
    return img_path


def main() -> None:
    ap = argparse.ArgumentParser(description="Build OpenBeken for Marvell 88PAX01 (HP DeskJet 2620)")
    ap.add_argument("--obk-dir", type=Path, default=DEFAULT_OBK_DIR)
    ap.add_argument("--clean", action="store_true")
    args = ap.parse_args()
    build_obk(args.obk_dir, clean=args.clean)


if __name__ == "__main__":
    main()
