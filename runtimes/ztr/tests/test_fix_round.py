"""T10-A fix-round 결정론 1라운드/C1·C2·D1 회귀 테스트."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import tempfile
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.envelope import Envelope, Verdict
from src.engine.fix_feedback import extract_findings, select_accepted_findings
from src.engine.reapply_ledger import ReapplyLedger, findings_digest
from src.engine.goal_intent_ledger import canonical_json, canonical_json_line


def _args(tmp_path: Path) -> argparse.Namespace:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("원본 지시", encoding="utf-8")
    session_map = tmp_path / "sessions.json"
    session_map.write_text(json.dumps({"implementer": "thread-123"}), encoding="utf-8")
    return argparse.Namespace(
        prompt_file=str(prompt),
        report_file=str(tmp_path / "report.json"),
        ledger=str(tmp_path / "ledger.json"),
        max_rounds=3,
        approve_round=1,
        approve_findings="",
        out="",
        phase_id="t10-a",
        timeout=5.0,
        implementer_timeout=None,
        autofix_timeout=None,
        mechanical_timeout=None,
        test_timeout=None,
        reviewer_timeout=None,
        output_dir=str(tmp_path / "runs"),
        implementer_cmd='["codex", "exec", "-"]',
        reviewer_cmd="",
        autofix_cmd=None,
        mechanical_cmd="",
        test_cmd="",
        session_map=str(session_map),
        implementer_resume="new",
        reviewer_resume="new",
        implementer_resume_profile="codex",
        reviewer_resume_profile="none",
        reviewer_verdict_source="stdout_token",
        accept_leg=None,
        record=False,
        goal_intent_context_file=None,
    )


def test_fix_round_preserves_all_leg_timeout_overrides(tmp_path: Path) -> None:
    from src.runner import _relay_commands_from_args

    args = _args(tmp_path)
    args.autofix_cmd = ['["fix-1"]', '["fix-2"]']
    args.mechanical_cmd = '["mech"]'
    args.test_cmd = '["test"]'
    args.reviewer_cmd = '["review"]'
    args.implementer_timeout = 11.0
    args.autofix_timeout = 12.0
    args.mechanical_timeout = 13.0
    args.test_timeout = 14.0
    args.reviewer_timeout = 15.0

    commands = _relay_commands_from_args(args)

    assert [command.timeout_s for command in commands] == [
        11.0, 12.0, 12.0, 13.0, 14.0, 15.0
    ]


def _write_report(args: argparse.Namespace, verdict: str, *, step_status: str | None = None) -> None:
    steps: list[dict[str, Any]] = []
    if step_status is not None:
        steps.append({
            "name": "mechanical-review",
            "status": step_status,
            "gating": True,
            "skipped": False,
            "stdout_preview": "finding body",
        })
    Path(args.report_file).write_text(
        json.dumps({"steps": steps, "summary": {"verdict": verdict}}),
        encoding="utf-8",
    )
    payload = json.loads(Path(args.report_file).read_text(encoding="utf-8"))
    args.approve_findings = findings_digest(extract_findings(payload))


def _report(
    *,
    fallback: bool = False,
    resumed: bool = True,
    requested_id: str = "thread-123",
    captured_id: str = "thread-123",
) -> SimpleNamespace:
    step = SimpleNamespace(
        name="implementer",
        resume={
            "resumed": resumed,
            "requested_id": requested_id,
            "captured_session_id": captured_id,
        },
    )
    payload = {
        "steps": [{"name": "implementer", "status": "PASS", "resume": step.resume}],
        "summary": {"verdict": "PASS", "exit_code": 0},
        "resume": {"fallback_used": fallback},
    }
    return SimpleNamespace(
        status=Verdict.PASS,
        exit_code=0,
        steps=[step],
        resume_fallback_used=fallback,
        as_payload=lambda: payload,
    )


@pytest.mark.asyncio
async def test_fix_round_repo_root_failure_uses_closed_error_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    args = _args(tmp_path)
    args.goal_intent_context_file = "audit/context.json"
    relay_calls = 0
    recording_calls = 0

    async def fail_root(*_: object, **__: object) -> Path:
        raise RuntimeError("fatal: not a git repository")

    async def fake_run(*_: object, **__: object) -> Any:
        nonlocal relay_calls
        relay_calls += 1
        return _report()

    def fake_record(*_: object, **__: object) -> None:
        nonlocal recording_calls
        recording_calls += 1
        return None

    output = io.StringIO()
    monkeypatch.setattr(runner_mod, "resolve_repo_root", fail_root)
    monkeypatch.setattr(runner_mod, "_run_phase_once", fake_run)
    monkeypatch.setattr(runner_mod, "_start_recording", fake_record)
    monkeypatch.setattr(sys, "stdout", output)

    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_fix_round(args)

    assert raised.value.code == 70
    envelope = json.loads(output.getvalue())
    assert envelope["status"] == "BLOCKED"
    assert "GOAL_INTENT_CONTEXT_INVALID" in envelope["stderr_sanitized"]
    assert "fatal:" not in envelope["stderr_sanitized"]
    assert relay_calls == 0
    assert recording_calls == 0
    assert not Path(args.ledger).exists()
    assert not Path(args.output_dir).exists()


@pytest.mark.asyncio
async def test_fix_round_changes_builds_prompt_and_runs_relay_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    args = _args(tmp_path)
    _write_report(args, "CHANGES_REQUESTED", step_status="CHANGES_REQUESTED")
    calls = 0

    async def fake_run(*_: object, **__: object) -> Any:
        nonlocal calls
        calls += 1
        return _report()

    monkeypatch.setattr(runner_mod, "_run_phase_once", fake_run)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_fix_round(args)

    assert raised.value.code == 0
    assert calls == 1  # C1: relay/implementer 경로는 정확히 1회
    assert args.implementer_resume == "thread-123"  # auto가 아닌 명시 id
    fix_prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    assert "finding body" in fix_prompt
    ledger = ReapplyLedger.load(args.ledger)
    assert len(ledger.rounds) == 1
    assert f"{args.phase_id}-round-1-report.json" in ledger.rounds[0]["report_path"]
    assert f"{args.phase_id}-round-1-fix-prompt.md" in ledger.rounds[0]["fix_prompt_path"]
    assert (Path(args.output_dir) / f"{args.phase_id}-round-1-envelope.json").exists()


@pytest.mark.asyncio
async def test_fix_round_accept_leg_subset_drives_order_digest_and_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    args = _args(tmp_path)
    args.accept_leg = ["orchestrator-accepted-review", "leg-b"]
    payload = {
        "steps": [
            {"name": "leg-b", "status": "CHANGES_REQUESTED", "gating": True,
             "skipped": False, "stdout_preview": "B selected"},
            {"name": "not-selected", "status": "BLOCKED", "gating": True,
             "skipped": False, "stdout_preview": "SECRET BODY"},
            {"name": "orchestrator-accepted-review", "status": "CHANGES_REQUESTED",
             "gating": True, "skipped": False, "stdout_preview": "A selected"},
        ],
        "summary": {"verdict": "CHANGES_REQUESTED"},
    }
    Path(args.report_file).write_text(json.dumps(payload), encoding="utf-8")
    selected = select_accepted_findings(payload, args.accept_leg)
    args.approve_findings = findings_digest(selected)

    monkeypatch.setattr(runner_mod, "_run_phase_once", lambda *_args, **_kwargs: None)

    async def fake_run(*_: object, **__: object) -> Any:
        return _report()

    monkeypatch.setattr(runner_mod, "_run_phase_once", fake_run)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_fix_round(args)

    assert raised.value.code == 0
    prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    assert prompt.index("B selected") < prompt.index("A selected")
    assert "SECRET BODY" not in prompt
    ledger = ReapplyLedger.load(args.ledger)
    assert ledger.rounds[0]["input_digest"] == findings_digest(selected)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("accept_leg", "step"),
    [
        ([""], {"name": "leg", "status": "CHANGES_REQUESTED", "gating": True, "skipped": False}),
        (["leg", "leg"], {"name": "leg", "status": "CHANGES_REQUESTED", "gating": True, "skipped": False}),
        (["unknown"], {"name": "leg", "status": "CHANGES_REQUESTED", "gating": True, "skipped": False}),
        (["leg"], {"name": "leg", "status": "PASS", "gating": True, "skipped": False}),
        (["leg"], {"name": "leg", "status": "BLOCKED", "gating": True, "skipped": False}),
        (["leg"], {"name": "leg", "status": "CHANGES_REQUESTED", "gating": False, "skipped": False}),
        (["leg"], {"name": "leg", "status": "CHANGES_REQUESTED", "gating": True, "skipped": True}),
    ],
)
async def test_fix_round_invalid_accept_leg_blocks_before_spawn_or_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    accept_leg: list[str], step: dict[str, object],
) -> None:
    from src import runner as runner_mod

    args = _args(tmp_path)
    args.accept_leg = accept_leg
    Path(args.report_file).write_text(json.dumps({
        "steps": [step], "summary": {"verdict": "CHANGES_REQUESTED"},
    }), encoding="utf-8")
    calls = 0

    async def fake_run(*_: object, **__: object) -> Any:
        nonlocal calls
        calls += 1
        return _report()

    monkeypatch.setattr(runner_mod, "_run_phase_once", fake_run)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_fix_round(args)

    assert raised.value.code == 2
    assert calls == 0
    assert not Path(args.ledger).exists()
    assert not Path(args.output_dir).exists()


@pytest.mark.asyncio
async def test_fix_round_goal_intent_missing_context_blocks_before_recording_or_relay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    args = _args(tmp_path)
    args.goal_intent_context_file = "missing-goal-intent-context.json"
    _write_report(args, "CHANGES_REQUESTED", step_status="CHANGES_REQUESTED")
    relay_calls = 0
    recording_calls = 0

    async def fake_run(*_: object, **__: object) -> Any:
        nonlocal relay_calls
        relay_calls += 1
        return _report()

    def fake_record(*_: object, **__: object) -> None:
        nonlocal recording_calls
        recording_calls += 1
        return None

    monkeypatch.setattr(runner_mod, "_run_phase_once", fake_run)
    monkeypatch.setattr(runner_mod, "_start_recording", fake_record)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_fix_round(args)

    assert raised.value.code == 70
    assert relay_calls == 0
    assert recording_calls == 0
    assert not Path(args.ledger).exists()
    assert not Path(args.output_dir).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stale_kind",
    [
        None,
        "t10-ledger-temp",
        "session-map-temp",
        "fix-prompt",
        "t10-report",
        "t10-envelope",
    ],
)
async def test_fix_round_appends_second_goal_intent_entry_after_terminal_mapping(
    monkeypatch: pytest.MonkeyPatch, stale_kind: str | None,
) -> None:
    from src import runner as runner_mod

    repo_root = Path(__file__).resolve().parents[3]
    scratch_root = repo_root / ".ztr"
    scratch_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="t13-p2-fix-", dir=scratch_root) as raw:
        work = Path(raw)
        args = _args(work)
        _write_report(args, "CHANGES_REQUESTED", step_status="CHANGES_REQUESTED")
        audit = work / "audit"
        audit.mkdir()
        contract = audit / "contract.json"
        phase_ledger = audit / "phase-ledger.json"
        phase_ledger.write_bytes(canonical_json_line({"phase": "T13-P2"}))
        run_report = audit / "run-report.json"
        run_envelope = audit / "run-envelope.json"
        run_report.write_bytes(canonical_json_line({"summary": {"verdict": "CHANGES_REQUESTED"}}))
        run_envelope.write_bytes(
            canonical_json_line(
                Envelope.from_verdict(
                    status=Verdict.CHANGES_REQUESTED,
                    backend="phase-relay",
                    model="external-cli",
                    duration_s=0.0,
                ).as_stdout_payload()
            )
        )
        required_artifacts = [
            {
                "artifact_id": "fix_round_1_envelope",
                "artifact_type": "ztr_envelope_v2",
                "path": (audit / "fix-envelope.json").relative_to(repo_root).as_posix(),
            },
            {
                "artifact_id": "fix_round_1_report",
                "artifact_type": "phase_relay_report_v1",
                "path": (audit / "fix-report.json").relative_to(repo_root).as_posix(),
            },
            {
                "artifact_id": "run_envelope",
                "artifact_type": "ztr_envelope_v2",
                "path": run_envelope.relative_to(repo_root).as_posix(),
            },
            {
                "artifact_id": "run_report",
                "artifact_type": "phase_relay_report_v1",
                "path": run_report.relative_to(repo_root).as_posix(),
            },
        ]
        contract_value: dict[str, Any] = {
            "schema_version": 1,
            "contract_id": "T13-P2",
            "revision": 1,
            "parent_contract_digest": None,
            "objective": "preserve run and fix source facts",
            "non_goals": [],
            "allowed_write_scope": [],
            "forbidden_outcomes": [],
            "validation_claims": [
                {
                    "claim_id": "fix_pass",
                    "required_artifact_ids": ["fix_round_1_report"],
                    "required_verdict_source_ids": ["fix_verdict"],
                }
            ],
            "required_artifacts": required_artifacts,
            "required_verdict_sources": [
                {
                    "verdict_source_id": "fix_verdict",
                    "artifact_id": "fix_round_1_envelope",
                    "adapter": "ztr_envelope_v2",
                    "required_status": "PASS",
                    "required_exit_code": 0,
                }
            ],
            "contract_digest": "",
        }
        contract_value["contract_digest"] = hashlib.sha256(
            canonical_json(
                {key: value for key, value in contract_value.items() if key != "contract_digest"}
            )
        ).hexdigest()
        contract.write_bytes(canonical_json_line(contract_value))
        contract_row = {
            "contract_id": "T13-P2",
            "revision": 1,
            "path": contract.relative_to(repo_root).as_posix(),
            "sha256": hashlib.sha256(contract.read_bytes()).hexdigest(),
        }
        manifest_value: dict[str, Any] = {
            "schema_version": 1,
            "contracts": [contract_row],
            "manifest_digest": "",
        }
        manifest_value["manifest_digest"] = hashlib.sha256(
            canonical_json(
                {key: value for key, value in manifest_value.items() if key != "manifest_digest"}
            )
        ).hexdigest()
        manifest = audit / "manifest.json"
        manifest.write_bytes(canonical_json_line(manifest_value))
        ledger_path = audit / "trajectory.jsonl"
        first: dict[str, Any] = {
            "schema_version": 1,
            "sequence": 1,
            "phase_id": "T13-P2",
            "round_id": "round-0",
            "leg_id": "run-phase",
            "role": "ORCHESTRATOR",
            "action_type": "CHECKPOINT_RECORDED",
            "contract_ref": contract_row,
            "phase_ledger_ref": {
                "artifact_type": "canonical_phase_ledger_snapshot",
                "path": phase_ledger.relative_to(repo_root).as_posix(),
                "sha256": hashlib.sha256(phase_ledger.read_bytes()).hexdigest(),
            },
            "changed_paths": [],
            "artifact_refs": [
                {
                    **required_artifacts[2],
                    "sha256": hashlib.sha256(run_envelope.read_bytes()).hexdigest(),
                },
                {
                    **required_artifacts[3],
                    "sha256": hashlib.sha256(run_report.read_bytes()).hexdigest(),
                },
            ],
            "claimed_pass": [],
            "not_claimed": [],
            "claim_evidence_links": [],
            "previous_entry_digest": None,
            "entry_digest": "",
        }
        first["entry_digest"] = hashlib.sha256(
            canonical_json({key: value for key, value in first.items() if key != "entry_digest"})
        ).hexdigest()
        ledger_path.write_bytes(canonical_json_line(first))
        context = {
            "schema_version": 1,
            "ledger_path": ledger_path.relative_to(repo_root).as_posix(),
            "contract": {
                "contract_id": "T13-P2",
                "revision": 1,
                "path": contract.relative_to(repo_root).as_posix(),
            },
            "phase_ledger_path": phase_ledger.relative_to(repo_root).as_posix(),
            "report_artifact": {
                "artifact_id": "fix_round_1_report",
                "artifact_type": "phase_relay_report_v1",
                "path": (audit / "fix-report.json").relative_to(repo_root).as_posix(),
            },
            "envelope_artifact": {
                "artifact_id": "fix_round_1_envelope",
                "artifact_type": "ztr_envelope_v2",
                "path": (audit / "fix-envelope.json").relative_to(repo_root).as_posix(),
            },
            "claimed_pass": ["fix_pass"],
            "not_claimed": [],
            "claim_evidence_links": [
                {
                    "claim_id": "fix_pass",
                    "artifact_ids": ["fix_round_1_report"],
                    "verdict_source_ids": ["fix_verdict"],
                }
            ],
        }
        context_path = audit / "context.json"
        context_path.write_bytes(canonical_json_line(context))
        args.goal_intent_context_file = context_path.relative_to(repo_root).as_posix()
        stale_paths = {
            "t10-ledger-temp": Path(args.ledger).with_name(
                f".{Path(args.ledger).name}.{os.getpid()}.tmp"
            ),
            "session-map-temp": Path(args.session_map).with_name(
                f".{Path(args.session_map).name}.{os.getpid()}.tmp"
            ),
            "fix-prompt": Path(args.output_dir)
            / f"{args.phase_id}-round-1-fix-prompt.md",
            "t10-report": Path(args.output_dir) / f"{args.phase_id}-round-1-report.json",
            "t10-envelope": Path(args.output_dir)
            / f"{args.phase_id}-round-1-envelope.json",
        }
        original_trajectory = ledger_path.read_bytes()
        original_session_map = Path(args.session_map).read_bytes()
        stale_path = stale_paths.get(stale_kind or "")
        if stale_path is not None:
            stale_path.parent.mkdir(parents=True, exist_ok=True)
            stale_path.write_bytes(b"stale")

        relay_calls = 0
        async def fake_run(*_: object, **__: object) -> Any:
            nonlocal relay_calls
            relay_calls += 1
            return _report()

        output = io.StringIO()
        monkeypatch.setattr(runner_mod, "_run_phase_once", fake_run)
        monkeypatch.setattr(sys, "stdout", output)
        with pytest.raises(SystemExit) as raised:
            await runner_mod.cmd_fix_round(args)

        if stale_path is not None:
            assert raised.value.code == 70
            assert relay_calls == 0
            assert stale_path.read_bytes() == b"stale"
            assert ledger_path.read_bytes() == original_trajectory
            assert Path(args.session_map).read_bytes() == original_session_map
            assert not (audit / "fix-report.json").exists()
            assert not (audit / "fix-envelope.json").exists()
            return

        assert raised.value.code == 0, output.getvalue()
        assert relay_calls == 1
        entries = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
        assert len(entries) == 2
        assert entries[1]["sequence"] == 2
        assert entries[1]["action_type"] == "ROUND_RECORDED"
        assert entries[1]["previous_entry_digest"] == entries[0]["entry_digest"]
        assert entries[1]["round_id"] == "round-1"
        assert (audit / "fix-report.json").exists()
        assert (audit / "fix-envelope.json").read_text(encoding="utf-8") == output.getvalue()
        assert len(
            {
                (ref["artifact_id"], ref["path"])
                for entry in entries
                for ref in entry["artifact_refs"]
            }
        ) == 4
        checked = subprocess.run(
            [
                sys.executable,
                str(repo_root / "methodology" / "tools" / "goal_intent_checker.py"),
                "--contract-manifest",
                manifest.relative_to(repo_root).as_posix(),
                "--contract",
                contract.relative_to(repo_root).as_posix(),
                "--ledger",
                ledger_path.relative_to(repo_root).as_posix(),
                "--artifact",
                phase_ledger.relative_to(repo_root).as_posix(),
                "--artifact",
                run_report.relative_to(repo_root).as_posix(),
                "--artifact",
                run_envelope.relative_to(repo_root).as_posix(),
                "--artifact",
                (audit / "fix-report.json").relative_to(repo_root).as_posix(),
                "--artifact",
                (audit / "fix-envelope.json").relative_to(repo_root).as_posix(),
            ],
            cwd=repo_root,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert checked.returncode == 0, checked.stdout + checked.stderr
        assert json.loads(checked.stdout)["status"] == "PASS"


@pytest.mark.asyncio
@pytest.mark.parametrize(("verdict", "exit_code"), [("PASS", 0), ("BLOCKED", 2)])
async def test_fix_round_pass_or_blocked_never_enters_relay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verdict: str, exit_code: int,
) -> None:
    from src import runner as runner_mod

    args = _args(tmp_path)
    _write_report(args, verdict)
    ReapplyLedger.create(args.ledger, phase_id=args.phase_id, max_rounds=3).save()
    calls = 0

    async def fake_run(*_: object, **__: object) -> Any:
        nonlocal calls
        calls += 1
        return _report()

    monkeypatch.setattr(runner_mod, "_run_phase_once", fake_run)
    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_fix_round(args)
    assert raised.value.code == exit_code
    assert calls == 0


@pytest.mark.asyncio
async def test_fix_round_mixed_findings_are_blocked(tmp_path: Path) -> None:
    from src import runner as runner_mod

    args = _args(tmp_path)
    _write_report(args, "CHANGES_REQUESTED", step_status="BLOCKED")
    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_fix_round(args)
    assert raised.value.code == 2


@pytest.mark.asyncio
async def test_fix_round_missing_session_id_is_blocked(tmp_path: Path) -> None:
    from src import runner as runner_mod

    args = _args(tmp_path)
    Path(args.session_map).write_text("{}", encoding="utf-8")
    _write_report(args, "CHANGES_REQUESTED", step_status="CHANGES_REQUESTED")
    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_fix_round(args)
    assert raised.value.code == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fallback", "resumed", "requested_id", "captured_id"),
    [
        (True, True, "thread-123", "thread-123"),
        (False, False, "thread-123", "thread-123"),
        (False, True, "different-thread", "thread-123"),
        (False, True, "thread-123", "different-thread"),
    ],
)
async def test_fix_round_resume_invariant_violation_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fallback: bool,
    resumed: bool,
    requested_id: str,
    captured_id: str,
) -> None:
    from src import runner as runner_mod

    args = _args(tmp_path)
    _write_report(args, "CHANGES_REQUESTED", step_status="CHANGES_REQUESTED")

    async def fake_run(*_: object, **__: object) -> Any:
        return _report(
            fallback=fallback,
            resumed=resumed,
            requested_id=requested_id,
            captured_id=captured_id,
        )

    monkeypatch.setattr(runner_mod, "_run_phase_once", fake_run)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_fix_round(args)
    assert raised.value.code == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prior_verdict", "expected_terminal", "exit_code"),
    [("PASS", "CONVERGED", 0), ("BLOCKED", "ESCALATED_BLOCKED", 2)],
)
async def test_fix_round_early_report_sets_terminal_only_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prior_verdict: str,
    expected_terminal: str, exit_code: int,
) -> None:
    from src import runner as runner_mod

    args = _args(tmp_path)
    _write_report(args, prior_verdict)
    ReapplyLedger.create(args.ledger, phase_id=args.phase_id, max_rounds=3).save()
    monkeypatch.setattr(runner_mod, "_run_phase_once", pytest.fail)
    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_fix_round(args)
    ledger = ReapplyLedger.load(args.ledger)
    assert raised.value.code == exit_code
    assert ledger.terminal_state == expected_terminal
    assert ledger.rounds == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prior_verdict", "terminal", "exit_code"),
    [("PASS", "NO_PROGRESS", 0), ("BLOCKED", "TIMEBOX_EXHAUSTED", 2)],
)
async def test_fix_round_early_report_preserves_existing_terminal_bytes_and_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prior_verdict: str,
    terminal: str, exit_code: int,
) -> None:
    from src import runner as runner_mod

    args = _args(tmp_path)
    _write_report(args, prior_verdict)
    ledger = ReapplyLedger.create(args.ledger, phase_id=args.phase_id, max_rounds=3)
    ledger.terminal_state = terminal
    ledger.save()
    path = Path(args.ledger)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    monkeypatch.setattr(runner_mod, "_run_phase_once", pytest.fail)
    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_fix_round(args)
    assert raised.value.code == exit_code
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_verdict", ["PASS", "BLOCKED"])
async def test_fix_round_phase_mismatch_precedes_early_exit_and_preserves_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prior_verdict: str,
) -> None:
    from src import runner as runner_mod

    args = _args(tmp_path)
    _write_report(args, prior_verdict)
    ReapplyLedger.create(args.ledger, phase_id="other", max_rounds=3).save()
    path = Path(args.ledger)
    before = path.read_bytes()
    monkeypatch.setattr(runner_mod, "_run_phase_once", pytest.fail)
    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_fix_round(args)
    assert raised.value.code == 2
    assert path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operational_exit", "max_rounds", "terminal"),
    [(124, 1, "TIMEBOX_EXHAUSTED"), (70, 1, "TIMEBOX_EXHAUSTED"), (124, 2, None)],
)
async def test_fix_round_operational_exit_consumes_round_and_applies_timebox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operational_exit: int,
    max_rounds: int, terminal: str | None,
) -> None:
    from src import runner as runner_mod

    args = _args(tmp_path)
    args.max_rounds = max_rounds
    _write_report(args, "CHANGES_REQUESTED", step_status="CHANGES_REQUESTED")
    blocked_report = _report()
    blocked_report.status = Verdict.BLOCKED
    blocked_report.exit_code = operational_exit
    blocked_report.as_payload = lambda: {
        "steps": [{"name": "implementer", "status": "BLOCKED", "gating": True,
                   "skipped": False, "resume": blocked_report.steps[0].resume}],
        "summary": {"verdict": "BLOCKED", "exit_code": operational_exit},
    }

    async def fake_run(*_: object, **__: object) -> Any:
        return blocked_report

    monkeypatch.setattr(runner_mod, "_run_phase_once", fake_run)
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_fix_round(args)
    ledger = ReapplyLedger.load(args.ledger)
    output = json.loads(stdout.getvalue())
    assert raised.value.code == operational_exit
    assert output["status"] == "BLOCKED"
    assert ledger.terminal_state == terminal
    assert len(ledger.rounds) == 1
    assert ledger.rounds[0]["exit_code"] == operational_exit


@pytest.mark.asyncio
async def test_fix_round_internal_exception_on_last_round_consumes_and_exhausts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    args = _args(tmp_path)
    args.max_rounds = 1
    _write_report(args, "CHANGES_REQUESTED", step_status="CHANGES_REQUESTED")

    async def fail_after_spawn(*_: object, **__: object) -> Any:
        raise RuntimeError("relay failed")

    monkeypatch.setattr(runner_mod, "_run_phase_once", fail_after_spawn)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_fix_round(args)
    ledger = ReapplyLedger.load(args.ledger)
    assert raised.value.code == 70
    assert ledger.terminal_state == "TIMEBOX_EXHAUSTED"
    assert len(ledger.rounds) == 1
    assert ledger.rounds[0]["exit_code"] == 70
    assert ledger.rounds[0]["result_digest"] is None


@pytest.mark.parametrize(
    ("terminal", "status", "exit_code"),
    [("CONVERGED", "PASS", 0), (None, "CHANGES_REQUESTED", 1),
     ("NO_PROGRESS", "BLOCKED", 2)],
)
def test_reapply_status_maps_ledger_without_saving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, terminal: str | None,
    status: str, exit_code: int,
) -> None:
    from src import runner as runner_mod

    path = tmp_path / "ledger.json"
    ledger = ReapplyLedger.create(path, phase_id="phase", max_rounds=3)
    ledger.terminal_state = terminal
    ledger.save()
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    with pytest.raises(SystemExit) as raised:
        runner_mod.cmd_reapply_status(argparse.Namespace(ledger=str(path)))
    envelope = json.loads(stdout.getvalue())
    payload = json.loads(envelope["stdout"])
    assert raised.value.code == exit_code
    assert envelope["status"] == status
    assert payload["phase_id"] == "phase"
    assert payload["terminal_state"] == terminal
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_reapply_status_corrupt_ledger_is_blocked_70_without_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    path = tmp_path / "ledger.json"
    path.write_text("{broken", encoding="utf-8")
    before = path.read_bytes()
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    with pytest.raises(SystemExit) as raised:
        runner_mod.cmd_reapply_status(argparse.Namespace(ledger=str(path)))
    envelope = json.loads(stdout.getvalue())
    assert raised.value.code == 70
    assert envelope["status"] == "BLOCKED"
    assert path.read_bytes() == before
