"""v2 CLI 관측 기록(--record) 테스트."""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.engine.post_merge_verifier import VerifyResult
from src.engine.session_store import SessionStore
from src.engine.static_review import StaticReviewReport, ToolReviewResult, ToolRun
from src.envelope import Verdict


async def test_cmd_review_record_writes_v2_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    db_path = tmp_path / "sessions.db"
    config_path = _write_config(tmp_path, db_path)
    monkeypatch.setattr(runner_mod, "run_static_review", _fake_pass_report)
    monkeypatch.setattr(runner_mod, "collect_git_diff", _fake_diff)
    monkeypatch.setattr(
        runner_mod,
        "_invoke_optional_ollama_review",
        _fake_ollama,
    )
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)

    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_review(
            argparse.Namespace(
                config=str(config_path),
                changed=False,
                paths=["src/envelope.py"],
                timeout=5.0,
                verbose=False,
                record=True,
            )
        )

    assert raised.value.code == 0
    assert json.loads(stdout.getvalue())["status"] == "PASS"
    session = _only_session(db_path)
    assert session["task"] == "ztr review"
    expected_target = str((Path.cwd() / "src/envelope.py").resolve())
    assert session["target_file"] == expected_target
    assert json.loads(str(session["config_snapshot"]))["targets"] == [
        expected_target
    ]
    assert session["final_verdict"] == "PASS"
    assert session["rounds"] == 1


async def test_cmd_review_record_changed_keeps_repo_relative_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    db_path = tmp_path / "sessions.db"
    config_path = _write_config(tmp_path, db_path)
    repo_root = Path.cwd().resolve().parents[1]
    expected_target = "runtimes/ztr/src/envelope.py"

    async def fake_repo_root(*, cwd: Path) -> Path:
        del cwd
        return repo_root

    async def fake_collect(*, cwd: Path) -> list[str]:
        assert cwd == repo_root
        return [expected_target]

    monkeypatch.setattr(runner_mod, "resolve_repo_root", fake_repo_root)
    monkeypatch.setattr(runner_mod, "collect_changed_paths", fake_collect)
    monkeypatch.setattr(runner_mod, "run_static_review", _fake_pass_report)
    monkeypatch.setattr(runner_mod, "collect_git_diff", _fake_diff)
    monkeypatch.setattr(
        runner_mod,
        "_invoke_optional_ollama_review",
        _fake_ollama,
    )
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)

    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_review(
            argparse.Namespace(
                config=str(config_path),
                changed=True,
                paths=[],
                timeout=5.0,
                verbose=False,
                record=True,
            )
        )

    assert raised.value.code == 0
    session = _only_session(db_path)
    assert session["target_file"] == expected_target
    assert json.loads(str(session["config_snapshot"]))["targets"] == [
        expected_target
    ]


async def test_cmd_verify_record_writes_v2_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    db_path = tmp_path / "sessions.db"
    config_path = _write_config(tmp_path, db_path)

    class FakeVerifier:
        def __init__(self, *, timeout_s: float) -> None:
            del timeout_s

        async def verify(self, target: str) -> VerifyResult:
            del target
            return VerifyResult(passed=True)

    monkeypatch.setattr(runner_mod, "PostMergeVerifier", FakeVerifier)
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)

    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_verify(
            argparse.Namespace(
                config=str(config_path),
                post_merge=True,
                changed=False,
                paths=["src/envelope.py"],
                timeout=5.0,
                record=True,
            )
        )

    assert raised.value.code == 0
    assert json.loads(stdout.getvalue())["status"] == "PASS"
    session = _only_session(db_path)
    assert session["task"] == "ztr verify --post-merge"
    expected_target = str((Path.cwd() / "src/envelope.py").resolve())
    assert session["target_file"] == expected_target
    assert json.loads(str(session["config_snapshot"]))["targets"] == [
        expected_target
    ]
    assert session["final_verdict"] == "PASS"
    assert session["rounds"] == 1


async def test_cmd_run_phase_record_writes_step_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    db_path = tmp_path / "sessions.db"
    config_path = _write_config(tmp_path, db_path)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("record prompt", encoding="utf-8")
    fake_cli = tmp_path / "fake_cli.py"
    fake_cli.write_text(
        "import sys\n"
        "data = sys.stdin.read()\n"
        "print(f'ok:{len(data)}')\n",
        encoding="utf-8",
    )
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)

    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_run_phase(
            argparse.Namespace(
                config=str(config_path),
                prompt_file=str(prompt),
                phase_id="record-test",
                timeout=5.0,
                output_dir=str(tmp_path / "runs"),
                implementer_cmd=json.dumps([sys.executable, str(fake_cli)]),
                reviewer_cmd="",
                mechanical_cmd="",
                record=True,
            )
        )

    assert raised.value.code == 0
    outer = json.loads(stdout.getvalue())
    assert outer["status"] == "PASS"
    assert outer["model"] == "external-cli"
    session = _only_session(db_path)
    assert session["task"] == "ztr run-phase record-test"
    assert session["target_file"] == str(prompt)
    assert session["final_verdict"] == "PASS"
    assert session["rounds"] == 1


async def test_record_start_failure_does_not_change_review_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    bad_db_path = tmp_path / "db-dir"
    bad_db_path.mkdir()
    config_path = _write_config(tmp_path, bad_db_path)
    monkeypatch.setattr(runner_mod, "run_static_review", _fake_pass_report)
    monkeypatch.setattr(runner_mod, "collect_git_diff", _fake_diff)
    monkeypatch.setattr(
        runner_mod,
        "_invoke_optional_ollama_review",
        _fake_ollama,
    )
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_review(
            argparse.Namespace(
                config=str(config_path),
                changed=False,
                paths=["src/envelope.py"],
                timeout=5.0,
                verbose=False,
                record=True,
            )
        )

    assert raised.value.code == 0
    assert json.loads(stdout.getvalue())["status"] == "PASS"
    assert "record warning: start failed" in stderr.getvalue()


def test_fix_round_help_exposes_accept_leg_and_record() -> None:
    import subprocess

    proc = subprocess.run(
        [sys.executable, "-m", "src", "fix-round", "--help"],
        text=True, encoding="utf-8", capture_output=True, check=False,
    )
    assert proc.returncode == 0
    assert "--accept-leg ACCEPT_LEG" in proc.stdout
    assert "--record" in proc.stdout


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verdict", "exit_code", "final_verdict"),
    [("PASS", 0, "PASS"), ("BLOCKED", 2, "BLOCKED")],
)
async def test_fix_round_record_finishes_prior_terminal_with_zero_rounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    verdict: str, exit_code: int, final_verdict: str,
) -> None:
    from src import runner as runner_mod
    from src.engine.reapply_ledger import ReapplyLedger

    db_path = tmp_path / "sessions.db"
    args = _fix_round_args(tmp_path, _write_config(tmp_path, db_path))
    Path(args.report_file).write_text(json.dumps({
        "steps": [], "summary": {"verdict": verdict},
    }), encoding="utf-8")
    ReapplyLedger.create(args.ledger, phase_id=args.phase_id, max_rounds=3).save()
    monkeypatch.setattr(runner_mod, "_run_phase_once", pytest.fail)

    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_fix_round(args)

    assert raised.value.code == exit_code
    session = _only_session(db_path)
    assert session["task"] == f"ztr fix-round {args.phase_id}"
    assert session["target_file"] == str(tmp_path / "prompt.md")
    assert session["final_verdict"] == final_verdict
    assert session["rounds"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [0, 2, 70])
async def test_fix_round_record_defensive_system_exit_finishes_and_reraises_same_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int,
) -> None:
    from src import runner as runner_mod

    db_path = tmp_path / "sessions.db"
    args = _fix_round_args(tmp_path, _write_config(tmp_path, db_path))
    original = SystemExit(code)

    def raise_exit(_: Path) -> dict[str, object]:
        raise original

    monkeypatch.setattr(runner_mod, "load_report_payload", raise_exit)
    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_fix_round(args)

    assert raised.value is original
    session = _only_session(db_path)
    assert session["final_verdict"] == ("PASS" if code == 0 else "BLOCKED")
    assert session["rounds"] == 0


@pytest.mark.asyncio
async def test_fix_round_record_finish_and_close_failures_preserve_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod
    from src.engine.reapply_ledger import ReapplyLedger

    args = _fix_round_args(tmp_path, _write_config(tmp_path, tmp_path / "unused.db"))
    Path(args.report_file).write_text(json.dumps({
        "steps": [], "summary": {"verdict": "PASS"},
    }), encoding="utf-8")
    ReapplyLedger.create(args.ledger, phase_id=args.phase_id, max_rounds=3).save()

    class FailingStore:
        def finish_session(self, *_: object, **__: object) -> None:
            raise RuntimeError("finish boom")

        def close(self) -> None:
            raise RuntimeError("close boom")

    monkeypatch.setattr(
        runner_mod, "_start_recording",
        lambda *_args, **_kwargs: runner_mod.RecordHandle(FailingStore(), 1),
    )
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_fix_round(args)

    assert raised.value.code == 0
    assert "record warning: finish failed" in stderr.getvalue()
    assert "record warning: close failed" in stderr.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "exit_code"),
    [(Verdict.PASS, 0), (Verdict.CHANGES_REQUESTED, 1),
     (Verdict.BLOCKED, 2), (Verdict.BLOCKED, 124)],
)
async def test_fix_round_record_spawned_report_preserves_status_exit_and_one_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    status: Verdict, exit_code: int,
) -> None:
    from src import runner as runner_mod
    from src.engine.fix_feedback import extract_findings
    from src.engine.reapply_ledger import findings_digest

    db_path = tmp_path / "sessions.db"
    args = _fix_round_args(tmp_path, _write_config(tmp_path, db_path))
    input_payload = {"steps": [{
        "name": "mechanical-review", "status": "CHANGES_REQUESTED",
        "gating": True, "skipped": False, "stdout_preview": "finding",
    }], "summary": {"verdict": "CHANGES_REQUESTED"}}
    Path(args.report_file).write_text(json.dumps(input_payload), encoding="utf-8")
    args.approve_findings = findings_digest(extract_findings(input_payload))
    resume = {"resumed": True, "requested_id": "thread-123",
              "captured_session_id": "thread-123"}
    step = SimpleNamespace(name="implementer", resume=resume)
    output_payload = {
        "steps": [{"name": "implementer", "status": status.value, "gating": True,
                   "skipped": False, "resume": resume}],
        "summary": {"verdict": status.value, "exit_code": exit_code},
    }
    fake_report = SimpleNamespace(
        status=status, exit_code=exit_code, steps=[step], resume_fallback_used=False,
        as_payload=lambda: output_payload,
    )

    async def fake_run(*_: object, **__: object) -> object:
        return fake_report

    monkeypatch.setattr(runner_mod, "_run_phase_once", fake_run)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_fix_round(args)

    assert raised.value.code == exit_code
    session = _only_session(db_path)
    assert session["final_verdict"] == status.value
    assert session["rounds"] == 1


@pytest.mark.asyncio
async def test_fix_round_record_spawn_exception_is_blocked_70_with_one_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod
    from src.engine.fix_feedback import extract_findings
    from src.engine.reapply_ledger import findings_digest

    db_path = tmp_path / "sessions.db"
    args = _fix_round_args(tmp_path, _write_config(tmp_path, db_path))
    payload = {"steps": [{
        "name": "mechanical-review", "status": "CHANGES_REQUESTED",
        "gating": True, "skipped": False, "stdout_preview": "finding",
    }], "summary": {"verdict": "CHANGES_REQUESTED"}}
    Path(args.report_file).write_text(json.dumps(payload), encoding="utf-8")
    args.approve_findings = findings_digest(extract_findings(payload))

    async def fail_after_start(*_: object, **__: object) -> object:
        raise RuntimeError("relay failed")

    monkeypatch.setattr(runner_mod, "_run_phase_once", fail_after_start)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_fix_round(args)

    assert raised.value.code == 70
    session = _only_session(db_path)
    assert session["final_verdict"] == "BLOCKED"
    assert session["rounds"] == 1


async def _fake_ollama(*_: object, **__: object) -> tuple[str | None, list[str]]:
    return None, []


async def _fake_diff(*_: object, **__: object) -> str:
    return "diff --git a/src/envelope.py b/src/envelope.py\n"


async def _fake_pass_report(*_: object, **__: object) -> StaticReviewReport:
    return StaticReviewReport(
        tool_results={
            "ruff": ToolReviewResult("ruff", _tool_run()),
            "mypy": ToolReviewResult("mypy", _tool_run()),
        },
        findings=[],
        verdict=Verdict.PASS,
        exit_code=0,
        duration_s=0.01,
    )


def _tool_run(*, exit_code: int = 0) -> ToolRun:
    return ToolRun(
        command=(),
        exit_code=exit_code,
        stdout="",
        stderr_sanitized="",
        duration_s=0.01,
    )


def _write_config(tmp_path: Path, db_path: Path) -> Path:
    path = tmp_path / "agents.config.yaml"
    db_text = str(db_path).replace("\\", "/")
    path.write_text(
        "\n".join([
            "roles:",
            "  implementer-reviewer:",
            "    backend: claude_cli",
            "    model: sonnet",
            "    call_type: headless",
            "  mechanical:",
            "    backend: ollama",
            "    model: qwen2.5-coder:7b",
            "    call_type: local",
            "agents: []",
            "session:",
            f"  db_path: {db_text}",
            "  enable_metrics: true",
            "  enable_session_log: true",
        ]),
        encoding="utf-8",
    )
    return path


def _fix_round_args(tmp_path: Path, config_path: Path) -> argparse.Namespace:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("original", encoding="utf-8")
    session_map = tmp_path / "sessions.json"
    session_map.write_text(json.dumps({"implementer": "thread-123"}), encoding="utf-8")
    return argparse.Namespace(
        config=str(config_path), prompt_file=str(prompt),
        report_file=str(tmp_path / "report.json"), ledger=str(tmp_path / "ledger.json"),
        max_rounds=3, approve_round=1, approve_findings="", accept_leg=None,
        record=True, out="", phase_id="record-fix", timeout=5.0,
        output_dir=str(tmp_path / "runs"), implementer_cmd='["codex", "exec", "-"]',
        reviewer_cmd="", autofix_cmd=None, mechanical_cmd="", test_cmd="",
        session_map=str(session_map), implementer_resume="new", reviewer_resume="new",
        implementer_resume_profile="codex", reviewer_resume_profile="none",
        reviewer_verdict_source="stdout_token",
    )


def _only_session(db_path: Path) -> dict[str, object]:
    store = SessionStore(str(db_path))
    try:
        sessions = store.list_sessions(limit=10)
    finally:
        store.close()
    assert len(sessions) == 1
    return sessions[0]
