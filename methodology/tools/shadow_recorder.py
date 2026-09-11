#!/usr/bin/env python3
"""T16-P2d shadow recorder.

selector 결과를 실행 판정으로 승격하지 않고, freeze 대조와 관측 필드만
결정론 JSON 기록으로 남긴다.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import validation_impact_selector as selector

SCHEMA_VERSION = 2
EXIT_OK = 0
EXIT_INPUT = 2
EXIT_INTERNAL = 70
SELECTOR_STATUSES = frozenset({"PASS", "BLOCKED"})
FULL_GATE_STATUSES = frozenset({"PASS", "CHANGES_REQUESTED", "BLOCKED"})
FOCUSED_STATUS_PRIORITY = {"PASS": 0, "CHANGES_REQUESTED": 1, "BLOCKED": 2}
if set(FOCUSED_STATUS_PRIORITY) != FULL_GATE_STATUSES:
    raise RuntimeError(
        "FOCUSED_STATUS_PRIORITY keys must match FULL_GATE_STATUSES"
    )
FULL_GATE_SOURCES = frozenset({"relay", "orchestrator-manual"})
COMPARISON_STATUSES = frozenset(
    {
        "PENDING_PHASE_FULL_GATE",
        "NOT_AVAILABLE_IN_ATTACHMENT_PROOF",
        "COMPARED",
        "BLOCKED_INPUT",
    }
)
FULL_GATE_REF_FIELDS = frozenset(
    {
        "status",
        "exit_code",
        "duration_s",
        "envelope_path",
        "stdout_path",
        "source",
    }
)
FOCUSED_GATE_REF_FIELDS = FULL_GATE_REF_FIELDS | {"group_id"}
AGREEMENT_SAME = "SAME_VERDICT_MACHINE"
AGREEMENT_DIFFERENT = "DIFFERENT_VERDICT_MACHINE"
AGREEMENT_UNKNOWN = "NOT_MACHINE_DETERMINED"
NOT_CLAIMED = [
    "promotion",
    "token_saving",
    "p2d_completion",
    "comparison_interpretation",
]
SIDECAR_PATH = Path(".ztr/orchestrator/T16-P2b2/draft/derivation-sidecar.json")


class RecorderInputError(ValueError):
    """계약 위반을 exit 2로 닫기 위한 구조화 오류."""

    def __init__(
        self,
        reasons: Sequence[str],
        summary_fields: Mapping[str, object] | None = None,
    ) -> None:
        self.reasons = tuple(sorted(set(reasons)))
        self.summary_fields = dict(summary_fields or {})
        super().__init__("; ".join(self.reasons))


def _load_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as error:
        raise RecorderInputError([f"{label}:not_utf8"]) from error
    except json.JSONDecodeError as error:
        raise RecorderInputError([f"{label}:malformed_json"]) from error
    except OSError as error:
        raise RecorderInputError([f"{label}:unreadable:{path.as_posix()}"]) from error


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise RecorderInputError([f"file:unreadable:{path.as_posix()}"]) from error


def _validate_freeze(doc: Any) -> str:
    if not isinstance(doc, dict):
        raise RecorderInputError(["freeze:not_object"])
    sha = doc.get("registry_sha256")
    if not isinstance(sha, str) or len(sha) != 64:
        raise RecorderInputError(["freeze:invalid_registry_sha256"])
    return sha


def _load_rule_classes(repo_root: Path) -> dict[str, str]:
    sidecar = repo_root / SIDECAR_PATH
    if not sidecar.exists():
        return {}
    doc = _load_json(sidecar, "derivation_sidecar")
    classes: dict[str, str] = {}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            rule_id = value.get("rule_id")
            rule_class = value.get("class")
            if (
                isinstance(rule_id, str)
                and isinstance(rule_class, str)
                and rule_class in {"A", "B", "C", "D", "E"}
            ):
                classes[rule_id] = rule_class
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(doc)
    return classes


def _validate_full_gate_ref(doc: Any) -> dict[str, object]:
    if not isinstance(doc, dict):
        raise RecorderInputError(["full_gate_ref:not_object"])
    reasons: list[str] = []
    for key in doc:
        if key not in FULL_GATE_REF_FIELDS:
            reasons.append(f"full_gate_ref:unknown_field:{key}")
    for key in FULL_GATE_REF_FIELDS:
        if key not in doc:
            reasons.append(f"full_gate_ref:missing_field:{key}")
    status = doc.get("status")
    if not isinstance(status, str) or status not in FULL_GATE_STATUSES:
        reasons.append("full_gate_ref:invalid_status")
    exit_code = doc.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        reasons.append("full_gate_ref:invalid_exit_code")
    duration_s = doc.get("duration_s")
    if (
        isinstance(duration_s, bool)
        or not isinstance(duration_s, int | float)
        or duration_s < 0
    ):
        reasons.append("full_gate_ref:invalid_duration_s")
        duration_value = 0.0
    else:
        duration_value = float(duration_s)
    for key in ("envelope_path", "stdout_path"):
        if not isinstance(doc.get(key), str):
            reasons.append(f"full_gate_ref:invalid_{key}")
    source = doc.get("source")
    if not isinstance(source, str) or source not in FULL_GATE_SOURCES:
        reasons.append("full_gate_ref:invalid_source")
    if reasons:
        raise RecorderInputError(reasons)
    return {
        "status": status,
        "exit_code": exit_code,
        "duration_s": duration_value,
        "envelope_path": doc["envelope_path"],
        "stdout_path": doc["stdout_path"],
        "source": source,
    }


def _validate_focused_gate_ref(doc: Any) -> dict[str, object]:
    if not isinstance(doc, dict):
        raise RecorderInputError(["focused_gate_ref:not_object"])
    reasons: list[str] = []
    for key in doc:
        if key not in FOCUSED_GATE_REF_FIELDS:
            reasons.append(f"focused_gate_ref:unknown_field:{key}")
    for key in FOCUSED_GATE_REF_FIELDS:
        if key not in doc:
            reasons.append(f"focused_gate_ref:missing_field:{key}")
    status = doc.get("status")
    if not isinstance(status, str) or status not in FULL_GATE_STATUSES:
        reasons.append("focused_gate_ref:invalid_status")
    exit_code = doc.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        reasons.append("focused_gate_ref:invalid_exit_code")
    duration_s = doc.get("duration_s")
    if (
        isinstance(duration_s, bool)
        or not isinstance(duration_s, int | float)
        or duration_s < 0
    ):
        reasons.append("focused_gate_ref:invalid_duration_s")
        duration_value = 0.0
    else:
        duration_value = float(duration_s)
    envelope_path = doc.get("envelope_path")
    if not isinstance(envelope_path, str):
        reasons.append("focused_gate_ref:invalid_envelope_path")
    stdout_path = doc.get("stdout_path")
    if not isinstance(stdout_path, str):
        reasons.append("focused_gate_ref:invalid_stdout_path")
    source = doc.get("source")
    if not isinstance(source, str) or source not in FULL_GATE_SOURCES:
        reasons.append("focused_gate_ref:invalid_source")
    group_id = doc.get("group_id")
    if not isinstance(group_id, str):
        reasons.append("focused_gate_ref:invalid_group_id")
    if reasons:
        raise RecorderInputError(reasons)
    return {
        "status": status,
        "exit_code": exit_code,
        "duration_s": duration_value,
        "envelope_path": envelope_path,
        "stdout_path": stdout_path,
        "source": source,
        "group_id": group_id,
    }


def _recommended_group_ids(
    recommended_groups: Sequence[Mapping[str, object]],
) -> set[str]:
    ids: set[str] = set()
    for group in recommended_groups:
        group_id = group.get("id")
        if isinstance(group_id, str):
            ids.add(group_id)
    return ids


def _focused_group_ids(
    recommended_groups: Sequence[Mapping[str, object]],
) -> set[str]:
    ids: set[str] = set()
    for group in recommended_groups:
        group_id = group.get("id")
        if isinstance(group_id, str) and group.get("scope_class") == "focused":
            ids.add(group_id)
    return ids


def _validate_focused_gate_refs(
    docs: Sequence[dict[str, object]],
    recommended_groups: Sequence[Mapping[str, object]],
) -> None:
    recommended_ids = _recommended_group_ids(recommended_groups)
    seen: set[str] = set()
    reasons: list[str] = []
    for doc in docs:
        group_id = doc["group_id"]
        if not isinstance(group_id, str):
            continue
        if group_id in seen:
            reasons.append("focused_gate_ref:duplicate_group_id")
        seen.add(group_id)
        if group_id not in recommended_ids:
            reasons.append("focused_gate_ref:group_id_not_in_recommendation")
    if reasons:
        raise RecorderInputError(reasons)


def _compose_focused_result(
    focused_gate_refs: Sequence[Mapping[str, object]],
) -> tuple[str | None, int | None]:
    if not focused_gate_refs:
        return None, None
    statuses: list[str] = []
    for ref in focused_gate_refs:
        status = ref.get("status")
        if not isinstance(status, str) or status not in FOCUSED_STATUS_PRIORITY:
            raise RecorderInputError(["focused_gate_ref:invalid_status"])
        statuses.append(status)
    worst_status = max(statuses, key=lambda status: FOCUSED_STATUS_PRIORITY[status])
    focused_exit: int | None = None
    if len(focused_gate_refs) == 1:
        exit_code = focused_gate_refs[0].get("exit_code")
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            raise RecorderInputError(["focused_gate_ref:invalid_exit_code"])
        focused_exit = exit_code
    return worst_status, focused_exit


def _build_comparison(
    *,
    full_gate_ref: Mapping[str, object],
    focused_gate_refs: Sequence[Mapping[str, object]],
    recommended_groups: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    focused_ids = _focused_group_ids(recommended_groups)
    focused_would_have_run = bool(focused_ids)
    full_status = full_gate_ref["status"]
    full_exit = full_gate_ref["exit_code"]
    focused_refs_for_comparison = [
        ref for ref in focused_gate_refs if ref.get("group_id") in focused_ids
    ]
    focused_status, focused_exit = _compose_focused_result(focused_refs_for_comparison)
    covered_ids = {
        ref["group_id"]
        for ref in focused_refs_for_comparison
        if isinstance(ref.get("group_id"), str)
    }
    if not focused_would_have_run:
        agreement = AGREEMENT_UNKNOWN
        unknown_reason: str | None = "no_focused_group_in_recommendation"
    elif not focused_refs_for_comparison:
        agreement = AGREEMENT_UNKNOWN
        unknown_reason = "focused_gate_ref_absent"
    elif covered_ids != focused_ids:
        agreement = AGREEMENT_UNKNOWN
        unknown_reason = "focused_gate_ref_incomplete"
    elif focused_status == full_status:
        agreement = AGREEMENT_SAME
        unknown_reason = None
    else:
        agreement = AGREEMENT_DIFFERENT
        unknown_reason = None

    if (
        agreement != AGREEMENT_UNKNOWN
        and isinstance(full_status, str)
        and isinstance(focused_status, str)
    ):
        red_missed_machine: bool | None = (
            full_status != "PASS" and focused_status == "PASS"
        )
    else:
        red_missed_machine = None

    return {
        "focused_status": focused_status,
        "focused_exit": focused_exit,
        "full_status": full_status,
        "full_exit": full_exit,
        "focused_would_have_run": focused_would_have_run,
        "agreement": agreement,
        "agreement_unknown_reason": unknown_reason,
        "red_missed_machine": red_missed_machine,
    }


def _rule_ids_by_group(matches: Sequence[object]) -> dict[str, list[str]]:
    by_group: dict[str, set[str]] = {}
    for match in matches:
        if not isinstance(match, Mapping):
            continue
        rule_ids = match.get("rule_ids")
        group_ids = match.get("group_ids")
        if not isinstance(rule_ids, list) or not isinstance(group_ids, list):
            continue
        valid_rule_ids = [item for item in rule_ids if isinstance(item, str)]
        for group_id in group_ids:
            if isinstance(group_id, str):
                by_group.setdefault(group_id, set()).update(valid_rule_ids)
    return {key: sorted(value) for key, value in sorted(by_group.items())}


def _unmapped_paths(selector_result: Mapping[str, object]) -> list[str]:
    paths: list[str] = []
    reasons = selector_result.get("reasons", [])
    if isinstance(reasons, list):
        prefix = "selector:unmatched_path:"
        for reason in reasons:
            if isinstance(reason, str) and reason.startswith(prefix):
                paths.append(reason.removeprefix(prefix))
    return sorted(paths)


def _selector_reasons(selector_result: Mapping[str, object]) -> list[str]:
    reasons = selector_result.get("reasons", [])
    if not isinstance(reasons, list):
        return []
    return [reason for reason in reasons if isinstance(reason, str)]


def _is_unmapped_only_block(selector_result: Mapping[str, object]) -> bool:
    reasons = _selector_reasons(selector_result)
    return bool(reasons) and all(
        reason.startswith("selector:unmatched_path:") for reason in reasons
    )


def _decorate_groups(
    selector_result: Mapping[str, object], rule_classes: Mapping[str, str]
) -> list[dict[str, object]]:
    matches = selector_result.get("matches", [])
    if isinstance(matches, str | bytes) or not isinstance(matches, Sequence):
        matches = []
    rule_ids_for_group = _rule_ids_by_group(matches)
    decorated: list[dict[str, object]] = []
    groups = selector_result.get("recommended_groups", [])
    if not isinstance(groups, list):
        return decorated
    for group in groups:
        if not isinstance(group, Mapping):
            continue
        group_id = group.get("id")
        if not isinstance(group_id, str):
            continue
        rule_ids = rule_ids_for_group.get(group_id, [])
        classes = sorted({rule_classes.get(rule_id, "UNKNOWN") for rule_id in rule_ids})
        decorated.append(
            {
                "id": group_id,
                "scope_class": group.get("scope_class"),
                "cwd": group.get("cwd"),
                "argv": group.get("argv"),
                "rule_ids": rule_ids,
                "observed_rule_classes": classes,
                "class_provenance": "derivation-sidecar (non-authoritative)",
            }
        )
    return decorated


def _comparison_status(
    requested: str | None, selector_status: str, full_gate_ref_provided: bool
) -> str:
    if requested is not None:
        if requested not in COMPARISON_STATUSES:
            raise RecorderInputError(["comparison_status:unknown"])
        if requested == "COMPARED" and not full_gate_ref_provided:
            raise RecorderInputError(["comparison_status:compared_without_full_gate_ref"])
        return requested
    if selector_status == "BLOCKED":
        return "BLOCKED_INPUT"
    if full_gate_ref_provided:
        return "COMPARED"
    return "PENDING_PHASE_FULL_GATE"


def canonical_json(doc: Mapping[str, object]) -> str:
    return json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def record_shadow(
    *,
    changed_paths: Path,
    registry: Path,
    freeze: Path,
    phase_id: str,
    out_dir: Path,
    full_gate_ref_path: Path | None = None,
    focused_gate_ref_paths: Sequence[Path] | None = None,
    comparison_status: str | None = None,
    repo_root: Path | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    root = Path.cwd() if repo_root is None else repo_root
    registry_sha256 = _sha256(registry)
    freeze_sha256 = _validate_freeze(_load_json(freeze, "freeze"))
    if registry_sha256 != freeze_sha256:
        raise RecorderInputError(["freeze:registry_sha256_mismatch"])

    started = time.perf_counter()
    selector_result, selector_exit_code = selector.run_selector(changed_paths, registry)
    overhead_s = time.perf_counter() - started
    selector_status = selector_result.get("status")
    if not isinstance(selector_status, str) or selector_status not in SELECTOR_STATUSES:
        raise RecorderInputError(["selector:invalid_status"])
    observed_selector = {"selector_status": selector_status}
    if selector_status == "BLOCKED" and not _is_unmapped_only_block(selector_result):
        raise RecorderInputError(
            _selector_reasons(selector_result) or ["selector:block"],
            observed_selector,
        )

    full_gate_ref_provided = full_gate_ref_path is not None
    focused_gate_ref_paths = tuple(focused_gate_ref_paths or ())
    focused_gate_ref_provided = bool(focused_gate_ref_paths)
    try:
        full_gate_ref: dict[str, object] | None = (
            _validate_full_gate_ref(_load_json(full_gate_ref_path, "full_gate_ref"))
            if full_gate_ref_path is not None
            else None
        )
        focused_gate_refs = [
            _validate_focused_gate_ref(_load_json(path, "focused_gate_ref"))
            for path in focused_gate_ref_paths
        ]
        if focused_gate_ref_provided and not full_gate_ref_provided:
            raise RecorderInputError(["focused_gate_ref:without_full_gate_ref"])
        final_comparison_status = _comparison_status(
            comparison_status, selector_status, full_gate_ref_provided
        )
        if focused_gate_ref_provided and final_comparison_status != "COMPARED":
            raise RecorderInputError(
                [f"focused_gate_ref:non_compared:{final_comparison_status}"]
            )
    except RecorderInputError as error:
        raise RecorderInputError(error.reasons, observed_selector) from error
    if final_comparison_status == "COMPARED" and selector_status == "BLOCKED":
        raise RecorderInputError(
            ["comparison_status:compared_with_blocked_selector"],
            {
                "selector_status": selector_status,
                "comparison_status": final_comparison_status,
            },
        )

    rule_classes = _load_rule_classes(root)
    recommended_groups = _decorate_groups(selector_result, rule_classes)
    try:
        _validate_focused_gate_refs(focused_gate_refs, recommended_groups)
    except RecorderInputError as error:
        raise RecorderInputError(error.reasons, observed_selector) from error
    record: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "phase_id": phase_id,
        "recorded_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "registry_sha256": registry_sha256,
        "freeze_registry_sha256": freeze_sha256,
        "freeze_match": True,
        "changed_paths_sha256": selector_result.get("changed_paths_sha256"),
        "selector_status": selector_status,
        "selector_exit_code": selector_exit_code,
        "recommended_groups": recommended_groups,
        "unmapped_paths": _unmapped_paths(selector_result),
        "overhead_s": round(overhead_s, 6),
        "full_gate_ref": full_gate_ref,
        "focused_gate_refs": focused_gate_refs,
        "recommendation_full_comparison_status": final_comparison_status,
        "not_claimed": NOT_CLAIMED,
    }
    if final_comparison_status == "COMPARED":
        if full_gate_ref is None:
            raise RecorderInputError(
                ["comparison_status:compared_without_full_gate_ref"],
                observed_selector,
            )
        record["comparison"] = _build_comparison(
            full_gate_ref=full_gate_ref,
            focused_gate_refs=focused_gate_refs,
            recommended_groups=recommended_groups,
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    record_name = f"{phase_id}-{str(record['changed_paths_sha256'])[:12]}.json"
    record_path = out_dir / record_name
    record_path.write_text(canonical_json(record) + "\n", encoding="utf-8")
    summary = {
        "record_path": record_path.as_posix(),
        "selector_status": selector_status,
        "exit_code": EXIT_OK,
        "comparison_status": final_comparison_status,
    }
    return record, summary


def _emit(doc: Mapping[str, object]) -> None:
    text = canonical_json(doc) + "\n"
    stdout = sys.stdout
    if isinstance(stdout, io.TextIOWrapper):
        try:
            stdout.reconfigure(encoding="utf-8", newline="\n")
        except Exception:
            pass
    try:
        stdout.write(text)
    except UnicodeEncodeError:
        stdout.write(json.dumps(doc, ensure_ascii=True, sort_keys=True) + "\n")
    stdout.flush()


def _blocked_summary(
    exit_code: int,
    reasons: Sequence[str],
    observed: Mapping[str, object] | None = None,
) -> dict[str, object]:
    summary: dict[str, object] = {
        "record_path": None,
        "exit_code": exit_code,
        "reasons": sorted(set(reasons)),
    }
    if observed is not None:
        for key in ("selector_status", "comparison_status"):
            if key in observed:
                summary[key] = observed[key]
    return summary


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shadow_recorder.py")
    sub = parser.add_subparsers(dest="command", required=True)
    record = sub.add_parser("record")
    record.add_argument("--changed-paths", required=True)
    record.add_argument("--registry", required=True)
    record.add_argument("--freeze", required=True)
    record.add_argument("--phase-id", required=True)
    record.add_argument("--out-dir", required=True)
    record.add_argument("--full-gate-ref")
    record.add_argument("--focused-gate-ref", action="append")
    record.add_argument("--comparison-status")
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    except SystemExit as error:
        if error.code in (0, None):
            return EXIT_OK
        _emit(_blocked_summary(EXIT_INPUT, ["usage_error"]))
        return EXIT_INPUT
    try:
        _record, summary = record_shadow(
            changed_paths=Path(args.changed_paths),
            registry=Path(args.registry),
            freeze=Path(args.freeze),
            phase_id=args.phase_id,
            out_dir=Path(args.out_dir),
            full_gate_ref_path=Path(args.full_gate_ref)
            if args.full_gate_ref is not None
            else None,
            focused_gate_ref_paths=[
                Path(path) for path in (args.focused_gate_ref or [])
            ],
            comparison_status=args.comparison_status,
        )
        _emit(summary)
        return EXIT_OK
    except RecorderInputError as error:
        _emit(_blocked_summary(EXIT_INPUT, error.reasons, error.summary_fields))
        return EXIT_INPUT
    except Exception as error:
        _emit(
            _blocked_summary(
                EXIT_INTERNAL, [f"internal_error:{type(error).__name__}"]
            )
        )
        return EXIT_INTERNAL


def main() -> int:
    return run(None)


if __name__ == "__main__":
    sys.exit(main())
