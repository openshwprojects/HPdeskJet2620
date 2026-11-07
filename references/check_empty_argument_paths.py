"""Offline review of exact no-argument UDW invocations in the 2018 image.

This never opens a serial port and does not boot firmware. It runs isolated
handler instructions, substituting 0 and -1 for the known argument parser's
result. The known output formatter is observed, not executed. These paths are
analysis evidence, NOT physical command tests or captured printer responses.

The checker deliberately rejects unknown calls, global/peripheral writes,
nonliteral global reads, uninitialized stack reads, and branches or addresses
dependent on unknown register values. A rejected candidate is not necessarily
dangerous: it requires a separate manual review. Exact empty arguments only!
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re

from capstone import Cs, CS_ARCH_ARM, CS_MODE_BIG_ENDIAN, CS_MODE_THUMB
from capstone.arm import ARM_CC_AL, ARM_CC_INVALID, ARM_OP_IMM, ARM_OP_MEM, ARM_OP_REG
from unicorn import (
    Uc, UcError, UC_ARCH_ARM, UC_MODE_ARM, UC_MODE_BIG_ENDIAN,
    UC_HOOK_CODE, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE, UC_PROT_READ, UC_PROT_EXEC,
)
import unicorn.arm_const as arm


ROOT = Path(__file__).resolve().parents[1]
PARSER = 0xA02C3DE2
FORMATTER = 0xA02C37BE
STOP = 0x70000000
STACK = 0x71000000
STACK_SIZE = 0x10000
HEX = lambda value: f"0x{value:08x}"
CONVERSION = re.compile(r"%([-+ #0]*)(\d+|\*)?(?:\.(\d+|\*))?(hh|ll|h|l|L|z|j|t)?([diouxXfFeEgGaAcspnS%])")
SCAN_CONVERSION = re.compile(r"%(\*)?(\d+)?(hh|ll|h|l|L|z|j|t)?([diouxXfFeEgGaAcspnS%])")


def canonical(name: str) -> str:
    return {"sb": "r9", "sl": "r10", "fp": "r11", "ip": "r12"}.get(name, name)


class Checker:
    def __init__(self, raw: bytes):
        self.raw = raw
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_ARM | UC_MODE_BIG_ENDIAN)
        self.uc.mem_map(0xA0000000, len(raw))
        self.uc.mem_write(0xA0000000, raw)
        self.uc.mem_protect(0xA0000000, len(raw), UC_PROT_READ | UC_PROT_EXEC)
        self.uc.mem_map(STACK, STACK_SIZE)
        self.uc.mem_map(STOP, 0x1000)
        self.md = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_BIG_ENDIAN)
        self.md.detail = True
        self.registers = {f"r{i}": getattr(arm, f"UC_ARM_REG_R{i}") for i in range(13)}
        self.registers.update(sp=arm.UC_ARM_REG_SP, lr=arm.UC_ARM_REG_LR, pc=arm.UC_ARM_REG_PC)
        self.uc.hook_add(UC_HOOK_CODE, self.code)
        self.uc.hook_add(UC_HOOK_MEM_READ, self.read)
        self.uc.hook_add(UC_HOOK_MEM_WRITE, self.write)

    def reg(self, name: str) -> int:
        return self.uc.reg_read(self.registers[canonical(name)])

    def fail(self, reason: str) -> None:
        if self.result["failure"] is None:
            self.result["failure"] = reason
        self.uc.emu_stop()

    def string(self, address: int, limit: int = 1024) -> str | None:
        if 0xA0000000 <= address < 0xA4000000:
            off = address - 0xA0000000
            data = self.raw[off:off + limit]
        elif STACK <= address < STACK + STACK_SIZE:
            limit = min(limit, STACK + STACK_SIZE - address)
            data = bytes(self.uc.mem_read(address, limit))
            end = data.find(b"\0")
            if end < 0 or not all(address + i in self.initialized for i in range(end + 1)):
                return None
        else:
            return None
        end = data.find(b"\0")
        if end < 0:
            return None
        try:
            return data[:end].decode("ascii")
        except UnicodeDecodeError:
            return None

    def stack_known(self, address: int, size: int) -> bool:
        return all(address + i in self.initialized for i in range(size))

    def output_args_known(self, fmt: str) -> bool:
        """Check the simple UDW printf arguments used by error/help messages."""
        slot = 0
        for match in CONVERSION.finditer(fmt):
            flags, width, precision, length, kind = match.groups()
            if kind == "%":
                continue
            if kind == "n" or width == "*" or precision == "*" or length in {"ll", "L"} or kind in "fFeEgGaA":
                return False
            if slot < 2:
                name = f"r{slot + 2}"
                if name not in self.known:
                    return False
                value = self.reg(name)
            else:
                address = self.reg("sp") + 4 * (slot - 2)
                if not self.stack_known(address, 4):
                    return False
                value = int.from_bytes(self.uc.mem_read(address, 4), "big")
            if kind in "sS" and self.string(value) is None:
                return False
            slot += 1
        return True

    def parser_destinations_are_stack(self, fmt: str) -> bool:
        """Keep even the intercepted parser's destinations out of global state.

        Empty string conversions may still terminate their destination buffer.
        Formats containing %n are rejected even though scanf does not count that
        write as a successful conversion. This is stricter than checking r0.
        """
        slot = 0
        matches = list(SCAN_CONVERSION.finditer(fmt))
        if fmt.count("%") != len(matches):
            return False
        destinations = []
        for match in matches:
            suppressed, width, length, kind = match.groups()
            if kind == "n":
                return False
            if suppressed or kind == "%":
                continue
            if slot < 2:
                name = f"r{slot + 2}"
                if name not in self.known:
                    return False
                destination = self.reg(name)
            else:
                argument = self.reg("sp") + 4 * (slot - 2)
                if not self.stack_known(argument, 4):
                    return False
                destination = int.from_bytes(self.uc.mem_read(argument, 4), "big")
            if kind in "sS" and width is None:
                return False
            extent = int(width) + 1 if kind in "sS" else 8 if length in {"ll", "L"} else 4
            if not STACK <= destination < destination + extent <= STACK + STACK_SIZE:
                return False
            destinations.append({"address": HEX(destination), "format": match.group(), "maximum_extent": extent})
            slot += 1
        self.result["parser_destinations"] = destinations
        return True

    def code(self, uc, address: int, size: int, _) -> None:
        if address == STOP:
            if "pc" not in self.known:
                self.fail("return target depended on unknown state")
            else:
                self.result["returned"] = True
                uc.emu_stop()
            return
        if not 0xA0000000 <= address < 0xA4000000:
            self.fail("execution escaped reviewed image")
            return
        ins = next(self.md.disasm(bytes(uc.mem_read(address, size)), address), None)
        if ins is None:
            self.fail("instruction did not decode")
            return
        self.current = ins
        self.before_known = set(self.known)
        self.before_sp = self.reg("sp")
        self.result["instructions"].append(f"{HEX(address)}: {ins.mnemonic} {ins.op_str}")
        mnemonic = ins.mnemonic

        if ins.cc not in (ARM_CC_AL, ARM_CC_INVALID) and not self.flags_known:
            self.fail("conditional instruction depended on unknown flags")
            return
        if mnemonic in ("bl", "blx"):
            if ins.operands[0].type != ARM_OP_IMM:
                self.fail("indirect function call")
                return
            target = ins.operands[0].imm & 0xFFFFFFFE
            self.result["calls"].append({"at": HEX(address), "target": HEX(target)})
            if target == PARSER:
                if "r1" not in self.known:
                    self.fail("parser format depends on unknown input")
                    return
                fmt = self.string(self.reg("r1"))
                if not fmt or "%n" in fmt or not CONVERSION.search(fmt):
                    self.fail("unknown or unsupported parser format")
                    return
                if not self.parser_destinations_are_stack(fmt):
                    self.fail("parser destination is unknown, global, or has unsupported format")
                    return
                self.result["parser_count"] += 1
                self.result["parser_formats"].append(fmt)
                uc.reg_write(arm.UC_ARM_REG_R0, self.result["parser_return"] & 0xFFFFFFFF)
                self.known.difference_update({"r1", "r2", "r3", "r12"})
                self.known.add("r0")
            elif target == FORMATTER and self.result["parser_count"]:
                if "r1" not in self.known:
                    self.fail("output format depends on unknown state")
                    return
                fmt = self.string(self.reg("r1"))
                if fmt is None or not self.output_args_known(fmt):
                    self.fail("output formatting could consume unknown data or pointers")
                    return
                self.result["outputs"].append({"at": HEX(address), "format_address": HEX(self.reg("r1")), "text": fmt})
                # No output is produced; this models the ABI only. The return
                # value and caller-saved registers are deliberately unknown.
                uc.reg_write(arm.UC_ARM_REG_R0, 0)
                self.known.difference_update({"r0", "r1", "r2", "r3", "r12"})
            else:
                self.fail("unreviewed function call " + HEX(target))
                return
            self.flags_known = False
            self.known.update({"pc", "lr"})
            uc.reg_write(arm.UC_ARM_REG_LR, (address + size) | 1)
            uc.reg_write(arm.UC_ARM_REG_PC, (address + size) | 1)
            return

        # A memory address must be independent of the poison values used for
        # unknown inputs, even if a particular concrete run happened to work.
        for operand in ins.operands:
            if operand.type == ARM_OP_MEM:
                for reg in (operand.mem.base, operand.mem.index):
                    if reg and canonical(ins.reg_name(reg)) not in self.before_known:
                        self.fail("memory address depends on unknown register")
                        return
        if mnemonic in ("bx", "blx") and ins.operands[0].type == ARM_OP_REG:
            if canonical(ins.reg_name(ins.operands[0].reg)) not in self.before_known:
                self.fail("branch target depends on unknown register")
                return
        reads, writes = ins.regs_access()
        if any(ins.reg_name(reg) == "cpsr" for reg in reads) and not self.flags_known:
            self.fail("arithmetic depends on unknown condition flags")
            return
        read_names = {canonical(ins.reg_name(reg)) for reg in reads} - {"cpsr"}
        deterministic = read_names <= self.before_known
        if mnemonic in ("push", "pop") or mnemonic.startswith(("stm", "ldm")):
            # Data-register knowledge is updated individually in memory hooks.
            deterministic = "sp" in self.before_known if mnemonic in ("push", "pop") else canonical(ins.reg_name(ins.operands[0].reg)) in self.before_known
        for reg in writes:
            name = canonical(ins.reg_name(reg))
            if name == "cpsr":
                continue
            if deterministic:
                self.known.add(name)
            else:
                self.known.discard(name)
        if ins.update_flags:
            if mnemonic in {"cmp", "cmn", "adds", "subs", "rsbs", "adcs", "sbcs"}:
                self.flags_known = deterministic
            else:
                # MOVS/logical/shift operations can preserve C or V. Do not
                # promote partially known flags to a full known flags state.
                self.flags_known = self.flags_known and deterministic

    def read(self, uc, access, address: int, size: int, value: int, _) -> None:
        ins = self.current
        if STACK <= address < address + size <= STACK + STACK_SIZE:
            known = self.stack_known(address, size)
            if ins.mnemonic == "pop":
                index = (address - self.before_sp) // 4
                if not 0 <= index < len(ins.operands):
                    self.fail("unexpected pop access")
                    return
                name = canonical(ins.reg_name(ins.operands[index].reg))
                if known:
                    self.known.add(name)
                else:
                    self.known.discard(name)
                if name == "pc" and not known:
                    self.fail("pop return target depends on unknown stack data")
            elif not known:
                self.fail("read from uninitialized or unknown stack data")
        elif (0xA0000000 <= address < address + size <= 0xA4000000 and
              any(op.type == ARM_OP_MEM and canonical(ins.reg_name(op.mem.base)) == "pc" for op in ins.operands)):
            pass  # immutable PC-relative literal
        else:
            self.fail("nonliteral global/peripheral read " + HEX(address))

    def write(self, uc, access, address: int, size: int, value: int, _) -> None:
        if not STACK <= address < address + size <= STACK + STACK_SIZE:
            self.fail("non-stack write " + HEX(address))
            return
        ins = self.current
        if ins.mnemonic == "push":
            start = self.before_sp - 4 * len(ins.operands)
            index = (address - start) // 4
            name = canonical(ins.reg_name(ins.operands[index].reg))
            known = name in self.before_known
        elif ins.mnemonic.startswith("stm"):
            base = canonical(ins.reg_name(ins.operands[0].reg))
            # Unicorn memory callbacks precede writeback for this instruction.
            index = (address - self.reg(base)) // 4 + 1
            if not 1 <= index < len(ins.operands):
                self.fail("unrecognized multiple-store addressing")
                return
            known = canonical(ins.reg_name(ins.operands[index].reg)) in self.before_known
        elif ins.mnemonic.startswith("str"):
            known = canonical(ins.reg_name(ins.operands[0].reg)) in self.before_known
        else:
            self.fail("unrecognized stack-writing instruction")
            return
        for byte in range(address, address + size):
            if known:
                self.initialized.add(byte)
            else:
                self.initialized.discard(byte)

    def check(self, handler: int, parser_return: int) -> dict:
        if not handler & 1:
            return {"parser_return": parser_return, "instructions": [], "calls": [],
                    "outputs": [], "parser_count": 0, "parser_formats": [],
                    "returned": False, "failure": "ARM-state handler requires separate review"}
        self.uc.reg_write(arm.UC_ARM_REG_CPSR, 0x230)  # big-endian data, Thumb, User
        for index in range(13):
            self.uc.reg_write(self.registers[f"r{index}"], 0x77777777)
        self.uc.mem_write(STACK, b"\0" * STACK_SIZE)
        self.uc.reg_write(arm.UC_ARM_REG_SP, STACK + STACK_SIZE - 0x100)
        self.uc.reg_write(arm.UC_ARM_REG_LR, STOP | 1)
        self.known = {"sp", "pc", "lr"}
        self.flags_known = False
        self.initialized: set[int] = set()
        self.result = {"parser_return": parser_return, "instructions": [], "calls": [],
                       "outputs": [], "parser_count": 0, "parser_formats": [],
                       "returned": False, "failure": None}
        try:
            self.uc.emu_start(handler, STOP + 4, count=4096)
        except UcError as exc:
            self.fail(str(exc))
        if not self.result["returned"] and self.result["failure"] is None:
            self.result["failure"] = "instruction bound reached before return"
        return self.result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, default=ROOT / "references/static_inventory.json")
    parser.add_argument("--image", type=Path, default=ROOT / "artifacts/ghs/dram.bin")
    parser.add_argument("--review", type=Path, default=ROOT / "references/help_review.json")
    parser.add_argument("--runtime-inventory", type=Path, default=ROOT / "references/physical-05-registry/runtime_inventory.json")
    args = parser.parse_args()
    inventory = json.loads(args.inventory.read_text())
    raw = args.image.read_bytes()
    if hashlib.sha256(raw).hexdigest() != inventory["provenance"]["image_sha256"]:
        raise ValueError("2018 image does not match the inventoried image")
    review = json.loads(args.review.read_text())
    # Recompute entries previously generated by this checker. Preserve the
    # original manual-help/37-usage review and append fresh automated results.
    review["queries"] = [entry for entry in review["queries"]
                         if entry.get("evidence", {}).get("checker") != "references/check_empty_argument_paths.py"]
    for entry in review["queries"]:
        entry.pop("systematic_recheck", None)
    runtime = json.loads(args.runtime_inventory.read_text())
    registered = {entry["name"] for entry in runtime["entries"]}
    checker = Checker(raw)
    descriptors = inventory["commands"] + inventory.get("hierarchical_commands_with_static_registration", [])
    seen_handlers = {}
    existing = {entry["name"]: entry for entry in review["queries"]}
    held = []
    accepted = []
    for desc in descriptors:
        analysis = inventory["handler_analysis"].get(desc["handler"], {})
        if not analysis.get("direct_argument_parser_calls"):
            continue
        handler = int(desc["handler"], 16)
        if handler not in seen_handlers:
            seen_handlers[handler] = [checker.check(handler, result) for result in (0, -1)]
        cases = seen_handlers[handler]
        passed = all(case["returned"] and not case["failure"] and case["parser_count"] == 1 for case in cases)
        if not passed:
            held.append({"name": desc["name"], "handler": desc["handler"],
                         "reasons": sorted({case["failure"] or "not exactly one parser call before return" for case in cases if case["failure"] or case["parser_count"] != 1})})
            continue
        has_usage = any(re.search(r"usage|syntax", output["text"], re.I) for output in cases[0]["outputs"])
        has_output = bool(cases[0]["outputs"])
        behavior = ("usage_or_syntax_text" if has_usage else
                    "prints_initialized_local_defaults" if has_output else
                    "returns_without_operation_or_handler_output")
        entry = {
            "command": f'udws "{desc["name"]};"', "name": desc["name"],
            "kind": "exact_empty_argument_usage_path" if has_usage else "exact_empty_argument_rejection_path",
            "purpose": "Record the exact empty-argument response without reaching the underlying operation.",
            "behavior_from_review": behavior,
            "handler": desc["handler"], "descriptor_address": desc["descriptor_address"],
            "runtime_registered_at_review": desc["name"] in registered,
            "runtime_registry_complete_at_review": runtime.get("complete", False),
            "side_effects_assessment": "Exact empty arguments only. For parser returns 0 and -1 the handler returns without non-stack writes, global/peripheral data reads, unknown calls, uninitialized argument reads, or decisions based on unknown values. All parser destinations are stack-local; %n and unsupported formats are excluded. Any help/error formatter has known arguments. Normal parser/output bookkeeping remains; supplying valid arguments is NOT covered.",
            "evidence": {"checker": "references/check_empty_argument_paths.py", "parser_address": HEX(PARSER), "formatter_address": HEX(FORMATTER), "paths": cases},
            "physically_tested_by_this_review": False,
        }
        if desc["name"] not in existing:
            review["queries"].append(entry)
        else:
            # Preserve earlier provenance while adding stronger reproducible checks.
            existing[desc["name"]]["systematic_recheck"] = entry["evidence"]
        accepted.append(desc["name"])
    review["systematic_sweep"] = {
        "checker": "references/check_empty_argument_paths.py", "checker_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "unique_direct_parser_handlers_checked": len(seen_handlers), "accepted_descriptor_names": accepted,
        "held_descriptor_names": held,
        "method": "Isolated 2018 handler instructions with parser returns 0/-1 and known output formatter intercepted. Conservative known-value tracking rejects unknown branch/address dependence, uninitialized argument reads, unknown callees, non-stack parser destinations, and non-stack writes. This is offline analysis; output format strings are not physical results. Some accepted cases silently return or print initialized defaults rather than report an argument error.",
    }
    for entry in review["queries"]:
        entry["runtime_registered_at_review"] = entry["name"] in registered
        entry["runtime_registry_complete_at_review"] = runtime.get("complete", False)
    review["counts"]["reviewed_exact_invocations"] = len(review["queries"])
    review["counts"]["systematically_verified_empty_argument_invocations"] = len(accepted)
    review["counts"]["systematic_held_invocations"] = len(held)
    args.review.write_text(json.dumps(review, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"handlers_checked": len(seen_handlers), "accepted_names": len(accepted), "held_names": len(held), "total_reviewed_invocations": len(review["queries"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
