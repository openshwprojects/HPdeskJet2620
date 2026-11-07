"""Decode complete, bounded physical array and optional storage-helper captures.

Offline only. The original sessions and raw replies are never changed. Defaults
rebuild the finalized artifacts from physical23 arrays, physical24 family reads,
and physical26 scalar gaps. The capture options can select resumed sessions.
Both generation and --check use the same complete default capture set.
"""
from __future__ import annotations

import argparse
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


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def capture(plan_name: str, directories: list[Path]) -> dict:
    plan_path = REF / plan_name
    plan_data = plan_path.read_bytes()
    plan = json.loads(plan_data)
    by_command = {row["command"]: row for row in plan}
    require(len(by_command) == len(plan), "Duplicate planned UART commands")
    seen, rows, sessions, revalidations = set(), [], [], []
    verified_bounds = set()
    for position, directory in enumerate(directories):
        session_path = directory / "session.json"
        session_data = session_path.read_bytes()
        session = json.loads(session_data)
        reason = session.get("stop_reason")
        require(reason in ("batch-complete", "response-validation-failed"), f"Incomplete capture {directory}: {reason}")
        require(position != len(directories) - 1 or reason == "batch-complete", "Final resumed capture must be complete")
        require(session["source"] == "physical UART", "Only physical replies accepted")
        sessions.append({"session_file": relative(session_path), "sha256": digest(session_data),
                         "started_utc": session["started_utc"], "ended_utc": session.get("ended_utc"),
                         "original_stop_reason": reason})
        for entry in session["commands"]:
            command = entry["command"]
            require(command in by_command, f"Unplanned command {command}")
            require(entry.get("prompt_returned"), f"No prompt for {command}")
            spec = by_command[command]
            response_path = directory / entry["response_file"]
            raw = response_path.read_bytes()
            sha = digest(raw)
            require(len(raw) == entry["response_bytes"] and sha == entry["response_sha256"], f"Raw response integrity failure: {response_path}")
            valid = validate_storage_response(spec, raw)
            require(valid["valid"], f"Response failed validation {response_path}: {valid['errors']}")
            require(len(valid["parsed"]) == len(spec["expected"]), "Result/spec count mismatch")
            source = {"response_file": relative(response_path), "response_sha256": sha,
                      "started_utc": entry["started_utc"], "plan_index": spec["plan_index"]}
            revalidations.append({**source, "command": command, "valid": True, "errors": [],
                                  "parsed_result_count": len(valid["parsed"]),
                                  "original_status": entry.get("status"),
                                  "original_response_validation": entry.get("validation")})
            seen.add(command)
            for result, expected in zip(valid["parsed"], spec["expected"]):
                for key in ("count_source", "size_source"):
                    if key not in expected:
                        continue
                    bound = expected[key]
                    identity = (bound["response_file"], bound["response_sha256"])
                    if identity not in verified_bounds:
                        bound_path = ROOT / bound["response_file"]
                        require(digest(bound_path.read_bytes()) == bound["response_sha256"], f"Bound source changed: {bound_path}")
                        verified_bounds.add(identity)
                rows.append({**result, "known_name": expected.get("known_name"), "source": source,
                             "reviewed_input": expected})
    require(seen == set(by_command), f"Missing {len(set(by_command)-seen)} planned UART lines")
    return {"plan_file": relative(plan_path), "plan_sha256": digest(plan_data),
            "sessions": sessions, "response_revalidations": revalidations,
            "unique_plan_lines": len(seen), "results": rows}


def build(array_dirs: list[Path], family_dirs: list[Path], gap_dirs: list[Path]) -> dict:
    data = capture("23-storage-array-commands.json", array_dirs)
    catalog_path = REF / "physical-nvm-catalog.json"
    catalog = {o["dsid"]: o for o in json.loads(catalog_path.read_bytes())["objects"]}
    values_path = REF / "physical-storage-values.json"
    values = json.loads(values_path.read_bytes())
    objects = {}
    for result in data.pop("results"):
        dsid, index = result["dsid"], result["array_index"]
        expected = result["reviewed_input"]
        obj = objects.setdefault(dsid, {"dsid": dsid, "name": catalog[dsid]["name"],
            "nvm_type": catalog[dsid]["nvm_type"], "category": catalog[dsid]["category"],
            "element_count": expected["fresh_element_count"], "count_source": expected["count_source"],
            "record_byte_count": expected.get("expected_record_bytes"),
            "size_source": expected.get("size_source"), "entries": {}})
        if "bytes" in result:
            raw = bytes(result["bytes"])
            result["record_sha256"] = digest(raw)
            result["binary_file"] = f"references/physical-storage-records/{dsid}-{index:05d}.bin"
            prefix = raw.split(b"\0", 1)[0]
            if prefix and all(32 <= byte <= 126 for byte in prefix):
                result["printable_prefix_before_nul"] = prefix.decode("ascii")
                result["text_view_note"] = "Convenience view only; bytes/hex retain the complete authoritative record."
        obj["entries"].setdefault(index, []).append(result)
    array_list = []
    for dsid, obj in sorted(objects.items()):
        require(set(obj["entries"]) == set(range(obj["element_count"])), f"Incomplete array {dsid}")
        obj["entries"] = [{"index": index, "latest": observations[-1], "observations": observations}
                          for index, observations in sorted(obj["entries"].items())]
        obj["complete_for_fresh_metadata"] = True
        array_list.append(obj)
    simple = [entry for obj in array_list if obj["category"] == "simple_array" for entry in obj["entries"]]
    records = [entry for obj in array_list if obj["category"] == "record_array" for entry in obj["entries"]]
    result = {"schema_version": 1, "provenance": {"source": "physical UART; revalidated offline",
        "catalog_file": relative(catalog_path), "catalog_sha256": digest(catalog_path.read_bytes()),
        "storage_values_file": relative(values_path), "storage_values_sha256": digest(values_path.read_bytes()),
        "validator_file": "references/storage_response.py", "validator_sha256": digest((REF / "storage_response.py").read_bytes()),
        "generator_file": relative(Path(__file__)), "generator_sha256": digest(Path(__file__).read_bytes())},
        "capture": data, "arrays": array_list, "exclusions": values["array_read_exclusions"],
        "summary": {"complete_arrays": len(array_list), "simple_array_entries": len(simple),
                    "binary_records": len(records), "binary_record_bytes": sum(e["latest"]["record_byte_count"] for e in records),
                    "excluded_arrays": len(values["array_read_exclusions"]), "uart_lines": data["unique_plan_lines"]},
        "interpretation": ["Array identity follows the exact echoed command and reviewed physical bounds; array replies do not repeat the DSID.",
                           "Binary records remain decimal-byte lists plus lossless hex, never inferred C strings.",
                           "Exported binaries contain logical bytes returned by DS2; packed NVM/flash representations can differ (e.g. DSID66224 reports NVM2 bytes versus DS2 3bytes).",
                           "Zero-count DS2 replies do not establish a readable index; NVM objects may be absent from the DS2 registry."]}
    if family_dirs:
        result["family_samples"] = capture("24-storage-family-commands.json", family_dirs)
    if gap_dirs:
        gaps = capture("26-storage-gap-commands.json", gap_dirs)
        direct = {row["dsid"]: row for row in gaps["results"]}
        require(len(direct) == 46 and all(row["nvm_type"] in ("NVM_SIM_UNSIGNED", "NVM_SIM_SIGNED") for row in direct.values()), "Unexpected direct scalar gap coverage")
        contents_ids = {obj["dsid"] for obj in values["objects"] if obj["category"] == "scalar" and obj.get("nvm_contents_latest")}
        require(len(contents_ids) == 841 and not (contents_ids & set(direct)), "Direct scalar sources must be disjoint841+46")
        merged, comparisons = [], []
        for obj in values["objects"]:
            if obj["category"] != "scalar":
                continue
            nvm = obj.get("nvm_contents_latest") or direct.get(obj["dsid"])
            require(nvm is not None, f"No direct NVM value for scalar {obj['dsid']}")
            ds2 = obj["ds2_scalar_latest"]
            merged.append({"dsid": obj["dsid"], "name": obj["name"], "nvm_type": obj["nvm_type"],
                           "file_id": obj["file_id"], "table_index": obj["table_index"], "object_index": obj["object_index"],
                           "nvm_value": nvm["value"], "nvm_wire_value": nvm["wire_value"], "nvm_source": nvm["source"],
                           "nvm_reader": nvm["name"], "ds2_value": ds2["value"], "ds2_source": ds2["source"],
                           "nvm_observations": obj["nvm_contents_observations"] if obj.get("nvm_contents_latest") else [row for row in gaps["results"] if row["dsid"] == obj["dsid"]],
                           "ds2_observations": obj["ds2_scalar_observations"],
                           "ds2_registry_evidence": obj["ds2_registry_evidence"]})
            if nvm["value"] != ds2["value"]:
                comparisons.append({"dsid": obj["dsid"], "nvm_value": nvm["value"], "ds2_value": ds2["value"],
                                    "note": "Different reader/capture time; DS2 zero may also be the lookup-failure fallback."})
        require(len(merged) == 887, "Direct NVM scalar union must cover all887 known scalar objects")
        result["direct_nvm_scalar_gap_capture"] = gaps
        result["direct_nvm_scalar_values"] = merged
        result["direct_nvm_vs_ds2_differences"] = comparisons
        result["summary"].update({"direct_nvm_scalar_values": len(merged), "direct_nvm_scalar_gap_values": len(direct),
                                  "direct_nvm_get_contents_scalar_values": len(contents_ids),
                                  "direct_nvm_vs_ds2_differences": len(comparisons)})
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", nargs="+", type=Path, default=[REF / "physical-23-storage-arrays"])
    parser.add_argument("--family-capture", nargs="+", type=Path, default=[REF / "physical-24-storage-families"])
    parser.add_argument("--gap-capture", nargs="+", type=Path, default=[REF / "physical-26-storage-gaps"])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    def full(paths: list[Path]) -> list[Path]:
        return [p if p.is_absolute() else ROOT / p for p in paths]
    result = build(full(args.capture), full(args.family_capture), full(args.gap_capture))
    output = REF / "physical-storage-arrays.json"
    encoded = (json.dumps(result, indent=2) + "\n").encode("utf-8")
    for obj in result["arrays"]:
        if obj["category"] != "record_array":
            continue
        for entry in obj["entries"]:
            row = entry["latest"]
            binary_path = ROOT / row["binary_file"]
            binary_data = bytes(row["bytes"])
            if args.check:
                require(binary_path.read_bytes() == binary_data, f"Exported record differs: {binary_path}")
            else:
                binary_path.parent.mkdir(parents=True, exist_ok=True)
                binary_path.write_bytes(binary_data)
    if args.check:
        require(output.read_bytes() == encoded, "Decoded array artifact differs")
    else:
        output.write_bytes(encoded)
    if result.get("direct_nvm_scalar_values"):
        scalar_path = REF / "physical-storage-scalars.json"
        scalar_result = {"schema_version": 1, "provenance": result["provenance"],
                         "summary": {"direct_nvm_scalar_values": 887, "get_contents_values": 841,
                                     "get_for_c_gap_values": 46, "source_sets_disjoint": True,
                                     "nvm_vs_ds2_differences": len(result["direct_nvm_vs_ds2_differences"])},
                         "gap_capture": result["direct_nvm_scalar_gap_capture"],
                         "values": result["direct_nvm_scalar_values"],
                         "nvm_vs_ds2_differences": result["direct_nvm_vs_ds2_differences"],
                         "interpretation": ["Complete logical values for the887 scalar IDs in the physically enumerated1003-object NVM catalog.",
                                            "Each scalar has direct NVM-reader evidence, independent of ambiguous DS2 zero replies. Canonical signedness follows the captured NVM type.",
                                            "Captures are sequential and not an atomic snapshot; running counters can change between readers.",
                                            "This is a typed logical scalar catalog, not the complete raw64KiB NVM byte image."]}
        scalar_encoded = (json.dumps(scalar_result, indent=2) + "\n").encode("utf-8")
        if args.check:
            require(scalar_path.read_bytes() == scalar_encoded, "Decoded scalar artifact differs")
        else:
            scalar_path.write_bytes(scalar_encoded)
    print(json.dumps({"check_only": args.check, **result["summary"]}, indent=2))


if __name__ == "__main__":
    main()
