"""Revalidate completed physical storage captures and prepare typed array reads.

No serial access. Historical capture validation is retained without alteration.
Defaults join physical-19 and its resumed physical-19b capture. The outputs are
physical-storage-values.json, 23-storage-array-commands.json, and
24-storage-family-commands.json. --check compares without writing.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
from storage_response import validate_storage_response

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "references"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def pack(specs: list[dict], phase: str, max_per_line: int = 4) -> list[dict]:
    lines, pending = [], []

    def line(parts: list[dict]) -> str:
        return 'udws "' + "; ".join(p["udw_command"] for p in parts) + ';"'

    def output_size(parts: list[dict]) -> int:
        # Numeric records print a width-five field and delimiter for the count
        # and every byte (%5d%c); uint32 values use %12u%c. Include echo/status.
        return len(line(parts)) + 64 + sum(
            6 * (p["expected_record_bytes"] + 1)
            if p["name"] in ("ds2.get_rec_array", "ds2.get_rec_array_by_name")
            else p["expected_record_bytes"] + 4 if p["name"] in ("ds2.get_rec_array_string", "ds2.get_rec_array_str_by_name")
            else 13 for p in parts)

    def flush() -> None:
        if pending:
            command = line(pending)
            require(len(command.encode("ascii")) <= 120, "Overlong planned command")
            lines.append({"command": command, "classification": "reviewed-information-query",
                          "purpose": "Read typed array entries using fresh physical bounds." if phase == "array_values" else "Exercise known-name and bounded storage helper read paths.",
                          "phase": phase, "timeout": 90, "udw_count": len(pending),
                          "uart_line_bytes": len(command), "expected": list(pending),
                          "estimated_response_bytes_upper_bound": output_size(pending),
                          "response_batch_limit_bytes": 4096,
                          "review_source": "references/storage_query_review.json"})
            pending.clear()

    for spec in specs:
        if pending and (len(pending) >= max_per_line or len(line(pending + [spec])) > 120 or output_size(pending + [spec]) > 4096):
            flush()
        require(len(line([spec])) <= 120, "Single storage command exceeds line bound")
        require(output_size([spec]) <= 4096, "Single storage response exceeds output bound; review separately")
        pending.append(spec)
    flush()
    for n, item in enumerate(lines, 1):
        item["plan_index"] = n
    return lines


def build(capture_dirs: list[Path]) -> dict[str, object]:
    plan_path = REF / "19-storage-values-commands.json"
    catalog_path = REF / "physical-nvm-catalog.json"
    plan = json.loads(plan_path.read_bytes())
    catalog = json.loads(catalog_path.read_bytes())
    by_command = {item["command"]: item for item in plan}
    require(len(by_command) == len(plan) == 358, "Storage plan is not the reviewed 358-line plan")
    objects = {o["dsid"]: {"dsid": o["dsid"], "name": o["name"], "nvm_type": o["nvm_type"],
                          "category": o["category"], "file_id": o["file_id"],
                          "table_index": o["table_index"], "object_index": o["object_index"],
                          "nvm_contents_observations": [], "ds2_scalar_observations": [],
                          "array_metadata_observations": {}} for o in catalog["objects"]}
    catalog_by_id = {o["dsid"]: o for o in catalog["objects"]}
    revalidations, sessions, seen_commands = [], [], Counter()
    for capture_index, directory in enumerate(capture_dirs):
        session_path = directory / "session.json"
        session_data = session_path.read_bytes()
        session = json.loads(session_data)
        reason = session.get("stop_reason")
        require(reason in ("batch-complete", "response-validation-failed"), f"Capture is unfinished or stopped unexpectedly: {session_path}: {reason}")
        if capture_index == len(capture_dirs) - 1:
            require(reason == "batch-complete", "Last resumed capture must be complete")
        require(session.get("source") == "physical UART", "Only physical captures accepted")
        sessions.append({"session_file": rel(session_path), "sha256": sha(session_data),
                         "original_stop_reason": reason, "started_utc": session["started_utc"],
                         "ended_utc": session.get("ended_utc")})
        for entry in session["commands"]:
            command = entry["command"]
            require(command in by_command, f"Unexpected storage capture command {command}")
            item = by_command[command]
            require(entry.get("prompt_returned") and entry.get("status") in ("prompt-returned", "response-validation-failed"),
                    f"Response did not return prompt: {directory.name}:{entry['index']}")
            response_path = directory / entry["response_file"]
            raw = response_path.read_bytes()
            digest = sha(raw)
            require(len(raw) == entry["response_bytes"] and digest == entry["response_sha256"],
                    f"Raw storage response hash/length mismatch: {response_path}")
            validation = validate_storage_response(item, raw)
            require(validation["valid"], f"Current validator rejected {response_path}: {validation['errors']}")
            seen_commands[command] += 1
            source = {"response_file": rel(response_path), "response_sha256": digest,
                      "started_utc": entry["started_utc"], "plan_index": item["plan_index"]}
            revalidations.append({**source, "command": command, "valid": True, "errors": [],
                                  "parsed_result_count": len(validation["parsed"]),
                                  "original_status": entry.get("status"),
                                  "original_response_validation": entry.get("validation")})
            for result in validation["parsed"]:
                if result["name"] == "nvm2.get_contents":
                    objects[result["dsid"]]["nvm_contents_observations"].append({**result, "source": source})
                elif result["name"] == "ds2.get_list":
                    for value in result["values"]:
                        objects[value["dsid"]]["ds2_scalar_observations"].append({**value, "source": source})
                else:
                    objects[result["dsid"]]["array_metadata_observations"].setdefault(result["name"], []).append({**result, "source": source})
    require(set(seen_commands) == set(by_command), f"Missing {len(set(by_command) - set(seen_commands))} planned storage UART lines")
    scalar_differences, metadata_changes = [], []
    for dsid, obj in objects.items():
        if obj["nvm_contents_observations"]:
            obj["nvm_contents_latest"] = obj["nvm_contents_observations"][-1]
        if obj["category"] == "scalar":
            require(bool(obj["ds2_scalar_observations"]), f"No scalar value for {dsid}")
            obj["ds2_scalar_latest"] = obj["ds2_scalar_observations"][-1]
            obj["ds2_registry_evidence"] = "nonzero returned word distinguishes value from the zero lookup-failure fallback" if obj["ds2_scalar_latest"]["wire_value"] != 0 else "zero is ambiguous: real zero or unregistered/non-scalar lookup fallback; NVM presence alone does not prove DS2 registration"
            if obj.get("nvm_contents_latest"):
                nvm = obj["nvm_contents_latest"]["value"]
                ds2 = obj["ds2_scalar_latest"]["value"]
                obj["scalar_readers_agree"] = nvm == ds2
                if nvm != ds2:
                    scalar_differences.append({"dsid": dsid, "name": obj["name"], "nvm_value": nvm, "ds2_value": ds2,
                                               "note": "DS2 zero may be a missing-registry fallback, particularly for unnamed NVM objects; different capture times may also change values. Retain both observations."})
        else:
            required = ["ds2.get_num_elem"] + (["ds2.get_elem_size"] if obj["category"] == "record_array" else [])
            obj["array_metadata_latest"] = {}
            for name in required:
                rows = obj["array_metadata_observations"].get(name, [])
                require(bool(rows), f"Missing fresh {name} for typed array {dsid}")
                obj["array_metadata_latest"][name] = rows[-1]
                if not rows[-1]["matches_prior_metadata"]:
                    metadata_changes.append({"dsid": dsid, "name": name, "prior": rows[-1]["prior_metadata_value"], "fresh": rows[-1]["value"]})

    array_specs, exclusions = [], []
    for dsid, obj in sorted(objects.items()):
        if obj["category"] == "scalar":
            continue
        metadata = obj["array_metadata_latest"]
        count_row = metadata["ds2.get_num_elem"]
        count = count_row["value"]
        base = {"dsid": dsid, "known_name": obj["name"], "nvm_type": obj["nvm_type"],
                "fresh_element_count": count, "count_source": count_row["source"],
                "type_source": catalog_by_id[dsid]["sources"]["get_all"]}
        if count == 0:
            exclusions.append({**base, "reason": "DS2 count is zero, possibly because this NVM object is absent from the live DS2 registry; no valid DS2 index is established."})
            continue
        if obj["category"] == "record_array":
            size_row = metadata["ds2.get_elem_size"]
            size = size_row["value"]
            base.update({"expected_record_bytes": size, "size_source": size_row["source"]})
            if not 1 <= size <= 1024:
                exclusions.append({**base, "reason": "Record size outside permitted complete-read range 1..1024; no truncated record is presented as complete.", "excluded_record_indices": {"start": 0, "end_exclusive": count}})
                continue
            name, handler = "ds2.get_rec_array", "0xa005cd5d"
        else:
            name, handler = "ds2.get_sim_array", "0xa005cbf5"
        if count > 65536:
            exclusions.append({**base, "reason": "uint16 array-index limit", "excluded_record_indices": {"start": 65536, "end_exclusive": count}})
        for index in range(min(count, 65536)):
            array_specs.append({**base, "name": name, "handler": handler, "array_index": index,
                                "udw_command": f"{name} {dsid} {index}"})
    array_plan = pack(array_specs, "array_values")

    # Bounded named variants and NVM compatibility helpers use observed DSIDs,
    # types, and fresh metadata. No unknown-ID/invalid-index probes are generated.
    scalar = next(o for o in objects.values() if o["name"] == "DSID_POWER_CYCLE_COUNT" and o["category"] == "scalar")
    simple = next(o for o in objects.values() if o["category"] == "simple_array" and o["name"] and o["array_metadata_latest"]["ds2.get_num_elem"]["value"] > 0)
    record = next(o for o in objects.values() if o["name"] == "DSID_MODEL_NAME" and o["category"] == "record_array")
    require(record["array_metadata_latest"]["ds2.get_num_elem"]["value"] > 0, "Model-name record0 unavailable")
    record_size = record["array_metadata_latest"]["ds2.get_elem_size"]["value"]
    require(1 <= record_size <= 1024, "Model-name record exceeds bounded reader")
    family_specs = []

    def family(command: str, name: str, handler: str, obj: dict, **extra: object) -> None:
        spec = {"udw_command": command, "name": name, "handler": handler, "dsid": obj["dsid"],
                "known_name": obj["name"], "nvm_type": obj["nvm_type"],
                "identity_source": catalog_by_id[obj["dsid"]]["sources"]["get_all"], **extra}
        if obj["category"] != "scalar":
            spec.update({"fresh_element_count": obj["array_metadata_latest"]["ds2.get_num_elem"]["value"],
                         "count_source": obj["array_metadata_latest"]["ds2.get_num_elem"]["source"]})
        if obj["category"] == "record_array":
            spec.update({"expected_record_bytes": record_size, "size_source": obj["array_metadata_latest"]["ds2.get_elem_size"]["source"]})
        family_specs.append(spec)

    family(f"ds2.get_by_name {scalar['name']}", "ds2.get_by_name", "0xa005d2d1", scalar)
    family(f"ds2.get_sim_array_by_name {simple['name']} 0", "ds2.get_sim_array_by_name", "0xa005d3a7", simple, array_index=0)
    family(f"ds2.get_rec_array_by_name {record['name']} 0", "ds2.get_rec_array_by_name", "0xa005d55b", record, array_index=0)
    family(f"ds2.get_rec_array_string {record['dsid']} 0", "ds2.get_rec_array_string", "0xa005cf17", record, array_index=0,
           review="A005CF88..CF8E explicitly appends NUL at buffer[bounded_size] before strlen; does not read beyond record-sized buffer.")
    family(f"ds2.get_rec_array_str_by_name {record['name']} 0", "ds2.get_rec_array_str_by_name", "0xa005d735", record, array_index=0)
    for obj in [simple, record]:
        family(f"ds2.get_num_elem_by_name {obj['name']}", "ds2.get_num_elem_by_name", "0xa005d7f7", obj,
               expected_from_prior_metadata=obj["array_metadata_latest"]["ds2.get_num_elem"]["value"])
    family(f"ds2.get_elem_size_by_name {record['name']}", "ds2.get_elem_size_by_name", "0xa005d853", record,
           expected_from_prior_metadata=record_size)
    family(f"nvm2.get_for_c {scalar['dsid']} 0", "nvm2.get_for_c", "0xa02f450f", scalar, array_index=0,
           review="DSID is physically in NVM catalog; scalar index0. No unknown-ID scan or unsupported array type.")
    family(f"nvm2.get_for_c {simple['dsid']} 0", "nvm2.get_for_c", "0xa02f450f", simple, array_index=0,
           review="Known simple-array DSID with fresh count>0; index0 fits uint8.")
    family(f"nvm2.get_rec_array_for_contents {record['dsid']} 0", "nvm2.get_rec_array_for_contents", "0xa02f4569", record,
           array_index=0, byte_index=0, review="Reads byte0 of record0, not record index0 as the second argument. Known record count>0 and record length>0.")
    # String results may contain delimiters, so family probes are isolated lines.
    family_plan = pack(family_specs, "storage_family_samples", 1)
    gap_specs = []
    for dsid, obj in sorted(objects.items()):
        if obj["category"] != "scalar" or obj.get("nvm_contents_latest"):
            continue
        original = catalog_by_id[dsid]
        require(not original["get_contents_addressable"] and original["object_index"] >= 256,
                "Unexpected scalar gap outside the reviewed get_contents uint8 index limit")
        gap_specs.append({"udw_command": f"nvm2.get_for_c {dsid} 0", "name": "nvm2.get_for_c",
                          "handler": "0xa02f450f", "dsid": dsid, "known_name": obj["name"],
                          "nvm_type": obj["nvm_type"], "array_index": 0,
                          "identity_source": original["sources"], "expected_file_id": obj["file_id"],
                          "original_table_index": obj["table_index"], "original_object_index": obj["object_index"],
                          "review": "Known physically enumerated NVM scalar DSID, scalar index0. The NVM backend is independent of the possibly missing DS2 registry entry."})
    require(len(gap_specs) == 46, "Unexpected number of scalar gaps")
    gap_plan = pack(gap_specs, "nvm_scalar_gaps")
    summary = {"catalog_objects": len(objects), "captured_unique_plan_lines": len(seen_commands),
               "captured_responses": len(revalidations), "repeated_plan_lines": sum(n - 1 for n in seen_commands.values()),
               "nvm_contents_objects": sum(bool(o["nvm_contents_observations"]) for o in objects.values()),
               "ds2_scalar_objects": sum(bool(o["ds2_scalar_observations"]) for o in objects.values()),
               "ds2_scalar_nonzero_values_proven": sum(bool(o.get("ds2_scalar_latest", {}).get("wire_value")) for o in objects.values()),
               "ds2_scalar_zero_values_or_lookup_fallback": sum(o.get("ds2_scalar_latest", {}).get("wire_value") == 0 for o in objects.values()),
               "arrays_with_fresh_metadata": sum(bool(o.get("array_metadata_latest")) for o in objects.values()),
               "scalar_reader_differences": len(scalar_differences), "array_metadata_changes": len(metadata_changes),
               "array_read_invocations": len(array_specs), "array_read_uart_lines": len(array_plan),
               "array_exclusions": len(exclusions), "family_sample_uart_lines": len(family_plan)}
    summary.update({"nvm_scalar_gap_invocations": len(gap_specs), "nvm_scalar_gap_uart_lines": len(gap_plan)})
    require(summary["nvm_contents_objects"] == 950 and summary["ds2_scalar_objects"] == 887
            and summary["arrays_with_fresh_metadata"] == 116, "Decoded coverage does not match reviewed typed catalog")
    result = {"schema_version": 1, "provenance": {"source": "physical UART; revalidated offline", "sessions": sessions,
               "catalog": rel(catalog_path), "catalog_sha256": sha(catalog_path.read_bytes()),
               "plan": rel(plan_path), "plan_sha256": sha(plan_path.read_bytes()),
               "validator": "references/storage_response.py", "validator_sha256": sha((REF / "storage_response.py").read_bytes()),
               "generator": rel(Path(__file__)), "generator_sha256": sha(Path(__file__).read_bytes())},
              "summary": summary, "response_revalidations": revalidations,
              "objects": [objects[x] for x in sorted(objects)],
              "scalar_reader_differences": scalar_differences, "array_metadata_changes": metadata_changes,
              "array_read_exclusions": exclusions,
              "interpretation": ["Original capture validation/status is unchanged; current revalidation is an additional finding.",
                                 "Native get_contents prints raw uint32 words including 4294967295 and signed-kind -5 as 4294967291. Canonical signedness follows kind/type, not decimal spelling.",
                                 "Array plans use latest captured metadata, never substitute stale get_all counts.",
                                 "887 scalar response slots are not 887 proven live DS2 values: lookup failures emit zero. A nonzero return is value evidence; zero alone cannot prove registration.",
                                 "All array reads require the same live object configuration; counts are not an authorization for indices beyond the fresh bounds.",
                                 "Record arrays larger than 1024 bytes are explicitly excluded from complete-record reads."]}
    return {"physical-storage-values.json": result, "23-storage-array-commands.json": array_plan,
            "24-storage-family-commands.json": family_plan, "26-storage-gap-commands.json": gap_plan}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", nargs="+", type=Path,
                        default=[REF / "physical-19-storage-values", REF / "physical-19b-storage-values"])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    directories = [p if p.is_absolute() else ROOT / p for p in args.capture]
    products = build(directories)
    for name, obj in products.items():
        data = (json.dumps(obj, indent=2) + "\n").encode("utf-8")
        path = REF / name
        if args.check:
            require(path.read_bytes() == data, f"Generated output differs: {path}")
        else:
            path.write_bytes(data)
    print(json.dumps({"check_only": args.check, **products["physical-storage-values.json"]["summary"]}, indent=2))


if __name__ == "__main__":
    main()
