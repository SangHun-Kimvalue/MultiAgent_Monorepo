"""ZTR run-phase relay driver for ACP orchestrator runs.

The relay boundary consumes only the deterministic ztr Envelope fields:
`status` and process exit code. Stdout prose is never interpreted.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from acp.orch_events import OrchEventType, OrchPhaseEvent
from acp.orch_runs import SegmentResult, SegmentStatus

DRIVER_KIND_ZTR_RELAY = "ztr-relay"

STATUS_PASS = "PASS"
STATUS_CHANGES_REQUESTED = "CHANGES_REQUESTED"
STATUS_BLOCKED = "BLOCKED"
EXIT_CODE_BY_STATUS = {
    STATUS_PASS: 0,
    STATUS_CHANGES_REQUESTED: 1,
    STATUS_BLOCKED: 2,
}
TIMEOUT_EXIT_CODE = 124
INTERNAL_ERROR_EXIT_CODE = 70
_ALLOWED_STATUSES = set(EXIT_CODE_BY_STATUS)
_ALLOWED_EXIT_CODES = {*EXIT_CODE_BY_STATUS.values(), TIMEOUT_EXIT_CODE, INTERNAL_ERROR_EXIT_CODE}


@dataclass(frozen=True)
class ToolRun:
    command: tuple[str, ...] = ()
    exit_code: int | None = 0
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    timed_out: bool = False
    error: str = ""


class SubprocessRunner(Protocol):
    async def __call__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        timeout_s: float,
    ) -> ToolRun:
        ...


async def run_subprocess_tool(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout_s: float,
) -> ToolRun:
    """Run ztr with timeout/spawn failures converted to data."""
    start = time.monotonic()
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout_s)
        exit_code = proc.returncode if proc.returncode is not None else -1
    except (asyncio.TimeoutError, TimeoutError):
        if proc is not None and proc.returncode is None:
            proc.kill()
            try:
                # 독립 리뷰 P2: Windows kill은 트리 킬이 아니라 grandchild가 파이프를 물고
                # 있으면 communicate가 EOF를 못 받아 영구 hang — 짧은 유예로 제한하고 포기한다.
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), 5.0)
            except (asyncio.TimeoutError, TimeoutError):
                stdout_b, stderr_b = b"", b""
        else:
            stdout_b, stderr_b = b"", b""
        return ToolRun(
            command=tuple(command),
            exit_code=TIMEOUT_EXIT_CODE,
            stdout=_decode(stdout_b),
            stderr=_decode(stderr_b),
            duration_s=time.monotonic() - start,
            timed_out=True,
            error="timeout",
        )
    except OSError as exc:
        return ToolRun(
            command=tuple(command),
            exit_code=INTERNAL_ERROR_EXIT_CODE,
            stderr=str(exc),
            duration_s=time.monotonic() - start,
            error=str(exc),
        )
    finally:
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()

    return ToolRun(
        command=tuple(command),
        exit_code=exit_code,
        stdout=_decode(stdout_b),
        stderr=_decode(stderr_b),
        duration_s=time.monotonic() - start,
    )


@dataclass(frozen=True)
class RelayEnvelope:
    status: str
    exit_code: int
    duration_s: float


class ZtrRelayDriver:
    """ACP driver that runs one real `ztr run-phase` segment.

    First segment runs ztr and maps PASS to a human gate. Resume segment is
    close-only by design: human approval completes the ACP run without
    re-running ztr.
    """

    # 독립 리뷰 P1: relay는 수십 분급 — 매니저가 first segment를 백그라운드 task로 돌리고
    # run_id를 즉시 반환하게 하는 opt-in capability. PHASE_STARTED는 매니저가 선-emit한다
    # (드라이버는 started를 내지 않음 — BLOCKED 경로에서 started 유실되던 P2도 함께 해소).
    run_in_background = True

    def __init__(
        self,
        *,
        python: str | Path,
        runner_script: str | Path,
        cwd: str | Path,
        implementer_cmd: str,
        reviewer_cmd: str = "",
        mechanical_cmd: str = "",
        test_cmd: str = "",
        output_dir: str | Path = ".ztr/acp-relay",
        leg_timeout_s: float = 600.0,
        process_timeout_s: float | None = None,
        runner: SubprocessRunner | None = None,
    ) -> None:
        self._python = str(python)
        self._runner_script = Path(runner_script)
        self._cwd = Path(cwd)
        self._implementer_cmd = implementer_cmd
        self._reviewer_cmd = reviewer_cmd
        self._mechanical_cmd = mechanical_cmd
        self._test_cmd = test_cmd
        self._output_dir = Path(output_dir)
        self._leg_timeout_s = leg_timeout_s
        self._process_timeout_s = process_timeout_s if process_timeout_s is not None else leg_timeout_s * 5
        self._runner: SubprocessRunner = runner or run_subprocess_tool

    async def run_segment(
        self,
        *,
        prompt: str,
        project_id: str,
        phase_id: str,
        run_id: str,
        resume_token: str | None,
    ) -> SegmentResult:
        try:
            if resume_token is not None:
                return self._approve_segment(
                    project_id=project_id,
                    phase_id=phase_id,
                    run_id=run_id,
                    resume_token=resume_token,
                )
            return await self._first_segment(
                prompt=prompt,
                project_id=project_id,
                phase_id=phase_id,
                run_id=run_id,
            )
        except Exception as exc:
            return _blocked(f"ztr relay internal failure: {exc.__class__.__name__}: {exc}")

    async def _first_segment(
        self,
        *,
        prompt: str,
        project_id: str,
        phase_id: str,
        run_id: str,
    ) -> SegmentResult:
        prompt_path = self._write_prompt(prompt, run_id=run_id)
        command = self._command(prompt_path=prompt_path, phase_id=phase_id)
        # PHASE_STARTED는 매니저가 백그라운드 착수 시점에 선-emit(P1) — 여기서 내지 않는다.
        run = await self._runner(command, cwd=self._cwd, timeout_s=self._process_timeout_s)
        envelope, blocked_message = interpret_run(run)
        if blocked_message is not None:
            return _blocked(blocked_message)
        assert envelope is not None

        if envelope.status == STATUS_PASS:
            gate_token = f"{run_id}:ztr-relay-gate"
            return SegmentResult(
                status=SegmentStatus.AWAITING_GATE,
                resume_token=gate_token,
                message=f"ztr relay PASS in {envelope.duration_s:.3f}s; awaiting human approval",
                events=(
                    _event(
                        project_id,
                        phase_id,
                        OrchEventType.GATE_WAITING,
                        {
                            "run_id": run_id,
                            "status": SegmentStatus.AWAITING_GATE.value,
                            "resume_token": gate_token,
                            "driver": DRIVER_KIND_ZTR_RELAY,
                            "ztr_status": envelope.status,
                            "ztr_exit_code": envelope.exit_code,
                            "ztr_duration_s": envelope.duration_s,
                            "approval_required": True,
                        },
                    ),
                ),
            )

        if envelope.status in {STATUS_CHANGES_REQUESTED, STATUS_BLOCKED}:
            return _blocked(
                f"ztr relay {envelope.status} in {envelope.duration_s:.3f}s "
                f"(exit {envelope.exit_code})"
            )
        return _blocked(f"ztr relay unsupported status: {envelope.status}")

    def _approve_segment(
        self,
        *,
        project_id: str,
        phase_id: str,
        run_id: str,
        resume_token: str,
    ) -> SegmentResult:
        return SegmentResult(
            status=SegmentStatus.DONE,
            message="approved by human gate (ztr-relay)",
            events=(
                _event(
                    project_id,
                    phase_id,
                    OrchEventType.PHASE_VERDICT,
                    {
                        "run_id": run_id,
                        "status": SegmentStatus.DONE.value,
                        "approved": True,
                        "driver": DRIVER_KIND_ZTR_RELAY,
                        "resume_token": resume_token,
                    },
                ),
            ),
        )

    def _write_prompt(self, prompt: str, *, run_id: str) -> Path:
        prompt_dir = _resolve_against(self._cwd, self._output_dir) / "prompts"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = prompt_dir / f"{_safe_name(run_id)}.prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        return prompt_path

    def _command(self, *, prompt_path: Path, phase_id: str) -> tuple[str, ...]:
        command = [
            self._python,
            str(self._runner_script),
            "run-phase",
            "--prompt-file",
            str(prompt_path),
            "--phase-id",
            phase_id,
            "--timeout",
            str(self._leg_timeout_s),
            "--output-dir",
            str(self._output_dir),
            "--implementer-cmd",
            self._implementer_cmd,
        ]
        if self._reviewer_cmd:
            command.extend(("--reviewer-cmd", self._reviewer_cmd))
        if self._mechanical_cmd:
            command.extend(("--mechanical-cmd", self._mechanical_cmd))
        if self._test_cmd:
            command.extend(("--test-cmd", self._test_cmd))
        return tuple(command)


def interpret_run(run: ToolRun) -> tuple[RelayEnvelope | None, str | None]:
    if run.timed_out:
        return None, f"ztr relay timed out after {run.duration_s:.3f}s"
    if run.error:
        return None, f"ztr relay spawn failed: {run.error}"
    envelope, error = parse_last_envelope(run.stdout)
    if error is not None:
        # 독립 리뷰 P2: envelope 미발견의 흔한 원인(잘못된 python으로 즉사 등)은 stderr에만
        # 남는다 — 진단 정보를 message에 표면화(exit + stderr 꼬리).
        return None, f"{error} (exit {run.exit_code}){_stderr_tail(run.stderr)}"
    assert envelope is not None
    if run.exit_code != envelope.exit_code:
        return (
            None,
            "ztr relay status/exit mismatch: "
            f"envelope {envelope.status}/{envelope.exit_code}, process {run.exit_code}",
        )
    return envelope, None


def parse_last_envelope(stdout: str) -> tuple[RelayEnvelope | None, str | None]:
    last_object: dict[str, Any] | None = None
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            candidate = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(candidate, dict):
            last_object = candidate

    if last_object is None:
        return None, "ztr relay envelope not found"
    return _parse_envelope_object(last_object)


def _parse_envelope_object(data: dict[str, Any]) -> tuple[RelayEnvelope | None, str | None]:
    status = data.get("status")
    exit_code = data.get("exit_code")
    duration_s = data.get("duration_s")
    if status not in _ALLOWED_STATUSES:
        return None, "ztr relay envelope invalid status"
    if not isinstance(exit_code, int) or exit_code not in _ALLOWED_EXIT_CODES:
        return None, "ztr relay envelope invalid exit_code"
    if not isinstance(duration_s, int | float) or duration_s < 0:
        return None, "ztr relay envelope invalid duration_s"
    expected = EXIT_CODE_BY_STATUS[status]
    if exit_code in {TIMEOUT_EXIT_CODE, INTERNAL_ERROR_EXIT_CODE}:
        if status != STATUS_BLOCKED:
            return None, "ztr relay envelope status/exit mismatch"
    elif exit_code != expected:
        return None, "ztr relay envelope status/exit mismatch"
    return RelayEnvelope(status=status, exit_code=exit_code, duration_s=float(duration_s)), None


def _blocked(message: str) -> SegmentResult:
    return SegmentResult(status=SegmentStatus.BLOCKED, message=message)


def _event(
    project_id: str,
    phase_id: str,
    event_type: OrchEventType,
    payload: dict[str, object],
) -> OrchPhaseEvent:
    return OrchPhaseEvent(
        project_id=project_id,
        phase_id=phase_id,
        type=event_type,
        ts=datetime.now(timezone.utc),
        payload=payload,
    )


def _resolve_against(base: Path, path: Path) -> Path:
    return path if path.is_absolute() else base / path


def _safe_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "_" for char in value)


def _decode(payload: bytes) -> str:
    return payload.decode("utf-8", errors="replace")


# ztr envelope.redact_stderr와 동일 규칙(32자+ 토큰) — raw stderr가 store/SSE로 나가기 전 정제(2R P3).
_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])")


def _stderr_tail(stderr: str, limit: int = 300) -> str:
    # redact 후 tail — tail을 먼저 뜨면 잘린 토큰이 32자 미만이 돼 정규식을 빠져나간다.
    tail = _TOKEN_RE.sub("[REDACTED]", stderr.strip())[-limit:]
    return f" | stderr tail: {tail}" if tail else ""
