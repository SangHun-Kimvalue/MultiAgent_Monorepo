#!/usr/bin/env python3
"""T16-P2b2 registry derivation 도구 — 확장 registry **draft 생성기**(registry 권위가 아니다).

repo 루트 + 모듈 스펙(JSON 데이터 — class C/D 고정 목록 포함, 하드코딩 금지)을 입력으로
Python AST 기반 모듈 내부 import 그래프를 만들고, 소스별 역방향 전이 폐쇄로 focused
테스트 집합을 유도해 draft registry JSON + 유도 근거 사이드카 JSON을 산출한다.

경계(설계 정본 `.ztr/orchestrator/T16-P2b2/planner-design.md` §4):

- **R5**: 도구는 draft와 근거만 산출한다. registry 반영은 Planner 검토 + 리뷰 leg를
  거친 사람 소유 편집이며, 이 도구는 `methodology/config/validation-impact-registry.json`
  을 절대 쓰지 않는다(출력은 --out-dir의 draft/사이드카 2개 파일뿐).
- **형식 준수**: draft는 기존 registry v1 스키마와 동일 형식이다. enforcement 정본인
  `validation_impact_selector.validate_registry`(같은 디렉터리)를 재사용해 산출 직전
  자기검증한다 — 검증 실패는 fail-closed(BLOCKED)이지 자동 교정이 아니다.
- **결정론**: 동일 트리 + 동일 스펙 재실행 시 byte-identical(정렬 walk·고정 직렬화·
  타임스탬프 금지). LLM 호출 0. 파일 입출력 encoding='utf-8'.
- **유도 한계의 정직성**: 동적 문자열 import·subprocess 실행 테스트는 유도 불가로
  사이드카에 **명시 기록**한다(silent 누락 금지). path-literal(예: methodology 테스트의
  spec_from_file_location 경로 상수) 해석 엣지는 `path_literal` kind로 구분 기록한다.

rule class(§3): A test-self(focused) · B import-closure(focused, 강등 가드) ·
C coupling-forced/커버0/비-py 자산(module-full) · D docs/config prefix(module-full) ·
E methodology 계층(B와 동일 유도 + methodology-full = guard 전체 재정의).

강등 가드: focused set 크기 > threshold × 모듈 테스트 파일 수 → module-full 강등.
모든 판단의 분자/분모와 0.4/0.5/0.6 그리드 통계를 사이드카에 기록한다.
threshold 최종값 선택은 Planner 소유(이 도구는 기본 0.5의 CLI 파라미터일 뿐).
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import re
import sys
from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

import validation_impact_selector as _selector

SPEC_SCHEMA_VERSION = 1
REGISTRY_SCHEMA_VERSION = 1
SIDECAR_SCHEMA_VERSION = 1
DEFAULT_THRESHOLD = 0.5
THRESHOLD_GRID: tuple[float, ...] = (0.4, 0.5, 0.6)
PYTEST_BOILERPLATE: tuple[str, ...] = ("-m", "pytest", "-q", "-p", "no:cacheprovider")
DRAFT_FILENAME = "draft-registry.json"
SIDECAR_FILENAME = "derivation-sidecar.json"
EXIT_OK = 0
EXIT_INPUT = 2
EXIT_INTERNAL = 70
NOT_CLAIMED: tuple[str, ...] = (
    "census_v2",
    "coverage_for_zero_covered_paths",
    "freeze_v2",
    "held_out_detection",
    "registry_reflection",
    "threshold_final_value",
)
_ID_SANITIZE = re.compile(r"[^A-Za-z0-9]+")


class DerivationInputError(ValueError):
    """fail-closed 입력 오류 — 구조화 사유 목록을 보존한다."""

    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons: tuple[str, ...] = tuple(sorted(set(reasons)))
        super().__init__("; ".join(self.reasons))


@dataclass(frozen=True)
class PackageMap:
    """dotted import 해석용 패키지 → 디렉터리 매핑."""

    package: str
    path: str


@dataclass(frozen=True)
class SpecRuleEntry:
    """스펙이 선언한 고정 규칙(class C coupling / asset prefix)."""

    kind: str  # exact | prefix
    value: str
    rationale: str


@dataclass(frozen=True)
class ClassDRule:
    """docs/config prefix 또는 루트 단독 파일 → module-full 제안(class D)."""

    kind: str
    value: str
    group: str
    rationale: str


@dataclass(frozen=True)
class ModuleSpec:
    """모듈 하나의 유도 스펙 — 전부 데이터(도구 하드코딩 금지)."""

    name: str
    packages: tuple[PackageMap, ...]
    source_roots: tuple[str, ...]
    test_root: str
    test_exclude_dirs: tuple[str, ...]
    interpreter: str
    pytest_cwd: str
    full_group_id: str
    full_group_paths: tuple[str, ...]
    coupling_forced: tuple[SpecRuleEntry, ...]
    asset_prefixes: tuple[SpecRuleEntry, ...]
    asset_wrapper_glob: str | None


@dataclass(frozen=True)
class ChainInfo:
    """테스트 → 소스 최단 유도 경로(전이 깊이 포함)."""

    chain: tuple[str, ...]
    depth: int
    edge_kinds: tuple[str, ...]


@dataclass(frozen=True)
class ClosureInfo:
    """강등 판단 근거 — 분자/분모/conftest 결합 여부를 그대로 보존한다."""

    numerator: int
    denominator: int
    demoted: bool
    reaches_conftest: bool
    tests: tuple[str, ...]
    chains: dict[str, ChainInfo]


@dataclass(frozen=True)
class RuleRecord:
    """draft rule 1건 + 사이드카 근거."""

    rule_id: str
    kind: str
    value: str
    group_ids: tuple[str, ...]
    klass: str  # A|B|C|D|E
    subkind: str
    module: str | None
    decision: str  # focused | module-full
    rationale: str | None
    closure: ClosureInfo | None


@dataclass(frozen=True)
class GroupRecord:
    """draft group 1건."""

    group_id: str
    cwd: str
    argv: tuple[str, ...]
    scope_class: str
    module: str | None
    test_files: tuple[str, ...]


def _canonical_text(obj: object) -> str:
    """결정론 직렬화 — key 정렬, 2-space indent, 후행 개행 1개."""
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _compact_json(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha10(obj: object) -> str:
    return hashlib.sha256(_compact_json(obj).encode("utf-8")).hexdigest()[:10]


def _sanitize_id(text: str) -> str:
    return _ID_SANITIZE.sub("-", text).strip("-").lower()


def _require_str(doc: dict[str, object], key: str, label: str, reasons: list[str]) -> str:
    value = doc.get(key)
    if not isinstance(value, str) or value == "":
        reasons.append(f"{label}:invalid_field:{key}")
        return ""
    return value


def _str_list(doc: dict[str, object], key: str, label: str, reasons: list[str]) -> tuple[str, ...]:
    value = doc.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) and item != "" for item in value):
        reasons.append(f"{label}:invalid_field:{key}")
        return ()
    return tuple(value)


def _spec_rule_entries(
    doc: dict[str, object], key: str, label: str, reasons: list[str], *, allowed_kinds: frozenset[str]
) -> tuple[SpecRuleEntry, ...]:
    raw = doc.get(key, [])
    if not isinstance(raw, list):
        reasons.append(f"{label}:invalid_field:{key}")
        return ()
    entries: list[SpecRuleEntry] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            reasons.append(f"{label}:{key}:not_object:index-{index}")
            continue
        kind = item.get("kind", "prefix")
        value = item.get("value")
        rationale = item.get("rationale")
        if not isinstance(kind, str) or kind not in allowed_kinds:
            reasons.append(f"{label}:{key}:invalid_kind:index-{index}")
            continue
        if not isinstance(value, str) or value == "":
            reasons.append(f"{label}:{key}:invalid_value:index-{index}")
            continue
        if not isinstance(rationale, str) or rationale == "":
            reasons.append(f"{label}:{key}:missing_rationale:{value}")
            continue
        entries.append(SpecRuleEntry(kind=kind, value=value, rationale=rationale))
    return tuple(entries)


def load_module_spec(
    spec_path: Path,
) -> tuple[tuple[ModuleSpec, ...], tuple[ClassDRule, ...], tuple[str, ...], tuple[str, ...]]:
    """모듈 스펙 JSON을 fail-closed 검증 후 로드한다."""
    reasons: list[str] = []
    try:
        doc_raw: object = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DerivationInputError([f"spec:unreadable_or_malformed:{type(error).__name__}"]) from error
    if not isinstance(doc_raw, dict):
        raise DerivationInputError(["spec:not_object"])
    if doc_raw.get("schema_version") != SPEC_SCHEMA_VERSION:
        reasons.append("spec:unsupported_schema_version")
    exclude_names = _str_list(doc_raw, "exclude_dir_names", "spec", reasons)
    exclude_suffixes = _str_list(doc_raw, "exclude_dir_suffixes", "spec", reasons)
    modules_raw = doc_raw.get("modules")
    modules: list[ModuleSpec] = []
    if not isinstance(modules_raw, list) or not modules_raw:
        reasons.append("spec:invalid_field:modules")
        modules_raw = []
    for index, entry in enumerate(modules_raw):
        if not isinstance(entry, dict):
            reasons.append(f"spec:module_not_object:index-{index}")
            continue
        label = f"spec:module:{entry.get('name', index)}"
        packages_raw = entry.get("packages", [])
        packages: list[PackageMap] = []
        if isinstance(packages_raw, list):
            for pkg in packages_raw:
                if (
                    isinstance(pkg, dict)
                    and isinstance(pkg.get("package"), str)
                    and isinstance(pkg.get("path"), str)
                ):
                    packages.append(PackageMap(package=str(pkg["package"]), path=str(pkg["path"])))
                else:
                    reasons.append(f"{label}:invalid_package_entry")
        else:
            reasons.append(f"{label}:invalid_field:packages")
        wrapper_glob_raw = entry.get("asset_wrapper_glob")
        wrapper_glob: str | None
        if wrapper_glob_raw is None:
            wrapper_glob = None
        elif isinstance(wrapper_glob_raw, str) and wrapper_glob_raw != "":
            wrapper_glob = wrapper_glob_raw
        else:
            reasons.append(f"{label}:invalid_field:asset_wrapper_glob")
            wrapper_glob = None
        modules.append(
            ModuleSpec(
                name=_require_str(entry, "name", label, reasons),
                packages=tuple(packages),
                source_roots=_str_list(entry, "source_roots", label, reasons),
                test_root=_require_str(entry, "test_root", label, reasons),
                test_exclude_dirs=_str_list(entry, "test_exclude_dirs", label, reasons),
                interpreter=_require_str(entry, "interpreter", label, reasons),
                pytest_cwd=_require_str(entry, "pytest_cwd", label, reasons),
                full_group_id=_require_str(entry, "full_group_id", label, reasons),
                full_group_paths=_str_list(entry, "full_group_paths", label, reasons),
                coupling_forced=_spec_rule_entries(
                    entry, "coupling_forced", label, reasons, allowed_kinds=frozenset({"exact", "prefix"})
                ),
                asset_prefixes=_spec_rule_entries(
                    entry, "asset_prefixes", label, reasons, allowed_kinds=frozenset({"prefix"})
                ),
                asset_wrapper_glob=wrapper_glob,
            )
        )
    full_group_ids = {module.full_group_id for module in modules}
    class_d_raw = doc_raw.get("class_d_rules", [])
    class_d: list[ClassDRule] = []
    if not isinstance(class_d_raw, list):
        reasons.append("spec:invalid_field:class_d_rules")
        class_d_raw = []
    for index, entry in enumerate(class_d_raw):
        if not isinstance(entry, dict):
            reasons.append(f"spec:class_d:not_object:index-{index}")
            continue
        kind = entry.get("kind")
        value = entry.get("value")
        group = entry.get("group")
        rationale = entry.get("rationale")
        if (
            not isinstance(kind, str)
            or kind not in {"exact", "prefix"}
            or not isinstance(value, str)
            or value == ""
            or not isinstance(group, str)
            or not isinstance(rationale, str)
            or rationale == ""
        ):
            reasons.append(f"spec:class_d:invalid_entry:index-{index}")
            continue
        if group not in full_group_ids:
            reasons.append(f"spec:class_d:unknown_group:{value}:{group}")
            continue
        class_d.append(ClassDRule(kind=kind, value=value, group=group, rationale=rationale))
    module_names = [module.name for module in modules]
    if len(module_names) != len(set(module_names)):
        reasons.append("spec:duplicate_module_name")
    if reasons:
        raise DerivationInputError(reasons)
    return tuple(modules), tuple(class_d), exclude_names, exclude_suffixes


def _is_excluded_dir(name: str, exclude_names: Sequence[str], exclude_suffixes: Sequence[str]) -> bool:
    if name in exclude_names:
        return True
    return any(name.endswith(suffix) for suffix in exclude_suffixes)


def _walk_files(
    root: Path, exclude_names: Sequence[str], exclude_suffixes: Sequence[str]
) -> Iterator[Path]:
    """결정론 walk — 디렉터리 항목을 이름순으로 순회한다."""
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if entry.is_dir():
            if _is_excluded_dir(entry.name, exclude_names, exclude_suffixes):
                continue
            yield from _walk_files(entry, exclude_names, exclude_suffixes)
        elif entry.is_file():
            yield entry


def _rel_posix(path: Path, repo_root: Path) -> str:
    return path.relative_to(repo_root).as_posix()


def _is_test_file(rel_path: str) -> bool:
    name = rel_path.rsplit("/", 1)[-1]
    return name.startswith("test_") and name.endswith(".py")


@dataclass(frozen=True)
class FileScan:
    """py 파일 1개의 AST 스캔 결과."""

    dotted_imports: tuple[str, ...]
    from_imports: tuple[tuple[int, str, tuple[str, ...]], ...]  # (level, module, names)
    string_literals: tuple[str, ...]
    uses_subprocess: bool
    uses_dynamic_import: bool
    parse_error: bool


def _scan_file(abs_path: Path) -> FileScan:
    """AST 전체 순회 — 함수 내부 지연 import까지 수집한다."""
    try:
        tree = ast.parse(abs_path.read_text(encoding="utf-8"), filename=str(abs_path))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return FileScan((), (), (), False, False, True)
    dotted: list[str] = []
    from_imports: list[tuple[int, str, tuple[str, ...]]] = []
    literals: list[str] = []
    uses_subprocess = False
    uses_dynamic = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                dotted.append(alias.name)
                if alias.name == "subprocess":
                    uses_subprocess = True
                if alias.name in {"importlib", "importlib.util"}:
                    uses_dynamic = True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "subprocess" or module.startswith("subprocess."):
                uses_subprocess = True
            if module == "importlib" or module.startswith("importlib."):
                uses_dynamic = True
            from_imports.append((node.level, module, tuple(alias.name for alias in node.names)))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.append(node.value)
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "__import__":
                uses_dynamic = True
    return FileScan(
        dotted_imports=tuple(dotted),
        from_imports=tuple(from_imports),
        string_literals=tuple(literals),
        uses_subprocess=uses_subprocess,
        uses_dynamic_import=uses_dynamic,
        parse_error=False,
    )


@dataclass
class ModuleGraph:
    """모듈 내부 import 그래프 + 유도 메타."""

    spec: ModuleSpec
    graph_files: list[str]
    test_files: list[str]
    source_files: list[str]
    helper_files: list[str]
    non_py_files: list[str]
    conftest_path: str | None
    test_init_path: str | None
    edges: dict[str, set[str]]
    edge_kinds: dict[tuple[str, str], str]
    underivable: dict[str, list[str]]
    ambiguous_literals: list[str]
    wrapper_asset_refs: dict[str, list[str]]  # asset -> [wrapper tests]


def _dotted_candidates(dotted_map: dict[str, str], name: str) -> list[str]:
    """dotted 모듈명 → 실재 파일 목록(조상 __init__ 포함, 존재분만)."""
    parts = name.split(".")
    found: list[str] = []
    for end in range(1, len(parts) + 1):
        candidate = ".".join(parts[:end])
        target = dotted_map.get(candidate)
        if target is not None:
            found.append(target)
    return found


def build_module_graph(
    repo_root: Path,
    spec: ModuleSpec,
    exclude_names: Sequence[str],
    exclude_suffixes: Sequence[str],
) -> ModuleGraph:
    """소스·테스트 파일 발견 + AST import 그래프 구축."""
    source_files: list[str] = []
    co_located_tests: list[str] = []
    non_py: list[str] = []
    for root_rel in spec.source_roots:
        root_abs = repo_root / root_rel
        if not root_abs.is_dir():
            raise DerivationInputError([f"spec:module:{spec.name}:missing_source_root:{root_rel}"])
        for abs_path in _walk_files(root_abs, exclude_names, exclude_suffixes):
            rel = _rel_posix(abs_path, repo_root)
            if rel.endswith(".py"):
                if _is_test_file(rel):
                    co_located_tests.append(rel)
                else:
                    source_files.append(rel)
            else:
                non_py.append(rel)

    test_root_abs = repo_root / spec.test_root
    if not test_root_abs.is_dir():
        raise DerivationInputError([f"spec:module:{spec.name}:missing_test_root:{spec.test_root}"])
    test_files: list[str] = []
    helper_files: list[str] = []
    conftest_path: str | None = None
    test_init_path: str | None = None
    excluded_test_prefixes = tuple(
        f"{spec.test_root}/{name}/" for name in spec.test_exclude_dirs
    )
    for abs_path in _walk_files(test_root_abs, exclude_names, exclude_suffixes):
        rel = _rel_posix(abs_path, repo_root)
        if any(rel.startswith(prefix) for prefix in excluded_test_prefixes):
            if not rel.endswith(".py"):
                non_py.append(rel)
            continue
        if not rel.endswith(".py"):
            non_py.append(rel)
            continue
        name = rel.rsplit("/", 1)[-1]
        if _is_test_file(rel):
            test_files.append(rel)
        elif name == "conftest.py" and rel == f"{spec.test_root}/conftest.py":
            conftest_path = rel
        elif name == "__init__.py" and rel == f"{spec.test_root}/__init__.py":
            test_init_path = rel
        else:
            helper_files.append(rel)

    test_files = sorted(set(test_files) | set(co_located_tests))
    source_files = sorted(set(source_files))
    helper_files = sorted(set(helper_files))
    non_py = sorted(set(non_py))

    graph_files = sorted(
        set(source_files)
        | set(test_files)
        | set(helper_files)
        | ({conftest_path} if conftest_path else set())
        | ({test_init_path} if test_init_path else set())
    )
    graph_set = set(graph_files)

    # dotted 모듈명 → 파일 매핑(패키지 스펙 기반)
    dotted_map: dict[str, str] = {}
    file_dotted: dict[str, str] = {}
    for pkg in spec.packages:
        prefix = pkg.path.rstrip("/") + "/"
        for rel in graph_files:
            if not rel.startswith(prefix):
                continue
            inner = rel[len(prefix):]
            parts = inner[:-3].split("/")  # strip ".py"
            if parts[-1] == "__init__":
                dotted = ".".join([pkg.package, *parts[:-1]]) if parts[:-1] else pkg.package
            else:
                dotted = ".".join([pkg.package, *parts])
            dotted_map[dotted] = rel
            file_dotted[rel] = dotted

    # bare 모듈명(stem) → 소스 파일(유일할 때만) — sys.path.insert 패턴 해석용
    stem_counts: dict[str, list[str]] = {}
    for rel in source_files + helper_files:
        stem = rel.rsplit("/", 1)[-1][:-3]
        stem_counts.setdefault(stem, []).append(rel)
    bare_map = {stem: paths[0] for stem, paths in stem_counts.items() if len(paths) == 1}

    # path-literal 해석용: 소스/헬퍼 py basename(유일할 때만)
    py_name_counts: dict[str, list[str]] = {}
    for rel in source_files + helper_files:
        base = rel.rsplit("/", 1)[-1]
        py_name_counts.setdefault(base, []).append(rel)
    py_literal_map = {base: paths[0] for base, paths in py_name_counts.items() if len(paths) == 1}
    ambiguous_literals = sorted(base for base, paths in py_name_counts.items() if len(paths) > 1)

    # 비-py 자산 basename(유일할 때만) — JS 계약 래퍼 참조 실측용
    asset_name_counts: dict[str, list[str]] = {}
    for rel in non_py:
        base = rel.rsplit("/", 1)[-1]
        asset_name_counts.setdefault(base, []).append(rel)
    asset_literal_map = {
        base: paths[0] for base, paths in asset_name_counts.items() if len(paths) == 1
    }

    test_file_set = set(test_files)
    edges: dict[str, set[str]] = {rel: set() for rel in graph_files}
    edge_kinds: dict[tuple[str, str], str] = {}
    underivable: dict[str, list[str]] = {}
    wrapper_asset_refs: dict[str, list[str]] = {}

    def _add_edge(importer: str, imported: str, kind: str) -> None:
        if imported == importer or imported not in graph_set:
            return
        if imported not in edges[importer]:
            edges[importer].add(imported)
            edge_kinds[(importer, imported)] = kind

    for rel in graph_files:
        scan = _scan_file(repo_root / rel)
        if scan.parse_error:
            underivable[rel] = ["syntax_or_read_error"]
            continue
        for name in scan.dotted_imports:
            for target in _dotted_candidates(dotted_map, name):
                _add_edge(rel, target, "static")
            if "." not in name and name in bare_map:
                _add_edge(rel, bare_map[name], "static")
        current_dotted = file_dotted.get(rel)
        for level, module, names in scan.from_imports:
            if level > 0:
                if current_dotted is None:
                    continue
                parts = current_dotted.split(".")
                package_parts = parts if rel.endswith("/__init__.py") else parts[:-1]
                if level - 1 > len(package_parts):
                    continue
                base_parts = package_parts[: len(package_parts) - (level - 1)]
                resolved = ".".join([*base_parts, module]) if module else ".".join(base_parts)
            else:
                resolved = module
            if not resolved:
                continue
            targets = _dotted_candidates(dotted_map, resolved)
            for target in targets:
                _add_edge(rel, target, "static")
            for alias_name in names:
                for target in _dotted_candidates(dotted_map, f"{resolved}.{alias_name}"):
                    _add_edge(rel, target, "static")
            if level == 0 and "." not in resolved and resolved in bare_map:
                _add_edge(rel, bare_map[resolved], "static")
        # path-literal(.py 문자열 상수) 해석 — 명시 기록되는 보조 엣지
        for literal in scan.string_literals:
            base = literal.replace("\\", "/").rsplit("/", 1)[-1]
            if base.endswith(".py") and base in py_literal_map:
                _add_edge(rel, py_literal_map[base], "path_literal")
        # JS 계약 래퍼의 비-py 자산 참조 실측
        if (
            spec.asset_wrapper_glob is not None
            and rel in test_file_set
            and fnmatch(rel.rsplit("/", 1)[-1], spec.asset_wrapper_glob)
        ):
            for literal in scan.string_literals:
                base = literal.replace("\\", "/").rsplit("/", 1)[-1]
                if base in asset_literal_map:
                    asset = asset_literal_map[base]
                    wrapper_asset_refs.setdefault(asset, [])
                    if rel not in wrapper_asset_refs[asset]:
                        wrapper_asset_refs[asset].append(rel)
        # 유도 불가의 명시 기록(silent 누락 금지)
        if rel in test_file_set and not edges[rel]:
            reasons: list[str] = []
            if scan.uses_subprocess:
                reasons.append("subprocess_blackbox_no_static_imports")
            if scan.uses_dynamic_import:
                reasons.append("dynamic_import_without_resolvable_literal")
            if not reasons:
                reasons.append("no_intra_module_imports")
            underivable[rel] = reasons

    for refs in wrapper_asset_refs.values():
        refs.sort()

    return ModuleGraph(
        spec=spec,
        graph_files=graph_files,
        test_files=test_files,
        source_files=source_files,
        helper_files=helper_files,
        non_py_files=non_py,
        conftest_path=conftest_path,
        test_init_path=test_init_path,
        edges=edges,
        edge_kinds=edge_kinds,
        underivable=underivable,
        ambiguous_literals=ambiguous_literals,
        wrapper_asset_refs=wrapper_asset_refs,
    )


def _reverse_edges(edges: dict[str, set[str]]) -> dict[str, list[str]]:
    reverse: dict[str, list[str]] = {}
    for importer in sorted(edges):
        for imported in sorted(edges[importer]):
            reverse.setdefault(imported, []).append(importer)
    for importers in reverse.values():
        importers.sort()
    return reverse


def compute_closure(
    graph: ModuleGraph, source: str, reverse: dict[str, list[str]]
) -> tuple[set[str], dict[str, ChainInfo], bool]:
    """소스의 역방향 전이 폐쇄 → (테스트 집합, 최단 chain, conftest 결합 여부)."""
    parents: dict[str, str] = {}
    visited: set[str] = {source}
    queue: deque[str] = deque([source])
    while queue:
        current = queue.popleft()
        for importer in reverse.get(current, []):
            if importer not in visited:
                visited.add(importer)
                parents[importer] = current
                queue.append(importer)
    test_set = {rel for rel in visited if rel in set(graph.test_files) and rel != source}
    reaches_conftest = graph.conftest_path is not None and graph.conftest_path in visited
    chains: dict[str, ChainInfo] = {}
    for test in sorted(test_set):
        chain: list[str] = [test]
        cursor = test
        while cursor != source:
            cursor = parents[cursor]
            chain.append(cursor)
        kinds = tuple(
            graph.edge_kinds.get((chain[i], chain[i + 1]), "static") for i in range(len(chain) - 1)
        )
        chains[test] = ChainInfo(chain=tuple(chain), depth=len(chain) - 1, edge_kinds=kinds)
    return test_set, chains, reaches_conftest


class DraftBuilder:
    """draft registry + 사이드카 조립 — 결정론 보장을 위해 전부 정렬 순회."""

    def __init__(self, repo_root: Path, threshold: float) -> None:
        self.repo_root = repo_root
        self.threshold = threshold
        self.groups: dict[str, GroupRecord] = {}
        self.rules: dict[str, RuleRecord] = {}
        self.notes: list[str] = []

    def _register_rule(self, record: RuleRecord) -> None:
        if record.rule_id in self.rules:
            existing = self.rules[record.rule_id]
            if existing.kind == record.kind and existing.value == record.value:
                return
            raise DerivationInputError([f"derivation:rule_id_collision:{record.rule_id}"])
        self.rules[record.rule_id] = record

    def _full_group(self, spec: ModuleSpec) -> str:
        group_id = spec.full_group_id
        if group_id not in self.groups:
            argv = (spec.interpreter, *PYTEST_BOILERPLATE, *spec.full_group_paths)
            self.groups[group_id] = GroupRecord(
                group_id=group_id,
                cwd=spec.pytest_cwd,
                argv=argv,
                scope_class="module-full",
                module=spec.name,
                test_files=(),
            )
        return group_id

    def _focused_group(self, spec: ModuleSpec, test_paths: Sequence[str]) -> str:
        ordered = tuple(sorted(set(test_paths)))
        group_id = f"{spec.name}-focused-{_sha10(list(ordered))}"
        if group_id not in self.groups:
            cwd = spec.pytest_cwd
            rel_args: list[str] = []
            for path in ordered:
                if cwd != "." and path.startswith(cwd.rstrip("/") + "/"):
                    rel_args.append(path[len(cwd.rstrip("/")) + 1 :])
                else:
                    rel_args.append(path)
            argv = (spec.interpreter, *PYTEST_BOILERPLATE, *rel_args)
            self.groups[group_id] = GroupRecord(
                group_id=group_id,
                cwd=cwd,
                argv=argv,
                scope_class="focused",
                module=spec.name,
                test_files=ordered,
            )
        return group_id

    def add_module(self, graph: ModuleGraph) -> None:
        spec = graph.spec
        full_group_id = self._full_group(spec)
        reverse = _reverse_edges(graph.edges)
        denominator = len(graph.test_files)
        source_class = "E" if spec.name == "methodology" else "B"

        coupling_exact = {
            entry.value: entry for entry in spec.coupling_forced if entry.kind == "exact"
        }
        coupling_prefixes = [entry for entry in spec.coupling_forced if entry.kind == "prefix"]
        asset_prefixes = list(spec.asset_prefixes)

        def _under_prefix(path: str, prefixes: Sequence[SpecRuleEntry]) -> bool:
            return any(path.startswith(entry.value) for entry in prefixes)

        # class C — 스펙 선언 coupling(exact/prefix) + asset prefix
        for entry in sorted(coupling_exact.values(), key=lambda item: item.value):
            self._register_rule(
                RuleRecord(
                    rule_id=f"exact-{_sanitize_id(entry.value)}",
                    kind="exact",
                    value=entry.value,
                    group_ids=(full_group_id,),
                    klass="C",
                    subkind="coupling_forced",
                    module=spec.name,
                    decision="module-full",
                    rationale=entry.rationale,
                    closure=None,
                )
            )
        for entry in coupling_prefixes:
            self._register_rule(
                RuleRecord(
                    rule_id=f"prefix-{_sanitize_id(entry.value)}",
                    kind="prefix",
                    value=entry.value,
                    group_ids=(full_group_id,),
                    klass="C",
                    subkind="coupling_forced_prefix",
                    module=spec.name,
                    decision="module-full",
                    rationale=entry.rationale,
                    closure=None,
                )
            )
        for entry in asset_prefixes:
            self._register_rule(
                RuleRecord(
                    rule_id=f"prefix-{_sanitize_id(entry.value)}",
                    kind="prefix",
                    value=entry.value,
                    group_ids=(full_group_id,),
                    klass="C",
                    subkind="asset_prefix",
                    module=spec.name,
                    decision="module-full",
                    rationale=entry.rationale,
                    closure=None,
                )
            )

        # conftest/테스트 패키지 __init__ — 스펙 누락이어도 안전하게 C 강제
        if graph.conftest_path is not None and graph.conftest_path not in coupling_exact:
            self._register_rule(
                RuleRecord(
                    rule_id=f"exact-{_sanitize_id(graph.conftest_path)}",
                    kind="exact",
                    value=graph.conftest_path,
                    group_ids=(full_group_id,),
                    klass="C",
                    subkind="conftest_autouse",
                    module=spec.name,
                    decision="module-full",
                    rationale="conftest는 suite 전역 결합 — focused 주장 불가",
                    closure=None,
                )
            )
        if graph.test_init_path is not None:
            self._register_rule(
                RuleRecord(
                    rule_id=f"exact-{_sanitize_id(graph.test_init_path)}",
                    kind="exact",
                    value=graph.test_init_path,
                    group_ids=(full_group_id,),
                    klass="C",
                    subkind="test_package_init",
                    module=spec.name,
                    decision="module-full",
                    rationale="테스트 패키지 __init__은 전 테스트 수집 경로에서 실행 — 전역 결합",
                    closure=None,
                )
            )

        # class A — test-self focused
        for test in graph.test_files:
            group_id = self._focused_group(spec, [test])
            self._register_rule(
                RuleRecord(
                    rule_id=f"exact-{_sanitize_id(test)}",
                    kind="exact",
                    value=test,
                    group_ids=(group_id,),
                    klass="A",
                    subkind="test_self",
                    module=spec.name,
                    decision="focused",
                    rationale=None,
                    closure=None,
                )
            )

        # class B/E — 소스·테스트 헬퍼의 전이 폐쇄 + 강등 가드
        closure_targets = [(rel, "source") for rel in graph.source_files] + [
            (rel, "test_helper") for rel in graph.helper_files
        ]
        for rel, role in closure_targets:
            if rel in coupling_exact:
                continue  # class C가 이미 소유
            if _under_prefix(rel, coupling_prefixes):
                continue  # class C prefix가 소유(예: ztr src/web/**)
            test_set, chains, reaches_conftest = compute_closure(graph, rel, reverse)
            if reaches_conftest:
                numerator = denominator
            else:
                numerator = len(test_set)
            demoted = numerator == 0 or numerator > self.threshold * denominator
            closure = ClosureInfo(
                numerator=numerator,
                denominator=denominator,
                demoted=demoted,
                reaches_conftest=reaches_conftest,
                tests=tuple(sorted(test_set)),
                chains=chains,
            )
            if demoted:
                group_ids: tuple[str, ...] = (full_group_id,)
                decision = "module-full"
                if numerator == 0:
                    subkind = f"{role}_empty_closure"
                elif reaches_conftest:
                    subkind = f"{role}_conftest_coupled"
                else:
                    subkind = f"{role}_demoted_over_threshold"
            else:
                group_ids = (self._focused_group(spec, sorted(test_set)),)
                decision = "focused"
                subkind = role
            self._register_rule(
                RuleRecord(
                    rule_id=f"exact-{_sanitize_id(rel)}",
                    kind="exact",
                    value=rel,
                    group_ids=group_ids,
                    klass=source_class,
                    subkind=subkind,
                    module=spec.name,
                    decision=decision,
                    rationale=None,
                    closure=closure,
                )
            )

        # 비-py 자산 — 래퍼 참조 실측 시 focused, 아니면 prefix 커버 또는 exact module-full
        for rel in graph.non_py_files:
            wrappers = graph.wrapper_asset_refs.get(rel)
            if wrappers:
                group_id = self._focused_group(spec, wrappers)
                chains = {
                    wrapper: ChainInfo(
                        chain=(wrapper, rel), depth=1, edge_kinds=("wrapper_path_literal",)
                    )
                    for wrapper in wrappers
                }
                self._register_rule(
                    RuleRecord(
                        rule_id=f"exact-{_sanitize_id(rel)}",
                        kind="exact",
                        value=rel,
                        group_ids=(group_id,),
                        klass="B",
                        subkind="js_contract_wrapper_ref",
                        module=spec.name,
                        decision="focused",
                        rationale="JS 계약 래퍼가 파일명을 문자열 상수로 참조(실측)",
                        closure=ClosureInfo(
                            numerator=len(wrappers),
                            denominator=denominator,
                            demoted=False,
                            reaches_conftest=False,
                            tests=tuple(wrappers),
                            chains=chains,
                        ),
                    )
                )
                continue
            if _under_prefix(rel, coupling_prefixes) or _under_prefix(rel, asset_prefixes):
                continue  # 스펙 선언 prefix 규칙(class C)이 커버
            self._register_rule(
                RuleRecord(
                    rule_id=f"exact-{_sanitize_id(rel)}",
                    kind="exact",
                    value=rel,
                    group_ids=(full_group_id,),
                    klass="C",
                    subkind="non_py_asset",
                    module=spec.name,
                    decision="module-full",
                    rationale="비-py 자산 — 래퍼 참조 실측 없음, module-full이 정직",
                    closure=None,
                )
            )

    def add_class_d(self, rules: Sequence[ClassDRule]) -> None:
        for entry in rules:
            self._register_rule(
                RuleRecord(
                    rule_id=f"{entry.kind}-{_sanitize_id(entry.value)}",
                    kind=entry.kind,
                    value=entry.value,
                    group_ids=(entry.group,),
                    klass="D",
                    subkind="docs_config_prefix" if entry.kind == "prefix" else "root_standalone_file",
                    module=None,
                    decision="module-full",
                    rationale=entry.rationale,
                    closure=None,
                )
            )

    def draft_registry(self) -> dict[str, object]:
        groups_out: list[dict[str, object]] = [
            {
                "id": record.group_id,
                "cwd": record.cwd,
                "argv": list(record.argv),
                "scope_class": record.scope_class,
            }
            for record in sorted(self.groups.values(), key=lambda item: item.group_id)
        ]
        rules_out: list[dict[str, object]] = [
            {
                "id": record.rule_id,
                "match": {"kind": record.kind, "value": record.value},
                "groups": list(record.group_ids),
            }
            for record in sorted(self.rules.values(), key=lambda item: item.rule_id)
        ]
        return {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "groups": groups_out,
            "rules": rules_out,
        }

    def sidecar(
        self, graphs: Sequence[ModuleGraph], spec_path_display: str
    ) -> dict[str, object]:
        modules_out: dict[str, object] = {}
        for graph in graphs:
            module_rules: dict[str, object] = {}
            for record in sorted(self.rules.values(), key=lambda item: item.rule_id):
                if record.module != graph.spec.name:
                    continue
                entry: dict[str, object] = {
                    "class": record.klass,
                    "subkind": record.subkind,
                    "match_kind": record.kind,
                    "value": record.value,
                    "decision": record.decision,
                    "groups": list(record.group_ids),
                }
                if record.rationale is not None:
                    entry["rationale"] = record.rationale
                if record.closure is not None:
                    entry["closure"] = {
                        "numerator": record.closure.numerator,
                        "denominator": record.closure.denominator,
                        "threshold": self.threshold,
                        "demoted": record.closure.demoted,
                        "reaches_conftest": record.closure.reaches_conftest,
                        "tests": list(record.closure.tests),
                        "chains": {
                            test: {
                                "chain": list(info.chain),
                                "depth": info.depth,
                                "edge_kinds": list(info.edge_kinds),
                            }
                            for test, info in sorted(record.closure.chains.items())
                        },
                    }
                module_rules[record.rule_id] = entry
            grid_stats: dict[str, object] = {}
            denominator = len(graph.test_files)
            for grid_value in sorted(set(THRESHOLD_GRID) | {self.threshold}):
                focused = 0
                demoted = 0
                for record in self.rules.values():
                    if record.module != graph.spec.name or record.closure is None:
                        continue
                    if record.klass not in {"B", "E"} or record.subkind == "js_contract_wrapper_ref":
                        continue
                    numerator = record.closure.numerator
                    if numerator == 0 or numerator > grid_value * denominator:
                        demoted += 1
                    else:
                        focused += 1
                grid_stats[f"{grid_value}"] = {
                    "focused_rules": focused,
                    "demoted_rules": demoted,
                }
            modules_out[graph.spec.name] = {
                "test_file_count": denominator,
                "test_files": list(graph.test_files),
                "underivable": {
                    path: reasons for path, reasons in sorted(graph.underivable.items())
                },
                "ambiguous_path_literals": list(graph.ambiguous_literals),
                "threshold_grid_stats": grid_stats,
                "rules": module_rules,
            }
        class_d_out: list[dict[str, object]] = [
            {
                "rule_id": record.rule_id,
                "kind": record.kind,
                "value": record.value,
                "group": record.group_ids[0],
                "rationale": record.rationale,
            }
            for record in sorted(self.rules.values(), key=lambda item: item.rule_id)
            if record.klass == "D"
        ]
        groups_out: dict[str, object] = {
            record.group_id: {
                "module": record.module,
                "scope_class": record.scope_class,
                "test_files": list(record.test_files),
            }
            for record in sorted(self.groups.values(), key=lambda item: item.group_id)
        }
        return {
            "schema_version": SIDECAR_SCHEMA_VERSION,
            "tool": "methodology/tools/registry_derivation.py",
            "module_spec": spec_path_display,
            "threshold": self.threshold,
            "modules": modules_out,
            "class_d": class_d_out,
            "groups": groups_out,
            "notes": list(self.notes),
            "not_claimed": list(NOT_CLAIMED),
        }


def run_derivation(
    repo_root: Path, spec_path: Path, out_dir: Path, threshold: float, spec_path_display: str
) -> dict[str, object]:
    """유도 전체 실행 — draft/사이드카 파일 산출 + 요약 dict 반환."""
    modules, class_d, exclude_names, exclude_suffixes = load_module_spec(spec_path)
    builder = DraftBuilder(repo_root=repo_root, threshold=threshold)
    graphs: list[ModuleGraph] = []
    for spec in modules:
        graph = build_module_graph(repo_root, spec, exclude_names, exclude_suffixes)
        graphs.append(graph)
        builder.add_module(graph)
    builder.add_class_d(class_d)
    builder.notes.append(
        "methodology-full argv는 guard 전체(methodology/tests methodology/nitpicker)로 "
        "재정의(P2c 이월 ② 처분). lint/t8/plugins 스위트는 guard 밖 — co-located focused "
        "그룹으로만 신설하고 module-full 계약은 guard 유지."
    )
    builder.notes.append(
        "이 draft는 도구 산출물이며 registry 권위가 아니다 — 반영은 Planner 검토 + "
        "Human 승인 게이트를 거친 S2 소유(자동 승격 금지)."
    )

    draft = builder.draft_registry()
    draft_text = _canonical_text(draft)
    # 형식 준수 자기검증 — enforcement 정본(validation_impact_selector) 재사용
    try:
        _selector.validate_registry(json.loads(draft_text))
    except _selector.SelectorInputError as error:
        raise DerivationInputError(
            [f"derivation:draft_schema_violation:{reason}" for reason in error.reasons]
        ) from error

    sidecar = builder.sidecar(graphs, spec_path_display)
    sidecar_text = _canonical_text(sidecar)

    out_dir.mkdir(parents=True, exist_ok=True)
    draft_path = out_dir / DRAFT_FILENAME
    sidecar_path = out_dir / SIDECAR_FILENAME
    draft_path.write_text(draft_text, encoding="utf-8", newline="\n")
    sidecar_path.write_text(sidecar_text, encoding="utf-8", newline="\n")

    focused_groups = sum(
        1 for record in builder.groups.values() if record.scope_class == "focused"
    )
    return {
        "status": "OK",
        "draft": draft_path.as_posix(),
        "sidecar": sidecar_path.as_posix(),
        "threshold": threshold,
        "group_count": len(builder.groups),
        "focused_group_count": focused_groups,
        "rule_count": len(builder.rules),
    }


def _emit(result: dict[str, object]) -> None:
    stdout = sys.stdout
    if isinstance(stdout, io.TextIOWrapper):
        stdout.reconfigure(encoding="utf-8", newline="\n")
    stdout.write(_compact_json(result) + "\n")
    stdout.flush()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="registry_derivation.py",
        description="T16-P2b2 registry derivation — draft registry + 근거 사이드카 생성기",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    derive = subparsers.add_parser("derive", help="draft registry + 사이드카 산출")
    derive.add_argument("--repo-root", required=True, dest="repo_root")
    derive.add_argument("--module-spec", required=True, dest="module_spec")
    derive.add_argument("--out-dir", required=True, dest="out_dir")
    derive.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help="강등 가드 임계(모듈 테스트 파일 수 대비 비율, 기본 0.5 — 확정값은 Planner 소유)",
    )
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    """CLI 진입점 — 결과는 stdout 마지막 줄 JSON + exit code."""
    parser = _build_parser()
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    except SystemExit as exc:
        if exc.code in (0, None):
            return 0
        _emit({"status": "BLOCKED", "exit_code": EXIT_INPUT, "reasons": ["usage_error"]})
        return EXIT_INPUT
    threshold = float(args.threshold)
    if not (0.0 < threshold <= 1.0):
        _emit(
            {
                "status": "BLOCKED",
                "exit_code": EXIT_INPUT,
                "reasons": ["threshold_out_of_range"],
            }
        )
        return EXIT_INPUT
    try:
        result = run_derivation(
            repo_root=Path(args.repo_root).resolve(),
            spec_path=Path(args.module_spec),
            out_dir=Path(args.out_dir),
            threshold=threshold,
            spec_path_display=str(args.module_spec).replace("\\", "/"),
        )
    except DerivationInputError as error:
        _emit(
            {
                "status": "BLOCKED",
                "exit_code": EXIT_INPUT,
                "reasons": list(error.reasons),
            }
        )
        return EXIT_INPUT
    except Exception as error:  # fail-closed 내부 가드 — enum/exit로만 신호
        _emit(
            {
                "status": "BLOCKED",
                "exit_code": EXIT_INTERNAL,
                "reasons": [f"internal_error:{type(error).__name__}"],
            }
        )
        return EXIT_INTERNAL
    _emit(result)
    return EXIT_OK


def main() -> int:
    return run(None)


if __name__ == "__main__":
    sys.exit(main())
