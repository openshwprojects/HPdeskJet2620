"""Join captured physical NVM metadata and prepare bounded read-only commands.

No serial library, device access, or firmware mutation is used. Run from any cwd:
    python references/prepare_storage_reads.py
    python references/prepare_storage_reads.py --check

The three generated JSON files are deterministic. --check validates inputs and
compares the generated content with existing files without writing anything.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "references"
SCALAR_TYPES = {"NVM_SIM_UNSIGNED", "NVM_SIM_SIGNED"}
SIMPLE_TYPES = {"NVM_SIM_ARRAY_UNSIGNED", "NVM_SIM_ARRAY_SIGNED"}
RECORD_TYPES = {"NVM_REC_ARRAY", "NVM_REC_ARRAY_ASCII6", "NVM_REC_ARRAY_ASCII7"}
TYPE_KIND = {"NVM_SIM_SIGNED": 1, "NVM_SIM_UNSIGNED": 2,
             "NVM_SIM_ARRAY_SIGNED": 3, "NVM_SIM_ARRAY_UNSIGNED": 4,
             **{t: 5 for t in RECORD_TYPES}}
MAX_LINE = 120
MAX_COMMANDS = 4
MAX_SCALARS = 12


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Captures:
    def __init__(self) -> None:
        self.sources: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}

    def session(self, dirname: str) -> dict:
        if dirname not in self.sessions:
            path = REF / dirname / "session.json"
            data = path.read_bytes()
            obj = json.loads(data)
            require(obj.get("source") == "physical UART", f"Not physical: {path}")
            require(obj.get("stop_reason") == "batch-complete", f"Incomplete: {path}")
            self.sources[relative(path)] = {"sha256": sha(data), "kind": "session_manifest"}
            self.sessions[dirname] = obj
        return self.sessions[dirname]

    def response(self, dirname: str, entry: dict) -> tuple[str, str]:
        require(entry.get("status") == "prompt-returned" and entry.get("prompt_returned"),
                f"No completed prompt for {entry['command']}")
        path = REF / dirname / entry["response_file"]
        data = path.read_bytes()
        require(len(data) == entry["response_bytes"], f"Length mismatch: {path}")
        require(sha(data) == entry["response_sha256"], f"Hash mismatch: {path}")
        text = data.decode("ascii").replace("\r\n", "\n").replace("\r", "\n")
        command = entry["command"]
        require(text.count(command) == 1, f"Ambiguous command echo: {path}")
        after_echo = text.split(command, 1)[1]
        marker = "udws() returns 0 (0x0)"
        require(after_echo.count(marker) == 1, f"No unique successful wrapper return: {path}")
        payload, suffix = after_echo.split(marker)
        require(suffix.strip() == "->", f"Unexpected response tail: {path}")
        key = relative(path)
        self.sources[key] = {"sha256": sha(data), "bytes": len(data),
                             "command": command, "command_index": entry["index"],
                             "started_utc": entry["started_utc"],
                             "kind": "physical_uart_response"}
        return payload.strip(), key

    def exact(self, dirname: str, command: str) -> tuple[str, str]:
        entries = [e for e in self.session(dirname)["commands"] if e["command"] == command]
        require(len(entries) == 1, f"Expected one capture of {command!r} in {dirname}")
        return self.response(dirname, entries[0])


def source_row(source: str, row: int) -> dict:
    return {"response": source, "payload_line": row}


def make_line(parts: list[str]) -> str:
    return 'udws "' + "; ".join(parts) + ';"'


def pack(phase: str, specs: list[dict], max_commands: int = MAX_COMMANDS) -> list[dict]:
    result: list[dict] = []
    pending: list[dict] = []

    def flush() -> None:
        if not pending:
            return
        line = make_line([s["udw_command"] for s in pending])
        require(len(line.encode("ascii")) <= MAX_LINE, "Generated overlong UART line")
        result.append({"command": line, "classification": "reviewed-information-query",
                       "phase": phase, "purpose": {
                           "nvm_contents": "Read bounded NVM scalar values or array counts.",
                           "ds2_scalars": "Read physically catalogued scalar IDs in order.",
                           "array_metadata": "Refresh valid array counts and record sizes before future reads."
                       }[phase], "timeout": 60, "uart_line_bytes": len(line),
                       "udw_count": len(pending), "expected": list(pending),
                       "review_source": "references/storage_query_review.json"})
        pending.clear()

    for spec in specs:
        line = make_line([s["udw_command"] for s in pending] + [spec["udw_command"]])
        if pending and (len(pending) >= max_commands or len(line) > MAX_LINE):
            flush()
        require(len(make_line([spec["udw_command"]])) <= MAX_LINE, "Single command too long")
        pending.append(spec)
    flush()
    return result


def build() -> dict[str, object]:
    captures = Captures()
    review_path = REF / "storage_query_review.json"
    rules_data = review_path.read_bytes()
    rules = json.loads(rules_data)
    captures.sources[relative(review_path)] = {"sha256": sha(rules_data), "kind": "offline_handler_review"}

    # Ordered physical list_files response, not file IDs invented from table indices.
    payload, files_source = captures.exact("physical-10-reviewed-queries", 'udws "nvm2.list_files;"')
    require(re.fullmatch(r"[\d,\s]+;", payload) is not None, "Malformed list_files payload")
    numbers = [int(x) for x in re.findall(r"\d+", payload)]
    count, file_ids = numbers[0], numbers[1:]
    require(count == len(file_ids) == len(set(file_ids)) == 101, "File list/count mismatch")
    require(file_ids == [e["file_id"] for e in rules["file_table"]["entries"]],
            "Reviewed file map differs from physical list_files")
    files = [{"table_index": index, "file_id": fid, "sources": {"file_order": files_source}}
             for index, fid in enumerate(file_ids)]
    by_file = {e["file_id"]: e for e in files}

    payload, all_source = captures.exact("physical-16-storage-metadata", 'udws "nvm2.get_all;"')
    objects: dict[int, dict] = {}
    for lineno, line in enumerate(payload.splitlines(), 1):
        if not line.strip() or line.strip() == ";":
            continue
        match = re.fullmatch(r"\s*(\d+):(NVM_[A-Z0-9_]+):([\d:]+)\s*", line)
        require(match is not None, f"Malformed get_all row {lineno}: {line!r}")
        dsid, kind = int(match[1]), match[2]
        values = [int(x) for x in match[3].split(":")]
        require(dsid not in objects and dsid > 0, f"Duplicate/invalid DSID {dsid}")
        require(kind in TYPE_KIND, f"Unreviewed type {kind}")
        obj = {"dsid": dsid, "nvm_type": kind, "name": None,
               "get_contents_kind": TYPE_KIND[kind],
               "sources": {"get_all": source_row(all_source, lineno)}}
        if kind in SCALAR_TYPES:
            require(len(values) == 2 and 1 <= values[0] <= 4, f"Unexpected scalar shape {dsid}")
            obj.update({"category": "scalar", "byte_width": values[0],
                        "signed": kind == "NVM_SIM_SIGNED", "object_field_0x18": values[1]})
        else:
            require(len(values) == 3 and values[0] > 0, f"Unexpected array shape {dsid}")
            require(values[1] % values[0] == 0, f"Nonintegral stored element width {dsid}")
            obj.update({"category": "simple_array" if kind in SIMPLE_TYPES else "record_array",
                        "element_count_from_get_all": values[0], "total_bytes_from_get_all": values[1],
                        "bytes_per_element_from_get_all": values[1] // values[0],
                        "object_field_0x18": values[2]})
        objects[dsid] = obj
    require(len(objects) == 1003, f"Expected 1003 physical NVM IDs, found {len(objects)}")

    # Full reset-group reports have two missing rows in a reproducible merged-line
    # defect. Smaller physical filter captures provide independent missing rows.
    reset_payloads = [captures.exact("physical-16-storage-metadata", 'udws "nvm2.list_reset_group;"')]
    details = captures.session("physical-18-storage-details")
    for entry in details["commands"]:
        if re.fullmatch(r'udws "nvm2\.list_reset_group (PARTIAL|SEMI|FULL|FACTORY|ROM);"', entry["command"]):
            reset_payloads.append(captures.response("physical-18-storage-details", entry))
    require(len(reset_payloads) == 6, "Missing reset-group recovery captures")
    reset_pattern = re.compile(r"(?<!\S)(\d+)[ \t]+([A-Z_0-9]+)[ \t]+([A-Z_]+)[ \t]+(\d+)[ \t]*(DSID_\w+)?[ \t]*$")
    reset: dict[int, dict] = {}
    anomalies: list[dict] = []
    reset_capture_counts: list[dict] = []
    for text, source in reset_payloads:
        seen: set[int] = set()
        for lineno, line in enumerate(text.splitlines(), 1):
            if not re.match(r"^\s*\d", line):
                continue
            match = reset_pattern.search(line)
            require(match is not None, f"Unparseable numeric reset row: {source}:{lineno}")
            prefix = line[:match.start()].strip()
            if prefix:
                require(prefix == "2295  USER_RESET       PAR", f"New unexpected malformed prefix {prefix!r}")
                anomalies.append({"source": source_row(source, lineno), "raw_line": line,
                                  "discarded_incomplete_prefix": prefix,
                                  "action": "Retain valid trailing 493 row; recover missing 2295 objects from smaller filtered captures."})
            fid, group, level, dsid, name = int(match[1]), match[2], match[3], int(match[4]), match[5]
            require(dsid in objects and fid in by_file and dsid not in seen, "Invalid/duplicate reset membership")
            seen.add(dsid)
            row = {"file_id": fid, "reset_group": group, "reset_level_name": level, "name": name}
            if dsid in reset:
                require({k: reset[dsid][k] for k in row} == row, f"Conflicting reset metadata for {dsid}")
            else:
                reset[dsid] = {**row, "sources": []}
            reset[dsid]["sources"].append(source_row(source, lineno))
        reset_capture_counts.append({"source": source, "unique_rows": len(seen),
                                     "missing_from_full_catalog": sorted(set(objects) - seen)})
    require(set(reset) == set(objects), "Reset metadata union does not cover all physical NVM IDs")
    for dsid, row in reset.items():
        objects[dsid].update({k: v for k, v in row.items() if k != "sources"})
        objects[dsid]["sources"]["reset_membership"] = row["sources"]

    # Each index bound and within-file DSID order is established independently.
    joined: set[int] = set()
    for table in files:
        index, fid = table["table_index"], table["file_id"]
        text, source = captures.exact("physical-18-storage-details", f'udws "nvm2.get_table_size {index};"')
        match = re.fullmatch(r"\s*(\d+);\s*(\d+);\s*", text)
        require(match is not None and int(match[1]) == count, f"Bad table bound {index}")
        table["object_count"] = int(match[2])
        table["sources"]["table_size"] = source
        text, source = captures.exact("physical-18-storage-details", f'udws "nvm2.list_objects_in_file {fid};"')
        match = re.fullmatch(r"\s*(\d+)\s*(,.*)?;\s*", text, re.S)
        require(match is not None, f"Bad object list header {fid}")
        reported = int(match[1])
        fields = (match[2] or "").split(",")[1:]
        require(reported == len(fields) == table["object_count"], f"Object count disagreement {fid}")
        table["sources"]["object_list"] = source
        table["dsids_in_physical_order"] = []
        for object_index, field in enumerate(fields):
            triple = re.fullmatch(r"\s*(\d+):\s*(\d+):\s*(\d+)\s*", field)
            require(triple is not None, f"Malformed object triple {fid}:{object_index}")
            dsid, reset_number, returned_fid = map(int, triple.groups())
            require(dsid in objects and dsid not in joined and returned_fid == fid,
                    f"Bad/duplicate object join {dsid}")
            require(objects[dsid]["file_id"] == fid, f"Reset/file mismatch for {dsid}")
            joined.add(dsid)
            table["dsids_in_physical_order"].append(dsid)
            objects[dsid].update({"table_index": index, "object_index": object_index,
                                 "reset_level_numeric": reset_number,
                                 "get_contents_addressable": object_index <= 255})
            objects[dsid]["sources"]["file_object_list"] = {"response": source, "object_ordinal": object_index}
            objects[dsid]["sources"]["table_size"] = table["sources"]["table_size"]
    require(joined == set(objects), "Table object union differs from get_all IDs")
    require(sum(t["object_count"] for t in files) == 1003, "Wrong total table objects")

    ordered = [objects[x] for t in files for x in t["dsids_in_physical_order"]]
    accessible = [o for o in ordered if o["get_contents_addressable"]]
    excluded = [o for o in ordered if not o["get_contents_addressable"]]
    scalars = [objects[x] for x in sorted(objects) if objects[x]["category"] == "scalar"]
    arrays = [objects[x] for x in sorted(objects) if objects[x]["category"] != "scalar"]
    require(len(accessible) == 950 and len(excluded) == 53, "Unexpected uint8 index coverage")
    require(all(o["table_index"] == 61 and 256 <= o["object_index"] <= 308 for o in excluded),
            "Unexpected unaddressable table indices")
    require(len(scalars) == 887 and len(arrays) == 116, "Unexpected scalar/array census")

    contents_specs = [{"udw_command": f'nvm2.get_contents {o["table_index"]} {o["object_index"]}',
                       "name": "nvm2.get_contents", "handler": "0xa02f46cd", "dsid": o["dsid"],
                       "known_name": o["name"], "nvm_type": o["nvm_type"], "expected_file_id": o["file_id"],
                       "file_table_index": o["table_index"], "object_index": o["object_index"],
                       "expected_kind": o["get_contents_kind"],
                       "value_interpretation": "scalar value" if o["category"] == "scalar" else "array element count, not array contents",
                       "bound_source": o["sources"]["table_size"],
                       "identity_source": o["sources"]["file_object_list"]} for o in accessible]
    scalar_specs = []
    for start in range(0, len(scalars), MAX_SCALARS):
        group = scalars[start:start + MAX_SCALARS]
        scalar_specs.append({"udw_command": "ds2.get_list " + " ".join(str(o["dsid"]) for o in group),
                             "name": "ds2.get_list", "handler": "0xa005cae1",
                             "expected_count": len(group), "dsids_in_response_order": [o["dsid"] for o in group],
                             "nvm_types_in_response_order": [o["nvm_type"] for o in group],
                             "type_sources": [o["sources"]["get_all"] for o in group]})
    metadata_specs = []
    for obj in arrays:
        for name, handler in [("ds2.get_num_elem", "0xa005d221")] + (
                [("ds2.get_elem_size", "0xa005d275")] if obj["category"] == "record_array" else []):
            metadata_specs.append({"udw_command": f'{name} {obj["dsid"]}', "name": name,
                                   "handler": handler, "dsid": obj["dsid"], "known_name": obj["name"],
                                   "nvm_type": obj["nvm_type"], "type_source": obj["sources"]["get_all"],
                                   "expected_from_prior_metadata": obj["element_count_from_get_all"] if name.endswith("num_elem") else obj["bytes_per_element_from_get_all"],
                                   "interpretation": "Fresh response is required before planning any array element/record reads; prior value is a comparison, not a substituted fresh result."})
    commands = (pack("nvm_contents", contents_specs) + pack("ds2_scalars", scalar_specs, 1)
                + pack("array_metadata", metadata_specs))
    for index, command in enumerate(commands, 1):
        command["plan_index"] = index
    counts = {"tables": len(files), "nvm_objects": len(objects), "named_objects": sum(o["name"] is not None for o in objects.values()),
              "unnamed_objects": sum(o["name"] is None for o in objects.values()),
              "scalar_objects": len(scalars), "simple_array_objects": sum(o["category"] == "simple_array" for o in arrays),
              "record_array_objects": sum(o["category"] == "record_array" for o in arrays),
              "get_contents_queries": len(contents_specs), "get_contents_excluded_objects": len(excluded),
              "ds2_get_list_queries": len(scalar_specs), "array_metadata_queries": len(metadata_specs),
              "uart_lines": len(commands), "uart_lines_by_phase": dict(Counter(c["phase"] for c in commands)),
              "udw_invocations": sum(c["udw_count"] for c in commands),
              "max_uart_line_bytes": max(c["uart_line_bytes"] for c in commands)}
    provenance = {"kind": "joined_physical_metadata_not_emulator_output", "generator": relative(Path(__file__)),
                  "generator_sha256": sha(Path(__file__).read_bytes()), "sources": captures.sources,
                  "firmware_image_sha256_from_review": rules["provenance"]["image_sha256"],
                  "no_serial_access_by_generator": True}
    catalog = {"schema_version": 1, "provenance": provenance, "counts": counts,
               "validation": {"all_capture_hashes_and_lengths_verified": True,
                              "all_used_commands_returned_prompt": True,
                              "file_list_table_sizes_object_lists_get_all_sets_agree": True,
                              "reset_membership_union_covers_all_objects": True,
                              "reset_capture_counts": reset_capture_counts,
                              "capture_anomalies": anomalies},
               "interpretation": ["object_field_0x18 is metadata, not the object's value.",
                                  "Object index comes from physical list_objects_in_file order, not DSID sort order.",
                                  "A blank printed name remains null; no names are invented.",
                                  "This catalogs NVM objects only; it is not proof of the whole volatile DS2 registry.",
                                  "The generated read plan has not been executed by this offline script."],
               "tables": files, "objects": [objects[x] for x in sorted(objects)]}
    limits = {"schema_version": 1, "source_review": relative(review_path), "counts": counts,
              "limits": {"max_uart_line_ascii_bytes_excluding_CRLF": MAX_LINE,
                         "max_udw_invocations_per_uart_line": MAX_COMMANDS,
                         "ds2_get_list_max_ids": MAX_SCALARS,
                         "nvm_get_contents_max_table_index": 255,
                         "nvm_get_contents_max_object_index": 255,
                         "ds2_array_max_index": 65535, "ds2_record_max_returned_bytes": 1024},
              "excluded_get_contents": [{"dsid": o["dsid"], "name": o["name"], "table_index": o["table_index"],
                                         "file_id": o["file_id"], "object_index": o["object_index"],
                                         "reason": "Index exceeds uint8; sending it would wrap to a different object.",
                                         "alternative": "Typed scalar get_list included" if o["category"] == "scalar" else "Fresh typed array metadata included; no array read planned yet."} for o in excluded],
              "future_array_requirements": ["Use fresh successful get_num_elem result for each known array ID.",
                                            "For record arrays also require fresh get_elem_size result.",
                                            "Read only 0 <= index < fresh count and index <= 65535.",
                                            "Record byte lengths above 1024 are truncated by the supplied reader; preserve that limitation.",
                                            "No unsupported-type, unknown-ID, out-of-bounds, reset, erase, write, or gate-change probes."],
              "capture_anomalies": anomalies,
              "notes": ["Same observed 101-table firmware/file mapping is a precondition for execution.",
                        "Get_contents returns values only for scalars; array results are counts.",
                        "Typed DS2 scalar reads can cover scalar IDs excluded by the NVM helper's uint8 object index.",
                        "UART output/source hashes remain available in the physical catalog."]}
    return {"physical-nvm-catalog.json": catalog, "19-storage-values-commands.json": commands,
            "storage-query-limits.json": limits}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate deterministic generated outputs without writing.")
    args = parser.parse_args()
    products = build()
    for name, obj in products.items():
        data = (json.dumps(obj, indent=2, ensure_ascii=True) + "\n").encode("utf-8")
        path = REF / name
        if args.check:
            require(path.read_bytes() == data, f"Generated output differs: {path}")
        else:
            path.write_bytes(data)
    print(json.dumps({"check_only": args.check, **products["physical-nvm-catalog.json"]["counts"]}, indent=2))


if __name__ == "__main__":
    main()
