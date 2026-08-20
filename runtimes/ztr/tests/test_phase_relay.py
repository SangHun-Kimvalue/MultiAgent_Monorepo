"""Phase 5 run-phase relay 엔진 테스트."""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from src.engine.phase_relay import PhaseRelay, RelayCommand, _scope_violations
from src.engine.static_review import collect_changed_paths
from src.engine.resume_chain import (
    ResumeAttempt,
    ResumeCoordinator,
    ResumeSpec,
    SessionMap,
)
from src.envelope import INTERNAL_ERROR_EXIT_CODE, TIMEOUT_EXIT_CODE, Verdict


@pytest.mark.parametrize(
    ("flag", "trailing"),
    [("--cd", []), ("-C", []), ("--cd", ["--json", "-"])],
)
async def test_malformed_resume_blocks_before_child_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
    trailing: list[str],
) -> None:
    prompt = _prompt_file(tmp_path)
    coordinator = ResumeCoordinator(
        session_map=None,
        specs={
            "implementer": ResumeSpec(
                role="implementer", policy="thread-123", profile="codex"
            )
        },
    )
    calls = 0

    async def forbidden_spawn(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("기형 resume argv에서 child를 spawn하면 안 됩니다")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_spawn)
    relay = PhaseRelay(
        prompt_path=prompt,
        commands=[
            RelayCommand(name="implementer", argv=["codex", "exec", flag, *trailing])
        ],
        output_dir=tmp_path / "runs",
        resume_coordinator=coordinator,
    )

    report = await relay.run()

    assert report.status == Verdict.BLOCKED
    assert calls == 0
    assert report.steps[0].resume is not None
    assert report.steps[0].resume["block_reason"] == f"codex resume argv: {flag} 값 누락"
    assert report.steps[0].timeout_s is None


async def test_resume_leg_passes_transposed_cwd_and_new_leg_omits_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompt = _prompt_file(tmp_path)
    coordinator = ResumeCoordinator(
        session_map=None,
        specs={
            "implementer": ResumeSpec(
                role="implementer", policy="thread-123", profile="codex"
            )
        },
    )
    relay = PhaseRelay(
        prompt_path=prompt,
        commands=[
            RelayCommand(
                name="implementer",
                argv=["codex", "exec", "--cd=D:\\ZRT"],
                timeout_s=12.5,
            )
        ],
        output_dir=tmp_path / "runs",
        resume_coordinator=coordinator,
    )
    resume_command, attempt = relay._prepare_resume(relay._commands[0])
    assert resume_command is not None
    assert resume_command.cwd == "D:\\ZRT"
    assert resume_command.timeout_s == 12.5
    assert attempt is not None

    seen_kwargs: list[dict[str, object]] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self, stdin: bytes) -> tuple[bytes, bytes]:
            return b"", b""

    async def fake_spawn(*args: object, **kwargs: object) -> FakeProcess:
        seen_kwargs.append(kwargs)
        return FakeProcess()

    monkeypatch.setattr(RelayCommand, "resolved_argv", lambda self: self.argv)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    run_dir = tmp_path / "direct-run"
    run_dir.mkdir()
    await relay._run_one(
        index=1, command=resume_command, stdin_text="", run_dir=run_dir
    )
    await relay._run_one(
        index=2,
        command=RelayCommand(name="implementer", argv=["codex", "exec"]),
        stdin_text="",
        run_dir=run_dir,
    )

    assert seen_kwargs[0]["cwd"] == "D:\\ZRT"
    assert "cwd" not in seen_kwargs[1]


def test_resume_fallback_keeps_cwd_none(tmp_path: Path) -> None:
    coordinator = ResumeCoordinator(
        session_map=None,
        specs={
            "implementer": ResumeSpec(
                role="implementer", policy="auto", profile="codex"
            )
        },
    )
    relay = PhaseRelay(
        prompt_path=_prompt_file(tmp_path),
        commands=[
            RelayCommand(
                name="implementer",
                argv=["codex", "exec", "--cd", "D:\\ZRT"],
                timeout_s=12.5,
            )
        ],
        output_dir=tmp_path / "runs",
        resume_coordinator=coordinator,
    )
    failed = ResumeAttempt(
        role="implementer",
        profile="codex",
        policy="auto",
        requested_id="thread-123",
        resumed=True,
        working_dir="D:\\ZRT",
    )

    command, _ = relay._fallback_resume(relay._commands[0], failed_attempt=failed)

    assert command.cwd is None
    assert command.argv == ["codex", "exec", "--json", "--cd", "D:\\ZRT"]
    assert command.timeout_s == 12.5


@pytest.mark.parametrize(
    ("changed", "pattern"),
    [
        ("runtimes/ztr/docs/LESSONS_LEARNED.md", "runtimes/ztr/docs/LESSONS_LEARNED.md"),
        ("methodology/docs/design/deep.md", "methodology/docs"),
        ("docs/archive/HANDOFF_OLD.md", "**/HANDOFF*.md"),
        ("methodology/docs/design.md", "methodology/docs/*.md"),
    ],
)
def test_scope_violations_supports_exact_prefix_and_glob(changed: str, pattern: str) -> None:
    assert _scope_violations([changed], [pattern]) == [{"path": changed, "pattern": pattern}]


def test_scope_violations_rejects_outside_paths_and_handles_nested_names() -> None:
    changed = ["new-dir", "links/doc-link", "../outside.md", "C:/outside.md"]
    assert _scope_violations(changed, ["new-dir", "links/**", "**/*.md"]) == [
        {"path": "new-dir", "pattern": "new-dir"},
        {"path": "links/doc-link", "pattern": "links/**"},
    ]


def test_scope_violations_star_does_not_cross_segment_boundary() -> None:
    assert _scope_violations(
        ["methodology/docs/a/b.md"], ["methodology/*"]
    ) == []


def test_scope_violations_treats_brackets_as_literals() -> None:
    assert _scope_violations(["a[1].md"], ["a[1].md"]) == [
        {"path": "a[1].md", "pattern": "a[1].md"}
    ]


@pytest.mark.parametrize(
    "invalid_pattern",
    [
        r"C:\Users\x\methodology\docs",
        "/methodology/docs",
        r"\methodology\docs",
        r"\\server\share\methodology\docs",
        "",
        "   ",
        "../methodology/docs",
        r"  C:\Users\x\methodology\docs",
        " /methodology/docs",
        "\t../methodology/docs",
    ],
)
def test_phase_relay_rejects_invalid_forbidden_patterns_before_run(
    tmp_path: Path, invalid_pattern: str
) -> None:
    with pytest.raises(ValueError) as exc_info:
        PhaseRelay(
            prompt_path=tmp_path / "prompt.md",
            commands=[RelayCommand(name="implementer", argv=["unused"])],
            output_dir=tmp_path / "runs",
            forbidden_paths=[invalid_pattern],
        )

    assert repr(invalid_pattern) in str(exc_info.value)


def test_phase_relay_reports_all_invalid_forbidden_patterns(tmp_path: Path) -> None:
    invalid_patterns = ["../methodology/docs", r"C:\absolute\docs"]

    with pytest.raises(ValueError) as exc_info:
        PhaseRelay(
            prompt_path=tmp_path / "prompt.md",
            commands=[RelayCommand(name="implementer", argv=["unused"])],
            output_dir=tmp_path / "runs",
            forbidden_paths=invalid_patterns,
        )

    assert all(repr(pattern) in str(exc_info.value) for pattern in invalid_patterns)


@pytest.mark.parametrize(
    ("mode", "expected_exit"),
    [("pass", 2), ("changes", 2), ("blocked", 2), ("internal", 70)],
)
async def test_scope_guard_is_verdict_independent_and_skips_later_legs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, expected_exit: int
) -> None:
    async def fake_collect(**_kwargs: object) -> list[str]:
        return ["methodology/docs/PHASE.md"]

    monkeypatch.setattr("src.engine.phase_relay.collect_changed_paths", fake_collect)
    fake_cli = _fake_cli(tmp_path)
    relay = PhaseRelay(
        prompt_path=_prompt_file(tmp_path),
        commands=[
            RelayCommand(name="implementer", argv=[sys.executable, str(fake_cli), mode]),
            RelayCommand(name="autofix", argv=[sys.executable, str(fake_cli), "pass"], gating=False),
            RelayCommand(name="mechanical-review", argv=[sys.executable, str(fake_cli), "pass"]),
            RelayCommand(name="test", argv=[sys.executable, str(fake_cli), "pass"]),
            RelayCommand(name="implementer-reviewer", argv=[sys.executable, str(fake_cli), "pass"]),
        ],
        output_dir=tmp_path / "runs",
        forbidden_paths=["methodology/docs"],
        cwd=tmp_path,
    )

    report = await relay.run()

    assert report.status == Verdict.BLOCKED
    assert report.exit_code == expected_exit
    assert [step.name for step in report.steps[:2]] == [
        "implementer-scope-guard", "implementer"
    ]
    assert report.steps[0].scope_violations
    assert report.steps[0].timeout_s is None
    assert all(step.skipped for step in report.steps[2:])
    assert all(step.timeout_s is None for step in report.steps[2:])


async def test_scope_guard_disabled_or_allowed_keeps_existing_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_collect(**_kwargs: object) -> list[str]:
        return ["runtimes/ztr/src/allowed.py"]

    monkeypatch.setattr("src.engine.phase_relay.collect_changed_paths", fake_collect)
    fake_cli = _fake_cli(tmp_path)
    commands = [RelayCommand(name="implementer", argv=[sys.executable, str(fake_cli), "pass"])]
    allowed = await PhaseRelay(
        prompt_path=_prompt_file(tmp_path), commands=commands, output_dir=tmp_path / "a",
        forbidden_paths=["methodology/docs"], cwd=tmp_path,
    ).run()
    disabled = await PhaseRelay(
        prompt_path=_prompt_file(tmp_path), commands=commands, output_dir=tmp_path / "b",
        forbidden_paths=None, cwd=tmp_path,
    ).run()
    assert allowed.status == disabled.status == Verdict.PASS
    assert [step.name for step in allowed.steps] == ["implementer"]
    assert [step.name for step in disabled.steps] == ["implementer"]


async def test_scope_guard_valid_patterns_remain_lossless_and_block_exit_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_collect(**_kwargs: object) -> list[str]:
        return ["docs/archive/HANDOFF_OLD.md"]

    monkeypatch.setattr("src.engine.phase_relay.collect_changed_paths", fake_collect)
    fake_cli = _fake_cli(tmp_path)
    patterns = [
        "methodology/docs",
        "**/HANDOFF*.md",
        "runtimes/ztr/docs/LESSONS_LEARNED.md",
    ]
    report = await PhaseRelay(
        prompt_path=_prompt_file(tmp_path),
        commands=[
            RelayCommand(
                name="implementer", argv=[sys.executable, str(fake_cli), "pass"]
            )
        ],
        output_dir=tmp_path / "runs",
        forbidden_paths=patterns,
        cwd=tmp_path,
    ).run()

    assert report.status == Verdict.BLOCKED
    assert report.exit_code == 2
    assert report.steps[0].scope_violations == [
        {"path": "docs/archive/HANDOFF_OLD.md", "pattern": "**/HANDOFF*.md"}
    ]


async def test_scope_guard_collection_failure_preserves_implementer_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reason = "잘린 name-status 출력"

    async def failing_collect(**_kwargs: object) -> list[str]:
        raise RuntimeError(reason)

    monkeypatch.setattr("src.engine.phase_relay.collect_changed_paths", failing_collect)
    fake_cli = _fake_cli(tmp_path)
    report = await PhaseRelay(
        prompt_path=_prompt_file(tmp_path),
        commands=[
            RelayCommand(name="implementer", argv=[sys.executable, str(fake_cli), "pass"]),
            RelayCommand(name="mechanical-review", argv=[sys.executable, str(fake_cli), "pass"]),
        ],
        output_dir=tmp_path / "runs",
        forbidden_paths=["methodology/docs"],
        cwd=tmp_path,
    ).run()

    assert report.status == Verdict.BLOCKED
    assert report.exit_code == INTERNAL_ERROR_EXIT_CODE
    assert [step.name for step in report.steps[:2]] == [
        "implementer-scope-guard", "implementer"
    ]
    guard, implementer = report.steps[:2]
    assert guard.scope_violations is None
    assert reason in guard.stderr_sanitized
    assert implementer.stdout_path is not None
    assert Path(implementer.stdout_path).exists()
    assert report.run_dir.exists()
    assert report.steps[2].skipped is True
    assert guard.timeout_s is None
    assert report.steps[2].timeout_s is None
    assert report.as_payload()["steps"][0].get("scope_violations") is None


async def test_scope_guard_preserves_timeout_exit_124(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_collect(**_kwargs: object) -> list[str]:
        return ["methodology/docs/PHASE.md"]

    monkeypatch.setattr("src.engine.phase_relay.collect_changed_paths", fake_collect)
    fake_cli = _fake_cli(tmp_path)
    report = await PhaseRelay(
        prompt_path=_prompt_file(tmp_path),
        commands=[RelayCommand(name="implementer", argv=[sys.executable, str(fake_cli), "sleep"])],
        output_dir=tmp_path / "timeout-runs",
        timeout_s=0.05,
        forbidden_paths=["methodology/docs"],
        cwd=tmp_path,
    ).run()
    assert report.status == Verdict.BLOCKED
    assert report.exit_code == TIMEOUT_EXIT_CODE
    assert [step.name for step in report.steps] == [
        "implementer-scope-guard", "implementer"
    ]
    assert report.steps[0].scope_violations


@pytest.mark.parametrize("change", ["worktree-delete", "staged-delete", "rename-away"])
async def test_collect_changed_paths_includes_deleted_and_rename_source(
    tmp_path: Path, change: str
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    forbidden = tmp_path / "methodology" / "docs" / "한글-SoT.md"
    forbidden.parent.mkdir(parents=True)
    forbidden.write_text("SoT", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "base"],
        cwd=tmp_path, check=True,
    )
    if change == "worktree-delete":
        forbidden.unlink()
    elif change == "staged-delete":
        forbidden.unlink()
        subprocess.run(["git", "add", "-u"], cwd=tmp_path, check=True)
    else:
        target = tmp_path / "allowed" / "이동됨.md"
        target.parent.mkdir()
        subprocess.run(["git", "mv", str(forbidden), str(target)], cwd=tmp_path, check=True)

    paths = await collect_changed_paths(cwd=tmp_path, include_deleted=True)
    assert "methodology/docs/한글-SoT.md" in paths
    if change == "rename-away":
        assert "allowed/이동됨.md" in paths
    assert _scope_violations(paths, ["methodology/docs"])


async def test_scope_guard_finds_untracked_forbidden_path_outside_relay_cwd(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    relay_cwd = tmp_path / "runtimes" / "ztr"
    relay_cwd.mkdir(parents=True)
    forbidden = tmp_path / "methodology" / "docs" / "__scope_probe.md"
    forbidden.parent.mkdir(parents=True)
    forbidden.write_text("probe", encoding="utf-8")

    paths = await collect_changed_paths(cwd=relay_cwd, include_deleted=True)

    assert "methodology/docs/__scope_probe.md" in paths
    assert _scope_violations(paths, ["methodology/docs"])


async def test_phase_relay_passes_payload_to_next_leg(tmp_path: Path) -> None:
    prompt = _prompt_file(tmp_path)
    fake_cli = _fake_cli(tmp_path)

    relay = PhaseRelay(
        prompt_path=prompt,
        commands=[
            RelayCommand(name="implementer", argv=[sys.executable, str(fake_cli), "pass"]),
            RelayCommand(name="implementer-reviewer", argv=[sys.executable, str(fake_cli), "pass"]),
        ],
        output_dir=tmp_path / "runs",
        phase_id="phase5",
        timeout_s=5.0,
    )

    report = await relay.run()

    assert report.status == Verdict.PASS
    assert report.exit_code == 0
    assert len(report.steps) == 2
    assert report.steps[0].stdout_preview.startswith("mode=pass")
    assert [step.timeout_s for step in report.steps] == [5.0, 5.0]
    reviewer_stdin = Path(str(report.steps[1].stdin_path)).read_text(encoding="utf-8")
    assert '"name": "implementer"' in reviewer_stdin
    assert '"status": "PASS"' in reviewer_stdin


async def test_phase_relay_changes_requested_stops_later_steps(tmp_path: Path) -> None:
    prompt = _prompt_file(tmp_path)
    fake_cli = _fake_cli(tmp_path)

    relay = PhaseRelay(
        prompt_path=prompt,
        commands=[
            RelayCommand(name="implementer", argv=[sys.executable, str(fake_cli), "changes"]),
            RelayCommand(name="implementer-reviewer", argv=[sys.executable, str(fake_cli), "pass"]),
        ],
        output_dir=tmp_path / "runs",
        timeout_s=5.0,
    )

    report = await relay.run()

    assert report.status == Verdict.CHANGES_REQUESTED
    assert report.exit_code == 1
    assert report.steps[0].status == Verdict.CHANGES_REQUESTED
    assert report.steps[0].timeout_s == 5.0
    assert report.steps[1].skipped is True
    assert report.steps[1].timeout_s is None


async def test_phase_relay_blocked_exit_routes_to_blocked(tmp_path: Path) -> None:
    prompt = _prompt_file(tmp_path)
    fake_cli = _fake_cli(tmp_path)

    relay = PhaseRelay(
        prompt_path=prompt,
        commands=[
            RelayCommand(name="implementer", argv=[sys.executable, str(fake_cli), "blocked"]),
        ],
        output_dir=tmp_path / "runs",
        timeout_s=5.0,
    )

    report = await relay.run()

    assert report.status == Verdict.BLOCKED
    assert report.exit_code == 2
    assert report.steps[0].child_exit_code == 2


async def test_phase_relay_timeout_kills_process_and_returns_124(tmp_path: Path) -> None:
    prompt = _prompt_file(tmp_path)
    fake_cli = _fake_cli(tmp_path)

    relay = PhaseRelay(
        prompt_path=prompt,
        commands=[
            RelayCommand(
                name="implementer",
                argv=[sys.executable, str(fake_cli), "sleep"],
                timeout_s=0.2,
            ),
        ],
        output_dir=tmp_path / "runs",
        timeout_s=5.0,
    )

    report = await relay.run()

    assert report.status == Verdict.BLOCKED
    assert report.exit_code == TIMEOUT_EXIT_CODE
    assert report.steps[0].timed_out is True
    assert report.steps[0].exit_code == TIMEOUT_EXIT_CODE
    assert report.steps[0].timeout_s == 0.2


async def test_phase_relay_closes_stdin(tmp_path: Path) -> None:
    prompt = _prompt_file(tmp_path, text="stdin close check")
    fake_cli = _fake_cli(tmp_path)

    relay = PhaseRelay(
        prompt_path=prompt,
        commands=[
            RelayCommand(name="implementer", argv=[sys.executable, str(fake_cli), "stdin"]),
        ],
        output_dir=tmp_path / "runs",
        timeout_s=5.0,
    )

    report = await relay.run()

    assert report.status == Verdict.PASS
    assert "stdin_len=17" in report.steps[0].stdout_preview


async def test_phase_relay_missing_executable_is_internal_blocked(tmp_path: Path) -> None:
    prompt = _prompt_file(tmp_path)

    relay = PhaseRelay(
        prompt_path=prompt,
        commands=[RelayCommand(name="implementer", argv=["definitely-missing-ztr-cli"])],
        output_dir=tmp_path / "runs",
        timeout_s=5.0,
    )

    report = await relay.run()

    assert report.status == Verdict.BLOCKED
    assert report.exit_code == 70
    assert "찾을 수 없습니다" in report.steps[0].stderr_sanitized
    assert report.steps[0].timeout_s is None


async def test_phase_relay_spawn_error_before_child_return_has_null_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    async def fail_spawn(*_args: object, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        raise OSError("spawn failed")

    monkeypatch.setattr(RelayCommand, "resolved_argv", lambda self: self.argv)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)
    report = await PhaseRelay(
        prompt_path=_prompt_file(tmp_path),
        commands=[RelayCommand(name="implementer", argv=["fake"], timeout_s=1.25)],
        output_dir=tmp_path / "runs",
        timeout_s=5.0,
    ).run()

    assert calls == 1
    assert report.status == Verdict.BLOCKED
    assert report.exit_code == INTERNAL_ERROR_EXIT_CODE
    assert report.steps[0].child_exit_code is None
    assert report.steps[0].timeout_s is None


async def test_phase_relay_auto_resume_failure_falls_back_to_new_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = _prompt_file(tmp_path)
    fake_cli = _fake_codex_resume_cli(tmp_path)
    session_map = SessionMap.load(tmp_path / "sessions.json")
    session_map.set("implementer", "expired-thread")
    coordinator = ResumeCoordinator(
        session_map=session_map,
        specs={
            "implementer": ResumeSpec(
                role="implementer",
                policy="auto",
                profile="codex",
            )
        },
    )

    def fake_resolved(command: RelayCommand) -> list[str]:
        return [sys.executable, str(fake_cli), *command.argv[1:]]

    monkeypatch.setattr(RelayCommand, "resolved_argv", fake_resolved)
    relay = PhaseRelay(
        prompt_path=prompt,
        commands=[
            RelayCommand(
                name="implementer",
                argv=["codex", "exec", "prompt"],
                timeout_s=1.25,
            )
        ],
        output_dir=tmp_path / "runs",
        timeout_s=5.0,
        resume_coordinator=coordinator,
    )

    report = await relay.run()

    assert report.status == Verdict.PASS
    assert report.resume_fallback_used is True
    assert "맥락을 잃었을 수 있습니다" in report.resume_warnings[0]
    assert report.steps[0].resume is not None
    assert report.steps[0].resume["fallback_from"] == "expired-thread"
    assert report.steps[0].timeout_s == 1.25
    failed_resume = report.steps[0].resume["failed_resume"]
    assert failed_resume["timeout_s"] == 1.25
    assert SessionMap.load(tmp_path / "sessions.json").get("implementer") == "new-thread"


async def test_phase_relay_explicit_resume_failure_does_not_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = _prompt_file(tmp_path)
    fake_cli = _fake_codex_resume_cli(tmp_path)
    coordinator = ResumeCoordinator(
        session_map=None,
        specs={
            "implementer": ResumeSpec(
                role="implementer",
                policy="expired-thread",
                profile="codex",
            )
        },
    )

    def fake_resolved(command: RelayCommand) -> list[str]:
        return [sys.executable, str(fake_cli), *command.argv[1:]]

    monkeypatch.setattr(RelayCommand, "resolved_argv", fake_resolved)
    relay = PhaseRelay(
        prompt_path=prompt,
        commands=[RelayCommand(name="implementer", argv=["codex", "exec", "prompt"])],
        output_dir=tmp_path / "runs",
        timeout_s=5.0,
        resume_coordinator=coordinator,
    )

    report = await relay.run()

    assert report.status == Verdict.BLOCKED
    assert report.exit_code == 2
    assert report.resume_fallback_used is False
    assert "명시 세션 expired-thread resume 실패" in report.resume_warnings[0]
    assert report.steps[0].resume is not None
    assert report.steps[0].resume["requested_id"] == "expired-thread"
    assert report.steps[0].resume["policy_error"] == report.resume_warnings[0]


async def test_phase_relay_explicit_resume_pass_with_new_session_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = _prompt_file(tmp_path)
    fake_cli = _fake_codex_silent_new_session_cli(tmp_path)
    coordinator = ResumeCoordinator(
        session_map=SessionMap.load(tmp_path / "sessions.json"),
        specs={
            "implementer": ResumeSpec(
                role="implementer",
                policy="expected-thread",
                profile="codex",
            )
        },
    )

    def fake_resolved(command: RelayCommand) -> list[str]:
        return [sys.executable, str(fake_cli), *command.argv[1:]]

    monkeypatch.setattr(RelayCommand, "resolved_argv", fake_resolved)
    relay = PhaseRelay(
        prompt_path=prompt,
        commands=[RelayCommand(name="implementer", argv=["codex", "exec", "prompt"])],
        output_dir=tmp_path / "runs",
        timeout_s=5.0,
        resume_coordinator=coordinator,
    )

    report = await relay.run()

    assert report.status == Verdict.BLOCKED
    assert report.exit_code == 2
    assert report.resume_fallback_used is False
    assert "다른 세션 actual-new-thread" in report.resume_warnings[0]
    assert report.steps[0].resume is not None
    assert report.steps[0].resume["requested_id"] == "expected-thread"
    assert report.steps[0].resume["policy_error"] == report.resume_warnings[0]
    assert SessionMap.load(tmp_path / "sessions.json").get("implementer") is None


def test_relay_command_shell_text_preserves_windows_backslashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    command = RelayCommand.from_text(
        name="implementer",
        value=r'"C:\Program Files\Tool\tool.exe" --flag C:\Temp\input.txt',
    )

    assert command.argv == [
        r"C:\Program Files\Tool\tool.exe",
        "--flag",
        r"C:\Temp\input.txt",
    ]


async def test_reviewer_stdout_token_blocked_overrides_exit_zero(tmp_path: Path) -> None:
    """reviewer가 exit 0이어도 stdout의 ZTR_VERDICT: BLOCKED를 verdict로 반영(핵심 결함 수정)."""
    prompt = _prompt_file(tmp_path)
    fake_cli = _fake_cli(tmp_path)

    relay = PhaseRelay(
        prompt_path=prompt,
        commands=[
            RelayCommand(name="implementer", argv=[sys.executable, str(fake_cli), "pass"]),
            RelayCommand(
                name="implementer-reviewer",
                argv=[sys.executable, str(fake_cli), "verdict_blocked"],
                verdict_source="stdout_token",
            ),
        ],
        output_dir=tmp_path / "runs",
        timeout_s=5.0,
    )

    report = await relay.run()

    assert report.steps[1].child_exit_code == 0  # 프로세스는 성공 종료
    assert report.steps[1].status == Verdict.BLOCKED  # 그러나 토큰으로 BLOCKED
    assert report.status == Verdict.BLOCKED
    assert report.exit_code == 2


async def test_reviewer_stdout_token_missing_fails_closed(tmp_path: Path) -> None:
    """stdout_token leg인데 토큰이 없으면 exit 0이어도 fail-closed BLOCKED."""
    prompt = _prompt_file(tmp_path)
    fake_cli = _fake_cli(tmp_path)

    relay = PhaseRelay(
        prompt_path=prompt,
        commands=[
            RelayCommand(
                name="implementer-reviewer",
                argv=[sys.executable, str(fake_cli), "pass"],  # exit 0, 토큰 없음
                verdict_source="stdout_token",
            ),
        ],
        output_dir=tmp_path / "runs",
        timeout_s=5.0,
    )

    report = await relay.run()

    assert report.steps[0].child_exit_code == 0
    assert report.status == Verdict.BLOCKED
    assert "ZTR_VERDICT" in report.steps[0].stderr_sanitized


async def test_reviewer_stdout_token_pass_is_pass(tmp_path: Path) -> None:
    """stdout_token leg가 ZTR_VERDICT: PASS면 PASS."""
    prompt = _prompt_file(tmp_path)
    fake_cli = _fake_cli(tmp_path)

    relay = PhaseRelay(
        prompt_path=prompt,
        commands=[
            RelayCommand(name="implementer", argv=[sys.executable, str(fake_cli), "pass"]),
            RelayCommand(
                name="implementer-reviewer",
                argv=[sys.executable, str(fake_cli), "verdict_pass"],
                verdict_source="stdout_token",
            ),
        ],
        output_dir=tmp_path / "runs",
        timeout_s=5.0,
    )

    report = await relay.run()

    assert report.status == Verdict.PASS
    assert report.exit_code == 0


async def test_exit_code_source_unchanged_ignores_token(tmp_path: Path) -> None:
    """기본 verdict_source=exit_code leg는 토큰 무시, exit code만 본다(기존 동작 보존)."""
    prompt = _prompt_file(tmp_path)
    fake_cli = _fake_cli(tmp_path)

    relay = PhaseRelay(
        prompt_path=prompt,
        commands=[
            # verdict_blocked 토큰을 찍지만 exit 0 → exit_code source면 PASS여야 한다.
            RelayCommand(name="implementer", argv=[sys.executable, str(fake_cli), "verdict_blocked"]),
        ],
        output_dir=tmp_path / "runs",
        timeout_s=5.0,
    )

    report = await relay.run()

    assert report.status == Verdict.PASS
    assert report.exit_code == 0


async def test_reviewer_stdout_token_multiple_tokens_last_wins(tmp_path: Path) -> None:
    """단독 라인 토큰이 여러 개면 마지막을 채택(CHANGES then PASS → PASS)."""
    prompt = _prompt_file(tmp_path)
    fake_cli = _fake_cli(tmp_path)

    relay = PhaseRelay(
        prompt_path=prompt,
        commands=[
            RelayCommand(name="implementer", argv=[sys.executable, str(fake_cli), "pass"]),
            RelayCommand(
                name="implementer-reviewer",
                argv=[sys.executable, str(fake_cli), "verdict_multi"],
                verdict_source="stdout_token",
            ),
        ],
        output_dir=tmp_path / "runs",
        timeout_s=5.0,
    )

    report = await relay.run()

    assert report.status == Verdict.PASS


async def test_reviewer_stdout_token_inline_mention_not_matched_fails_closed(tmp_path: Path) -> None:
    """산문/예시 속 비-단독 ZTR_VERDICT 멘션은 토큰으로 인정하지 않음 → fail-closed BLOCKED."""
    prompt = _prompt_file(tmp_path)
    fake_cli = _fake_cli(tmp_path)

    relay = PhaseRelay(
        prompt_path=prompt,
        commands=[
            RelayCommand(
                name="implementer-reviewer",
                argv=[sys.executable, str(fake_cli), "verdict_inline"],
                verdict_source="stdout_token",
            ),
        ],
        output_dir=tmp_path / "runs",
        timeout_s=5.0,
    )

    report = await relay.run()

    assert report.status == Verdict.BLOCKED


async def test_reviewer_stdout_token_nonzero_exit_with_pass_token_is_blocked(tmp_path: Path) -> None:
    """PASS 토큰이 있어도 child가 non-zero exit이면 프로세스 실패 우선 → BLOCKED."""
    prompt = _prompt_file(tmp_path)
    fake_cli = _fake_cli(tmp_path)

    relay = PhaseRelay(
        prompt_path=prompt,
        commands=[
            RelayCommand(
                name="implementer-reviewer",
                argv=[sys.executable, str(fake_cli), "verdict_pass_exit1"],
                verdict_source="stdout_token",
            ),
        ],
        output_dir=tmp_path / "runs",
        timeout_s=5.0,
    )

    report = await relay.run()

    assert report.steps[0].child_exit_code == 1
    assert report.status == Verdict.BLOCKED


def test_relay_command_rejects_unknown_verdict_source() -> None:
    """verdict_source 오타는 조용한 exit_code 폴백이 아니라 ValueError로 막는다."""
    with pytest.raises(ValueError):
        RelayCommand(name="x", argv=["a"], verdict_source="stdout-token")


async def test_reviewer_stdout_token_json_output_blocked(tmp_path: Path) -> None:
    """결함 B 회귀: claude -p --output-format json(1줄, 토큰이 result 문자열 안)에서도 verdict 추출."""
    prompt = _prompt_file(tmp_path)
    fake_cli = _fake_cli(tmp_path)

    relay = PhaseRelay(
        prompt_path=prompt,
        commands=[
            RelayCommand(
                name="implementer-reviewer",
                argv=[sys.executable, str(fake_cli), "verdict_json_blocked"],
                verdict_source="stdout_token",
            ),
        ],
        output_dir=tmp_path / "runs",
        timeout_s=5.0,
    )

    report = await relay.run()

    assert report.steps[0].child_exit_code == 0  # claude는 verdict와 무관히 exit 0
    assert report.status == Verdict.BLOCKED  # JSON result 안 토큰을 읽어 게이트


async def test_resume_preserves_verdict_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """결함 A 회귀: resume_coordinator 활성(=session-map) 시에도 reviewer leg의 verdict_source가
    보존돼 exit_code 폴백으로 게이트가 우회되지 않는다."""
    prompt = _prompt_file(tmp_path)
    fake_cli = _fake_cli(tmp_path)
    session_map = SessionMap.load(tmp_path / "sessions.json")
    coordinator = ResumeCoordinator(
        session_map=session_map,
        specs={
            "reviewer": ResumeSpec(role="reviewer", policy="new", profile="claude"),
        },
    )

    def fake_resolved(command: RelayCommand) -> list[str]:
        return [sys.executable, str(fake_cli), *command.argv[1:]]

    monkeypatch.setattr(RelayCommand, "resolved_argv", fake_resolved)
    relay = PhaseRelay(
        prompt_path=prompt,
        commands=[
            RelayCommand(
                name="implementer-reviewer",
                argv=["claude", "verdict_blocked"],
                verdict_source="stdout_token",
            ),
        ],
        output_dir=tmp_path / "runs",
        timeout_s=5.0,
        resume_coordinator=coordinator,
    )

    report = await relay.run()

    # resume 경로(_prepare_resume)가 RelayCommand를 재생성해도 verdict_source가 살아 있어야 한다.
    assert report.steps[0].status == Verdict.BLOCKED
    assert report.status == Verdict.BLOCKED


async def test_phase_relay_non_gating_autofix_does_not_gate(tmp_path: Path) -> None:
    """(ㄴ) non-gating autofix leg: exit 1(changes)이어도 게이트하지 않고 다음 leg를 막지 않는다."""
    prompt = _prompt_file(tmp_path)
    fake_cli = _fake_cli(tmp_path)

    relay = PhaseRelay(
        prompt_path=prompt,
        commands=[
            RelayCommand(name="implementer", argv=[sys.executable, str(fake_cli), "pass"]),
            RelayCommand(
                name="autofix",
                argv=[sys.executable, str(fake_cli), "changes"],
                gating=False,
            ),
            RelayCommand(name="mechanical-review", argv=[sys.executable, str(fake_cli), "pass"]),
        ],
        output_dir=tmp_path / "runs",
        timeout_s=5.0,
    )

    report = await relay.run()

    # autofix가 non-zero exit여도 non-gating이라 전체 PASS, mechanical은 skip 안 되고 실행된다.
    assert report.status == Verdict.PASS
    assert report.exit_code == 0
    assert [step.name for step in report.steps] == ["implementer", "autofix", "mechanical-review"]
    autofix_step = report.steps[1]
    assert autofix_step.gating is False
    assert autofix_step.status == Verdict.CHANGES_REQUESTED  # 기록은 되지만 게이트 안 함
    assert autofix_step.timeout_s == 5.0
    assert autofix_step.as_payload()["gating"] is False
    assert report.steps[2].skipped is False
    assert report.as_payload()["summary"]["failed_step"] is None


async def test_phase_relay_autofix_does_not_alter_next_leg_stdin(tmp_path: Path) -> None:
    """(ㄴ) autofix는 next_input을 바꾸지 않는다 — mechanical은 implementer payload를 본다."""
    prompt = _prompt_file(tmp_path)
    fake_cli = _fake_cli(tmp_path)

    relay = PhaseRelay(
        prompt_path=prompt,
        commands=[
            RelayCommand(name="implementer", argv=[sys.executable, str(fake_cli), "pass"]),
            RelayCommand(
                name="autofix",
                argv=[sys.executable, str(fake_cli), "pass"],
                gating=False,
            ),
            RelayCommand(name="mechanical-review", argv=[sys.executable, str(fake_cli), "pass"]),
        ],
        output_dir=tmp_path / "runs",
        timeout_s=5.0,
    )

    report = await relay.run()

    mech_stdin = Path(str(report.steps[2].stdin_path)).read_text(encoding="utf-8")
    assert '"name": "implementer"' in mech_stdin
    assert '"name": "autofix"' not in mech_stdin


async def test_phase_relay_autofix_exec_error_does_not_block(tmp_path: Path) -> None:
    """(ㄴ) autofix 실행오류(실행 파일 없음)는 봉투에 기록되지만 BLOCKED로 게이트하지 않는다."""
    prompt = _prompt_file(tmp_path)
    fake_cli = _fake_cli(tmp_path)

    relay = PhaseRelay(
        prompt_path=prompt,
        commands=[
            RelayCommand(name="implementer", argv=[sys.executable, str(fake_cli), "pass"]),
            RelayCommand(name="autofix", argv=["definitely-missing-autofix-cli"], gating=False),
            RelayCommand(name="mechanical-review", argv=[sys.executable, str(fake_cli), "pass"]),
        ],
        output_dir=tmp_path / "runs",
        timeout_s=5.0,
    )

    report = await relay.run()

    assert report.status == Verdict.PASS  # 실행오류여도 non-gating
    autofix_step = report.steps[1]
    assert autofix_step.status == Verdict.BLOCKED  # 기록은 됨
    assert "찾을 수 없습니다" in autofix_step.stderr_sanitized
    assert autofix_step.timeout_s is None
    assert report.steps[2].skipped is False  # mechanical은 그대로 실행


@pytest.mark.parametrize("timeout_s", [0.0, -1.0, float("nan"), float("inf")])
def test_timeout_values_must_be_positive_finite_before_spawn(
    tmp_path: Path, timeout_s: float
) -> None:
    with pytest.raises(ValueError, match="양의 유한수"):
        RelayCommand(name="implementer", argv=["unused"], timeout_s=timeout_s)

    command = RelayCommand(name="implementer", argv=["unused"])
    with pytest.raises(ValueError, match="양의 유한수"):
        PhaseRelay(
            prompt_path=_prompt_file(tmp_path),
            commands=[command],
            output_dir=tmp_path / "runs",
            timeout_s=timeout_s,
        )


def _prompt_file(tmp_path: Path, *, text: str = "구현 프롬프트") -> Path:
    path = tmp_path / "prompt.md"
    path.write_text(text, encoding="utf-8")
    return path


def _fake_cli(tmp_path: Path) -> Path:
    path = tmp_path / "fake_cli.py"
    path.write_text(
        "\n".join([
            "from __future__ import annotations",
            "import json",
            "import sys",
            "import time",
            "mode = sys.argv[1]",
            "data = sys.stdin.read()",
            "if mode == 'sleep':",
            "    time.sleep(2)",
            "elif mode == 'stdin':",
            "    print(f'stdin_len={len(data)}')",
            "else:",
            "    print(f'mode={mode}')",
            "    print(data[:80])",
            # verdict_* 모드: verdict 토큰을 stdout에 찍되 exit 0으로 끝낸다.
            # (claude -p가 review verdict와 무관하게 exit 0을 반환하는 상황 재현)
            "if mode == 'verdict_blocked':",
            "    print('ZTR_VERDICT: BLOCKED')",
            "if mode == 'verdict_changes':",
            "    print('ZTR_VERDICT: CHANGES_REQUESTED')",
            "if mode == 'verdict_pass':",
            "    print('ZTR_VERDICT: PASS')",
            # 다중 단독토큰: 마지막(PASS) 채택 검증용
            "if mode == 'verdict_multi':",
            "    print('ZTR_VERDICT: CHANGES_REQUESTED')",
            "    print('ZTR_VERDICT: PASS')",
            # 산문/예시 멘션(단독 라인 아님): 매칭되면 안 됨 → fail-closed 검증용
            "if mode == 'verdict_inline':",
            "    print('참고로 ZTR_VERDICT: PASS 는 예시일 뿐 단독 라인이 아니다')",
            # PASS 토큰 + non-zero exit: 프로세스 실패가 우선 → BLOCKED 검증용
            "if mode == 'verdict_pass_exit1':",
            "    print('ZTR_VERDICT: PASS')",
            "    raise SystemExit(1)",
            # claude -p --output-format json 재현: JSON 한 줄, 토큰은 result 문자열 안 escape 개행 뒤.
            "if mode == 'verdict_json_blocked':",
            "    print(json.dumps({'type': 'result', 'is_error': False,",
            "                      'result': 'review findings...\\nZTR_VERDICT: BLOCKED'}))",
            "if mode == 'changes':",
            "    raise SystemExit(1)",
            "if mode == 'blocked':",
            "    raise SystemExit(2)",
            "if mode == 'internal':",
            "    raise SystemExit(70)",
        ]),
        encoding="utf-8",
    )
    return path


def _fake_codex_resume_cli(tmp_path: Path) -> Path:
    path = tmp_path / "fake_codex_cli.py"
    path.write_text(
        "\n".join([
            "from __future__ import annotations",
            "import json",
            "import sys",
            "if 'resume' in sys.argv:",
            "    raise SystemExit(1)",
            "print(json.dumps({'type': 'thread.started', 'thread_id': 'new-thread'}))",
        ]),
        encoding="utf-8",
    )
    return path


def _fake_codex_silent_new_session_cli(tmp_path: Path) -> Path:
    path = tmp_path / "fake_codex_silent_new.py"
    path.write_text(
        "\n".join([
            "from __future__ import annotations",
            "import json",
            "print(json.dumps({'type': 'thread.started', 'thread_id': 'actual-new-thread'}))",
        ]),
        encoding="utf-8",
    )
    return path
