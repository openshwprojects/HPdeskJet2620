"""Reproducible, offline UDW descriptor inventory of the verified 2018 image.

This never opens a serial port. A static descriptor is not evidence that the
physical firmware registered it, accepted it, or that executing it is harmless.
The live UART transcripts are the authority for those separate questions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
from collections import Counter, deque

from capstone import Cs, CS_ARCH_ARM, CS_MODE_ARM, CS_MODE_BIG_ENDIAN, CS_MODE_THUMB
from capstone.arm import ARM_OP_IMM, ARM_OP_MEM, ARM_OP_REG, ARM_REG_PC

ROOT = Path(__file__).resolve().parents[1]
ALPHABET = b"._abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
COMPONENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
PARSER = 0xA02C3DE2
PRINTF = 0xA02C37BE
HIERARCHY_REGISTRAR = 0xA02C3022
TEXT_START, TEXT_END = 0x150D0, 0x4F8F00
SCANF_TOKEN = re.compile(r"%(?P<suppress>\*)?(?P<width>\d+)?(?P<length>hh|ll|h|l|L|z|j|t)?(?P<type>[diouxXfFeEgGaAcspn]|\[[^]]*\])")


def hx(n: int) -> str:
    return f"0x{n:08x}"


class Image:
    def __init__(self, path: Path):
        self.path = path
        self.data = path.read_bytes()
        if len(self.data) != 0x4000000:
            raise ValueError("expected the complete 64 MiB offline DDR image")

    def u32(self, off: int) -> int:
        return struct.unpack_from(">I", self.data, off & 0x3FFFFFF)[0]

    def string(self, address: int, limit: int = 512) -> str:
        if address >> 24 not in (0x20, 0x60, 0xA0):
            return ""
        off = address & 0x3FFFFFF
        raw = self.data[off:off + limit]
        end = raw.find(b"\0")
        if end < 0 or any(c < 32 and c not in (9, 10, 13) or c > 126 for c in raw[:end]):
            return ""
        return raw[:end].decode("ascii")

    def code(self, address: int) -> bool:
        return address >> 24 in (0x20, 0x60, 0xA0) and TEXT_START <= (address & 0x3FFFFFF) < TEXT_END

    def cipher(self) -> dict:
        hits = list(re.finditer(re.escape(ALPHABET + b"\0"), self.data))
        if len(hits) != 1:
            raise ValueError("could not uniquely locate the UDW plaintext alphabet")
        address = 0xA0000000 | hits[0].start()
        refs = [m.start() for m in re.finditer(re.escape(struct.pack(">I", address)), self.data)]
        candidates = []
        for off in refs:
            tables = [self.string(self.u32(off + 8 + i * 4)) for i in range(62)]
            if all(len(s) >= 64 and len(set(s[:64])) == 64 for s in tables):
                candidates.append((off, tables))
        if len(candidates) != 1:
            raise ValueError("could not uniquely locate all 62 substitution alphabets")
        off, tables = candidates[0]
        self.plain, self.tables = ALPHABET.decode(), tables
        return {"plain_alphabet_address": hx(address), "pointer_table_address": hx(0xA0000000 | off),
                "plain_alphabet": self.plain, "substitution_table_count": len(tables)}

    def decode(self, name: str) -> str:
        if NAME.fullmatch(name):
            return name
        if len(name) >= 4 and name[1] in self.plain[2:]:
            table = self.tables[self.plain.index(name[1]) - 2]
            if table[28] == name[0] and all(c in table[:64] for c in name[2:-1]):
                return "".join(self.plain[table.index(c)] for c in name[2:-1])
        return name


def verify_image(image: Image, directory: Path) -> list[dict]:
    verification = json.loads((directory / "verification.json").read_text())
    banks = {"dram": image.data, "itcm": (directory / "itcm.bin").read_bytes(),
             "dtcm": (directory / "dtcm.bin").read_bytes()}
    results = []
    for item in verification["final_ranges"]:
        start, end = item["start"], item["end"]
        if start < 0x2000:
            bank, off = "itcm", start
        elif 0x60000000 <= start < 0x60001000:
            bank, off = "dtcm", start - 0x60000000
        else:
            bank, off = "dram", start & 0x3FFFFFF
        actual = hashlib.sha256(banks[bank][off:off + end - start]).hexdigest()
        if actual != item["sha256"]:
            raise ValueError(f"offline image verification failed at {start:#x}")
        results.append({**item, "matches": True, "bank": bank})
    return results


def classification(name: str) -> dict:
    """Conservative NAME-ONLY triage; never an automatic execution permission."""
    low = name.lower()
    leaf = low.split(".")[-1]
    groups = []
    if re.search(r"reset|reboot|poweroff|power_off|shutdown|restart|assert|panic|crash|watchdog|wdog|oops", low):
        groups.append("reset_shutdown_or_fault")
    if re.search(r"write|erase|format|delete|remove|clear|set_|_set$|store|save|commit|download|upload|flash|program|update|enable|disable|invalidate|flush|purge|modify|patch|force|override|inject", low):
        groups.append("state_or_storage_mutation")
    if re.search(r"move|motion|eject|feed|carriage|paper|service|servicing|spit|wipe|prime|nozzle|pen|motor|print|scan|cap$|uncap|calibr|heat|fire", low):
        groups.append("mechanism_ink_or_calibration_related")
    if re.search(r"func_call|call_flow|call_keyword|lua|execute|exec_|script|\.u$", low):
        groups.append("arbitrary_or_scripted_execution")
    if not groups and (leaf.startswith(("get_", "show_", "list_", "read_")) or
                       leaf in {"help", "info", "status", "query", "msecs", "version", "rev_str", "base_rev_str", "list", "cmds"}):
        groups.append("information_candidate")
    return {"groups": groups or ["unknown"], "basis": "name-only heuristic; handler side effects not proved",
            "safe_to_execute": "not established"}


def hierarchy_registrations(image: Image, roots: list[int]) -> list[dict]:
    """Recover literal r0/r1 arguments at direct Thumb BL registrar calls."""
    sites = []
    for off in range(TEXT_START, TEXT_END - 4, 2):
        hi, lo = struct.unpack_from(">HH", image.data, off)
        if hi & 0xF800 != 0xF000 or lo & 0xF800 != 0xF800:
            continue
        displacement = ((hi & 0x7FF) << 12) | ((lo & 0x7FF) << 1)
        if displacement & 0x400000:
            displacement -= 0x800000
        if (0xA0000000 + off + 4 + displacement) & 0xFFFFFFFF == HIERARCHY_REGISTRAR:
            sites.append(0xA0000000 | off)
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_BIG_ENDIAN)
    md.detail = True
    result = []
    for site in sites:
        possibilities = set()
        # Try halfword boundaries because an arbitrary backward window may
        # otherwise begin on the second half of a BL instruction or in data.
        for start in range(site - 64, site, 2):
            regs = {}
            for ins in md.disasm(image.data[start & 0x3FFFFFF:(site & 0x3FFFFFF) + 4], start):
                ops = ins.operands
                if ins.address == site:
                    table, namespace = regs.get("r0"), regs.get("r1")
                    if table is not None and (table & 0x3FFFFFF) in roots and namespace is not None:
                        raw = image.string(namespace)
                        name = raw if COMPONENT.fullmatch(raw) else image.decode(raw)
                        if COMPONENT.fullmatch(name):
                            possibilities.add((table & 0x3FFFFFF, namespace, name))
                    break
                updates = {}
                if ops and ops[0].type == ARM_OP_REG:
                    dest = ins.reg_name(ops[0].reg)
                    if ins.mnemonic == "adr" and ops[1].type == ARM_OP_IMM:
                        updates[dest] = ((ins.address + 4) & ~3) + ops[1].imm
                    elif ins.mnemonic == "ldr" and ops[1].type == ARM_OP_MEM and ops[1].mem.base == ARM_REG_PC:
                        updates[dest] = image.u32(((ins.address + 4) & ~3) + ops[1].mem.disp)
                    elif ins.mnemonic in ("mov", "movs") and len(ops) == 2 and ops[1].type == ARM_OP_REG:
                        source = ins.reg_name(ops[1].reg)
                        if source in regs:
                            updates[dest] = regs[source]
                    elif ins.mnemonic in ("add", "adds") and len(ops) == 3 and ops[1].type == ARM_OP_REG and ops[2].type == ARM_OP_IMM:
                        source = ins.reg_name(ops[1].reg)
                        if source in regs:
                            updates[dest] = regs[source] + ops[2].imm
                _, written = ins.regs_access()
                for reg in written:
                    regs.pop(ins.reg_name(reg), None)
                regs.update(updates)
                if ins.mnemonic in ("bl", "blx"):
                    for register in ("r0", "r1", "r2", "r3", "r12", "lr"):
                        regs.pop(register, None)
                elif ins.mnemonic.startswith("b") or (ins.mnemonic == "pop" and "pc" in ins.op_str):
                    regs.clear()
        if len(possibilities) == 1:
            table, pointer, name = possibilities.pop()
            result.append({"call_address": hx(site), "registrar_address": hx(HIERARCHY_REGISTRAR),
                "table_address": hx(0xA0000000 | table), "namespace_pointer": hx(pointer),
                "raw_namespace": image.string(pointer), "namespace": name,
                "evidence": "literal arguments at static registration call; execution/registration on the physical device is not established"})
        else:
            result.append({"call_address": hx(site), "registrar_address": hx(HIERARCHY_REGISTRAR),
                           "unresolved": True, "candidate_count": len(possibilities)})
    return result


def analyze_handler(image: Image, address: int, known_starts: list[int]) -> dict:
    """Bounded control-flow walk; reports direct parser calls, not a full ABI."""
    start = address & ~1
    following = next((n for n in known_starts if n > start), start + 4096)
    end = min(start + 4096, following)
    md = Cs(CS_ARCH_ARM, (CS_MODE_THUMB if address & 1 else CS_MODE_ARM) | CS_MODE_BIG_ENDIAN)
    md.detail = True
    pending = deque([(start, ())])
    visited = set()
    parsers, strings, calls = {}, {}, set()
    truncated = False
    while pending:
        pc, values = pending.popleft()
        state = (pc, values)
        if state in visited:
            continue
        if len(visited) >= 1500:
            truncated = True
            break
        visited.add(state)
        if not start <= pc < end:
            continue
        off = pc & 0x3FFFFFF
        ins = next(md.disasm(image.data[off:off + 4], pc, count=1), None)
        if ins is None:
            continue
        regs = dict(values)
        ops = ins.operands
        updates = {}
        if ops and ops[0].type == ARM_OP_REG:
            dest = ins.reg_name(ops[0].reg)
            if ins.mnemonic == "adr" and ops[1].type == ARM_OP_IMM:
                updates[dest] = ((pc + 4) & ~3) + ops[1].imm
            elif ins.mnemonic == "ldr" and ops[1].type == ARM_OP_MEM and ops[1].mem.base == ARM_REG_PC:
                literal = ((pc + (4 if address & 1 else 8)) & ~3) + ops[1].mem.disp
                updates[dest] = image.u32(literal)
            elif ins.mnemonic in ("mov", "movs") and len(ops) == 2:
                if ops[1].type == ARM_OP_IMM:
                    updates[dest] = ops[1].imm
                elif ops[1].type == ARM_OP_REG and ins.reg_name(ops[1].reg) in regs:
                    updates[dest] = regs[ins.reg_name(ops[1].reg)]
            elif ins.mnemonic in ("add", "adds") and len(ops) == 3 and ops[1].type == ARM_OP_REG and ops[2].type == ARM_OP_IMM:
                source = ins.reg_name(ops[1].reg)
                if source in regs:
                    updates[dest] = regs[source] + ops[2].imm
        _, written = ins.regs_access()
        for reg in written:
            regs.pop(ins.reg_name(reg), None)
        regs.update(updates)
        for value in updates.values():
            string = image.string(value, 1024)
            if string and len(string) >= 2:
                strings[(value, string)] = {"address": hx(value), "text": string, "referenced_at": hx(pc)}
        if ins.mnemonic in ("bl", "blx"):
            if ops[0].type == ARM_OP_IMM:
                target = ops[0].imm & 0xFFFFFFFE
                calls.add(target)
                if target == PARSER:
                    fmt_address = regs.get("r1")
                    fmt = image.string(fmt_address) if fmt_address is not None else ""
                    parsers[pc] = {"call_address": hx(pc), "parser_address": hx(PARSER),
                        "format_address": hx(fmt_address) if fmt_address is not None else None,
                        "format": fmt or None,
                        "conversions": [m.groupdict() for m in SCANF_TOKEN.finditer(fmt)]}
            for register in ("r0", "r1", "r2", "r3", "r12", "lr"):
                regs.pop(register, None)
        state_values = tuple(sorted(regs.items()))
        if ins.mnemonic == "bx" or (ins.mnemonic == "pop" and "pc" in ins.op_str):
            continue
        if ins.mnemonic.startswith("b") and ins.mnemonic not in ("bl", "blx", "bic", "bics") and ops and ops[0].type == ARM_OP_IMM:
            target = ops[0].imm & 0xFFFFFFFE
            pending.append((target, state_values))
            if ins.mnemonic == "b":
                continue
        pending.append((pc + ins.size, state_values))
    return {"handler_address": hx(address), "analysis_start": hx(start), "analysis_end_exclusive": hx(end),
        "instruction_states_visited": len(visited), "state_budget_exhausted": truncated,
        "direct_argument_parser_calls": list(parsers.values()), "referenced_strings": list(strings.values()),
        "direct_call_targets": [hx(n) for n in sorted(calls)],
        "signature_complete": False,
        "limitations": "Direct bounded CFG only; helper functions, indirect dispatch, constraints, defaults and side effects are not fully analyzed. No parser call does not imply zero arguments."}


def extract(image: Image) -> dict:
    cipher = image.cipher()
    commands = []
    excluded = []
    hierarchy_nodes = {}
    # All aligned words across the whole DDR image, including currently zero BSS.
    for off in range(0, len(image.data) - 12, 4):
        name_pointer = image.u32(off)
        if name_pointer >> 24 not in (0x20, 0x60, 0xA0):
            continue
        raw_name = image.string(name_pointer, 160)
        name = image.decode(raw_name)
        handler = image.u32(off + 4)
        component_name = raw_name if COMPONENT.fullmatch(raw_name) else name
        if handler in (0, 1) and COMPONENT.fullmatch(component_name):
            target = image.u32(off + 8)
            if image.code(target) if handler == 0 else target >> 24 in (0x20, 0x60, 0xA0):
                hierarchy_nodes[off] = {"raw_name": raw_name, "name": component_name,
                    "kind": handler, "target": target, "context": image.u32(off + 12)}
        if not image.code(handler):
            continue
        if not NAME.fullmatch(name):
            continue
        context = image.u32(off + 8)
        if off < TEXT_END or name.endswith((".c", ".h", ".cpp", ".png", ".gz")):
            excluded.append({"name": name, "descriptor_address": hx(0xA0000000 | off),
                "reason": "name/code sequence inside executable text or a source/resource filename; likely literal-pool coincidence, not an established descriptor"})
            continue
        commands.append({"name": name, "raw_name": raw_name, "obfuscated": name != raw_name,
            "subsystem": name.split(".", 1)[0], "descriptor_address": hx(0xA0000000 | off),
            "descriptor_file_offset": off, "name_pointer": hx(name_pointer), "handler": hx(handler),
            "context_word": hx(context), "context_interpretation": "opaque dispatch context; NOT an argument count",
            "static_evidence": "aligned name/code/context descriptor candidate in offline updated image",
            "runtime_registered": "not established", "physically_tested": False,
            "triage": classification(name)})
    # Kind 1 points to another null-terminated 16-byte node array. Kind 0 is
    # a leaf callback. A runtime registry also supplies an outer namespace;
    # do not manufacture that missing prefix from a table's first leaf.
    hierarchy_nodes = {off: n for off, n in hierarchy_nodes.items()
                       if off >= TEXT_END and (n["kind"] == 0 or (n["target"] & 0x3FFFFFF) in hierarchy_nodes
                           or image.data[n["target"] & 0x3FFFFFF:(n["target"] & 0x3FFFFFF) + 16] == bytes(16))}
    child_starts = {n["target"] & 0x3FFFFFF for n in hierarchy_nodes.values() if n["kind"] == 1}
    root_starts = sorted(off for off in hierarchy_nodes if off - 16 not in hierarchy_nodes and off not in child_starts)
    def valid_table(table, ancestry=()):
        if table in ancestry or len(ancestry) >= 4:
            return False
        cursor = table
        while cursor in hierarchy_nodes:
            node = hierarchy_nodes[cursor]
            if node["kind"] == 1 and not valid_table(node["target"] & 0x3FFFFFF, (*ancestry, table)):
                return False
            cursor += 16
        return image.data[cursor:cursor + 16] == bytes(16)
    root_starts = [off for off in root_starts if valid_table(off)]
    hierarchical = []
    def walk(table, prefix=(), ancestry=()):
        if table in ancestry or len(ancestry) >= 4:
            return
        cursor = table
        while cursor in hierarchy_nodes:
            n = hierarchy_nodes[cursor]
            components = (*prefix, n["name"])
            if n["kind"] == 1:
                walk(n["target"] & 0x3FFFFFF, components, (*ancestry, table))
            else:
                hierarchical.append({"relative_name": " ".join(components),
                    "registration_namespace": "unknown; obtain from live iterator or registration analysis",
                    "root_table_address": hx(0xA0000000 | (ancestry[0] if ancestry else table)),
                    "descriptor_address": hx(0xA0000000 | cursor), "handler": hx(n["target"]),
                    "context_word": hx(n["context"]), "format": "16-byte hierarchical node",
                    "all_table_null_terminators_verified": True,
                    "runtime_registered": "not established", "physically_tested": False,
                    "triage": classification("_".join(components))})
            cursor += 16
    for table in root_starts:
        walk(table)
    registrations = hierarchy_registrations(image, root_starts)
    registered_hierarchy = []
    for entry in hierarchical:
        for registration in registrations:
            if entry["root_table_address"] != registration.get("table_address"):
                continue
            name = registration["namespace"] + " " + entry["relative_name"]
            registered_hierarchy.append({**entry, "name": name,
                "registration_namespace": registration["namespace"],
                "registration_call": registration["call_address"],
                "triage": classification(name.replace(" ", "_"))})
    handlers = sorted({int(c["handler"], 16) for c in commands + hierarchical})
    starts = sorted({a & ~1 for a in handlers})
    analysis = {hx(a): analyze_handler(image, a, starts) for a in handlers}
    for command in commands:
        command["argument_analysis_ref"] = command["handler"]
    return {"cipher": cipher, "candidate_descriptor_count": len(commands),
        "unique_candidate_names": len({c["name"] for c in commands}),
        "obfuscated_descriptor_count": sum(c["obfuscated"] for c in commands),
        "subsystems": dict(sorted(Counter(c["subsystem"] for c in commands).items())),
        "commands": commands, "excluded_sequence_candidates": excluded, "hierarchical_leaf_candidates": hierarchical,
        "hierarchical_root_candidate_count": len(root_starts), "hierarchy_registrations": registrations,
        "hierarchical_commands_with_static_registration": registered_hierarchy, "handler_analysis": analysis}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--image", type=Path, default=ROOT / "artifacts/ghs/dram.bin")
    ap.add_argument("--output", type=Path, default=ROOT / "references/static_inventory.json")
    args = ap.parse_args()
    image = Image(args.image)
    checked = verify_image(image, args.image.parent)
    result = extract(image)
    result = {"schema_version": 1, "provenance": {
        "kind": "offline_static_analysis_not_a_physical_command_log",
        "image": str(args.image.resolve()), "image_sha256": hashlib.sha256(image.data).hexdigest(),
        "source_dump_sha256": hashlib.sha256((ROOT / "dump.bin").read_bytes()).hexdigest(),
        "firmware": "2018 GHS update reconstructed from dump.bin", "verified_ranges": checked,
        "enumeration_limitations": "Finds aligned 12-byte data-section descriptors with dotted decoded names and handlers in updated main text, plus null-terminated 16-byte hierarchy candidates. Dormant aliases and callback-table lookalikes may be present. Static registration calls supply some hierarchy namespaces; live enumeration is needed. This is NOT a 100% runtime inventory.",
        "format_limitations": "Parser format literals are recovered evidence, not complete command signatures; descriptor context is not argument count."},
        "command_discovery": {
            "list": {"command": 'udws "udw.cmds;"', "handler": "0xa02c3e1d",
                     "argument_format": "%127s %127s", "gate": "0xa00089de",
                     "note": "Silently returns without listing when gate is nonzero. Zero arguments otherwise lists all; optional string filters narrow the list."},
            "iterator_reset": {"command": 'udws "udw.iter_reset;"', "handler": "0xa02c3e9f",
                               "note": "Resets only the enumeration index at 0x20b85264."},
            "iterator_next": {"command": 'udws "udw.iter_get_next;"', "handler": "0xa02c3ea7",
                              "completion_literal": "iter_done", "literal_address": "0xa02c40fc",
                              "note": "No list gate. Increments the index and outputs one full command name, or iter_done after the registry ends."}},
        **result}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    parsed = sum(bool(a["direct_argument_parser_calls"]) for a in result["handler_analysis"].values())
    print(f"{result['candidate_descriptor_count']} descriptors, {result['unique_candidate_names']} unique names, "
          f"{result['obfuscated_descriptor_count']} decoded; direct parser evidence for {parsed} handlers -> {args.output}")


if __name__ == "__main__":
    main()
