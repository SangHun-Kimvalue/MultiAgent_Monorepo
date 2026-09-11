"""Deterministic, read-only structural checker for T13 Goal/Intent ledgers."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, NoReturn, Sequence


JsonObject = dict[str, Any]

REASON_ORDER = (
    "CONTRACT_MANIFEST_MISSING",
    "CONTRACT_INPUT_MISSING",
    "PHASE_LEDGER_MISSING",
    "LEDGER_MISSING",
    "ARTIFACT_INPUT_MISSING",
    "INPUT_ENCODING_INVALID",
    "INPUT_JSON_PARSE_ERROR",
    "INPUT_JSONL_PARSE_ERROR",
    "INPUT_SCHEMA_INVALID",
    "INPUT_NON_CANONICAL",
    "CONTRACT_MANIFEST_DIGEST_MISMATCH",
    "CONTRACT_INPUT_SET_MISMATCH",
    "CONTRACT_DIGEST_MISMATCH",
    "CONTRACT_REVISION_REUSED",
    "CONTRACT_PARENT_MISMATCH",
    "PHASE_LEDGER_DIGEST_MISMATCH",
    "LEDGER_SEQUENCE_GAP",
    "LEDGER_CHAIN_MISMATCH",
    "PATH_INVALID",
    "OBJECTIVE_DELTA",
    "SCOPE_DELTA",
    "WRITE_SCOPE_VIOLATION",
    "UNDECLARED_CLAIM",
    "CLAIM_STATE_CONFLICT",
    "UNCLASSIFIED_CLAIM",
    "CLAIM_EVIDENCE_LINK_MISSING",
    "MISSING_REQUIRED_ARTIFACT",
    "ARTIFACT_IDENTITY_MISMATCH",
    "ARTIFACT_DIGEST_MISMATCH",
    "VERDICT_SOURCE_LINK_MISSING",
    "VERDICT_SOURCE_ARTIFACT_MISSING",
    "VERDICT_SOURCE_ADAPTER_UNSUPPORTED",
    "VERDICT_SOURCE_PARSE_ERROR",
    "VERDICT_SOURCE_STATUS_MISMATCH",
    "VERDICT_SOURCE_EXIT_MISMATCH",
    "HUMAN_GATE_REQUIRED",
)

HUMAN_GATE_BASE = frozenset(
    {
        "CONTRACT_DIGEST_MISMATCH",
        "CONTRACT_REVISION_REUSED",
        "CONTRACT_PARENT_MISMATCH",
        "PHASE_LEDGER_DIGEST_MISMATCH",
        "OBJECTIVE_DELTA",
        "SCOPE_DELTA",
        "WRITE_SCOPE_VIOLATION",
        "UNDECLARED_CLAIM",
        "CLAIM_STATE_CONFLICT",
        "UNCLASSIFIED_CLAIM",
        "CLAIM_EVIDENCE_LINK_MISSING",
        "MISSING_REQUIRED_ARTIFACT",
        "ARTIFACT_IDENTITY_MISMATCH",
        "ARTIFACT_DIGEST_MISMATCH",
        "VERDICT_SOURCE_LINK_MISSING",
        "VERDICT_SOURCE_ARTIFACT_MISSING",
        "VERDICT_SOURCE_ADAPTER_UNSUPPORTED",
        "VERDICT_SOURCE_PARSE_ERROR",
        "VERDICT_SOURCE_STATUS_MISMATCH",
        "VERDICT_SOURCE_EXIT_MISMATCH",
    }
)

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
HEX_RE = re.compile(r"^[0-9a-f]{64}$")
ROLES = {"ORCHESTRATOR", "PLANNER", "IMPLEMENTER", "REVIEWER", "MECHANICAL", "HUMAN"}
ACTIONS = {
    "CONTRACT_ACTIVATED",
    "LEG_RECORDED",
    "ROUND_RECORDED",
    "CHECKPOINT_RECORDED",
    "CLOSEOUT_RECORDED",
}
ENVELOPE_KEYS = {
    "status",
    "exit_code",
    "backend",
    "model",
    "duration_s",
    "stdout",
    "stderr_sanitized",
    "fallback_used",
    "not_claimed",
}


class _CliContractError(Exception):
    pass


class _EncodingError(Exception):
    pass


class _JsonError(Exception):
    pass


class _JsonlError(Exception):
    pass


class _SchemaError(Exception):
    pass


class _NonCanonicalError(Exception):
    pass


class _ClosedParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _CliContractError(message)

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        raise _CliContractError(message or f"parser exit {status}")


def _build_parser() -> argparse.ArgumentParser:
    parser = _ClosedParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--contract-manifest", action="append")
    parser.add_argument("--contract", action="append", default=[])
    parser.add_argument("--ledger", action="append")
    parser.add_argument("--artifact", action="append", default=[])
    return parser


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    args = _build_parser().parse_args(list(argv))
    if args.contract_manifest is None or len(args.contract_manifest) != 1:
        raise _CliContractError("exactly one --contract-manifest is required")
    if args.ledger is None or len(args.ledger) != 1:
        raise _CliContractError("exactly one --ledger is required")
    return args


def _canonical_json(value: JsonObject) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _object_digest(value: JsonObject, self_field: str) -> str:
    return _sha256(_canonical_json({key: item for key, item in value.items() if key != self_field}))


def _reject_constant(value: str) -> NoReturn:
    raise _JsonError(value)


def _pairs_object(pairs: list[tuple[str, Any]]) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise _JsonError(f"duplicate key: {key}")
        result[key] = value
    return result


def _has_surrogate(value: Any) -> bool:
    if isinstance(value, str):
        return any(0xD800 <= ord(char) <= 0xDFFF for char in value)
    if isinstance(value, list):
        return any(_has_surrogate(item) for item in value)
    if isinstance(value, dict):
        return any(_has_surrogate(key) or _has_surrogate(item) for key, item in value.items())
    return False


def _decode_utf8(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        raise _EncodingError
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _EncodingError from exc
    return text


def _loads_json(data: bytes) -> JsonObject:
    text = _decode_utf8(data)
    try:
        value = json.loads(
            text,
            object_pairs_hook=_pairs_object,
            parse_constant=_reject_constant,
        )
    except _JsonError:
        raise
    except (json.JSONDecodeError, ValueError) as exc:
        raise _JsonError from exc
    if not isinstance(value, dict) or _has_surrogate(value):
        raise _JsonError
    return value


def _loads_jsonl(data: bytes) -> list[JsonObject]:
    text = _decode_utf8(data)
    if not text or not text.endswith("\n"):
        raise _NonCanonicalError
    lines = text[:-1].split("\n")
    if not lines or any(line == "" for line in lines):
        raise _NonCanonicalError
    values: list[JsonObject] = []
    for line in lines:
        try:
            value = json.loads(
                line,
                object_pairs_hook=_pairs_object,
                parse_constant=_reject_constant,
            )
        except _JsonError:
            raise _JsonlError from None
        except (json.JSONDecodeError, ValueError) as exc:
            raise _JsonlError from exc
        if not isinstance(value, dict) or _has_surrogate(value):
            raise _JsonlError
        values.append(value)
    return values


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _normalize_repo_path(value: str) -> str | None:
    raw = value.strip().replace("\\", "/")
    if not raw or re.match(r"^[A-Za-z]:", raw) or raw.startswith("/"):
        return None
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


def _glob_regex(pattern: str) -> str:
    pieces: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                index += 1
                if index + 1 < len(pattern) and pattern[index + 1] == "/":
                    index += 1
                    pieces.append("(?:.*/)?")
                else:
                    pieces.append(".*")
            else:
                pieces.append("[^/]*")
        elif char == "?":
            pieces.append("[^/]")
        else:
            pieces.append(re.escape(char))
        index += 1
    return "".join(pieces)


def _path_matches(path: str, pattern: str) -> bool:
    normalized_path = _normalize_repo_path(path)
    normalized_pattern = _normalize_repo_path(pattern)
    if normalized_path is None or normalized_pattern is None:
        return False
    if any(char in normalized_pattern for char in "*?"):
        return bool(re.fullmatch(_glob_regex(normalized_pattern), normalized_path))
    return normalized_path == normalized_pattern or normalized_path.startswith(
        f"{normalized_pattern}/"
    )


def _resolve_inputs(
    cwd: Path, raw_paths: Sequence[str]
) -> tuple[dict[str, Path], bool, bool]:
    root = cwd.resolve(strict=True)
    resolved: dict[str, Path] = {}
    invalid = False
    duplicate = False
    for raw in raw_paths:
        logical = _normalize_repo_path(raw)
        if logical is None:
            invalid = True
            continue
        if logical in resolved:
            duplicate = True
            continue
        candidate = cwd / logical
        try:
            physical = candidate.resolve(strict=False)
            physical.relative_to(root)
        except (OSError, ValueError):
            invalid = True
            continue
        resolved[logical] = candidate
    return resolved, invalid, duplicate


def _exact_keys(value: JsonObject, expected: set[str]) -> None:
    if set(value) != expected:
        raise _SchemaError


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _id(value: Any) -> str:
    if not isinstance(value, str) or ID_RE.fullmatch(value) is None:
        raise _SchemaError
    return value


def _hex(value: Any, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or HEX_RE.fullmatch(value) is None:
        raise _SchemaError
    return value


def _nonempty_string(value: Any) -> str:
    if not isinstance(value, str) or value == "":
        raise _SchemaError
    return value


def _sorted_unique_strings(
    value: Any, *, ids: bool = False, ordering_is_schema: bool = False
) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise _SchemaError
    items = list(value)
    if ids:
        for item in items:
            _id(item)
    if len(items) != len(set(items)):
        raise _SchemaError
    if items != sorted(items):
        if ordering_is_schema:
            raise _SchemaError
        raise _NonCanonicalError
    return items


def _sorted_objects(value: Any, key: str) -> list[JsonObject]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise _SchemaError
    objects = list(value)
    keys = [_id(item.get(key)) for item in objects]
    if len(keys) != len(set(keys)):
        raise _SchemaError
    if keys != sorted(keys):
        raise _NonCanonicalError
    return objects


def _validate_manifest(value: JsonObject) -> bool:
    _exact_keys(value, {"schema_version", "contracts", "manifest_digest"})
    if value["schema_version"] != 1 or not _is_int(value["schema_version"]):
        raise _SchemaError
    _hex(value["manifest_digest"])
    rows = value["contracts"]
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise _SchemaError
    tuples: list[tuple[str, int]] = []
    for row in rows:
        _exact_keys(row, {"contract_id", "revision", "path", "sha256"})
        contract_id = _id(row["contract_id"])
        revision = row["revision"]
        if not _is_int(revision) or revision < 1:
            raise _SchemaError
        path = _nonempty_string(row["path"])
        if _normalize_repo_path(path) != path:
            raise _SchemaError
        _hex(row["sha256"])
        tuples.append((contract_id, revision))
    duplicate_tuple = len(tuples) != len(set(tuples))
    if not duplicate_tuple and tuples != sorted(tuples):
        raise _NonCanonicalError
    return duplicate_tuple


def _validate_contract(value: JsonObject) -> None:
    _exact_keys(
        value,
        {
            "schema_version",
            "contract_id",
            "revision",
            "parent_contract_digest",
            "objective",
            "non_goals",
            "allowed_write_scope",
            "forbidden_outcomes",
            "validation_claims",
            "required_artifacts",
            "required_verdict_sources",
            "contract_digest",
        },
    )
    if value["schema_version"] != 1 or not _is_int(value["schema_version"]):
        raise _SchemaError
    _id(value["contract_id"])
    revision = value["revision"]
    if not _is_int(revision) or revision < 1:
        raise _SchemaError
    parent = _hex(value["parent_contract_digest"], nullable=True)
    if (revision == 1 and parent is not None) or (revision > 1 and parent is None):
        raise _SchemaError
    _nonempty_string(value["objective"])
    _sorted_unique_strings(value["non_goals"])
    scopes = _sorted_unique_strings(value["allowed_write_scope"])
    if any(_normalize_repo_path(item) != item for item in scopes):
        raise _SchemaError
    _sorted_unique_strings(value["forbidden_outcomes"], ids=True)
    claims = _sorted_objects(value["validation_claims"], "claim_id")
    artifacts = _sorted_objects(value["required_artifacts"], "artifact_id")
    sources = _sorted_objects(value["required_verdict_sources"], "verdict_source_id")
    artifact_ids: set[str] = set()
    for row in artifacts:
        _exact_keys(row, {"artifact_id", "artifact_type", "path"})
        artifact_ids.add(_id(row["artifact_id"]))
        _id(row["artifact_type"])
        path = _nonempty_string(row["path"])
        if _normalize_repo_path(path) != path:
            raise _SchemaError
    source_ids: set[str] = set()
    for row in sources:
        _exact_keys(
            row,
            {
                "verdict_source_id",
                "artifact_id",
                "adapter",
                "required_status",
                "required_exit_code",
            },
        )
        source_ids.add(_id(row["verdict_source_id"]))
        if _id(row["artifact_id"]) not in artifact_ids:
            raise _SchemaError
        _id(row["adapter"])
        if row["required_status"] != "PASS" or row["required_exit_code"] != 0:
            raise _SchemaError
    for row in claims:
        _exact_keys(
            row,
            {"claim_id", "required_artifact_ids", "required_verdict_source_ids"},
        )
        _id(row["claim_id"])
        required_artifacts = _sorted_unique_strings(
            row["required_artifact_ids"], ids=True, ordering_is_schema=True
        )
        required_sources = _sorted_unique_strings(
            row["required_verdict_source_ids"], ids=True, ordering_is_schema=True
        )
        if not set(required_artifacts).issubset(artifact_ids):
            raise _SchemaError
        if not set(required_sources).issubset(source_ids):
            raise _SchemaError
    _hex(value["contract_digest"])


def _validate_contract_ref(value: Any) -> JsonObject:
    if not isinstance(value, dict):
        raise _SchemaError
    _exact_keys(value, {"contract_id", "revision", "path", "sha256"})
    _id(value["contract_id"])
    if not _is_int(value["revision"]) or value["revision"] < 1:
        raise _SchemaError
    path = _nonempty_string(value["path"])
    if _normalize_repo_path(path) != path:
        raise _SchemaError
    _hex(value["sha256"])
    return value


def _validate_phase_ref(value: Any) -> JsonObject:
    if not isinstance(value, dict):
        raise _SchemaError
    _exact_keys(value, {"artifact_type", "path", "sha256"})
    if value["artifact_type"] != "canonical_phase_ledger_snapshot":
        raise _SchemaError
    path = _nonempty_string(value["path"])
    if _normalize_repo_path(path) != path:
        raise _SchemaError
    _hex(value["sha256"])
    return value


def _validate_entries(values: list[JsonObject]) -> None:
    if not values:
        raise _SchemaError
    base_keys = {
        "schema_version",
        "sequence",
        "phase_id",
        "round_id",
        "leg_id",
        "role",
        "action_type",
        "contract_ref",
        "phase_ledger_ref",
        "changed_paths",
        "artifact_refs",
        "claimed_pass",
        "not_claimed",
        "claim_evidence_links",
        "previous_entry_digest",
        "entry_digest",
    }
    for index, entry in enumerate(values):
        action = entry.get("action_type")
        expected = base_keys | ({"closeout_status"} if action == "CLOSEOUT_RECORDED" else set())
        _exact_keys(entry, expected)
        if entry["schema_version"] != 1 or not _is_int(entry["schema_version"]):
            raise _SchemaError
        if not _is_int(entry["sequence"]) or entry["sequence"] < 1:
            raise _SchemaError
        for key in ("phase_id", "round_id", "leg_id"):
            _id(entry[key])
        if entry["role"] not in ROLES or action not in ACTIONS:
            raise _SchemaError
        if action == "CLOSEOUT_RECORDED" and entry["closeout_status"] not in {
            "STRUCTURAL_TRAJECTORY_COMPLETE",
            "STRUCTURAL_TRAJECTORY_INCOMPLETE",
        }:
            raise _SchemaError
        if action == "CLOSEOUT_RECORDED" and index != len(values) - 1:
            raise _SchemaError
        _validate_contract_ref(entry["contract_ref"])
        _validate_phase_ref(entry["phase_ledger_ref"])
        _sorted_unique_strings(entry["changed_paths"])
        artifacts = _sorted_objects(entry["artifact_refs"], "artifact_id")
        for row in artifacts:
            _exact_keys(row, {"artifact_id", "artifact_type", "path", "sha256"})
            _id(row["artifact_id"])
            _id(row["artifact_type"])
            path = _nonempty_string(row["path"])
            if _normalize_repo_path(path) != path:
                raise _SchemaError
            _hex(row["sha256"])
        _sorted_unique_strings(entry["claimed_pass"], ids=True)
        _sorted_unique_strings(entry["not_claimed"], ids=True)
        links = _sorted_objects(entry["claim_evidence_links"], "claim_id")
        for row in links:
            _exact_keys(row, {"claim_id", "artifact_ids", "verdict_source_ids"})
            _id(row["claim_id"])
            _sorted_unique_strings(
                row["artifact_ids"], ids=True, ordering_is_schema=True
            )
            _sorted_unique_strings(
                row["verdict_source_ids"], ids=True, ordering_is_schema=True
            )
        _hex(entry["previous_entry_digest"], nullable=True)
        _hex(entry["entry_digest"])


def _validate_envelope(value: JsonObject) -> None:
    _exact_keys(value, ENVELOPE_KEYS)
    status = value["status"]
    exit_code = value["exit_code"]
    if status not in {"PASS", "CHANGES_REQUESTED", "BLOCKED"} or not _is_int(exit_code):
        raise _SchemaError
    if (status, exit_code) not in {
        ("PASS", 0),
        ("CHANGES_REQUESTED", 1),
        ("BLOCKED", 2),
        ("BLOCKED", 70),
        ("BLOCKED", 124),
    }:
        raise _SchemaError
    for key in ("backend", "model", "stdout", "stderr_sanitized"):
        if not isinstance(value[key], str):
            raise _SchemaError
    duration = value["duration_s"]
    if (
        not isinstance(duration, (int, float))
        or isinstance(duration, bool)
        or not math.isfinite(float(duration))
        or duration < 0
    ):
        raise _SchemaError
    if not isinstance(value["fallback_used"], bool):
        raise _SchemaError
    if not isinstance(value["not_claimed"], list) or any(
        not isinstance(item, str) for item in value["not_claimed"]
    ):
        raise _SchemaError


def _reason_from_owned_json(data: bytes, validator: Any, digest_field: str) -> tuple[JsonObject | None, str | None]:
    try:
        value = _loads_json(data)
    except _EncodingError:
        return None, "INPUT_ENCODING_INVALID"
    except _JsonError:
        return None, "INPUT_JSON_PARSE_ERROR"
    try:
        validator(value)
    except _SchemaError:
        return None, "INPUT_SCHEMA_INVALID"
    except _NonCanonicalError:
        return None, "INPUT_NON_CANONICAL"
    if data != _canonical_json(value) + b"\n":
        return None, "INPUT_NON_CANONICAL"
    if value[digest_field] != _object_digest(value, digest_field):
        return value, (
            "CONTRACT_MANIFEST_DIGEST_MISMATCH"
            if digest_field == "manifest_digest"
            else "CONTRACT_DIGEST_MISMATCH"
        )
    return value, None


def _ordered(reasons: set[str]) -> list[str]:
    if reasons & HUMAN_GATE_BASE:
        reasons.add("HUMAN_GATE_REQUIRED")
    return [reason for reason in REASON_ORDER if reason in reasons]


def _payload(reasons: set[str], exit_code: int | None = None) -> JsonObject:
    ordered = _ordered(set(reasons))
    code = (0 if not ordered else 2) if exit_code is None else exit_code
    passed = code == 0 and not ordered
    return {
        "claim": "LEDGER_STRUCTURAL_INTEGRITY_PASS" if passed else None,
        "exit_code": code,
        "mutations_performed": False,
        "reason_codes": ordered,
        "semantic_objective_satisfaction": "NOT_CLAIMED",
        "status": "PASS" if passed else "BLOCKED",
    }


def _evaluate(args: argparse.Namespace, cwd: Path) -> JsonObject:
    manifest_raw = str(args.contract_manifest[0])
    ledger_raw = str(args.ledger[0])
    contract_raw = [str(item) for item in args.contract]
    artifact_raw = [str(item) for item in args.artifact]
    all_raw = [manifest_raw, *contract_raw, ledger_raw, *artifact_raw]
    resolved, invalid, duplicate = _resolve_inputs(cwd, all_raw)
    if invalid or len(resolved) != len(all_raw):
        if duplicate and not invalid:
            return _payload({"INPUT_SCHEMA_INVALID"})
        return _payload({"PATH_INVALID"})
    manifest_path = _normalize_repo_path(manifest_raw)
    ledger_path = _normalize_repo_path(ledger_raw)
    assert manifest_path is not None and ledger_path is not None

    primary_missing: set[str] = set()
    if not resolved[manifest_path].is_file():
        primary_missing.add("CONTRACT_MANIFEST_MISSING")
    if not resolved[ledger_path].is_file():
        primary_missing.add("LEDGER_MISSING")
    if primary_missing:
        return _payload(primary_missing)

    manifest_data = _read_bytes(resolved[manifest_path])
    try:
        manifest = _loads_json(manifest_data)
    except _EncodingError:
        return _payload({"INPUT_ENCODING_INVALID"})
    except _JsonError:
        return _payload({"INPUT_JSON_PARSE_ERROR"})
    try:
        duplicate_tuple = _validate_manifest(manifest)
    except _SchemaError:
        return _payload({"INPUT_SCHEMA_INVALID"})
    except _NonCanonicalError:
        return _payload({"INPUT_NON_CANONICAL"})
    if manifest_data != _canonical_json(manifest) + b"\n":
        return _payload({"INPUT_NON_CANONICAL"})
    if manifest["manifest_digest"] != _object_digest(manifest, "manifest_digest"):
        return _payload({"CONTRACT_MANIFEST_DIGEST_MISMATCH"})

    manifest_rows = list(manifest["contracts"])
    expected_paths = {str(row["path"]) for row in manifest_rows}
    supplied_contracts = {_normalize_repo_path(item) for item in contract_raw}
    if supplied_contracts != expected_paths:
        return _payload({"CONTRACT_INPUT_SET_MISMATCH"})
    missing_contracts = {
        path for path in expected_paths if not resolved[path].is_file()
    }
    if missing_contracts:
        return _payload({"CONTRACT_INPUT_MISSING"})

    contracts_by_path: dict[str, JsonObject] = {}
    contract_bytes: dict[str, bytes] = {}
    reasons: set[str] = set()
    for path in sorted(expected_paths):
        data = _read_bytes(resolved[path])
        value, failure = _reason_from_owned_json(data, _validate_contract, "contract_digest")
        if failure is not None:
            return _payload({failure})
        assert value is not None
        contracts_by_path[path] = value
        contract_bytes[path] = data
        row = next(row for row in manifest_rows if row["path"] == path)
        if row["sha256"] != _sha256(data):
            reasons.add("CONTRACT_DIGEST_MISMATCH")
    if reasons:
        return _payload(reasons)

    if duplicate_tuple:
        grouped: dict[tuple[str, int], list[JsonObject]] = {}
        for row in manifest_rows:
            grouped.setdefault((row["contract_id"], row["revision"]), []).append(row)
        for rows in grouped.values():
            if len(rows) > 1:
                reasons.add("CONTRACT_REVISION_REUSED")
                docs = [contracts_by_path[str(row["path"])] for row in rows]
                if len({str(doc["objective"]) for doc in docs}) > 1:
                    reasons.add("OBJECTIVE_DELTA")
                if len({json.dumps(doc["allowed_write_scope"], ensure_ascii=False) for doc in docs}) > 1:
                    reasons.add("SCOPE_DELTA")
        return _payload(reasons)

    rows_by_tuple = {
        (str(row["contract_id"]), int(row["revision"])): row for row in manifest_rows
    }
    for row in manifest_rows:
        contract = contracts_by_path[str(row["path"])]
        if contract["contract_id"] != row["contract_id"] or contract["revision"] != row["revision"]:
            reasons.add("CONTRACT_DIGEST_MISMATCH")
            continue
        revision = int(row["revision"])
        if revision == 1:
            if contract["parent_contract_digest"] is not None:
                reasons.add("CONTRACT_PARENT_MISMATCH")
        else:
            parent = rows_by_tuple.get((str(row["contract_id"]), revision - 1))
            if parent is None:
                reasons.add("CONTRACT_PARENT_MISMATCH")
                continue
            parent_contract = contracts_by_path[str(parent["path"])]
            if contract["parent_contract_digest"] != parent_contract["contract_digest"]:
                reasons.add("CONTRACT_PARENT_MISMATCH")
    if reasons:
        return _payload(reasons)

    ledger_data = _read_bytes(resolved[ledger_path])
    try:
        entries = _loads_jsonl(ledger_data)
    except _EncodingError:
        return _payload({"INPUT_ENCODING_INVALID"})
    except _JsonlError:
        return _payload({"INPUT_JSONL_PARSE_ERROR"})
    except _NonCanonicalError:
        return _payload({"INPUT_NON_CANONICAL"})
    try:
        _validate_entries(entries)
    except _SchemaError:
        return _payload({"INPUT_SCHEMA_INVALID"})
    except _NonCanonicalError:
        return _payload({"INPUT_NON_CANONICAL"})
    if ledger_data != b"".join(_canonical_json(entry) + b"\n" for entry in entries):
        return _payload({"INPUT_NON_CANONICAL"})

    artifact_paths = {_normalize_repo_path(item) for item in artifact_raw}
    if any(not resolved[path].is_file() for path in artifact_paths if path is not None):
        return _payload({"ARTIFACT_INPUT_MISSING"})
    artifact_bytes = {
        path: _read_bytes(resolved[path]) for path in artifact_paths if path is not None
    }

    previous_digest: str | None = None
    for index, entry in enumerate(entries, start=1):
        if entry["sequence"] != index:
            reasons.add("LEDGER_SEQUENCE_GAP")
        expected_previous = None if index == 1 else previous_digest
        if entry["previous_entry_digest"] != expected_previous:
            reasons.add("LEDGER_CHAIN_MISMATCH")
        actual_entry_digest = _object_digest(entry, "entry_digest")
        if entry["entry_digest"] != actual_entry_digest:
            reasons.add("LEDGER_CHAIN_MISMATCH")
        previous_digest = str(entry["entry_digest"])

        ref = entry["contract_ref"]
        tuple_key = (str(ref["contract_id"]), int(ref["revision"]))
        manifest_row = rows_by_tuple.get(tuple_key)
        if manifest_row is None or ref != manifest_row:
            reasons.add("CONTRACT_DIGEST_MISMATCH")
            continue
        contract = contracts_by_path[str(manifest_row["path"])]

        phase_ref = entry["phase_ledger_ref"]
        phase_path = str(phase_ref["path"])
        if phase_path not in artifact_bytes:
            reasons.add("PHASE_LEDGER_MISSING")
        elif phase_ref["sha256"] != _sha256(artifact_bytes[phase_path]):
            reasons.add("PHASE_LEDGER_DIGEST_MISMATCH")

        for changed in entry["changed_paths"]:
            if _normalize_repo_path(changed) is None:
                reasons.add("PATH_INVALID")
            elif not any(_path_matches(changed, pattern) for pattern in contract["allowed_write_scope"]):
                reasons.add("WRITE_SCOPE_VIOLATION")

        claims = {str(row["claim_id"]): row for row in contract["validation_claims"]}
        declared = set(claims)
        passed = set(entry["claimed_pass"])
        not_claimed = set(entry["not_claimed"])
        if not passed.issubset(declared) or not not_claimed.issubset(declared):
            reasons.add("UNDECLARED_CLAIM")
        if passed & not_claimed:
            reasons.add("CLAIM_STATE_CONFLICT")
        if entry["action_type"] == "CLOSEOUT_RECORDED" and declared - (passed | not_claimed):
            reasons.add("UNCLASSIFIED_CLAIM")

        refs = {str(row["artifact_id"]): row for row in entry["artifact_refs"]}
        links = {str(row["claim_id"]): row for row in entry["claim_evidence_links"]}
        required_artifacts = {
            str(row["artifact_id"]): row for row in contract["required_artifacts"]
        }
        required_sources = {
            str(row["verdict_source_id"]): row
            for row in contract["required_verdict_sources"]
        }
        for link_claim_id, link in links.items():
            if link_claim_id not in declared:
                reasons.add("UNDECLARED_CLAIM")
            linked_artifact_ids = set(link["artifact_ids"])
            if not linked_artifact_ids.issubset(required_artifacts) or not linked_artifact_ids.issubset(
                refs
            ):
                reasons.add("MISSING_REQUIRED_ARTIFACT")
            if not set(link["verdict_source_ids"]).issubset(required_sources):
                reasons.add("VERDICT_SOURCE_LINK_MISSING")
        for claim_id in sorted(passed & declared):
            claim = claims[claim_id]
            link = links.get(claim_id)
            if link is None:
                reasons.add("CLAIM_EVIDENCE_LINK_MISSING")
                continue
            linked_artifacts = set(link["artifact_ids"])
            needed_artifacts = set(claim["required_artifact_ids"])
            if not needed_artifacts.issubset(linked_artifacts):
                reasons.add("MISSING_REQUIRED_ARTIFACT")
            else:
                for artifact_id in sorted(needed_artifacts):
                    actual = refs.get(artifact_id)
                    required = required_artifacts[artifact_id]
                    if actual is None:
                        reasons.add("MISSING_REQUIRED_ARTIFACT")
                        continue
                    if any(actual[key] != required[key] for key in ("artifact_id", "artifact_type", "path")):
                        reasons.add("ARTIFACT_IDENTITY_MISMATCH")
                        continue
                    artifact_path = str(actual["path"])
                    if artifact_path not in artifact_bytes:
                        reasons.add("MISSING_REQUIRED_ARTIFACT")
                    elif actual["sha256"] != _sha256(artifact_bytes[artifact_path]):
                        reasons.add("ARTIFACT_DIGEST_MISMATCH")

            linked_sources = set(link["verdict_source_ids"])
            needed_sources = set(claim["required_verdict_source_ids"])
            if not needed_sources.issubset(linked_sources):
                reasons.add("VERDICT_SOURCE_LINK_MISSING")
                continue
            for source_id in sorted(needed_sources):
                source = required_sources[source_id]
                if source["adapter"] != "ztr_envelope_v2":
                    reasons.add("VERDICT_SOURCE_ADAPTER_UNSUPPORTED")
                    continue
                actual = refs.get(str(source["artifact_id"]))
                if actual is None or str(actual["path"]) not in artifact_bytes:
                    reasons.add("VERDICT_SOURCE_ARTIFACT_MISSING")
                    continue
                required = required_artifacts[str(source["artifact_id"])]
                if any(actual[key] != required[key] for key in ("artifact_id", "artifact_type", "path")):
                    reasons.add("ARTIFACT_IDENTITY_MISMATCH")
                    continue
                source_data = artifact_bytes[str(actual["path"])]
                if actual["sha256"] != _sha256(source_data):
                    reasons.add("ARTIFACT_DIGEST_MISMATCH")
                    continue
                try:
                    envelope = _loads_json(source_data)
                    _validate_envelope(envelope)
                except (_EncodingError, _JsonError, _SchemaError, _NonCanonicalError):
                    reasons.add("VERDICT_SOURCE_PARSE_ERROR")
                    continue
                if envelope["status"] != source["required_status"]:
                    reasons.add("VERDICT_SOURCE_STATUS_MISMATCH")
                if envelope["exit_code"] != source["required_exit_code"]:
                    reasons.add("VERDICT_SOURCE_EXIT_MISMATCH")
    return _payload(reasons)


def execute(argv: Sequence[str], cwd: Path | None = None) -> JsonObject:
    args = _parse_args(argv)
    return _evaluate(args, Path.cwd() if cwd is None else cwd)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        payload = execute(sys.argv[1:] if argv is None else argv)
    except _CliContractError:
        payload = _payload(set(), exit_code=70)
    except Exception:
        payload = _payload(set(), exit_code=70)
    sys.stdout.buffer.write(_canonical_json(payload) + b"\n")
    return int(payload["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
