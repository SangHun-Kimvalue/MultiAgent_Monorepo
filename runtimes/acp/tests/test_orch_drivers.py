"""Phase 3 Claude CLI probe driver — deterministic fake-runner tests.

live Claude 호출 없이 injected fake runner/which로 driver 계약을 결정론 검증한다.
모든 실패 경로가 예외 누수 없이 BLOCKED로 닫히는지, argv가 resolved full path를 쓰는지,
approve 없이는 DONE으로 넘어가지 않는지(no-auto-advance)를 확인한다.
"""
from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from acp.orch_drivers import (
    ClaudeCliProbeDriver,
    CodexCliProbeDriver,
    ToolRun,
    build_driver,
    parse_claude_session_id,
    parse_codex_thread_id,
    run_subprocess_tool,
)
from acp.orch_events import OrchEventType
from acp.orch_runs import (
    MockGateDriver,
    OrchRunManager,
    OrchRunStartRequest,
    SegmentStatus,
)
from acp.store import SessionStore
from acp.web.app import EventBroadcaster

LAUNCHER = r"C:\fake\bin\claude.exe"
LAUNCHER_CMD = r"C:\fake\bin\claude.cmd"
LAUNCHER_PS1 = r"C:\fake\bin\claude.ps1"


class FakeRunner:
    """주입된 ToolRun 결과를 순서대로 반환하며 호출 인자를 기록하는 fake 실행 경계."""

    def __init__(self, results: Sequence[ToolRun]) -> None:
        self._results = list(results)
        self.calls: list[dict[str, object]] = []

    async def __call__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        timeout_s: float,
        stdin_text: str | None = None,
    ) -> ToolRun:
        self.calls.append(
            {
                "command": tuple(command),
                "cwd": cwd,
                "timeout_s": timeout_s,
                "stdin_text": stdin_text,
            }
        )
        if not self._results:
            raise AssertionError("FakeRunner: 예상보다 많은 spawn 호출")
        return self._results.pop(0)


def _ok(stdout: str) -> ToolRun:
    return ToolRun(exit_code=0, stdout=stdout)


def _run(driver: ClaudeCliProbeDriver, **kwargs: object):
    base = {
        "prompt": "drive phase one",
        "project_id": "T2",
        "phase_id": "P1",
        "run_id": "orch-run-test",
        "resume_token": None,
    }
    base.update(kwargs)
    return asyncio.run(driver.run_segment(**base))  # type: ignore[arg-type]


# ── parse_claude_session_id 단위 ──


def test_parse_session_id_success():
    assert parse_claude_session_id('{"session_id": "sess-abc"}') == "sess-abc"


def test_parse_session_id_invalid_json_raises():
    with pytest.raises(ValueError, match="invalid claude json"):
        parse_claude_session_id("not json at all")


def test_parse_session_id_missing_raises():
    with pytest.raises(ValueError, match="missing claude session_id"):
        parse_claude_session_id('{"foo": "bar"}')


def test_parse_session_id_blank_raises():
    with pytest.raises(ValueError, match="missing claude session_id"):
        parse_claude_session_id('{"session_id": "   "}')


# ── 첫 segment ──


def test_first_segment_success_awaits_gate():
    runner = FakeRunner([_ok('{"session_id": "sess-abc"}')])
    driver = ClaudeCliProbeDriver(launcher=LAUNCHER, runner=runner)

    result = _run(driver)

    assert result.status == SegmentStatus.AWAITING_GATE
    assert result.resume_token == "sess-abc"
    types = [event.type for event in result.events]
    assert types == [OrchEventType.PHASE_STARTED, OrchEventType.GATE_WAITING]
    gate = result.events[1]
    assert gate.payload["approval_required"] is True
    assert gate.payload["resume_token"] == "sess-abc"
    assert gate.payload["driver"] == "claude-cli"
    started = result.events[0]
    assert started.payload["status"] == "running"
    assert started.payload["driver"] == "claude-cli"


def test_first_segment_argv_uses_resolved_full_path():
    runner = FakeRunner([_ok('{"session_id": "sess-abc"}')])
    driver = ClaudeCliProbeDriver(launcher=LAUNCHER, runner=runner)

    _run(driver)

    command = runner.calls[0]["command"]
    assert command[0] == LAUNCHER
    assert command[0] != "claude"
    assert "--output-format" in command
    assert "json" in command
    assert "-p" in command


def test_first_segment_prompt_goes_to_stdin_not_argv():
    # stdin transport: prompt 본문은 argv 원소에 없고 stdin_text에만 있어야 한다.
    runner = FakeRunner([_ok('{"session_id": "sess-abc"}')])
    driver = ClaudeCliProbeDriver(launcher=LAUNCHER, runner=runner)

    _run(driver, prompt="drive phase one")

    call = runner.calls[0]
    assert "drive phase one" not in call["command"]
    assert call["stdin_text"] == "drive phase one"


def test_first_segment_timeout_blocked_and_cleanup_path():
    # timed_out ToolRun은 run_subprocess_tool이 kill/cleanup을 마친 뒤 반환하는 형태.
    runner = FakeRunner(
        [ToolRun(exit_code=None, timed_out=True, error="timeout")]
    )
    driver = ClaudeCliProbeDriver(launcher=LAUNCHER, runner=runner)

    result = _run(driver)

    assert result.status == SegmentStatus.BLOCKED
    assert result.events == ()
    assert result.message == "claude cli timed out"
    assert len(runner.calls) == 1  # spawn은 시도됐고 cleanup 경로를 통과했다.


def test_first_segment_nonzero_exit_blocked():
    runner = FakeRunner([ToolRun(exit_code=2, stdout="")])
    driver = ClaudeCliProbeDriver(launcher=LAUNCHER, runner=runner)

    result = _run(driver)

    assert result.status == SegmentStatus.BLOCKED
    assert result.message == "claude cli exited 2"


def test_first_segment_invalid_json_blocked():
    runner = FakeRunner([_ok("definitely not json")])
    driver = ClaudeCliProbeDriver(launcher=LAUNCHER, runner=runner)

    result = _run(driver)

    assert result.status == SegmentStatus.BLOCKED
    assert result.message == "invalid claude json"


def test_first_segment_missing_session_id_blocked():
    runner = FakeRunner([_ok('{"result": "ok"}')])
    driver = ClaudeCliProbeDriver(launcher=LAUNCHER, runner=runner)

    result = _run(driver)

    assert result.status == SegmentStatus.BLOCKED
    assert result.message == "missing claude session_id"


def test_first_segment_oserror_blocked_no_leak():
    runner = FakeRunner(
        [ToolRun(exit_code=None, error="[WinError 206] 파일 이름이 너무 깁니다")]
    )
    driver = ClaudeCliProbeDriver(launcher=LAUNCHER, runner=runner)

    result = _run(driver)

    assert result.status == SegmentStatus.BLOCKED
    assert result.message.startswith("claude cli spawn failed:")


def test_launcher_unresolved_blocked_without_spawn():
    runner = FakeRunner([])  # 호출되면 AssertionError → spawn 시도 자체가 실패의 증거
    driver = ClaudeCliProbeDriver(which=lambda _name: None, runner=runner)

    result = _run(driver)

    assert result.status == SegmentStatus.BLOCKED
    assert result.message == "claude launcher not found"
    assert runner.calls == []


def test_short_launcher_name_resolved_to_full_path_not_left_in_argv():
    # P3(codex 교차리뷰): 명시 launcher가 짧은 "claude"여도 argv 첫 토큰에 그대로 남으면
    # Windows에서 [WinError 2]. which로 resolve된 full path가 argv에 들어가야 한다.
    runner = FakeRunner([_ok('{"session_id": "sess-abc"}')])
    driver = ClaudeCliProbeDriver(
        launcher="claude",
        which=lambda name: LAUNCHER if name == "claude" else None,
        runner=runner,
    )

    result = _run(driver)

    assert result.status == SegmentStatus.AWAITING_GATE
    command = runner.calls[0]["command"]
    assert command[0] == LAUNCHER
    assert command[0] != "claude"


def test_cmd_launcher_uses_cmd_call_wrapper():
    runner = FakeRunner([_ok('{"session_id": "sess-abc"}')])
    driver = ClaudeCliProbeDriver(launcher=LAUNCHER_CMD, runner=runner)

    result = _run(driver)

    assert result.status == SegmentStatus.AWAITING_GATE
    call = runner.calls[0]
    command = call["command"]
    assert command[0].lower().endswith("cmd.exe")
    assert command[1:4] == ("/d", "/c", "call")
    assert command[4] == LAUNCHER_CMD
    # prompt 본문은 argv에 없고, wrapper 뒤에는 짧은 옵션만 남는다.
    assert command[5:] == ("-p", "--output-format", "json")
    assert "drive phase one" not in command
    assert call["stdin_text"] == "drive phase one"


def test_ps1_launcher_uses_powershell_file_wrapper():
    runner = FakeRunner([_ok('{"session_id": "sess-abc"}')])
    driver = ClaudeCliProbeDriver(launcher=LAUNCHER_PS1, runner=runner)

    result = _run(driver)

    assert result.status == SegmentStatus.AWAITING_GATE
    command = runner.calls[0]["command"]
    assert command[:5] == (
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
    )
    assert command[5] == LAUNCHER_PS1


def test_short_launcher_name_unresolved_blocked():
    # 짧은 이름이 which로도 resolve되지 않으면 argv에 남기지 않고 BLOCKED로 닫는다.
    runner = FakeRunner([])
    driver = ClaudeCliProbeDriver(
        launcher="claude",
        which=lambda _name: None,
        runner=runner,
    )

    result = _run(driver)

    assert result.status == SegmentStatus.BLOCKED
    assert result.message == "claude launcher not found"
    assert runner.calls == []


def test_prompt_too_long_blocked_without_spawn():
    runner = FakeRunner([])
    driver = ClaudeCliProbeDriver(
        launcher=LAUNCHER, runner=runner, max_prompt_chars=10
    )

    result = _run(driver, prompt="x" * 50)

    assert result.status == SegmentStatus.BLOCKED
    assert result.message == "claude cli prompt too long (stdin probe sanity cap)"
    assert runner.calls == []


# ── approve segment ──


def test_approve_segment_success_done():
    runner = FakeRunner([_ok('{"session_id": "sess-abc"}')])
    driver = ClaudeCliProbeDriver(launcher=LAUNCHER, runner=runner)

    result = _run(driver, resume_token="sess-abc")

    assert result.status == SegmentStatus.DONE
    assert result.resume_token is None
    verdict = result.events[0]
    assert verdict.type == OrchEventType.PHASE_VERDICT
    assert verdict.payload["approved"] is True
    assert verdict.payload["status"] == "done"
    assert verdict.payload["driver"] == "claude-cli"
    assert verdict.payload["session_id"] == "sess-abc"
    assert verdict.payload["requested_session_id"] == "sess-abc"


def test_approve_segment_argv_has_resume_token():
    runner = FakeRunner([_ok('{"session_id": "sess-abc"}')])
    driver = ClaudeCliProbeDriver(launcher=LAUNCHER, runner=runner)

    _run(driver, resume_token="sess-abc")

    command = runner.calls[0]["command"]
    assert command[0] == LAUNCHER
    assert "--resume" in command
    assert "sess-abc" in command


def test_approve_segment_approval_prompt_goes_to_stdin_not_argv():
    # approve: approval prompt 본문은 argv에 없고 stdin_text에만, argv에는 --resume +
    # --output-format json 같은 짧은 옵션만.
    runner = FakeRunner([_ok('{"session_id": "sess-abc"}')])
    driver = ClaudeCliProbeDriver(launcher=LAUNCHER, runner=runner)

    _run(driver, resume_token="sess-abc")

    call = runner.calls[0]
    assert ClaudeCliProbeDriver.APPROVAL_PROMPT not in call["command"]
    assert call["stdin_text"] == ClaudeCliProbeDriver.APPROVAL_PROMPT
    assert call["command"] == (
        LAUNCHER,
        "-p",
        "--resume",
        "sess-abc",
        "--output-format",
        "json",
    )


def test_approve_segment_id_mismatch_still_done_but_observed():
    # resume가 다른 id를 반환해도 success 조건(exit0+session_id)을 만족하면 DONE.
    # 동일성 미입증 — requested/returned id를 payload에 관측만 남긴다.
    runner = FakeRunner([_ok('{"session_id": "sess-returned-different"}')])
    driver = ClaudeCliProbeDriver(launcher=LAUNCHER, runner=runner)

    result = _run(driver, resume_token="sess-requested")

    assert result.status == SegmentStatus.DONE
    verdict = result.events[0]
    assert verdict.payload["session_id"] == "sess-returned-different"
    assert verdict.payload["requested_session_id"] == "sess-requested"


def test_approve_segment_timeout_blocked():
    runner = FakeRunner([ToolRun(exit_code=None, timed_out=True, error="timeout")])
    driver = ClaudeCliProbeDriver(launcher=LAUNCHER, runner=runner)

    result = _run(driver, resume_token="sess-abc")

    assert result.status == SegmentStatus.BLOCKED
    assert result.message == "claude cli timed out"


# ── manager 통합: no-auto-advance + default driver ──


def _store(tmp_path) -> SessionStore:
    return SessionStore(str(tmp_path / "t.db"), str(tmp_path / "e.jsonl"))


def test_claude_driver_no_auto_advance_until_approve(tmp_path):
    store = _store(tmp_path)
    runner = FakeRunner(
        [
            _ok('{"session_id": "sess-1"}'),  # first segment
            _ok('{"session_id": "sess-1"}'),  # approve segment
        ]
    )
    driver = ClaudeCliProbeDriver(launcher=LAUNCHER, runner=runner)
    manager = OrchRunManager(
        store=store, broadcaster=EventBroadcaster(), driver=driver
    )

    async def scenario():
        start = await manager.start_run(
            OrchRunStartRequest(prompt="go", phase_id="P1")
        )
        # 첫 segment 후 approve 없이는 resume(2번째 spawn)이 일어나지 않는다(AD-7).
        assert start.status == SegmentStatus.AWAITING_GATE
        assert len(runner.calls) == 1
        done = await manager.approve_run(start.run_id)
        return start, done

    start, done = asyncio.run(scenario())

    assert done.status == SegmentStatus.DONE
    assert len(runner.calls) == 2
    verdicts = [
        event
        for event in store.list_orch_events(phase_id="P1")
        if event["type"] == OrchEventType.PHASE_VERDICT.value
    ]
    assert len(verdicts) == 1
    store.close()


def test_blocked_first_segment_does_not_leave_active_run(tmp_path):
    # BLOCKED는 terminal — 다음 run을 막지 않고(409 아님) 새 run을 허용해야 한다.
    store = _store(tmp_path)
    runner = FakeRunner([ToolRun(exit_code=7, stdout="")])
    driver = ClaudeCliProbeDriver(launcher=LAUNCHER, runner=runner)
    manager = OrchRunManager(
        store=store, broadcaster=EventBroadcaster(), driver=driver
    )

    async def scenario():
        first = await manager.start_run(OrchRunStartRequest(prompt="go", phase_id="P1"))
        assert first.status == SegmentStatus.BLOCKED
        # 추가 spawn 결과를 주입해 두 번째 run이 막히지 않음을 확인.
        runner._results.append(ToolRun(exit_code=8, stdout=""))
        second = await manager.start_run(OrchRunStartRequest(prompt="go2", phase_id="P2"))
        return first, second

    first, second = asyncio.run(scenario())
    assert second.status == SegmentStatus.BLOCKED
    assert second.run_id != first.run_id
    store.close()


# ── build_driver ──


def test_build_driver_default_is_mock():
    assert isinstance(build_driver(), MockGateDriver)
    assert isinstance(build_driver("mock"), MockGateDriver)
    assert isinstance(build_driver("MOCK"), MockGateDriver)


def test_build_driver_claude_cli_returns_probe_driver():
    driver = build_driver("claude-cli", launcher=LAUNCHER)
    assert isinstance(driver, ClaudeCliProbeDriver)
    assert driver.launcher == LAUNCHER


def test_build_driver_unknown_kind_raises():
    with pytest.raises(ValueError, match="unknown orch driver kind"):
        build_driver("gpt-cli")


def test_default_manager_uses_mock_gate_driver(tmp_path):
    # default app path: run_manager 미주입 시 OrchRunManager는 MockGateDriver를 쓴다.
    store = _store(tmp_path)
    manager = OrchRunManager(store=store, broadcaster=EventBroadcaster())
    assert isinstance(manager._driver, MockGateDriver)
    store.close()


# ── run_subprocess_tool 실 OSError 경로(launcher 미존재) ──


def test_run_subprocess_tool_oserror_maps_to_error(tmp_path):
    async def scenario():
        return await run_subprocess_tool(
            (r"C:\definitely\nonexistent\claude_xyz.exe", "-p", "x"),
            cwd=tmp_path,
            timeout_s=5.0,
        )

    run = asyncio.run(scenario())
    assert run.error != ""
    assert run.exit_code is None
    assert run.timed_out is False


# ── run_subprocess_tool stdin transport(hermetic child Python) ──

# 자식 Python이 stdin bytes를 stdout으로 그대로 되돌린다. text wrapper(cp949 등)를 타면
# 비ASCII/메타문자가 훼손되므로 양쪽 다 `.buffer`(bytes 경계)만 쓴다.
_ECHO_STDIN = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"
_NO_STDIN = "import sys; sys.stdout.buffer.write(b'no-stdin-ok')"


def test_run_subprocess_tool_stdin_roundtrip_utf8(tmp_path):
    # 한글 + shell metacharacter payload가 stdin→stdout으로 손실 없이 왕복해야 한다.
    payload = "한글 & | ^ % \" 메타문자"

    async def scenario():
        return await run_subprocess_tool(
            (sys.executable, "-c", _ECHO_STDIN),
            cwd=tmp_path,
            timeout_s=30.0,
            stdin_text=payload,
        )

    run = asyncio.run(scenario())
    assert run.exit_code == 0
    assert run.error == ""
    assert run.timed_out is False
    assert run.stdout == payload


def test_run_subprocess_tool_stdin_none_preserves_legacy_path(tmp_path):
    # stdin_text 미지정이면 stdin PIPE를 열지 않고도 자식이 정상 종료해야 한다(기존 동작).
    async def scenario():
        return await run_subprocess_tool(
            (sys.executable, "-c", _NO_STDIN),
            cwd=tmp_path,
            timeout_s=30.0,
        )

    run = asyncio.run(scenario())
    assert run.exit_code == 0
    assert run.error == ""
    assert run.stdout == "no-stdin-ok"


# ══════════════════════════════════════════════════════════════════════════
# Phase 5 — Codex CLI probe driver (deterministic fake-runner tests)
# ══════════════════════════════════════════════════════════════════════════

CODEX_LAUNCHER = r"C:\fake\bin\codex.exe"
CODEX_LAUNCHER_CMD = r"C:\fake\bin\codex.cmd"

# 실측된 Codex stdout JSONL 모양(설계 §3): 첫 줄 thread.started, 이후 item.* 이벤트.
_CODEX_FIRST = (
    '{"type": "thread.started", "thread_id": "thr-abc"}\n'
    '{"type": "item.completed", "text": "ok"}\n'
)


def _run_codex(driver: CodexCliProbeDriver, **kwargs: object):
    base = {
        "prompt": "drive phase one",
        "project_id": "T2",
        "phase_id": "P1",
        "run_id": "orch-run-codex",
        "resume_token": None,
    }
    base.update(kwargs)
    return asyncio.run(driver.run_segment(**base))  # type: ignore[arg-type]


# ── parse_codex_thread_id 단위 ──


def test_parse_codex_thread_id_success():
    assert parse_codex_thread_id(_CODEX_FIRST) == "thr-abc"


def test_parse_codex_thread_id_single_line_success():
    assert (
        parse_codex_thread_id('{"type": "thread.started", "thread_id": "thr-x"}')
        == "thr-x"
    )


def test_parse_codex_thread_id_skips_nonjson_prefix_and_suffix():
    # banner/warning(비JSON)과 JSON non-object(배열)는 건너뛰고 thread.started 1개면 통과한다.
    stdout = (
        "codex 0.140.0 starting up\n"
        "[1, 2, 3]\n"
        '{"type": "thread.started", "thread_id": "thr-mid"}\n'
        "WARN: some plugin banner\n"
        '{"type": "item.completed", "text": "done"}\n'
    )
    assert parse_codex_thread_id(stdout) == "thr-mid"


def test_parse_codex_thread_id_no_valid_object_raises():
    # 유효 JSON object가 0개(비JSON banner만) → invalid codex jsonl.
    with pytest.raises(ValueError, match="invalid codex jsonl"):
        parse_codex_thread_id("not json\nstill not json\n")


def test_parse_codex_thread_id_empty_stdout_raises():
    with pytest.raises(ValueError, match="invalid codex jsonl"):
        parse_codex_thread_id("")


def test_parse_codex_thread_id_missing_thread_id_raises():
    # 유효 object는 있으나 thread.started/thread_id가 없음 → missing codex thread_id.
    with pytest.raises(ValueError, match="missing codex thread_id"):
        parse_codex_thread_id('{"type": "item.completed", "text": "ok"}')


def test_parse_codex_thread_id_blank_thread_id_raises():
    with pytest.raises(ValueError, match="missing codex thread_id"):
        parse_codex_thread_id('{"type": "thread.started", "thread_id": "   "}')


def test_parse_codex_thread_id_non_string_thread_id_raises():
    with pytest.raises(ValueError, match="missing codex thread_id"):
        parse_codex_thread_id('{"type": "thread.started", "thread_id": 123}')


def test_parse_codex_thread_id_multiple_thread_started_raises():
    # 현재 실측은 단수 thread.started. 복수 출현은 의도된 fail-closed(invalid codex jsonl).
    stdout = (
        '{"type": "thread.started", "thread_id": "thr-1"}\n'
        '{"type": "thread.started", "thread_id": "thr-2"}\n'
    )
    with pytest.raises(ValueError, match="invalid codex jsonl"):
        parse_codex_thread_id(stdout)


# ── 첫 segment ──


def test_codex_first_segment_success_awaits_gate():
    runner = FakeRunner([_ok(_CODEX_FIRST)])
    driver = CodexCliProbeDriver(launcher=CODEX_LAUNCHER, runner=runner)

    result = _run_codex(driver)

    assert result.status == SegmentStatus.AWAITING_GATE
    assert result.resume_token == "thr-abc"
    types = [event.type for event in result.events]
    assert types == [OrchEventType.PHASE_STARTED, OrchEventType.GATE_WAITING]
    gate = result.events[1]
    assert gate.payload["approval_required"] is True
    assert gate.payload["resume_token"] == "thr-abc"
    assert gate.payload["driver"] == "codex-cli"
    started = result.events[0]
    assert started.payload["status"] == "running"
    assert started.payload["driver"] == "codex-cli"


def test_codex_first_segment_argv_shape():
    runner = FakeRunner([_ok(_CODEX_FIRST)])
    driver = CodexCliProbeDriver(
        launcher=CODEX_LAUNCHER, runner=runner, cwd=r"C:\tmp\safe"
    )

    _run_codex(driver)

    command = runner.calls[0]["command"]
    assert command[0] == CODEX_LAUNCHER
    assert command[0] != "codex"
    # 첫 argv 양성 assert: exec + 실측 플래그 전부 + -C <cwd> + stdin sentinel(-).
    assert "exec" in command
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert "--skip-git-repo-check" in command
    assert "--json" in command
    # --sandbox read-only는 인접 토큰쌍으로 들어간다.
    assert "--sandbox" in command
    sandbox_idx = command.index("--sandbox")
    assert command[sandbox_idx + 1] == "read-only"
    assert "-C" in command
    cwd_idx = command.index("-C")
    assert command[cwd_idx + 1] == r"C:\tmp\safe"
    # stdin sentinel `-`가 argv 마지막에 있어야 한다(prompt는 stdin으로).
    assert command[-1] == "-"


def test_codex_first_segment_prompt_goes_to_stdin_not_argv():
    runner = FakeRunner([_ok(_CODEX_FIRST)])
    driver = CodexCliProbeDriver(launcher=CODEX_LAUNCHER, runner=runner)

    _run_codex(driver, prompt="drive phase one")

    call = runner.calls[0]
    assert "drive phase one" not in call["command"]
    assert call["stdin_text"] == "drive phase one"


def test_codex_first_segment_korean_prompt_roundtrips_via_stdin():
    # 한국어 + shell metacharacter prompt가 argv가 아니라 stdin_text에 그대로 보존돼야 한다.
    payload = "페이즈 하나 구동 & | ^ % \" 메타문자"
    runner = FakeRunner([_ok(_CODEX_FIRST)])
    driver = CodexCliProbeDriver(launcher=CODEX_LAUNCHER, runner=runner)

    _run_codex(driver, prompt=payload)

    call = runner.calls[0]
    assert payload not in call["command"]
    assert call["stdin_text"] == payload


def test_codex_first_segment_timeout_blocked():
    runner = FakeRunner([ToolRun(exit_code=None, timed_out=True, error="timeout")])
    driver = CodexCliProbeDriver(launcher=CODEX_LAUNCHER, runner=runner)

    result = _run_codex(driver)

    assert result.status == SegmentStatus.BLOCKED
    assert result.events == ()
    assert result.message == "codex cli timed out"
    assert len(runner.calls) == 1


def test_codex_first_segment_nonzero_exit_blocked():
    runner = FakeRunner([ToolRun(exit_code=1, stdout="")])
    driver = CodexCliProbeDriver(launcher=CODEX_LAUNCHER, runner=runner)

    result = _run_codex(driver)

    assert result.status == SegmentStatus.BLOCKED
    assert result.message == "codex cli exited 1"


def test_codex_first_segment_invalid_jsonl_blocked():
    runner = FakeRunner([_ok("not json at all\nstill not json")])
    driver = CodexCliProbeDriver(launcher=CODEX_LAUNCHER, runner=runner)

    result = _run_codex(driver)

    assert result.status == SegmentStatus.BLOCKED
    assert result.message == "invalid codex jsonl"


def test_codex_first_segment_missing_thread_id_blocked():
    runner = FakeRunner([_ok('{"type": "item.completed", "text": "ok"}')])
    driver = CodexCliProbeDriver(launcher=CODEX_LAUNCHER, runner=runner)

    result = _run_codex(driver)

    assert result.status == SegmentStatus.BLOCKED
    assert result.message == "missing codex thread_id"


def test_codex_first_segment_oserror_blocked_no_leak():
    runner = FakeRunner([ToolRun(exit_code=None, error="[WinError 2] 파일 없음")])
    driver = CodexCliProbeDriver(launcher=CODEX_LAUNCHER, runner=runner)

    result = _run_codex(driver)

    assert result.status == SegmentStatus.BLOCKED
    assert result.message.startswith("codex cli spawn failed:")


def test_codex_launcher_unresolved_blocked_without_spawn():
    runner = FakeRunner([])  # 호출되면 AssertionError → spawn 시도 자체가 실패의 증거
    driver = CodexCliProbeDriver(which=lambda _name: None, runner=runner)

    result = _run_codex(driver)

    assert result.status == SegmentStatus.BLOCKED
    assert result.message == "codex launcher not found"
    assert runner.calls == []


def test_codex_short_launcher_name_resolved_to_full_path():
    runner = FakeRunner([_ok(_CODEX_FIRST)])
    driver = CodexCliProbeDriver(
        launcher="codex",
        which=lambda name: CODEX_LAUNCHER if name == "codex" else None,
        runner=runner,
    )

    result = _run_codex(driver)

    assert result.status == SegmentStatus.AWAITING_GATE
    command = runner.calls[0]["command"]
    assert command[0] == CODEX_LAUNCHER
    assert command[0] != "codex"


def test_codex_cmd_launcher_uses_cmd_call_wrapper_no_prompt_in_argv():
    # npm shim codex.cmd → cmd.exe /d /c call wrapper. prompt 본문은 argv에 없어야 한다.
    runner = FakeRunner([_ok(_CODEX_FIRST)])
    driver = CodexCliProbeDriver(launcher=CODEX_LAUNCHER_CMD, runner=runner)

    result = _run_codex(driver, prompt="drive phase one")

    assert result.status == SegmentStatus.AWAITING_GATE
    call = runner.calls[0]
    command = call["command"]
    assert command[0].lower().endswith("cmd.exe")
    assert command[1:4] == ("/d", "/c", "call")
    assert command[4] == CODEX_LAUNCHER_CMD
    assert command[5] == "exec"
    assert "drive phase one" not in command
    assert call["stdin_text"] == "drive phase one"


def test_codex_prompt_too_long_blocked_without_spawn():
    runner = FakeRunner([])
    driver = CodexCliProbeDriver(
        launcher=CODEX_LAUNCHER, runner=runner, max_prompt_chars=10
    )

    result = _run_codex(driver, prompt="x" * 50)

    assert result.status == SegmentStatus.BLOCKED
    assert result.message == "codex cli prompt too long (stdin probe sanity cap)"
    assert runner.calls == []


# ── approve segment ──


def test_codex_approve_segment_success_done():
    runner = FakeRunner([_ok('{"type": "thread.started", "thread_id": "thr-abc"}')])
    driver = CodexCliProbeDriver(launcher=CODEX_LAUNCHER, runner=runner)

    result = _run_codex(driver, resume_token="thr-abc")

    assert result.status == SegmentStatus.DONE
    assert result.resume_token is None
    verdict = result.events[0]
    assert verdict.type == OrchEventType.PHASE_VERDICT
    assert verdict.payload["approved"] is True
    assert verdict.payload["status"] == "done"
    assert verdict.payload["driver"] == "codex-cli"
    assert verdict.payload["thread_id"] == "thr-abc"
    assert verdict.payload["requested_thread_id"] == "thr-abc"


def test_codex_approve_segment_argv_shape_no_sandbox():
    runner = FakeRunner([_ok('{"type": "thread.started", "thread_id": "thr-abc"}')])
    driver = CodexCliProbeDriver(launcher=CODEX_LAUNCHER, runner=runner)

    _run_codex(driver, resume_token="thr-abc")

    command = runner.calls[0]["command"]
    # approve argv: exec resume <thread_id> + 플래그 + stdin sentinel.
    assert command == (
        CODEX_LAUNCHER,
        "exec",
        "resume",
        "thr-abc",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--json",
        "-",
    )
    # resume subcommand는 --sandbox를 거부하므로(설계 §3 실측) approve argv에 없어야 한다.
    assert "--sandbox" not in command


def test_codex_approve_segment_approval_prompt_goes_to_stdin_not_argv():
    runner = FakeRunner([_ok('{"type": "thread.started", "thread_id": "thr-abc"}')])
    driver = CodexCliProbeDriver(launcher=CODEX_LAUNCHER, runner=runner)

    _run_codex(driver, resume_token="thr-abc")

    call = runner.calls[0]
    # 한국어 approval 본문이 argv가 아니라 stdin에 보존된다(UTF-8 round-trip).
    assert CodexCliProbeDriver.APPROVAL_PROMPT not in call["command"]
    assert call["stdin_text"] == CodexCliProbeDriver.APPROVAL_PROMPT


def test_codex_approve_segment_thread_mismatch_blocked():
    # 계약 §6: resume가 다른 thread_id를 반환하면 BLOCKED(Claude와 달리 동일성이 success 조건).
    runner = FakeRunner(
        [_ok('{"type": "thread.started", "thread_id": "thr-different"}')]
    )
    driver = CodexCliProbeDriver(launcher=CODEX_LAUNCHER, runner=runner)

    result = _run_codex(driver, resume_token="thr-requested")

    assert result.status == SegmentStatus.BLOCKED
    assert result.events == ()
    assert "mismatch" in result.message
    assert "thr-requested" in result.message
    assert "thr-different" in result.message


def test_codex_approve_segment_timeout_blocked():
    runner = FakeRunner([ToolRun(exit_code=None, timed_out=True, error="timeout")])
    driver = CodexCliProbeDriver(launcher=CODEX_LAUNCHER, runner=runner)

    result = _run_codex(driver, resume_token="thr-abc")

    assert result.status == SegmentStatus.BLOCKED
    assert result.message == "codex cli timed out"


# ── manager 통합: no-auto-advance ──


def test_codex_driver_no_auto_advance_until_approve(tmp_path):
    store = _store(tmp_path)
    runner = FakeRunner(
        [
            _ok('{"type": "thread.started", "thread_id": "thr-1"}'),  # first
            _ok('{"type": "thread.started", "thread_id": "thr-1"}'),  # approve
        ]
    )
    driver = CodexCliProbeDriver(launcher=CODEX_LAUNCHER, runner=runner)
    manager = OrchRunManager(
        store=store, broadcaster=EventBroadcaster(), driver=driver
    )

    async def scenario():
        start = await manager.start_run(
            OrchRunStartRequest(prompt="go", phase_id="P1")
        )
        # 첫 segment 후 approve 없이는 resume(2번째 spawn)이 일어나지 않는다(AD-7).
        assert start.status == SegmentStatus.AWAITING_GATE
        assert len(runner.calls) == 1
        done = await manager.approve_run(start.run_id)
        return start, done

    start, done = asyncio.run(scenario())

    assert done.status == SegmentStatus.DONE
    assert len(runner.calls) == 2
    store.close()


# ── build_driver ──


def test_build_driver_codex_cli_returns_probe_driver():
    driver = build_driver("codex-cli", launcher=CODEX_LAUNCHER)
    assert isinstance(driver, CodexCliProbeDriver)
    assert driver.launcher == CODEX_LAUNCHER


def test_build_driver_codex_cli_case_insensitive():
    assert isinstance(build_driver("CODEX-CLI", launcher=CODEX_LAUNCHER), CodexCliProbeDriver)
