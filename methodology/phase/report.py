"""Non-authoritative phase manifest projection and tracked promotion."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from .template import _TemplateError, _render_template_text

__all__ = ["render_template", "project_report", "promote"]

_PHASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_VERDICT_STAGES = {"input_gate", "implementation_review", "final_verification"}
_VERDICT_SUBJECTS = {"design", "prompt", "diff", "mechanical", "test", "integration"}
_REQUIRED_FINAL_SUBJECTS = ("mechanical", "test", "integration")


class _ReportError(Exception):
    def __init__(self, exit_code: int, message: str, **details: Any) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.details = details


def _emit(exit_code: int, message: str, **details: Any) -> int:
    status = {0: "PASS", 1: "FAIL", 2: "BLOCKED"}[exit_code]
    payload = {"status": status, "exit_code": exit_code, "message": message, **details}
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return exit_code


def _load_json_bytes(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        raw = Path(path).read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _ReportError(2, f"manifest is unavailable or invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise _ReportError(2, "manifest root must be a JSON object")
    return value, raw


def _validate_sequences(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise _ReportError(2, "manifest entries must be a list")
    values: list[int] = []
    invalid: list[Any] = []
    for entry in entries:
        seq = entry.get("seq") if isinstance(entry, dict) else None
        if not isinstance(seq, int) or isinstance(seq, bool):
            invalid.append(seq)
        else:
            values.append(seq)
    counts = Counter(values)
    duplicates = sorted(value for value, count in counts.items() if count > 1)
    expected = set(range(1, len(entries) + 1))
    actual = set(values)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if invalid or duplicates or missing or unexpected:
        raise _ReportError(
            2,
            "manifest seq values are not consecutive unique integers from 1",
            invalid_seq=invalid,
            duplicate_seq=duplicates,
            missing_seq=missing,
            unexpected_seq=unexpected,
        )
    return entries


def _inline_record(entry: dict[str, Any]) -> str:
    return json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _section(title: str, entries: list[dict[str, Any]]) -> list[str]:
    lines = [f"## {title}", ""]
    if entries:
        lines.extend(f"- {_inline_record(entry)}" for entry in entries)
    else:
        lines.append("(none recorded)")
    lines.append("")
    return lines


def _unclassified_records(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for entry in entries:
        if entry.get("kind") != "verdict":
            continue
        for field, allowed in (("stage", _VERDICT_STAGES), ("subject", _VERDICT_SUBJECTS)):
            value = entry.get(field)
            if value not in allowed:
                records.append(
                    {"seq": entry["seq"], "kind": entry.get("kind"), "field": field, "value": value}
                )
    return records


def _status_projection(
    entries: list[dict[str, Any]], unclassified: list[dict[str, Any]]
) -> tuple[str, list[dict[str, Any] | str]]:
    candidates: dict[str, list[dict[str, Any]]] = {
        subject: [] for subject in _REQUIRED_FINAL_SUBJECTS
    }
    for entry in entries:
        if entry.get("kind") != "verdict" or entry.get("stage") != "final_verification":
            continue
        subject = entry.get("subject")
        if subject not in _REQUIRED_FINAL_SUBJECTS:
            continue
        candidates[subject].append(entry)
    latest = {
        subject: max(records, key=lambda record: record["seq"])
        for subject, records in candidates.items()
        if records
    }
    if unclassified:
        return "IN_PROGRESS", list(unclassified)
    blocked = [latest[name] for name in _REQUIRED_FINAL_SUBJECTS if latest.get(name, {}).get("verdict") == "BLOCKED"]
    if blocked:
        return "BLOCKED", [_blocking_record(entry) for entry in blocked]
    changes = [
        latest[name]
        for name in _REQUIRED_FINAL_SUBJECTS
        if latest.get(name, {}).get("verdict") == "CHANGES_REQUESTED"
    ]
    if changes:
        return "CHANGES_REQUESTED", [_blocking_record(entry) for entry in changes]
    missing = [name for name in _REQUIRED_FINAL_SUBJECTS if name not in latest]
    if missing:
        return "IN_PROGRESS", [f"missing_subject:{name}" for name in missing]
    if all(latest[name].get("verdict") == "PASS" for name in _REQUIRED_FINAL_SUBJECTS):
        return "PASS", [_blocking_record(latest[name]) for name in _REQUIRED_FINAL_SUBJECTS]
    return "IN_PROGRESS", [_blocking_record(latest[name]) for name in _REQUIRED_FINAL_SUBJECTS]


def _blocking_record(entry: dict[str, Any]) -> dict[str, Any]:
    return {"seq": entry["seq"], "subject": entry.get("subject"), "verdict": entry.get("verdict")}


def _build_report(manifest: dict[str, Any], raw: bytes) -> tuple[str, str]:
    entries = _validate_sequences(manifest)
    unclassified = _unclassified_records(entries)
    status, blocking_records = _status_projection(entries, unclassified)
    input_gate = [
        entry
        for entry in entries
        if entry.get("kind") == "verdict" and entry.get("stage") == "input_gate"
    ]
    implementation = [
        entry
        for entry in entries
        if (entry.get("kind") == "verdict" and entry.get("stage") == "implementation_review")
        or entry.get("kind") in {"role_session", "changed_paths", "command"}
    ]
    final_verification = [
        entry
        for entry in entries
        if (entry.get("kind") == "verdict" and entry.get("stage") == "final_verification")
        or entry.get("kind") == "validation_fact"
    ]
    claim_boundary = [
        entry for entry in entries if entry.get("kind") in {"validation_fact", "finding_disposition"}
    ]
    next_records = [
        entry
        for entry in entries
        if entry.get("kind") == "finding_disposition"
        and entry.get("decision") == "DEFER_OUT_OF_SCOPE"
    ]
    lines = [
        "# PHASE_REPORT",
        "",
        f"phase_id: {manifest.get('phase_id')}",
        f"manifest_sha256: {hashlib.sha256(raw).hexdigest()}",
        "evidence_local_only: true",
        "",
    ]
    lines.extend(_section("Input Gate", input_gate))
    lines.extend(_section("Implementation And Review", implementation))
    lines.extend(_section("Final Verification", final_verification))
    lines.extend(_section("Claim Boundary", claim_boundary))
    lines.extend(_section("Next", next_records))
    if unclassified:
        lines.extend(["## Unclassified Records", ""])
        lines.extend(
            "- "
            + f"(seq={record['seq']}, kind={record['kind']}, field={record['field']}, "
            + f"value={json.dumps(record['value'], ensure_ascii=False)})"
            for record in unclassified
        )
        lines.append("")
    lines.extend(["## Status", "", f"status: {status}", "blocking_records:"])
    lines.extend(f"- {_inline_record(item) if isinstance(item, dict) else item}" for item in blocking_records)
    lines.append("")
    return "\n".join(lines), status


def _write_text(path: Path, content: str) -> None:
    parent = Path(path).parent
    if not parent.is_dir():
        raise _ReportError(2, f"output directory is unavailable: {parent}")
    try:
        Path(path).write_text(content, encoding="utf-8", newline="")
    except OSError as exc:
        raise _ReportError(2, f"output cannot be written: {exc}") from exc


def render_template(template_name: str, values: dict[str, Any], out: Path) -> int:
    """Render one named phase template and return its process-style exit code."""
    try:
        if Path(template_name).name != template_name:
            raise _ReportError(1, "template name must not contain a path")
        template_path = Path(__file__).parent / "templates" / f"{template_name}.md"
        try:
            source = template_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise _ReportError(2, f"template is unavailable: {exc}") from exc
        content = _render_template_text(source, template_name, values)
        _write_text(Path(out), content)
    except _TemplateError as exc:
        return _emit(exc.exit_code, str(exc))
    except _ReportError as exc:
        return _emit(exc.exit_code, str(exc), **exc.details)
    except Exception as exc:
        return _emit(2, f"template rendering is blocked: {exc}")
    return _emit(0, "template rendered", out=str(out))


def project_report(manifest: Path, out: Path) -> int:
    """Project one working manifest to Markdown and return an exit code."""
    try:
        value, raw = _load_json_bytes(Path(manifest))
        content, status = _build_report(value, raw)
        _write_text(Path(out), content)
    except _ReportError as exc:
        return _emit(exc.exit_code, str(exc), **exc.details)
    except Exception as exc:
        return _emit(2, f"report projection is blocked: {exc}")
    return _emit(0, "report projected", out=str(out), projected_status=status)


def _validate_phase_id(phase_id: Any) -> str:
    if not isinstance(phase_id, str) or _PHASE_ID_PATTERN.fullmatch(phase_id) is None:
        raise _ReportError(1, f"phase_id has invalid format: {phase_id!r}")
    return phase_id


def _ensure_contained(root: Path, paths: list[Path]) -> None:
    root_resolved = root.resolve()
    for path in paths:
        resolved = path.resolve()
        if not resolved.is_relative_to(root_resolved):
            raise _ReportError(1, f"promotion path escapes root: {resolved}")


def _temp_path(directory: Path, name: str) -> Path:
    return directory / f"{name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"


def _remove_temp(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def promote(manifest: Path, phase_id: str, root: Path) -> int:
    """Promote a working manifest and its deterministic report to tracked files."""
    manifest_temp: Path | None = None
    report_temp: Path | None = None
    promoted: list[str] = []
    try:
        manifest_value, manifest_bytes = _load_json_bytes(Path(manifest))
        _validate_sequences(manifest_value)
        destination_phase = _validate_phase_id(phase_id)
        source_phase = manifest_value.get("phase_id")
        if source_phase != destination_phase:
            raise _ReportError(
                1,
                "manifest phase_id does not match destination phase_id",
                manifest_phase_id=source_phase,
                destination_phase_id=destination_phase,
            )
        report_text, _status = _build_report(manifest_value, manifest_bytes)
        report_bytes = report_text.encode("utf-8")
        root_path = Path(root)
        target_directory = root_path / destination_phase
        manifest_target = target_directory / "manifest.json"
        report_target = target_directory / "PHASE_REPORT.md"
        _ensure_contained(root_path, [target_directory, manifest_target, report_target])
        try:
            target_directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise _ReportError(2, f"promotion directory cannot be created: {exc}") from exc
        manifest_temp = _temp_path(target_directory, "manifest.json")
        report_temp = _temp_path(target_directory, "PHASE_REPORT.md")
        try:
            with open(manifest_temp, "xb") as handle:
                handle.write(manifest_bytes)
            with open(report_temp, "xb") as handle:
                handle.write(report_bytes)
        except OSError as exc:
            raise _ReportError(2, f"promotion temp files cannot be created: {exc}") from exc
        _ensure_contained(
            root_path,
            [target_directory, manifest_target, report_target, manifest_temp, report_temp],
        )
        desired = ((manifest_target, manifest_temp, manifest_bytes), (report_target, report_temp, report_bytes))
        for target, _temporary, expected_bytes in desired:
            if target.exists() and target.read_bytes() != expected_bytes:
                raise _ReportError(1, f"promotion target differs from requested content: {target}")
        for target, temporary, expected_bytes in desired:
            if target.exists() and target.read_bytes() == expected_bytes:
                continue
            try:
                os.replace(temporary, target)
            except OSError as exc:
                partial = promoted == ["manifest.json"]
                return _emit(
                    2,
                    f"promotion replace is blocked: {exc}",
                    promoted=promoted,
                    partial=partial,
                )
            promoted.append(target.name)
    except _ReportError as exc:
        return _emit(exc.exit_code, str(exc), **exc.details)
    except (OSError, UnicodeError) as exc:
        return _emit(2, f"promotion is blocked: {exc}", promoted=promoted)
    except Exception as exc:
        return _emit(2, f"promotion is blocked: {exc}", promoted=promoted)
    finally:
        _remove_temp(manifest_temp)
        _remove_temp(report_temp)
    return _emit(0, "phase artifacts promoted", promoted=promoted, partial=False)


def _load_values(source: str) -> Any:
    if source == "-":
        return json.load(sys.stdin)
    return json.loads(source)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m methodology.phase.report")
    subparsers = parser.add_subparsers(dest="command", required=True)
    render_parser = subparsers.add_parser("render-plan")
    render_parser.add_argument("--template", required=True)
    render_parser.add_argument("--values", required=True)
    render_parser.add_argument("--out", required=True, type=Path)
    project_parser = subparsers.add_parser("project")
    project_parser.add_argument("--manifest", required=True, type=Path)
    project_parser.add_argument("--out", required=True, type=Path)
    promote_parser = subparsers.add_parser("promote")
    promote_parser.add_argument("--manifest", required=True, type=Path)
    promote_parser.add_argument("--phase-id", required=True)
    promote_parser.add_argument("--root", required=True, type=Path)
    return parser


def _main(arguments: list[str] | None = None) -> int:
    parsed = _build_parser().parse_args(arguments)
    if parsed.command == "render-plan":
        try:
            values = _load_values(parsed.values)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return _emit(2, f"template values are unavailable or invalid JSON: {exc}")
        return render_template(parsed.template, values, parsed.out)
    if parsed.command == "project":
        return project_report(parsed.manifest, parsed.out)
    return promote(parsed.manifest, parsed.phase_id, parsed.root)


if __name__ == "__main__":
    raise SystemExit(_main())
