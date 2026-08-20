"""T13 Goal/Intent audit ledger의 결정론적 opt-in writer.

이 모듈은 phase 상태나 자연어 의미를 판정하지 않는다. 기존 relay report와
``ztr_envelope_v2`` bytes를 write-once artifact로 보존하고, 그 사실을 P1 checker가
읽을 수 있는 canonical JSONL entry로 연결하는 역할만 맡는다.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NoReturn

from src.config.loader import DEFAULT_CONFIG_PATH
from src.config.schema import RoundtableConfig
from src.engine.fix_feedback import (
    load_report_payload,
    select_accepted_findings,
)
from src.engine.reapply_ledger import (
    ReapplyLedger,
    findings_digest,
    gate_check,
    validate_max_rounds,
)
from src.engine.resume_chain import SessionMap
from src.engine.static_review import run_subprocess_tool
from src.envelope import Envelope


JsonObject = dict[str, Any]
CommandKind = Literal["run-phase", "fix-round"]

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_INVALID_PORTABLE = re.compile(r'[<>:"/\\|?*]|[\x00-\x1f\x7f]')
_RESERVED_DEVICE = re.compile(
    r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.IGNORECASE
)
_CONTEXT_KEYS = {
    "schema_version",
    "ledger_path",
    "contract",
    "phase_ledger_path",
    "report_artifact",
    "envelope_artifact",
    "claimed_pass",
    "not_claimed",
    "claim_evidence_links",
}
_CONTRACT_KEYS = {
    "schema_version",
    "contract_id",
    "revision",
    "parent_contract_digest",
    "objective",
    "non_goals",
    "allowed_write_scope",
    "forbidden_outcomes",
    "validation_claims",
    "required_artifacts",
    "required_verdict_sources",
    "contract_digest",
}
_ENTRY_KEYS = {
    "schema_version",
    "sequence",
    "phase_id",
    "round_id",
    "leg_id",
    "role",
    "action_type",
    "contract_ref",
    "phase_ledger_ref",
    "changed_paths",
    "artifact_refs",
    "claimed_pass",
    "not_claimed",
    "claim_evidence_links",
    "previous_entry_digest",
    "entry_digest",
}
_ROLES = frozenset(
    {"ORCHESTRATOR", "PLANNER", "IMPLEMENTER", "REVIEWER", "MECHANICAL", "HUMAN"}
)
_ACTIONS = frozenset(
    {
        "CONTRACT_ACTIVATED",
        "LEG_RECORDED",
        "ROUND_RECORDED",
        "CHECKPOINT_RECORDED",
        "CLOSEOUT_RECORDED",
    }
)


class GoalIntentError(RuntimeError):
    """닫힌 T13 writer 오류 코드."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class _StrictJsonError(ValueError):
    pass


def _raise_constant(value: str) -> NoReturn:
    raise _StrictJsonError(f"non-finite JSON number: {value}")


def _pairs_object(pairs: list[tuple[str, Any]]) -> JsonObject:
    value: JsonObject = {}
    for key, item in pairs:
        if key in value:
            raise _StrictJsonError(f"duplicate key: {key}")
        value[key] = item
    return value


def _has_surrogate(value: Any) -> bool:
    if isinstance(value, str):
        return any(0xD800 <= ord(char) <= 0xDFFF for char in value)
    if isinstance(value, list):
        return any(_has_surrogate(item) for item in value)
    if isinstance(value, dict):
        return any(_has_surrogate(key) or _has_surrogate(item) for key, item in value.items())
    return False


def canonical_json(value: JsonObject) -> bytes:
    """P1과 동일한 canonical JSON object bytes(LF 제외)."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_json_line(value: JsonObject) -> bytes:
    return canonical_json(value) + b"\n"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _object_digest(value: JsonObject, self_field: str) -> str:
    return _sha256(canonical_json({key: item for key, item in value.items() if key != self_field}))


def _strict_json(data: bytes) -> JsonObject:
    if data.startswith(b"\xef\xbb\xbf"):
        raise _StrictJsonError("UTF-8 BOM is forbidden")
    try:
        text = data.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_pairs_object,
            parse_constant=_raise_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _StrictJsonError, ValueError) as exc:
        raise _StrictJsonError(str(exc)) from exc
    if not isinstance(value, dict) or _has_surrogate(value):
        raise _StrictJsonError("top-level JSON must be an object without surrogates")
    return value


def _strict_canonical_json_file(path: Path) -> tuple[JsonObject, bytes]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", str(exc)) from exc
    try:
        value = _strict_json(data)
    except _StrictJsonError as exc:
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", str(exc)) from exc
    if data != canonical_json_line(value):
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", "context is not canonical JSON+LF")
    return value, data


def _exact_keys(value: Any, expected: set[str], label: str) -> JsonObject:
    if not isinstance(value, dict) or set(value) != expected:
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", f"{label} keys are not exact")
    return value


def _valid_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise GoalIntentError("GOAL_INTENT_ID_INVALID", label)
    return value


def _ledger_invalid(detail: str) -> NoReturn:
    raise GoalIntentError("GOAL_INTENT_LEDGER_INVALID", detail)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _ledger_exact_keys(value: Any, expected: set[str], label: str) -> JsonObject:
    if not isinstance(value, dict) or set(value) != expected:
        _ledger_invalid(f"{label} keys are not exact")
    return value


def _ledger_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        _ledger_invalid(label)
    return value


def _ledger_hex(value: Any, label: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or _HEX_RE.fullmatch(value) is None:
        _ledger_invalid(label)
    return value


def _normalize_repo_path(value: str) -> str | None:
    raw = value.strip().replace("\\", "/")
    if not raw or re.match(r"^[A-Za-z]:", raw) or raw.startswith("/"):
        return None
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


def _ledger_repo_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or _normalize_repo_path(value) != value:
        _ledger_invalid(label)
    return value


def _ledger_sorted_strings(value: Any, label: str, *, ids: bool = False) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        _ledger_invalid(label)
    items = list(value)
    if ids:
        for item in items:
            _ledger_id(item, label)
    if items != sorted(set(items)):
        _ledger_invalid(f"{label} is not sorted unique")
    return items


def _ledger_sorted_objects(value: Any, key: str, label: str) -> list[JsonObject]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        _ledger_invalid(label)
    objects = list(value)
    keys = [_ledger_id(item.get(key), f"{label}.{key}") for item in objects]
    if keys != sorted(set(keys)):
        _ledger_invalid(f"{label} is not sorted unique")
    return objects


def _portable_phase_id(value: Any) -> str:
    phase_id = _valid_id(value, "phase_id")
    if (
        _INVALID_PORTABLE.search(phase_id)
        or phase_id.endswith((".", " "))
        or _RESERVED_DEVICE.fullmatch(phase_id)
    ):
        raise GoalIntentError("GOAL_INTENT_ID_INVALID", "phase_id is not portable")
    return phase_id


def _repo_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", label)
    if "\\" in value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", label)
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", label)
    return value


def _sorted_unique_ids(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", label)
    items = tuple(_valid_id(item, label) for item in value)
    if list(items) != sorted(set(items)):
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", f"{label} is not sorted unique")
    return items


@dataclass(frozen=True)
class ArtifactSpec:
    artifact_id: str
    artifact_type: str
    logical_path: str
    path: Path


@dataclass(frozen=True)
class GoalIntentContext:
    context_path: Path
    context_logical_path: str
    ledger_logical_path: str
    ledger_path: Path
    contract_id: str
    contract_revision: int
    contract_logical_path: str
    contract_path: Path
    phase_ledger_logical_path: str
    phase_ledger_path: Path
    report: ArtifactSpec
    envelope: ArtifactSpec
    claimed_pass: tuple[str, ...]
    not_claimed: tuple[str, ...]
    claim_evidence_links: tuple[JsonObject, ...]


@dataclass(frozen=True)
class GoalIntentPreflight:
    kind: CommandKind
    process_cwd: Path
    repo_root: Path
    context: GoalIntentContext
    phase_id: str
    round_index: int
    action_type: str
    round_id: str
    leg_id: str
    ledger_bytes: bytes | None
    entries: tuple[JsonObject, ...]
    protected_bytes: tuple[tuple[Path, bytes | None], ...]
    relay_mutable_paths: frozenset[Path]
    excluded_paths: frozenset[str]

    def assert_inputs_unchanged(self, *, after_relay: bool = False) -> None:
        for path, expected in self.protected_bytes:
            if after_relay and path in self.relay_mutable_paths:
                continue
            try:
                actual = path.read_bytes() if path.exists() else None
            except OSError as exc:
                raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", str(exc)) from exc
            if actual != expected:
                raise GoalIntentError(
                    "GOAL_INTENT_CONTEXT_INVALID", f"protected input changed: {path.name}"
                )


@dataclass(frozen=True)
class _ValidationClaim:
    artifact_ids: frozenset[str]
    verdict_source_ids: frozenset[str]


@dataclass(frozen=True)
class _ContractSemantics:
    contract_ref: JsonObject
    allowed_write_scope: tuple[str, ...]
    claims: dict[str, _ValidationClaim]
    artifacts: dict[str, JsonObject]
    verdict_sources: dict[str, JsonObject]


def _physical(path: Path, *, base: Path | None = None) -> Path:
    effective = path if path.is_absolute() else (base / path if base is not None else path)
    return effective.resolve(strict=False)


def _path_key(path: Path) -> tuple[str, ...]:
    return Path(os.path.normcase(str(path))).parts


def _overlaps(left: Path, right: Path) -> bool:
    left_parts = _path_key(left)
    right_parts = _path_key(right)
    shortest = min(len(left_parts), len(right_parts))
    return left_parts[:shortest] == right_parts[:shortest]


def _inside(path: Path, root: Path) -> bool:
    root_parts = _path_key(root)
    path_parts = _path_key(path)
    return len(path_parts) >= len(root_parts) and path_parts[: len(root_parts)] == root_parts


def _derived_tmp(path: Path) -> Path:
    return path.with_name(f"{path.name}.tmp")


def _ledger_lock(path: Path) -> Path:
    return path.with_name(f"{path.name}.lock")


def _pid_tmp(path: Path) -> Path:
    return path.with_name(f".{path.name}.{os.getpid()}.tmp")


def _append_suffix(path: Path, suffix: str) -> Path:
    return Path(f"{path}{suffix}")


def _path_entry_exists(path: Path) -> bool:
    """Broken symlink까지 포함해 namespace node의 선점을 감지한다."""
    return os.path.lexists(os.fspath(path))


def _glob_regex(pattern: str) -> str:
    """Committed P1 checker와 같은 repo-relative glob 번역."""
    pieces: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                index += 1
                if index + 1 < len(pattern) and pattern[index + 1] == "/":
                    index += 1
                    pieces.append("(?:.*/)?")
                else:
                    pieces.append(".*")
            else:
                pieces.append("[^/]*")
        elif char == "?":
            pieces.append("[^/]")
        else:
            pieces.append(re.escape(char))
        index += 1
    return "".join(pieces)


def _path_matches(path: str, pattern: str) -> bool:
    normalized_path = _normalize_repo_path(path)
    normalized_pattern = _normalize_repo_path(pattern)
    if normalized_path is None or normalized_pattern is None:
        return False
    if any(char in normalized_pattern for char in "*?"):
        return bool(re.fullmatch(_glob_regex(normalized_pattern), normalized_path))
    return normalized_path == normalized_pattern or normalized_path.startswith(
        f"{normalized_pattern}/"
    )


def _require_absent(paths: list[tuple[str, Path]]) -> None:
    for label, path in paths:
        if _path_entry_exists(path):
            raise GoalIntentError(
                "GOAL_INTENT_ARTIFACT_COLLISION", f"pre-existing write node: {label}"
            )


def _artifact_spec(value: Any, *, repo_root: Path, label: str) -> ArtifactSpec:
    row = _exact_keys(value, {"artifact_id", "artifact_type", "path"}, label)
    logical = _repo_path(row["path"], f"{label}.path")
    path = _physical(Path(logical), base=repo_root)
    if not _inside(path, repo_root):
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", f"{label} escapes repo")
    return ArtifactSpec(
        artifact_id=_valid_id(row["artifact_id"], f"{label}.artifact_id"),
        artifact_type=_valid_id(row["artifact_type"], f"{label}.artifact_type"),
        logical_path=logical,
        path=path,
    )


def _load_context(path: Path, *, logical_path: str, repo_root: Path) -> tuple[GoalIntentContext, bytes]:
    value, data = _strict_canonical_json_file(path)
    _exact_keys(value, _CONTEXT_KEYS, "context")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", "schema_version")
    contract = _exact_keys(value["contract"], {"contract_id", "revision", "path"}, "contract")
    revision = contract["revision"]
    if type(revision) is not int or revision < 1:
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", "contract.revision")
    ledger_logical = _repo_path(value["ledger_path"], "ledger_path")
    contract_logical = _repo_path(contract["path"], "contract.path")
    phase_ledger_logical = _repo_path(value["phase_ledger_path"], "phase_ledger_path")
    ledger_path = _physical(Path(ledger_logical), base=repo_root)
    contract_path = _physical(Path(contract_logical), base=repo_root)
    phase_ledger_path = _physical(Path(phase_ledger_logical), base=repo_root)
    if not all(_inside(item, repo_root) for item in (ledger_path, contract_path, phase_ledger_path)):
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", "declared path escapes repo")
    report = _artifact_spec(value["report_artifact"], repo_root=repo_root, label="report_artifact")
    envelope = _artifact_spec(
        value["envelope_artifact"], repo_root=repo_root, label="envelope_artifact"
    )
    if report.artifact_id == envelope.artifact_id:
        raise GoalIntentError("GOAL_INTENT_ARTIFACT_COLLISION", "duplicate artifact id")
    links_value = value["claim_evidence_links"]
    if not isinstance(links_value, list):
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", "claim_evidence_links")
    links: list[JsonObject] = []
    for raw in links_value:
        row = _exact_keys(raw, {"claim_id", "artifact_ids", "verdict_source_ids"}, "claim link")
        links.append(
            {
                "claim_id": _valid_id(row["claim_id"], "claim_id"),
                "artifact_ids": list(_sorted_unique_ids(row["artifact_ids"], "artifact_ids")),
                "verdict_source_ids": list(
                    _sorted_unique_ids(row["verdict_source_ids"], "verdict_source_ids")
                ),
            }
        )
    if [row["claim_id"] for row in links] != sorted({row["claim_id"] for row in links}):
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", "claim links are not sorted unique")
    return (
        GoalIntentContext(
            context_path=path,
            context_logical_path=logical_path,
            ledger_logical_path=ledger_logical,
            ledger_path=ledger_path,
            contract_id=_valid_id(contract["contract_id"], "contract.contract_id"),
            contract_revision=revision,
            contract_logical_path=contract_logical,
            contract_path=contract_path,
            phase_ledger_logical_path=phase_ledger_logical,
            phase_ledger_path=phase_ledger_path,
            report=report,
            envelope=envelope,
            claimed_pass=_sorted_unique_ids(value["claimed_pass"], "claimed_pass"),
            not_claimed=_sorted_unique_ids(value["not_claimed"], "not_claimed"),
            claim_evidence_links=tuple(links),
        ),
        data,
    )


def _contract_sorted_objects(value: Any, key: str, label: str) -> list[JsonObject]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", label)
    rows = list(value)
    keys = [_valid_id(row.get(key), f"{label}.{key}") for row in rows]
    if keys != sorted(set(keys)):
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", f"{label} is not sorted unique")
    return rows


def _contract_sorted_paths(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", label)
    paths = tuple(value)
    if list(paths) != sorted(set(paths)) or any(
        _normalize_repo_path(item) != item for item in paths
    ):
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", label)
    return paths


def _contract_sorted_strings(
    value: Any, label: str, *, ids: bool = False
) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", label)
    items = tuple(value)
    if ids:
        for item in items:
            _valid_id(item, label)
    if list(items) != sorted(set(items)):
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", f"{label} is not sorted unique")
    return items


def _load_contract_semantics(context: GoalIntentContext) -> _ContractSemantics:
    """현재 contract에서 P1이 ledger 의미 검증에 쓰는 선언만 고정한다."""
    value, data = _strict_canonical_json_file(context.contract_path)
    _exact_keys(value, _CONTRACT_KEYS, "contract")
    if value["schema_version"] != 1 or not _is_int(value["schema_version"]):
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", "contract.schema_version")
    contract_id = _valid_id(value["contract_id"], "contract.contract_id")
    revision = value["revision"]
    if not _is_int(revision) or revision < 1:
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", "contract.revision")
    if contract_id != context.contract_id or revision != context.contract_revision:
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", "context/contract identity mismatch")
    parent = value["parent_contract_digest"]
    if revision == 1:
        if parent is not None:
            raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", "contract.parent_contract_digest")
    elif not isinstance(parent, str) or _HEX_RE.fullmatch(parent) is None:
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", "contract.parent_contract_digest")
    if not isinstance(value["objective"], str) or not value["objective"]:
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", "contract.objective")
    _contract_sorted_strings(value["non_goals"], "contract.non_goals")
    allowed_write_scope = _contract_sorted_paths(
        value["allowed_write_scope"], "contract.allowed_write_scope"
    )
    _contract_sorted_strings(
        value["forbidden_outcomes"], "contract.forbidden_outcomes", ids=True
    )

    artifacts: dict[str, JsonObject] = {}
    for row in _contract_sorted_objects(
        value["required_artifacts"], "artifact_id", "contract.required_artifacts"
    ):
        exact = _exact_keys(row, {"artifact_id", "artifact_type", "path"}, "required artifact")
        artifact_id = _valid_id(exact["artifact_id"], "required artifact.id")
        artifact_type = _valid_id(exact["artifact_type"], "required artifact.type")
        path = _repo_path(exact["path"], "required artifact.path")
        artifacts[artifact_id] = {
            "artifact_id": artifact_id,
            "artifact_type": artifact_type,
            "path": path,
        }

    verdict_sources: dict[str, JsonObject] = {}
    for row in _contract_sorted_objects(
        value["required_verdict_sources"],
        "verdict_source_id",
        "contract.required_verdict_sources",
    ):
        exact = _exact_keys(
            row,
            {
                "verdict_source_id",
                "artifact_id",
                "adapter",
                "required_status",
                "required_exit_code",
            },
            "required verdict source",
        )
        source_id = _valid_id(exact["verdict_source_id"], "verdict source.id")
        artifact_id = _valid_id(exact["artifact_id"], "verdict source.artifact_id")
        if artifact_id not in artifacts:
            raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", "verdict artifact undeclared")
        _valid_id(exact["adapter"], "verdict source.adapter")
        if exact["required_status"] != "PASS" or exact["required_exit_code"] != 0:
            raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", "verdict source contract")
        verdict_sources[source_id] = dict(exact)

    claims: dict[str, _ValidationClaim] = {}
    for row in _contract_sorted_objects(
        value["validation_claims"], "claim_id", "contract.validation_claims"
    ):
        exact = _exact_keys(
            row,
            {"claim_id", "required_artifact_ids", "required_verdict_source_ids"},
            "validation claim",
        )
        claim_id = _valid_id(exact["claim_id"], "validation claim.id")
        artifact_ids = frozenset(
            _sorted_unique_ids(exact["required_artifact_ids"], "required_artifact_ids")
        )
        source_ids = frozenset(
            _sorted_unique_ids(
                exact["required_verdict_source_ids"], "required_verdict_source_ids"
            )
        )
        if not artifact_ids.issubset(artifacts) or not source_ids.issubset(verdict_sources):
            raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", "claim dependency undeclared")
        claims[claim_id] = _ValidationClaim(
            artifact_ids=artifact_ids,
            verdict_source_ids=source_ids,
        )

    digest = value["contract_digest"]
    if not isinstance(digest, str) or _HEX_RE.fullmatch(digest) is None:
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", "contract.contract_digest")
    if digest != _object_digest(value, "contract_digest"):
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", "contract digest mismatch")
    return _ContractSemantics(
        contract_ref={
            "contract_id": contract_id,
            "revision": revision,
            "path": context.contract_logical_path,
            "sha256": _sha256(data),
        },
        allowed_write_scope=allowed_write_scope,
        claims=claims,
        artifacts=artifacts,
        verdict_sources=verdict_sources,
    )


def _validate_artifact_ref(value: Any) -> tuple[str, str]:
    row = _ledger_exact_keys(
        value, {"artifact_id", "artifact_type", "path", "sha256"}, "artifact_ref"
    )
    artifact_id = _ledger_id(row["artifact_id"], "artifact_ref.id")
    _ledger_id(row["artifact_type"], "artifact_ref.type")
    path = _ledger_repo_path(row["path"], "artifact_ref.path")
    _ledger_hex(row["sha256"], "artifact_ref.sha256")
    return artifact_id, path


def _validate_contract_ref(value: Any) -> None:
    row = _ledger_exact_keys(
        value, {"contract_id", "revision", "path", "sha256"}, "contract_ref"
    )
    _ledger_id(row["contract_id"], "contract_ref.contract_id")
    if not _is_int(row["revision"]) or row["revision"] < 1:
        _ledger_invalid("contract_ref.revision")
    _ledger_repo_path(row["path"], "contract_ref.path")
    _ledger_hex(row["sha256"], "contract_ref.sha256")


def _validate_phase_ref(value: Any) -> None:
    row = _ledger_exact_keys(
        value, {"artifact_type", "path", "sha256"}, "phase_ledger_ref"
    )
    if row["artifact_type"] != "canonical_phase_ledger_snapshot":
        _ledger_invalid("phase_ledger_ref.artifact_type")
    _ledger_repo_path(row["path"], "phase_ledger_ref.path")
    _ledger_hex(row["sha256"], "phase_ledger_ref.sha256")


def _validate_claim_links(value: Any) -> None:
    rows = _ledger_sorted_objects(value, "claim_id", "claim_evidence_links")
    for row in rows:
        exact = _ledger_exact_keys(
            row, {"claim_id", "artifact_ids", "verdict_source_ids"}, "claim link"
        )
        _ledger_id(exact["claim_id"], "claim link.claim_id")
        _ledger_sorted_strings(exact["artifact_ids"], "claim link.artifact_ids", ids=True)
        _ledger_sorted_strings(
            exact["verdict_source_ids"], "claim link.verdict_source_ids", ids=True
        )


def _validate_entry_semantics(
    entry: JsonObject,
    *,
    contract: _ContractSemantics,
    expected_phase_ref: JsonObject,
) -> None:
    """P1 checker가 기존 entry에서 거부할 의미 불일치를 prewrite로 닫는다."""
    if entry["contract_ref"] != contract.contract_ref:
        _ledger_invalid("contract ref does not match current contract")
    if entry["phase_ledger_ref"] != expected_phase_ref:
        _ledger_invalid("phase ledger ref does not match current snapshot")

    for changed in entry["changed_paths"]:
        if _normalize_repo_path(changed) != changed:
            _ledger_invalid("changed path is not canonical repo-relative")
        if not any(_path_matches(changed, pattern) for pattern in contract.allowed_write_scope):
            _ledger_invalid("changed path is outside current contract scope")

    declared_claims = set(contract.claims)
    claimed_pass = set(entry["claimed_pass"])
    not_claimed = set(entry["not_claimed"])
    if not claimed_pass.issubset(declared_claims) or not not_claimed.issubset(
        declared_claims
    ):
        _ledger_invalid("claim is not declared by current contract")

    refs = {str(row["artifact_id"]): row for row in entry["artifact_refs"]}
    links = {str(row["claim_id"]): row for row in entry["claim_evidence_links"]}
    for claim_id, link in links.items():
        if claim_id not in declared_claims:
            _ledger_invalid("claim evidence link is undeclared")
        artifact_ids = set(link["artifact_ids"])
        if not artifact_ids.issubset(contract.artifacts) or not artifact_ids.issubset(refs):
            _ledger_invalid("claim link artifact is missing")
        if not set(link["verdict_source_ids"]).issubset(contract.verdict_sources):
            _ledger_invalid("claim link verdict source is missing")

    for claim_id in claimed_pass:
        link = links.get(claim_id)
        if link is None:
            _ledger_invalid("claimed_pass has no evidence link")
        claim = contract.claims[claim_id]
        linked_artifacts = set(link["artifact_ids"])
        if not claim.artifact_ids.issubset(linked_artifacts):
            _ledger_invalid("claimed_pass is missing required artifact link")
        for artifact_id in claim.artifact_ids:
            actual = refs.get(artifact_id)
            required = contract.artifacts[artifact_id]
            if actual is None or any(
                actual[key] != required[key]
                for key in ("artifact_id", "artifact_type", "path")
            ):
                _ledger_invalid("claimed_pass artifact identity mismatch")

        linked_sources = set(link["verdict_source_ids"])
        if not claim.verdict_source_ids.issubset(linked_sources):
            _ledger_invalid("claimed_pass is missing required verdict source link")
        for source_id in claim.verdict_source_ids:
            source = contract.verdict_sources[source_id]
            artifact_id = str(source["artifact_id"])
            actual = refs.get(artifact_id)
            required = contract.artifacts[artifact_id]
            if actual is None or any(
                actual[key] != required[key]
                for key in ("artifact_id", "artifact_type", "path")
            ):
                _ledger_invalid("verdict source artifact identity mismatch")


def _validate_context_claims(
    context: GoalIntentContext, contract: _ContractSemantics
) -> None:
    """새 entry도 P1이 즉시 거부할 claim/ref 조합을 만들지 않게 한다."""
    declared = set(contract.claims)
    claimed = set(context.claimed_pass)
    not_claimed = set(context.not_claimed)
    if not claimed.issubset(declared) or not not_claimed.issubset(declared):
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", "undeclared claim")
    current_refs = {
        context.report.artifact_id: {
            "artifact_id": context.report.artifact_id,
            "artifact_type": context.report.artifact_type,
            "path": context.report.logical_path,
        },
        context.envelope.artifact_id: {
            "artifact_id": context.envelope.artifact_id,
            "artifact_type": context.envelope.artifact_type,
            "path": context.envelope.logical_path,
        },
    }
    links = {str(row["claim_id"]): row for row in context.claim_evidence_links}
    for claim_id, link in links.items():
        if claim_id not in declared:
            raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", "undeclared claim link")
        if not set(link["artifact_ids"]).issubset(contract.artifacts) or not set(
            link["artifact_ids"]
        ).issubset(current_refs):
            raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", "missing linked artifact")
        if not set(link["verdict_source_ids"]).issubset(contract.verdict_sources):
            raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", "missing verdict source")
    for claim_id in claimed:
        claim_link = links.get(claim_id)
        if claim_link is None:
            raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", "claimed_pass has no link")
        claim = contract.claims[claim_id]
        if not claim.artifact_ids.issubset(set(claim_link["artifact_ids"])):
            raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", "claim artifact link incomplete")
        if not claim.verdict_source_ids.issubset(
            set(claim_link["verdict_source_ids"])
        ):
            raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", "claim verdict link incomplete")
        identities = set(claim.artifact_ids)
        identities.update(
            str(contract.verdict_sources[source_id]["artifact_id"])
            for source_id in claim.verdict_source_ids
        )
        for artifact_id in identities:
            actual = current_refs.get(artifact_id)
            required = contract.artifacts[artifact_id]
            if actual is None or any(
                actual[key] != required[key]
                for key in ("artifact_id", "artifact_type", "path")
            ):
                raise GoalIntentError(
                    "GOAL_INTENT_CONTEXT_INVALID", "claim artifact identity mismatch"
                )


def _load_ledger(
    path: Path,
    *,
    allow_absent: bool,
) -> tuple[bytes | None, tuple[JsonObject, ...]]:
    if not _path_entry_exists(path):
        if allow_absent:
            return None, ()
        raise GoalIntentError("GOAL_INTENT_LEDGER_INVALID", "fix-round cannot bootstrap ledger")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise GoalIntentError("GOAL_INTENT_LEDGER_INVALID", str(exc)) from exc
    if data.startswith(b"\xef\xbb\xbf") or not data or not data.endswith(b"\n"):
        raise GoalIntentError("GOAL_INTENT_LEDGER_INVALID", "non-canonical JSONL")
    raw_lines = data[:-1].split(b"\n")
    if not raw_lines or any(not line for line in raw_lines):
        raise GoalIntentError("GOAL_INTENT_LEDGER_INVALID", "empty JSONL row")
    entries: list[JsonObject] = []
    previous: str | None = None
    global_artifact_ids: set[str] = set()
    global_artifact_paths: set[str] = set()
    for expected_sequence, line in enumerate(raw_lines, start=1):
        try:
            entry = _strict_json(line)
        except _StrictJsonError as exc:
            raise GoalIntentError("GOAL_INTENT_LEDGER_INVALID", str(exc)) from exc
        action = entry.get("action_type")
        expected_keys = _ENTRY_KEYS | ({"closeout_status"} if action == "CLOSEOUT_RECORDED" else set())
        if set(entry) != expected_keys or line != canonical_json(entry):
            raise GoalIntentError("GOAL_INTENT_LEDGER_INVALID", "entry shape/canonical bytes")
        if entry.get("schema_version") != 1 or not _is_int(entry.get("schema_version")):
            raise GoalIntentError("GOAL_INTENT_LEDGER_INVALID", "schema_version")
        if not _is_int(entry.get("sequence")) or entry.get("sequence") != expected_sequence:
            raise GoalIntentError("GOAL_INTENT_LEDGER_INVALID", "sequence")
        for key in ("phase_id", "round_id", "leg_id"):
            _ledger_id(entry.get(key), key)
        if entry.get("role") not in _ROLES or action not in _ACTIONS:
            raise GoalIntentError("GOAL_INTENT_LEDGER_INVALID", "role/action enum")
        if action == "CLOSEOUT_RECORDED":
            if entry.get("closeout_status") not in {
                "STRUCTURAL_TRAJECTORY_COMPLETE",
                "STRUCTURAL_TRAJECTORY_INCOMPLETE",
            }:
                raise GoalIntentError("GOAL_INTENT_LEDGER_INVALID", "closeout_status")
            raise GoalIntentError("GOAL_INTENT_LEDGER_INVALID", "ledger is closed")
        _validate_contract_ref(entry.get("contract_ref"))
        _validate_phase_ref(entry.get("phase_ledger_ref"))
        _ledger_sorted_strings(entry.get("changed_paths"), "changed_paths")
        claimed = set(
            _ledger_sorted_strings(entry.get("claimed_pass"), "claimed_pass", ids=True)
        )
        not_claimed = set(
            _ledger_sorted_strings(entry.get("not_claimed"), "not_claimed", ids=True)
        )
        if claimed & not_claimed:
            raise GoalIntentError("GOAL_INTENT_LEDGER_INVALID", "claim state conflict")
        _validate_claim_links(entry.get("claim_evidence_links"))
        _ledger_hex(entry.get("previous_entry_digest"), "previous digest", nullable=True)
        if entry.get("previous_entry_digest") != previous:
            raise GoalIntentError("GOAL_INTENT_LEDGER_INVALID", "previous digest")
        digest = entry.get("entry_digest")
        _ledger_hex(digest, "entry digest")
        if digest != _object_digest(entry, "entry_digest"):
            raise GoalIntentError("GOAL_INTENT_LEDGER_INVALID", "entry digest")
        refs = _ledger_sorted_objects(entry.get("artifact_refs"), "artifact_id", "artifact_refs")
        ids_paths = [_validate_artifact_ref(item) for item in refs]
        artifact_ids = {item[0] for item in ids_paths}
        artifact_paths = {item[1] for item in ids_paths}
        if len(artifact_paths) != len(ids_paths):
            raise GoalIntentError("GOAL_INTENT_LEDGER_INVALID", "duplicate artifact path")
        if global_artifact_ids & artifact_ids or global_artifact_paths & artifact_paths:
            raise GoalIntentError("GOAL_INTENT_LEDGER_INVALID", "artifact identity reused")
        global_artifact_ids.update(artifact_ids)
        global_artifact_paths.update(artifact_paths)
        previous = digest
        entries.append(entry)
    if data != b"".join(canonical_json_line(entry) for entry in entries):
        raise GoalIntentError("GOAL_INTENT_LEDGER_INVALID", "ledger bytes are not canonical")
    return data, tuple(entries)


def _snapshot(path: Path) -> tuple[Path, bytes | None]:
    try:
        return path, path.read_bytes() if path.exists() else None
    except OSError as exc:
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", str(exc)) from exc


def _legacy_path(value: Any, *, process_cwd: Path) -> Path:
    return _physical(Path(str(value)), base=process_cwd)


def _effective_config_path(args: Any, process_cwd: Path) -> Path:
    value = getattr(args, "config", None)
    return _physical(Path(value), base=process_cwd) if value else DEFAULT_CONFIG_PATH.resolve()


def _session_db_nodes(config: RoundtableConfig, *, process_cwd: Path) -> list[tuple[str, Path]]:
    raw = Path(config.session.db_path)
    if str(raw) == ":memory:":
        return []
    effective = _physical(raw, base=process_cwd)
    return [
        ("session-db", effective),
        ("session-db-wal", _append_suffix(effective, "-wal")),
        ("session-db-shm", _append_suffix(effective, "-shm")),
        ("session-db-journal", _append_suffix(effective, "-journal")),
    ]


def _validate_graph(
    *,
    nodes: list[tuple[str, Path]],
    output_root: Path,
    output_root_children: set[Path],
) -> None:
    for index, (left_label, left) in enumerate(nodes):
        for right_label, right in nodes[index + 1 :]:
            labels = {left_label, right_label}
            if (
                left == right
                and "prior-report" in labels
                and any(label.startswith("prior-artifact-") for label in labels)
            ):
                # Actual run→fix는 보존된 run report를 fix의 read-only 입력으로 재사용한다.
                continue
            if (
                (left == output_root and right in output_root_children)
                or (right == output_root and left in output_root_children)
            ):
                continue
            if _overlaps(left, right):
                raise GoalIntentError(
                    "GOAL_INTENT_ARTIFACT_COLLISION",
                    f"path namespace collision: {left_label}/{right_label}",
                )


def _preflight_fix_state(args: Any) -> tuple[int, list[JsonObject]]:
    try:
        report_payload = load_report_payload(Path(args.report_file))
        summary = report_payload.get("summary")
        if not isinstance(summary, dict) or summary.get("verdict") != "CHANGES_REQUESTED":
            raise ValueError("context-enabled fix-round requires CHANGES_REQUESTED prior report")
        findings = select_accepted_findings(report_payload, getattr(args, "accept_leg", None))
        if not findings or any(item.status != "CHANGES_REQUESTED" for item in findings):
            raise ValueError("accepted findings are not closed CHANGES_REQUESTED rows")
        input_digest = findings_digest(findings)
        validate_max_rounds(args.max_rounds)
        ledger = ReapplyLedger.load_or_create(
            args.ledger,
            phase_id=args.phase_id,
            max_rounds=args.max_rounds,
        )
        reason = gate_check(
            ledger,
            phase_id=args.phase_id,
            approve_round=args.approve_round,
            approve_findings=args.approve_findings,
            input_digest=input_digest,
            max_rounds=args.max_rounds,
        )
        if reason is not None:
            raise ValueError(reason)
        if not args.session_map:
            raise ValueError("session-map is required")
        session_map = SessionMap.load(Path(args.session_map))
        if session_map.get("implementer") is None:
            raise ValueError("session-map has no implementer id")
        return ledger.next_round_index, list(report_payload.get("steps", []))
    except (OSError, ValueError, TypeError) as exc:
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", str(exc)) from exc


async def prepare_preflight(
    args: Any,
    *,
    kind: CommandKind,
    config: RoundtableConfig,
    process_cwd: Path,
    repo_root: Path,
) -> GoalIntentPreflight:
    """모든 context-enabled command write보다 앞선 read-only barrier."""
    phase_id = _portable_phase_id(args.phase_id)
    raw_context = getattr(args, "goal_intent_context_file", None)
    logical_context = _repo_path(raw_context, "goal-intent context argv")
    context_path = _physical(Path(logical_context), base=repo_root)
    if not _inside(context_path, repo_root):
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", "context escapes repo")
    context, context_bytes = _load_context(
        context_path, logical_path=logical_context, repo_root=repo_root
    )
    for required in (context.contract_path, context.phase_ledger_path):
        if not required.is_file():
            raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", f"missing input: {required.name}")
    contract = _load_contract_semantics(context)
    try:
        phase_ledger_sha = _sha256(context.phase_ledger_path.read_bytes())
    except OSError as exc:
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", str(exc)) from exc
    expected_phase_ref: JsonObject = {
        "artifact_type": "canonical_phase_ledger_snapshot",
        "path": context.phase_ledger_logical_path,
        "sha256": phase_ledger_sha,
    }

    round_index = 0
    action_type = "CHECKPOINT_RECORDED"
    round_id = "round-0"
    leg_id = "run-phase"
    if kind == "fix-round":
        round_index, _ = _preflight_fix_state(args)
        action_type = "ROUND_RECORDED"
        round_id = f"round-{round_index}"
        leg_id = "fix-round"
    _valid_id(round_id, "round_id")
    _valid_id(leg_id, "leg_id")

    ledger_bytes, entries = _load_ledger(
        context.ledger_path,
        allow_absent=kind == "run-phase",
    )
    previous_ids: set[str] = set()
    previous_paths: set[str] = set()
    prior_nodes_by_path: dict[str, tuple[str, Path]] = {}
    for entry in entries:
        for raw_ref in entry["artifact_refs"]:
            artifact_id, logical_path = _validate_artifact_ref(raw_ref)
            previous_ids.add(artifact_id)
            previous_paths.add(logical_path)
            prior_nodes_by_path.setdefault(
                logical_path,
                (
                    f"prior-artifact-{artifact_id}",
                    _physical(Path(logical_path), base=repo_root),
                ),
            )
    new_ids = {context.report.artifact_id, context.envelope.artifact_id}
    new_paths = {context.report.logical_path, context.envelope.logical_path}
    if previous_ids & new_ids or previous_paths & new_paths:
        raise GoalIntentError("GOAL_INTENT_ARTIFACT_COLLISION", "prior artifact identity reused")
    must_be_absent: list[tuple[str, Path]] = [
        ("t13-report", context.report.path),
        ("t13-report-temp", _derived_tmp(context.report.path)),
        ("t13-envelope", context.envelope.path),
        ("t13-envelope-temp", _derived_tmp(context.envelope.path)),
        ("t13-ledger-temp", _derived_tmp(context.ledger_path)),
        ("t13-ledger-lock", _ledger_lock(context.ledger_path)),
    ]

    prompt_path = _legacy_path(args.prompt_file, process_cwd=process_cwd)
    output_root = _legacy_path(args.output_dir, process_cwd=process_cwd)
    config_path = _effective_config_path(args, process_cwd)
    if not prompt_path.is_file() or not config_path.is_file():
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", "prompt/config input missing")
    session_map_value = getattr(args, "session_map", "")
    session_map_path = (
        _legacy_path(session_map_value, process_cwd=process_cwd)
        if session_map_value
        else None
    )
    nodes: list[tuple[str, Path]] = [
        ("context", context.context_path),
        ("prompt", prompt_path),
        ("config", config_path),
        ("contract", context.contract_path),
        ("phase-ledger", context.phase_ledger_path),
        ("t13-ledger", context.ledger_path),
        ("t13-ledger-temp", _derived_tmp(context.ledger_path)),
        ("t13-ledger-lock", _ledger_lock(context.ledger_path)),
        ("t13-report", context.report.path),
        ("t13-report-temp", _derived_tmp(context.report.path)),
        ("t13-envelope", context.envelope.path),
        ("t13-envelope-temp", _derived_tmp(context.envelope.path)),
        ("output-root", output_root),
        *prior_nodes_by_path.values(),
    ]
    if getattr(args, "record", False):
        nodes.extend(_session_db_nodes(config, process_cwd=process_cwd))
    protected_paths = [
        context.context_path,
        prompt_path,
        config_path,
        context.contract_path,
        context.phase_ledger_path,
        context.ledger_path,
    ]
    relay_mutable_paths: set[Path] = set()
    output_children: set[Path] = set()
    if session_map_path is not None:
        nodes.extend(
            [
                ("session-map", session_map_path),
                ("session-map-temp", _pid_tmp(session_map_path)),
            ]
        )
        protected_paths.append(session_map_path)
        relay_mutable_paths.add(session_map_path)
        must_be_absent.append(("session-map-temp", _pid_tmp(session_map_path)))
    if kind == "fix-round":
        report_input = _legacy_path(args.report_file, process_cwd=process_cwd)
        t10_ledger = _legacy_path(args.ledger, process_cwd=process_cwd)
        fix_prompt = output_root / f"{phase_id}-round-{round_index}-fix-prompt.md"
        t10_report = output_root / f"{phase_id}-round-{round_index}-report.json"
        t10_envelope = output_root / f"{phase_id}-round-{round_index}-envelope.json"
        output_children = {fix_prompt, t10_report, t10_envelope}
        must_be_absent.extend(
            [
                ("fix-prompt", fix_prompt),
                ("t10-report", t10_report),
                ("t10-envelope", t10_envelope),
                ("t10-ledger-temp", _pid_tmp(t10_ledger)),
            ]
        )
        nodes.extend(
            [
                ("prior-report", report_input),
                ("t10-ledger", t10_ledger),
                ("t10-ledger-temp", _pid_tmp(t10_ledger)),
                ("fix-prompt", fix_prompt),
                ("t10-report", t10_report),
                ("t10-envelope", t10_envelope),
            ]
        )
        protected_paths.extend([report_input, t10_ledger])
        relay_mutable_paths.add(t10_ledger)
    _require_absent(must_be_absent)
    _validate_graph(nodes=nodes, output_root=output_root, output_root_children=output_children)
    _validate_context_claims(context, contract)
    for entry in entries:
        _validate_entry_semantics(
            entry,
            contract=contract,
            expected_phase_ref=expected_phase_ref,
        )

    protected = tuple(_snapshot(path) for path in dict.fromkeys(protected_paths))
    # context bytes를 위에서 읽은 값과 다시 대조해 read 사이 race를 즉시 차단한다.
    if dict(protected).get(context.context_path) != context_bytes:
        raise GoalIntentError("GOAL_INTENT_CONTEXT_INVALID", "context changed during preflight")
    excluded = frozenset(
        {
            context.report.logical_path,
            context.envelope.logical_path,
            context.ledger_logical_path,
            context.ledger_path.with_name(f"{context.ledger_path.name}.tmp").relative_to(repo_root).as_posix(),
            context.ledger_path.with_name(f"{context.ledger_path.name}.lock").relative_to(repo_root).as_posix(),
        }
    )
    return GoalIntentPreflight(
        kind=kind,
        process_cwd=process_cwd,
        repo_root=repo_root,
        context=context,
        phase_id=phase_id,
        round_index=round_index,
        action_type=action_type,
        round_id=round_id,
        leg_id=leg_id,
        ledger_bytes=ledger_bytes,
        entries=entries,
        protected_bytes=protected,
        relay_mutable_paths=frozenset(relay_mutable_paths),
        excluded_paths=excluded,
    )


async def _git_fields(repo_root: Path, command: tuple[str, ...]) -> list[str]:
    run = await run_subprocess_tool(command, cwd=repo_root, timeout_s=10.0)
    if run.exit_code != 0:
        raise GoalIntentError(
            "GOAL_INTENT_BASELINE_FAILED", run.stderr_sanitized or "git command failed"
        )
    return [field for field in run.stdout.split("\0") if field]


async def capture_fingerprints(
    repo_root: Path,
    *,
    excluded_paths: frozenset[str],
) -> dict[str, str]:
    """captured root에서 index/worktree/untracked/deleted 상태를 fingerprint한다."""
    commands = (
        (
            "git", "-c", "core.quotepath=false", "diff", "--name-status", "-M",
            "--diff-filter=ACMRD", "-z",
        ),
        (
            "git", "-c", "core.quotepath=false", "diff", "--cached", "--name-status",
            "-M", "--diff-filter=ACMRD", "-z",
        ),
        (
            "git", "-c", "core.quotepath=false", "ls-files", "--full-name", "--others",
            "--exclude-standard", "-z",
        ),
    )
    paths: list[str] = []
    for command in commands:
        fields = await _git_fields(repo_root, command)
        if "--name-status" in command:
            cursor = 0
            while cursor < len(fields):
                status = fields[cursor]
                cursor += 1
                count = 2 if status.startswith(("R", "C")) else 1
                if cursor + count > len(fields):
                    raise GoalIntentError("GOAL_INTENT_BASELINE_FAILED", "truncated git status")
                paths.extend(fields[cursor : cursor + count])
                cursor += count
        else:
            paths.extend(fields)
    fingerprints: dict[str, str] = {}
    for logical in sorted(set(paths) - set(excluded_paths)):
        path = repo_root / logical
        try:
            if path.is_symlink():
                content = f"SYMLINK:{os.readlink(path)}".encode("utf-8")
            elif path.is_file():
                content = b"FILE:" + path.read_bytes()
            elif path.exists():
                content = b"OTHER"
            else:
                content = b"MISSING"
        except OSError as exc:
            raise GoalIntentError("GOAL_INTENT_BASELINE_FAILED", str(exc)) from exc
        unstaged = await run_subprocess_tool(
            ("git", "diff", "--binary", "--", logical), cwd=repo_root, timeout_s=10.0
        )
        staged = await run_subprocess_tool(
            ("git", "diff", "--cached", "--binary", "--", logical),
            cwd=repo_root,
            timeout_s=10.0,
        )
        if unstaged.exit_code != 0 or staged.exit_code != 0:
            raise GoalIntentError("GOAL_INTENT_BASELINE_FAILED", "git diff failed")
        payload = (
            content
            + b"\0UNSTAGED\0"
            + unstaged.stdout.encode("utf-8")
            + b"\0STAGED\0"
            + staged.stdout.encode("utf-8")
        )
        fingerprints[logical] = _sha256(payload)
    return fingerprints


def changed_fingerprints(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(
        path for path in set(before) | set(after) if before.get(path) != after.get(path)
    )


def _write_temp_bytes(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException as exc:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            path.unlink(missing_ok=True)
        except OSError as cleanup_exc:
            if isinstance(exc, OSError):
                raise OSError(
                    f"{exc}; owned temp cleanup failed: {cleanup_exc}"
                ) from exc
            exc.add_note(f"owned temp cleanup failed: {cleanup_exc}")
        raise


def _cleanup_owned_node(
    path: Path,
    *,
    owned: bool,
    label: str,
) -> tuple[str, OSError] | None:
    if not owned:
        return None
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        detail = (
            f"{label} cleanup failed "
            f"(path={path}; state=cleanup-unconfirmed; retry_safe=false): {exc}"
        )
        return detail, exc
    return None


def _compose_cleanup_detail(primary: str, cleanup: list[tuple[str, OSError]]) -> str:
    return "; ".join(part for part in [primary, *(detail for detail, _ in cleanup)] if part)


def _write_once(path: Path, data: bytes) -> None:
    if path.exists():
        raise GoalIntentError("GOAL_INTENT_ARTIFACT_COLLISION", path.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = _derived_tmp(path)
    created = False
    primary: GoalIntentError | None = None
    primary_cause: OSError | None = None
    try:
        _write_temp_bytes(temp, data)
        created = True
        os.link(temp, path)
    except FileExistsError as exc:
        primary = GoalIntentError("GOAL_INTENT_ARTIFACT_COLLISION", path.name)
        primary_cause = exc
    except OSError as exc:
        primary = GoalIntentError("GOAL_INTENT_ARTIFACT_FAILED", str(exc))
        primary_cause = exc

    cleanup: list[tuple[str, OSError]] = []
    cleanup_failure = _cleanup_owned_node(
        temp,
        owned=created,
        label="artifact temp",
    )
    if cleanup_failure is not None:
        cleanup.append(cleanup_failure)
    if cleanup:
        if primary is None:
            raise GoalIntentError(
                "GOAL_INTENT_ARTIFACT_FAILED",
                _compose_cleanup_detail("", cleanup),
            ) from cleanup[0][1]
        raise GoalIntentError(
            primary.code,
            _compose_cleanup_detail(primary.detail, cleanup),
        ) from primary
    if primary is not None:
        raise primary from primary_cause


def _append_entry(preflight: GoalIntentPreflight, entry: JsonObject) -> None:
    ledger = preflight.context.ledger_path
    lock = _ledger_lock(ledger)
    temp = _derived_tmp(ledger)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    lock_fd: int | None = None
    lock_created = False
    temp_created = False
    primary: GoalIntentError | None = None
    primary_cause: OSError | None = None
    try:
        try:
            lock_fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            lock_created = True
        except FileExistsError as exc:
            raise GoalIntentError("GOAL_INTENT_LEDGER_LOCKED") from exc
        current = ledger.read_bytes() if ledger.exists() else None
        if current != preflight.ledger_bytes:
            raise GoalIntentError("GOAL_INTENT_LEDGER_CHANGED")
        new_bytes = (current or b"") + canonical_json_line(entry)
        _write_temp_bytes(temp, new_bytes)
        temp_created = True
        latest = ledger.read_bytes() if ledger.exists() else None
        if latest != preflight.ledger_bytes:
            raise GoalIntentError("GOAL_INTENT_LEDGER_CHANGED")
        if latest is None:
            try:
                os.link(temp, ledger)
            except FileExistsError as exc:
                raise GoalIntentError("GOAL_INTENT_LEDGER_CHANGED") from exc
        else:
            os.replace(temp, ledger)
            temp_created = False
    except GoalIntentError as exc:
        primary = exc
    except OSError as exc:
        primary = GoalIntentError("GOAL_INTENT_APPEND_FAILED", str(exc))
        primary_cause = exc

    cleanup: list[tuple[str, OSError]] = []
    temp_cleanup = _cleanup_owned_node(
        temp,
        owned=temp_created,
        label="ledger temp",
    )
    if temp_cleanup is not None:
        cleanup.append(temp_cleanup)
    if lock_fd is not None:
        try:
            os.close(lock_fd)
        except OSError as exc:
            cleanup.append((f"ledger lock handle cleanup failed: {exc}", exc))
    lock_cleanup = _cleanup_owned_node(
        lock,
        owned=lock_created,
        label="ledger lock",
    )
    if lock_cleanup is not None:
        cleanup.append(lock_cleanup)

    if cleanup:
        if primary is None:
            raise GoalIntentError(
                "GOAL_INTENT_APPEND_FAILED",
                _compose_cleanup_detail("", cleanup),
            ) from cleanup[0][1]
        raise GoalIntentError(
            primary.code,
            _compose_cleanup_detail(primary.detail, cleanup),
        ) from primary
    if primary is not None:
        raise primary from primary_cause


def persist_and_append(
    preflight: GoalIntentPreflight,
    *,
    report_payload: JsonObject,
    envelope: Envelope,
    changed_paths: list[str],
) -> bytes:
    """report/envelope를 먼저 보존한 뒤 canonical ledger row를 append한다."""
    report_bytes = canonical_json_line(report_payload)
    envelope_payload = envelope.as_stdout_payload()
    envelope_bytes = canonical_json_line(envelope_payload)
    _write_once(preflight.context.report.path, report_bytes)
    _write_once(preflight.context.envelope.path, envelope_bytes)
    sequence = len(preflight.entries) + 1
    previous = preflight.entries[-1]["entry_digest"] if preflight.entries else None
    entry: JsonObject = {
        "schema_version": 1,
        "sequence": sequence,
        "phase_id": preflight.phase_id,
        "round_id": preflight.round_id,
        "leg_id": preflight.leg_id,
        "role": "ORCHESTRATOR",
        "action_type": preflight.action_type,
        "contract_ref": {
            "contract_id": preflight.context.contract_id,
            "revision": preflight.context.contract_revision,
            "path": preflight.context.contract_logical_path,
            "sha256": _sha256(preflight.context.contract_path.read_bytes()),
        },
        "phase_ledger_ref": {
            "artifact_type": "canonical_phase_ledger_snapshot",
            "path": preflight.context.phase_ledger_logical_path,
            "sha256": _sha256(preflight.context.phase_ledger_path.read_bytes()),
        },
        "changed_paths": sorted(set(changed_paths)),
        "artifact_refs": sorted(
            [
                {
                    "artifact_id": preflight.context.report.artifact_id,
                    "artifact_type": preflight.context.report.artifact_type,
                    "path": preflight.context.report.logical_path,
                    "sha256": _sha256(report_bytes),
                },
                {
                    "artifact_id": preflight.context.envelope.artifact_id,
                    "artifact_type": preflight.context.envelope.artifact_type,
                    "path": preflight.context.envelope.logical_path,
                    "sha256": _sha256(envelope_bytes),
                },
            ],
            key=lambda row: row["artifact_id"],
        ),
        "claimed_pass": list(preflight.context.claimed_pass),
        "not_claimed": list(preflight.context.not_claimed),
        "claim_evidence_links": [dict(row) for row in preflight.context.claim_evidence_links],
        "previous_entry_digest": previous,
        "entry_digest": "",
    }
    entry["entry_digest"] = _object_digest(entry, "entry_digest")
    _append_entry(preflight, entry)
    return envelope_bytes
