"""Phase 5 deterministic relay for headless CLI legs.

이 모듈은 CLI 프로세스를 고정 순서로 실행하고 결과를 캡처한다.
자연어 의미를 해석하지 않고, child exit code와 timeout만 verdict로 라우팅한다.
"""
from __future__ import annotations

import asyncio
import json
import re
import shlex
import shutil
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from src.envelope import (
    INTERNAL_ERROR_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    Verdict,
    exit_code_for_verdict,
    redact_stderr,
)
from src.engine.resume_chain import ResumeAttempt, ResumeCoordinator, ResumePolicyError
from src.engine.static_review import collect_changed_paths

_STDOUT_PREVIEW_CHARS = 4000


@dataclass(frozen=True)
class RelayCommand:
    """단일 relay leg 명령.

    verdict_source:
      - "exit_code" (기본): child process exit code만으로 verdict 라우팅(기존 동작).
      - "stdout_token": exit code가 아니라 stdout의 `ZTR_VERDICT:` 토큰으로 분기.
        리뷰어처럼 verdict를 exit code로 신호하지 않는(예: `claude -p`는 BLOCKED여도
        exit 0) leg에 쓴다. 토큰이 없으면 fail-closed로 BLOCKED 처리한다.

    gating:
      - True (기본): 이 leg의 verdict가 relay 전체를 게이트한다(non-PASS면 이후 leg
        skip, report verdict로 집계). 기존 leg(implementer/mechanical/test/reviewer).
      - False: **게이트 아님(파일 변형 전용)**. 결정론 autofix leg(예: `ruff --fix`)에
        쓴다. 실행은 하고 산출물/실행오류는 봉투에 기록하지만, verdict를 내지 않으므로
        relay를 멈추거나 report verdict에 집계되지 않고, 다음 leg stdin도 바꾸지 않는다
        (autofix는 디스크의 파일을 고치고, 다음 leg는 그 파일을 본다). [(ㄴ) ADR: codex가
        Windows sandbox에서 self-lint 불가 → implementer 뒤·mechanical 전 결정론 정정.]
    """

    name: str
    argv: list[str]
    verdict_source: str = "exit_code"
    gating: bool = True
    cwd: str | None = None

    def __post_init__(self) -> None:
        if self.verdict_source not in ("exit_code", "stdout_token"):
            raise ValueError(
                f"{self.name} verdict_source는 'exit_code'|'stdout_token'이어야 합니다: "
                f"{self.verdict_source!r}"
            )

    @classmethod
    def from_text(
        cls,
        *,
        name: str,
        value: str,
        verdict_source: str = "exit_code",
        gating: bool = True,
    ) -> RelayCommand:
        """JSON array 또는 shell-like 문자열을 argv로 정규화한다."""
        text = value.strip()
        if not text:
            raise ValueError(f"{name} command가 비어 있습니다")
        if text.startswith("["):
            data = json.loads(text)
            if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
                raise ValueError(f"{name} command JSON은 문자열 배열이어야 합니다")
            return cls(name=name, argv=data, verdict_source=verdict_source, gating=gating)
        return cls(
            name=name,
            argv=_split_command_text(text),
            verdict_source=verdict_source,
            gating=gating,
        )

    def resolved_argv(self) -> list[str]:
        """Windows .CMD 포함 실행 파일을 실제 경로로 해석한다."""
        if not self.argv:
            raise ValueError(f"{self.name} command가 비어 있습니다")
        resolved = shutil.which(self.argv[0])
        if resolved is None:
            raise FileNotFoundError(f"{self.name} 실행 파일을 찾을 수 없습니다: {self.argv[0]}")
        return [resolved, *self.argv[1:]]


@dataclass(frozen=True)
class RelayStepResult:
    """단일 relay leg 실행 결과."""

    name: str
    command: list[str]
    status: Verdict
    exit_code: int
    child_exit_code: int | None
    duration_s: float
    timed_out: bool
    skipped: bool
    gating: bool = True
    stdin_path: Path | None = None
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    envelope_path: Path | None = None
    stdout_preview: str = ""
    stderr_sanitized: str = ""
    resume: dict[str, Any] | None = None
    scope_violations: list[dict[str, str]] | None = None

    def as_payload(self) -> dict[str, Any]:
        """Envelope stdout 내부에 넣을 JSON-safe payload를 반환한다."""
        payload = {
            "name": self.name,
            "command": self.command,
            "status": self.status.value,
            "exit_code": self.exit_code,
            "child_exit_code": self.child_exit_code,
            "duration_s": round(self.duration_s, 6),
            "timed_out": self.timed_out,
            "skipped": self.skipped,
            "gating": self.gating,
            "stdin_path": _path_or_none(self.stdin_path),
            "stdout_path": _path_or_none(self.stdout_path),
            "stderr_path": _path_or_none(self.stderr_path),
            "envelope_path": _path_or_none(self.envelope_path),
            "stdout_preview": self.stdout_preview,
            "stderr_sanitized": self.stderr_sanitized,
        }
        if self.resume is not None:
            payload["resume"] = self.resume
        if self.scope_violations is not None:
            payload["scope_violations"] = self.scope_violations
        return payload


@dataclass(frozen=True)
class PhaseRelayReport:
    """run-phase 전체 실행 결과."""

    phase_id: str
    prompt_path: Path
    run_dir: Path
    steps: list[RelayStepResult] = field(default_factory=list)
    resume_fallback_used: bool = False
    resume_warnings: list[str] = field(default_factory=list)

    @property
    def status(self) -> Verdict:
        # non-gating leg(autofix)는 verdict를 내지 않으므로 집계에서 제외한다.
        for step in self.steps:
            if step.gating and step.status != Verdict.PASS:
                return step.status
        return Verdict.PASS

    @property
    def exit_code(self) -> int:
        gating_steps = [step for step in self.steps if step.gating]
        if any(step.exit_code == TIMEOUT_EXIT_CODE for step in gating_steps):
            return TIMEOUT_EXIT_CODE
        if any(step.exit_code == INTERNAL_ERROR_EXIT_CODE for step in gating_steps):
            return INTERNAL_ERROR_EXIT_CODE
        return exit_code_for_verdict(self.status)

    def as_payload(self) -> dict[str, Any]:
        completed = [step for step in self.steps if not step.skipped]
        failed = next(
            (step for step in self.steps if step.gating and step.status != Verdict.PASS),
            None,
        )
        return {
            "phase": {
                "id": self.phase_id,
                "prompt_path": str(self.prompt_path),
                "run_dir": str(self.run_dir),
            },
            "steps": [step.as_payload() for step in self.steps],
            "summary": {
                "verdict": self.status.value,
                "exit_code": self.exit_code,
                "completed": len(completed),
                "total": len(self.steps),
                "failed_step": failed.name if failed is not None else None,
            },
            "resume": {
                "fallback_used": self.resume_fallback_used,
                "warnings": self.resume_warnings,
            },
        }


class PhaseRelay:
    """헤드리스 CLI leg를 순서대로 실행하는 deterministic relay."""

    def __init__(
        self,
        *,
        prompt_path: Path,
        commands: list[RelayCommand],
        output_dir: Path,
        phase_id: str = "phase",
        timeout_s: float = 600.0,
        resume_coordinator: ResumeCoordinator | None = None,
        forbidden_paths: list[str] | None = None,
        cwd: Path | None = None,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout은 0보다 커야 합니다")
        if not commands:
            raise ValueError("최소 하나의 relay command가 필요합니다")
        invalid_forbidden_paths = [
            raw for raw in forbidden_paths or [] if _normalize_repo_path(raw) is None
        ]
        if invalid_forbidden_paths:
            invalid_patterns = ", ".join(repr(raw) for raw in invalid_forbidden_paths)
            raise ValueError(
                "implementer 금지 경로 패턴이 repo 상대 POSIX 경로가 아닙니다: "
                f"{invalid_patterns}\n"
                "(절대경로·드라이브 문자·'..' 불가. 예: 'methodology/docs', "
                "'**/HANDOFF*.md')"
            )
        self._prompt_path = prompt_path
        self._commands = commands
        self._output_dir = output_dir
        self._phase_id = phase_id
        self._timeout_s = timeout_s
        self._resume_coordinator = resume_coordinator
        self._forbidden_paths = forbidden_paths
        self._cwd = cwd or Path.cwd()

    async def run(self) -> PhaseRelayReport:
        """prompt를 첫 leg stdin으로 전달하고 순차 실행한다."""
        prompt_text = self._prompt_path.read_text(encoding="utf-8-sig")
        run_dir = self._make_run_dir()
        (run_dir / "prompt.txt").write_text(prompt_text, encoding="utf-8")

        steps: list[RelayStepResult] = []
        next_input = prompt_text
        should_stop = False

        for index, command in enumerate(self._commands, start=1):
            if should_stop:
                steps.append(self._skipped_step(command))
                continue

            if not command.gating:
                # non-gating leg(autofix): 실행만 하고 게이트하지 않는다. resume 경로(reviewer/
                # implementer 전용)도 타지 않는다. should_stop·next_input을 바꾸지 않으므로
                # 다음 leg는 autofix가 디스크에서 고친 파일을 그대로 본다. 실행오류도 BLOCKED로
                # 게이트하지 않고 봉투에만 기록한다(파일 변형 전용, verdict 없음).
                result = await self._run_one(
                    index=index,
                    command=command,
                    stdin_text=next_input,
                    run_dir=run_dir,
                )
                steps.append(result)
                continue

            run_command, attempt = self._prepare_resume(command)
            if attempt is not None and attempt.block_reason is not None:
                result = self._blocked_resume_step(command, attempt=attempt)
                steps.append(result)
                next_input = json.dumps(result.as_payload(), ensure_ascii=False)
                should_stop = True
                continue
            if run_command is None:
                raise AssertionError("차단 사유 없는 resume 명령이 누락되었습니다")
            result = await self._run_one(
                index=index,
                command=run_command,
                stdin_text=next_input,
                run_dir=run_dir,
                resume_payload=_attempt_payload(attempt),
            )
            if (
                attempt is not None
                and result.status != Verdict.PASS
                and self._resume_coordinator is not None
                and self._resume_coordinator.should_fallback(attempt)
            ):
                fallback_command, fallback_attempt = self._fallback_resume(
                    command,
                    failed_attempt=attempt,
                )
                result = await self._run_one(
                    index=index,
                    command=fallback_command,
                    stdin_text=next_input,
                    run_dir=run_dir,
                    attempt_suffix="fallback",
                    resume_payload={
                        **fallback_attempt.as_payload(),
                        "failed_resume": result.as_payload(),
                    },
                )
                attempt = fallback_attempt

            if (
                attempt is not None
                and result.status != Verdict.PASS
                and self._resume_coordinator is not None
                and self._resume_coordinator.should_block_failure(attempt)
            ):
                result = self._block_explicit_resume_failure(result, attempt=attempt)

            if attempt is not None and result.status == Verdict.PASS:
                result = self._capture_resume(result, attempt=attempt)

            steps.append(result)
            next_input = json.dumps(result.as_payload(), ensure_ascii=False)
            guard_blocked = False
            if command.name == "implementer" and self._forbidden_paths:
                try:
                    changed = await collect_changed_paths(
                        cwd=self._cwd, include_deleted=True
                    )
                # 수집 실패 = BLOCKED 강등(fail-closed)하고 implementer 증거는 보존한다.
                except Exception as exc:
                    guard_blocked = True
                    guard_step = RelayStepResult(
                        name="implementer-scope-guard",
                        command=[],
                        status=Verdict.BLOCKED,
                        exit_code=INTERNAL_ERROR_EXIT_CODE,
                        child_exit_code=None,
                        duration_s=0.0,
                        timed_out=False,
                        skipped=False,
                        gating=True,
                        stderr_sanitized=(
                            "post-implementer 범위 가드 수집 실패 → fail-closed BLOCKED.\n"
                            f"{redact_stderr(str(exc))}"
                        ),
                        scope_violations=None,
                    )
                    steps.insert(len(steps) - 1, guard_step)
                else:
                    violations = _scope_violations(changed, self._forbidden_paths)
                    if violations:
                        guard_blocked = True
                        summary = "\n".join(
                            f"[scope-guard] {item['path']} <- {item['pattern']}"
                            for item in violations
                        )
                        guard_step = RelayStepResult(
                            name="implementer-scope-guard",
                            command=[],
                            status=Verdict.BLOCKED,
                            exit_code=exit_code_for_verdict(Verdict.BLOCKED),
                            child_exit_code=None,
                            duration_s=0.0,
                            timed_out=False,
                            skipped=False,
                            gating=True,
                            stderr_sanitized=(
                                "post-implementer 현재 변경 집합이 금지 경로와 일치합니다.\n"
                                f"{summary}"
                            ),
                            scope_violations=violations,
                        )
                        steps.insert(len(steps) - 1, guard_step)
            should_stop = guard_blocked or (result.status != Verdict.PASS)

        return PhaseRelayReport(
            phase_id=self._phase_id,
            prompt_path=self._prompt_path,
            run_dir=run_dir,
            steps=steps,
            resume_fallback_used=(
                self._resume_coordinator.fallback_used
                if self._resume_coordinator is not None
                else False
            ),
            resume_warnings=(
                self._resume_coordinator.warnings
                if self._resume_coordinator is not None
                else []
            ),
        )

    def _prepare_resume(
        self,
        command: RelayCommand,
    ) -> tuple[RelayCommand | None, ResumeAttempt | None]:
        if self._resume_coordinator is None:
            return command, None
        role = _resume_role_for_command(command.name)
        argv, attempt = self._resume_coordinator.prepare(command.argv, role=role)
        if attempt.block_reason is not None:
            return None, attempt
        return (
            RelayCommand(
                name=command.name,
                argv=argv,
                verdict_source=command.verdict_source,
                gating=command.gating,
                cwd=attempt.working_dir,
            ),
            attempt,
        )

    def _fallback_resume(
        self,
        command: RelayCommand,
        *,
        failed_attempt: ResumeAttempt,
    ) -> tuple[RelayCommand, ResumeAttempt]:
        if self._resume_coordinator is None:
            raise ValueError("resume coordinator가 없습니다")
        argv, attempt = self._resume_coordinator.fallback(
            command.argv,
            failed_attempt=failed_attempt,
        )
        return (
            RelayCommand(
                name=command.name,
                argv=argv,
                verdict_source=command.verdict_source,
                gating=command.gating,
                # 신규 세션 폴백 argv에는 원래 --cd가 남으므로 cwd를 중복 지정하지 않는다.
                cwd=None,
            ),
            attempt,
        )

    @staticmethod
    def _blocked_resume_step(
        command: RelayCommand, *, attempt: ResumeAttempt
    ) -> RelayStepResult:
        reason = attempt.block_reason or "codex resume argv 차단"
        return RelayStepResult(
            name=command.name,
            command=command.argv,
            status=Verdict.BLOCKED,
            exit_code=exit_code_for_verdict(Verdict.BLOCKED),
            child_exit_code=None,
            duration_s=0.0,
            timed_out=False,
            skipped=False,
            gating=command.gating,
            stderr_sanitized=redact_stderr(reason),
            resume=attempt.as_payload(),
        )

    def _capture_resume(
        self,
        result: RelayStepResult,
        *,
        attempt: ResumeAttempt,
    ) -> RelayStepResult:
        if self._resume_coordinator is None or result.stdout_path is None:
            return result
        try:
            stdout_text = result.stdout_path.read_text(encoding="utf-8")
            session_id = self._resume_coordinator.capture(stdout_text, attempt=attempt)
        except ResumePolicyError as exc:
            updated = replace(
                result,
                status=Verdict.BLOCKED,
                exit_code=exit_code_for_verdict(Verdict.BLOCKED),
                stderr_sanitized=redact_stderr(str(exc)),
                resume={**(result.resume or {}), "policy_error": str(exc)},
            )
            self._write_step_envelope(updated)
            return updated
        except ValueError as exc:
            updated = replace(
                result,
                status=Verdict.BLOCKED,
                exit_code=INTERNAL_ERROR_EXIT_CODE,
                stderr_sanitized=redact_stderr(str(exc)),
                resume={**(result.resume or {}), "capture_error": str(exc)},
            )
            self._write_step_envelope(updated)
            return updated
        if session_id is None:
            return result
        updated = replace(
            result,
            resume={**(result.resume or {}), "captured_session_id": session_id},
        )
        self._write_step_envelope(updated)
        return updated

    def _block_explicit_resume_failure(
        self,
        result: RelayStepResult,
        *,
        attempt: ResumeAttempt,
    ) -> RelayStepResult:
        if self._resume_coordinator is None:
            return result
        warning = self._resume_coordinator.block_warning(attempt)
        updated = replace(
            result,
            status=Verdict.BLOCKED,
            exit_code=exit_code_for_verdict(Verdict.BLOCKED),
            stderr_sanitized=redact_stderr(
                "\n".join(part for part in [result.stderr_sanitized, warning] if part)
            ),
            resume={**(result.resume or {}), "policy_error": warning},
        )
        self._write_step_envelope(updated)
        return updated

    def _make_run_dir(self) -> Path:
        safe_phase = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in self._phase_id)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        run_dir = self._output_dir / f"{stamp}-{safe_phase}"
        suffix = 1
        while run_dir.exists():
            run_dir = self._output_dir / f"{stamp}-{safe_phase}-{suffix}"
            suffix += 1
        run_dir.mkdir(parents=True)
        return run_dir

    async def _run_one(
        self,
        *,
        index: int,
        command: RelayCommand,
        stdin_text: str,
        run_dir: Path,
        attempt_suffix: str = "",
        resume_payload: dict[str, Any] | None = None,
    ) -> RelayStepResult:
        suffix = f"-{attempt_suffix}" if attempt_suffix else ""
        step_prefix = f"{index:02d}-{command.name}{suffix}"
        stdin_path = run_dir / f"{step_prefix}.stdin.txt"
        stdout_path = run_dir / f"{step_prefix}.stdout.txt"
        stderr_path = run_dir / f"{step_prefix}.stderr.txt"
        envelope_path = run_dir / f"{step_prefix}.envelope.json"

        stdin_path.write_text(stdin_text, encoding="utf-8")
        start = time.monotonic()
        resolved = command.argv
        stdout_text = ""
        stderr_text = ""
        child_exit_code: int | None = None
        timed_out = False

        try:
            resolved = command.resolved_argv()
            if command.cwd is None:
                proc = await asyncio.create_subprocess_exec(
                    *resolved,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    *resolved,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=command.cwd,
                )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(stdin_text.encode("utf-8")),
                    timeout=self._timeout_s,
                )
                child_exit_code = proc.returncode
            except asyncio.TimeoutError:
                timed_out = True
                proc.kill()
                stdout_b, stderr_b = await proc.communicate()
                child_exit_code = proc.returncode

            stdout_text = stdout_b.decode("utf-8", errors="replace")
            stderr_text = stderr_b.decode("utf-8", errors="replace")
            status, exit_code = _route_exit_code(child_exit_code, timed_out=timed_out)
            if command.verdict_source == "stdout_token" and not timed_out:
                status, exit_code, stderr_text = _resolve_stdout_token_verdict(
                    child_exit_code=child_exit_code,
                    stdout_text=stdout_text,
                    stderr_text=stderr_text,
                )
        except (OSError, ValueError) as exc:
            status = Verdict.BLOCKED
            exit_code = INTERNAL_ERROR_EXIT_CODE
            stderr_text = str(exc)

        duration_s = time.monotonic() - start
        stdout_path.write_text(stdout_text, encoding="utf-8")
        stderr_path.write_text(stderr_text, encoding="utf-8")
        result = RelayStepResult(
            name=command.name,
            command=resolved,
            status=status,
            exit_code=exit_code,
            child_exit_code=child_exit_code,
            duration_s=duration_s,
            timed_out=timed_out,
            skipped=False,
            gating=command.gating,
            stdin_path=stdin_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            envelope_path=envelope_path,
            stdout_preview=stdout_text[:_STDOUT_PREVIEW_CHARS],
            stderr_sanitized=redact_stderr(stderr_text),
            resume=resume_payload,
        )
        self._write_step_envelope(result)
        return result

    @staticmethod
    def _skipped_step(command: RelayCommand) -> RelayStepResult:
        return RelayStepResult(
            name=command.name,
            command=command.argv,
            status=Verdict.BLOCKED,
            exit_code=exit_code_for_verdict(Verdict.BLOCKED),
            child_exit_code=None,
            duration_s=0.0,
            timed_out=False,
            skipped=True,
            gating=command.gating,
        )

    @staticmethod
    def _write_step_envelope(result: RelayStepResult) -> None:
        if result.envelope_path is None:
            return
        result.envelope_path.write_text(
            json.dumps(result.as_payload(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


# 단독 라인 토큰만 인정한다(산문/예시 속 멘션 오탐 방지). 여러 개면 마지막을 채택.
# trailing에 \r 허용(Windows CRLF: print → "...\r\n", MULTILINE $ 앞 \r 잔류 방지).
_VERDICT_TOKEN_RE = re.compile(
    r"^[ \t]*ZTR_VERDICT:[ \t]*(PASS|CHANGES_REQUESTED|BLOCKED)[ \t\r]*$",
    re.MULTILINE,
)


def _extract_stdout_verdict(stdout_text: str) -> Verdict | None:
    """stdout에서 마지막 **단독 라인** `ZTR_VERDICT: <verdict>` 토큰을 추출한다.

    산문 의미를 해석하지 않고, 합의된 구조화 토큰만 읽는다(R5 일관). 줄 전체가 토큰인
    경우만 인정해 "예: ZTR_VERDICT: PASS" 같은 산문/예시 멘션을 배제한다.

    reviewer가 `claude -p --output-format json`처럼 **JSON 한 줄**로 출력하면 토큰이 JSON
    문자열 값 안에 escape된 개행(`\\n`) 뒤에 들어가 물리 단독 라인이 아니다. 이 경우
    JSON을 파싱해 문자열 값(개행 unescape됨)에서 단독 라인 토큰을 다시 찾는다. 토큰이
    없으면 None을 반환하고, 호출부는 fail-closed로 BLOCKED 처리한다.
    """
    matches = _VERDICT_TOKEN_RE.findall(stdout_text)
    if matches:
        return Verdict(matches[-1])
    for text in _json_string_values(stdout_text):
        nested = _VERDICT_TOKEN_RE.findall(text)
        if nested:
            return Verdict(nested[-1])
    return None


def _json_string_values(stdout_text: str) -> list[str]:
    """JSON 출력(전체 또는 줄 단위)에서 모든 문자열 값을 수집한다(개행 unescape됨)."""
    collected: list[str] = []
    for blob in (stdout_text, *stdout_text.splitlines()):
        stripped = blob.strip()
        if not stripped or stripped[0] not in "{[":
            continue
        try:
            obj = json.loads(stripped)
        except ValueError:
            continue
        _collect_json_strings(obj, collected)
    return collected


def _collect_json_strings(obj: object, out: list[str]) -> None:
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for value in obj.values():
            _collect_json_strings(value, out)
    elif isinstance(obj, list):
        for value in obj:
            _collect_json_strings(value, out)


def _resolve_stdout_token_verdict(
    *,
    child_exit_code: int | None,
    stdout_text: str,
    stderr_text: str,
) -> tuple[Verdict, int, str]:
    """verdict_source=stdout_token leg의 verdict를 결정한다(fail-closed).

    - child가 non-zero exit이면 프로세스 실패로 보고 토큰을 신뢰하지 않는다 → BLOCKED.
      (non-zero exit + 잔여 PASS 토큰이 PASS로 둔갑하는 false-PASS 차단.)
    - exit 0이지만 단독 라인 토큰이 없으면 → fail-closed BLOCKED.
    - exit 0 + 토큰 → 토큰 verdict.
    """
    if child_exit_code != 0:
        msg = (
            "[relay] verdict-source=stdout_token leg가 non-zero exit"
            f"({child_exit_code}) → stdout 토큰 무시, fail-closed BLOCKED."
        )
        return Verdict.BLOCKED, exit_code_for_verdict(Verdict.BLOCKED), f"{stderr_text}\n{msg}".strip()
    token_verdict = _extract_stdout_verdict(stdout_text)
    if token_verdict is None:
        msg = (
            "[relay] verdict-source=stdout_token leg인데 단독 라인 ZTR_VERDICT"
            " 토큰을 찾지 못함 → fail-closed BLOCKED."
        )
        return Verdict.BLOCKED, exit_code_for_verdict(Verdict.BLOCKED), f"{stderr_text}\n{msg}".strip()
    return token_verdict, exit_code_for_verdict(token_verdict), stderr_text


def _route_exit_code(child_exit_code: int | None, *, timed_out: bool) -> tuple[Verdict, int]:
    if timed_out:
        return Verdict.BLOCKED, TIMEOUT_EXIT_CODE
    if child_exit_code == 0:
        return Verdict.PASS, 0
    if child_exit_code == 1:
        return Verdict.CHANGES_REQUESTED, 1
    if child_exit_code in {2, INTERNAL_ERROR_EXIT_CODE}:
        return Verdict.BLOCKED, child_exit_code
    return Verdict.BLOCKED, exit_code_for_verdict(Verdict.BLOCKED)


def _path_or_none(path: Path | None) -> str | None:
    return str(path) if path is not None else None


def _scope_violations(
    changed: list[str], patterns: list[str]
) -> list[dict[str, str]]:
    """repo 상대 POSIX 경로를 exact/prefix/glob 규칙으로만 판정한다.

    glob의 ``*``는 단일 경로 segment, ``**``는 여러 segment를 포함한다.
    절대경로와 ``..`` 경로는 repo 변경 경로가 아니므로 매칭 대상에서 제외한다.
    """
    violations: list[dict[str, str]] = []
    for raw_path in changed:
        path = _normalize_repo_path(raw_path)
        if path is None:
            continue
        for raw_pattern in patterns:
            pattern = _normalize_repo_path(raw_pattern)
            if pattern is None:
                # 패턴 유효성은 PhaseRelay.__init__에서 fail-closed로 선검증된다
                # (여기 도달 = 직접 호출 경로).
                continue
            has_glob = any(char in pattern for char in "*?")
            matched = (
                bool(re.fullmatch(_glob_regex(pattern), path))
                if has_glob
                else path == pattern or path.startswith(f"{pattern}/")
            )
            if matched:
                violations.append({"path": path, "pattern": raw_pattern})
    return violations


def _normalize_repo_path(value: str) -> str | None:
    raw = value.strip().replace("\\", "/")
    if not raw or re.match(r"^[A-Za-z]:", raw) or raw.startswith("/"):
        return None
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if ".." in parts:
        return None
    return "/".join(parts)


def _glob_regex(pattern: str) -> str:
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


def _resume_role_for_command(command_name: str) -> str:
    if command_name == "implementer-reviewer":
        return "reviewer"
    return command_name


def _attempt_payload(attempt: ResumeAttempt | None) -> dict[str, Any] | None:
    return attempt.as_payload() if attempt is not None else None


def _split_command_text(text: str) -> list[str]:
    if sys.platform == "win32":
        return [_strip_wrapping_quotes(part) for part in shlex.split(text, posix=False)]
    return shlex.split(text)


def _strip_wrapping_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value
