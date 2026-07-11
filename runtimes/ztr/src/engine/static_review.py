"""Phase 2 mechanical static review engine.

ruff와 mypy 실행, 출력 파싱, verdict 조립을 한 곳에 모은다.
LLM 리뷰는 판단에 관여하지 않고 호출자가 review_text로 주입한다.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from src.envelope import (
    Envelope,
    INTERNAL_ERROR_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    Verdict,
    exit_code_for_verdict,
    redact_stderr,
)


RUFF_SUCCESS_CODES = {0, 1}
MYPY_SUCCESS_CODES = {0, 1}


class ToolRunner(Protocol):
    """테스트에서 deterministic stub을 주입하기 위한 실행 경계."""

    async def __call__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        timeout_s: float,
    ) -> ToolRun:
        ...


@dataclass(frozen=True)
class M2Finding:
    """Mechanical 리뷰가 노출하는 고정 5-field finding."""

    severity: str
    finding: str
    evidence_or_repro: str
    impact: str
    recommendation: str

    def as_payload(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "finding": self.finding,
            "evidence_or_repro": self.evidence_or_repro,
            "impact": self.impact,
            "recommendation": self.recommendation,
        }


@dataclass(frozen=True)
class ToolRun:
    """subprocess 실행 결과."""

    command: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr_sanitized: str
    duration_s: float
    timed_out: bool = False
    error: str = ""


@dataclass(frozen=True)
class ToolReviewResult:
    """도구 실행과 파싱 결과."""

    name: str
    run: ToolRun
    findings: list[M2Finding] = field(default_factory=list)
    parse_error: str = ""
    skipped: bool = False

    @property
    def execution_failed(self) -> bool:
        if self.skipped:
            return False
        if self.run.timed_out or self.parse_error:
            return True
        if self.run.exit_code != 0 and not self.findings:
            return True
        allowed = RUFF_SUCCESS_CODES if self.name == "ruff" else MYPY_SUCCESS_CODES
        return self.run.exit_code not in allowed

    def as_payload(self) -> dict[str, Any]:
        return {
            "command": list(self.run.command),
            "exit_code": self.run.exit_code,
            "timed_out": self.run.timed_out,
            "duration_s": round(self.run.duration_s, 3),
            "diagnostics_count": len(self.findings),
            "stderr_sanitized": self.run.stderr_sanitized,
            "parse_error": self.parse_error,
            "skipped": self.skipped,
        }


@dataclass(frozen=True)
class StaticReviewReport:
    """ruff/mypy만으로 결정된 mechanical 리뷰 보고서."""

    tool_results: dict[str, ToolReviewResult]
    findings: list[M2Finding]
    verdict: Verdict
    exit_code: int
    duration_s: float

    def as_review_payload(self, review_text: str | None) -> dict[str, Any]:
        ruff_count = len(self.tool_results["ruff"].findings)
        mypy_count = len(self.tool_results["mypy"].findings)
        return {
            "findings": [finding.as_payload() for finding in self.findings],
            "tool_results": {
                "ruff": self.tool_results["ruff"].as_payload(),
                "mypy": self.tool_results["mypy"].as_payload(),
            },
            "review_text": review_text,
            "summary": {
                "verdict": self.verdict.value,
                "counts": {
                    "ruff": ruff_count,
                    "mypy": mypy_count,
                    "total": ruff_count + mypy_count,
                },
            },
        }

    def as_envelope(
        self,
        *,
        backend: str,
        model: str,
        duration_s: float,
        review_text: str | None,
        fallback_used: bool,
        not_claimed: list[str],
    ) -> Envelope:
        payload = self.as_review_payload(review_text)
        return Envelope(
            status=self.verdict,
            exit_code=self.exit_code,
            backend=backend,
            model=model,
            duration_s=duration_s,
            stdout=json.dumps(payload, ensure_ascii=False),
            stderr_sanitized="",
            fallback_used=fallback_used,
            not_claimed=not_claimed,
        )


async def run_static_review(
    targets: Sequence[str],
    *,
    cwd: Path,
    mypy_cwd: Path | None = None,
    timeout_s: float = 60.0,
    runner: ToolRunner | None = None,
) -> StaticReviewReport:
    """ruff 대상과 전체 src mypy를 실행하고 deterministic verdict를 만든다."""
    start = time.monotonic()
    effective_runner = runner or run_subprocess_tool
    effective_mypy_cwd = mypy_cwd or cwd
    ruff_targets = [
        target for target in targets
        if target and _is_ruff_target(cwd, target)
    ]

    if ruff_targets:
        ruff_command = (
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--output-format",
            "json",
            *ruff_targets,
        )
        ruff_result = await _run_and_parse_ruff(
            ruff_command,
            cwd=cwd,
            timeout_s=timeout_s,
            runner=effective_runner,
        )
    else:
        ruff_result = _skipped_result("ruff")

    mypy_command = (
        sys.executable,
        "-m",
        "mypy",
        "src",
        "--output",
        "json",
    )
    mypy_result = await _run_and_parse_mypy(
        mypy_command,
        cwd=effective_mypy_cwd,
        timeout_s=timeout_s,
        runner=effective_runner,
    )

    findings = [*ruff_result.findings, *mypy_result.findings]
    blocked = ruff_result.execution_failed or mypy_result.execution_failed
    if blocked:
        verdict = Verdict.BLOCKED
        exit_code = _blocked_exit_code(ruff_result, mypy_result)
    elif findings:
        verdict = Verdict.CHANGES_REQUESTED
        exit_code = exit_code_for_verdict(verdict)
    else:
        verdict = Verdict.PASS
        exit_code = exit_code_for_verdict(verdict)

    return StaticReviewReport(
        tool_results={"ruff": ruff_result, "mypy": mypy_result},
        findings=findings,
        verdict=verdict,
        exit_code=exit_code,
        duration_s=time.monotonic() - start,
    )


async def run_subprocess_tool(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout_s: float,
) -> ToolRun:
    """LESSON-001 규칙: communicate + wait_for + finally kill."""
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
        exit_code = proc.returncode
        if exit_code is None:
            exit_code = INTERNAL_ERROR_EXIT_CODE
    except TimeoutError:
        if proc is not None and proc.returncode is None:
            proc.kill()
            stdout_b, stderr_b = await proc.communicate()
        else:
            stdout_b, stderr_b = b"", b""
        return ToolRun(
            command=tuple(command),
            exit_code=TIMEOUT_EXIT_CODE,
            stdout=_decode(stdout_b),
            stderr_sanitized=redact_stderr(_decode(stderr_b)),
            duration_s=time.monotonic() - start,
            timed_out=True,
            error="timeout",
        )
    except OSError as exc:
        return ToolRun(
            command=tuple(command),
            exit_code=INTERNAL_ERROR_EXIT_CODE,
            stdout="",
            stderr_sanitized=redact_stderr(str(exc)),
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
        stderr_sanitized=redact_stderr(_decode(stderr_b)),
        duration_s=time.monotonic() - start,
    )


async def collect_changed_paths(
    *,
    cwd: Path,
    runner: ToolRunner | None = None,
) -> list[str]:
    """worktree와 index의 변경 파일을 deterministic 순서로 수집한다."""
    effective_runner = runner or run_subprocess_tool
    commands = [
        ("git", "diff", "--name-only", "--diff-filter=ACM"),
        ("git", "diff", "--cached", "--name-only", "--diff-filter=ACM"),
        ("git", "ls-files", "--others", "--exclude-standard"),
    ]
    paths: list[str] = []
    for command in commands:
        run = await effective_runner(command, cwd=cwd, timeout_s=10.0)
        if run.exit_code != 0:
            raise RuntimeError(run.stderr_sanitized or "git diff failed")
        paths.extend(line.strip() for line in run.stdout.splitlines() if line.strip())

    seen: set[str] = set()
    existing: list[str] = []
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        if (cwd / path).exists():
            existing.append(path)
    return existing


async def collect_git_diff(
    targets: Sequence[str],
    *,
    cwd: Path,
    runner: ToolRunner | None = None,
) -> str:
    """Ollama 리뷰용 raw diff를 수집한다. 판단에는 사용하지 않는다."""
    effective_runner = runner or run_subprocess_tool
    commands = [
        ("git", "diff", "--", *targets),
        ("git", "diff", "--cached", "--", *targets),
    ]
    chunks: list[str] = []
    for command in commands:
        run = await effective_runner(command, cwd=cwd, timeout_s=10.0)
        if run.exit_code == 0 and run.stdout.strip():
            chunks.append(run.stdout)
    for target in targets:
        path = cwd / target
        if not path.is_file():
            continue
        tracked = await effective_runner(
            ("git", "ls-files", "--error-unmatch", "--", target),
            cwd=cwd,
            timeout_s=10.0,
        )
        if tracked.exit_code != 0:
            chunks.append(_format_untracked_file_diff(target, path))
    return "\n".join(chunks)


def parse_ruff_json(stdout: str) -> list[M2Finding]:
    """ruff JSON 배열을 M2 finding으로 변환한다."""
    data = json.loads(stdout)
    if not isinstance(data, list):
        raise ValueError("ruff JSON 출력은 배열이어야 합니다")

    findings: list[M2Finding] = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("ruff diagnostic 항목은 object여야 합니다")
        location = item.get("location")
        if not isinstance(location, dict):
            location = {}
        filename = _string_field(item, "filename", "<unknown>")
        row = location.get("row", 0)
        column = location.get("column", 0)
        code = _string_field(item, "code", "ruff")
        message = _string_field(item, "message", "")
        evidence = f"{filename}:{row}:{column} [{code}]"
        findings.append(
            M2Finding(
                severity="major",
                finding=f"ruff: {message}",
                evidence_or_repro=evidence,
                impact="정적 품질 게이트에서 위반이 감지되어 변경 승인 신뢰도가 낮아집니다.",
                recommendation="ruff 지적 사항을 수정한 뒤 ztr review를 다시 실행하세요.",
            )
        )
    return findings


def parse_mypy_jsonl(stdout: str) -> list[M2Finding]:
    """mypy --output json JSONL을 M2 finding으로 변환한다."""
    findings: list[M2Finding] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        item = json.loads(line)
        if not isinstance(item, dict):
            raise ValueError("mypy diagnostic 행은 object여야 합니다")
        filename = _string_field(item, "file", "<unknown>")
        line_no = item.get("line", 0)
        column = item.get("column", 0)
        code = _string_field(item, "code", "mypy")
        message = _string_field(item, "message", "")
        severity = _string_field(item, "severity", "error")
        evidence = f"{filename}:{line_no}:{column} [{code}]"
        findings.append(
            M2Finding(
                severity="major" if severity == "error" else "minor",
                finding=f"mypy: {message}",
                evidence_or_repro=evidence,
                impact="타입 계약 위반은 런타임 결함 또는 리팩터링 회귀로 이어질 수 있습니다.",
                recommendation="mypy 오류를 수정하고 전체 src 타입 체크를 다시 통과시키세요.",
            )
        )
    return findings


async def _run_and_parse_ruff(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout_s: float,
    runner: ToolRunner,
) -> ToolReviewResult:
    run = await runner(command, cwd=cwd, timeout_s=timeout_s)
    try:
        findings = parse_ruff_json(run.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        return ToolReviewResult("ruff", run, parse_error=str(exc))
    return ToolReviewResult("ruff", run, findings=findings)


async def _run_and_parse_mypy(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout_s: float,
    runner: ToolRunner,
) -> ToolReviewResult:
    run = await runner(command, cwd=cwd, timeout_s=timeout_s)
    try:
        findings = parse_mypy_jsonl(run.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        return ToolReviewResult("mypy", run, parse_error=str(exc))
    return ToolReviewResult("mypy", run, findings=findings)


def _skipped_result(name: str) -> ToolReviewResult:
    return ToolReviewResult(
        name=name,
        run=ToolRun(
            command=(),
            exit_code=0,
            stdout="",
            stderr_sanitized="",
            duration_s=0.0,
        ),
        skipped=True,
    )


def _blocked_exit_code(*results: ToolReviewResult) -> int:
    if any(result.run.timed_out for result in results):
        return TIMEOUT_EXIT_CODE
    return exit_code_for_verdict(Verdict.BLOCKED)


def _decode(payload: bytes) -> str:
    return payload.decode("utf-8", errors="replace")


def _string_field(data: dict[str, Any], key: str, default: str) -> str:
    value = data.get(key, default)
    if value is None:
        return default
    return str(value)


def _is_ruff_target(cwd: Path, target: str) -> bool:
    path = cwd / target
    if path.is_dir():
        return True
    return path.suffix in {".py", ".pyi"}


def _format_untracked_file_diff(target: str, path: Path) -> str:
    content = path.read_text(encoding="utf-8", errors="replace")
    lines = [f"diff --git a/{target} b/{target}", "new file mode 100644"]
    lines.extend(f"+{line}" for line in content.splitlines())
    return "\n".join(lines)
