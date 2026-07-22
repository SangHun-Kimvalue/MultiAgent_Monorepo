"""Human-triggered accepted-only remediation report adapter.

This module validates an Orchestrator-authored disposition artifact and emits the
single synthetic report consumed by the existing ``ztr fix-prompt`` command.  It
does not interpret reviewer prose, rank severity, or choose a disposition.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Never


PASS = "PASS"
BLOCKED = "BLOCKED"
INTERNAL_ERROR = "INTERNAL_ERROR"
STEP_NAME = "orchestrator-accepted-review"
CHANGES_REQUESTED = "CHANGES_REQUESTED"
MAX_PREVIEW_CHARS = 4000
REPO_ROOT = Path(__file__).resolve().parents[2]

TOP_FIELDS = frozenset({"schema_version", "phase_id", "human_trigger", "findings"})
HUMAN_FIELDS = frozenset({"approved", "actor", "approved_at", "scope", "evidence_ref"})
FINDING_FIELDS = frozenset(
    {
        "id",
        "reviewer",
        "severity",
        "finding",
        "evidence_or_repro",
        "impact",
        "recommendation",
        "disposition",
        "disposition_evidence",
        "rationale",
        "owner",
        "corrective_round",
        "fix_instruction",
    }
)
DISPOSITIONS = frozenset(
    {
        "ACCEPT",
        "REJECT_FALSE_POSITIVE",
        "DEFER_OUT_OF_SCOPE",
        "REJECT_OVERENGINEERING",
    }
)
REPORT_FIELDS = frozenset({"steps", "summary"})
REPORT_STEP_FIELDS = frozenset({"name", "status", "gating", "skipped", "stdout_preview"})
REPORT_SUMMARY_FIELDS = frozenset({"verdict", "failed_step"})
PREVIEW_FIELDS = frozenset(
    {
        "id",
        "severity",
        "finding",
        "evidence_or_repro",
        "impact",
        "recommendation",
        "fix_instruction",
    }
)

ReplaceFile = Callable[[str, str], None]


class AdapterBlocked(RuntimeError):
    """A contract violation that must fail closed with BLOCKED/2."""


class AdapterIOError(RuntimeError):
    """An internal or I/O failure that must return 70."""


class JsonArgumentParser(argparse.ArgumentParser):
    """Keep argparse errors inside the one-line JSON stdout contract."""

    def error(self, message: str) -> Never:
        raise AdapterBlocked(f"invalid arguments: {message}")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AdapterBlocked(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _exact_fields(value: Any, expected: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AdapterBlocked(f"{label} must be an object")
    actual = set(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing or unknown:
        raise AdapterBlocked(f"{label} fields mismatch: missing={missing}, unknown={unknown}")
    return value


def _non_empty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AdapterBlocked(f"{label} must be a non-empty string")
    return value


def _load_strict_json(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8", errors="strict")
    except UnicodeError:
        raise AdapterBlocked(f"{label} is not strict UTF-8 JSON") from None
    except OSError as exc:
        raise AdapterIOError(f"cannot read {label}: {type(exc).__name__}") from None
    try:
        value = json.loads(raw, object_pairs_hook=_strict_object)
    except AdapterBlocked:
        raise
    except json.JSONDecodeError:
        raise AdapterBlocked(f"{label} is not strict JSON") from None
    if not isinstance(value, dict):
        raise AdapterBlocked(f"{label} root must be an object")
    return value


def _resolve_repo_file(path: Path, repo_root: Path, label: str) -> Path:
    if not path.is_absolute():
        raise AdapterBlocked(f"{label} must be an absolute path")
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        raise AdapterBlocked(f"{label} does not exist") from None
    except OSError as exc:
        raise AdapterIOError(f"cannot resolve {label}: {type(exc).__name__}") from None
    if not resolved.is_file():
        raise AdapterBlocked(f"{label} must be a regular file")
    if not resolved.is_relative_to(repo_root):
        raise AdapterBlocked(f"{label} must be inside repo root")
    return resolved


def _resolve_output(path: Path, repo_root: Path) -> Path:
    if not path.is_absolute():
        raise AdapterBlocked("--out-report must be an absolute path")
    if path.is_symlink():
        raise AdapterBlocked("--out-report cannot be a symbolic link")
    try:
        parent = path.parent.resolve(strict=True)
    except FileNotFoundError:
        raise AdapterBlocked("--out-report parent must already exist") from None
    except OSError as exc:
        raise AdapterIOError(f"cannot resolve --out-report parent: {type(exc).__name__}") from None
    if not parent.is_dir():
        raise AdapterBlocked("--out-report parent must be a directory")
    if not parent.is_relative_to(repo_root):
        raise AdapterBlocked("--out-report must be inside repo root")
    return parent / path.name


def _parse_disposition(value: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    top = _exact_fields(value, TOP_FIELDS, "disposition")
    if type(top["schema_version"]) is not int or top["schema_version"] != 1:
        raise AdapterBlocked("schema_version must be integer 1")
    _non_empty_string(top["phase_id"], "phase_id")

    human = _exact_fields(top["human_trigger"], HUMAN_FIELDS, "human_trigger")
    if type(human["approved"]) is not bool:
        raise AdapterBlocked("human_trigger.approved must be a boolean")
    for field in ("actor", "approved_at", "scope", "evidence_ref"):
        _non_empty_string(human[field], f"human_trigger.{field}")

    raw_findings = top["findings"]
    if not isinstance(raw_findings, list):
        raise AdapterBlocked("findings must be an array")
    findings: list[dict[str, Any]] = []
    ids: set[str] = set()
    accepted_round: str | None = None
    for index, raw_finding in enumerate(raw_findings):
        label = f"findings[{index}]"
        finding = _exact_fields(raw_finding, FINDING_FIELDS, label)
        for field in (
            "id",
            "reviewer",
            "severity",
            "finding",
            "evidence_or_repro",
            "impact",
            "recommendation",
            "disposition_evidence",
            "rationale",
            "owner",
        ):
            _non_empty_string(finding[field], f"{label}.{field}")
        finding_id = finding["id"]
        assert isinstance(finding_id, str)
        if finding_id in ids:
            raise AdapterBlocked(f"duplicate finding id: {finding_id}")
        ids.add(finding_id)

        disposition = finding["disposition"]
        if not isinstance(disposition, str) or disposition not in DISPOSITIONS:
            raise AdapterBlocked(f"{label}.disposition has an invalid enum value")
        if disposition == "ACCEPT":
            corrective_round = _non_empty_string(
                finding["corrective_round"], f"{label}.corrective_round"
            )
            _non_empty_string(finding["fix_instruction"], f"{label}.fix_instruction")
            if accepted_round is None:
                accepted_round = corrective_round
            elif corrective_round != accepted_round:
                raise AdapterBlocked("all ACCEPT findings must use one corrective_round")
        elif finding["corrective_round"] is not None or finding["fix_instruction"] is not None:
            raise AdapterBlocked(
                f"{label} non-ACCEPT corrective_round and fix_instruction must both be null"
            )
        findings.append(finding)
    return human, findings


def _accepted_preview(accepted: list[dict[str, Any]]) -> str:
    relay_fields = (
        "id",
        "severity",
        "finding",
        "evidence_or_repro",
        "impact",
        "recommendation",
        "fix_instruction",
    )
    lines = [
        json.dumps(
            {field: finding[field] for field in relay_fields},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        for finding in accepted
    ]
    preview = "\n".join(lines)
    if len(preview) > MAX_PREVIEW_CHARS:
        raise AdapterBlocked("accepted-only stdout_preview exceeds 4000 characters")
    return preview


def _build_report(preview: str) -> dict[str, Any]:
    return {
        "steps": [
            {
                "name": STEP_NAME,
                "status": CHANGES_REQUESTED,
                "gating": True,
                "skipped": False,
                "stdout_preview": preview,
            }
        ],
        "summary": {
            "verdict": CHANGES_REQUESTED,
            "failed_step": STEP_NAME,
        },
    }


def _validate_owned_report(value: dict[str, Any]) -> None:
    report = _exact_fields(value, REPORT_FIELDS, "existing report")
    steps = report["steps"]
    if not isinstance(steps, list) or len(steps) != 1:
        raise AdapterBlocked("existing output is not an adapter-owned report")
    step = _exact_fields(steps[0], REPORT_STEP_FIELDS, "existing report step")
    summary = _exact_fields(report["summary"], REPORT_SUMMARY_FIELDS, "existing report summary")
    if (
        step["name"] != STEP_NAME
        or step["status"] != CHANGES_REQUESTED
        or type(step["gating"]) is not bool
        or step["gating"] is not True
        or type(step["skipped"]) is not bool
        or step["skipped"] is not False
        or not isinstance(step["stdout_preview"], str)
        or not step["stdout_preview"]
        or len(step["stdout_preview"]) > MAX_PREVIEW_CHARS
        or summary["verdict"] != CHANGES_REQUESTED
        or summary["failed_step"] != STEP_NAME
    ):
        raise AdapterBlocked("existing output is not an adapter-owned report")
    preview = step["stdout_preview"]
    assert isinstance(preview, str)
    for index, line in enumerate(preview.splitlines()):
        try:
            parsed = json.loads(line, object_pairs_hook=_strict_object)
            item = _exact_fields(parsed, PREVIEW_FIELDS, f"existing preview[{index}]")
            for field in PREVIEW_FIELDS:
                _non_empty_string(item[field], f"existing preview[{index}].{field}")
        except (AdapterBlocked, json.JSONDecodeError) as exc:
            raise AdapterBlocked("existing output is not an adapter-owned report") from exc


def _check_overwrite_policy(output: Path, overwrite: bool) -> None:
    if not output.exists():
        return
    if not output.is_file():
        raise AdapterBlocked("existing output must be a regular file")
    if not overwrite:
        raise AdapterBlocked("existing output requires --overwrite")
    try:
        existing = _load_strict_json(output, "existing output")
    except AdapterIOError:
        raise
    except AdapterBlocked as exc:
        raise AdapterBlocked("existing output is malformed or unrelated; preserving it") from exc
    try:
        _validate_owned_report(existing)
    except AdapterBlocked as exc:
        raise AdapterBlocked("existing output is malformed or unrelated; preserving it") from exc


def _atomic_write(output: Path, report: dict[str, Any], replace_file: ReplaceFile) -> None:
    serialized = json.dumps(
        report, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ) + "\n"
    temp_path: Path | None = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{output.name}.", suffix=".tmp", dir=str(output.parent)
        )
        temp_path = Path(temp_name)
        with os.fdopen(fd, "w", encoding="utf-8", errors="strict", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        replace_file(str(temp_path), str(output))
        temp_path = None
    except (OSError, UnicodeError) as exc:
        raise AdapterIOError(f"atomic output write failed: {type(exc).__name__}") from None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _base_payload() -> dict[str, Any]:
    return {
        "status": BLOCKED,
        "accepted_ids": [],
        "accepted_count": 0,
        "output_path": None,
        "mutations_performed": [],
        "diagnostic": None,
    }


def build_parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(description="Emit an accepted-only ztr remediation report")
    parser.add_argument("--disposition", type=Path, required=True)
    parser.add_argument("--out-report", type=Path, required=True)
    parser.add_argument("--human-triggered", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    return parser


def execute(
    argv: Sequence[str] | None = None,
    *,
    repo_root: Path = REPO_ROOT,
    replace_file: ReplaceFile = os.replace,
) -> tuple[dict[str, Any], int]:
    """Validate one artifact and atomically emit one accepted-only report."""

    payload = _base_payload()
    try:
        args = build_parser().parse_args(argv)
        if not args.human_triggered:
            raise AdapterBlocked("explicit --human-triggered flag is required")
        try:
            canonical_root = repo_root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise AdapterIOError(f"cannot resolve repo root: {type(exc).__name__}") from None
        if not canonical_root.is_dir():
            raise AdapterIOError("repo root must be a directory")
        disposition_path = _resolve_repo_file(args.disposition, canonical_root, "--disposition")
        output_path = _resolve_output(args.out_report, canonical_root)
        payload["output_path"] = str(output_path)
        if disposition_path == output_path:
            raise AdapterBlocked("input and output paths must be different")

        artifact = _load_strict_json(disposition_path, "disposition")
        human, findings = _parse_disposition(artifact)
        if human["approved"] is not True:
            raise AdapterBlocked("human_trigger.approved must be true")
        accepted = [finding for finding in findings if finding["disposition"] == "ACCEPT"]
        accepted_ids = [str(finding["id"]) for finding in accepted]
        payload["accepted_ids"] = accepted_ids
        payload["accepted_count"] = len(accepted_ids)
        if not accepted:
            raise AdapterBlocked(
                "corrective round emission unnecessary: no ACCEPT findings"
            )

        preview = _accepted_preview(accepted)
        _check_overwrite_policy(output_path, bool(args.overwrite))
        _atomic_write(output_path, _build_report(preview), replace_file)
        payload["status"] = PASS
        payload["mutations_performed"] = ["only_out_report"]
        return payload, 0
    except AdapterBlocked as exc:
        payload["diagnostic"] = str(exc)
        return payload, 2
    except AdapterIOError as exc:
        payload["status"] = INTERNAL_ERROR
        payload["diagnostic"] = str(exc)
        return payload, 70
    except Exception as exc:
        payload["status"] = INTERNAL_ERROR
        payload["diagnostic"] = f"internal error: {type(exc).__name__}"
        return payload, 70


def _write_payload(payload: dict[str, Any]) -> None:
    data = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8", errors="strict"
    )
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(data)
        buffer.flush()
    else:
        sys.stdout.write(data.decode("utf-8"))
        sys.stdout.flush()


def main(
    argv: Sequence[str] | None = None,
    *,
    repo_root: Path = REPO_ROOT,
    replace_file: ReplaceFile = os.replace,
) -> int:
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    except Exception as exc:
        payload = _base_payload()
        payload["status"] = INTERNAL_ERROR
        payload["diagnostic"] = f"stdout setup failed: {type(exc).__name__}"
        _write_payload(payload)
        return 70
    payload, exit_code = execute(argv, repo_root=repo_root, replace_file=replace_file)
    _write_payload(payload)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
