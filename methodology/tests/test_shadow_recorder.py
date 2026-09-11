"""T16-P2d shadow recorder 계약 테스트."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, cast

import pytest

METHODOLOGY_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = METHODOLOGY_ROOT.parent
TOOLS_DIR = METHODOLOGY_ROOT / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import shadow_recorder as recorder  # noqa: E402


VALID_CHANGED: dict[str, Any] = {
    "schema_version": 1,
    "paths": ["methodology/tools/shadow_recorder.py"],
}
VALID_REGISTRY: dict[str, Any] = {
    "schema_version": 1,
    "groups": [
        {
            "id": "shadow-focused",
            "cwd": ".",
            "argv": [
                "python",
                "-m",
                "pytest",
                "-q",
                "methodology/tests/test_shadow_recorder.py",
            ],
            "scope_class": "focused",
        }
    ],
    "rules": [
        {
            "id": "exact-shadow-recorder",
            "match": {
                "kind": "exact",
                "value": "methodology/tools/shadow_recorder.py",
            },
            "groups": ["shadow-focused"],
        }
    ],
}
FULL_GATE_REF: dict[str, Any] = {
    "status": "BLOCKED",
    "exit_code": 2,
    "duration_s": 0.0,
    "envelope_path": ".ztr/orchestrator/T16-P2d3/records/not-available.json",
    "stdout_path": ".ztr/orchestrator/T16-P2d3/records/not-available.stdout.txt",
    "source": "orchestrator-manual",
}


def focused_ref(
    group_id: str,
    *,
    status: str = "PASS",
    exit_code: int = 0,
    duration_s: int | float = 0.0,
) -> dict[str, Any]:
    doc = copy.deepcopy(FULL_GATE_REF)
    doc["status"] = status
    doc["exit_code"] = exit_code
    doc["duration_s"] = duration_s
    doc["group_id"] = group_id
    return doc


def registry_with_groups(groups: list[dict[str, Any]]) -> dict[str, Any]:
    group_ids = [str(group["id"]) for group in groups]
    return {
        "schema_version": 1,
        "groups": groups,
        "rules": [
            {
                "id": "exact-shadow-recorder",
                "match": {
                    "kind": "exact",
                    "value": "methodology/tools/shadow_recorder.py",
                },
                "groups": group_ids,
            }
        ],
    }


def make_inputs_with_registry(
    tmp_path: Path, registry_doc: dict[str, Any]
) -> tuple[Path, Path, Path, Path]:
    changed = write_json(tmp_path, "changed-paths.json", VALID_CHANGED)
    registry = write_json(tmp_path, "registry.json", registry_doc)
    freeze = freeze_for(registry, tmp_path)
    full_ref = write_json(tmp_path, "full-gate-ref.json", FULL_GATE_REF)
    return changed, registry, freeze, full_ref


def record_compared(
    tmp_path: Path,
    *,
    full_ref_doc: dict[str, Any] | None = None,
    focused_ref_docs: list[dict[str, Any]] | None = None,
    registry_doc: dict[str, Any] | None = None,
    comparison_status: str = "COMPARED",
) -> dict[str, Any]:
    changed, registry, freeze, full_ref = make_inputs_with_registry(
        tmp_path, registry_doc or VALID_REGISTRY
    )
    if full_ref_doc is not None:
        full_ref.write_text(json.dumps(full_ref_doc), encoding="utf-8")
    focused_paths = [
        write_json(tmp_path, f"focused-{index}.json", doc)
        for index, doc in enumerate(focused_ref_docs or [])
    ]
    record, _summary = recorder.record_shadow(
        changed_paths=changed,
        registry=registry,
        freeze=freeze,
        phase_id="T16-RECSCHEMA",
        out_dir=tmp_path / "records",
        full_gate_ref_path=full_ref,
        focused_gate_ref_paths=focused_paths,
        comparison_status=comparison_status,
        repo_root=REPO_ROOT,
    )
    return record


def write_json(tmp_path: Path, name: str, doc: Any) -> Path:
    target = tmp_path / name
    target.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return target


def freeze_for(registry_file: Path, tmp_path: Path) -> Path:
    return write_json(
        tmp_path,
        "freeze.json",
        {
            "schema_version": 1,
            "registry_sha256": hashlib.sha256(registry_file.read_bytes()).hexdigest(),
        },
    )


def make_inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    changed = write_json(tmp_path, "changed-paths.json", VALID_CHANGED)
    registry = write_json(tmp_path, "registry.json", VALID_REGISTRY)
    freeze = freeze_for(registry, tmp_path)
    full_ref = write_json(tmp_path, "full-gate-ref.json", FULL_GATE_REF)
    return changed, registry, freeze, full_ref


def sanitize(record: dict[str, Any]) -> dict[str, Any]:
    copy_record = copy.deepcopy(record)
    copy_record.pop("recorded_at_utc")
    return copy_record


def test_record_writes_observation_only_schema(tmp_path: Path) -> None:
    changed, registry, freeze, full_ref = make_inputs(tmp_path)

    record, summary = recorder.record_shadow(
        changed_paths=changed,
        registry=registry,
        freeze=freeze,
        phase_id="T16-P2d-S1",
        out_dir=tmp_path / "records",
        full_gate_ref_path=full_ref,
        comparison_status="NOT_AVAILABLE_IN_ATTACHMENT_PROOF",
        repo_root=REPO_ROOT,
    )

    assert summary["exit_code"] == 0
    record_path = Path(str(summary["record_path"]))
    assert record_path.exists()
    assert "verdict" not in record
    assert record["selector_status"] == "PASS"
    assert record["selector_exit_code"] == 0
    assert record["freeze_match"] is True
    assert (
        record["recommendation_full_comparison_status"]
        == "NOT_AVAILABLE_IN_ATTACHMENT_PROOF"
    )
    assert "comparison" not in record
    assert record["not_claimed"] == [
        "promotion",
        "token_saving",
        "p2d_completion",
        "comparison_interpretation",
    ]
    groups = cast(list[dict[str, Any]], record["recommended_groups"])
    group = groups[0]
    assert group["rule_ids"] == ["exact-shadow-recorder"]
    assert group["class_provenance"] == "derivation-sidecar (non-authoritative)"


def test_freeze_mismatch_blocks_without_record(tmp_path: Path) -> None:
    changed, registry, _freeze, full_ref = make_inputs(tmp_path)
    bad_freeze = write_json(tmp_path, "bad-freeze.json", {"registry_sha256": "0" * 64})

    with pytest.raises(recorder.RecorderInputError) as excinfo:
        recorder.record_shadow(
            changed_paths=changed,
            registry=registry,
            freeze=bad_freeze,
            phase_id="T16-P2d-S1",
            out_dir=tmp_path / "records",
            full_gate_ref_path=full_ref,
            comparison_status="NOT_AVAILABLE_IN_ATTACHMENT_PROOF",
            repo_root=REPO_ROOT,
        )

    assert "freeze:registry_sha256_mismatch" in excinfo.value.reasons
    assert not (tmp_path / "records").exists()


@pytest.mark.parametrize(
    ("doc", "reason"),
    [
        (
            {"paths": ["methodology/tools/shadow_recorder.py"]},
            "changed_paths:missing_field:schema_version",
        ),
        (
            {"schema_version": 1, "paths": "methodology/tools/shadow_recorder.py"},
            "changed_paths:paths_not_array",
        ),
    ],
)
def test_malformed_changed_paths_blocks_without_record(
    tmp_path: Path, doc: dict[str, Any], reason: str
) -> None:
    changed, registry, freeze, full_ref = make_inputs(tmp_path)
    changed.write_text(json.dumps(doc), encoding="utf-8")

    with pytest.raises(recorder.RecorderInputError) as excinfo:
        recorder.record_shadow(
            changed_paths=changed,
            registry=registry,
            freeze=freeze,
            phase_id="T16-P2d-S1",
            out_dir=tmp_path / "records",
            full_gate_ref_path=full_ref,
            comparison_status="NOT_AVAILABLE_IN_ATTACHMENT_PROOF",
            repo_root=REPO_ROOT,
        )

    assert any(reason in item for item in excinfo.value.reasons)
    assert not (tmp_path / "records").exists()


def test_unmapped_selector_blocked_is_recorded_as_successful_observation(
    tmp_path: Path,
) -> None:
    changed, registry, freeze, full_ref = make_inputs(tmp_path)
    changed.write_text(
        json.dumps({"schema_version": 1, "paths": ["unmapped/path.py"]}),
        encoding="utf-8",
    )

    record, summary = recorder.record_shadow(
        changed_paths=changed,
        registry=registry,
        freeze=freeze,
        phase_id="T16-P2d-S1",
        out_dir=tmp_path / "records",
        full_gate_ref_path=full_ref,
        comparison_status="BLOCKED_INPUT",
        repo_root=REPO_ROOT,
    )

    assert summary["exit_code"] == 0
    assert record["selector_status"] == "BLOCKED"
    assert record["selector_exit_code"] == 2
    assert record["unmapped_paths"] == ["unmapped/path.py"]
    assert Path(str(summary["record_path"])).exists()


def test_compared_without_full_gate_ref_blocks(tmp_path: Path) -> None:
    changed, registry, freeze, _full_ref = make_inputs(tmp_path)

    with pytest.raises(recorder.RecorderInputError) as excinfo:
        recorder.record_shadow(
            changed_paths=changed,
            registry=registry,
            freeze=freeze,
            phase_id="T16-P2d-S1",
            out_dir=tmp_path / "records",
            comparison_status="COMPARED",
            repo_root=REPO_ROOT,
        )

    assert "comparison_status:compared_without_full_gate_ref" in excinfo.value.reasons
    assert not (tmp_path / "records").exists()


def test_missing_full_gate_ref_records_null_without_synthesized_status(
    tmp_path: Path,
) -> None:
    changed, registry, freeze, _full_ref = make_inputs(tmp_path)

    record, summary = recorder.record_shadow(
        changed_paths=changed,
        registry=registry,
        freeze=freeze,
        phase_id="T16-P2d-S1",
        out_dir=tmp_path / "records",
        comparison_status="NOT_AVAILABLE_IN_ATTACHMENT_PROOF",
        repo_root=REPO_ROOT,
    )

    assert summary["exit_code"] == 0
    assert record["full_gate_ref"] is None
    assert (
        record["recommendation_full_comparison_status"]
        == "NOT_AVAILABLE_IN_ATTACHMENT_PROOF"
    )
    assert "comparison" not in record


def test_cli_compared_without_full_gate_ref_blocks_without_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    changed, registry, freeze, _full_ref = make_inputs(tmp_path)

    exit_code = recorder.run(
        [
            "record",
            "--changed-paths",
            str(changed),
            "--registry",
            str(registry),
            "--freeze",
            str(freeze),
            "--phase-id",
            "T16-P2d-S1",
            "--out-dir",
            str(tmp_path / "records"),
            "--comparison-status",
            "COMPARED",
        ]
    )

    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    summary = json.loads(lines[-1])
    assert exit_code == 2
    assert summary["exit_code"] == 2
    assert summary["record_path"] is None
    assert summary["selector_status"] == "PASS"
    assert "comparison_status" not in summary
    assert "comparison_status:compared_without_full_gate_ref" in summary["reasons"]
    assert not (tmp_path / "records").exists()


def test_cli_freeze_mismatch_omits_unobserved_statuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    changed, registry, _freeze, full_ref = make_inputs(tmp_path)
    bad_freeze = write_json(tmp_path, "bad-freeze.json", {"registry_sha256": "0" * 64})

    exit_code = recorder.run(
        [
            "record",
            "--changed-paths",
            str(changed),
            "--registry",
            str(registry),
            "--freeze",
            str(bad_freeze),
            "--phase-id",
            "T16-P2d-S1",
            "--out-dir",
            str(tmp_path / "records"),
            "--full-gate-ref",
            str(full_ref),
            "--comparison-status",
            "NOT_AVAILABLE_IN_ATTACHMENT_PROOF",
        ]
    )

    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    summary = json.loads(lines[-1])
    assert exit_code == 2
    assert summary["exit_code"] == 2
    assert summary["record_path"] is None
    assert "selector_status" not in summary
    assert "comparison_status" not in summary
    assert "freeze:registry_sha256_mismatch" in summary["reasons"]
    assert not (tmp_path / "records").exists()


def test_unknown_full_gate_ref_field_blocks(tmp_path: Path) -> None:
    changed, registry, freeze, full_ref = make_inputs(tmp_path)
    bad_ref = copy.deepcopy(FULL_GATE_REF)
    bad_ref["extra"] = "nope"
    full_ref.write_text(json.dumps(bad_ref), encoding="utf-8")

    with pytest.raises(recorder.RecorderInputError) as excinfo:
        recorder.record_shadow(
            changed_paths=changed,
            registry=registry,
            freeze=freeze,
            phase_id="T16-P2d-S1",
            out_dir=tmp_path / "records",
            full_gate_ref_path=full_ref,
            comparison_status="NOT_AVAILABLE_IN_ATTACHMENT_PROOF",
            repo_root=REPO_ROOT,
        )

    assert "full_gate_ref:unknown_field:extra" in excinfo.value.reasons
    assert not (tmp_path / "records").exists()


def test_record_is_deterministic_except_timestamp(tmp_path: Path) -> None:
    """Records match byte-for-byte except time-dependent timestamp and overhead."""
    changed, registry, freeze, full_ref = make_inputs(tmp_path)
    first, _ = recorder.record_shadow(
        changed_paths=changed,
        registry=registry,
        freeze=freeze,
        phase_id="T16-P2d-S1",
        out_dir=tmp_path / "records",
        full_gate_ref_path=full_ref,
        comparison_status="NOT_AVAILABLE_IN_ATTACHMENT_PROOF",
        repo_root=REPO_ROOT,
    )
    second, _ = recorder.record_shadow(
        changed_paths=changed,
        registry=registry,
        freeze=freeze,
        phase_id="T16-P2d-S1",
        out_dir=tmp_path / "records",
        full_gate_ref_path=full_ref,
        comparison_status="NOT_AVAILABLE_IN_ATTACHMENT_PROOF",
        repo_root=REPO_ROOT,
    )

    sanitized_first = sanitize(first)
    sanitized_second = sanitize(second)
    sanitized_first["overhead_s"] = 0.0
    sanitized_second["overhead_s"] = 0.0
    assert recorder.canonical_json(sanitized_first) == recorder.canonical_json(
        sanitized_second
    )


def test_cli_emits_last_line_summary_and_writes_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    changed, registry, freeze, full_ref = make_inputs(tmp_path)

    exit_code = recorder.run(
        [
            "record",
            "--changed-paths",
            str(changed),
            "--registry",
            str(registry),
            "--freeze",
            str(freeze),
            "--phase-id",
            "T16-P2d-S1",
            "--out-dir",
            str(tmp_path / "records"),
            "--full-gate-ref",
            str(full_ref),
            "--comparison-status",
            "NOT_AVAILABLE_IN_ATTACHMENT_PROOF",
        ]
    )

    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    summary = json.loads(lines[-1])
    assert exit_code == 0
    assert summary["exit_code"] == 0
    assert summary["selector_status"] == "PASS"
    assert Path(summary["record_path"]).exists()


def test_acceptance_01_same_pass_machine_agreement(tmp_path: Path) -> None:
    full_ref = copy.deepcopy(FULL_GATE_REF)
    full_ref["status"] = "PASS"
    full_ref["exit_code"] = 0

    record = record_compared(
        tmp_path,
        full_ref_doc=full_ref,
        focused_ref_docs=[focused_ref("shadow-focused", status="PASS", exit_code=0)],
    )

    comparison = cast(dict[str, Any], record["comparison"])
    assert comparison["agreement"] == "SAME_VERDICT_MACHINE"
    assert comparison["agreement_unknown_reason"] is None
    assert comparison["red_missed_machine"] is False


def test_acceptance_02_focused_pass_full_changes_requested_red_missed(
    tmp_path: Path,
) -> None:
    full_ref = copy.deepcopy(FULL_GATE_REF)
    full_ref["status"] = "CHANGES_REQUESTED"
    full_ref["exit_code"] = 1

    record = record_compared(
        tmp_path,
        full_ref_doc=full_ref,
        focused_ref_docs=[focused_ref("shadow-focused", status="PASS", exit_code=0)],
    )

    comparison = cast(dict[str, Any], record["comparison"])
    assert comparison["agreement"] == "DIFFERENT_VERDICT_MACHINE"
    assert comparison["red_missed_machine"] is True


def test_acceptance_03_focused_changes_requested_full_pass_not_red_missed(
    tmp_path: Path,
) -> None:
    full_ref = copy.deepcopy(FULL_GATE_REF)
    full_ref["status"] = "PASS"
    full_ref["exit_code"] = 0

    record = record_compared(
        tmp_path,
        full_ref_doc=full_ref,
        focused_ref_docs=[
            focused_ref("shadow-focused", status="CHANGES_REQUESTED", exit_code=1)
        ],
    )

    comparison = cast(dict[str, Any], record["comparison"])
    assert comparison["agreement"] == "DIFFERENT_VERDICT_MACHINE"
    assert comparison["red_missed_machine"] is False


def test_acceptance_04_missing_focused_ref_with_focused_group_is_unknown(
    tmp_path: Path,
) -> None:
    record = record_compared(tmp_path, focused_ref_docs=[])

    comparison = cast(dict[str, Any], record["comparison"])
    assert comparison["focused_status"] is None
    assert comparison["focused_exit"] is None
    assert comparison["agreement"] == "NOT_MACHINE_DETERMINED"
    assert comparison["agreement_unknown_reason"] == "focused_gate_ref_absent"
    assert comparison["red_missed_machine"] is None


def test_acceptance_05_no_focused_group_is_unknown(tmp_path: Path) -> None:
    registry_doc = registry_with_groups(
        [
            {
                "id": "shadow-full",
                "cwd": ".",
                "argv": ["python", "-m", "pytest"],
                "scope_class": "module-full",
            }
        ]
    )

    record = record_compared(tmp_path, registry_doc=registry_doc)

    comparison = cast(dict[str, Any], record["comparison"])
    assert comparison["focused_would_have_run"] is False
    assert comparison["agreement"] == "NOT_MACHINE_DETERMINED"
    assert (
        comparison["agreement_unknown_reason"] == "no_focused_group_in_recommendation"
    )
    assert comparison["red_missed_machine"] is None


def test_acceptance_06_partial_multi_group_coverage_is_incomplete(
    tmp_path: Path,
) -> None:
    registry_doc = registry_with_groups(
        [
            {
                "id": "a-focused",
                "cwd": ".",
                "argv": ["python", "-m", "pytest", "a"],
                "scope_class": "focused",
            },
            {
                "id": "b-focused",
                "cwd": ".",
                "argv": ["python", "-m", "pytest", "b"],
                "scope_class": "focused",
            },
        ]
    )

    record = record_compared(
        tmp_path,
        registry_doc=registry_doc,
        focused_ref_docs=[focused_ref("a-focused")],
    )

    comparison = cast(dict[str, Any], record["comparison"])
    assert comparison["agreement"] == "NOT_MACHINE_DETERMINED"
    assert comparison["agreement_unknown_reason"] == "focused_gate_ref_incomplete"
    assert comparison["red_missed_machine"] is None


def test_acceptance_07_multi_group_priority_composes_blocked(
    tmp_path: Path,
) -> None:
    registry_doc = registry_with_groups(
        [
            {
                "id": "b-focused",
                "cwd": ".",
                "argv": ["python", "-m", "pytest", "b"],
                "scope_class": "focused",
            },
            {
                "id": "a-focused",
                "cwd": ".",
                "argv": ["python", "-m", "pytest", "a"],
                "scope_class": "focused",
            },
        ]
    )

    record = record_compared(
        tmp_path,
        registry_doc=registry_doc,
        focused_ref_docs=[
            focused_ref("b-focused", status="PASS", exit_code=0),
            focused_ref("a-focused", status="BLOCKED", exit_code=2),
        ],
    )

    comparison = cast(dict[str, Any], record["comparison"])
    assert comparison["focused_status"] == "BLOCKED"
    assert comparison["focused_exit"] is None
    assert comparison["agreement"] == "SAME_VERDICT_MACHINE"


@pytest.mark.parametrize(
    ("refs", "reason"),
    [
        ([focused_ref("missing")], "focused_gate_ref:group_id_not_in_recommendation"),
        (
            [focused_ref("shadow-focused"), focused_ref("shadow-focused")],
            "focused_gate_ref:duplicate_group_id",
        ),
        (
            [
                {
                    key: value
                    for key, value in focused_ref("shadow-focused").items()
                    if key != "group_id"
                }
            ],
            "focused_gate_ref:missing_field:group_id",
        ),
    ],
)
def test_acceptance_08_invalid_group_refs_exit_2(
    tmp_path: Path, refs: list[dict[str, Any]], reason: str
) -> None:
    with pytest.raises(recorder.RecorderInputError) as excinfo:
        record_compared(tmp_path, focused_ref_docs=refs)

    assert reason in excinfo.value.reasons


def test_acceptance_09_focused_ref_without_full_ref_exit_2(tmp_path: Path) -> None:
    changed, registry, freeze, _full_ref = make_inputs(tmp_path)
    focused_path = write_json(tmp_path, "focused.json", focused_ref("shadow-focused"))

    with pytest.raises(recorder.RecorderInputError) as excinfo:
        recorder.record_shadow(
            changed_paths=changed,
            registry=registry,
            freeze=freeze,
            phase_id="T16-RECSCHEMA",
            out_dir=tmp_path / "records",
            focused_gate_ref_paths=[focused_path],
            comparison_status="COMPARED",
            repo_root=REPO_ROOT,
        )

    assert "focused_gate_ref:without_full_gate_ref" in excinfo.value.reasons


def test_acceptance_10_schema_v2_and_no_full_result(tmp_path: Path) -> None:
    record = record_compared(
        tmp_path,
        focused_ref_docs=[
            focused_ref(
                "shadow-focused", status="BLOCKED", exit_code=2, duration_s=1
            )
        ],
    )

    assert record["schema_version"] == 2
    assert record["focused_gate_refs"] == [
        focused_ref("shadow-focused", status="BLOCKED", exit_code=2, duration_s=1.0)
    ]
    comparison = cast(dict[str, Any], record["comparison"])
    assert "full_result" not in comparison


def test_acceptance_11_module_full_ref_never_counts_as_same_verdict(
    tmp_path: Path,
) -> None:
    registry_doc = registry_with_groups(
        [
            {
                "id": "shadow-full",
                "cwd": ".",
                "argv": ["python", "-m", "pytest"],
                "scope_class": "module-full",
            }
        ]
    )

    record = record_compared(
        tmp_path,
        registry_doc=registry_doc,
        focused_ref_docs=[focused_ref("shadow-full", status="BLOCKED", exit_code=2)],
    )

    comparison = cast(dict[str, Any], record["comparison"])
    assert comparison["focused_would_have_run"] is False
    assert comparison["agreement"] == "NOT_MACHINE_DETERMINED"
    assert (
        comparison["agreement_unknown_reason"] == "no_focused_group_in_recommendation"
    )
    assert comparison["red_missed_machine"] is None


def test_acceptance_12_mixed_focused_and_module_full_full_coverage_compares(
    tmp_path: Path,
) -> None:
    registry_doc = registry_with_groups(
        [
            {
                "id": "shadow-focused",
                "cwd": ".",
                "argv": ["python", "-m", "pytest", "focused"],
                "scope_class": "focused",
            },
            {
                "id": "shadow-full",
                "cwd": ".",
                "argv": ["python", "-m", "pytest"],
                "scope_class": "module-full",
            },
        ]
    )

    record = record_compared(
        tmp_path,
        registry_doc=registry_doc,
        focused_ref_docs=[
            focused_ref("shadow-focused", status="BLOCKED", exit_code=2),
            focused_ref("shadow-full", status="PASS", exit_code=0),
        ],
    )

    comparison = cast(dict[str, Any], record["comparison"])
    assert comparison["focused_would_have_run"] is True
    assert comparison["focused_status"] == "BLOCKED"
    assert comparison["agreement"] == "SAME_VERDICT_MACHINE"
    assert comparison["agreement_unknown_reason"] is None


def test_acceptance_12_mixed_module_full_does_not_mask_focused_red_miss(
    tmp_path: Path,
) -> None:
    registry_doc = registry_with_groups(
        [
            {
                "id": "shadow-focused",
                "cwd": ".",
                "argv": ["python", "-m", "pytest", "focused"],
                "scope_class": "focused",
            },
            {
                "id": "shadow-full",
                "cwd": ".",
                "argv": ["python", "-m", "pytest"],
                "scope_class": "module-full",
            },
        ]
    )

    record = record_compared(
        tmp_path,
        registry_doc=registry_doc,
        focused_ref_docs=[
            focused_ref("shadow-focused", status="PASS", exit_code=0),
            focused_ref("shadow-full", status="BLOCKED", exit_code=2),
        ],
    )

    comparison = cast(dict[str, Any], record["comparison"])
    assert comparison["focused_would_have_run"] is True
    assert comparison["focused_status"] == "PASS"
    assert comparison["focused_exit"] == 0
    assert comparison["agreement"] == "DIFFERENT_VERDICT_MACHINE"
    assert comparison["red_missed_machine"] is True


@pytest.mark.parametrize(
    "comparison_status",
    [
        "PENDING_PHASE_FULL_GATE",
        "NOT_AVAILABLE_IN_ATTACHMENT_PROOF",
        "BLOCKED_INPUT",
    ],
)
def test_acceptance_13_focused_ref_with_non_compared_status_exit_2(
    tmp_path: Path, comparison_status: str
) -> None:
    with pytest.raises(recorder.RecorderInputError) as excinfo:
        record_compared(
            tmp_path,
            focused_ref_docs=[focused_ref("shadow-focused")],
            comparison_status=comparison_status,
        )

    assert (
        f"focused_gate_ref:non_compared:{comparison_status}" in excinfo.value.reasons
    )


@pytest.mark.parametrize(
    ("mutator", "reason"),
    [
        (
            lambda doc: doc.__setitem__("extra", "nope"),
            "focused_gate_ref:unknown_field:extra",
        ),
        (lambda doc: doc.pop("status"), "focused_gate_ref:missing_field:status"),
        (lambda doc: doc.pop("exit_code"), "focused_gate_ref:missing_field:exit_code"),
        (
            lambda doc: doc.pop("duration_s"),
            "focused_gate_ref:missing_field:duration_s",
        ),
        (
            lambda doc: doc.pop("envelope_path"),
            "focused_gate_ref:missing_field:envelope_path",
        ),
        (
            lambda doc: doc.pop("stdout_path"),
            "focused_gate_ref:missing_field:stdout_path",
        ),
        (lambda doc: doc.pop("source"), "focused_gate_ref:missing_field:source"),
        (
            lambda doc: doc.__setitem__("status", "BAD"),
            "focused_gate_ref:invalid_status",
        ),
        (
            lambda doc: doc.__setitem__("exit_code", True),
            "focused_gate_ref:invalid_exit_code",
        ),
        (
            lambda doc: doc.__setitem__("source", "synthetic"),
            "focused_gate_ref:invalid_source",
        ),
    ],
)
def test_acceptance_14_focused_ref_schema_violations_exit_2(
    tmp_path: Path, mutator: Any, reason: str
) -> None:
    doc = focused_ref("shadow-focused")
    mutator(doc)

    with pytest.raises(recorder.RecorderInputError) as excinfo:
        record_compared(tmp_path, focused_ref_docs=[doc])

    assert reason in excinfo.value.reasons


def test_acceptance_15_known_agreement_has_null_unknown_reason(
    tmp_path: Path,
) -> None:
    record = record_compared(
        tmp_path,
        focused_ref_docs=[focused_ref("shadow-focused", status="BLOCKED", exit_code=2)],
    )

    comparison = cast(dict[str, Any], record["comparison"])
    assert comparison["agreement"] != "NOT_MACHINE_DETERMINED"
    assert comparison["agreement_unknown_reason"] is None


def test_acceptance_16_cli_accepts_repeated_focused_gate_ref(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    registry_doc = registry_with_groups(
        [
            {
                "id": "a-focused",
                "cwd": ".",
                "argv": ["python", "-m", "pytest", "a"],
                "scope_class": "focused",
            },
            {
                "id": "b-focused",
                "cwd": ".",
                "argv": ["python", "-m", "pytest", "b"],
                "scope_class": "focused",
            },
        ]
    )
    changed, registry, freeze, full_ref = make_inputs_with_registry(
        tmp_path, registry_doc
    )
    first_ref = write_json(tmp_path, "focused-a.json", focused_ref("a-focused"))
    second_ref = write_json(tmp_path, "focused-b.json", focused_ref("b-focused"))

    exit_code = recorder.run(
        [
            "record",
            "--changed-paths",
            str(changed),
            "--registry",
            str(registry),
            "--freeze",
            str(freeze),
            "--phase-id",
            "T16-RECSCHEMA",
            "--out-dir",
            str(tmp_path / "records"),
            "--full-gate-ref",
            str(full_ref),
            "--focused-gate-ref",
            str(first_ref),
            "--focused-gate-ref",
            str(second_ref),
            "--comparison-status",
            "COMPARED",
        ]
    )

    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    summary = json.loads(lines[-1])
    record = json.loads(Path(summary["record_path"]).read_text(encoding="utf-8"))
    assert exit_code == 0
    assert len(record["focused_gate_refs"]) == 2
