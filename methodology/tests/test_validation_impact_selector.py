"""T16-P2a validation impact selector 계약 테스트.

판별력 축(각 테스트가 잡는 결함):
- calibration shape 3종(ACP/Methodology/ZTR 형태): 기본 exact/prefix 매핑 회귀.
- permutation: rule/group/paths 입력 순서 의존 구현.
- segment-aware prefix: naive ``str.startswith`` 오매칭(``methodology`` vs ``methodology2``).
- fail-closed 배터리: 위험 입력의 자동 교정·부분 추천 PASS·silent skip.
- side-effect 가드: 테스트 실행/Git/network 호출이 selector에 끼어드는 회귀.
- R5 출력 계약: stdout 마지막 줄 canonical JSON + enum/exit 분기 붕괴.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

METHODOLOGY_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = METHODOLOGY_ROOT / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import validation_impact_selector as selector  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "validation_impact_selector"
NOT_CLAIMED = ["phase_pass", "selector_recall", "production_safety", "token_saving"]

VALID_CHANGED: dict[str, Any] = {
    "schema_version": 1,
    "paths": ["methodology/tools/workflow_doctor.py"],
}
VALID_REGISTRY: dict[str, Any] = {
    "schema_version": 1,
    "groups": [
        {
            "id": "methodology-tests",
            "cwd": ".",
            "argv": [
                "runtimes/ztr/.venv/Scripts/python.exe",
                "-m",
                "pytest",
                "-q",
                "methodology/tests",
            ],
            "scope_class": "module-full",
        }
    ],
    "rules": [
        {
            "id": "methodology-prefix",
            "match": {"kind": "prefix", "value": "methodology/"},
            "groups": ["methodology-tests"],
        }
    ],
}


def fixture(*parts: str) -> Path:
    return FIXTURES.joinpath(*parts)


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(tmp_path: Path, name: str, doc: Any) -> Path:
    target = tmp_path / name
    target.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return target


def run_case(
    tmp_path: Path,
    changed: Any = None,
    registry: Any = None,
) -> tuple[dict[str, Any], int]:
    changed_doc = VALID_CHANGED if changed is None else changed
    registry_doc = VALID_REGISTRY if registry is None else registry
    changed_file = write_json(tmp_path, "changed-paths.json", changed_doc)
    registry_file = write_json(tmp_path, "registry.json", registry_doc)
    return selector.run_selector(changed_file, registry_file)


def assert_blocked_2(result: dict[str, Any], exit_code: int) -> None:
    assert exit_code == 2
    assert result["status"] == "BLOCKED"
    assert result["exit_code"] == 2
    assert result["mode"] == "SHADOW_RECOMMENDATION"
    assert result["matches"] == []
    assert result["recommended_groups"] == []
    assert result["reasons"], "BLOCKED 결과에는 구조화 사유가 있어야 한다"
    assert result["not_claimed"] == NOT_CLAIMED


# ---------------------------------------------------------------------------
# calibration shape 3종 — ACP / Methodology / ZTR '형태' fixture (실HR 결과 아님)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape", ["acp_shape", "methodology_shape", "ztr_shape"])
def test_calibration_shape_fixture_pass(shape: str) -> None:
    changed_file = fixture(shape, "changed-paths.json")
    registry_file = fixture(shape, "registry.json")
    expected = json.loads(fixture(shape, "expected.json").read_text(encoding="utf-8"))

    result, exit_code = selector.run_selector(changed_file, registry_file)

    assert exit_code == 0
    assert result["status"] == "PASS"
    assert result["exit_code"] == 0
    assert result["mode"] == "SHADOW_RECOMMENDATION"
    assert result["matches"] == expected["matches"]
    assert result["recommended_groups"] == expected["recommended_groups"]
    assert result["changed_paths_sha256"] == sha256_of(changed_file)
    assert result["registry_sha256"] == sha256_of(registry_file)
    assert result["not_claimed"] == NOT_CLAIMED


def test_overlap_union_is_stable_and_deduplicated(tmp_path: Path) -> None:
    """multi-rule overlap: rule_ids/group_ids는 정렬된 unique union이어야 한다."""
    registry = copy.deepcopy(VALID_REGISTRY)
    registry["groups"].append(
        {
            "id": "doctor-focused",
            "cwd": ".",
            "argv": ["runtimes/ztr/.venv/Scripts/python.exe", "-m", "pytest", "-q"],
            "scope_class": "focused",
        }
    )
    registry["rules"].append(
        {
            "id": "doctor-exact",
            "match": {
                "kind": "exact",
                "value": "methodology/tools/workflow_doctor.py",
            },
            "groups": ["doctor-focused", "methodology-tests"],
        }
    )
    result, exit_code = run_case(tmp_path, registry=registry)

    assert exit_code == 0
    assert result["matches"] == [
        {
            "path": "methodology/tools/workflow_doctor.py",
            "rule_ids": ["doctor-exact", "methodology-prefix"],
            "group_ids": ["doctor-focused", "methodology-tests"],
        }
    ]
    recommended_ids = [group["id"] for group in result["recommended_groups"]]
    assert recommended_ids == ["doctor-focused", "methodology-tests"]
    assert len(recommended_ids) == len(set(recommended_ids))


def test_prefix_match_is_segment_aware(tmp_path: Path) -> None:
    """trailing '/' 없는 prefix가 naive startswith로 오매칭되면 안 된다."""
    registry = copy.deepcopy(VALID_REGISTRY)
    registry["rules"][0]["match"]["value"] = "methodology"

    hit, hit_code = run_case(
        tmp_path, changed={"schema_version": 1, "paths": ["methodology/x.py"]},
        registry=registry,
    )
    assert hit_code == 0
    assert hit["matches"][0]["rule_ids"] == ["methodology-prefix"]

    miss, miss_code = run_case(
        tmp_path, changed={"schema_version": 1, "paths": ["methodology2/x.py"]},
        registry=registry,
    )
    assert_blocked_2(miss, miss_code)
    assert any(
        reason == "selector:unmatched_path:methodology2/x.py"
        for reason in miss["reasons"]
    )


# ---------------------------------------------------------------------------
# 순서 permutation — semantic equality, hash는 raw bytes 차이만큼 달라진다
# ---------------------------------------------------------------------------


def test_registry_permutation_semantic_equality() -> None:
    changed_file = fixture("permutation", "changed-paths.json")
    registry_a = fixture("permutation", "registry-a.json")
    registry_b = fixture("permutation", "registry-b.json")

    result_a, code_a = selector.run_selector(changed_file, registry_a)
    result_b, code_b = selector.run_selector(changed_file, registry_b)

    assert (code_a, code_b) == (0, 0)
    assert result_a["matches"] == result_b["matches"]
    assert result_a["recommended_groups"] == result_b["recommended_groups"]
    assert result_a["changed_paths_sha256"] == result_b["changed_paths_sha256"]
    # raw registry bytes가 다르므로 provenance hash 차이는 정상이다.
    assert result_a["registry_sha256"] != result_b["registry_sha256"]


def test_changed_paths_order_permutation_semantic_equality(tmp_path: Path) -> None:
    registry = copy.deepcopy(VALID_REGISTRY)
    paths = ["methodology/a.py", "methodology/b.py"]
    forward, code_f = run_case(
        tmp_path, changed={"schema_version": 1, "paths": paths}, registry=registry
    )
    backward, code_b = run_case(
        tmp_path,
        changed={"schema_version": 1, "paths": list(reversed(paths))},
        registry=registry,
    )
    assert (code_f, code_b) == (0, 0)
    assert forward["matches"] == backward["matches"]
    assert forward["recommended_groups"] == backward["recommended_groups"]
    assert forward["changed_paths_sha256"] != backward["changed_paths_sha256"]


# ---------------------------------------------------------------------------
# fail-closed 배터리 — 전부 BLOCKED/2, 부분 추천 PASS 금지
# ---------------------------------------------------------------------------


def test_partial_match_is_blocked_not_partial_pass(tmp_path: Path) -> None:
    result, exit_code = run_case(
        tmp_path,
        changed={
            "schema_version": 1,
            "paths": [
                "methodology/tools/workflow_doctor.py",
                "unmapped/zone/file.py",
            ],
        },
    )
    assert_blocked_2(result, exit_code)
    assert "selector:unmatched_path:unmapped/zone/file.py" in result["reasons"]


def test_empty_paths_blocked(tmp_path: Path) -> None:
    result, exit_code = run_case(
        tmp_path, changed={"schema_version": 1, "paths": []}
    )
    assert_blocked_2(result, exit_code)
    assert "changed_paths:paths_empty" in result["reasons"]


def test_duplicate_changed_path_blocked(tmp_path: Path) -> None:
    result, exit_code = run_case(
        tmp_path,
        changed={
            "schema_version": 1,
            "paths": ["methodology/a.py", "methodology/a.py"],
        },
    )
    assert_blocked_2(result, exit_code)
    assert "changed_paths:duplicate_path:methodology/a.py" in result["reasons"]


@pytest.mark.parametrize(
    "bad_path",
    [
        "/etc/passwd",
        "C:/work/x.py",
        "c:relative.py",
        "a/../b.py",
        "..",
        ".",
        "./a.py",
        "a\\b.py",
        "",
        "a//b.py",
        "a/b.py/",
    ],
)
def test_dangerous_changed_path_blocked(tmp_path: Path, bad_path: str) -> None:
    result, exit_code = run_case(
        tmp_path, changed={"schema_version": 1, "paths": [bad_path]}
    )
    assert_blocked_2(result, exit_code)
    assert any(
        reason.startswith("changed_paths:invalid_path:") for reason in result["reasons"]
    )


def test_non_string_changed_path_blocked(tmp_path: Path) -> None:
    result, exit_code = run_case(
        tmp_path, changed={"schema_version": 1, "paths": [42]}
    )
    assert_blocked_2(result, exit_code)
    assert any(
        reason.startswith("changed_paths:invalid_path:not_string")
        for reason in result["reasons"]
    )


@pytest.mark.parametrize("version", [None, 0, 2, "1", True])
def test_changed_paths_schema_version_gate(tmp_path: Path, version: Any) -> None:
    changed = copy.deepcopy(VALID_CHANGED)
    if version is None:
        del changed["schema_version"]
    else:
        changed["schema_version"] = version
    result, exit_code = run_case(tmp_path, changed=changed)
    assert_blocked_2(result, exit_code)
    assert any(
        reason
        in (
            "changed_paths:missing_field:schema_version",
            "changed_paths:unsupported_schema_version",
        )
        for reason in result["reasons"]
    )


@pytest.mark.parametrize("version", [None, 0, 2, "1", True])
def test_registry_schema_version_gate(tmp_path: Path, version: Any) -> None:
    registry = copy.deepcopy(VALID_REGISTRY)
    if version is None:
        del registry["schema_version"]
    else:
        registry["schema_version"] = version
    result, exit_code = run_case(tmp_path, registry=registry)
    assert_blocked_2(result, exit_code)
    assert any(
        reason
        in (
            "registry:missing_field:schema_version",
            "registry:unsupported_schema_version",
        )
        for reason in result["reasons"]
    )


def test_malformed_changed_paths_fixture_blocked(tmp_path: Path) -> None:
    registry_file = write_json(tmp_path, "registry.json", VALID_REGISTRY)
    result, exit_code = selector.run_selector(
        fixture("malformed", "changed-paths-truncated.json"), registry_file
    )
    assert_blocked_2(result, exit_code)
    assert "changed_paths:malformed_json" in result["reasons"]


def test_malformed_registry_blocked(tmp_path: Path) -> None:
    changed_file = write_json(tmp_path, "changed-paths.json", VALID_CHANGED)
    registry_file = tmp_path / "broken-registry.json"
    registry_file.write_bytes(b'{"schema_version": 1, "groups": [')
    result, exit_code = selector.run_selector(changed_file, registry_file)
    assert_blocked_2(result, exit_code)
    assert "registry:malformed_json" in result["reasons"]


def test_missing_changed_paths_file_blocked(tmp_path: Path) -> None:
    registry_file = write_json(tmp_path, "registry.json", VALID_REGISTRY)
    result, exit_code = selector.run_selector(
        tmp_path / "no-such-changed.json", registry_file
    )
    assert_blocked_2(result, exit_code)
    assert any(
        reason.startswith("changed_paths:unreadable") for reason in result["reasons"]
    )
    assert result["changed_paths_sha256"] is None
    assert result["registry_sha256"] == sha256_of(registry_file)


def test_changed_paths_not_object_blocked(tmp_path: Path) -> None:
    result, exit_code = run_case(tmp_path, changed=["methodology/a.py"])
    assert_blocked_2(result, exit_code)
    assert "changed_paths:not_object" in result["reasons"]


def test_changed_paths_unknown_field_blocked(tmp_path: Path) -> None:
    changed = copy.deepcopy(VALID_CHANGED)
    changed["extra"] = "nope"
    result, exit_code = run_case(tmp_path, changed=changed)
    assert_blocked_2(result, exit_code)
    assert "changed_paths:unknown_field:extra" in result["reasons"]


def test_unknown_match_kind_blocked(tmp_path: Path) -> None:
    registry = copy.deepcopy(VALID_REGISTRY)
    registry["rules"][0]["match"]["kind"] = "glob"
    result, exit_code = run_case(tmp_path, registry=registry)
    assert_blocked_2(result, exit_code)
    assert any(
        reason.startswith("registry:unknown_match_kind") for reason in result["reasons"]
    )


def test_unknown_group_ref_blocked(tmp_path: Path) -> None:
    registry = copy.deepcopy(VALID_REGISTRY)
    registry["rules"][0]["groups"] = ["ghost-group"]
    result, exit_code = run_case(tmp_path, registry=registry)
    assert_blocked_2(result, exit_code)
    assert any(
        reason.startswith("registry:unknown_group_ref") for reason in result["reasons"]
    )


def test_duplicate_group_id_blocked(tmp_path: Path) -> None:
    registry = copy.deepcopy(VALID_REGISTRY)
    registry["groups"].append(copy.deepcopy(registry["groups"][0]))
    result, exit_code = run_case(tmp_path, registry=registry)
    assert_blocked_2(result, exit_code)
    assert "registry:duplicate_group_id:methodology-tests" in result["reasons"]


def test_duplicate_rule_id_blocked(tmp_path: Path) -> None:
    registry = copy.deepcopy(VALID_REGISTRY)
    registry["rules"].append(copy.deepcopy(registry["rules"][0]))
    result, exit_code = run_case(tmp_path, registry=registry)
    assert_blocked_2(result, exit_code)
    assert "registry:duplicate_rule_id:methodology-prefix" in result["reasons"]


def test_duplicate_group_ref_in_rule_blocked(tmp_path: Path) -> None:
    registry = copy.deepcopy(VALID_REGISTRY)
    registry["rules"][0]["groups"] = ["methodology-tests", "methodology-tests"]
    result, exit_code = run_case(tmp_path, registry=registry)
    assert_blocked_2(result, exit_code)
    assert any(
        reason.startswith("registry:rule_duplicate_group_ref")
        for reason in result["reasons"]
    )


@pytest.mark.parametrize(
    "argv",
    [[], ["ok", ""], "pytest -q methodology/tests", [["nested"]]],
)
def test_invalid_argv_blocked(tmp_path: Path, argv: Any) -> None:
    registry = copy.deepcopy(VALID_REGISTRY)
    registry["groups"][0]["argv"] = argv
    result, exit_code = run_case(tmp_path, registry=registry)
    assert_blocked_2(result, exit_code)
    assert any(
        reason.startswith("registry:group_invalid_argv") for reason in result["reasons"]
    )


def test_unknown_scope_class_blocked(tmp_path: Path) -> None:
    registry = copy.deepcopy(VALID_REGISTRY)
    registry["groups"][0]["scope_class"] = "everything"
    result, exit_code = run_case(tmp_path, registry=registry)
    assert_blocked_2(result, exit_code)
    assert any(
        reason.startswith("registry:group_invalid_scope_class")
        for reason in result["reasons"]
    )


@pytest.mark.parametrize("cwd", ["C:/repo", "/abs", "a/../b", "a\\b", ""])
def test_invalid_group_cwd_blocked(tmp_path: Path, cwd: str) -> None:
    registry = copy.deepcopy(VALID_REGISTRY)
    registry["groups"][0]["cwd"] = cwd
    result, exit_code = run_case(tmp_path, registry=registry)
    assert_blocked_2(result, exit_code)
    assert any(
        reason.startswith("registry:group_invalid_cwd") for reason in result["reasons"]
    )


def test_exact_rule_trailing_slash_blocked(tmp_path: Path) -> None:
    registry = copy.deepcopy(VALID_REGISTRY)
    registry["rules"][0]["match"] = {"kind": "exact", "value": "methodology/"}
    result, exit_code = run_case(tmp_path, registry=registry)
    assert_blocked_2(result, exit_code)
    assert any(
        reason.startswith("registry:rule_invalid_match_value")
        for reason in result["reasons"]
    )


def test_registry_unknown_top_field_blocked(tmp_path: Path) -> None:
    registry = copy.deepcopy(VALID_REGISTRY)
    registry["notes"] = "draft"
    result, exit_code = run_case(tmp_path, registry=registry)
    assert_blocked_2(result, exit_code)
    assert "registry:unknown_field:notes" in result["reasons"]


# ---------------------------------------------------------------------------
# side-effect 부재 증명 — 실행/Git/network/LLM 호출 0
# ---------------------------------------------------------------------------


def test_selector_performs_no_execution_git_or_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("selector가 금지된 side effect를 시도했다")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "check_output", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    result, exit_code = selector.run_selector(
        fixture("acp_shape", "changed-paths.json"),
        fixture("acp_shape", "registry.json"),
    )
    assert exit_code == 0
    assert result["status"] == "PASS"


def test_selector_source_has_no_side_effect_imports() -> None:
    source = (TOOLS_DIR / "validation_impact_selector.py").read_text(encoding="utf-8")
    forbidden_fragments = [
        "import subprocess",
        "import socket",
        "import urllib",
        "import http",
        "import shutil",
        "os.system",
        "os.popen",
        "Popen",
        ".write_text(",
        ".write_bytes(",
        ".unlink(",
        ".mkdir(",
        '"git"',
        "'git'",
    ]
    for fragment in forbidden_fragments:
        assert fragment not in source, f"selector 소스에 금지 조각: {fragment}"


# ---------------------------------------------------------------------------
# R5 출력 계약 — stdout 마지막 줄 canonical JSON, enum/exit 분기만
# ---------------------------------------------------------------------------


def test_cli_pass_emits_single_canonical_json_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = selector.run(
        [
            "recommend",
            "--changed-paths",
            str(fixture("acp_shape", "changed-paths.json")),
            "--registry",
            str(fixture("acp_shape", "registry.json")),
        ]
    )
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line]

    assert exit_code == 0
    assert len(lines) == 1
    payload = json.loads(lines[-1])
    assert lines[-1] == selector.canonical_json(payload)
    assert payload["status"] == "PASS"
    assert payload["exit_code"] == 0
    assert payload["mode"] == "SHADOW_RECOMMENDATION"
    assert payload["not_claimed"] == NOT_CLAIMED


def test_cli_blocked_emits_canonical_json_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    changed_file = write_json(
        tmp_path, "changed-paths.json", {"schema_version": 1, "paths": []}
    )
    registry_file = write_json(tmp_path, "registry.json", VALID_REGISTRY)
    exit_code = selector.run(
        [
            "recommend",
            "--changed-paths",
            str(changed_file),
            "--registry",
            str(registry_file),
        ]
    )
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line]

    assert exit_code == 2
    payload = json.loads(lines[-1])
    assert lines[-1] == selector.canonical_json(payload)
    assert payload["status"] == "BLOCKED"
    assert payload["exit_code"] == 2


def test_cli_usage_error_blocked_2(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = selector.run(["recommend"])
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line]

    assert exit_code == 2
    payload = json.loads(lines[-1])
    assert payload["status"] == "BLOCKED"
    assert payload["exit_code"] == 2
    assert "usage_error" in payload["reasons"]


def test_internal_error_maps_to_blocked_70(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(changed_paths_file: Path, registry_file: Path) -> Any:
        raise RuntimeError("예기치 않은 내부 오류")

    monkeypatch.setattr(selector, "run_selector", boom)
    exit_code = selector.run(
        [
            "recommend",
            "--changed-paths",
            "x.json",
            "--registry",
            "y.json",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out.splitlines()[-1])

    assert exit_code == 70
    assert payload["status"] == "BLOCKED"
    assert payload["exit_code"] == 70
    assert payload["reasons"] == ["internal_error:RuntimeError"]


def test_selector_never_emits_changes_requested(tmp_path: Path) -> None:
    """P2a는 CHANGES_REQUESTED/1을 생성하지 않는다 — PASS/0 아니면 BLOCKED/2·70."""
    ok_result, ok_code = run_case(tmp_path)
    bad_result, bad_code = run_case(
        tmp_path, changed={"schema_version": 1, "paths": []}
    )
    assert (ok_result["status"], ok_code) == ("PASS", 0)
    assert (bad_result["status"], bad_code) == ("BLOCKED", 2)
    assert ok_code != 1 and bad_code != 1


# ---------------------------------------------------------------------------
# bootstrap registry / schema 계약 정합
# ---------------------------------------------------------------------------


def test_bootstrap_registry_is_structurally_valid() -> None:
    """live registry가 구조적으로 유효 — calibration 주장 아님.

    P2b corrective에서 내용 결합 제거(B1): 특정 group id·경로 매핑 등
    registry 내용 기대는 calibration 테스트
    (``test_validation_impact_selector_calibration.py``) 소유다. 여기서는
    schema 정합·id 유일성·group ref 실재·selector 로드/파싱만 잠근다.
    jsonschema 미설치 환경이므로 schema 문서에서 유도한 구조 제약을 직접
    적용한다(schema 자체가 enforcement 정본을 selector 검증으로 선언).
    """
    registry_file = METHODOLOGY_ROOT / "config" / "validation-impact-registry.json"
    schema = json.loads(
        (
            METHODOLOGY_ROOT / "artifacts" / "validation-impact-registry.schema.json"
        ).read_text(encoding="utf-8")
    )
    doc = json.loads(registry_file.read_text(encoding="utf-8"))

    # 1) schema 검증 — required + additionalProperties(false) + const/enum.
    assert set(doc) == set(schema["required"])
    assert doc["schema_version"] == schema["properties"]["schema_version"]["const"]

    group_schema = schema["properties"]["groups"]["items"]
    scope_enum = set(group_schema["properties"]["scope_class"]["enum"])
    for group in doc["groups"]:
        assert set(group) == set(group_schema["required"])
        assert isinstance(group["id"], str) and group["id"]
        assert isinstance(group["cwd"], str) and group["cwd"]
        assert isinstance(group["argv"], list) and group["argv"]
        assert all(isinstance(arg, str) and arg for arg in group["argv"])
        assert group["scope_class"] in scope_enum

    rule_schema = schema["properties"]["rules"]["items"]
    match_schema = rule_schema["properties"]["match"]
    kind_enum = set(match_schema["properties"]["kind"]["enum"])
    for rule in doc["rules"]:
        assert set(rule) == set(rule_schema["required"])
        assert isinstance(rule["id"], str) and rule["id"]
        assert set(rule["match"]) == set(match_schema["required"])
        assert rule["match"]["kind"] in kind_enum
        assert isinstance(rule["match"]["value"], str) and rule["match"]["value"]
        assert isinstance(rule["groups"], list) and rule["groups"]
        assert all(isinstance(ref, str) and ref for ref in rule["groups"])

    # 2) rule/group id 유일성.
    group_ids = [group["id"] for group in doc["groups"]]
    rule_ids = [rule["id"] for rule in doc["rules"]]
    assert len(group_ids) == len(set(group_ids))
    assert len(rule_ids) == len(set(rule_ids))

    # 3) 모든 rule의 group ref 실재.
    known_group_ids = set(group_ids)
    for rule in doc["rules"]:
        assert set(rule["groups"]) <= known_group_ids

    # 4) selector가 registry를 로드·파싱 가능 — 내용 기대 없음.
    groups, rules = selector.validate_registry(doc)
    assert len(groups) == len(doc["groups"])
    assert len(rules) == len(doc["rules"])


def test_schema_contract_in_sync_with_selector_enums() -> None:
    schema = json.loads(
        (
            METHODOLOGY_ROOT / "artifacts" / "validation-impact-registry.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema_version"]["const"] == selector.SCHEMA_VERSION

    scope_enum = schema["properties"]["groups"]["items"]["properties"]["scope_class"][
        "enum"
    ]
    assert set(scope_enum) == set(selector.SCOPE_CLASSES)

    kind_enum = schema["properties"]["rules"]["items"]["properties"]["match"][
        "properties"
    ]["kind"]["enum"]
    assert set(kind_enum) == set(selector.MATCH_KINDS)

    group_fields = schema["properties"]["groups"]["items"]["required"]
    assert set(group_fields) == set(selector.GROUP_FIELDS)
    rule_fields = schema["properties"]["rules"]["items"]["required"]
    assert set(rule_fields) == set(selector.RULE_FIELDS)
