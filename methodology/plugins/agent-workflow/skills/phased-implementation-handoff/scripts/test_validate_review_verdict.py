from __future__ import annotations

import builtins
import copy
import importlib.util
import json
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = Path(__file__).with_name("validate_review_verdict.py")
SPEC = importlib.util.spec_from_file_location("validate_review_verdict", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


def _example() -> dict[str, Any]:
    return json.loads(
        (SKILL_ROOT / "assets" / "review-verdict.example.json").read_text(
            encoding="utf-8"
        )
    )


def _validate(tmp_path: Path, value: dict[str, Any]) -> list[str]:
    artifact = tmp_path / "review.json"
    artifact.write_text(json.dumps(value), encoding="utf-8")
    return validator.validate_artifact(
        artifact, SKILL_ROOT / "assets" / "review-verdict.schema.json"
    )


def test_example_passes(tmp_path: Path) -> None:
    assert _validate(tmp_path, _example()) == []


def test_cross_lineage_rejects_matching_lineage(tmp_path: Path) -> None:
    value = _example()
    value["reviewer"]["lineage"] = value["executor"]["lineage"]
    assert "requires different" in " ".join(_validate(tmp_path, value))


def test_mode_and_fallback_must_match(tmp_path: Path) -> None:
    value = _example()
    value["independence_mode"] = "same-lineage-degraded"
    value["reviewer"]["lineage"] = value["executor"]["lineage"]
    value["degraded_reason"] = "cross-lineage reviewer unavailable"
    assert "3 was expected" in " ".join(_validate(tmp_path, value))


def test_pass_requires_zero_exit(tmp_path: Path) -> None:
    value = _example()
    value["exit_code"] = 2
    assert "0 was expected" in " ".join(_validate(tmp_path, value))


def test_pass_rejects_p1_finding_even_with_disposition(tmp_path: Path) -> None:
    value = _example()
    value["findings"] = [
        {
            "id": "R-1",
            "severity": "P1",
            "finding": "finding",
            "evidence_or_repro": "repro",
            "impact": "impact",
            "recommendation": "recommendation"
        }
    ]
    value["dispositions"] = [
        {
            "finding_id": "R-1",
            "status": "ACCEPT",
            "evidence": "fixed in worktree and targeted test passed",
            "rationale": "accepted before mandatory re-review"
        }
    ]
    assert "re-review required" in " ".join(_validate(tmp_path, value))


def test_invalid_timestamp_is_blocked(tmp_path: Path) -> None:
    value = copy.deepcopy(_example())
    value["reviewed_at_utc"] = "not-a-date"
    assert "date-time" in " ".join(_validate(tmp_path, value))


def test_missing_jsonschema_dependency_is_blocked(
    monkeypatch: Any, tmp_path: Path
) -> None:
    real_import = builtins.__import__

    def blocked_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "jsonschema":
            raise ImportError("simulated missing dependency")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    assert _validate(tmp_path, _example()) == [
        "python dependency unavailable: jsonschema"
    ]


# --- 리뷰 예산 (schema 2.0) -------------------------------------------------


def test_budget_exhausted_is_blocked(tmp_path: Path) -> None:
    value = _example()
    value["work_grade"] = "L2"
    value["budget_limit"] = 4
    value["rounds_consumed"] = 5
    diagnostics = _validate(tmp_path, value)
    assert any("review budget exhausted" in item for item in diagnostics)


def test_budget_limit_must_derive_from_work_grade(tmp_path: Path) -> None:
    """등급-예산 위조 — L1(=3)인데 4를 신고하면 거부된다."""
    value = _example()
    value["work_grade"] = "L1"
    value["budget_limit"] = 4
    value["rounds_consumed"] = 4
    diagnostics = _validate(tmp_path, value)
    assert any("budget_limit must be derived from work_grade" in item for item in diagnostics)


def test_budget_boundary_is_allowed(tmp_path: Path) -> None:
    """소진 직전(=limit)은 통과한다. 초과만 막는다."""
    value = _example()
    value["work_grade"] = "L2"
    value["budget_limit"] = 4
    value["rounds_consumed"] = 4
    assert _validate(tmp_path, value) == []


def test_schema_version_1_0_artifact_is_rejected(tmp_path: Path) -> None:
    """전환 후 1.0 신규 생성 거부 (D1)."""
    value = _example()
    value["schema_version"] = "1.0"
    diagnostics = _validate(tmp_path, value)
    assert any("schema_version" in item for item in diagnostics)


def test_budget_fields_are_required(tmp_path: Path) -> None:
    for field in ("slice_id", "work_grade", "budget_limit", "rounds_consumed"):
        value = _example()
        del value[field]
        diagnostics = _validate(tmp_path, value)
        assert any(field in item for item in diagnostics), f"{field} must be required"
