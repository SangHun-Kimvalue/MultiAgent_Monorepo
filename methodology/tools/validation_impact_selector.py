#!/usr/bin/env python3
"""T16-P2a 결정론 read-only validation impact selector.

구조화 changed-path artifact와 사람 소유 registry를 exact/prefix 규칙으로
매핑해 test group 추천(shadow recommendation) artifact를 stdout 마지막 줄
canonical UTF-8 JSON 한 개로 출력한다.

경계(R5 · fail-closed):

- 테스트 실행·Git 조회·network·LLM 호출·파일 쓰기 없음. 입력 두 파일의
  raw bytes 읽기와 stdout 한 줄이 I/O의 전부다.
- 정상 완전 매핑 = ``PASS``/exit 0. 이는 recommendation artifact 생성 성공일
  뿐 ITERATION_PASS/Phase PASS/selector recall/production 안전성 주장이 아니다
  (출력의 ``not_claimed``가 이를 구조화로 고정한다).
- malformed/schema/empty/unmatched/unknown kind·ref·path = ``BLOCKED``/exit 2.
  위험한 입력을 자동 교정하지 않고, 부분 추천 PASS를 만들지 않는다.
- 예기치 않은 내부 오류 = ``BLOCKED``/exit 70. ``CHANGES_REQUESTED``/1은
  생성하지 않는다.
- 호출자는 ``status`` enum + exit code만 분기한다(prose 의미판정 금지).

prefix match는 segment-aware다: trailing ``/``가 없는 value는 ``value`` 자체
또는 ``value + "/"``로 시작하는 경로만 매칭한다(naive ``startswith``의
``methodology`` → ``methodology2/...`` 오매칭 차단).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 1
MODE = "SHADOW_RECOMMENDATION"
STATUS_PASS = "PASS"
STATUS_BLOCKED = "BLOCKED"
EXIT_PASS = 0
EXIT_BLOCKED_INPUT = 2
EXIT_BLOCKED_INTERNAL = 70
NOT_CLAIMED: tuple[str, ...] = (
    "phase_pass",
    "selector_recall",
    "production_safety",
    "token_saving",
)
SCOPE_CLASSES: frozenset[str] = frozenset(
    {"focused", "module-full", "repository-full"}
)
MATCH_KINDS: frozenset[str] = frozenset({"exact", "prefix"})
CHANGED_PATHS_FIELDS: frozenset[str] = frozenset({"schema_version", "paths"})
REGISTRY_FIELDS: frozenset[str] = frozenset({"schema_version", "groups", "rules"})
GROUP_FIELDS: frozenset[str] = frozenset({"id", "cwd", "argv", "scope_class"})
RULE_FIELDS: frozenset[str] = frozenset({"id", "match", "groups"})
MATCH_FIELDS: frozenset[str] = frozenset({"kind", "value"})

_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


class SelectorInputError(ValueError):
    """fail-closed 입력 오류 — 구조화 사유 목록을 보존한다."""

    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons: tuple[str, ...] = tuple(sorted(set(reasons)))
        super().__init__("; ".join(self.reasons))


@dataclass(frozen=True)
class Group:
    """registry test group — selector는 구조만 보존하고 실행하지 않는다."""

    group_id: str
    cwd: str
    argv: tuple[str, ...]
    scope_class: str


@dataclass(frozen=True)
class Rule:
    """registry 매칭 규칙 — kind는 closed enum(exact|prefix)."""

    rule_id: str
    kind: str
    value: str
    group_ids: tuple[str, ...]


def _repo_path_error(
    value: object,
    *,
    allow_dot: bool = False,
    allow_dir_suffix: bool = False,
) -> str | None:
    """repository-relative POSIX 경로 검증. 위반 코드 또는 None을 돌려준다."""
    if not isinstance(value, str):
        return "not_string"
    if value == "":
        return "empty"
    if "\\" in value:
        return "backslash"
    if _DRIVE_PREFIX.match(value):
        return "drive_qualified"
    if value.startswith("/"):
        return "absolute"
    if value == ".":
        return None if allow_dot else "dot"
    candidate = value
    if candidate.endswith("/"):
        if not allow_dir_suffix:
            return "trailing_slash"
        candidate = candidate[:-1]
        if candidate == "":
            return "empty"
    for segment in candidate.split("/"):
        if segment == "":
            return "empty_segment"
        if segment == "..":
            return "traversal"
        if segment == ".":
            return "dot_segment"
    return None


def _check_schema_version(
    doc: dict[str, object], label: str, reasons: list[str]
) -> None:
    if "schema_version" not in doc:
        reasons.append(f"{label}:missing_field:schema_version")
        return
    version = doc.get("schema_version")
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        reasons.append(f"{label}:unsupported_schema_version")


def validate_changed_paths(doc: object) -> list[str]:
    """changed-path artifact v1 검증. 위반 시 SelectorInputError."""
    if not isinstance(doc, dict):
        raise SelectorInputError(["changed_paths:not_object"])
    reasons: list[str] = []
    for key in doc:
        if key not in CHANGED_PATHS_FIELDS:
            reasons.append(f"changed_paths:unknown_field:{key}")
    _check_schema_version(doc, "changed_paths", reasons)
    paths: list[str] = []
    if "paths" not in doc:
        reasons.append("changed_paths:missing_field:paths")
    else:
        paths_value = doc.get("paths")
        if not isinstance(paths_value, list):
            reasons.append("changed_paths:paths_not_array")
        elif not paths_value:
            reasons.append("changed_paths:paths_empty")
        else:
            seen: set[str] = set()
            for index, entry in enumerate(paths_value):
                if not isinstance(entry, str):
                    reasons.append(
                        f"changed_paths:invalid_path:not_string:index-{index}"
                    )
                    continue
                error = _repo_path_error(entry)
                if error is not None:
                    reasons.append(f"changed_paths:invalid_path:{error}:{entry}")
                    continue
                if entry in seen:
                    reasons.append(f"changed_paths:duplicate_path:{entry}")
                    continue
                seen.add(entry)
                paths.append(entry)
    if reasons:
        raise SelectorInputError(reasons)
    return paths


def _validate_group(entry: object, index: int, reasons: list[str]) -> Group | None:
    if not isinstance(entry, dict):
        reasons.append(f"registry:group_not_object:index-{index}")
        return None
    for key in entry:
        if key not in GROUP_FIELDS:
            reasons.append(f"registry:group_unknown_field:index-{index}:{key}")
    group_id = entry.get("id")
    if not isinstance(group_id, str) or group_id == "":
        reasons.append(f"registry:group_invalid_id:index-{index}")
    cwd = entry.get("cwd")
    cwd_error = _repo_path_error(cwd, allow_dot=True)
    if cwd_error is not None:
        reasons.append(f"registry:group_invalid_cwd:{cwd_error}:index-{index}")
    argv_value = entry.get("argv")
    argv: tuple[str, ...] = ()
    if (
        not isinstance(argv_value, list)
        or not argv_value
        or not all(isinstance(item, str) and item != "" for item in argv_value)
    ):
        reasons.append(f"registry:group_invalid_argv:index-{index}")
    else:
        argv = tuple(argv_value)
    scope_class = entry.get("scope_class")
    if not isinstance(scope_class, str) or scope_class not in SCOPE_CLASSES:
        reasons.append(f"registry:group_invalid_scope_class:index-{index}")
    if (
        isinstance(group_id, str)
        and group_id != ""
        and isinstance(cwd, str)
        and cwd_error is None
        and argv
        and isinstance(scope_class, str)
        and scope_class in SCOPE_CLASSES
    ):
        return Group(group_id=group_id, cwd=cwd, argv=argv, scope_class=scope_class)
    return None


def _validate_rule(
    entry: object,
    index: int,
    known_group_ids: frozenset[str],
    reasons: list[str],
) -> Rule | None:
    if not isinstance(entry, dict):
        reasons.append(f"registry:rule_not_object:index-{index}")
        return None
    for key in entry:
        if key not in RULE_FIELDS:
            reasons.append(f"registry:rule_unknown_field:index-{index}:{key}")
    rule_id = entry.get("id")
    if not isinstance(rule_id, str) or rule_id == "":
        reasons.append(f"registry:rule_invalid_id:index-{index}")
    label = rule_id if isinstance(rule_id, str) and rule_id != "" else f"index-{index}"
    kind: str | None = None
    value: str | None = None
    match = entry.get("match")
    if not isinstance(match, dict):
        reasons.append(f"registry:rule_invalid_match:{label}")
    else:
        for key in match:
            if key not in MATCH_FIELDS:
                reasons.append(f"registry:rule_match_unknown_field:{label}:{key}")
        kind_value = match.get("kind")
        if not isinstance(kind_value, str) or kind_value not in MATCH_KINDS:
            reasons.append(f"registry:unknown_match_kind:{label}")
        else:
            kind = kind_value
        raw_value = match.get("value")
        value_error = _repo_path_error(
            raw_value, allow_dir_suffix=(kind == "prefix")
        )
        if value_error is not None:
            reasons.append(f"registry:rule_invalid_match_value:{value_error}:{label}")
        elif isinstance(raw_value, str):
            value = raw_value
    refs: list[str] = []
    groups_value = entry.get("groups")
    if (
        not isinstance(groups_value, list)
        or not groups_value
        or not all(isinstance(item, str) and item != "" for item in groups_value)
    ):
        reasons.append(f"registry:rule_invalid_groups:{label}")
    else:
        seen: set[str] = set()
        for ref in groups_value:
            if ref in seen:
                reasons.append(f"registry:rule_duplicate_group_ref:{label}:{ref}")
                continue
            seen.add(ref)
            if ref not in known_group_ids:
                reasons.append(f"registry:unknown_group_ref:{label}:{ref}")
                continue
            refs.append(ref)
    if (
        isinstance(rule_id, str)
        and rule_id != ""
        and kind is not None
        and value is not None
        and refs
        and isinstance(groups_value, list)
        and len(refs) == len(groups_value)
    ):
        return Rule(rule_id=rule_id, kind=kind, value=value, group_ids=tuple(refs))
    return None


def validate_registry(doc: object) -> tuple[list[Group], list[Rule]]:
    """registry v1 검증 후 unique id 오름차순 canonical sort로 돌려준다."""
    if not isinstance(doc, dict):
        raise SelectorInputError(["registry:not_object"])
    reasons: list[str] = []
    for key in doc:
        if key not in REGISTRY_FIELDS:
            reasons.append(f"registry:unknown_field:{key}")
    _check_schema_version(doc, "registry", reasons)

    groups: list[Group] = []
    group_ids: set[str] = set()
    if "groups" not in doc:
        reasons.append("registry:missing_field:groups")
    else:
        groups_value = doc.get("groups")
        if not isinstance(groups_value, list):
            reasons.append("registry:groups_not_array")
        else:
            for index, entry in enumerate(groups_value):
                group = _validate_group(entry, index, reasons)
                if group is None:
                    continue
                if group.group_id in group_ids:
                    reasons.append(f"registry:duplicate_group_id:{group.group_id}")
                    continue
                group_ids.add(group.group_id)
                groups.append(group)

    rules: list[Rule] = []
    rule_ids: set[str] = set()
    if "rules" not in doc:
        reasons.append("registry:missing_field:rules")
    else:
        rules_value = doc.get("rules")
        if not isinstance(rules_value, list):
            reasons.append("registry:rules_not_array")
        else:
            known = frozenset(group_ids)
            for index, entry in enumerate(rules_value):
                rule = _validate_rule(entry, index, known, reasons)
                if rule is None:
                    continue
                if rule.rule_id in rule_ids:
                    reasons.append(f"registry:duplicate_rule_id:{rule.rule_id}")
                    continue
                rule_ids.add(rule.rule_id)
                rules.append(rule)

    if reasons:
        raise SelectorInputError(reasons)
    groups.sort(key=lambda group: group.group_id)
    rules.sort(key=lambda rule: rule.rule_id)
    return groups, rules


def _rule_matches(rule: Rule, path: str) -> bool:
    if rule.kind == "exact":
        return path == rule.value
    if rule.value.endswith("/"):
        return path.startswith(rule.value)
    return path == rule.value or path.startswith(rule.value + "/")


def build_matches(
    paths: Sequence[str], groups: Sequence[Group], rules: Sequence[Rule]
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """모든 path의 완전 매핑을 요구한다 — unmatched 하나라도 있으면 전체 실패."""
    reasons: list[str] = []
    matches: list[dict[str, object]] = []
    recommended_ids: set[str] = set()
    for path in sorted(paths):
        matched_rules = [rule for rule in rules if _rule_matches(rule, path)]
        if not matched_rules:
            reasons.append(f"selector:unmatched_path:{path}")
            continue
        rule_ids = sorted({rule.rule_id for rule in matched_rules})
        group_ids = sorted(
            {group_id for rule in matched_rules for group_id in rule.group_ids}
        )
        recommended_ids.update(group_ids)
        matches.append(
            {"path": path, "rule_ids": rule_ids, "group_ids": group_ids}
        )
    if reasons:
        raise SelectorInputError(reasons)
    group_by_id = {group.group_id: group for group in groups}
    recommended_groups: list[dict[str, object]] = [
        {
            "id": group.group_id,
            "cwd": group.cwd,
            "argv": list(group.argv),
            "scope_class": group.scope_class,
        }
        for group in (
            group_by_id[group_id] for group_id in sorted(recommended_ids)
        )
    ]
    return matches, recommended_groups


def _blocked_result(
    exit_code: int,
    reasons: Sequence[str],
    registry_sha256: str | None,
    changed_paths_sha256: str | None,
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": STATUS_BLOCKED,
        "exit_code": exit_code,
        "mode": MODE,
        "registry_sha256": registry_sha256,
        "changed_paths_sha256": changed_paths_sha256,
        "matches": [],
        "recommended_groups": [],
        "reasons": sorted(set(reasons)),
        "not_claimed": list(NOT_CLAIMED),
    }


def _read_bytes(path: Path, label: str, reasons: list[str]) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        reasons.append(f"{label}:unreadable:{path.as_posix()}")
        return None


def _parse_json(data: bytes, label: str, reasons: list[str]) -> object:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        reasons.append(f"{label}:not_utf8")
        return None
    try:
        parsed: object = json.loads(text)
    except json.JSONDecodeError:
        reasons.append(f"{label}:malformed_json")
        return None
    return parsed


def run_selector(
    changed_paths_file: Path, registry_file: Path
) -> tuple[dict[str, object], int]:
    """입력 두 파일을 읽어 (result JSON object, exit code)를 돌려준다.

    입력 위반은 전부 BLOCKED/2 result로 수렴한다. 예기치 않은 내부 오류만
    예외로 전파되며 ``run``이 BLOCKED/70으로 감싼다.
    """
    io_reasons: list[str] = []
    changed_bytes = _read_bytes(changed_paths_file, "changed_paths", io_reasons)
    registry_bytes = _read_bytes(registry_file, "registry", io_reasons)
    changed_sha256 = (
        hashlib.sha256(changed_bytes).hexdigest() if changed_bytes is not None else None
    )
    registry_sha256 = (
        hashlib.sha256(registry_bytes).hexdigest()
        if registry_bytes is not None
        else None
    )
    if io_reasons:
        return (
            _blocked_result(
                EXIT_BLOCKED_INPUT, io_reasons, registry_sha256, changed_sha256
            ),
            EXIT_BLOCKED_INPUT,
        )
    assert changed_bytes is not None and registry_bytes is not None

    parse_reasons: list[str] = []
    changed_doc = _parse_json(changed_bytes, "changed_paths", parse_reasons)
    registry_doc = _parse_json(registry_bytes, "registry", parse_reasons)
    if parse_reasons:
        return (
            _blocked_result(
                EXIT_BLOCKED_INPUT, parse_reasons, registry_sha256, changed_sha256
            ),
            EXIT_BLOCKED_INPUT,
        )

    validation_reasons: list[str] = []
    paths: list[str] = []
    groups: list[Group] = []
    rules: list[Rule] = []
    try:
        paths = validate_changed_paths(changed_doc)
    except SelectorInputError as error:
        validation_reasons.extend(error.reasons)
    try:
        groups, rules = validate_registry(registry_doc)
    except SelectorInputError as error:
        validation_reasons.extend(error.reasons)
    if validation_reasons:
        return (
            _blocked_result(
                EXIT_BLOCKED_INPUT,
                validation_reasons,
                registry_sha256,
                changed_sha256,
            ),
            EXIT_BLOCKED_INPUT,
        )

    try:
        matches, recommended_groups = build_matches(paths, groups, rules)
    except SelectorInputError as error:
        return (
            _blocked_result(
                EXIT_BLOCKED_INPUT, error.reasons, registry_sha256, changed_sha256
            ),
            EXIT_BLOCKED_INPUT,
        )

    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "status": STATUS_PASS,
        "exit_code": EXIT_PASS,
        "mode": MODE,
        "registry_sha256": registry_sha256,
        "changed_paths_sha256": changed_sha256,
        "matches": matches,
        "recommended_groups": recommended_groups,
        "reasons": [],
        "not_claimed": list(NOT_CLAIMED),
    }
    return result, EXIT_PASS


def canonical_json(result: dict[str, object]) -> str:
    """canonical 직렬화: UTF-8, key 정렬, 최소 구분자, ASCII escape 없음."""
    return json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _emit(result: dict[str, object]) -> None:
    stdout = sys.stdout
    if isinstance(stdout, io.TextIOWrapper):
        stdout.reconfigure(encoding="utf-8", newline="\n")
    stdout.write(canonical_json(result) + "\n")
    stdout.flush()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="validation_impact_selector.py",
        description=(
            "결정론 read-only validation impact selector — "
            "changed-path artifact + registry -> shadow recommendation JSON"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    recommend = subparsers.add_parser(
        "recommend",
        help="changed-path artifact와 registry로 test group 추천 artifact 생성",
    )
    recommend.add_argument("--changed-paths", required=True, dest="changed_paths")
    recommend.add_argument("--registry", required=True, dest="registry")
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    """CLI 진입점. stdout 마지막 줄 canonical JSON + exit code로만 신호한다."""
    parser = _build_parser()
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    except SystemExit as exc:
        if exc.code in (0, None):
            return 0
        _emit(_blocked_result(EXIT_BLOCKED_INPUT, ["usage_error"], None, None))
        return EXIT_BLOCKED_INPUT
    try:
        result, exit_code = run_selector(
            Path(args.changed_paths), Path(args.registry)
        )
    except Exception as error:  # fail-closed 내부 가드 — enum/exit로만 신호
        result = _blocked_result(
            EXIT_BLOCKED_INTERNAL,
            [f"internal_error:{type(error).__name__}"],
            None,
            None,
        )
        exit_code = EXIT_BLOCKED_INTERNAL
    _emit(result)
    return exit_code


def main() -> int:
    return run(None)


if __name__ == "__main__":
    sys.exit(main())
