"""Strict offline validation of the bounded physical storage-read plan.

The helper performs no I/O. Inputs are one plan item and its complete captured
response. It rejects missing echoes/prompts, extra result groups, wrong object
identities/types, and malformed or out-of-range numeric output.
"""
from __future__ import annotations

import re


UINT32_MAX = (1 << 32) - 1
INT32_MIN = -(1 << 31)
INT32_MAX = (1 << 31) - 1
INTEGER = re.compile(r"-?\d+\Z")
RECORD_TYPES = ("NVM_REC_ARRAY", "NVM_REC_ARRAY_ASCII6", "NVM_REC_ARRAY_ASCII7")
SIMPLE_ARRAY_TYPES = ("NVM_SIM_ARRAY_SIGNED", "NVM_SIM_ARRAY_UNSIGNED")
SCALAR_TYPES = ("NVM_SIM_SIGNED", "NVM_SIM_UNSIGNED")
STRING_READS = ("ds2.get_rec_array_string", "ds2.get_rec_array_str_by_name")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _integer(text: str, low: int, high: int, label: str) -> int:
    word = text.strip()
    _require(INTEGER.fullmatch(word) is not None, f"{label}: expected decimal integer, got {word!r}")
    value = int(word)
    _require(low <= value <= high, f"{label}: {value} outside [{low}, {high}]")
    return value


def _signed32(value: int) -> int:
    return value - (1 << 32) if value > INT32_MAX else value


def validate_storage_response(item: dict, text: str | bytes) -> dict:
    """Return {'valid': bool, 'parsed': list, 'errors': list} without side effects.

    nvm2.get_contents emits file,DSID,kind,value; (commas inside each
    semicolon-terminated result). Physical DSID 78954 demonstrates unsigned32
    wire output (4294967295) even though the value represents a 32-bit word.
    Both signed32 and unsigned32 decimal representations are range checked;
    the explicit kind determines the canonical signedness of scalar values.
    ds2.get_list emits count,value,...; with unsigned32 values. Array metadata
    emits one signed decimal count/size followed by a semicolon.
    """
    parsed: list[dict] = []
    result = {"valid": False, "parsed": parsed, "errors": []}
    try:
        if isinstance(text, bytes):
            text = text.decode("ascii")
        _require(isinstance(text, str), "Response must be text or ASCII bytes")
        command = item["command"]
        expected = item["expected"]
        _require(isinstance(expected, list) and 1 <= len(expected) <= 4, "Plan must describe one to four UDW results")
        _require(command.isascii() and len(command) <= 120, "Plan command is not within reviewed UART limit")
        _require(item.get("udw_count", len(expected)) == len(expected), "Plan UDW count mismatch")
        reconstructed = 'udws "' + "; ".join(e["udw_command"] for e in expected) + ';"'
        _require(reconstructed == command, "Plan command and expected subcommands differ")
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        _require(normalized.count(command) == 1, "Expected exactly one exact command echo")
        before, after = normalized.split(command, 1)
        _require(re.fullmatch(r"\s*(?:->\s*)?", before) is not None,
                 "Unexpected output before command echo")
        _require(after.startswith("\n"), "Echo is not terminated by a line ending")
        marker = "udws() returns 0 (0x0)"
        _require(after.count(marker) == 1, "Expected exactly one udws return 0 marker")
        body, tail = after.split(marker, 1)
        _require(re.fullmatch(r"\s*->\s*", tail) is not None, "Missing final prompt or unexpected output after return")
        body = body.strip()
        _require(body.endswith(";"), "Storage output lacks final semicolon")
        # Isolated bounded strings may themselves contain commas/semicolons.
        strings = any(e["name"] in STRING_READS for e in expected)
        _require(not strings or len(expected) == 1, "String reads must be isolated UART lines")
        groups = [body[:-1]] if strings else body[:-1].split(";")
        _require(len(groups) == len(expected),
                 f"Expected {len(expected)} semicolon result groups; received {len(groups)}")
        for ordinal, (spec, group) in enumerate(zip(expected, groups), 1):
            label = f"result {ordinal} ({spec['udw_command']})"
            name = spec["name"]
            fields = group.split(",")
            if name == "nvm2.get_contents":
                _require(len(fields) == 4, f"{label}: expected file,DSID,kind,value")
                fid = _integer(fields[0], 0, 65535, label + " file ID")
                dsid = _integer(fields[1], 1, UINT32_MAX, label + " DSID")
                kind = _integer(fields[2], 1, 6, label + " kind")
                # Do not confuse the logical signedness with the formatter's
                # representation of the returned 32-bit machine word.
                raw = _integer(fields[3], INT32_MIN, UINT32_MAX, label + " 32-bit wire value")
                _require(fid == spec["expected_file_id"], f"{label}: file ID {fid} != expected {spec['expected_file_id']}")
                _require(dsid == spec["dsid"], f"{label}: DSID {dsid} != expected {spec['dsid']}")
                _require(kind == spec["expected_kind"], f"{label}: kind {kind} != expected {spec['expected_kind']}")
                value = _signed32(raw & UINT32_MAX) if kind == 1 else raw & UINT32_MAX
                if kind in (3, 4, 5):
                    _require(0 <= raw <= 65535, f"{label}: NVM array count outside uint16")
                parsed.append({"name": name, "dsid": dsid, "file_id": fid, "kind": kind,
                               "table_index": spec["file_table_index"], "object_index": spec["object_index"],
                               "wire_value": raw, "value": value,
                               "value_interpretation": "scalar" if kind in (1, 2) else "array_count"})
            elif name == "ds2.get_list":
                ids = spec["dsids_in_response_order"]
                types = spec["nvm_types_in_response_order"]
                _require(1 <= len(ids) <= 12 and all(x > 0 for x in ids), f"{label}: invalid planned scalar IDs")
                _require(len(types) == len(ids) == spec["expected_count"], f"{label}: inconsistent planned scalar count")
                _require(len(fields) == len(ids) + 1, f"{label}: scalar output field count mismatch")
                count = _integer(fields[0], 1, 12, label + " returned count")
                _require(count == len(ids), f"{label}: returned scalar count {count} != {len(ids)}")
                values = []
                for dsid, nvm_type, field in zip(ids, types, fields[1:]):
                    _require(nvm_type in ("NVM_SIM_SIGNED", "NVM_SIM_UNSIGNED"), f"{label}: planned non-scalar type")
                    raw = _integer(field, 0, UINT32_MAX, f"{label} DSID {dsid} unsigned32 wire value")
                    values.append({"dsid": dsid, "nvm_type": nvm_type, "wire_value": raw,
                                   "value": _signed32(raw) if nvm_type == "NVM_SIM_SIGNED" else raw})
                parsed.append({"name": name, "count": count, "values": values})
            elif name in ("ds2.get_num_elem", "ds2.get_elem_size", "ds2.get_num_elem_by_name", "ds2.get_elem_size_by_name"):
                _require(len(fields) == 1, f"{label}: metadata must contain exactly one integer")
                nvm_type = spec["nvm_type"]
                record = nvm_type in RECORD_TYPES
                simple = nvm_type in SIMPLE_ARRAY_TYPES
                _require(record or (simple and name.startswith("ds2.get_num_elem")), f"{label}: unsupported metadata/type pairing")
                value = _integer(fields[0], 0, INT32_MAX, label + " metadata value")
                parsed.append({"name": name, "dsid": spec["dsid"], "nvm_type": nvm_type,
                               "value": value, "prior_metadata_value": spec.get("expected_from_prior_metadata"),
                               "matches_prior_metadata": value == spec.get("expected_from_prior_metadata")})
            elif name in ("ds2.get_sim_array", "ds2.get_sim_array_by_name", "ds2.get_rec_array", "ds2.get_rec_array_by_name", *STRING_READS):
                nvm_type = spec["nvm_type"]
                index = spec["array_index"]
                _require(0 <= index <= 65535 and index < spec["fresh_element_count"], f"{label}: invalid planned array index")
                base = {"name": name, "dsid": spec["dsid"], "nvm_type": nvm_type, "array_index": index}
                if name in ("ds2.get_sim_array", "ds2.get_sim_array_by_name"):
                    _require(nvm_type in SIMPLE_ARRAY_TYPES and len(fields) == 1, f"{label}: invalid simple-array type/output")
                    raw = _integer(fields[0], 0, UINT32_MAX, label + " unsigned32 wire value")
                    parsed.append({**base, "wire_value": raw, "value": _signed32(raw) if nvm_type == "NVM_SIM_ARRAY_SIGNED" else raw})
                else:
                    size = spec["expected_record_bytes"]
                    _require(nvm_type in RECORD_TYPES and 1 <= size <= 1024, f"{label}: invalid planned complete-record bounds")
                    if name in STRING_READS:
                        value = group.strip()
                        _require(value.isascii() and len(value) <= size and "\x00" not in value, f"{label}: invalid bounded record string")
                        parsed.append({**base, "value": value, "maximum_record_bytes": size,
                                       "interpretation": "NUL-terminated string view; original decimal bytes are the authoritative binary record."})
                    else:
                        _require(len(fields) == size + 1, f"{label}: record field count differs from expected {size} bytes")
                        count = _integer(fields[0], 1, 1024, label + " record byte count")
                        _require(count == size, f"{label}: returned record length {count} != expected {size}")
                        data = [_integer(field, 0, 255, f"{label} byte {i}") for i, field in enumerate(fields[1:])]
                        parsed.append({**base, "record_byte_count": count, "bytes": data, "hex": bytes(data).hex()})
            elif name in ("ds2.get_by_name", "nvm2.get_for_c", "nvm2.get_rec_array_for_contents"):
                _require(len(fields) == 1, f"{label}: expected one numeric value")
                nvm_type = spec["nvm_type"]
                if name == "nvm2.get_rec_array_for_contents":
                    _require(nvm_type in RECORD_TYPES and spec["fresh_element_count"] > 0, f"{label}: record0 is not available")
                    _require(0 <= spec["byte_index"] <= 255 and spec["byte_index"] < spec["expected_record_bytes"], f"{label}: invalid record0 byte index")
                    value = _integer(fields[0], 0, 255, label + " record0 byte")
                    parsed.append({"name": name, "dsid": spec["dsid"], "array_index": 0, "byte_index": spec["byte_index"], "value": value})
                else:
                    scalar = nvm_type in SCALAR_TYPES
                    _require(scalar or (name == "nvm2.get_for_c" and nvm_type in SIMPLE_ARRAY_TYPES), f"{label}: unsupported scalar/simple-array type")
                    if name == "nvm2.get_for_c":
                        index = spec["array_index"]
                        _require(index == 0 if scalar else 0 <= index <= 255 and index < spec["fresh_element_count"], f"{label}: invalid NVM helper index")
                    raw = _integer(fields[0], 0, UINT32_MAX, label + " unsigned32 wire value")
                    parsed.append({"name": name, "dsid": spec["dsid"], "nvm_type": nvm_type,
                                   "wire_value": raw, "value": _signed32(raw) if nvm_type in ("NVM_SIM_SIGNED", "NVM_SIM_ARRAY_SIGNED") else raw})
            else:
                raise ValueError(f"{label}: unreviewed storage response name")
        result["valid"] = True
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        result["errors"].append(str(exc))
    return result
