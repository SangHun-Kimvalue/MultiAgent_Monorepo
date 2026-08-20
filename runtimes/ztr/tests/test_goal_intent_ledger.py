"""T13-P2 Goal/Intent writer의 결정론/실 artifact 계약 테스트."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO, Literal, cast

import pytest

from src.engine.goal_intent_ledger import (
    ArtifactSpec,
    GoalIntentError,
    GoalIntentContext,
    GoalIntentPreflight,
    _append_entry,
    _load_ledger,
    _portable_phase_id,
    _session_db_nodes,
    _validate_graph,
    _write_once,
    _write_temp_bytes,
    canonical_json,
    canonical_json_line,
    capture_fingerprints,
    changed_fingerprints,
    persist_and_append,
)
from src.engine.fix_feedback import extract_findings
from src.engine.reapply_ledger import findings_digest
from src.config.schema import RoundtableConfig, SessionConfig
from src.envelope import Envelope, Verdict


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = RUNTIME_ROOT.parents[1]
CHECKER = REPO_ROOT / "methodology" / "tools" / "goal_intent_checker.py"


_TempFailure = Literal["write", "flush", "fsync", "close"]


class _FailingBinaryStream:
    def __init__(self, stream: BinaryIO, operation: _TempFailure) -> None:
        self._stream = stream
        self._operation = operation

    def __enter__(self) -> _FailingBinaryStream:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        self._stream.close()
        if self._operation == "close":
            raise OSError("injected close failure")
        return False

    def write(self, data: bytes) -> int:
        if self._operation == "write":
            raise OSError("injected write failure")
        return self._stream.write(data)

    def flush(self) -> None:
        if self._operation == "flush":
            raise OSError("injected flush failure")
        self._stream.flush()

    def fileno(self) -> int:
        return self._stream.fileno()


def _inject_temp_failure(
    monkeypatch: pytest.MonkeyPatch, operation: _TempFailure
) -> None:
    if operation == "fsync":

        def fail_fsync(_: int) -> None:
            raise OSError("injected fsync failure")

        monkeypatch.setattr(os, "fsync", fail_fsync)
        return

    real_fdopen = os.fdopen

    def failing_fdopen(fd: int, mode: str, *, closefd: bool = True) -> _FailingBinaryStream:
        stream = cast(BinaryIO, real_fdopen(fd, mode, closefd=closefd))
        return _FailingBinaryStream(stream, operation)

    monkeypatch.setattr(os, "fdopen", failing_fdopen)


def _digest_without(value: dict[str, Any], field: str) -> str:
    payload = {key: item for key, item in value.items() if key != field}
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _ledger_entry(
    *,
    sequence: int = 1,
    previous: str | None = None,
    artifact_id: str = "prior_report",
    artifact_path: str = "audit/prior-report.json",
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "schema_version": 1,
        "sequence": sequence,
        "phase_id": "T13-P2",
        "round_id": f"round-{sequence - 1}",
        "leg_id": "run-phase" if sequence == 1 else "fix-round",
        "role": "ORCHESTRATOR",
        "action_type": "CHECKPOINT_RECORDED" if sequence == 1 else "ROUND_RECORDED",
        "contract_ref": {
            "contract_id": "T13-P2",
            "revision": 1,
            "path": "audit/contract.json",
            "sha256": "1" * 64,
        },
        "phase_ledger_ref": {
            "artifact_type": "canonical_phase_ledger_snapshot",
            "path": "audit/phase-ledger.json",
            "sha256": "2" * 64,
        },
        "changed_paths": ["changed.py"],
        "artifact_refs": [
            {
                "artifact_id": artifact_id,
                "artifact_type": "phase_relay_report_v1",
                "path": artifact_path,
                "sha256": "3" * 64,
            }
        ],
        "claimed_pass": ["relay_pass"],
        "not_claimed": [],
        "claim_evidence_links": [
            {
                "claim_id": "relay_pass",
                "artifact_ids": [artifact_id],
                "verdict_source_ids": ["relay_verdict"],
            }
        ],
        "previous_entry_digest": previous,
        "entry_digest": "",
    }
    entry["entry_digest"] = _digest_without(entry, "entry_digest")
    return entry


def _rewrite_digest(entry: dict[str, Any]) -> None:
    entry["entry_digest"] = _digest_without(entry, "entry_digest")


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_line(value))


def _contract(*, report_id: str, envelope_id: str) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": 1,
        "contract_id": "T13-P2",
        "revision": 1,
        "parent_contract_digest": None,
        "objective": "record existing relay facts",
        "non_goals": ["semantic objective judgment"],
        "allowed_write_scope": ["changed.py"],
        "forbidden_outcomes": ["semantic_greenwash"],
        "validation_claims": [
            {
                "claim_id": "relay_pass",
                "required_artifact_ids": [report_id],
                "required_verdict_source_ids": ["relay_verdict"],
            }
        ],
        "required_artifacts": [
            {
                "artifact_id": envelope_id,
                "artifact_type": "ztr_envelope_v2",
                "path": "audit/run-envelope.json",
            },
            {
                "artifact_id": report_id,
                "artifact_type": "phase_relay_report_v1",
                "path": "audit/run-report.json",
            },
        ],
        "required_verdict_sources": [
            {
                "verdict_source_id": "relay_verdict",
                "artifact_id": envelope_id,
                "adapter": "ztr_envelope_v2",
                "required_status": "PASS",
                "required_exit_code": 0,
            }
        ],
        "contract_digest": "",
    }
    value["contract_digest"] = _digest_without(value, "contract_digest")
    return value


def _chain_contract() -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": 1,
        "contract_id": "T13-P2",
        "revision": 1,
        "parent_contract_digest": None,
        "objective": "record an actual run then an actual fix round",
        "non_goals": ["semantic objective judgment"],
        "allowed_write_scope": ["fix-change.py", "run-change.py"],
        "forbidden_outcomes": ["synthetic_first_entry"],
        "validation_claims": [],
        "required_artifacts": [
            {
                "artifact_id": "fix_envelope",
                "artifact_type": "ztr_envelope_v2",
                "path": "audit/fix-envelope.json",
            },
            {
                "artifact_id": "fix_report",
                "artifact_type": "phase_relay_report_v1",
                "path": "audit/fix-report.json",
            },
            {
                "artifact_id": "run_envelope",
                "artifact_type": "ztr_envelope_v2",
                "path": "audit/run-envelope.json",
            },
            {
                "artifact_id": "run_report",
                "artifact_type": "phase_relay_report_v1",
                "path": "audit/run-report.json",
            },
        ],
        "required_verdict_sources": [],
        "contract_digest": "",
    }
    value["contract_digest"] = _digest_without(value, "contract_digest")
    return value


def _semantic_history_contract() -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": 1,
        "contract_id": "T13-P2",
        "revision": 1,
        "parent_contract_digest": None,
        "objective": "reject P1-incompatible existing history before relay",
        "non_goals": [],
        "allowed_write_scope": ["changed.py"],
        "forbidden_outcomes": [],
        "validation_claims": [
            {
                "claim_id": "prior_pass",
                "required_artifact_ids": ["prior_report"],
                "required_verdict_source_ids": ["prior_verdict"],
            },
            {
                "claim_id": "relay_pass",
                "required_artifact_ids": ["run_report"],
                "required_verdict_source_ids": ["relay_verdict"],
            },
        ],
        "required_artifacts": [
            {
                "artifact_id": "prior_envelope",
                "artifact_type": "ztr_envelope_v2",
                "path": "audit/prior-envelope.json",
            },
            {
                "artifact_id": "prior_report",
                "artifact_type": "phase_relay_report_v1",
                "path": "audit/prior-report.json",
            },
            {
                "artifact_id": "run_envelope",
                "artifact_type": "ztr_envelope_v2",
                "path": "audit/run-envelope.json",
            },
            {
                "artifact_id": "run_report",
                "artifact_type": "phase_relay_report_v1",
                "path": "audit/run-report.json",
            },
        ],
        "required_verdict_sources": [
            {
                "verdict_source_id": "prior_verdict",
                "artifact_id": "prior_envelope",
                "adapter": "ztr_envelope_v2",
                "required_status": "PASS",
                "required_exit_code": 0,
            },
            {
                "verdict_source_id": "relay_verdict",
                "artifact_id": "run_envelope",
                "adapter": "ztr_envelope_v2",
                "required_status": "PASS",
                "required_exit_code": 0,
            },
        ],
        "contract_digest": "",
    }
    value["contract_digest"] = _digest_without(value, "contract_digest")
    return value


def _context(*, report_id: str = "run_report", envelope_id: str = "run_envelope") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "ledger_path": "audit/trajectory.jsonl",
        "contract": {
            "contract_id": "T13-P2",
            "revision": 1,
            "path": "audit/contract.json",
        },
        "phase_ledger_path": "audit/phase-ledger.json",
        "report_artifact": {
            "artifact_id": report_id,
            "artifact_type": "phase_relay_report_v1",
            "path": "audit/run-report.json",
        },
        "envelope_artifact": {
            "artifact_id": envelope_id,
            "artifact_type": "ztr_envelope_v2",
            "path": "audit/run-envelope.json",
        },
        "claimed_pass": ["relay_pass"],
        "not_claimed": [],
        "claim_evidence_links": [
            {
                "claim_id": "relay_pass",
                "artifact_ids": [report_id],
                "verdict_source_ids": ["relay_verdict"],
            }
        ],
    }


def _init_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text(".ztr/\n", encoding="utf-8")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("implement exactly one fixture change", encoding="utf-8")
    fake = tmp_path / "fake_cli.py"
    fake.write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "import sys",
                "sys.stdin.read()",
                "Path('changed.py').write_text('value = 1\\n', encoding='utf-8')",
                "print('implemented')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    contract = _contract(report_id="run_report", envelope_id="run_envelope")
    _write_json(tmp_path / "audit" / "contract.json", contract)
    _write_json(
        tmp_path / "audit" / "phase-ledger.json",
        {
            "phase": "T13-P2",
            "status": "IMPLEMENTING",
            "current_gate": "OUTPUT_REVIEW_PENDING",
        },
    )
    context_path = tmp_path / "audit" / "context.json"
    _write_json(context_path, _context())
    return prompt, fake, context_path


def _env() -> dict[str, str]:
    env = os.environ.copy()
    current = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(RUNTIME_ROOT) if not current else f"{RUNTIME_ROOT}{os.pathsep}{current}"
    return env


def _fake_codex_launcher(tmp_path: Path) -> str:
    script = tmp_path / "fake_codex.py"
    script.write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "import json",
                "import sys",
                "sys.stdin.read()",
                "target = 'fix-change.py' if 'resume' in sys.argv else 'run-change.py'",
                "Path(target).write_text(target + '\\n', encoding='utf-8')",
                "print(json.dumps({'type': 'thread.started', 'thread_id': 'thread-t13'}))",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        launcher = tmp_path / "codex-fixture.cmd"
        launcher.write_text(
            f'@echo off\n"{sys.executable}" "{script}" %*\n', encoding="utf-8"
        )
    else:
        launcher = tmp_path / "codex-fixture.sh"
        launcher.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8"
        )
        launcher.chmod(0o755)
    return str(launcher)


def _run_with_context(
    tmp_path: Path, prompt: Path, fake: Path, *, phase_id: str = "T13-P2"
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "src",
            "run-phase",
            "--prompt-file",
            str(prompt),
            "--phase-id",
            phase_id,
            "--output-dir",
            str(tmp_path / ".ztr" / "runs"),
            "--implementer-cmd",
            json.dumps([sys.executable, str(fake)]),
            "--goal-intent-context-file",
            "audit/context.json",
            "--timeout",
            "10",
        ],
        cwd=tmp_path,
        env=_env(),
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=30,
        check=False,
    )


def _blocked_error_code(proc: subprocess.CompletedProcess[str]) -> str:
    payload = json.loads(proc.stdout)
    assert payload["status"] == "BLOCKED"
    assert payload["exit_code"] == 70
    return str(payload["stderr_sanitized"])


def test_real_run_bootstraps_checkpoint_and_p1_checker_passes(tmp_path: Path) -> None:
    prompt, fake, _ = _init_repo(tmp_path)

    proc = _run_with_context(tmp_path, prompt, fake)

    assert proc.returncode == 0, proc.stderr
    emitted = proc.stdout.encode("utf-8")
    stored_envelope = (tmp_path / "audit" / "run-envelope.json").read_bytes()
    assert emitted == stored_envelope
    report = json.loads((tmp_path / "audit" / "run-report.json").read_text(encoding="utf-8"))
    assert report["summary"]["verdict"] == "PASS"
    ledger_bytes = (tmp_path / "audit" / "trajectory.jsonl").read_bytes()
    entries = [json.loads(line) for line in ledger_bytes.decode("utf-8").splitlines()]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["action_type"] == "CHECKPOINT_RECORDED"
    assert entry["sequence"] == 1
    assert entry["previous_entry_digest"] is None
    assert entry["changed_paths"] == ["changed.py"]
    assert [row["artifact_id"] for row in entry["artifact_refs"]] == [
        "run_envelope",
        "run_report",
    ]
    assert "CONTRACT_ACTIVATED" not in ledger_bytes.decode("utf-8")

    contract_path = tmp_path / "audit" / "contract.json"
    contract_sha = hashlib.sha256(contract_path.read_bytes()).hexdigest()
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "contracts": [
            {
                "contract_id": "T13-P2",
                "revision": 1,
                "path": "audit/contract.json",
                "sha256": contract_sha,
            }
        ],
        "manifest_digest": "",
    }
    manifest["manifest_digest"] = _digest_without(manifest, "manifest_digest")
    _write_json(tmp_path / "audit" / "manifest.json", manifest)
    checked = subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--contract-manifest",
            "audit/manifest.json",
            "--contract",
            "audit/contract.json",
            "--ledger",
            "audit/trajectory.jsonl",
            "--artifact",
            "audit/phase-ledger.json",
            "--artifact",
            "audit/run-report.json",
            "--artifact",
            "audit/run-envelope.json",
        ],
        cwd=tmp_path,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert json.loads(checked.stdout)["status"] == "PASS"


def test_actual_context_run_then_actual_fix_round_passes_p1_checker(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text(".ztr/\n", encoding="utf-8")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("perform one deterministic fixture change", encoding="utf-8")
    launcher = _fake_codex_launcher(tmp_path)
    reviewer = tmp_path / "reviewer.py"
    reviewer.write_text("print('ZTR_VERDICT: CHANGES_REQUESTED')\n", encoding="utf-8")
    audit = tmp_path / "audit"
    audit.mkdir()
    contract_path = audit / "contract.json"
    phase_ledger = audit / "phase-ledger.json"
    _write_json(contract_path, _chain_contract())
    _write_json(phase_ledger, {"phase": "T13-P2", "status": "IMPLEMENTING"})
    run_context = _context()
    run_context["claimed_pass"] = []
    run_context["claim_evidence_links"] = []
    _write_json(audit / "run-context.json", run_context)

    run_cmd = [
        sys.executable,
        "-m",
        "src",
        "run-phase",
        "--prompt-file",
        str(prompt),
        "--phase-id",
        "T13-P2",
        "--output-dir",
        str(tmp_path / ".ztr" / "run-output"),
        "--implementer-cmd",
        json.dumps([launcher, "exec", "-"]),
        "--reviewer-cmd",
        json.dumps([sys.executable, str(reviewer)]),
        "--session-map",
        str(tmp_path / ".ztr" / "sessions.json"),
        "--implementer-resume-profile",
        "codex",
        "--goal-intent-context-file",
        "audit/run-context.json",
        "--timeout",
        "10",
    ]
    run = subprocess.run(
        run_cmd,
        cwd=tmp_path,
        env=_env(),
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert run.returncode == 1, run.stdout + run.stderr
    assert json.loads(run.stdout)["status"] == "CHANGES_REQUESTED"
    run_report = json.loads((audit / "run-report.json").read_text(encoding="utf-8"))
    approve_findings = findings_digest(extract_findings(run_report))

    fix_context = {
        **run_context,
        "report_artifact": {
            "artifact_id": "fix_report",
            "artifact_type": "phase_relay_report_v1",
            "path": "audit/fix-report.json",
        },
        "envelope_artifact": {
            "artifact_id": "fix_envelope",
            "artifact_type": "ztr_envelope_v2",
            "path": "audit/fix-envelope.json",
        },
    }
    _write_json(audit / "fix-context.json", fix_context)
    fix_cmd = [
        sys.executable,
        "-m",
        "src",
        "fix-round",
        "--report-file",
        str(audit / "run-report.json"),
        "--prompt-file",
        str(prompt),
        "--ledger",
        str(tmp_path / ".ztr" / "reapply.json"),
        "--max-rounds",
        "3",
        "--approve-round",
        "1",
        "--approve-findings",
        approve_findings,
        "--phase-id",
        "T13-P2",
        "--output-dir",
        str(tmp_path / ".ztr" / "fix-output"),
        "--implementer-cmd",
        json.dumps([launcher, "exec", "-"]),
        "--session-map",
        str(tmp_path / ".ztr" / "sessions.json"),
        "--implementer-resume-profile",
        "codex",
        "--goal-intent-context-file",
        "audit/fix-context.json",
        "--timeout",
        "10",
    ]
    fix = subprocess.run(
        fix_cmd,
        cwd=tmp_path,
        env=_env(),
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert fix.returncode == 0, fix.stdout + fix.stderr
    assert json.loads(fix.stdout)["status"] == "PASS"

    entries = [
        json.loads(line)
        for line in (audit / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [entry["action_type"] for entry in entries] == [
        "CHECKPOINT_RECORDED",
        "ROUND_RECORDED",
    ]
    assert [entry["sequence"] for entry in entries] == [1, 2]
    assert entries[0]["previous_entry_digest"] is None
    assert entries[1]["previous_entry_digest"] == entries[0]["entry_digest"]
    refs = [ref for entry in entries for ref in entry["artifact_refs"]]
    assert len({ref["artifact_id"] for ref in refs}) == 4
    assert len({ref["path"] for ref in refs}) == 4
    for ref in refs:
        assert ref["sha256"] == hashlib.sha256((tmp_path / ref["path"]).read_bytes()).hexdigest()

    contract_sha = hashlib.sha256(contract_path.read_bytes()).hexdigest()
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "contracts": [
            {
                "contract_id": "T13-P2",
                "revision": 1,
                "path": "audit/contract.json",
                "sha256": contract_sha,
            }
        ],
        "manifest_digest": "",
    }
    manifest["manifest_digest"] = _digest_without(manifest, "manifest_digest")
    _write_json(audit / "manifest.json", manifest)
    checked = subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--contract-manifest",
            "audit/manifest.json",
            "--contract",
            "audit/contract.json",
            "--ledger",
            "audit/trajectory.jsonl",
            "--artifact",
            "audit/phase-ledger.json",
            "--artifact",
            "audit/run-report.json",
            "--artifact",
            "audit/run-envelope.json",
            "--artifact",
            "audit/fix-report.json",
            "--artifact",
            "audit/fix-envelope.json",
        ],
        cwd=tmp_path,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert json.loads(checked.stdout)["status"] == "PASS"


def test_run_repo_root_failure_uses_closed_error_before_mutation(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("no repo", encoding="utf-8")
    fake = tmp_path / "fake.py"
    fake.write_text("raise SystemExit('must not run')\n", encoding="utf-8")

    proc = _run_with_context(tmp_path, prompt, fake)

    assert proc.returncode == 70
    error = _blocked_error_code(proc)
    assert "GOAL_INTENT_CONTEXT_INVALID" in error
    assert "fatal:" not in error
    assert not (tmp_path / "changed.py").exists()
    assert not (tmp_path / ".ztr").exists()


@pytest.mark.parametrize("value", ["phase:alias", "CON", "con.txt", "bad.", "bad "])
def test_portable_phase_id_rejects_aliases(value: str) -> None:
    with pytest.raises(GoalIntentError) as raised:
        _portable_phase_id(value)
    assert raised.value.code == "GOAL_INTENT_ID_INVALID"


@pytest.mark.parametrize("data", [b"", b" \n", b"{}\n", b'{"x":1}\n'])
def test_existing_invalid_ledger_is_never_empty_bootstrap(tmp_path: Path, data: bytes) -> None:
    path = tmp_path / "ledger.jsonl"
    path.write_bytes(data)
    with pytest.raises(GoalIntentError) as raised:
        _load_ledger(path, allow_absent=True)
    assert raised.value.code == "GOAL_INTENT_LEDGER_INVALID"


def test_absent_ledger_bootstrap_is_run_only(tmp_path: Path) -> None:
    path = tmp_path / "missing.jsonl"
    assert _load_ledger(path, allow_absent=True) == (None, ())
    with pytest.raises(GoalIntentError) as raised:
        _load_ledger(path, allow_absent=False)
    assert raised.value.code == "GOAL_INTENT_LEDGER_INVALID"


def test_dangling_ledger_node_is_not_absent_bootstrap(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    try:
        ledger.symlink_to(tmp_path / "missing-ledger-target")
    except OSError as exc:
        pytest.skip(f"symlink capability unavailable: {exc}")
    assert not ledger.exists()
    assert os.path.lexists(ledger)

    with pytest.raises(GoalIntentError) as raised:
        _load_ledger(ledger, allow_absent=True)

    assert raised.value.code == "GOAL_INTENT_LEDGER_INVALID"


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(lambda row: row.__setitem__("schema_version", True), id="bool-schema"),
        pytest.param(lambda row: row.__setitem__("sequence", True), id="bool-sequence"),
        pytest.param(lambda row: row.__setitem__("role", "MANAGER"), id="role-enum"),
        pytest.param(lambda row: row.__setitem__("action_type", "AUTO_APPROVED"), id="action-enum"),
        pytest.param(
            lambda row: row["contract_ref"].__setitem__("revision", True),
            id="bool-contract-revision",
        ),
        pytest.param(
            lambda row: row["phase_ledger_ref"].__setitem__("artifact_type", "phase_ledger"),
            id="phase-ref-type",
        ),
        pytest.param(
            lambda row: row.__setitem__("changed_paths", ["z.py", "a.py"]),
            id="changed-path-order",
        ),
        pytest.param(
            lambda row: row.__setitem__("claimed_pass", ["relay_pass", "relay_pass"]),
            id="claim-duplicate",
        ),
        pytest.param(
            lambda row: row.__setitem__("not_claimed", ["relay_pass"]),
            id="claim-conflict",
        ),
        pytest.param(
            lambda row: row["claim_evidence_links"][0].__setitem__(
                "artifact_ids", ["z", "a"]
            ),
            id="link-order",
        ),
        pytest.param(
            lambda row: row["artifact_refs"][0].__setitem__("path", "./audit/prior.json"),
            id="artifact-path-type",
        ),
    ],
)
def test_digest_valid_malformed_history_is_rejected(
    tmp_path: Path, mutation: Any
) -> None:
    entry = _ledger_entry()
    mutation(entry)
    _rewrite_digest(entry)
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_bytes(canonical_json_line(entry))

    with pytest.raises(GoalIntentError) as raised:
        _load_ledger(ledger, allow_absent=True)

    assert raised.value.code == "GOAL_INTENT_LEDGER_INVALID"


@pytest.mark.parametrize("reuse", ["id", "path"])
def test_history_rejects_global_artifact_identity_reuse(tmp_path: Path, reuse: str) -> None:
    first = _ledger_entry()
    second = _ledger_entry(
        sequence=2,
        previous=first["entry_digest"],
        artifact_id="prior_report" if reuse == "id" else "second_report",
        artifact_path=(
            "audit/second-report.json" if reuse == "id" else "audit/prior-report.json"
        ),
    )
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_bytes(canonical_json_line(first) + canonical_json_line(second))

    with pytest.raises(GoalIntentError) as raised:
        _load_ledger(ledger, allow_absent=True)

    assert raised.value.code == "GOAL_INTENT_LEDGER_INVALID"


def test_malformed_history_blocks_before_relay_or_mutation(tmp_path: Path) -> None:
    prompt, fake, _ = _init_repo(tmp_path)
    entry = _ledger_entry()
    entry["role"] = "MANAGER"
    _rewrite_digest(entry)
    (tmp_path / "audit" / "trajectory.jsonl").write_bytes(canonical_json_line(entry))

    proc = _run_with_context(tmp_path, prompt, fake)

    assert proc.returncode == 70
    assert "GOAL_INTENT_LEDGER_INVALID" in _blocked_error_code(proc)
    assert not (tmp_path / "changed.py").exists()
    assert not (tmp_path / ".ztr" / "runs").exists()
    assert not (tmp_path / "audit" / "run-report.json").exists()
    assert not (tmp_path / "audit" / "run-envelope.json").exists()


def _semantic_history_fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, Any]]:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text(".ztr/\n", encoding="utf-8")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("must not reach relay", encoding="utf-8")
    fake = tmp_path / "fake.py"
    fake.write_text(
        "from pathlib import Path\nPath('changed.py').write_text('mutated')\n",
        encoding="utf-8",
    )
    audit = tmp_path / "audit"
    audit.mkdir()
    contract = audit / "contract.json"
    phase_ledger = audit / "phase-ledger.json"
    prior_report = audit / "prior-report.json"
    prior_envelope = audit / "prior-envelope.json"
    _write_json(contract, _semantic_history_contract())
    _write_json(phase_ledger, {"phase": "T13-P2", "status": "IMPLEMENTING"})
    _write_json(prior_report, {"summary": {"verdict": "PASS"}})
    prior_envelope.write_bytes(
        canonical_json_line(
            Envelope.from_verdict(
                status=Verdict.PASS,
                backend="phase-relay",
                model="fixture",
                duration_s=0.0,
            ).as_stdout_payload()
        )
    )
    _write_json(audit / "context.json", _context())
    entry: dict[str, Any] = {
        "schema_version": 1,
        "sequence": 1,
        "phase_id": "T13-P2",
        "round_id": "round-0",
        "leg_id": "run-phase",
        "role": "ORCHESTRATOR",
        "action_type": "CHECKPOINT_RECORDED",
        "contract_ref": {
            "contract_id": "T13-P2",
            "revision": 1,
            "path": "audit/contract.json",
            "sha256": hashlib.sha256(contract.read_bytes()).hexdigest(),
        },
        "phase_ledger_ref": {
            "artifact_type": "canonical_phase_ledger_snapshot",
            "path": "audit/phase-ledger.json",
            "sha256": hashlib.sha256(phase_ledger.read_bytes()).hexdigest(),
        },
        "changed_paths": ["changed.py"],
        "artifact_refs": [
            {
                "artifact_id": "prior_envelope",
                "artifact_type": "ztr_envelope_v2",
                "path": "audit/prior-envelope.json",
                "sha256": hashlib.sha256(prior_envelope.read_bytes()).hexdigest(),
            },
            {
                "artifact_id": "prior_report",
                "artifact_type": "phase_relay_report_v1",
                "path": "audit/prior-report.json",
                "sha256": hashlib.sha256(prior_report.read_bytes()).hexdigest(),
            },
        ],
        "claimed_pass": ["prior_pass"],
        "not_claimed": [],
        "claim_evidence_links": [
            {
                "claim_id": "prior_pass",
                "artifact_ids": ["prior_report"],
                "verdict_source_ids": ["prior_verdict"],
            }
        ],
        "previous_entry_digest": None,
        "entry_digest": "",
    }
    _rewrite_digest(entry)
    return prompt, fake, entry


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(
            lambda row: row.__setitem__("changed_paths", ["../escape.py"]),
            id="noncanonical-changed-path",
        ),
        pytest.param(
            lambda row: row.__setitem__("claimed_pass", ["undeclared_claim"]),
            id="undeclared-claim",
        ),
        pytest.param(
            lambda row: row.__setitem__("claim_evidence_links", []),
            id="claimed-pass-without-link",
        ),
        pytest.param(
            lambda row: row["claim_evidence_links"][0].__setitem__(
                "artifact_ids", ["missing_artifact"]
            ),
            id="missing-linked-artifact",
        ),
        pytest.param(
            lambda row: row["claim_evidence_links"][0].__setitem__(
                "verdict_source_ids", ["missing_source"]
            ),
            id="missing-linked-verdict-source",
        ),
        pytest.param(
            lambda row: row["artifact_refs"][1].__setitem__(
                "artifact_type", "ztr_envelope_v2"
            ),
            id="artifact-identity-mismatch",
        ),
    ],
)
def test_p1_incompatible_history_blocks_before_command_mutation(
    tmp_path: Path, mutation: Any
) -> None:
    prompt, fake, entry = _semantic_history_fixture(tmp_path)
    mutation(entry)
    _rewrite_digest(entry)
    (tmp_path / "audit" / "trajectory.jsonl").write_bytes(canonical_json_line(entry))

    proc = _run_with_context(tmp_path, prompt, fake)

    assert proc.returncode == 70
    assert "GOAL_INTENT_LEDGER_INVALID" in _blocked_error_code(proc)
    assert not (tmp_path / "changed.py").exists()
    assert not (tmp_path / ".ztr" / "runs").exists()
    assert not (tmp_path / "audit" / "run-report.json").exists()
    assert not (tmp_path / "audit" / "run-envelope.json").exists()


@pytest.mark.parametrize(
    "case",
    ["portable-phase", "generated-id", "prior-id", "prior-path", "reserved-root"],
)
def test_first_write_barrier_rejects_ids_history_and_reserved_root(
    tmp_path: Path, case: str
) -> None:
    prompt, fake, context_path = _init_repo(tmp_path)
    context = json.loads(context_path.read_text(encoding="utf-8"))
    phase_id = "T13-P2"
    expected_code = "GOAL_INTENT_ARTIFACT_COLLISION"
    if case == "portable-phase":
        phase_id = "T13:P2"
        expected_code = "GOAL_INTENT_ID_INVALID"
    elif case == "generated-id":
        context["report_artifact"]["artifact_id"] = "bad id"
        _write_json(context_path, context)
        expected_code = "GOAL_INTENT_ID_INVALID"
    elif case in {"prior-id", "prior-path"}:
        first = _ledger_entry(
            artifact_id=("run_report" if case == "prior-id" else "prior_report"),
            artifact_path=(
                "audit/prior-report.json"
                if case == "prior-id"
                else "audit/run-report.json"
            ),
        )
        (tmp_path / "audit" / "trajectory.jsonl").write_bytes(canonical_json_line(first))
    else:
        context["report_artifact"]["path"] = ".ztr/runs/t13-report.json"
        _write_json(context_path, context)

    proc = _run_with_context(tmp_path, prompt, fake, phase_id=phase_id)

    assert proc.returncode == 70
    assert expected_code in _blocked_error_code(proc)
    assert not (tmp_path / "changed.py").exists()
    assert not (tmp_path / ".ztr" / "runs").exists()
    assert not (tmp_path / "audit" / "run-envelope.json").exists()


@pytest.mark.parametrize(
    "relative",
    [
        "audit/run-report.json.tmp",
        "audit/run-envelope.json.tmp",
        "audit/trajectory.jsonl.tmp",
        "audit/trajectory.jsonl.lock",
    ],
)
def test_preexisting_deterministic_temp_or_lock_blocks_before_relay(
    tmp_path: Path, relative: str
) -> None:
    prompt, fake, _ = _init_repo(tmp_path)
    stale = tmp_path / relative
    stale.write_bytes(b"stale")

    proc = _run_with_context(tmp_path, prompt, fake)

    assert proc.returncode == 70
    assert "GOAL_INTENT_ARTIFACT_COLLISION" in _blocked_error_code(proc)
    assert stale.read_bytes() == b"stale"
    assert not (tmp_path / "changed.py").exists()
    assert not (tmp_path / ".ztr" / "runs").exists()
    assert not (tmp_path / "audit" / "run-report.json").exists()
    assert not (tmp_path / "audit" / "run-envelope.json").exists()
    assert not (tmp_path / "audit" / "trajectory.jsonl").exists()


def test_preexisting_broken_symlink_temp_is_not_treated_as_absent(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("Windows symlink capability is environment-dependent")
    prompt, fake, _ = _init_repo(tmp_path)
    stale = tmp_path / "audit" / "run-report.json.tmp"
    stale.symlink_to(tmp_path / "missing-target")

    proc = _run_with_context(tmp_path, prompt, fake)

    assert proc.returncode == 70
    assert "GOAL_INTENT_ARTIFACT_COLLISION" in _blocked_error_code(proc)
    assert stale.is_symlink()
    assert not (tmp_path / "changed.py").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows path equality capability row")
def test_windows_case_alias_of_protected_input_is_rejected(tmp_path: Path) -> None:
    prompt, fake, context_path = _init_repo(tmp_path)
    context = json.loads(context_path.read_text(encoding="utf-8"))
    context["report_artifact"]["path"] = "AUDIT/CONTRACT.JSON"
    _write_json(context_path, context)

    proc = _run_with_context(tmp_path, prompt, fake)

    assert proc.returncode == 70
    assert "GOAL_INTENT_ARTIFACT_COLLISION" in _blocked_error_code(proc)
    assert not (tmp_path / "changed.py").exists()


def test_existing_parent_symlink_alias_collides_with_prior_artifact(tmp_path: Path) -> None:
    prompt, fake, context_path = _init_repo(tmp_path)
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(tmp_path / "audit", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")
    first = _ledger_entry(artifact_path="audit/prior-report.json")
    (tmp_path / "audit" / "trajectory.jsonl").write_bytes(canonical_json_line(first))
    context = json.loads(context_path.read_text(encoding="utf-8"))
    context["report_artifact"]["path"] = "alias/prior-report.json"
    _write_json(context_path, context)

    proc = _run_with_context(tmp_path, prompt, fake)

    assert proc.returncode == 70
    assert "GOAL_INTENT_ARTIFACT_COLLISION" in _blocked_error_code(proc)
    assert not (tmp_path / "changed.py").exists()
    assert not (tmp_path / "audit" / "prior-report.json").exists()


@pytest.mark.parametrize("raw", [":memory:", "./:memory:", ".\\:memory:"])
def test_session_store_memory_lexical_source_parity(tmp_path: Path, raw: str) -> None:
    config = RoundtableConfig(agents=[], session=SessionConfig(db_path=raw))
    assert _session_db_nodes(config, process_cwd=tmp_path) == []


def test_relative_session_store_reserves_exact_cwd_sidecars(tmp_path: Path) -> None:
    config = RoundtableConfig(agents=[], session=SessionConfig(db_path="state/sessions.db"))
    nodes = _session_db_nodes(config, process_cwd=tmp_path)
    assert [label for label, _ in nodes] == [
        "session-db",
        "session-db-wal",
        "session-db-shm",
        "session-db-journal",
    ]
    assert [path for _, path in nodes] == [
        tmp_path / "state" / "sessions.db",
        tmp_path / "state" / "sessions.db-wal",
        tmp_path / "state" / "sessions.db-shm",
        tmp_path / "state" / "sessions.db-journal",
    ]


@pytest.mark.parametrize("right", ["owned", "owned/child"])
def test_path_graph_rejects_identity_and_component_ancestry(
    tmp_path: Path, right: str
) -> None:
    output_root = tmp_path / "output"
    with pytest.raises(GoalIntentError) as raised:
        _validate_graph(
            nodes=[("left", tmp_path / "owned"), ("right", tmp_path / right)],
            output_root=output_root,
            output_root_children=set(),
        )
    assert raised.value.code == "GOAL_INTENT_ARTIFACT_COLLISION"


@pytest.mark.asyncio
async def test_execution_window_fingerprints_cover_all_git_states_and_exclusions(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    tracked = {
        "predirty-edited.py": "base\n",
        "predirty-untouched.py": "base\n",
        "staged.py": "base\n",
        "deleted.py": "base\n",
    }
    for name, text in tracked.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=T13 Test",
            "-c",
            "user.email=t13@example.invalid",
            "commit",
            "-qm",
            "baseline",
        ],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / "predirty-edited.py").write_text("dirty-1\n", encoding="utf-8")
    (tmp_path / "predirty-untouched.py").write_text("dirty\n", encoding="utf-8")
    before = await capture_fingerprints(
        tmp_path, excluded_paths=frozenset({"audit/excluded.json"})
    )

    (tmp_path / "predirty-edited.py").write_text("dirty-2\n", encoding="utf-8")
    (tmp_path / "new.py").write_text("new\n", encoding="utf-8")
    (tmp_path / "staged.py").write_text("staged\n", encoding="utf-8")
    subprocess.run(["git", "add", "staged.py"], cwd=tmp_path, check=True)
    (tmp_path / "deleted.py").unlink()
    excluded = tmp_path / "audit" / "excluded.json"
    excluded.parent.mkdir()
    excluded.write_text("writer-owned\n", encoding="utf-8")
    after = await capture_fingerprints(
        tmp_path, excluded_paths=frozenset({"audit/excluded.json"})
    )

    assert changed_fingerprints(before, after) == [
        "deleted.py",
        "new.py",
        "predirty-edited.py",
        "staged.py",
    ]


def _preflight_fixture(tmp_path: Path) -> GoalIntentPreflight:
    contract = tmp_path / "contract.json"
    phase_ledger = tmp_path / "phase-ledger.json"
    contract.write_bytes(b'{}\n')
    phase_ledger.write_bytes(b'{}\n')
    context = GoalIntentContext(
        context_path=tmp_path / "context.json",
        context_logical_path="context.json",
        ledger_logical_path="ledger.jsonl",
        ledger_path=tmp_path / "ledger.jsonl",
        contract_id="T13-P2",
        contract_revision=1,
        contract_logical_path="contract.json",
        contract_path=contract,
        phase_ledger_logical_path="phase-ledger.json",
        phase_ledger_path=phase_ledger,
        report=ArtifactSpec(
            artifact_id="report",
            artifact_type="phase_relay_report_v1",
            logical_path="report.json",
            path=tmp_path / "report.json",
        ),
        envelope=ArtifactSpec(
            artifact_id="envelope",
            artifact_type="ztr_envelope_v2",
            logical_path="envelope.json",
            path=tmp_path / "envelope.json",
        ),
        claimed_pass=(),
        not_claimed=(),
        claim_evidence_links=(),
    )
    return GoalIntentPreflight(
        kind="run-phase",
        process_cwd=tmp_path,
        repo_root=tmp_path,
        context=context,
        phase_id="T13-P2",
        round_index=0,
        action_type="CHECKPOINT_RECORDED",
        round_id="round-0",
        leg_id="run-phase",
        ledger_bytes=None,
        entries=(),
        protected_bytes=(),
        relay_mutable_paths=frozenset(),
        excluded_paths=frozenset(),
    )


def test_append_failure_leaves_artifacts_orphaned_and_ledger_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preflight = _preflight_fixture(tmp_path)
    envelope = Envelope.from_verdict(
        status=Verdict.PASS,
        backend="phase-relay",
        model="external-cli",
        duration_s=0.0,
    )

    def fail_append(*_: object, **__: object) -> None:
        raise GoalIntentError("GOAL_INTENT_APPEND_FAILED", "injected")

    monkeypatch.setattr(
        "src.engine.goal_intent_ledger._append_entry",
        fail_append,
    )
    with pytest.raises(GoalIntentError) as raised:
        persist_and_append(
            preflight,
            report_payload={"summary": {"verdict": "PASS"}},
            envelope=envelope,
            changed_paths=[],
        )

    assert raised.value.code == "GOAL_INTENT_APPEND_FAILED"
    assert preflight.context.report.path.is_file()
    assert preflight.context.envelope.path.is_file()
    assert not preflight.context.ledger_path.exists()
    assert not preflight.context.ledger_path.with_name("ledger.jsonl.tmp").exists()
    assert not preflight.context.ledger_path.with_name("ledger.jsonl.lock").exists()


def test_append_content_race_preserves_foreign_ledger_and_cleans_owned_nodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preflight = _preflight_fixture(tmp_path)
    foreign = canonical_json_line(_ledger_entry())
    original_write = __import__(
        "src.engine.goal_intent_ledger", fromlist=["_write_temp_bytes"]
    )._write_temp_bytes

    def inject_race(path: Path, data: bytes) -> None:
        original_write(path, data)
        preflight.context.ledger_path.write_bytes(foreign)

    monkeypatch.setattr(
        "src.engine.goal_intent_ledger._write_temp_bytes",
        inject_race,
    )
    with pytest.raises(GoalIntentError) as raised:
        _append_entry(preflight, _ledger_entry())

    assert raised.value.code == "GOAL_INTENT_LEDGER_CHANGED"
    assert preflight.context.ledger_path.read_bytes() == foreign
    assert not preflight.context.ledger_path.with_name("ledger.jsonl.tmp").exists()
    assert not preflight.context.ledger_path.with_name("ledger.jsonl.lock").exists()


@pytest.mark.parametrize("operation", ["write", "flush", "fsync", "close"])
def test_artifact_temp_io_failure_cleans_owned_temp_and_retry_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: _TempFailure,
) -> None:
    artifact = tmp_path / "artifact.json"
    temp = artifact.with_name("artifact.json.tmp")

    with monkeypatch.context() as injected:
        _inject_temp_failure(injected, operation)
        with pytest.raises(GoalIntentError) as raised:
            _write_once(artifact, b'{}\n')

    assert raised.value.code == "GOAL_INTENT_ARTIFACT_FAILED"
    assert not artifact.exists()
    assert not temp.exists()

    _write_once(artifact, b'{}\n')
    assert artifact.read_bytes() == b'{}\n'
    assert not temp.exists()


@pytest.mark.parametrize("operation", ["write", "flush", "fsync", "close"])
def test_ledger_temp_io_failure_cleans_owned_nodes_and_retry_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: _TempFailure,
) -> None:
    preflight = _preflight_fixture(tmp_path)
    entry = _ledger_entry()
    ledger = preflight.context.ledger_path
    temp = ledger.with_name("ledger.jsonl.tmp")
    lock = ledger.with_name("ledger.jsonl.lock")

    with monkeypatch.context() as injected:
        _inject_temp_failure(injected, operation)
        with pytest.raises(GoalIntentError) as raised:
            _append_entry(preflight, entry)

    assert raised.value.code == "GOAL_INTENT_APPEND_FAILED"
    assert not ledger.exists()
    assert not temp.exists()
    assert not lock.exists()

    _append_entry(preflight, entry)
    assert ledger.read_bytes() == canonical_json_line(entry)
    assert not temp.exists()
    assert not lock.exists()


def test_temp_prewrite_collision_preserves_foreign_bytes(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.json"
    temp = artifact.with_name("artifact.json.tmp")
    temp.write_bytes(b"foreign")

    with pytest.raises(GoalIntentError) as raised:
        _write_once(artifact, b'{}\n')

    assert raised.value.code == "GOAL_INTENT_ARTIFACT_COLLISION"
    assert not artifact.exists()
    assert temp.read_bytes() == b"foreign"


def test_ledger_temp_prewrite_collision_preserves_foreign_bytes_and_cleans_lock(
    tmp_path: Path,
) -> None:
    preflight = _preflight_fixture(tmp_path)
    ledger = preflight.context.ledger_path
    temp = ledger.with_name("ledger.jsonl.tmp")
    lock = ledger.with_name("ledger.jsonl.lock")
    temp.write_bytes(b"foreign")

    with pytest.raises(GoalIntentError) as raised:
        _append_entry(preflight, _ledger_entry())

    assert raised.value.code == "GOAL_INTENT_APPEND_FAILED"
    assert not ledger.exists()
    assert temp.read_bytes() == b"foreign"
    assert not lock.exists()


def test_temp_cleanup_failure_surfaces_original_and_cleanup_reasons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temp = tmp_path / "owned.tmp"
    real_unlink = Path.unlink

    def fail_fsync(_: int) -> None:
        raise OSError("injected fsync failure")

    def fail_owned_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == temp:
            raise OSError("injected cleanup failure")
        real_unlink(path, missing_ok=missing_ok)

    with monkeypatch.context() as injected:
        injected.setattr(os, "fsync", fail_fsync)
        injected.setattr(Path, "unlink", fail_owned_unlink)
        with pytest.raises(OSError) as raised:
            _write_temp_bytes(temp, b"payload")

    assert "injected fsync failure" in str(raised.value)
    assert "owned temp cleanup failed: injected cleanup failure" in str(raised.value)
    assert temp.read_bytes() == b"payload"
    temp.unlink()


def test_artifact_link_failure_cleans_owned_temp_and_preserves_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "artifact.json"
    temp = artifact.with_name("artifact.json.tmp")

    def fail_link(_: Path, __: Path) -> None:
        raise OSError("injected link failure")

    with monkeypatch.context() as injected:
        injected.setattr(os, "link", fail_link)
        with pytest.raises(GoalIntentError) as raised:
            _write_once(artifact, b'{}\n')

    assert raised.value.code == "GOAL_INTENT_ARTIFACT_FAILED"
    assert not artifact.exists()
    assert not temp.exists()


def test_ledger_replace_failure_preserves_bytes_and_cleans_owned_nodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _ledger_entry()
    first_bytes = canonical_json_line(first)
    preflight = _preflight_fixture(tmp_path)
    ledger = preflight.context.ledger_path
    ledger.write_bytes(first_bytes)
    preflight = replace(preflight, ledger_bytes=first_bytes, entries=(first,))
    second = _ledger_entry(
        sequence=2,
        previous=first["entry_digest"],
        artifact_id="second_report",
        artifact_path="audit/second-report.json",
    )
    temp = ledger.with_name("ledger.jsonl.tmp")
    lock = ledger.with_name("ledger.jsonl.lock")

    def fail_replace(_: Path, __: Path) -> None:
        raise OSError("injected replace failure")

    with monkeypatch.context() as injected:
        injected.setattr(os, "replace", fail_replace)
        with pytest.raises(GoalIntentError) as raised:
            _append_entry(preflight, second)

    assert raised.value.code == "GOAL_INTENT_APPEND_FAILED"
    assert ledger.read_bytes() == first_bytes
    assert not temp.exists()
    assert not lock.exists()


def test_ledger_bootstrap_link_failure_cleans_owned_nodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preflight = _preflight_fixture(tmp_path)
    ledger = preflight.context.ledger_path
    temp = ledger.with_name("ledger.jsonl.tmp")
    lock = ledger.with_name("ledger.jsonl.lock")

    def fail_link(_: Path, __: Path) -> None:
        raise OSError("injected ledger link failure")

    with monkeypatch.context() as injected:
        injected.setattr(os, "link", fail_link)
        with pytest.raises(GoalIntentError) as raised:
            _append_entry(preflight, _ledger_entry())

    assert raised.value.code == "GOAL_INTENT_APPEND_FAILED"
    assert not ledger.exists()
    assert not temp.exists()
    assert not lock.exists()


def test_artifact_link_and_owned_temp_cleanup_failures_are_composed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "artifact.json"
    temp = artifact.with_name("artifact.json.tmp")
    real_unlink = Path.unlink

    def fail_link(_: Path, __: Path) -> None:
        raise OSError("injected link failure")

    def fail_owned_temp_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == temp:
            raise OSError("injected temp unlink failure")
        real_unlink(path, missing_ok=missing_ok)

    with monkeypatch.context() as injected:
        injected.setattr(os, "link", fail_link)
        injected.setattr(Path, "unlink", fail_owned_temp_unlink)
        with pytest.raises(GoalIntentError) as raised:
            _write_once(artifact, b'{}\n')

    assert raised.value.code == "GOAL_INTENT_ARTIFACT_FAILED"
    assert raised.value.detail == (
        "injected link failure; "
        f"artifact temp cleanup failed "
        f"(path={temp}; state=cleanup-unconfirmed; retry_safe=false): "
        "injected temp unlink failure"
    )
    assert not artifact.exists()
    assert temp.read_bytes() == b'{}\n'

    real_unlink(temp)
    temp.write_bytes(b"foreign")
    with pytest.raises(GoalIntentError) as foreign_collision:
        _write_once(artifact, b'{}\n')
    assert foreign_collision.value.code == "GOAL_INTENT_ARTIFACT_COLLISION"
    assert temp.read_bytes() == b"foreign"

    real_unlink(temp)
    _write_once(artifact, b'{}\n')
    assert artifact.read_bytes() == b'{}\n'
    assert not temp.exists()


def test_ledger_replace_and_owned_temp_cleanup_failures_are_composed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _ledger_entry()
    first_bytes = canonical_json_line(first)
    preflight = _preflight_fixture(tmp_path)
    ledger = preflight.context.ledger_path
    ledger.write_bytes(first_bytes)
    preflight = replace(preflight, ledger_bytes=first_bytes, entries=(first,))
    second = _ledger_entry(
        sequence=2,
        previous=first["entry_digest"],
        artifact_id="second_report",
        artifact_path="audit/second-report.json",
    )
    second_bytes = canonical_json_line(second)
    temp = ledger.with_name("ledger.jsonl.tmp")
    lock = ledger.with_name("ledger.jsonl.lock")
    real_unlink = Path.unlink

    def fail_replace(_: Path, __: Path) -> None:
        raise OSError("injected replace failure")

    def fail_owned_temp_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == temp:
            raise OSError("injected temp unlink failure")
        real_unlink(path, missing_ok=missing_ok)

    with monkeypatch.context() as injected:
        injected.setattr(os, "replace", fail_replace)
        injected.setattr(Path, "unlink", fail_owned_temp_unlink)
        with pytest.raises(GoalIntentError) as raised:
            _append_entry(preflight, second)

    assert raised.value.code == "GOAL_INTENT_APPEND_FAILED"
    assert raised.value.detail == (
        "injected replace failure; "
        f"ledger temp cleanup failed "
        f"(path={temp}; state=cleanup-unconfirmed; retry_safe=false): "
        "injected temp unlink failure"
    )
    assert ledger.read_bytes() == first_bytes
    assert temp.read_bytes() == first_bytes + second_bytes
    assert not lock.exists()

    real_unlink(temp)
    temp.write_bytes(b"foreign")
    with pytest.raises(GoalIntentError) as foreign_collision:
        _append_entry(preflight, second)
    assert foreign_collision.value.code == "GOAL_INTENT_APPEND_FAILED"
    assert ledger.read_bytes() == first_bytes
    assert temp.read_bytes() == b"foreign"
    assert not lock.exists()

    real_unlink(temp)
    _append_entry(preflight, second)
    assert ledger.read_bytes() == first_bytes + second_bytes
    assert not temp.exists()
    assert not lock.exists()


def test_ledger_primary_and_owned_lock_cleanup_failures_are_composed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _ledger_entry()
    first_bytes = canonical_json_line(first)
    preflight = _preflight_fixture(tmp_path)
    ledger = preflight.context.ledger_path
    ledger.write_bytes(first_bytes)
    preflight = replace(preflight, ledger_bytes=first_bytes, entries=(first,))
    second = _ledger_entry(
        sequence=2,
        previous=first["entry_digest"],
        artifact_id="second_report",
        artifact_path="audit/second-report.json",
    )
    second_bytes = canonical_json_line(second)
    temp = ledger.with_name("ledger.jsonl.tmp")
    lock = ledger.with_name("ledger.jsonl.lock")
    real_unlink = Path.unlink

    def fail_replace(_: Path, __: Path) -> None:
        raise OSError("injected replace failure")

    def fail_owned_lock_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == lock:
            raise OSError("injected lock unlink failure")
        real_unlink(path, missing_ok=missing_ok)

    with monkeypatch.context() as injected:
        injected.setattr(os, "replace", fail_replace)
        injected.setattr(Path, "unlink", fail_owned_lock_unlink)
        with pytest.raises(GoalIntentError) as raised:
            _append_entry(preflight, second)

    assert raised.value.code == "GOAL_INTENT_APPEND_FAILED"
    assert raised.value.detail == (
        "injected replace failure; "
        f"ledger lock cleanup failed "
        f"(path={lock}; state=cleanup-unconfirmed; retry_safe=false): "
        "injected lock unlink failure"
    )
    assert ledger.read_bytes() == first_bytes
    assert not temp.exists()
    assert lock.is_file()

    real_unlink(lock)
    lock.write_bytes(b"foreign")
    with pytest.raises(GoalIntentError) as foreign_collision:
        _append_entry(preflight, second)
    assert foreign_collision.value.code == "GOAL_INTENT_LEDGER_LOCKED"
    assert ledger.read_bytes() == first_bytes
    assert lock.read_bytes() == b"foreign"
    assert not temp.exists()

    real_unlink(lock)
    _append_entry(preflight, second)
    assert ledger.read_bytes() == first_bytes + second_bytes
    assert not temp.exists()
    assert not lock.exists()
