"""Validate a review-verdict artifact and fail closed on semantic drift."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


PASS = "PASS"
BLOCKED = "BLOCKED"


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} cannot be read as UTF-8 JSON: {path}: {exc}") from None
    if not isinstance(value, dict):
        raise ValueError(f"{label} root must be a JSON object: {path}")
    return value


# 작업 등급에서 파생되는 리뷰 예산 (캐논 METHODOLOGY.md §3). 입력값으로 받지 않는다.
BUDGET_BY_GRADE = {"L0": 2, "L1": 3, "L2": 4}


def validate_artifact(artifact_path: Path, schema_path: Path) -> list[str]:
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError:
        return ["python dependency unavailable: jsonschema"]

    try:
        schema = _load_object(schema_path, "schema")
        artifact = _load_object(artifact_path, "artifact")
    except ValueError as exc:
        return [str(exc)]

    diagnostics = [
        f"schema: {error.json_path}: {error.message}"
        for error in sorted(
            Draft202012Validator(
                schema, format_checker=FormatChecker()
            ).iter_errors(artifact),
            key=lambda error: list(error.absolute_path),
        )
    ]
    if diagnostics:
        return diagnostics

    reviewed_at = artifact["reviewed_at_utc"]
    try:
        parsed_reviewed_at = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError:
        diagnostics.append("reviewed_at_utc must be an RFC 3339 date-time")
    else:
        if not reviewed_at.endswith("Z") or parsed_reviewed_at.utcoffset() != timezone.utc.utcoffset(None):
            diagnostics.append("reviewed_at_utc must use the UTC Z suffix")

    executor_lineage = artifact["executor"]["lineage"].casefold()
    reviewer_lineage = artifact["reviewer"]["lineage"].casefold()
    mode = artifact["independence_mode"]
    if mode.startswith("cross-lineage") and executor_lineage == reviewer_lineage:
        diagnostics.append("cross-lineage mode requires different executor/reviewer lineage")
    if mode == "same-lineage-degraded" and executor_lineage != reviewer_lineage:
        diagnostics.append("same-lineage-degraded requires matching executor/reviewer lineage")

    findings = artifact["findings"]
    dispositions = artifact["dispositions"]
    finding_ids = [finding["id"] for finding in findings]
    if len(finding_ids) != len(set(finding_ids)):
        diagnostics.append("finding.id values must be unique")
    disposition_ids = [item["finding_id"] for item in dispositions]
    if len(disposition_ids) != len(set(disposition_ids)):
        diagnostics.append("disposition.finding_id values must be unique")
    unknown = sorted(set(disposition_ids) - set(finding_ids))
    if unknown:
        diagnostics.append(f"dispositions reference unknown findings: {unknown}")

    unresolved = sorted(
        finding["id"]
        for finding in findings
        if finding["severity"] in {"P0", "P1", "P2"}
        and finding["id"] not in disposition_ids
    )
    if unresolved:
        diagnostics.append(f"P0/P1/P2 findings require dispositions: {unresolved}")
    # --- 리뷰 예산 (schema 2.0) -------------------------------------------
    # 주장 한정: 여기서 막는 것은 "등급-예산 불일치"와 "누적 초과"뿐이다.
    # rounds_consumed 는 자기신고이며, 호출 승인권은 진행 문서를 읽는 preflight 에 있다.
    work_grade = artifact["work_grade"]
    expected_budget = BUDGET_BY_GRADE[work_grade]
    if artifact["budget_limit"] != expected_budget:
        diagnostics.append(
            f"budget_limit must be derived from work_grade: {work_grade} -> {expected_budget}, "
            f"got {artifact['budget_limit']}"
        )
    if artifact["rounds_consumed"] > artifact["budget_limit"]:
        diagnostics.append(
            "review budget exhausted: "
            f"rounds_consumed={artifact['rounds_consumed']} > budget_limit={artifact['budget_limit']}"
        )

    if artifact["verdict"] == PASS:
        blocking_findings = sorted(
            finding["id"]
            for finding in findings
            if finding["severity"] in {"P0", "P1", "P2"}
        )
        if blocking_findings:
            diagnostics.append(
                "PASS artifact cannot retain P0/P1/P2 findings; re-review required: "
                f"{blocking_findings}"
            )
    return diagnostics


def execute(argv: Sequence[str] | None = None) -> tuple[dict[str, Any], int]:
    parser = argparse.ArgumentParser(description="Validate review-verdict JSON")
    parser.add_argument("artifact", type=Path)
    parser.add_argument(
        "--schema",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "assets" / "review-verdict.schema.json",
    )
    try:
        args = parser.parse_args(argv)
        diagnostics = validate_artifact(args.artifact, args.schema)
    except Exception as exc:
        diagnostics = [str(exc).strip() or type(exc).__name__]
    status = PASS if not diagnostics else BLOCKED
    return {
        "status": status,
        "artifact": str(args.artifact) if "args" in locals() else None,
        "schema": str(args.schema) if "args" in locals() else None,
        "diagnostics": diagnostics,
        "mutations_performed": False,
    }, 0 if status == PASS else 2


def main(argv: Sequence[str] | None = None) -> int:
    payload, exit_code = execute(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
