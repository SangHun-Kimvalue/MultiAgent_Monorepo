"""Append-only phase evidence manifest writer.

The two public functions intentionally return process-style exit codes and emit
one JSON result line.  The command-line interface is only a thin adapter around
that API.
"""

from __future__ import annotations

import argparse as _argparse
import hashlib as _hashlib
import json as _json
import os as _os
import posixpath as _posixpath
import re as _re
import sys as _sys
import uuid as _uuid
from datetime import datetime as _datetime
from datetime import timezone as _timezone
from pathlib import Path
from typing import Any as _Any

__all__ = ["append_entry", "init_manifest"]

_SCHEMA_VERSION = "t10-o1.phase-manifest.v1"
_BASE_SHA_PATTERN = _re.compile(r"^[0-9a-f]{40}$")
_CONTENT_SHA_PATTERN = _re.compile(r"^[0-9a-f]{64}$")
_DEFERRED_TO_PATTERN = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_ROLES = {"planner", "implementer", "mechanical", "test", "reviewer", "orchestrator"}
_VERDICT_EXIT_CODES = {"PASS": 0, "CHANGES_REQUESTED": 1, "BLOCKED": 2}
_VERDICT_STAGES = ("input_gate", "implementation_review", "final_verification")
_VERDICT_SUBJECTS = ("design", "prompt", "diff", "mechanical", "test", "integration")
_DECISIONS = {
    "ACCEPT",
    "REJECT_FALSE_POSITIVE",
    "DEFER_OUT_OF_SCOPE",
    "REJECT_OVERENGINEERING",
}
_FAILURE_REASONS = {"timeout", "no_output", "launch_failed", "source_unavailable", "quota"}

_KIND_FIELDS = {
    "role_session": ({"role", "session_id"}, set()),
    "artifact": ({"artifact_ref", "content_sha256"}, set()),
    "changed_paths": ({"changed_paths"}, set()),
    "command": ({"argv"}, {"artifact_ref"}),
    "verdict": (
        {
            "verdict",
            "exit_code",
            "artifact_ref",
            "content_sha256",
            "attempt_id",
            "stage",
            "subject",
        },
        {"failure_reason"},
    ),
    "finding_disposition": (
        {"finding_id", "decision", "rationale_ref"},
        {"deferred_to"},
    ),
    "validation_fact": ({"name", "result"}, {"artifact_ref"}),
}


class _ManifestError(Exception):
    def __init__(self, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _emit(exit_code: int, message: str, **details: _Any) -> int:
    status = {0: "PASS", 1: "FAIL", 2: "BLOCKED"}[exit_code]
    payload = {"status": status, "exit_code": exit_code, "message": message, **details}
    print(_json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return exit_code


def _nonempty_string(value: _Any) -> bool:
    return isinstance(value, str) and bool(value)


def _plain_int(value: _Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_utc_timestamp(value: _Any) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = _datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == _timezone.utc.utcoffset(parsed)


def _utc_now() -> str:
    return _datetime.now(_timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _write_atomic(path: Path, manifest: dict[str, _Any]) -> None:
    temporary = Path(f"{path}.{_os.getpid()}.{_uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8", newline="") as handle:
            _json.dump(manifest, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
        _os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _validate_existing_manifest(value: _Any) -> dict[str, _Any]:
    required = {"schema_version", "phase_id", "base_sha", "created_at_utc", "entries"}
    if not isinstance(value, dict) or not required.issubset(value):
        raise _ManifestError(2, "manifest is missing required top-level fields")
    if value["schema_version"] != _SCHEMA_VERSION:
        raise _ManifestError(1, "unsupported schema_version")
    if not _nonempty_string(value["phase_id"]):
        raise _ManifestError(1, "phase_id must be a non-empty string")
    if not isinstance(value["base_sha"], str) or _BASE_SHA_PATTERN.fullmatch(value["base_sha"]) is None:
        raise _ManifestError(1, "base_sha must be 40 lowercase hexadecimal characters")
    if not _is_utc_timestamp(value["created_at_utc"]):
        raise _ManifestError(1, "created_at_utc must be an RFC3339 UTC timestamp")
    entries = value["entries"]
    if not isinstance(entries, list):
        raise _ManifestError(1, "entries must be a list")
    for expected_seq, existing_entry in enumerate(entries, start=1):
        if (
            not isinstance(existing_entry, dict)
            or not _plain_int(existing_entry.get("seq"))
            or existing_entry["seq"] != expected_seq
        ):
            raise _ManifestError(1, "existing entry seq values must be consecutive from 1")
    return value


def _normalize_artifact_ref(manifest_path: Path, value: str) -> str:
    slash_value = value.replace("\\", "/")
    if Path(slash_value).is_absolute():
        raise _ManifestError(1, "artifact_ref must be relative to the phase directory")
    normalized = _posixpath.normpath(slash_value)
    phase_directory = manifest_path.parent.resolve()
    resolved = (phase_directory / Path(normalized)).resolve()
    if not resolved.is_relative_to(phase_directory):
        raise _ManifestError(1, "artifact_ref resolves outside the phase directory")
    return normalized


def _validate_string_list(name: str, value: _Any) -> None:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise _ManifestError(1, f"{name} must be a non-empty list of strings")


def _validate_entry(manifest_path: Path, source: _Any) -> dict[str, _Any]:
    if not isinstance(source, dict):
        raise _ManifestError(1, "entry must be a JSON object")
    entry = dict(source)
    if "seq" in entry or "recorded_at_utc" in entry:
        raise _ManifestError(1, "seq and recorded_at_utc are writer-owned fields")
    kind = entry.get("kind")
    if not isinstance(kind, str) or kind not in _KIND_FIELDS:
        raise _ManifestError(1, "kind is missing or unsupported")

    required, conditional = _KIND_FIELDS[kind]
    supplied = set(entry) - {"kind"}
    if not required.issubset(supplied):
        raise _ManifestError(1, f"{kind} entry is missing required fields")
    if not supplied.issubset(required | conditional):
        raise _ManifestError(1, f"{kind} entry contains fields outside its closed schema")

    if kind == "verdict":
        for name, allowed_values in (
            ("stage", _VERDICT_STAGES),
            ("subject", _VERDICT_SUBJECTS),
        ):
            value = entry[name]
            if not isinstance(value, str) or value not in allowed_values:
                raise _ManifestError(
                    1,
                    f"{name} has unsupported value {value!r}; "
                    f"allowed values: {list(allowed_values)!r}",
                )

    for name in {
        "session_id",
        "attempt_id",
        "stage",
        "subject",
        "finding_id",
        "rationale_ref",
        "name",
        "result",
    } & supplied:
        if not _nonempty_string(entry[name]):
            raise _ManifestError(1, f"{name} must be a non-empty string")

    if "argv" in supplied:
        _validate_string_list("argv", entry["argv"])
    if "changed_paths" in supplied:
        _validate_string_list("changed_paths", entry["changed_paths"])
    if "role" in supplied and (
        not isinstance(entry["role"], str) or entry["role"] not in _ROLES
    ):
        raise _ManifestError(1, "role is unsupported")

    if kind == "finding_disposition":
        decision = entry["decision"]
        if not isinstance(decision, str) or decision not in _DECISIONS:
            raise _ManifestError(1, "decision is unsupported")
        has_deferred_to = "deferred_to" in entry
        if decision == "DEFER_OUT_OF_SCOPE":
            if not has_deferred_to or not isinstance(entry["deferred_to"], str):
                raise _ManifestError(1, "DEFER_OUT_OF_SCOPE requires deferred_to")
            if _DEFERRED_TO_PATTERN.fullmatch(entry["deferred_to"]) is None:
                raise _ManifestError(1, "deferred_to has an invalid format")
        elif has_deferred_to:
            raise _ManifestError(1, "deferred_to is only allowed for DEFER_OUT_OF_SCOPE")

    if kind == "verdict":
        verdict = entry["verdict"]
        exit_code = entry["exit_code"]
        if not isinstance(verdict, str) or verdict not in _VERDICT_EXIT_CODES:
            raise _ManifestError(1, "verdict is unsupported")
        if not _plain_int(exit_code) or _VERDICT_EXIT_CODES[verdict] != exit_code:
            raise _ManifestError(1, "verdict and exit_code do not match")
        artifact_is_null = entry["artifact_ref"] is None
        hash_is_null = entry["content_sha256"] is None
        if artifact_is_null != hash_is_null:
            raise _ManifestError(1, "artifact_ref and content_sha256 must become null together")
        if artifact_is_null:
            if verdict != "BLOCKED":
                raise _ManifestError(1, "only BLOCKED may use null artifact evidence")
            if (
                not isinstance(entry.get("failure_reason"), str)
                or entry["failure_reason"] not in _FAILURE_REASONS
            ):
                raise _ManifestError(1, "null BLOCKED evidence requires failure_reason")
        elif "failure_reason" in entry and verdict != "BLOCKED":
            raise _ManifestError(1, "failure_reason is only allowed for BLOCKED")
        elif "failure_reason" in entry and (
            not isinstance(entry["failure_reason"], str)
            or entry["failure_reason"] not in _FAILURE_REASONS
        ):
            raise _ManifestError(1, "failure_reason is unsupported")

    if "artifact_ref" in supplied and entry["artifact_ref"] is not None:
        if not _nonempty_string(entry["artifact_ref"]):
            raise _ManifestError(1, "artifact_ref must be a non-empty string")
        entry["artifact_ref"] = _normalize_artifact_ref(manifest_path, entry["artifact_ref"])

    if "content_sha256" in supplied and entry["content_sha256"] is not None:
        content_sha = entry["content_sha256"]
        if not isinstance(content_sha, str) or _CONTENT_SHA_PATTERN.fullmatch(content_sha) is None:
            raise _ManifestError(1, "content_sha256 must be 64 lowercase hexadecimal characters")

    if kind in {"artifact", "verdict"} and entry["artifact_ref"] is not None:
        artifact_path = manifest_path.parent / Path(entry["artifact_ref"])
        try:
            content = artifact_path.read_bytes()
        except OSError as exc:
            raise _ManifestError(2, f"required artifact is unavailable: {exc}") from exc
        if _hashlib.sha256(content).hexdigest() != entry["content_sha256"]:
            raise _ManifestError(1, "content_sha256 does not match the artifact")
    return entry


def init_manifest(path: Path, phase_id: str, base_sha: str) -> int:
    """Create a new phase manifest and return its process-style exit code."""
    try:
        manifest_path = Path(path)
        if manifest_path.exists():
            raise _ManifestError(1, "manifest already exists")
        if not _nonempty_string(phase_id):
            raise _ManifestError(1, "phase_id must be a non-empty string")
        if not isinstance(base_sha, str) or _BASE_SHA_PATTERN.fullmatch(base_sha) is None:
            raise _ManifestError(1, "base_sha must be 40 lowercase hexadecimal characters")
        manifest = {
            "schema_version": _SCHEMA_VERSION,
            "phase_id": phase_id,
            "base_sha": base_sha,
            "created_at_utc": _utc_now(),
            "entries": [],
        }
        _write_atomic(manifest_path, manifest)
    except _ManifestError as exc:
        return _emit(exc.exit_code, str(exc))
    except Exception as exc:
        return _emit(2, f"manifest initialization is blocked: {exc}")
    return _emit(0, "manifest initialized", manifest=str(manifest_path))


def append_entry(path: Path, entry: dict, dry_run: bool = False) -> int:
    """Validate and append one entry, returning its process-style exit code."""
    try:
        manifest_path = Path(path)
        try:
            with open(manifest_path, encoding="utf-8") as handle:
                manifest = _json.load(handle)
        except (OSError, UnicodeError, _json.JSONDecodeError) as exc:
            raise _ManifestError(2, f"manifest is unavailable or invalid JSON: {exc}") from exc
        manifest = _validate_existing_manifest(manifest)
        validated = _validate_entry(manifest_path, entry)
        validated["seq"] = len(manifest["entries"]) + 1
        validated["recorded_at_utc"] = _utc_now()
        if not dry_run:
            manifest["entries"].append(validated)
            _write_atomic(manifest_path, manifest)
    except _ManifestError as exc:
        return _emit(exc.exit_code, str(exc))
    except Exception as exc:
        return _emit(2, f"manifest append is blocked: {exc}")
    action = "entry validated" if dry_run else "entry appended"
    return _emit(0, action, manifest=str(manifest_path))


def _load_entry(source: str) -> _Any:
    if source == "-":
        return _json.load(_sys.stdin)
    with open(source, encoding="utf-8") as handle:
        return _json.load(handle)


def _build_parser() -> _argparse.ArgumentParser:
    parser = _argparse.ArgumentParser(prog="python -m methodology.phase.manifest")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--manifest", required=True, type=Path)
    init_parser.add_argument("--phase-id", required=True)
    init_parser.add_argument("--base-sha", required=True)

    append_parser = subparsers.add_parser("append")
    append_parser.add_argument("--manifest", required=True, type=Path)
    append_parser.add_argument("--entry", required=True)
    append_parser.add_argument("--dry-run", action="store_true")
    return parser


def _main(arguments: list[str] | None = None) -> int:
    parsed = _build_parser().parse_args(arguments)
    if parsed.command == "init":
        return init_manifest(parsed.manifest, parsed.phase_id, parsed.base_sha)
    try:
        entry = _load_entry(parsed.entry)
    except (OSError, UnicodeError, _json.JSONDecodeError) as exc:
        return _emit(2, f"entry source is unavailable or invalid JSON: {exc}")
    except Exception as exc:
        return _emit(2, f"entry loading is blocked: {exc}")
    return append_entry(parsed.manifest, entry, parsed.dry_run)


if __name__ == "__main__":
    raise SystemExit(_main())
