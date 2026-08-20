from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
CHECKER_PATH = ROOT / "methodology" / "tools" / "goal_intent_checker.py"
FIXTURES = ROOT / "methodology" / "tests" / "fixtures" / "goal_intent"
F0 = FIXTURES / "f0-current"
F1 = FIXTURES / "f1-clean"
PYTHON = Path(sys.executable)

SPEC = importlib.util.spec_from_file_location("goal_intent_checker", CHECKER_PATH)
assert SPEC is not None and SPEC.loader is not None
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)

PASS_PAYLOAD = {
    "claim": "LEDGER_STRUCTURAL_INTEGRITY_PASS",
    "exit_code": 0,
    "mutations_performed": False,
    "reason_codes": [],
    "semantic_objective_satisfaction": "NOT_CLAIMED",
    "status": "PASS",
}

EXPECTED_REASON_ORDER = (
    "CONTRACT_MANIFEST_MISSING",
    "CONTRACT_INPUT_MISSING",
    "PHASE_LEDGER_MISSING",
    "LEDGER_MISSING",
    "ARTIFACT_INPUT_MISSING",
    "INPUT_ENCODING_INVALID",
    "INPUT_JSON_PARSE_ERROR",
    "INPUT_JSONL_PARSE_ERROR",
    "INPUT_SCHEMA_INVALID",
    "INPUT_NON_CANONICAL",
    "CONTRACT_MANIFEST_DIGEST_MISMATCH",
    "CONTRACT_INPUT_SET_MISMATCH",
    "CONTRACT_DIGEST_MISMATCH",
    "CONTRACT_REVISION_REUSED",
    "CONTRACT_PARENT_MISMATCH",
    "PHASE_LEDGER_DIGEST_MISMATCH",
    "LEDGER_SEQUENCE_GAP",
    "LEDGER_CHAIN_MISMATCH",
    "PATH_INVALID",
    "OBJECTIVE_DELTA",
    "SCOPE_DELTA",
    "WRITE_SCOPE_VIOLATION",
    "UNDECLARED_CLAIM",
    "CLAIM_STATE_CONFLICT",
    "UNCLASSIFIED_CLAIM",
    "CLAIM_EVIDENCE_LINK_MISSING",
    "MISSING_REQUIRED_ARTIFACT",
    "ARTIFACT_IDENTITY_MISMATCH",
    "ARTIFACT_DIGEST_MISMATCH",
    "VERDICT_SOURCE_LINK_MISSING",
    "VERDICT_SOURCE_ARTIFACT_MISSING",
    "VERDICT_SOURCE_ADAPTER_UNSUPPORTED",
    "VERDICT_SOURCE_PARSE_ERROR",
    "VERDICT_SOURCE_STATUS_MISMATCH",
    "VERDICT_SOURCE_EXIT_MISMATCH",
    "HUMAN_GATE_REQUIRED",
)


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _object_digest(value: dict[str, Any], field: str) -> str:
    return _digest(_canonical({key: item for key, item in value.items() if key != field}))


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _write_json(path: Path, value: dict[str, Any], *, canonical: bool = True) -> None:
    if canonical:
        path.write_bytes(_canonical(value) + b"\n")
    else:
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _entry(case: Path) -> dict[str, Any]:
    return _read_json(case / "trajectory-audit.jsonl")


def _write_entry(case: Path, value: dict[str, Any]) -> None:
    value["entry_digest"] = _object_digest(value, "entry_digest")
    _write_json(case / "trajectory-audit.jsonl", value)


def _write_entries(case: Path, values: list[dict[str, Any]]) -> None:
    previous_digest: str | None = None
    encoded: list[bytes] = []
    for sequence, value in enumerate(values, start=1):
        value["sequence"] = sequence
        value["previous_entry_digest"] = previous_digest
        value["entry_digest"] = _object_digest(value, "entry_digest")
        previous_digest = value["entry_digest"]
        encoded.append(_canonical(value) + b"\n")
    (case / "trajectory-audit.jsonl").write_bytes(b"".join(encoded))


def _write_manifest(case: Path, value: dict[str, Any]) -> None:
    value["manifest_digest"] = _object_digest(value, "manifest_digest")
    _write_json(case / "contract-manifest.json", value)


def _write_contract(case: Path, value: dict[str, Any], path: str | None = None) -> None:
    contract_path = path or "contracts/T13-FIXTURE-r1.json"
    value["contract_digest"] = _object_digest(value, "contract_digest")
    _write_json(case / contract_path, value)
    manifest = _read_json(case / "contract-manifest.json")
    row = next(row for row in manifest["contracts"] if row["path"] == contract_path)
    row["sha256"] = _digest((case / contract_path).read_bytes())
    _write_manifest(case, manifest)
    ledger = _entry(case)
    if ledger["contract_ref"]["path"] == contract_path:
        ledger["contract_ref"] = dict(row)
        _write_entry(case, ledger)


def _copy_clean(tmp_path: Path, name: str) -> Path:
    target = tmp_path / name
    shutil.copytree(F1, target)
    return target


def _add_contract_revision(
    case: Path, revision: int, parent_contract_digest: str | None
) -> str:
    contract = _read_json(case / "contracts/T13-FIXTURE-r1.json")
    contract["revision"] = revision
    contract["parent_contract_digest"] = parent_contract_digest
    contract["contract_digest"] = _object_digest(contract, "contract_digest")
    relative = f"contracts/T13-FIXTURE-r{revision}.json"
    _write_json(case / relative, contract)
    manifest = _read_json(case / "contract-manifest.json")
    manifest["contracts"].append(
        {
            "contract_id": "T13-FIXTURE",
            "path": relative,
            "revision": revision,
            "sha256": _digest((case / relative).read_bytes()),
        }
    )
    manifest["contracts"].sort(key=lambda row: (row["contract_id"], row["revision"]))
    _write_manifest(case, manifest)
    return relative


def _args_with_contract(relative: str) -> list[str]:
    args = _default_args()
    args[4:4] = ["--contract", relative]
    return args


def _append_unknown_claim_link(entry: dict[str, Any]) -> None:
    row = copy.deepcopy(entry["claim_evidence_links"][0])
    row["claim_id"] = "unknown_claim"
    entry["claim_evidence_links"].append(row)


def _default_args() -> list[str]:
    return [
        "--contract-manifest",
        "contract-manifest.json",
        "--contract",
        "contracts/T13-FIXTURE-r1.json",
        "--ledger",
        "trajectory-audit.jsonl",
        "--artifact",
        "canonical-phase-ledger.json",
        "--artifact",
        "phase-envelope.json",
        "--artifact",
        "pytest-report.json",
    ]


def _snapshot(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = f"symlink:{os.readlink(path)}"
        elif path.is_file():
            result[relative] = _digest(path.read_bytes())
        elif path.is_dir():
            result[relative] = "dir"
    return result


def _run(
    cwd: Path,
    expected_reasons: list[str],
    *,
    args: list[str] | None = None,
    expected_exit: int | None = None,
) -> tuple[subprocess.CompletedProcess[bytes], dict[str, Any]]:
    before = _snapshot(cwd)
    completed = subprocess.run(
        [str(PYTHON), str(CHECKER_PATH), *(args if args is not None else _default_args())],
        cwd=cwd,
        check=False,
        capture_output=True,
    )
    after = _snapshot(cwd)
    assert before == after
    assert completed.stderr == b""
    assert completed.stdout.endswith(b"\n")
    assert completed.stdout.count(b"\n") == 1
    payload = json.loads(completed.stdout)
    assert list(payload) == sorted(PASS_PAYLOAD)
    assert payload["mutations_performed"] is False
    assert payload["semantic_objective_satisfaction"] == "NOT_CLAIMED"
    assert payload["reason_codes"] == expected_reasons
    exit_code = (0 if not expected_reasons else 2) if expected_exit is None else expected_exit
    assert completed.returncode == exit_code
    assert payload["exit_code"] == exit_code
    if exit_code == 0:
        assert payload == PASS_PAYLOAD
    else:
        assert payload["status"] == "BLOCKED"
        assert payload["claim"] is None
    return completed, payload


def test_closed_reason_registry_has_exact_36_code_order() -> None:
    assert checker.REASON_ORDER == EXPECTED_REASON_ORDER
    assert len(checker.REASON_ORDER) == 36


def test_f0_current_shape_is_not_a_false_pass() -> None:
    _run(
        F0,
        ["CONTRACT_MANIFEST_MISSING", "LEDGER_MISSING"],
        args=[
            "--contract-manifest",
            "contract-manifest-missing.json",
            "--ledger",
            "trajectory-audit-missing.jsonl",
            "--artifact",
            "phase-report-current.json",
        ],
    )


def test_f1_clean_is_deterministic_and_read_only() -> None:
    first, _ = _run(F1, [])
    second, _ = _run(F1, [])
    assert first.stdout == second.stdout


def test_missing_secondary_inputs_have_distinct_closed_reasons(tmp_path: Path) -> None:
    contract_case = _copy_clean(tmp_path, "missing-contract")
    (contract_case / "contracts/T13-FIXTURE-r1.json").unlink()
    _run(contract_case, ["CONTRACT_INPUT_MISSING"])

    artifact_case = _copy_clean(tmp_path, "missing-artifact")
    _run(
        artifact_case,
        ["ARTIFACT_INPUT_MISSING"],
        args=[*_default_args(), "--artifact", "absent-report.json"],
    )

    ledger_case = _copy_clean(tmp_path, "missing-phase-ledger")
    args = _default_args()
    index = args.index("canonical-phase-ledger.json")
    del args[index - 1 : index + 1]
    _run(ledger_case, ["PHASE_LEDGER_MISSING"], args=args)


def test_manifest_and_contract_digest_failures_are_separate(tmp_path: Path) -> None:
    manifest_case = _copy_clean(tmp_path, "manifest-digest")
    manifest = _read_json(manifest_case / "contract-manifest.json")
    manifest["manifest_digest"] = "0" * 64
    _write_json(manifest_case / "contract-manifest.json", manifest)
    _run(manifest_case, ["CONTRACT_MANIFEST_DIGEST_MISMATCH"])

    contract_case = _copy_clean(tmp_path, "contract-digest")
    contract_path = contract_case / "contracts/T13-FIXTURE-r1.json"
    contract = _read_json(contract_path)
    contract["contract_digest"] = "0" * 64
    _write_json(contract_path, contract)
    manifest = _read_json(contract_case / "contract-manifest.json")
    manifest["contracts"][0]["sha256"] = _digest(contract_path.read_bytes())
    _write_manifest(contract_case, manifest)
    ledger = _entry(contract_case)
    ledger["contract_ref"] = dict(manifest["contracts"][0])
    _write_entry(contract_case, ledger)
    _run(contract_case, ["CONTRACT_DIGEST_MISMATCH", "HUMAN_GATE_REQUIRED"])


def test_contract_parent_uses_previous_contract_self_digest(tmp_path: Path) -> None:
    case = _copy_clean(tmp_path, "parent-valid")
    parent = _read_json(case / "contracts/T13-FIXTURE-r1.json")
    relative = _add_contract_revision(case, 2, parent["contract_digest"])
    _run(case, [], args=_args_with_contract(relative))


@pytest.mark.parametrize(
    ("name", "revision", "parent_digest", "expected"),
    [
        (
            "parent-wrong",
            2,
            "0" * 64,
            ["CONTRACT_PARENT_MISMATCH", "HUMAN_GATE_REQUIRED"],
        ),
        ("parent-missing", 2, None, ["INPUT_SCHEMA_INVALID"]),
    ],
)
def test_contract_parent_wrong_or_missing_is_rejected(
    tmp_path: Path,
    name: str,
    revision: int,
    parent_digest: str | None,
    expected: list[str],
) -> None:
    case = _copy_clean(tmp_path, name)
    relative = _add_contract_revision(case, revision, parent_digest)
    _run(case, expected, args=_args_with_contract(relative))


def test_contract_parent_skipped_revision_is_rejected(tmp_path: Path) -> None:
    case = _copy_clean(tmp_path, "parent-skipped")
    parent = _read_json(case / "contracts/T13-FIXTURE-r1.json")
    relative = _add_contract_revision(case, 3, parent["contract_digest"])
    _run(
        case,
        ["CONTRACT_PARENT_MISMATCH", "HUMAN_GATE_REQUIRED"],
        args=_args_with_contract(relative),
    )


def test_f2_a_input_set_mismatch_short_circuits_extra_bytes(tmp_path: Path) -> None:
    case = _copy_clean(tmp_path, "f2-a")
    extra = case / "contracts" / "T13-FIXTURE-r1-extra.json"
    extra.write_bytes(b"not-json")
    args = _default_args()
    args[6:6] = ["--contract", "contracts/T13-FIXTURE-r1-extra.json"]
    _run(case, ["CONTRACT_INPUT_SET_MISMATCH"], args=args)


def test_f2_b_duplicate_revision_reports_bounded_delta(tmp_path: Path) -> None:
    case = _copy_clean(tmp_path, "f2-b")
    accepted = _read_json(case / "contracts/T13-FIXTURE-r1.json")
    reused = dict(accepted)
    reused["objective"] = "cafe\u0301 structural integrity"
    reused["contract_digest"] = _object_digest(reused, "contract_digest")
    reused_path = "contracts/T13-FIXTURE-r1-reused.json"
    _write_json(case / reused_path, reused)
    manifest = _read_json(case / "contract-manifest.json")
    manifest["contracts"].append(
        {
            "contract_id": "T13-FIXTURE",
            "path": reused_path,
            "revision": 1,
            "sha256": _digest((case / reused_path).read_bytes()),
        }
    )
    _write_manifest(case, manifest)
    args = _default_args()
    args[6:6] = ["--contract", reused_path]
    _run(
        case,
        ["CONTRACT_REVISION_REUSED", "OBJECTIVE_DELTA", "HUMAN_GATE_REQUIRED"],
        args=args,
    )


def test_f2_c_structural_drift_exact_array(tmp_path: Path) -> None:
    case = _copy_clean(tmp_path, "f2-c")
    ledger = _entry(case)
    ledger["changed_paths"] = ["methodology/forbidden.py"]
    ledger["claimed_pass"] = ["full_e2e", "unit_tests"]
    ledger["claim_evidence_links"][0]["artifact_ids"] = []
    ledger["claim_evidence_links"][0]["verdict_source_ids"] = []
    _write_entry(case, ledger)
    _run(
        case,
        [
            "WRITE_SCOPE_VIOLATION",
            "UNDECLARED_CLAIM",
            "MISSING_REQUIRED_ARTIFACT",
            "VERDICT_SOURCE_LINK_MISSING",
            "HUMAN_GATE_REQUIRED",
        ],
    )


def test_f2_d1_artifact_identity(tmp_path: Path) -> None:
    case = _copy_clean(tmp_path, "f2-d1")
    ledger = _entry(case)
    row = next(row for row in ledger["artifact_refs"] if row["artifact_id"] == "pytest_report")
    row["artifact_type"] = "wrong_type"
    _write_entry(case, ledger)
    _run(case, ["ARTIFACT_IDENTITY_MISMATCH", "HUMAN_GATE_REQUIRED"])


def test_f2_d2_artifact_digest(tmp_path: Path) -> None:
    case = _copy_clean(tmp_path, "f2-d2")
    ledger = _entry(case)
    row = next(row for row in ledger["artifact_refs"] if row["artifact_id"] == "pytest_report")
    row["sha256"] = "0" * 64
    _write_entry(case, ledger)
    _run(case, ["ARTIFACT_DIGEST_MISMATCH", "HUMAN_GATE_REQUIRED"])


def test_f2_d3_missing_verdict_artifact(tmp_path: Path) -> None:
    case = _copy_clean(tmp_path, "f2-d3")
    ledger = _entry(case)
    ledger["artifact_refs"] = [
        row for row in ledger["artifact_refs"] if row["artifact_id"] != "phase_envelope"
    ]
    _write_entry(case, ledger)
    _run(case, ["VERDICT_SOURCE_ARTIFACT_MISSING", "HUMAN_GATE_REQUIRED"])


def test_f2_d4_unsupported_adapter(tmp_path: Path) -> None:
    case = _copy_clean(tmp_path, "f2-d4")
    contract = _read_json(case / "contracts/T13-FIXTURE-r1.json")
    contract["required_verdict_sources"][0]["adapter"] = "future_adapter"
    _write_contract(case, contract)
    _run(case, ["VERDICT_SOURCE_ADAPTER_UNSUPPORTED", "HUMAN_GATE_REQUIRED"])


def test_f2_d5_malformed_verdict_artifact(tmp_path: Path) -> None:
    case = _copy_clean(tmp_path, "f2-d5")
    malformed = b'{"status":'
    (case / "phase-envelope.json").write_bytes(malformed)
    ledger = _entry(case)
    row = next(row for row in ledger["artifact_refs"] if row["artifact_id"] == "phase_envelope")
    row["sha256"] = _digest(malformed)
    _write_entry(case, ledger)
    _run(case, ["VERDICT_SOURCE_PARSE_ERROR", "HUMAN_GATE_REQUIRED"])


def test_f2_d6_non_pass_verdict(tmp_path: Path) -> None:
    case = _copy_clean(tmp_path, "f2-d6")
    envelope = _read_json(case / "phase-envelope.json")
    envelope["status"] = "CHANGES_REQUESTED"
    envelope["exit_code"] = 1
    _write_json(case / "phase-envelope.json", envelope)
    ledger = _entry(case)
    row = next(row for row in ledger["artifact_refs"] if row["artifact_id"] == "phase_envelope")
    row["sha256"] = _digest((case / "phase-envelope.json").read_bytes())
    _write_entry(case, ledger)
    _run(
        case,
        [
            "VERDICT_SOURCE_STATUS_MISMATCH",
            "VERDICT_SOURCE_EXIT_MISMATCH",
            "HUMAN_GATE_REQUIRED",
        ],
    )


@pytest.mark.parametrize(
    ("name", "mutate", "expected"),
    [
        (
            "sequence-gap",
            lambda entry: entry.__setitem__("sequence", 2),
            ["LEDGER_SEQUENCE_GAP"],
        ),
        (
            "chain-mismatch",
            lambda entry: entry.__setitem__("previous_entry_digest", "0" * 64),
            ["LEDGER_CHAIN_MISMATCH"],
        ),
    ],
)
def test_f3_ledger_integrity(
    tmp_path: Path,
    name: str,
    mutate: Callable[[dict[str, Any]], None],
    expected: list[str],
) -> None:
    case = _copy_clean(tmp_path, name)
    ledger = _entry(case)
    mutate(ledger)
    _write_entry(case, ledger)
    _run(case, expected)


def test_closeout_is_valid_as_final_entry_in_multi_entry_ledger(tmp_path: Path) -> None:
    case = _copy_clean(tmp_path, "closeout-final")
    closeout = _entry(case)
    checkpoint = copy.deepcopy(closeout)
    checkpoint["action_type"] = "CHECKPOINT_RECORDED"
    del checkpoint["closeout_status"]
    _write_entries(case, [checkpoint, closeout])
    _run(case, [])


def test_entry_after_closeout_is_schema_invalid(tmp_path: Path) -> None:
    case = _copy_clean(tmp_path, "post-closeout")
    closeout = _entry(case)
    checkpoint = copy.deepcopy(closeout)
    checkpoint["action_type"] = "CHECKPOINT_RECORDED"
    del checkpoint["closeout_status"]
    _write_entries(case, [closeout, checkpoint])
    _run(case, ["INPUT_SCHEMA_INVALID"])


def test_f4_phase_ledger_digest_binding(tmp_path: Path) -> None:
    case = _copy_clean(tmp_path, "f4")
    ledger = _entry(case)
    ledger["phase_ledger_ref"]["sha256"] = "0" * 64
    _write_entry(case, ledger)
    _run(case, ["PHASE_LEDGER_DIGEST_MISMATCH", "HUMAN_GATE_REQUIRED"])


@pytest.mark.parametrize("variant", ["bom", "invalid-utf8"])
def test_f5_encoding_failures(tmp_path: Path, variant: str) -> None:
    case = _copy_clean(tmp_path, f"encoding-{variant}")
    path = case / "contract-manifest.json"
    path.write_bytes((b"\xef\xbb\xbf" + path.read_bytes()) if variant == "bom" else b"\xff")
    _run(case, ["INPUT_ENCODING_INVALID"])


@pytest.mark.parametrize(
    "bad_bytes",
    [
        b'{"schema_version":1,"schema_version":1}\n',
        b'{"schema_version":1} trailing\n',
        b'{"schema_version":NaN}\n',
        b'{"schema_version":"\\ud800"}\n',
    ],
)
def test_f5_json_parser_failures(tmp_path: Path, bad_bytes: bytes) -> None:
    case = _copy_clean(tmp_path, "json-parse")
    (case / "contract-manifest.json").write_bytes(bad_bytes)
    _run(case, ["INPUT_JSON_PARSE_ERROR"])


def test_f5_jsonl_parser_failure(tmp_path: Path) -> None:
    case = _copy_clean(tmp_path, "jsonl-parse")
    (case / "trajectory-audit.jsonl").write_bytes(b'{"sequence":1,"sequence":1}\n')
    _run(case, ["INPUT_JSONL_PARSE_ERROR"])


def test_f5_jsonl_blank_line_is_noncanonical(tmp_path: Path) -> None:
    case = _copy_clean(tmp_path, "jsonl-blank-line")
    path = case / "trajectory-audit.jsonl"
    path.write_bytes(path.read_bytes() + b"\n")
    _run(case, ["INPUT_NON_CANONICAL"])


@pytest.mark.parametrize("variant", ["unknown-key", "bool-int", "missing-artifact-digest"])
def test_f5_schema_failures(tmp_path: Path, variant: str) -> None:
    case = _copy_clean(tmp_path, f"schema-{variant}")
    if variant == "unknown-key":
        manifest = _read_json(case / "contract-manifest.json")
        manifest["unknown"] = 1
        _write_json(case / "contract-manifest.json", manifest)
    elif variant == "bool-int":
        manifest = _read_json(case / "contract-manifest.json")
        manifest["schema_version"] = True
        _write_json(case / "contract-manifest.json", manifest)
    else:
        ledger = _entry(case)
        del ledger["artifact_refs"][0]["sha256"]
        _write_entry(case, ledger)
    _run(case, ["INPUT_SCHEMA_INVALID"])


def test_f5_schema_valid_unsorted_collection_is_noncanonical(tmp_path: Path) -> None:
    case = _copy_clean(tmp_path, "noncanonical")
    contract = _read_json(case / "contracts/T13-FIXTURE-r1.json")
    contract["non_goals"] = ["z-last", "a-first"]
    contract["contract_digest"] = _object_digest(contract, "contract_digest")
    _write_json(case / "contracts/T13-FIXTURE-r1.json", contract)
    manifest = _read_json(case / "contract-manifest.json")
    manifest["contracts"][0]["sha256"] = _digest(
        (case / "contracts/T13-FIXTURE-r1.json").read_bytes()
    )
    _write_manifest(case, manifest)
    _run(case, ["INPUT_NON_CANONICAL"])


@pytest.mark.parametrize("objective", ["caf\u00e9", "cafe\u0301"])
def test_f5_nfc_and_nfd_are_individually_valid(tmp_path: Path, objective: str) -> None:
    case = _copy_clean(tmp_path, "unicode")
    contract = _read_json(case / "contracts/T13-FIXTURE-r1.json")
    contract["objective"] = objective
    _write_contract(case, contract)
    _run(case, [])


def _load_live_t11() -> tuple[Callable[[str], str | None], Callable[[str], str]]:
    runtime = ROOT / "runtimes" / "ztr"
    sys.path.insert(0, str(runtime))
    try:
        from src.engine.phase_relay import _glob_regex, _normalize_repo_path

        return _normalize_repo_path, _glob_regex
    finally:
        sys.path.remove(str(runtime))


@pytest.mark.parametrize(
    ("raw", "pattern"),
    [
        ("methodology/docs/design.md", "methodology/docs/design.md"),
        ("methodology/docs/deep/a.md", "methodology/docs"),
        ("methodology/docs/a.md", "methodology/docs/*.md"),
        ("docs/archive/HANDOFF_OLD.md", "**/HANDOFF*.md"),
        ("a/x.md", "a/?.md"),
        (r"methodology\docs\a.md", "methodology/docs"),
        ("methodology//docs/a.md", "methodology/docs"),
        ("methodology/./docs/a.md", "methodology/docs"),
        ("Methodology/Docs/a.md", "Methodology/Docs"),
        ("../outside.md", "**/*.md"),
        ("C:/outside.md", "**/*.md"),
        ("/outside.md", "**/*.md"),
    ],
)
def test_f5_independent_matcher_has_live_t11_byte_parity(raw: str, pattern: str) -> None:
    live_normalize, live_glob = _load_live_t11()
    assert checker._normalize_repo_path(raw) == live_normalize(raw)
    assert checker._normalize_repo_path(pattern) == live_normalize(pattern)
    normalized_pattern = live_normalize(pattern)
    if normalized_pattern is not None:
        assert checker._glob_regex(normalized_pattern) == live_glob(normalized_pattern)


@pytest.mark.parametrize(
    ("name", "changed", "scope", "expected"),
    [
        ("exact", "runtimes/ztr/src/example.py", "runtimes/ztr/src/example.py", []),
        ("prefix", "runtimes/ztr/src/example.py", "runtimes/ztr/src", []),
        ("star", "runtimes/ztr/src/example.py", "runtimes/ztr/src/*.py", []),
        ("double-star", "runtimes/ztr/src/deep/example.py", "runtimes/**/example.py", []),
        ("question", "runtimes/ztr/src/x.py", "runtimes/ztr/src/?.py", []),
        ("backslash", r"runtimes\ztr\src\example.py", "runtimes/ztr/src", []),
        ("duplicate-separator", "runtimes//ztr/src/example.py", "runtimes/ztr/src", []),
        ("dot-segment", "runtimes/./ztr/src/example.py", "runtimes/ztr/src", []),
        ("case", "Runtimes/Ztr/Src/example.py", "Runtimes/Ztr/Src", []),
        ("parent", "runtimes/../outside.py", "runtimes", ["PATH_INVALID"]),
        ("drive", "C:/outside.py", "runtimes", ["PATH_INVALID"]),
        ("absolute", "/outside.py", "runtimes", ["PATH_INVALID"]),
    ],
)
def test_f5_path_rows_are_isolated_cli_runs(
    tmp_path: Path,
    name: str,
    changed: str,
    scope: str,
    expected: list[str],
) -> None:
    case = _copy_clean(tmp_path, name)
    contract = _read_json(case / "contracts/T13-FIXTURE-r1.json")
    contract["allowed_write_scope"] = [scope]
    _write_contract(case, contract)
    ledger = _entry(case)
    ledger["changed_paths"] = [changed]
    _write_entry(case, ledger)
    _run(case, expected)


@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["--unknown"],
        ["--contract-manifest", "manifest.json"],
        [
            "--contract-manifest",
            "one.json",
            "--contract-manifest",
            "two.json",
            "--ledger",
            "ledger.jsonl",
        ],
    ],
)
def test_parser_exits_are_exact_blocked_70(argv: list[str], tmp_path: Path) -> None:
    _run(tmp_path, [], args=argv, expected_exit=70)


@pytest.mark.parametrize(
    "variant", ["artifact", "claim-link", "nested-list", "unsorted-nested-list"]
)
def test_duplicate_cardinality_is_schema_invalid(tmp_path: Path, variant: str) -> None:
    case = _copy_clean(tmp_path, f"duplicate-{variant}")
    ledger = _entry(case)
    if variant == "artifact":
        ledger["artifact_refs"].append(dict(ledger["artifact_refs"][0]))
    elif variant == "claim-link":
        ledger["claim_evidence_links"].append(dict(ledger["claim_evidence_links"][0]))
    elif variant == "nested-list":
        ledger["claim_evidence_links"][0]["artifact_ids"] = ["pytest_report", "pytest_report"]
    else:
        ledger["claim_evidence_links"][0]["artifact_ids"] = ["z_artifact", "a_artifact"]
    _write_entry(case, ledger)
    _run(case, ["INPUT_SCHEMA_INVALID"])


def test_duplicate_normalized_cli_path_is_schema_invalid(tmp_path: Path) -> None:
    case = _copy_clean(tmp_path, "duplicate-cli-path")
    _run(
        case,
        ["INPUT_SCHEMA_INVALID"],
        args=[*_default_args(), "--artifact", "./pytest-report.json"],
    )


def test_claim_link_missing_has_precedence(tmp_path: Path) -> None:
    case = _copy_clean(tmp_path, "missing-link")
    ledger = _entry(case)
    ledger["claim_evidence_links"] = []
    _write_entry(case, ledger)
    _run(case, ["CLAIM_EVIDENCE_LINK_MISSING", "HUMAN_GATE_REQUIRED"])


@pytest.mark.parametrize(
    ("name", "mutate", "expected"),
    [
        (
            "unknown-link-claim",
            _append_unknown_claim_link,
            ["UNDECLARED_CLAIM", "HUMAN_GATE_REQUIRED"],
        ),
        (
            "unknown-link-artifact",
            lambda entry: entry["claim_evidence_links"][0]["artifact_ids"].append(
                "unknown_artifact"
            ),
            ["MISSING_REQUIRED_ARTIFACT", "HUMAN_GATE_REQUIRED"],
        ),
        (
            "unknown-link-source",
            lambda entry: entry["claim_evidence_links"][0]["verdict_source_ids"].append(
                "unknown_source"
            ),
            ["VERDICT_SOURCE_LINK_MISSING", "HUMAN_GATE_REQUIRED"],
        ),
    ],
)
def test_claim_evidence_link_ids_are_closed_world(
    tmp_path: Path,
    name: str,
    mutate: Callable[[dict[str, Any]], None],
    expected: list[str],
) -> None:
    case = _copy_clean(tmp_path, name)
    ledger = _entry(case)
    mutate(ledger)
    _write_entry(case, ledger)
    _run(case, expected)


@pytest.mark.parametrize(
    ("name", "passed", "not_claimed", "expected"),
    [
        (
            "conflict",
            ["unit_tests"],
            ["unit_tests"],
            ["CLAIM_STATE_CONFLICT", "HUMAN_GATE_REQUIRED"],
        ),
        (
            "unclassified",
            [],
            [],
            ["UNCLASSIFIED_CLAIM", "HUMAN_GATE_REQUIRED"],
        ),
    ],
)
def test_claim_truth_table_is_fail_closed(
    tmp_path: Path,
    name: str,
    passed: list[str],
    not_claimed: list[str],
    expected: list[str],
) -> None:
    case = _copy_clean(tmp_path, name)
    ledger = _entry(case)
    ledger["claimed_pass"] = passed
    ledger["not_claimed"] = not_claimed
    _write_entry(case, ledger)
    _run(case, expected)


def test_external_envelope_format_is_opaque_and_noncanonical_is_accepted(tmp_path: Path) -> None:
    case = _copy_clean(tmp_path, "pretty-envelope")
    envelope = _read_json(case / "phase-envelope.json")
    _write_json(case / "phase-envelope.json", envelope, canonical=False)
    ledger = _entry(case)
    row = next(row for row in ledger["artifact_refs"] if row["artifact_id"] == "phase_envelope")
    row["sha256"] = _digest((case / "phase-envelope.json").read_bytes())
    _write_entry(case, ledger)
    _run(case, [])


def _make_symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, PermissionError) as exc:
        pytest.skip(f"symlink capability unavailable: {exc}")


def test_contained_regular_artifacts_are_accepted(tmp_path: Path) -> None:
    case = _copy_clean(tmp_path, "regular")
    _run(case, [])


def test_contained_symlink_to_internal_file_is_accepted(tmp_path: Path) -> None:
    case = _copy_clean(tmp_path, "internal-link")
    link = case / "linked-report.json"
    _make_symlink(link, case / "pytest-report.json")
    _run(case, [], args=[*_default_args(), "--artifact", "linked-report.json"])


def test_symlink_escape_is_path_invalid_and_external_bytes_are_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _copy_clean(tmp_path, "escape-link")
    external = tmp_path / "outside-secret.json"
    external.write_text("secret", encoding="utf-8")
    link = case / "escape.json"
    _make_symlink(link, external)
    args = [*_default_args(), "--artifact", "escape.json"]
    _run(case, ["PATH_INVALID"], args=args)

    reads: list[Path] = []
    real_read = checker._read_bytes

    def spy(path: Path) -> bytes:
        reads.append(path.resolve())
        return real_read(path)

    monkeypatch.setattr(checker, "_read_bytes", spy)
    payload = checker.execute(args, cwd=case)
    assert payload["reason_codes"] == ["PATH_INVALID"]
    assert external.resolve() not in reads
    assert reads == []


def test_unexpected_exception_is_fail_closed_70_without_details(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    def fail(_args: Any, _cwd: Path) -> dict[str, Any]:
        raise RuntimeError("sensitive internal detail")

    monkeypatch.setattr(checker, "_evaluate", fail)
    assert checker.main(_default_args()) == 70
    captured = capfd.readouterr()
    assert captured.err == ""
    assert "sensitive" not in captured.out
    assert "Traceback" not in captured.out
    payload = json.loads(captured.out)
    assert payload == {
        "claim": None,
        "exit_code": 70,
        "mutations_performed": False,
        "reason_codes": [],
        "semantic_objective_satisfaction": "NOT_CLAIMED",
        "status": "BLOCKED",
    }
