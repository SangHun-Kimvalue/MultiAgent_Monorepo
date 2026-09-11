"""T16-P2b2 registry derivation 도구 테스트 — 블랙박스(CLI subprocess) 실측.

검증 대상(설계 정본 §5, 지시서 하드 조건):

1. 결정론 — 동일 트리 derive 2회 실행이 byte-identical.
2. 소각 case fixture 6건(HR 3 + P2c 3) — draft registry가 changed-paths를 전부 매핑
   (selector PASS/exit 0)하고, focused-class group이 최소 1개 생성되며, focused set이
   gold RED 테스트 파일을 포함한다.
3. ztr 하드 조건 — `src/engine/resume_chain.py` → `tests/test_phase_relay.py` **전이**
   포함 + focused 유지(강등으로 소멸 금지).
4. 강등 가드 — 분자/분모 기록, threshold 파라미터 반응, conftest 결합 강등(합성 트리).
5. 유도 불가의 명시 기록(silent 누락 금지) — ztr test_e2e_integration.py 등.

draft 매핑 판정은 enforcement 정본인 validation_impact_selector CLI를 subprocess로
실행해 얻는다(등가 재구현 금지 — R5). 이 테스트의 전건 통과는 도구 계약 충족의
근거일 뿐, threshold 확정·registry 반영·freeze v2·census v2는 주장하지 않는다(S1 밖).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "methodology" / "tools" / "registry_derivation.py"
SELECTOR = REPO_ROOT / "methodology" / "tools" / "validation_impact_selector.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "registry_derivation"
MODULE_SPEC = FIXTURES / "module-spec.json"
CASES_DIR = FIXTURES / "cases"
CASE_NAMES = sorted(entry.name for entry in CASES_DIR.iterdir() if entry.is_dir())
# 도구 생성 argv 계약: [interpreter, -m, pytest, -q, -p, no:cacheprovider, <경로...>]
BOILERPLATE_LEN = 6


def _sub_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run_derive(
    out_dir: Path,
    *,
    threshold: float | None = None,
    spec: Path | None = None,
    repo_root: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        sys.executable,
        str(TOOL),
        "derive",
        "--repo-root",
        str(repo_root if repo_root is not None else REPO_ROOT),
        "--module-spec",
        str(spec if spec is not None else MODULE_SPEC),
        "--out-dir",
        str(out_dir),
    ]
    if threshold is not None:
        cmd.extend(["--threshold", str(threshold)])
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(REPO_ROOT),
        env=_sub_env(),
    )


def _run_selector(changed_paths: Path, registry: Path) -> tuple[int, dict[str, Any]]:
    proc = subprocess.run(
        [
            sys.executable,
            str(SELECTOR),
            "recommend",
            "--changed-paths",
            str(changed_paths),
            "--registry",
            str(registry),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(REPO_ROOT),
        env=_sub_env(),
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert lines, f"selector stdout이 비었다: stderr={proc.stderr}"
    result: dict[str, Any] = json.loads(lines[-1])
    return proc.returncode, result


def _load_json(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _repo_rel_test_paths(group: dict[str, Any]) -> set[str]:
    """focused group argv의 테스트 경로를 repo-relative로 정규화한다."""
    cwd = str(group["cwd"])
    covered: set[str] = set()
    for arg in list(group["argv"])[BOILERPLATE_LEN:]:
        entry = str(arg)
        covered.add(entry if cwd == "." else f"{cwd}/{entry}")
    return covered


def _focused_cover(result: dict[str, Any]) -> tuple[list[dict[str, Any]], set[str]]:
    focused = [
        group
        for group in result["recommended_groups"]
        if group["scope_class"] == "focused"
    ]
    covered: set[str] = set()
    for group in focused:
        covered.update(_repo_rel_test_paths(group))
    return focused, covered


@pytest.fixture(scope="module")
def derived(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """실제 트리 대상 derive 1회 — 모듈 내 전 테스트가 공유한다."""
    out_dir = tmp_path_factory.mktemp("derive-real")
    proc = _run_derive(out_dir)
    assert proc.returncode == 0, f"derive 실패: stdout={proc.stdout} stderr={proc.stderr}"
    draft_path = out_dir / "draft-registry.json"
    sidecar_path = out_dir / "derivation-sidecar.json"
    assert draft_path.is_file() and sidecar_path.is_file()
    return {
        "out_dir": out_dir,
        "draft_path": draft_path,
        "sidecar_path": sidecar_path,
        "draft": _load_json(draft_path),
        "sidecar": _load_json(sidecar_path),
    }


def test_determinism_two_runs_byte_identical(tmp_path: Path) -> None:
    """하드 조건 1 — 동일 트리 재실행 시 draft·사이드카 모두 byte-identical."""
    out_a = tmp_path / "run-a"
    out_b = tmp_path / "run-b"
    proc_a = _run_derive(out_a)
    proc_b = _run_derive(out_b)
    assert proc_a.returncode == 0, proc_a.stdout + proc_a.stderr
    assert proc_b.returncode == 0, proc_b.stdout + proc_b.stderr
    draft_a = (out_a / "draft-registry.json").read_bytes()
    draft_b = (out_b / "draft-registry.json").read_bytes()
    sidecar_a = (out_a / "derivation-sidecar.json").read_bytes()
    sidecar_b = (out_b / "derivation-sidecar.json").read_bytes()
    assert draft_a == draft_b, "draft registry가 재실행에서 byte-identical하지 않다"
    assert sidecar_a == sidecar_b, "사이드카가 재실행에서 byte-identical하지 않다"


@pytest.mark.parametrize("case_name", CASE_NAMES)
def test_burn_case_mapping_and_focused_gold(
    case_name: str, derived: dict[str, Any]
) -> None:
    """하드 조건 2 — 소각 case: 전 경로 매핑 + focused ≥1 + gold RED 포함."""
    case_dir = CASES_DIR / case_name
    expected = _load_json(case_dir / "expected.json")
    returncode, result = _run_selector(
        case_dir / "changed-paths.json", derived["draft_path"]
    )
    assert returncode == 0 and result["status"] == "PASS", (
        f"{case_name}: draft가 changed-paths를 전부 매핑하지 못했다 — "
        f"reasons={result['reasons']}"
    )
    focused, covered = _focused_cover(result)
    assert focused, f"{case_name}: focused-class group이 강등으로 소멸했다(≥1 필요)"
    gold = set(expected["gold_red_test_files"])
    assert gold.issubset(covered), (
        f"{case_name}: focused set이 gold RED 테스트 파일을 포함하지 않는다 — "
        f"missing={sorted(gold - covered)}"
    )
    required = expected.get("required_transitive_focused")
    if required is not None:
        _assert_transitive_focused(
            derived,
            source_path=str(required["source_path"]),
            must_include_test=str(required["must_include_test"]),
        )


def _assert_transitive_focused(
    derived: dict[str, Any], *, source_path: str, must_include_test: str
) -> None:
    draft = derived["draft"]
    groups = {group["id"]: group for group in draft["groups"]}
    rule = next(
        (
            entry
            for entry in draft["rules"]
            if entry["match"]["kind"] == "exact"
            and entry["match"]["value"] == source_path
        ),
        None,
    )
    assert rule is not None, f"{source_path}에 대한 exact 규칙이 없다"
    focused_groups = [
        groups[group_id]
        for group_id in rule["groups"]
        if groups[group_id]["scope_class"] == "focused"
    ]
    assert focused_groups, (
        f"{source_path} 규칙이 강등되어 focused group이 없다 — 하드 조건 위반"
    )
    covered: set[str] = set()
    for group in focused_groups:
        covered.update(_repo_rel_test_paths(group))
    assert must_include_test in covered, (
        f"{source_path}의 focused set에 {must_include_test} 전이 포함 실패"
    )


def test_hard_condition_ztr_resume_chain_transitive_focused(
    derived: dict[str, Any],
) -> None:
    """하드 조건 — resume_chain → test_phase_relay 전이 + focused 유지 + 근거 기록."""
    source = "runtimes/ztr/src/engine/resume_chain.py"
    gold = "runtimes/ztr/tests/test_phase_relay.py"
    _assert_transitive_focused(derived, source_path=source, must_include_test=gold)
    rule_id = "exact-runtimes-ztr-src-engine-resume-chain-py"
    sidecar_rule = derived["sidecar"]["modules"]["ztr"]["rules"][rule_id]
    closure = sidecar_rule["closure"]
    assert closure["demoted"] is False
    assert closure["reaches_conftest"] is False
    assert gold in closure["tests"]
    assert 0 < closure["numerator"] <= closure["denominator"]
    chain_info = closure["chains"][gold]
    chain = list(chain_info["chain"])
    assert chain[0] == gold and chain[-1] == source
    assert chain_info["depth"] == len(chain) - 1 >= 1


def test_full_group_execution_contracts(derived: dict[str, Any]) -> None:
    """module-full group 계약 — 특히 methodology-full = guard 전체 재정의(이월 ②)."""
    groups = {group["id"]: group for group in derived["draft"]["groups"]}
    ztr_full = groups["ztr-full"]
    assert ztr_full["cwd"] == "runtimes/ztr"
    assert ztr_full["argv"] == [
        "runtimes/ztr/.venv/Scripts/python.exe",
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
    ]
    acp_full = groups["acp-full"]
    assert acp_full["cwd"] == "runtimes/acp"
    assert acp_full["argv"][0] == "runtimes/acp/.venv/Scripts/python.exe"
    meth_full = groups["methodology-full"]
    assert meth_full["cwd"] == "."
    assert meth_full["argv"] == [
        "runtimes/ztr/.venv/Scripts/python.exe",
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "methodology/tests",
        "methodology/nitpicker",
    ], "methodology-full은 guard 전체(tests+nitpicker)로 재정의되어야 한다"


def test_class_d_prefixes_present(derived: dict[str, Any]) -> None:
    """class D — 신설 prefix(docs/·.claude/·methodology/config/)와 루트 단독 파일."""
    rules = {
        (entry["match"]["kind"], entry["match"]["value"]): entry
        for entry in derived["draft"]["rules"]
    }
    for value in ("docs/", ".claude/", "methodology/config/", "methodology/docs/"):
        entry = rules.get(("prefix", value))
        assert entry is not None, f"class D prefix 규칙 부재: {value}"
        assert entry["groups"] == ["methodology-full"]
    assert rules[("prefix", "runtimes/ztr/docs/")]["groups"] == ["ztr-full"]
    assert rules[("prefix", "runtimes/acp/docs/")]["groups"] == ["acp-full"]
    for value in ("CLAUDE.md", "AGENTS.md", "README.md"):
        assert ("exact", value) in rules, f"루트 단독 파일 규칙 부재: {value}"
    # Corrective 1 — census v2 preview BLOCKED 8커밋 실측으로 닫은 규칙 공백
    for value in (
        "methodology/artifacts/",
        "methodology/adapters/",
        "methodology/.claude-plugin/",
    ):
        entry = rules.get(("prefix", value))
        assert entry is not None, f"Corrective 1 prefix 규칙 부재: {value}"
        assert entry["groups"] == ["methodology-full"]
    for value in (
        "methodology/METHODOLOGY.md",
        "methodology/MULTI_AGENT.md",
        "methodology/INSTALL.md",
        "methodology/README.md",
        "methodology/install.sh",
        "methodology/package.sh",
    ):
        entry = rules.get(("exact", value))
        assert entry is not None, f"Corrective 1 모듈 직속 파일 규칙 부재: {value}"
        assert entry["groups"] == ["methodology-full"]
    assert rules[("exact", "runtimes/ztr/.gitignore")]["groups"] == ["ztr-full"]
    assert rules[("exact", "runtimes/acp/.gitignore")]["groups"] == ["acp-full"]


def test_sidecar_demotion_records_and_threshold_grid(
    derived: dict[str, Any],
) -> None:
    """모든 강등 판단의 분자/분모 기록 + 0.4/0.5/0.6 그리드 통계(§4·§5)."""
    sidecar = derived["sidecar"]
    assert sidecar["threshold"] == 0.5
    for module_name in ("ztr", "acp", "methodology"):
        module = sidecar["modules"][module_name]
        assert module["test_file_count"] > 0
        grid = module["threshold_grid_stats"]
        for key in ("0.4", "0.5", "0.6"):
            assert key in grid, f"{module_name}: threshold 그리드 {key} 통계 부재"
            assert grid[key]["focused_rules"] + grid[key]["demoted_rules"] > 0
        for rule_id, entry in module["rules"].items():
            closure = entry.get("closure")
            if closure is None:
                continue
            assert isinstance(closure["numerator"], int), rule_id
            assert isinstance(closure["denominator"], int), rule_id
            assert closure["denominator"] == module["test_file_count"]


def test_underivable_explicitly_recorded(derived: dict[str, Any]) -> None:
    """유도 불가(동적 import·subprocess 블랙박스)의 silent 누락 금지."""
    ztr = derived["sidecar"]["modules"]["ztr"]
    e2e = "runtimes/ztr/tests/test_e2e_integration.py"
    assert e2e in ztr["underivable"], "블랙박스 e2e 테스트가 유도 불가로 기록되지 않았다"
    assert "subprocess_blackbox_no_static_imports" in ztr["underivable"][e2e]
    acp = derived["sidecar"]["modules"]["acp"]
    cli_help = "runtimes/acp/tests/test_cli_help.py"
    assert cli_help in acp["underivable"], "subprocess 테스트가 유도 불가로 기록되지 않았다"


def test_js_contract_wrapper_reference_focused(derived: dict[str, Any]) -> None:
    """비-py 자산은 래퍼 참조 실측 시에만 focused(설계 §3 JS 계약 경계)."""
    draft = derived["draft"]
    groups = {group["id"]: group for group in draft["groups"]}
    rules = {
        entry["match"]["value"]: entry
        for entry in draft["rules"]
        if entry["match"]["kind"] == "exact"
    }
    mjs = "runtimes/acp/tests/js/execution_evidence_store.test.mjs"
    assert mjs in rules, "래퍼 참조가 실측된 mjs 자산의 exact 규칙 부재"
    focused = [
        groups[group_id]
        for group_id in rules[mjs]["groups"]
        if groups[group_id]["scope_class"] == "focused"
    ]
    assert focused
    covered: set[str] = set()
    for group in focused:
        covered.update(_repo_rel_test_paths(group))
    assert "runtimes/acp/tests/test_execution_evidence_js.py" in covered
    # dashboard.js는 래퍼가 직접 검증하지 않는다 — exact focused 규칙이 있으면 설계 위반
    dashboard = rules.get("runtimes/acp/acp/web/static/dashboard.js")
    if dashboard is not None:
        assert all(
            groups[group_id]["scope_class"] == "module-full"
            for group_id in dashboard["groups"]
        )


def _build_synthetic_repo(root: Path) -> Path:
    """합성 트리 — 지연 import·전이 폐쇄·강등·conftest 결합을 소형으로 재현."""

    def write(rel: str, text: str) -> None:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    write("mod/src/__init__.py", "")
    write("mod/src/a.py", "from src.b import helper\n\n\ndef use() -> int:\n    return helper()\n")
    write("mod/src/b.py", "def helper() -> int:\n    return 1\n")
    write("mod/src/c.py", "VALUE = 3\n")
    write("mod/tests/conftest.py", "from src.c import VALUE  # noqa: F401\n")
    write(
        "mod/tests/test_a.py",
        "def test_use() -> None:\n    from src.a import use\n\n    assert use() == 1\n",
    )
    write(
        "mod/tests/test_b.py",
        "import src.a  # noqa: F401\n\n\ndef test_two() -> None:\n    assert True\n",
    )
    write("mod/tests/test_plain.py", "def test_plain() -> None:\n    assert True\n")
    spec = {
        "schema_version": 1,
        "exclude_dir_names": ["__pycache__"],
        "exclude_dir_suffixes": [],
        "modules": [
            {
                "name": "mod",
                "packages": [
                    {"package": "src", "path": "mod/src"},
                    {"package": "tests", "path": "mod/tests"},
                ],
                "source_roots": ["mod/src"],
                "test_root": "mod/tests",
                "test_exclude_dirs": [],
                "interpreter": "python",
                "pytest_cwd": "mod",
                "full_group_id": "mod-full",
                "full_group_paths": [],
                "coupling_forced": [],
                "asset_prefixes": [],
            }
        ],
        "class_d_rules": [],
    }
    spec_path = root / "module-spec.json"
    spec_path.write_text(
        json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return spec_path


def test_synthetic_lazy_transitive_closure_and_conftest(tmp_path: Path) -> None:
    """지연 import 전이 폐쇄(depth 2)와 conftest 결합 강등 — threshold 0.7."""
    spec_path = _build_synthetic_repo(tmp_path)
    out_dir = tmp_path / "out"
    proc = _run_derive(out_dir, threshold=0.7, spec=spec_path, repo_root=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    sidecar = _load_json(out_dir / "derivation-sidecar.json")
    rules = sidecar["modules"]["mod"]["rules"]
    # b.py: test_a가 함수 내부(지연) import한 a를 거쳐 전이 도달 — depth 2, focused 유지
    closure_b = rules["exact-mod-src-b-py"]["closure"]
    assert closure_b["demoted"] is False
    assert closure_b["numerator"] == 2 and closure_b["denominator"] == 3
    chain = list(closure_b["chains"]["mod/tests/test_a.py"]["chain"])
    assert chain == ["mod/tests/test_a.py", "mod/src/a.py", "mod/src/b.py"]
    assert closure_b["chains"]["mod/tests/test_a.py"]["depth"] == 2
    # c.py: conftest가 import — 전 suite 결합으로 강등(분자=분모)
    closure_c = rules["exact-mod-src-c-py"]["closure"]
    assert closure_c["reaches_conftest"] is True
    assert closure_c["demoted"] is True
    assert closure_c["numerator"] == closure_c["denominator"] == 3
    assert rules["exact-mod-src-c-py"]["decision"] == "module-full"
    # 임포트 0 테스트는 유도 불가로 명시 기록
    assert "mod/tests/test_plain.py" in sidecar["modules"]["mod"]["underivable"]


def test_synthetic_demotion_guard_threshold_default(tmp_path: Path) -> None:
    """기본 threshold 0.5 — 2/3 > 0.5는 module-full 강등 + 분자/분모 기록."""
    spec_path = _build_synthetic_repo(tmp_path)
    out_dir = tmp_path / "out"
    proc = _run_derive(out_dir, spec=spec_path, repo_root=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    draft = _load_json(out_dir / "draft-registry.json")
    sidecar = _load_json(out_dir / "derivation-sidecar.json")
    rule_a = sidecar["modules"]["mod"]["rules"]["exact-mod-src-a-py"]
    assert rule_a["decision"] == "module-full"
    assert rule_a["closure"]["demoted"] is True
    assert rule_a["closure"]["numerator"] == 2
    assert rule_a["closure"]["denominator"] == 3
    groups = {group["id"]: group for group in draft["groups"]}
    rule_entry = next(
        entry
        for entry in draft["rules"]
        if entry["match"]["value"] == "mod/src/a.py"
    )
    assert all(
        groups[group_id]["scope_class"] == "module-full"
        for group_id in rule_entry["groups"]
    )
