from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from acp.__main__ import _build_ztr_relay_driver
from acp.orch_events import OrchEventType
from acp.orch_relay_driver import (
    STATUS_BLOCKED,
    STATUS_CHANGES_REQUESTED,
    STATUS_PASS,
    ToolRun,
    ZtrRelayDriver,
    parse_last_envelope,
)
from acp.orch_runs import SegmentStatus


def _payload(status: str, exit_code: int, duration_s: float = 1.25) -> str:
    return json.dumps(
        {
            "status": status,
            "exit_code": exit_code,
            "backend": "phase-relay",
            "model": "external-cli",
            "duration_s": duration_s,
            "stdout": "{}",
            "stderr_sanitized": "",
            "fallback_used": False,
            "not_claimed": ["full-e2e"],
        },
        ensure_ascii=False,
    )


class _FakeRunner:
    def __init__(self, run: ToolRun) -> None:
        self.run = run
        self.calls: list[tuple[tuple[str, ...], Path, float]] = []

    async def __call__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        timeout_s: float,
    ) -> ToolRun:
        self.calls.append((tuple(command), cwd, timeout_s))
        return self.run


def _driver(tmp_path: Path, runner: _FakeRunner) -> ZtrRelayDriver:
    return ZtrRelayDriver(
        python="python",
        runner_script=tmp_path / "runner.py",
        cwd=tmp_path,
        implementer_cmd='["python","impl.py"]',
        reviewer_cmd='["python","review.py"]',
        mechanical_cmd='["python","nit.py"]',
        test_cmd='["python","test.py"]',
        output_dir=".ztr/test-relay",
        leg_timeout_s=17.0,
        process_timeout_s=23.0,
        runner=runner,
    )


def _run_first(driver: ZtrRelayDriver):
    return asyncio.run(
        driver.run_segment(
            prompt="작업해줘",
            project_id="MAM",
            phase_id="P6",
            run_id="run-1",
            resume_token=None,
        )
    )


def test_parse_last_envelope_accepts_plain_json() -> None:
    envelope, error = parse_last_envelope(_payload(STATUS_PASS, 0))

    assert error is None
    assert envelope is not None
    assert envelope.status == STATUS_PASS
    assert envelope.exit_code == 0


def test_parse_last_envelope_uses_last_valid_json_line() -> None:
    stdout = "\n".join(
        [
            "progress log",
            _payload(STATUS_CHANGES_REQUESTED, 1, duration_s=2.0),
            "non-json suffix",
            _payload(STATUS_BLOCKED, 2, duration_s=3.0),
        ]
    )

    envelope, error = parse_last_envelope(stdout)

    assert error is None
    assert envelope is not None
    assert envelope.status == STATUS_BLOCKED
    assert envelope.duration_s == 3.0


def test_parse_last_envelope_blocks_when_no_json_object() -> None:
    envelope, error = parse_last_envelope("plain log\n[1, 2, 3]\n")

    assert envelope is None
    assert error == "ztr relay envelope not found"


def test_parse_last_envelope_blocks_status_exit_mismatch() -> None:
    envelope, error = parse_last_envelope(_payload(STATUS_PASS, 1))

    assert envelope is None
    assert error == "ztr relay envelope status/exit mismatch"


def test_pass_maps_to_awaiting_gate_with_events(tmp_path: Path) -> None:
    runner = _FakeRunner(ToolRun(exit_code=0, stdout=_payload(STATUS_PASS, 0)))
    driver = _driver(tmp_path, runner)

    result = _run_first(driver)

    assert result.status == SegmentStatus.AWAITING_GATE
    assert result.resume_token == "run-1:ztr-relay-gate"
    # PHASE_STARTED는 매니저가 백그라운드 착수 시 선-emit(P1) — 드라이버는 gate만 낸다.
    assert [e.type for e in result.events] == [OrchEventType.GATE_WAITING]
    assert result.events[0].payload["ztr_status"] == STATUS_PASS
    assert runner.calls[0][1] == tmp_path
    assert runner.calls[0][2] == 23.0
    command = runner.calls[0][0]
    assert command[:3] == ("python", str(tmp_path / "runner.py"), "run-phase")
    assert "--implementer-cmd" in command
    assert "--reviewer-cmd" in command
    assert (tmp_path / ".ztr" / "test-relay" / "prompts" / "run-1.prompt.txt").read_text(
        encoding="utf-8"
    ) == "작업해줘"


def test_default_process_timeout_allows_multiple_legs(tmp_path: Path) -> None:
    runner = _FakeRunner(ToolRun(exit_code=0, stdout=_payload(STATUS_PASS, 0)))
    driver = ZtrRelayDriver(
        python="python",
        runner_script=tmp_path / "runner.py",
        cwd=tmp_path,
        implementer_cmd='["python","impl.py"]',
        leg_timeout_s=11.0,
        runner=runner,
    )

    result = _run_first(driver)

    assert result.status == SegmentStatus.AWAITING_GATE
    assert runner.calls[0][2] == 55.0


def test_changes_requested_maps_to_blocked(tmp_path: Path) -> None:
    runner = _FakeRunner(ToolRun(exit_code=1, stdout=_payload(STATUS_CHANGES_REQUESTED, 1)))
    driver = _driver(tmp_path, runner)

    result = _run_first(driver)

    assert result.status == SegmentStatus.BLOCKED
    assert result.events == ()
    assert "CHANGES_REQUESTED" in (result.message or "")


def test_blocked_maps_to_blocked(tmp_path: Path) -> None:
    runner = _FakeRunner(ToolRun(exit_code=2, stdout=_payload(STATUS_BLOCKED, 2)))
    driver = _driver(tmp_path, runner)

    result = _run_first(driver)

    assert result.status == SegmentStatus.BLOCKED
    assert result.events == ()
    assert "BLOCKED" in (result.message or "")


def test_process_exit_mismatch_blocks(tmp_path: Path) -> None:
    runner = _FakeRunner(ToolRun(exit_code=2, stdout=_payload(STATUS_PASS, 0)))
    driver = _driver(tmp_path, runner)

    result = _run_first(driver)

    assert result.status == SegmentStatus.BLOCKED
    assert "status/exit mismatch" in (result.message or "")


def test_timeout_fail_closed(tmp_path: Path) -> None:
    runner = _FakeRunner(
        ToolRun(exit_code=124, stdout="", duration_s=23.0, timed_out=True, error="timeout")
    )
    driver = _driver(tmp_path, runner)

    result = _run_first(driver)

    assert result.status == SegmentStatus.BLOCKED
    assert "timed out" in (result.message or "")


def test_spawn_failure_fail_closed(tmp_path: Path) -> None:
    runner = _FakeRunner(ToolRun(exit_code=70, error="[WinError 2]"))
    driver = _driver(tmp_path, runner)

    result = _run_first(driver)

    assert result.status == SegmentStatus.BLOCKED
    assert "spawn failed" in (result.message or "")


def test_resume_is_close_only(tmp_path: Path) -> None:
    runner = _FakeRunner(ToolRun(exit_code=0, stdout=_payload(STATUS_PASS, 0)))
    driver = _driver(tmp_path, runner)

    result = asyncio.run(
        driver.run_segment(
            prompt="ignored",
            project_id="MAM",
            phase_id="P6",
            run_id="run-1",
            resume_token="run-1:ztr-relay-gate",
        )
    )

    assert result.status == SegmentStatus.DONE
    assert result.events[0].type is OrchEventType.PHASE_VERDICT
    assert result.events[0].payload["approved"] is True
    assert runner.calls == []


def test_cli_builder_uses_env_fallbacks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ACP_ZTR_PYTHON", str(tmp_path / "python.exe"))
    monkeypatch.setenv("ACP_ZTR_RUNNER", str(tmp_path / "runner.py"))
    monkeypatch.setenv("ACP_ZTR_CWD", str(tmp_path))
    monkeypatch.setenv("ACP_ZTR_IMPLEMENTER_CMD", "impl")
    monkeypatch.setenv("ACP_ZTR_REVIEWER_CMD", "review")
    monkeypatch.setenv("ACP_ZTR_MECHANICAL_CMD", "mechanical")
    monkeypatch.setenv("ACP_ZTR_TEST_CMD", "test")
    monkeypatch.setenv("ACP_ZTR_TIMEOUT", "9")
    monkeypatch.setenv("ACP_ZTR_PROCESS_TIMEOUT", "44")
    monkeypatch.setenv("ACP_ZTR_OUTPUT_DIR", ".ztr/env")

    driver = _build_ztr_relay_driver(
        ztr_python=None,
        ztr_runner=None,
        ztr_cwd=None,
        ztr_implementer_cmd=None,
        ztr_reviewer_cmd=None,
        ztr_mechanical_cmd=None,
        ztr_test_cmd=None,
        ztr_timeout=None,
        ztr_process_timeout=None,
        ztr_output_dir=None,
    )

    assert driver._python == str(tmp_path / "python.exe")
    assert driver._runner_script == tmp_path / "runner.py"
    assert driver._cwd == tmp_path
    assert driver._implementer_cmd == "impl"
    assert driver._reviewer_cmd == "review"
    assert driver._mechanical_cmd == "mechanical"
    assert driver._test_cmd == "test"
    assert driver._leg_timeout_s == 9.0
    assert driver._process_timeout_s == 44.0
    assert driver._output_dir == Path(".ztr/env")


def test_cli_builder_requires_implementer_cmd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ACP_ZTR_IMPLEMENTER_CMD", raising=False)

    with pytest.raises(SystemExit, match="--ztr-implementer-cmd"):
        _build_ztr_relay_driver(
            ztr_python=None,
            ztr_runner=None,
            ztr_cwd=None,
            ztr_implementer_cmd=None,
            ztr_reviewer_cmd=None,
            ztr_mechanical_cmd=None,
            ztr_test_cmd=None,
            ztr_timeout=None,
            ztr_process_timeout=None,
            ztr_output_dir=None,
        )


def test_driver_declares_background_capability() -> None:
    # 독립 리뷰 P1: 매니저가 이 속성 사실로 백그라운드 실행을 분기한다(R5).
    assert ZtrRelayDriver.run_in_background is True


def test_internal_exception_is_fail_closed(tmp_path: Path) -> None:
    # 독립 리뷰 P3: _write_prompt 등 내부 예외도 BLOCKED로 닫힌다(예외 비방출 계약).
    runner = _FakeRunner(ToolRun(exit_code=0, stdout=_payload(STATUS_PASS, 0)))
    driver = _driver(tmp_path, runner)
    # output_dir 자리에 파일을 만들어 mkdir(parents=True)가 실패하게 한다.
    (tmp_path / ".ztr").mkdir()
    (tmp_path / ".ztr" / "test-relay").write_text("not a dir", encoding="utf-8")

    result = _run_first(driver)

    assert result.status == SegmentStatus.BLOCKED
    assert "internal failure" in (result.message or "")


def test_envelope_not_found_surfaces_exit_and_stderr(tmp_path: Path) -> None:
    # 독립 리뷰 P2: 진단 정보(exit·stderr 꼬리)가 message에 표면화돼야 한다.
    runner = _FakeRunner(
        ToolRun(exit_code=1, stdout="no json here", stderr="ModuleNotFoundError: No module named 'src'")
    )
    driver = _driver(tmp_path, runner)

    result = _run_first(driver)

    assert result.status == SegmentStatus.BLOCKED
    message = result.message or ""
    assert "envelope not found" in message
    assert "exit 1" in message
    assert "ModuleNotFoundError" in message


def test_exit_code_contract_snapshot() -> None:
    # 독립 리뷰 P3: envelope.py 계약 상수를 복제하므로 드리프트를 스냅샷으로 고정.
    from acp.orch_relay_driver import (
        EXIT_CODE_BY_STATUS,
        INTERNAL_ERROR_EXIT_CODE,
        TIMEOUT_EXIT_CODE,
    )

    assert EXIT_CODE_BY_STATUS == {"PASS": 0, "CHANGES_REQUESTED": 1, "BLOCKED": 2}
    assert TIMEOUT_EXIT_CODE == 124
    assert INTERNAL_ERROR_EXIT_CODE == 70


def test_run_subprocess_tool_real_success() -> None:
    # 독립 리뷰 P2: 실 subprocess 경로 무커버 해소 — 성공 케이스.
    import sys

    from acp.orch_relay_driver import run_subprocess_tool

    run = asyncio.run(
        run_subprocess_tool(
            [sys.executable, "-c", "print('hello-relay')"],
            cwd=Path.cwd(),
            timeout_s=30.0,
        )
    )

    assert run.exit_code == 0
    assert "hello-relay" in run.stdout
    assert not run.timed_out


def test_run_subprocess_tool_real_timeout() -> None:
    import sys

    from acp.orch_relay_driver import TIMEOUT_EXIT_CODE, run_subprocess_tool

    run = asyncio.run(
        run_subprocess_tool(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=Path.cwd(),
            timeout_s=1.0,
        )
    )

    assert run.timed_out
    assert run.exit_code == TIMEOUT_EXIT_CODE


def test_run_subprocess_tool_real_spawn_failure() -> None:
    from acp.orch_relay_driver import INTERNAL_ERROR_EXIT_CODE, run_subprocess_tool

    run = asyncio.run(
        run_subprocess_tool(
            ["definitely-not-a-real-binary-xyz"],
            cwd=Path.cwd(),
            timeout_s=5.0,
        )
    )

    assert run.error
    assert run.exit_code == INTERNAL_ERROR_EXIT_CODE


def test_stderr_tail_redacts_long_tokens(tmp_path: Path) -> None:
    # 2R P3: raw stderr의 32자+ 토큰이 store/SSE message로 새지 않는다(ztr redact 규칙 동일).
    secret = "sk-" + "a" * 40
    runner = _FakeRunner(ToolRun(exit_code=1, stdout="no json", stderr=f"auth failed: {secret}"))
    driver = _driver(tmp_path, runner)

    result = _run_first(driver)

    message = result.message or ""
    assert secret not in message
    assert "[REDACTED]" in message
