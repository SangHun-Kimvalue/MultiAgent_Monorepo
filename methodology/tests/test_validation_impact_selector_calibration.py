"""T16-P2b2 calibration + registry freeze v2 계약 테스트.

잠그는 것(P2b 계약 승계 + P2b2 freeze v2 — planner-design.md §5):
- fixture changed-paths == ``git diff --name-only <parent>..<fix>`` 재실측 (D1).
- 각 case selector 추천 union == expected exact match, focused ≥1 +
  gold RED 테스트 파일 포함 (D3 · §5-2). expected fixture는 손으로 짓지 않고
  selector 실출력에서 유도해 고정한다(S2 결정론 스크립트 산출).
- freeze ``registry_sha256``(raw bytes primary) == calibrated registry 실제
  워킹트리 bytes 해시, ``registry_sha256_lf`` == LF 정규화 UTF-8 bytes 해시,
  ``hash_canonicalization`` == "lf-normalized-utf8" (D5 · §5-5 freeze v2).
- freeze ``contract_sha256`` == frozen_at·contract_sha256 두 필드 제외
  canonical JSON(sort_keys·UTF-8) 재계산 해시 (D5 — P2b 규약 승계).
- derivation 메타(강등 임계 0.5 = Planner 확정값·module-spec 해시) 동결 (D5).
- prefix 규칙은 module-full 전용(over-selection 허용 class), prefix 하위
  exact 규칙은 focused 정제일 때만 허용, repository-full 금지 (D2).

calibration ≠ promotion (D6): 이 테스트의 GREEN은 recommendation 재현성과
freeze 무결성만 뜻한다. selector recall·안전성·절감은 NOT CLAIMED이며,
expected fixture의 ``not_claimed: ["promotion_evidence"]``가 이를 고정한다.
selector는 import하지 않고 CLI 계약(subprocess) 그대로 검증한다.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "validation_impact_selector_calibration"
)
SELECTOR = REPO_ROOT / "methodology" / "tools" / "validation_impact_selector.py"
REGISTRY = REPO_ROOT / "methodology" / "config" / "validation-impact-registry.json"
FREEZE = (
    REPO_ROOT / "methodology" / "artifacts" / "validation-impact-registry.freeze.json"
)
MODULE_SPEC = (
    REPO_ROOT
    / "methodology"
    / "tests"
    / "fixtures"
    / "registry_derivation"
    / "module-spec.json"
)
CASES = ("mam-f852-env", "t14-s6", "ztr-4ffe-verdict")
DOCS_PREFIXES = ("methodology/docs/", "runtimes/acp/docs/", "runtimes/ztr/docs/")
# Planner 확정값(2026-08-16) — planner-design.md §3 강등 임계 파라미터.
DEMOTION_THRESHOLD = 0.5
SHA256_HEX_LEN = 64


def load_json(path: Path) -> dict[str, Any]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), f"{path}: JSON object가 아니다"
    return doc


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def lf_sha256_of(path: Path) -> str:
    """freeze v2 부가 해시 — CRLF -> LF 정규화 후 UTF-8 bytes SHA-256."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def canonical_contract_sha256(freeze_doc: dict[str, Any]) -> str:
    """frozen_at·contract_sha256 제외 canonical JSON(sort_keys·UTF-8) 해시."""
    body = {
        key: value
        for key, value in freeze_doc.items()
        if key not in ("frozen_at", "contract_sha256")
    }
    canonical = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def run_selector_cli(changed_paths: Path) -> tuple[dict[str, Any], int]:
    completed = subprocess.run(
        [
            sys.executable,
            str(SELECTOR),
            "recommend",
            "--changed-paths",
            str(changed_paths),
            "--registry",
            str(REGISTRY),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert lines, "selector stdout 마지막 줄에는 canonical JSON이 있어야 한다"
    doc = json.loads(lines[-1])
    assert isinstance(doc, dict)
    return doc, completed.returncode


def git_changed_paths(parent: str, fix: str) -> list[str]:
    completed = subprocess.run(
        ["git", "diff", "--name-only", f"{parent}..{fix}"],
        cwd=REPO_ROOT,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return [line for line in completed.stdout.splitlines() if line]


def focused_covered_files(result: dict[str, Any]) -> set[str]:
    """focused group argv가 커버하는 테스트 파일의 repo-relative 경로 집합."""
    covered: set[str] = set()
    for group in result["recommended_groups"]:
        if group["scope_class"] != "focused":
            continue
        for arg in group["argv"]:
            target = str(arg).split("::", 1)[0]
            if not target.endswith((".py", ".mjs")):
                continue
            cwd = str(group["cwd"])
            covered.add(target if cwd == "." else f"{cwd}/{target}")
    return covered


# ---------------------------------------------------------------------------
# D1 — fixture는 git 실측 그대로 (가공·누락 금지)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES)
def test_fixture_paths_equal_git_re_measure(case: str) -> None:
    fixture = load_json(FIXTURES / case / "changed-paths.json")
    expected = load_json(FIXTURES / case / "expected.json")
    assert fixture["schema_version"] == 1
    assert fixture["paths"] == git_changed_paths(expected["parent"], expected["fix"])


# ---------------------------------------------------------------------------
# D3 — 기대 추천 = 결정론 union의 exact match + gold RED 포함 (§5-2 회귀)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES)
def test_selector_union_exact_match(case: str) -> None:
    expected = load_json(FIXTURES / case / "expected.json")
    result, exit_code = run_selector_cli(FIXTURES / case / "changed-paths.json")

    assert exit_code == 0
    assert result["status"] == "PASS"
    assert result["exit_code"] == 0
    recommended_ids = [group["id"] for group in result["recommended_groups"]]
    assert recommended_ids == expected["recommended_groups"]

    # D6: calibration ≠ promotion — expected fixture가 구조화로 고정한다.
    assert expected["not_claimed"] == ["promotion_evidence"]

    # §5-2 (c): focused-class group ≥1 — 강등으로 focused가 소멸하면 실패.
    scope_by_id = {
        group["id"]: group["scope_class"] for group in result["recommended_groups"]
    }
    focused_ids = [gid for gid, scope in scope_by_id.items() if scope == "focused"]
    assert focused_ids == expected["focused_groups"]
    assert focused_ids, f"{case}: focused group이 소멸했다 (§5-2 (c) 위반)"
    assert "module-full" in scope_by_id.values()

    # §5-2 (b): focused set이 실측 gold RED 테스트 파일을 포함한다 —
    # regression-only 소각 fixture이며 일반화 주장에 쓰지 않는다.
    gold = set(expected["gold_red_test_files"])
    covered = focused_covered_files(result)
    assert gold.issubset(covered), (
        f"{case}: focused set이 gold RED 테스트 파일을 포함하지 않는다 — "
        f"missing={sorted(gold - covered)}"
    )

    # selector가 읽은 registry raw bytes == freeze가 동결한 raw bytes.
    freeze = load_json(FREEZE)
    assert result["registry_sha256"] == freeze["registry_sha256"]


# ---------------------------------------------------------------------------
# D5 — freeze v2 무결성 (raw primary + LF 정규화 부가 해시 + derivation 메타)
# ---------------------------------------------------------------------------


def test_freeze_registry_sha256_matches_raw_bytes() -> None:
    freeze = load_json(FREEZE)
    assert freeze["schema_version"] == 1
    assert (
        freeze["registry_path"] == "methodology/config/validation-impact-registry.json"
    )
    # raw bytes primary 유지(이월 ③ 처분 — 기존 대조 계약 후방 호환).
    assert freeze["registry_sha256"] == sha256_of(REGISTRY)
    # freeze v2 부가 필드: LF 정규화 해시 + canonicalization 메타.
    assert freeze["registry_sha256_lf"] == lf_sha256_of(REGISTRY)
    assert freeze["hash_canonicalization"] == "lf-normalized-utf8"


def test_freeze_contract_sha256_matches_canonical_recomputation() -> None:
    freeze = load_json(FREEZE)
    assert freeze["contract_sha256"] == canonical_contract_sha256(freeze)
    frozen_at = freeze["frozen_at"]
    assert isinstance(frozen_at, str)
    assert frozen_at.endswith("Z")


def test_freeze_derivation_meta_and_argv_authority() -> None:
    """v2: per-group argv_template 중복 등재 폐지 — argv 정본은 registry 자체."""
    freeze = load_json(FREEZE)
    derivation = freeze["derivation"]
    assert derivation["tool"] == "methodology/tools/registry_derivation.py"
    assert (REPO_ROOT / str(derivation["tool"])).is_file()
    assert derivation["module_spec_sha256"] == sha256_of(MODULE_SPEC)
    assert derivation["demotion_threshold"] == DEMOTION_THRESHOLD
    for field in ("approved_draft_sha256", "derivation_sidecar_sha256"):
        value = derivation[field]
        assert isinstance(value, str) and len(value) == SHA256_HEX_LEN
        assert all(ch in "0123456789abcdef" for ch in value)

    contract = freeze["command_contract"]
    assert "groups" not in contract, "v2에서 per-group argv_template 등재는 폐지됐다"
    assert str(contract["argv_authority"]).startswith(
        "methodology/config/validation-impact-registry.json#groups"
    )
    # volatile arg 배제는 argv 정본인 registry group 전건에서 실측 검증한다.
    registry = load_json(REGISTRY)
    for group in registry["groups"]:
        assert not any(
            str(arg).startswith("--junitxml") for arg in group["argv"]
        ), group["id"]


def test_freeze_calibration_cases_cover_fixtures() -> None:
    freeze = load_json(FREEZE)
    registry = load_json(REGISTRY)
    focused_group_ids = {
        group["id"]
        for group in registry["groups"]
        if group["scope_class"] == "focused"
    }
    cases = freeze["calibration_cases"]
    assert sorted(entry["case"] for entry in cases) == sorted(CASES)
    for entry in cases:
        expected = load_json(FIXTURES / str(entry["case"]) / "expected.json")
        assert entry["parent"] == expected["parent"]
        assert entry["fix"] == expected["fix"]
        assert entry["focused_groups"] == expected["focused_groups"]
        assert entry["focused_groups"], entry["case"]
        for focused_id in entry["focused_groups"]:
            assert focused_id in focused_group_ids
        assert (REPO_ROOT / str(entry["fixture"])).is_file()


def test_freeze_records_policy_env_and_volatile_exclusion() -> None:
    freeze = load_json(FREEZE)
    contract = freeze["command_contract"]
    assert contract["env"] == {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
    }
    assert contract["pytest_cache"] == "off"
    assert contract["volatile_args_excluded"] == ["--junitxml"]

    policy = freeze["p2c_candidate_execution_policy"]
    assert set(policy) == {
        "authority",
        "iteration_run",
        "module_full_recommendation",
        "final_full",
        "no_focused_recommendation",
    }
    assert policy["iteration_run"] == (
        "recommended groups where scope_class==focused only"
    )

    protocol = freeze["case_selection_protocol"]
    assert protocol["held_out_min"] == 3
    assert protocol["provenance_required"] == [
        "module",
        "defect class",
        "parent/fix SHA",
        "oracle blob SHA",
        "oracle grade",
    ]

    invalidation_rule = freeze["invalidation_rule"]
    assert isinstance(invalidation_rule, str)
    assert invalidation_rule


# ---------------------------------------------------------------------------
# D2 — prefix=module-full 전용 · prefix 하위 exact=focused 정제 · fail-closed
# ---------------------------------------------------------------------------


def test_prefix_rules_module_full_and_exact_overlap_refinement() -> None:
    registry = load_json(REGISTRY)
    scope_of = {group["id"]: group["scope_class"] for group in registry["groups"]}
    exact_rules = [
        rule for rule in registry["rules"] if rule["match"]["kind"] == "exact"
    ]
    prefix_rules = [
        rule for rule in registry["rules"] if rule["match"]["kind"] == "prefix"
    ]
    prefix_values = [str(rule["match"]["value"]) for rule in prefix_rules]

    # docs prefix 3종은 P2b 계약 승계로 반드시 존재한다.
    for docs_prefix in DOCS_PREFIXES:
        assert docs_prefix in prefix_values

    # prefix 규칙은 module-full 전용 — focused 위장 금지(over-selection 허용 class).
    for rule in prefix_rules:
        scopes = {scope_of[str(gid)] for gid in rule["groups"]}
        assert scopes == {"module-full"}, rule["id"]

    # prefix 하위 exact 규칙은 focused 정제를 더할 때만 허용된다(중복 등재 금지).
    for rule in exact_rules:
        value = str(rule["match"]["value"])
        if any(value.startswith(prefix) for prefix in prefix_values):
            scopes = {scope_of[str(gid)] for gid in rule["groups"]}
            assert "focused" in scopes, (rule["id"], value)

    # repository-full group 없음, match kind는 exact/prefix 그대로 (D2).
    scope_classes = {group["scope_class"] for group in registry["groups"]}
    assert scope_classes == {"focused", "module-full"}
    kinds = {rule["match"]["kind"] for rule in registry["rules"]}
    assert kinds == {"exact", "prefix"}


def test_unmapped_production_path_is_blocked_fail_closed(tmp_path: Path) -> None:
    """미지 production 경로 = unmapped -> 전체 BLOCKED/2 (D2 설계 fail-closed)."""
    changed = tmp_path / "changed-paths.json"
    changed.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "paths": [
                    "methodology/tools/execution_preflight.py",
                    "methodology/tools/unmapped_new_tool.py",
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    result, exit_code = run_selector_cli(changed)
    assert exit_code == 2
    assert result["status"] == "BLOCKED"
    assert result["recommended_groups"] == []
    assert (
        "selector:unmatched_path:methodology/tools/unmapped_new_tool.py"
        in result["reasons"]
    )
