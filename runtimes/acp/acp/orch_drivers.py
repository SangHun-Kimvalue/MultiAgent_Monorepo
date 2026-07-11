"""Phase 3 real driver probe — Claude CLI headless driver.

이 모듈은 ACP `Driver` 계약에 opt-in Claude CLI probe driver를 더한다. 기본 driver는
계속 `MockGateDriver`이고, 여기 정의된 `ClaudeCliProbeDriver`는 CLI flag로만 활성화된다.

핵심 계약(R5 준수): 상태 판정은 Claude prose가 아니라 process exit code + stdout JSON
parse + `session_id` 존재 사실만 사용한다. 모든 실패는 예외 누수가 아니라
`SegmentResult(status=BLOCKED, message=...)`로 fail-closed한다.

subprocess 경계는 ztr `static_review.run_subprocess_tool`의 3단 방어(communicate +
wait_for + finally kill)를 같은 패턴으로 복제한다(런타임 경계상 import 대신 복제 허용).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from acp.orch_events import OrchEventType, OrchPhaseEvent
from acp.orch_runs import Driver, MockGateDriver, SegmentResult, SegmentStatus

DRIVER_KIND_MOCK = "mock"
DRIVER_KIND_CLAUDE_CLI = "claude-cli"
DRIVER_KIND_CODEX_CLI = "codex-cli"
ALLOWED_DRIVER_KINDS = (
    DRIVER_KIND_MOCK,
    DRIVER_KIND_CLAUDE_CLI,
    DRIVER_KIND_CODEX_CLI,
)


@dataclass(frozen=True)
class ToolRun:
    """subprocess 실행 결과(드라이버 전용, static_review.ToolRun과 동형 복제)."""

    command: tuple[str, ...] = ()
    exit_code: int | None = 0
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    timed_out: bool = False
    error: str = ""  # OSError(spawn 실패) 시에만 채워진다. timeout은 timed_out으로 구분.


class SubprocessRunner(Protocol):
    """테스트에서 deterministic fake runner를 주입하기 위한 실행 경계."""

    async def __call__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        timeout_s: float,
        stdin_text: str | None = None,
    ) -> ToolRun:
        ...


async def run_subprocess_tool(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout_s: float,
    stdin_text: str | None = None,
) -> ToolRun:
    """LESSON-001 규칙: communicate + wait_for + finally kill 3단 방어.

    timeout이면 프로세스를 kill하고 잔여 출력을 회수한다. OSError(launcher 미해결 잔존
    경로/WinError 등)는 예외를 흘리지 않고 error 필드로 닫는다.

    `stdin_text`가 None이 아니면 stdin PIPE를 열고 그 본문을 UTF-8 bytes로 인코딩해
    `communicate(input_bytes)`로 한 번에 주입한다(EXECUTION_ADAPTER_CONTRACT §4 payload-as-
    stdin). None이면 stdin 인자를 주지 않아 기존 동작을 그대로 유지한다. locale 의존
    text wrapper(Windows cp949 등)를 타지 않도록 항상 bytes 경계만 쓴다.
    """
    start = time.monotonic()
    proc: asyncio.subprocess.Process | None = None
    input_bytes = stdin_text.encode("utf-8") if stdin_text is not None else None
    stdin_pipe = asyncio.subprocess.PIPE if stdin_text is not None else None
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            stdin=stdin_pipe,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(input_bytes), timeout_s
        )
        exit_code = proc.returncode if proc.returncode is not None else -1
    except (asyncio.TimeoutError, TimeoutError):
        if proc is not None and proc.returncode is None:
            proc.kill()
            stdout_b, stderr_b = await proc.communicate()
        else:
            stdout_b, stderr_b = b"", b""
        return ToolRun(
            command=tuple(command),
            exit_code=None,
            stdout=_decode(stdout_b),
            stderr=_decode(stderr_b),
            duration_s=time.monotonic() - start,
            timed_out=True,
            error="timeout",
        )
    except OSError as exc:
        return ToolRun(
            command=tuple(command),
            exit_code=None,
            stdout="",
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


def parse_claude_session_id(stdout: str) -> str:
    """stdout JSON에서 `session_id`를 추출한다.

    parse 실패는 `invalid claude json`, session_id 부재/공백은 `missing claude session_id`로
    ValueError를 던진다. 메시지 문자열은 BLOCKED 매핑 테이블과 1:1로 맞춘다.
    """
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("invalid claude json") from exc
    if not isinstance(data, dict):
        raise ValueError("invalid claude json")
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("missing claude session_id")
    return session_id


def parse_codex_thread_id(stdout: str) -> str:
    """Codex `--json` JSONL stdout에서 `thread.started.thread_id`를 추출한다(계약 §6).

    Codex stdout은 한 줄에 JSON object 1개씩인 JSONL이고, plugin/skill banner 같은
    비JSON prefix/suffix가 섞일 수 있다(설계 §3). 따라서 line-by-line 스캔으로:

    - 비JSON 라인과 JSON non-object 라인은 건너뛴다. banner/warning을 의미판정하지 않는다(R5).
    - 유효 JSON object가 0개면 `invalid codex jsonl`.
    - 유효 object는 있으나 nonblank `thread_id`를 가진 `thread.started`가 없으면
      `missing codex thread_id`.
    - `type=="thread.started"`가 복수면 `invalid codex jsonl`로 fail-closed한다. 현재 실측은
      단수 `thread.started`이며(설계 §3/§5), 복수 출현 BLOCK은 의도된 보수 처리다.
    - `thread.started`가 정확히 1개이고 `thread_id`가 nonblank string일 때만 그 값을 반환한다.

    메시지 문자열은 `CodexCliProbeDriver._interpret`의 BLOCKED 매핑과 1:1로 맞춘다.
    """
    valid_object_count = 0
    thread_started_count = 0
    thread_ids: list[str] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue  # 비JSON 라인(banner/warning) skip — 의미판정하지 않는다.
        if not isinstance(data, dict):
            continue  # JSON non-object(배열/스칼라) skip.
        valid_object_count += 1
        if data.get("type") == "thread.started":
            thread_started_count += 1
            thread_id = data.get("thread_id")
            if isinstance(thread_id, str) and thread_id.strip():
                thread_ids.append(thread_id)
    if valid_object_count == 0:
        raise ValueError("invalid codex jsonl")
    if thread_started_count > 1:
        raise ValueError("invalid codex jsonl")
    if not thread_ids:
        raise ValueError("missing codex thread_id")
    return thread_ids[0]


class ClaudeCliProbeDriver:
    """Claude CLI headless probe driver.

    첫 segment: argv는 `claude -p --output-format json`이고 prompt 본문은 UTF-8 stdin으로
    주입한다 → exit 0 + session_id 확보 시 `AWAITING_GATE`(사람 승인 전 자동 resume 금지,
    AD-7).
    approve segment: argv는 `claude -p --resume <id> --output-format json`이고 approval
    prompt 본문은 stdin으로 주입한다 → exit 0 + session_id 존재 시 `DONE`. 반환 id와 입력
    id 동일성은 success 조건이 아니라 payload 관측값으로만 남긴다.

    prompt 본문을 argv에 싣지 않으므로 Windows `.cmd` wrapper(`cmd.exe /c call`) 계층의
    shell metacharacter(`& | % ^ "`)와 명령줄 길이/cp949 argv 훼손 리스크를 피한다
    (EXECUTION_ADAPTER_CONTRACT §4).
    """

    DEFAULT_TIMEOUT_S = 120.0
    DEFAULT_MAX_PROMPT_CHARS = 8000
    APPROVAL_PROMPT = "Phase gate approved by human reviewer. Continue and acknowledge."

    def __init__(
        self,
        *,
        launcher: str | None = None,
        runner: SubprocessRunner | None = None,
        timeout_s: float | None = None,
        cwd: str | Path | None = None,
        max_prompt_chars: int | None = None,
        which: Callable[[str], str | None] | None = None,
    ) -> None:
        # launcher를 init 시점에 resolved full path(또는 None)로 정규화한다.
        # 미지정이면 which("claude"), 명시됐어도 절대경로가 아니면(짧은 "claude" 포함)
        # which로 PATHEXT 포함 resolve한다 — 짧은 이름이 argv 첫 토큰에 남으면 Windows에서
        # [WinError 2]가 난다(LESSON-001). resolve 실패는 None으로 두고 run_segment에서
        # BLOCKED로 닫는다(여기서 raise하지 않음).
        which_fn = which or shutil.which
        self._launcher = _resolve_launcher(launcher, which_fn)
        self._runner: SubprocessRunner = runner or run_subprocess_tool
        self._timeout_s = timeout_s if timeout_s is not None else self.DEFAULT_TIMEOUT_S
        self._cwd = Path(cwd) if cwd is not None else Path.cwd()
        self._max_prompt_chars = (
            max_prompt_chars if max_prompt_chars is not None else self.DEFAULT_MAX_PROMPT_CHARS
        )

    @property
    def launcher(self) -> str | None:
        return self._launcher

    async def run_segment(
        self,
        *,
        prompt: str,
        project_id: str,
        phase_id: str,
        run_id: str,
        resume_token: str | None,
    ) -> SegmentResult:
        if self._launcher is None:
            return _blocked("claude launcher not found")
        if resume_token is None:
            return await self._first_segment(
                prompt=prompt,
                project_id=project_id,
                phase_id=phase_id,
                run_id=run_id,
            )
        return await self._approve_segment(
            resume_token=resume_token,
            project_id=project_id,
            phase_id=phase_id,
            run_id=run_id,
        )

    async def _first_segment(
        self,
        *,
        prompt: str,
        project_id: str,
        phase_id: str,
        run_id: str,
    ) -> SegmentResult:
        if len(prompt) > self._max_prompt_chars:
            return _blocked("claude cli prompt too long (stdin probe sanity cap)")
        launcher = self._launcher
        if launcher is None:  # run_segment이 가드하나 메서드 경계 너머 narrowing 불가 — 방어 겸 명시
            return _blocked("claude launcher not found")
        command = _launcher_command(launcher, "-p", "--output-format", "json")
        run = await self._runner(
            command, cwd=self._cwd, timeout_s=self._timeout_s, stdin_text=prompt
        )
        session_id, blocked_message = self._interpret(run)
        if blocked_message is not None:
            return _blocked(blocked_message)
        assert session_id is not None  # _interpret 계약: message None이면 session_id 존재
        return SegmentResult(
            status=SegmentStatus.AWAITING_GATE,
            resume_token=session_id,
            message="awaiting human approval (claude-cli)",
            events=(
                _event(
                    project_id,
                    phase_id,
                    OrchEventType.PHASE_STARTED,
                    {
                        "run_id": run_id,
                        "status": SegmentStatus.RUNNING.value,
                        "driver": DRIVER_KIND_CLAUDE_CLI,
                    },
                ),
                _event(
                    project_id,
                    phase_id,
                    OrchEventType.GATE_WAITING,
                    {
                        "run_id": run_id,
                        "status": SegmentStatus.AWAITING_GATE.value,
                        "resume_token": session_id,
                        "driver": DRIVER_KIND_CLAUDE_CLI,
                        "approval_required": True,
                    },
                ),
            ),
        )

    async def _approve_segment(
        self,
        *,
        resume_token: str,
        project_id: str,
        phase_id: str,
        run_id: str,
    ) -> SegmentResult:
        approval_prompt = self.APPROVAL_PROMPT
        if len(approval_prompt) > self._max_prompt_chars:
            return _blocked("claude cli prompt too long (stdin probe sanity cap)")
        launcher = self._launcher
        if launcher is None:  # run_segment 가드의 메서드-경계 narrowing 한계 방어
            return _blocked("claude launcher not found")
        command = _launcher_command(
            launcher,
            "-p",
            "--resume",
            resume_token,
            "--output-format",
            "json",
        )
        run = await self._runner(
            command,
            cwd=self._cwd,
            timeout_s=self._timeout_s,
            stdin_text=approval_prompt,
        )
        session_id, blocked_message = self._interpret(run)
        if blocked_message is not None:
            return _blocked(blocked_message)
        assert session_id is not None
        return SegmentResult(
            status=SegmentStatus.DONE,
            message="approved by human gate (claude-cli)",
            events=(
                _event(
                    project_id,
                    phase_id,
                    OrchEventType.PHASE_VERDICT,
                    {
                        "run_id": run_id,
                        "status": SegmentStatus.DONE.value,
                        "approved": True,
                        "driver": DRIVER_KIND_CLAUDE_CLI,
                        # 관측값: 반환 id(session_id)와 요청 id(requested_session_id)를 병기.
                        # 동일성은 success 조건이 아니므로 payload에만 남긴다.
                        "session_id": session_id,
                        "requested_session_id": resume_token,
                    },
                ),
            ),
        )

    def _interpret(self, run: ToolRun) -> tuple[str | None, str | None]:
        """ToolRun을 (session_id, blocked_message)로 환원한다.

        message가 None이면 session_id가 보장된다. 사실(exit code/플래그)만 분기(R5).
        """
        if run.timed_out:
            return None, "claude cli timed out"
        if run.error:  # timeout은 위에서 처리됐으므로 여기 도달하면 OSError spawn 실패.
            return None, f"claude cli spawn failed: {run.error}"
        if run.exit_code != 0:
            return None, f"claude cli exited {run.exit_code}"
        try:
            session_id = parse_claude_session_id(run.stdout)
        except ValueError as exc:
            return None, str(exc)
        return session_id, None


class CodexCliProbeDriver:
    """Codex CLI headless probe driver.

    `ClaudeCliProbeDriver`와 같은 ACP `Driver` 계약을 Codex CLI로 만족하는 opt-in probe다.
    Codex는 resume id를 `--json` JSONL의 `thread.started.thread_id`로 회수한다(계약 §6).

    첫 segment: argv는
    `codex exec --ignore-user-config --ignore-rules --skip-git-repo-check --json
    --sandbox read-only -C <cwd> -`이고 prompt 본문은 UTF-8 stdin으로 주입한다 →
    exit 0 + thread_id 확보 시 `AWAITING_GATE`(승인 전 자동 resume 금지, AD-7).
    approve segment: argv는
    `codex exec resume <thread_id> --ignore-user-config --ignore-rules
    --skip-git-repo-check --json -`이고 approval 본문은 stdin으로 주입한다 →
    exit 0 + 캡처 thread_id == 요청 resume_token일 때만 `DONE`. 캡처 id가 다르면
    `BLOCKED`(계약 §6: 요청 id와 다른 캡처 id는 BLOCKED).

    `--sandbox`는 first segment에만 `read-only`로 붙인다. resume subcommand는 `--sandbox`를
    `unexpected argument`로 거부하므로(설계 §3 실측) approve argv에는 붙이지 않는다. 따라서
    resume의 sandbox override와 기본 격리 등급은 NOT CLAIMED다.
    `--skip-git-repo-check`는 실측 argv와 일치하도록 항상 포함한다(non-git temp smoke·repo
    trust prompt 회피 목적이며 보안 fail-open PASS 주장 아님, 설계 §5).

    상태 판정은 timeout/spawn error/exit_code/JSONL/thread_id 사실만 분기한다(R5).
    `item.completed.text` 등 Codex prose는 의미판정하지 않는다. 모든 실패는 예외 누수가 아니라
    `SegmentResult(status=BLOCKED, message=...)`로 닫는다.
    prompt 본문을 argv에 싣지 않으므로 Windows `.cmd` wrapper의 shell metacharacter/길이/cp949
    argv 훼손 리스크를 피한다(EXECUTION_ADAPTER_CONTRACT §4).
    """

    DEFAULT_TIMEOUT_S = 120.0
    DEFAULT_MAX_PROMPT_CHARS = 8000
    # 승인 segment는 probe다 — resume 턴을 끝내고 thread_id를 재방출하는 것만 확인한다.
    # Codex는 agentic CLI라 open-ended "계속 진행"은 실작업으로 턴을 길게 끌어 timeout이 난다.
    # 따라서 한 단어 ACK만 요구하는 bounded prompt로 둔다(한국어 → UTF-8 stdin round-trip도 겸함).
    APPROVAL_PROMPT = "페이즈 게이트가 사람 리뷰어에 의해 승인되었습니다. 확인으로 한 단어 ACK만 답하세요."

    def __init__(
        self,
        *,
        launcher: str | None = None,
        runner: SubprocessRunner | None = None,
        timeout_s: float | None = None,
        cwd: str | Path | None = None,
        max_prompt_chars: int | None = None,
        which: Callable[[str], str | None] | None = None,
    ) -> None:
        # ClaudeCliProbeDriver와 동일하게 launcher를 resolved full path(또는 None)로 정규화한다.
        # 미지정이면 which("codex") — npm shim `codex.cmd`가 잡힐 수 있으므로 _launcher_command
        # wrapper로 감싼다(설계 §3). resolve 실패는 None으로 두고 run_segment에서 BLOCKED로 닫는다.
        which_fn = which or shutil.which
        self._launcher = _resolve_launcher(launcher, which_fn, default_name="codex")
        self._runner: SubprocessRunner = runner or run_subprocess_tool
        self._timeout_s = timeout_s if timeout_s is not None else self.DEFAULT_TIMEOUT_S
        self._cwd = Path(cwd) if cwd is not None else Path.cwd()
        self._max_prompt_chars = (
            max_prompt_chars if max_prompt_chars is not None else self.DEFAULT_MAX_PROMPT_CHARS
        )

    @property
    def launcher(self) -> str | None:
        return self._launcher

    async def run_segment(
        self,
        *,
        prompt: str,
        project_id: str,
        phase_id: str,
        run_id: str,
        resume_token: str | None,
    ) -> SegmentResult:
        if self._launcher is None:
            return _blocked("codex launcher not found")
        if resume_token is None:
            return await self._first_segment(
                prompt=prompt,
                project_id=project_id,
                phase_id=phase_id,
                run_id=run_id,
            )
        return await self._approve_segment(
            resume_token=resume_token,
            project_id=project_id,
            phase_id=phase_id,
            run_id=run_id,
        )

    async def _first_segment(
        self,
        *,
        prompt: str,
        project_id: str,
        phase_id: str,
        run_id: str,
    ) -> SegmentResult:
        if len(prompt) > self._max_prompt_chars:
            return _blocked("codex cli prompt too long (stdin probe sanity cap)")
        launcher = self._launcher
        if launcher is None:  # run_segment 가드의 메서드-경계 narrowing 한계 방어
            return _blocked("codex launcher not found")
        command = _launcher_command(
            launcher,
            "exec",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--json",
            "--sandbox",
            "read-only",
            "-C",
            str(self._cwd),
            "-",
        )
        run = await self._runner(
            command, cwd=self._cwd, timeout_s=self._timeout_s, stdin_text=prompt
        )
        thread_id, blocked_message = self._interpret(run)
        if blocked_message is not None:
            return _blocked(blocked_message)
        assert thread_id is not None  # _interpret 계약: message None이면 thread_id 존재
        return SegmentResult(
            status=SegmentStatus.AWAITING_GATE,
            resume_token=thread_id,
            message="awaiting human approval (codex-cli)",
            events=(
                _event(
                    project_id,
                    phase_id,
                    OrchEventType.PHASE_STARTED,
                    {
                        "run_id": run_id,
                        "status": SegmentStatus.RUNNING.value,
                        "driver": DRIVER_KIND_CODEX_CLI,
                    },
                ),
                _event(
                    project_id,
                    phase_id,
                    OrchEventType.GATE_WAITING,
                    {
                        "run_id": run_id,
                        "status": SegmentStatus.AWAITING_GATE.value,
                        "resume_token": thread_id,
                        "driver": DRIVER_KIND_CODEX_CLI,
                        "approval_required": True,
                    },
                ),
            ),
        )

    async def _approve_segment(
        self,
        *,
        resume_token: str,
        project_id: str,
        phase_id: str,
        run_id: str,
    ) -> SegmentResult:
        approval_prompt = self.APPROVAL_PROMPT
        if len(approval_prompt) > self._max_prompt_chars:
            return _blocked("codex cli prompt too long (stdin probe sanity cap)")
        launcher = self._launcher
        if launcher is None:  # run_segment 가드의 메서드-경계 narrowing 한계 방어
            return _blocked("codex launcher not found")
        command = _launcher_command(
            launcher,
            "exec",
            "resume",
            resume_token,
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--json",
            "-",
        )
        run = await self._runner(
            command,
            cwd=self._cwd,
            timeout_s=self._timeout_s,
            stdin_text=approval_prompt,
        )
        thread_id, blocked_message = self._interpret(run)
        if blocked_message is not None:
            return _blocked(blocked_message)
        assert thread_id is not None
        if thread_id != resume_token:
            # 계약 §6: 요청 id와 다른 캡처 id는 BLOCKED(Claude와 달리 Codex는 동일성이 success 조건).
            return _blocked(
                f"codex resume thread mismatch: requested {resume_token!r} got {thread_id!r}"
            )
        return SegmentResult(
            status=SegmentStatus.DONE,
            message="approved by human gate (codex-cli)",
            events=(
                _event(
                    project_id,
                    phase_id,
                    OrchEventType.PHASE_VERDICT,
                    {
                        "run_id": run_id,
                        "status": SegmentStatus.DONE.value,
                        "approved": True,
                        "driver": DRIVER_KIND_CODEX_CLI,
                        # 관측값: 반환 thread_id와 요청 thread_id를 병기. 여기 도달했으면 동일하다.
                        "thread_id": thread_id,
                        "requested_thread_id": resume_token,
                    },
                ),
            ),
        )

    def _interpret(self, run: ToolRun) -> tuple[str | None, str | None]:
        """ToolRun을 (thread_id, blocked_message)로 환원한다.

        message가 None이면 thread_id가 보장된다. 사실(exit code/플래그/JSONL)만 분기(R5).
        """
        if run.timed_out:
            return None, "codex cli timed out"
        if run.error:  # timeout은 위에서 처리됐으므로 여기 도달하면 OSError spawn 실패.
            return None, f"codex cli spawn failed: {run.error}"
        if run.exit_code != 0:
            return None, f"codex cli exited {run.exit_code}"
        try:
            thread_id = parse_codex_thread_id(run.stdout)
        except ValueError as exc:
            return None, str(exc)
        return thread_id, None


def build_driver(kind: str = DRIVER_KIND_MOCK, **kwargs: object) -> Driver:
    """driver kind 문자열을 구체 driver로 환원한다. 미지원 kind는 fail-loud.

    mock은 인자를 받지 않는다. claude-cli/codex-cli만 probe driver 생성 인자를 위임한다.
    """
    normalized = (kind or DRIVER_KIND_MOCK).strip().lower()
    if normalized == DRIVER_KIND_MOCK:
        return MockGateDriver()
    if normalized == DRIVER_KIND_CLAUDE_CLI:
        return ClaudeCliProbeDriver(**kwargs)  # type: ignore[arg-type]
    if normalized == DRIVER_KIND_CODEX_CLI:
        return CodexCliProbeDriver(**kwargs)  # type: ignore[arg-type]
    raise ValueError(f"unknown orch driver kind: {kind!r}")


def _resolve_launcher(
    launcher: str | None,
    which_fn: Callable[[str], str | None],
    default_name: str = "claude",
) -> str | None:
    """launcher를 resolved full path(절대경로) 또는 None으로 정규화한다.

    절대경로면 그대로 신뢰하고, 그 외(짧은 이름/상대경로/미지정)는 which로 resolve한다.
    `default_name`은 launcher 미지정 시 which로 찾을 provider 실행 파일명이다
    (Claude는 "claude", Codex는 "codex").
    """
    target = launcher if launcher is not None else default_name
    if os.path.isabs(target):
        return target
    return which_fn(target)


def _launcher_command(launcher: str, *args: str) -> tuple[str, ...]:
    """Windows shim(.cmd/.bat/.ps1)을 실행 가능한 argv로 감싼다.

    provider-무관 helper다. Claude/Codex 양쪽 driver가 같은 wrapper 로직을 공유한다
    (npm shim `*.cmd`가 `shutil.which`로 잡힐 때 [WinError 2]를 피하기 위함, LESSON-001).
    """
    suffix = Path(launcher).suffix.lower()
    if suffix in {".cmd", ".bat"}:
        return (
            os.environ.get("COMSPEC") or "cmd.exe",
            "/d",
            "/c",
            "call",
            launcher,
            *args,
        )
    if suffix == ".ps1":
        return (
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            launcher,
            *args,
        )
    return (launcher, *args)


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


def _decode(payload: bytes) -> str:
    return payload.decode("utf-8", errors="replace")
