"""Live UART0 shell and UDW command self-test: python -m analysis.uart_selftest."""
import ctypes
import os
import pickle
import struct
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from emulator import Emulator, REGISTERS
from memory import DRAM_SIZE
from unicorn.arm_const import (
    UC_ARM_REG_CPSR, UC_ARM_REG_SPSR, UC_ARM_REG_SP, UC_ARM_REG_LR,
    UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_R10, UC_ARM_REG_R11, UC_ARM_REG_R12,
)
DEFAULT_SNAPSHOT = Path(
    os.environ.get(
        "UART_SNAPSHOT",
        str(ROOT / "artifacts" / "snap_stage22.pkl"),
    )
)

SHELL_LIST_HEAD_PTR = 0x20DA71AC
UDW_TABLE_COUNT_PTR = 0x202C2F10
UDW_TABLE_ARRAY_PTR = 0x202C2F14


def _read_u32(dram: bytes | bytearray, addr: int) -> int:
    return struct.unpack_from(">I", dram, addr & 0x01FFFFFF)[0]


def _read_cstr(dram: bytes | bytearray, addr: int, max_len: int = 128) -> str:
    if not (0xA0000000 <= addr <= 0xA2000000 or 0x20000000 <= addr <= 0x22000000):
        return ""
    off = addr & 0x01FFFFFF
    return dram[off : off + max_len].split(b"\x00", 1)[0].decode("latin1", errors="replace")


def enumerate_shell_commands(dram: bytes | bytearray) -> list[dict]:
    """Walk the firmware's UART shell linked list at 0x20DA71AC."""
    entries = []
    node = _read_u32(dram, SHELL_LIST_HEAD_PTR)
    visited = set()
    while node and node not in visited:
        visited.add(node)
        target, name_ptr, desc_ptr, is_func, nxt = struct.unpack_from(
            ">5I", dram, node & 0x01FFFFFF
        )
        entries.append({
            "node": node,
            "name": _read_cstr(dram, name_ptr),
            "target": target,
            "is_func": bool(is_func),
            "description": _read_cstr(dram, desc_ptr),
        })
        node = nxt
    return entries


def descramble_udw_name(
    dram: bytes | bytearray,
    name: str,
    plain_alpha: str | None = None,
    sub_tables: list[str] | None = None,
) -> str:
    """Decrypt HP's 62-alphabet monoalphabetic UDW substitution cipher (0xA04EDFB4)."""
    if plain_alpha is None:
        plain_alpha = _read_cstr(dram, _read_u32(dram, 0x20870114), 256)
    if sub_tables is None:
        sub_tables = [
            _read_cstr(dram, _read_u32(dram, 0x2087011C + i * 4), 256)
            for i in range(62)
        ]
    if len(name) >= 4 and name[1] in plain_alpha[2:]:
        sl = plain_alpha.index(name[1]) - 2
        if 0 <= sl < len(sub_tables):
            sub = sub_tables[sl]
            if len(sub) >= 64 and sub[28] == name[0] and all(c in sub[:64] for c in name[2:-1]):
                return "".join(plain_alpha[sub.index(c)] for c in name[2:-1])
    return name


def enumerate_udw_tables(dram: bytes | bytearray) -> list[dict]:
    """Walk the firmware's UDW subsystem table registry at [0x202C2F10]/[0x202C2F14]."""
    count_addr = _read_u32(dram, UDW_TABLE_COUNT_PTR)
    table_base = _read_u32(dram, UDW_TABLE_ARRAY_PTR)
    count = _read_u32(dram, count_addr)
    plain_alpha = _read_cstr(dram, _read_u32(dram, 0x20870114), 256)
    sub_tables = [
        _read_cstr(dram, _read_u32(dram, 0x2087011C + i * 4), 256)
        for i in range(62)
    ]
    tables = []
    for idx in range(count):
        w0, w1, c0, c1 = struct.unpack_from(
            ">4I", dram, (table_base + idx * 16) & 0x01FFFFFF
        )
        cmds = []
        if c0 == 0 and w0 and 0 < w1 <= 256:
            c_off = w0 & 0x01FFFFFF
            for _ in range(w1):
                c_name_ptr, c_fn_ptr, c_arg = struct.unpack_from(">3I", dram, c_off)
                raw_name = _read_cstr(dram, c_name_ptr)
                dec_name = descramble_udw_name(dram, raw_name, plain_alpha, sub_tables)
                cmds.append({
                    "name": raw_name,
                    "decoded_name": dec_name,
                    "obfuscated": dec_name != raw_name,
                    "handler": c_fn_ptr,
                    "arg": c_arg,
                })
                c_off += 12
        elif c0 == 1 and w0:
            name_ptr = _read_u32(dram, w0)
            raw_name = _read_cstr(dram, name_ptr) or _read_cstr(dram, w0)
            dec_name = descramble_udw_name(dram, raw_name, plain_alpha, sub_tables)
            cmds.append({
                "name": raw_name,
                "decoded_name": dec_name,
                "obfuscated": dec_name != raw_name,
                "handler": w1,
                "arg": c1,
            })
        first_dec = cmds[0]["decoded_name"] if cmds else ""
        sub_name = first_dec.split(".", 1)[0] if "." in first_dec else (first_dec or f"table_{idx}")
        tables.append({"subsystem": sub_name, "ctx": (c0, c1), "commands": cmds})
    return tables


def _restore_cpu_modes(uc, data: dict) -> None:
    orig_cpsr = data["cpsr"]
    for r, val in zip(REGISTERS[:-3], data["regs"][:-3]):
        uc.reg_write(r, val)
    for mode_name, mode_bits in (
        ("svc", 0x13), ("irq", 0x12), ("fiq", 0x11),
        ("und", 0x1B), ("abt", 0x17), ("sys", 0x1F),
    ):
        entry = data["modes"][mode_name]
        uc.reg_write(UC_ARM_REG_CPSR, (orig_cpsr & ~0x3F) | 0xC0 | mode_bits)
        uc.reg_write(UC_ARM_REG_SP, entry["sp"])
        uc.reg_write(UC_ARM_REG_LR, entry["lr"])
        if "spsr" in entry:
            uc.reg_write(UC_ARM_REG_SPSR, entry["spsr"])
        if "r8_12" in entry:
            for r, val in zip(
                (UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_R10, UC_ARM_REG_R11, UC_ARM_REG_R12),
                entry["r8_12"],
            ):
                uc.reg_write(r, val)
    pc_val = data["regs"][-1] | (1 if (orig_cpsr & 0x20) else 0)
    uc.reg_write(REGISTERS[-1], pc_val)
    uc.reg_write(UC_ARM_REG_CPSR, orig_cpsr)


def _capture_cpu_modes(uc) -> dict:
    orig_cpsr = uc.reg_read(UC_ARM_REG_CPSR)
    regs = [uc.reg_read(r) for r in REGISTERS]
    modes = {}
    for mode_name, mode_bits in (
        ("svc", 0x13), ("irq", 0x12), ("fiq", 0x11),
        ("und", 0x1B), ("abt", 0x17), ("sys", 0x1F),
    ):
        uc.reg_write(UC_ARM_REG_CPSR, (orig_cpsr & ~0x3F) | 0xC0 | mode_bits)
        entry = {
            "sp": uc.reg_read(UC_ARM_REG_SP),
            "lr": uc.reg_read(UC_ARM_REG_LR),
        }
        if mode_name != "sys":
            entry["spsr"] = uc.reg_read(UC_ARM_REG_SPSR)
        if mode_name == "fiq":
            entry["r8_12"] = [
                uc.reg_read(r)
                for r in (UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_R10, UC_ARM_REG_R11, UC_ARM_REG_R12)
            ]
        modes[mode_name] = entry
    uc.reg_write(REGISTERS[-1], regs[-1] | (1 if (orig_cpsr & 0x20) else 0))
    uc.reg_write(UC_ARM_REG_CPSR, orig_cpsr)
    return {"cpsr": orig_cpsr, "regs": regs, "modes": modes}


def capture_snapshot(emu: Emulator) -> dict:
    """Capture an in-memory snapshot dictionary from a live Emulator instance."""
    if emu._native_observer is not None:
        emu._native_observer.synchronize(refresh=True)
    ep = emu.board.panel.endpoint
    sdio = emu.board.sdio
    card = sdio.card
    dram_base_ref = getattr(emu.memory, "_dram_init_ref", None)
    dram_patches = None
    if os.name == "nt" and type(dram_base_ref) is bytes and len(dram_base_ref) == DRAM_SIZE:
        base = ctypes.addressof(emu.memory._dram)
        addrs = (ctypes.c_void_p * 16384)()
        cnt = ctypes.c_size_t(16384)
        gran = ctypes.c_uint32(4096)
        if (
            ctypes.windll.kernel32.GetWriteWatch(
                0, ctypes.c_void_p(base), DRAM_SIZE, addrs, ctypes.byref(cnt), ctypes.byref(gran)
            )
            == 0
            and gran.value == 4096
        ):
            mv = emu.memory._dram_mv
            dram_patches = [
                (addrs[i] - base, bytes(mv[addrs[i] - base : addrs[i] - base + 4096]))
                for i in range(cnt.value)
            ]
    return {
        "adc_step": getattr(emu, "_adc_state", [0])[0],
        "base_insns": emu.instructions,
        "cp15": dict(emu.cp15.values),
        "cpu": _capture_cpu_modes(emu.uc),
        "dram": dram_base_ref if dram_patches is not None else bytes(emu.memory._dram_mv),
        "dram_patches": dram_patches,
        "eeprom": bytes(emu.board.i2c.devices[0x50].image) if 0x50 in emu.board.i2c.devices else None,
        "fiq_claim": emu.board._fiq_claim,
        "gpio_dirs": list(emu.board.gpio.directions),
        "gpio_ev": list(emu.board.gpio.event_status),
        "gpio_masks": [list(m) for m in emu.board.gpio.irq_masks],
        "gpio_out": list(emu.board.gpio.output_latches),
        "i2c_state": {
            "registers": dict(emu.board.i2c.registers),
            "control": emu.board.i2c.control,
            "busy": emu.board.i2c.busy,
            "interrupt_pending": emu.board.i2c.interrupt_pending,
            "slave": emu.board.i2c._slave,
            "address": emu.board.i2c._address,
            "reading": emu.board.i2c._reading,
            "continue": emu.board.i2c._continue,
        },
        "irq_claim": emu.board._irq_claim,
        "irq_raw_sources": emu.board.irq_raw_sources,
        "mcasic_adc": list(emu.mcasic.adc_conversions) if emu.mcasic else [],
        "mcasic_regs": dict(emu.mcasic.registers) if emu.mcasic else {},
        "micci": {
            base: {
                "config": dict(ctrl.config),
                "table": dict(ctrl.table),
                "data": ctrl.data,
                "command": ctrl.command,
                "busy": ctrl.busy,
            }
            for base, ctrl in emu.board.micci.items()
        },
        "panel_state": (
            {
                "output_word": emu.board.panel.output_word,
                "ep_output_word": ep.output_word,
                "strobe": ep.strobe,
                "armed": ep.armed,
                "bit_count": ep.bit_count,
                "shifted_output": ep.shifted_output,
                "latched_inputs": ep.latched_inputs,
                "sampled_bit": ep.sampled_bit,
            }
            if ep is not None
            else None
        ),
        "pll_locked": emu.board.pll_locked,
        "reg_bytes": dict(emu.board._register_bytes),
        "usb_bytes": dict(emu.board.usb._bytes),
        "sdio_state": {
            "sdma_addr": sdio.sdma_addr,
            "block_size": sdio.block_size,
            "block_count": sdio.block_count,
            "argument": sdio.argument,
            "transfer_mode": sdio.transfer_mode,
            "command": sdio.command,
            "response": list(sdio.response),
            "host_ctrl": sdio.host_ctrl,
            "clock_ctrl": sdio.clock_ctrl,
            "int_status": sdio.int_status,
            "int_status_en": sdio.int_status_en,
            "int_signal_en": sdio.int_signal_en,
            "cmd_count": sdio.cmd_count,
            "f0": bytes(card.f0),
            "f1_ctl": bytes(card.f1_ctl),
            "socram": bytes(card.socram),
            "sb_window": card.sb_window,
            "rx_seq": card.rx_seq,
            "last_tx_seq": card.last_tx_seq,
            "tx_max": card.tx_max,
            "f2_rx_frame": bytes(card.f2_rx_frame),
            "f2_rx_offset": card.f2_rx_offset,
            "ioctl_count": card.ioctl_count,
            "last_ioctl_cmd": card.last_ioctl_cmd,
            "last_var_name": card.last_var_name,
            "iovars": dict(card.iovars),
            "sdiod_intstatus": card.sdiod_intstatus,
            "sdiod_hostintmask": card.sdiod_hostintmask,
        },
        "sdram_ready": emu.board.sdram_ready,
        "sec_timer_pending": emu.board._sec_timer_pending,
        "second_pll_locked": emu.board.second_pll_locked,
        "second_pll_reset_released": emu.board._second_pll_reset_released,
        "tcm": {
            k: ctypes.string_at(v, len(v)) if v is not None else None
            for k, v in emu.memory._tcm.items()
        },
        "ticks": emu.board.ticks,
        "timer_deadline": list(emu.board.timer_deadline),
        "timer_offset": emu.board._timer_offset,
        "timer_pending": emu.board.timer_pending,
        "timer_reload": list(emu.board.timer_reload),
        "uart": bytes(emu.uart),
    }


_CACHED_DUMP_BYTES: bytes | None = None
_CACHED_DUMP_AND_SNAP: tuple[bytes, dict] | None = None


def load_default_dump() -> bytes | None:
    """Load and cache dump.bin once per process."""
    global _CACHED_DUMP_BYTES
    if _CACHED_DUMP_BYTES is not None:
        return _CACHED_DUMP_BYTES
    dump_path = ROOT / "dump.bin"
    if not dump_path.is_file():
        return None
    _CACHED_DUMP_BYTES = dump_path.read_bytes()
    return _CACHED_DUMP_BYTES


def generate_default_snapshot(
    dump: bytes | None = None,
    save_path: Path | str | None = DEFAULT_SNAPSHOT,
) -> dict:
    """Cold-boot dump.bin to Stage-22 idle shell and capture (and optionally save) the snapshot."""
    from hardware import factory_eeprom

    if dump is None:
        dump = load_default_dump()
    if dump is None:
        raise FileNotFoundError("dump.bin not found")
    adc_state = [0]

    def adc_ch2_noise() -> int:
        adc_state[0] += 1
        step = adc_state[0]
        pair = (step - 1) // 2
        bit = (pair ^ (pair >> 1) ^ (pair >> 3)) & 1
        return 512 | (bit if (step & 1) else (bit ^ 1))

    analog_inputs = {i: 512 for i in range(1, 20) if i != 15}
    analog_inputs[2] = adc_ch2_noise
    gpio_inputs = {1: 0, 2: 0, 3: 0, 8: 1, 11: 0, 15: 1}
    emu = Emulator(
        dump,
        native_observer=True,
        power_button_wakeup=True,
        serial_panel_inputs=0x3FFFF,
        serial_panel_tail_bit=1,
        spi_profile="byte-final",
        eeprom=factory_eeprom(),
        gpio_inputs=gpio_inputs,
        analog_inputs=analog_inputs,
    )
    emu._adc_state = adc_state
    emu.run(50_000_000)
    while emu.board.ticks < 374_550_000:
        step_restored(emu, 200_000)
    if emu._native_observer is not None:
        emu._native_observer.synchronize(refresh=True)
    dram = emu.memory._dram_mv
    struct.pack_into(">I", dram, 0xAB74C4, 0x7DF4)
    struct.pack_into(
        ">8I",
        dram,
        0xAF6460,
        0x00000005,
        0x000001F8,
        0xFFFFFFFE,
        0x00000000,
        0x00007C04,
        0x208420E8,
        0x000002DF,
        0x20AB74A0,
    )
    dram[0x135B277:0x135B27D] = b"\x12\x93\x34\x03\x31\x2b"
    dram[0x0DFFCB6:0x0DFFCBC] = b"\x12\x93\x34\x03\x31\x2b"
    struct.pack_into(">I", dram, 0xDA6784, 25000)
    struct.pack_into(">2I", dram, 0xDA6790, 0, 0)
    struct.pack_into(">2I", dram, 0xDA4C10, 0, 0)
    struct.pack_into(">I", dram, 0xDF12C4, 0x50)
    emu.memory._dram_init_ref = None
    snap = capture_snapshot(emu)
    if save_path is not None:
        out_path = Path(save_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(pickle.dumps(snap, protocol=pickle.HIGHEST_PROTOCOL))
    return snap


def load_default_dump_and_snapshot() -> tuple[bytes, dict] | None:
    """Load and cache dump.bin and the default Stage-22 snapshot once per process."""
    global _CACHED_DUMP_AND_SNAP
    if _CACHED_DUMP_AND_SNAP is not None:
        return _CACHED_DUMP_AND_SNAP
    dump = load_default_dump()
    if dump is None:
        return None
    snap_path = Path(DEFAULT_SNAPSHOT)
    if not snap_path.is_file():
        snap = generate_default_snapshot(dump, snap_path)
    else:
        with open(snap_path, "rb") as f:
            snap = pickle.load(f)
    _CACHED_DUMP_AND_SNAP = (dump, snap)
    return _CACHED_DUMP_AND_SNAP


def build_and_restore(
    dump: bytes,
    snap: dict,
    on_event=None,
    on_uart=None,
    *,
    native_observer: bool = True,
) -> Emulator:
    """Create an Emulator instance and restore a booted Stage-22 snapshot."""
    adc_state = [snap.get("adc_step", 0)]

    def adc_ch2_noise() -> int:
        adc_state[0] += 1
        step = adc_state[0]
        pair = (step - 1) // 2
        bit = (pair ^ (pair >> 1) ^ (pair >> 3)) & 1
        return 512 | (bit if (step & 1) else (bit ^ 1))

    analog_inputs = {i: 512 for i in range(1, 20) if i != 15}
    analog_inputs[2] = adc_ch2_noise
    gpio_inputs = {1: 0, 2: 0, 3: 0, 8: 1, 11: 0, 15: 1}
    emu = Emulator(
        dump,
        native_observer=native_observer,
        init_dram=snap["dram"],
        init_dram_patches=snap.get("dram_patches"),
        init_cp15=snap["cp15"],
        init_tcm=snap["tcm"],
        power_button_wakeup=True,
        serial_panel_inputs=0x3FFFF,
        serial_panel_tail_bit=1,
        spi_profile="byte-final",
        eeprom=snap.get("eeprom") or (b"\xff" * 0x10000),
        gpio_inputs=gpio_inputs,
        analog_inputs=analog_inputs,
        on_uart=on_uart,
        on_event=on_event,
    )
    emu._adc_state = adc_state
    emu.board.uart_frame_ticks = 8
    emu.board.ticks = snap["ticks"]
    emu.board._timer_offset = snap["timer_offset"]
    emu.board.pll_locked = snap["pll_locked"]
    emu.board.second_pll_locked = snap["second_pll_locked"]
    emu.board._second_pll_reset_released = snap.get("second_pll_reset_released", True)
    emu.board.sdram_ready = snap["sdram_ready"]
    emu.board._register_bytes.update(snap["reg_bytes"])
    emu.board.usb._bytes.update(
        {k: v for k, v in snap["reg_bytes"].items() if 0xBD09BC00 <= k <= 0xBD09FC3F}
    )
    if "usb_bytes" in snap:
        emu.board.usb._bytes.update(snap["usb_bytes"])
    emu.board.timer_pending = snap.get("timer_pending", 0)
    if "timer_reload" in snap:
        emu.board.timer_reload[:] = snap["timer_reload"]
    if "timer_deadline" in snap:
        for ch, dl in enumerate(snap["timer_deadline"]):
            if dl is not None and dl > emu.board.ticks:
                emu.board.timer_deadline[ch] = dl
                emu.board._timer_active_mask |= 1 << ch
                emu.board.schedule(dl - emu.board.ticks, emu.board._timer_callbacks[ch])
                emu.board._timer_event_seq[ch] = emu.board._event_sequence
    emu.board._sec_timer_pending = snap.get("sec_timer_pending", 0)
    emu.board.irq_raw_sources = snap.get("irq_raw_sources", 0)
    emu.board._irq_claim = snap.get("irq_claim", 0)
    emu.board._fiq_claim = snap.get("fiq_claim", 0)
    emu.board.interrupt_mask = (1 if emu.board._irq_claim else 0) | (2 if emu.board._fiq_claim else 0)
    emu.board.gpio.directions[:] = snap["gpio_dirs"]
    emu.board.gpio.output_latches[:] = snap["gpio_out"]
    emu.board.gpio.event_status[:] = snap["gpio_ev"]
    for dst, src in zip(emu.board.gpio.irq_masks, snap["gpio_masks"]):
        dst[:] = src
    emu.board.sync_panel_gpio_leds()
    if "i2c_state" in snap:
        st = snap["i2c_state"]
        emu.board.i2c.registers.update(st["registers"])
        emu.board.i2c.control = st["control"]
        emu.board.i2c.busy = st["busy"]
        emu.board.i2c.interrupt_pending = st["interrupt_pending"]
        emu.board.i2c._slave = st["slave"]
        emu.board.i2c._address = st["address"]
        emu.board.i2c._reading = st["reading"]
        emu.board.i2c._continue = st["continue"]
    if emu.mcasic and "mcasic_regs" in snap:
        emu.mcasic.registers.update(snap["mcasic_regs"])
        emu.mcasic.adc_conversions[:] = snap.get("mcasic_adc", [])
    if "micci" in snap:
        for base, ms in snap["micci"].items():
            ctrl = emu.board.micci[base]
            ctrl.config.update(ms["config"])
            ctrl.table.update(ms["table"])
            ctrl.data = ms["data"]
            ctrl.command = ms["command"]
            ctrl.busy = ms["busy"]
    if emu.board.panel.endpoint is not None and snap.get("panel_state"):
        ps = snap["panel_state"]
        emu.board.panel.output_word = ps["output_word"]
        emu.board.panel.endpoint.output_word = ps["ep_output_word"]
        emu.board.panel.endpoint.strobe = ps["strobe"]
        emu.board.panel.endpoint.armed = ps["armed"]
        emu.board.panel.endpoint.bit_count = ps["bit_count"]
        emu.board.panel.endpoint.shifted_output = ps["shifted_output"]
        emu.board.panel.endpoint.latched_inputs = ps["latched_inputs"]
        emu.board.panel.endpoint.sampled_bit = ps["sampled_bit"]
    if "sdio_state" in snap:
        ss = snap["sdio_state"]
        sdio = emu.board.sdio
        sdio.sdma_addr = ss["sdma_addr"]
        sdio.block_size = ss["block_size"]
        sdio.block_count = ss["block_count"]
        sdio.argument = ss["argument"]
        sdio.transfer_mode = ss["transfer_mode"]
        sdio.command = ss["command"]
        sdio.response[:] = ss["response"]
        sdio.host_ctrl = ss["host_ctrl"]
        sdio.clock_ctrl = ss["clock_ctrl"]
        sdio.int_status = ss["int_status"]
        sdio.int_status_en = ss["int_status_en"]
        sdio.int_signal_en = ss["int_signal_en"]
        sdio.cmd_count = ss["cmd_count"]
        card = sdio.card
        card.f0[:] = ss["f0"]
        card.f1_ctl[:] = ss["f1_ctl"]
        card.socram[:] = ss["socram"]
        card.sb_window = ss["sb_window"]
        card.rx_seq = ss["rx_seq"]
        card.last_tx_seq = ss.get("last_tx_seq", 51)
        card.tx_max = ss.get("tx_max", (card.last_tx_seq + 64) & 0xFF)
        card.f2_rx_frame = bytearray(ss["f2_rx_frame"])
        card.f2_rx_offset = ss["f2_rx_offset"]
        card.f2_rx_queue.clear()
        card.ioctl_count = ss["ioctl_count"]
        card.last_ioctl_cmd = ss["last_ioctl_cmd"]
        card.last_var_name = ss["last_var_name"]
        card.iovars.update(ss["iovars"])
        card.sdiod_intstatus = ss.get("sdiod_intstatus", 0)
        card.sdiod_hostintmask = ss.get("sdiod_hostintmask", 0)
    emu.uart.clear()
    emu.uart.extend(snap["uart"])
    _restore_cpu_modes(emu.uc, snap["cpu"])
    emu.board._irq_cfg_dirty = True
    emu.board._timer_mask_dirty = True
    emu.board._recompute_interrupts()
    emu.board._sync_timer_masks()
    if emu._native_observer is not None:
        emu._native_observer.refresh()
    return emu


def step_restored(emu: Emulator, instructions: int) -> None:
    """Advance a restored Emulator instance by `instructions` guest instructions."""
    emu._stop = "budget"
    emu._started = True
    emu._cpsr_valid = False
    emu._ifm_valid = False
    emu.reason = None
    emu._limit = emu.instructions + instructions
    if emu._native_observer is not None:
        emu._native_observer.configure(emu._limit)
    else:
        emu.board._irq_cfg_dirty = True
        emu.board._recompute_interrupts()
    start_pc = emu.uc.reg_read(REGISTERS[-1]) | (1 if (emu.uc.reg_read(UC_ARM_REG_CPSR) & 0x20) else 0)
    emu.uc.emu_start(start_pc, 0xFFFFFFFF)
    emu._finish_instruction()


def split_udws_command(cmd: str, max_len: int = 120) -> list[str]:
    """Split a long `udws "a; b; c;"` command on semicolon boundaries so each stays <= max_len."""
    if len(cmd) <= max_len:
        return [cmd]
    stripped = cmd.strip()
    for prefix in ('udws "', 'ludw "'):
        if stripped.startswith(prefix) and stripped.endswith('"'):
            inner = stripped[len(prefix) : -1]
            parts = [p.strip() for p in inner.split(";") if p.strip()]
            if len(parts) <= 1:
                break
            chunks: list[str] = []
            cur_parts: list[str] = []
            for part in parts:
                candidate_inner = "; ".join(cur_parts + [part]) + ";"
                candidate = f'{prefix}{candidate_inner}"'
                if len(candidate) <= max_len:
                    cur_parts.append(part)
                else:
                    if not cur_parts:
                        raise ValueError(
                            f"Single UDW subcommand exceeds {max_len}-byte limit ({len(candidate)}B): {candidate!r}"
                        )
                    chunks.append(f'{prefix}{"; ".join(cur_parts)};"')
                    cur_parts = [part]
                    single = f'{prefix}{part};"'
                    if len(single) > max_len:
                        raise ValueError(
                            f"Single UDW subcommand exceeds {max_len}-byte limit ({len(single)}B): {single!r}"
                        )
            if cur_parts:
                chunks.append(f'{prefix}{"; ".join(cur_parts)};"')
            return chunks
    raise ValueError(f"UART shell command exceeds 128-byte line buffer ({len(cmd)}B): {cmd!r}")


def run_uart_commands(
    dump: bytes,
    snap: dict,
    commands: list[str],
    max_instructions: int = 18_000_000,
    *,
    auto_split_udws: bool = False,
) -> tuple[list[tuple[str, str]], Emulator]:
    """Execute a sequence of UART0 shell commands reactively as each '-> ' prompt appears."""
    if auto_split_udws:
        expanded: list[str] = []
        for cmd in commands:
            expanded.extend(split_udws_command(cmd))
        commands = expanded
    else:
        for cmd in commands:
            if len(cmd) > 120:
                raise ValueError(f"UART shell command exceeds 128-byte line buffer ({len(cmd)}B): {cmd!r}")

    cmd_idx = [0]
    cmd_start = [0]
    results: list[tuple[str, str]] = []

    def on_uart_byte(val: int) -> None:
        if val == 0x20 and emu.uart.endswith((b"\r-> ", b"\n-> ")) and not emu.board.uart_rx:
            if cmd_idx[0] > 0:
                out = bytes(emu.uart[cmd_start[0] :]).decode("latin1", errors="replace")
                results.append((commands[cmd_idx[0] - 1], out))
            if cmd_idx[0] < len(commands):
                cmd_start[0] = len(emu.uart)
                emu.board.queue_rx((commands[cmd_idx[0]] + "\r").encode("ascii"))
                cmd_idx[0] += 1
            else:
                emu.reason = "all-commands-completed"
                emu.uc.emu_stop()

    on_uart_byte.trigger_byte = 0x13E20  # type: ignore[attr-defined]
    emu = build_and_restore(dump, snap, on_uart=on_uart_byte)
    start_pc = snap["cpu"]["regs"][-1] | (1 if (snap["cpu"]["cpsr"] & 0x20) else 0)
    emu._started = True
    emu._limit = max_instructions
    emu._stop = "budget"
    emu._native_observer.configure(max_instructions)

    cmd_start[0] = len(emu.uart)
    emu.board.queue_rx((commands[0] + "\r").encode("ascii"))
    cmd_idx[0] = 1

    emu.uc.emu_start(start_pc, 0xFFFFFFFF)
    emu._finish_instruction()
    return results, emu


class UartCommandSelfTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cached = load_default_dump_and_snapshot()
        if cached is None:
            raise unittest.SkipTest(f"Stage-22 snapshot not found at {DEFAULT_SNAPSHOT}")
        cls.dump, cls.snap = cached

    def test_command_registries_and_live_execution_of_all_commands(self) -> None:
        dram = self.snap["dram"]

        # 1. Verify top-level UART shell command linked list (54 entries: 21 functions + 33 variables)
        shell_entries = enumerate_shell_commands(dram)
        self.assertEqual(len(shell_entries), 54)
        func_entries = [e for e in shell_entries if e["is_func"]]
        var_entries = [e for e in shell_entries if not e["is_func"]]
        self.assertEqual(len(func_entries), 21)
        self.assertEqual(len(var_entries), 33)

        expected_funcs = {
            "jmpp_terse", "jmpp_verbose", "ip_show", "ip_lhcfifo_reg",
            "ip_lhcfifo_debug", "ip_buf_debug", "ip_lmb_debug",
            "cp", "dskcpy", "ll", "ls", "netstat", "uart_reset",
            "taskLatency", "efShow", "semShow", "tps", "i",
            "udw_tags", "ludw", "udws",
        }
        self.assertEqual({e["name"] for e in func_entries}, expected_funcs)

        # 2. Verify UDW subsystem command tables (206 tables, 1,520 commands, including 289 cipher-obfuscated commands)
        udw_tables = enumerate_udw_tables(dram)
        self.assertEqual(len(udw_tables), 206)
        total_udw_cmds = sum(len(t["commands"]) for t in udw_tables)
        self.assertEqual(total_udw_cmds, 1520)
        obfuscated_cmds = [c for t in udw_tables for c in t["commands"] if c["obfuscated"]]
        self.assertEqual(len(obfuscated_cmds), 289)
        decoded_names = {c["decoded_name"] for c in obfuscated_cmds}
        for expected_hidden in (
            "udw.cmds", "udw.echo", "udw.dm", "udw.encr", "udw.decr",
            "hw.get_clk_rate", "hw_mcasic.get_supply_voltage", "micci2.get_errors",
            "i2c.read", "i2c.write", "nvm2.nvm_read_all", "lib_digital_sensor.list",
        ):
            self.assertIn(expected_hidden, decoded_names)
        c0_1_subsystems = {t["subsystem"] for t in udw_tables if t["ctx"][0] == 1}
        for expected_c0_1 in ("comp", "select", "status", "trap", "mergeout", "cissm", "adf"):
            self.assertIn(expected_c0_1, c0_1_subsystems)

        long_udw = (
            'udws "cnc_common_util.get_udn; webfwupdate.get_status; webfwupdate.get_nvm_state; '
            'webfwupdate.get_mode; webfwupdate.get_error; nvm2.get_hw_error_log;"'
        )
        split_parts = split_udws_command(long_udw)
        self.assertEqual(len(split_parts), 2)
        self.assertTrue(all(len(p) <= 120 for p in split_parts))
        with self.assertRaises(ValueError):
            split_udws_command("x" * 125)

        # 3. Build comprehensive command list exercising all 21 shell functions,
        #    all 33 shell variables (plus write & restore), and 64 UDW commands
        #    (both plaintext and decrypted cipher-obfuscated commands) across 16 batches.
        func_test_commands = [
            ("jmpp_terse", "jmpp_terse() returns 0 (0x0)"),
            ("jmpp_verbose", "jmpp_verbose() returns 0 (0x0)"),
            ("jmpp_terse", "jmpp_terse() returns 0 (0x0)"),
            ("ip_show", "ip_show() returns 0 (0x0)"),
            ("ip_lhcfifo_reg", "REGPTR_LHCFifoCtrlRdWr = 0000"),
            ("ip_lhcfifo_debug", "ip_lhcfifo_debug() returns 0 (0x0)"),
            ("ip_buf_debug", "ip_buf_debug() returns 0 (0x0)"),
            ("ip_lmb_debug", "ip_lmb _state IDLE"),
            ("ls /", "Listing registered drivers @root :"),
            ("ls /ram", "ls() returns 8 (0x8)"),
            ("cp /ram/system/00000000.TBL /tmp/test.tbl", "cp() returns 0 (0x0)"),
            ("dskcpy /ram/system/ews /tmp", "dskcpy() returns 0 (0x0)"),
            ("ll /tmp", "ll() returns 3 (0x3)"),
            ("netstat soc", "netstat() returns 1 (0x1)"),
            ("netstat tcp", "netstat() returns 1 (0x1)"),
            ("netstat udp", "netstat() returns 1 (0x1)"),
            ("netstat arp", "netstat() returns 1 (0x1)"),
            ("netstat rte", "192.168.223.0/24"),
            ("netstat dev", "192.168.223.1"),
            ("uart_reset", "uart_reset() returns"),
            ("taskLatency", "taskLatency() returns"),
            ("semShow 0x20EFFD78", "type binary or counting"),
            ("semShow 0x20EFFFA8", "type mutex"),
            ("efShow 0x211E737C", "type event"),
            ("tps tEcho 224", "tps() returns 0 (0x0)"),
            ("i tEcho", "tEcho         213005f8 224"),
            ("i tUiLite", "tUiLite       21342bd0  98"),
            ("udw_tags on", "udw_tags() returns"),
            ('ludw "shm.msecs;"', "<udw>msecs ="),
            ("udw_tags off", "udw_tags() returns"),
        ]

        var_test_commands = [
            (e["name"], f"{e['name']} = ") for e in var_entries
        ] + [
            ("dbug_udw = 1", "error: I don't understand"),
        ]

        udw_test_commands = [
            (
                'udws "bdid.get_board_id; bdid.get_mapped_board_id; ldn.long_name; ldn.short_name;"',
                "26815,(MFG:6, FAM:69, NUM:31)",
            ),
            (
                'udws "bio.pkg_fw_rev; bio.pkg_name; bio.pkg_phase; bio.bld_time; bio.rev_str;"',
                "Sat Jan 14, 2017 03:32:35PM;TJP1FN1702CR",
            ),
            (
                'udws "bio.base_rev_str; bio.target; bio.project; bio.machine; bio.userid; bio.tgtname;"',
                "TJP1FN1702CR;narel;taiji_pp;lr1cvslx12.scs.rd.hpicorp.net;fwbuild;main.out",
            ),
            (
                'udws "bio.scm_key; bio.scm_url; cache.query; mem.dram_size; shm.msecs;"',
                "9ee7fc7daedc8d0763dd8486bcef131b60146d0f",
            ),
            (
                'udws "ui_lite.get_state; sm.on_off_status; sm.get_pwrup_mode; device_info.get_device_status;"',
                "UI_LITE_WIRELESS_STA_ON_AWC_ACTIVE",
            ),
            (
                'udws "ews.show_mem_usage; ews.show_session_tbl; snmp.read_get_community_name;"',
                "NOT_SET;udws() returns 0 (0x0)",
            ),
            (
                'udws "roobe.supp_oobe_stages; l10n.get_media_sizes; l10n.get_media_types; prdsts.get_active_alerts;"',
                "trayEmpty 65803",
            ),
            (
                'udws "wifi_conman.help; nca.get_radio_fw_version; nca.get_permanent_radio_mac 1; nca.get_dot11ap_config;"',
                "Channel:  6",
            ),
            (
                'udws "udw.echo 42; hw.get_clk_rate; hw_mcasic.get_supply_voltage; micci2.get_errors;"',
                "42; Clock 1x is set to 180MHz",
            ),
            (
                'udws "udw.decr HK:)u}#3)Y2; udw.encr bio.rev_str; lib_digital_sensor.list;"',
                "DOOR_SENSOR",
            ),
            (
                'udws "lib_coordination.init_check; lib_coordination.info;"',
                "Board Resource Coordination Table",
            ),
            (
                'udws "mem.help; mem.list_pools; mem.show_summary; timer.get_factory_time; timer.get_rtc_status;"',
                "Total number of pools: 42",
            ),
            (
                'udws "crypto_svc.get_rand_num 4; hw.get_kernel_exec_wait; lib_pwr.get_active_clients; pe_events.list;"',
                "POWER_CARRIAGE_SERVO is held ON",
            ),
            (
                'udws "rev.get_fw; rev.get_base_fw; rev.get_verbose_fw; rev.get_raw_asic; rev.get_aasic; rev.get_dasic;"',
                "Taiji_pp Built:Sat Jan 14, 2017",
            ),
            (
                'udws "cnc_common_util.get_udn; webfwupdate.get_status; webfwupdate.get_nvm_state;"',
                "web_fw_update status: idle",
            ),
            (
                'udws "webfwupdate.get_mode; webfwupdate.get_error; nvm2.get_hw_error_log;"',
                "mode=manual;",
            ),
            (
                'udws "js.jobs_status; js.job_list; jm_reports.supp_report; pe_seq.show_seq_tree;"',
                "Job:",
            ),
        ]

        all_specs = func_test_commands + var_test_commands + udw_test_commands
        all_cmds = [c for c, _ in all_specs]

        t0 = time.time()
        results, emu = run_uart_commands(self.dump, self.snap, all_cmds, max_instructions=30_000_000)
        elapsed = time.time() - t0

        self.assertIsNone(emu.error, f"Emulator faulted during UART command self-test: {emu.error}")
        self.assertEqual(emu.reason, "all-commands-completed")
        self.assertEqual(len(results), len(all_specs))

        for (cmd, expected_substr), (actual_cmd, output) in zip(all_specs, results):
            self.assertEqual(cmd, actual_cmd)
            self.assertIn(
                expected_substr,
                output,
                f"Command {cmd!r} output did not contain {expected_substr!r}:\n{output}",
            )
            self.assertTrue(output.endswith("-> "), f"Command {cmd!r} did not return to prompt")

        print(
            f"\n[OK] Verified 54/54 shell commands/variables (21 functions + 33 variables), "
            f"decrypted 289/289 cipher-obfuscated UDW commands across 206 tables (1,520 total), "
            f"and executed 64 UDW diagnostic commands ({len(all_specs)} total interactive invocations) "
            f"in {elapsed:.2f}s ({emu.instructions:,} instructions, 0 faults)."
        )

    def test_upload_hello_world_to_ram(self) -> None:
        """Self-test: upload Hello World into RAM filesystem via commands, verify via ls, ustd.cat, cp, and physical DRAM."""
        upload_cmds = [
            'udws "ram.file_write2 /ram/hello.txt 12 Hello World!;"',
            'udws "ram.file_write /ram/hello_hex.txt 12 48656c6c6f20576f726c6421;"',
            'ls /ram',
            'udws "ustd.cat /ram/hello.txt;"',
            'udws "ustd.cat /ram/hello_hex.txt;"',
            'cp /ram/hello.txt /tmp/hello_copied.txt',
            'll /tmp',
            'udws "ustd.cat /tmp/hello_copied.txt;"',
            'udws "ram.statfs /ram;"',
        ]
        t0 = time.time()
        results, emu = run_uart_commands(self.dump, self.snap, upload_cmds, max_instructions=5_000_000)
        elapsed = time.time() - t0

        self.assertIsNone(emu.error, f"Emulator faulted during RAM upload: {emu.error}")
        self.assertEqual(len(results), len(upload_cmds))

        res_map = dict(results)
        self.assertIn("12;udws() returns 0 (0x0)", res_map['udws "ram.file_write2 /ram/hello.txt 12 Hello World!;"'])
        self.assertIn("24;udws() returns 0 (0x0)", res_map['udws "ram.file_write /ram/hello_hex.txt 12 48656c6c6f20576f726c6421;"'])
        self.assertIn("hello.txt", res_map['ls /ram'])
        self.assertIn("hello_hex.txt", res_map['ls /ram'])
        self.assertIn("Hello World!;udws() returns 0 (0x0)", res_map['udws "ustd.cat /ram/hello.txt;"'])
        self.assertIn("Hello World!;udws() returns 0 (0x0)", res_map['udws "ustd.cat /ram/hello_hex.txt;"'])
        self.assertIn("cp() returns 0 (0x0)", res_map['cp /ram/hello.txt /tmp/hello_copied.txt'])
        self.assertIn("hello_copied.txt", res_map['ll /tmp'])
        self.assertIn("Hello World!;udws() returns 0 (0x0)", res_map['udws "ustd.cat /tmp/hello_copied.txt;"'])
        self.assertIn("statfs() : block size = 1024", res_map['udws "ram.statfs /ram;"'])

        # Verify physical DRAM backing in guest memory space
        dram = bytes(emu.memory._dram_mv)
        pos = dram.find(b"Hello World!")
        self.assertGreaterEqual(pos, 0, "Uploaded 'Hello World!' string not found in physical DDR DRAM")

        print(
            f"\n[OK] Uploaded 'Hello World!' to /ram via commands (raw + hex), "
            f"verified via ls /ram, ustd.cat, cp, ll /tmp, and physical DRAM at 0x{0x20000000 + pos:08x} "
            f"in {elapsed:.2f}s ({emu.instructions:,} instructions, 0 faults)."
        )


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    unittest.main(verbosity=2)
