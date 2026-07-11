"""PostMergeVerifier - 파일 반영 후 자동 검증.

ast.parse (기존) + ruff check + mypy check 를 순차 실행하여
반영된 코드의 실제 품질을 검증한다.
"""
from __future__ import annotations

import ast
import logging
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Sequence
import sys

from src.engine.static_review import ToolRun, run_subprocess_tool

logger = logging.getLogger(__name__)


@dataclass
class VerifyResult:
    """검증 결과."""
    passed: bool
    syntax_ok: bool = True
    ruff_ok: bool = True
    ruff_errors: int = 0
    ruff_output: str = ""
    mypy_ok: bool = True
    mypy_errors: int = 0
    mypy_output: str = ""
    issues: list[str] = field(default_factory=list)
    blocked: bool = False
    timed_out: bool = False


@dataclass(frozen=True)
class ToolCheck:
    """ruff/mypy 1회 실행 결과."""

    ok: bool
    errors: int
    output: str
    blocked: bool = False
    timed_out: bool = False


class PostMergeVerifier:
    """파일 반영 후 자동 검증 파이프라인.

    Usage:
        verifier = PostMergeVerifier()
        result = await verifier.verify("src/auth.py")
        if not result.passed:
            print("검증 실패:", result.issues)
    """

    def __init__(self, *, timeout_s: float = 60.0) -> None:
        self._timeout_s = timeout_s

    async def verify(
        self,
        target_file: str,
        *,
        check_ruff: bool = True,
        check_mypy: bool = True,
    ) -> VerifyResult:
        """파일을 검증한다.

        Args:
            target_file: 검증할 파일 경로
            check_ruff: ruff 검사 여부
            check_mypy: mypy 검사 여부
        """
        path = Path(target_file)
        issues: list[str] = []

        if not path.exists():
            return VerifyResult(
                passed=False,
                issues=[f"파일 없음: {target_file}"],
                blocked=True,
            )

        if not target_file.endswith(".py"):
            return VerifyResult(passed=True, issues=["Python 파일 아님, 스킵"])

        content = path.read_text(encoding="utf-8")

        # 1. ast.parse
        syntax_ok = True
        try:
            ast.parse(content)
        except SyntaxError as exc:
            syntax_ok = False
            issues.append(f"문법 에러: line {exc.lineno}: {exc.msg}")

        # 2. ruff check
        ruff_ok = True
        ruff_errors = 0
        ruff_output = ""
        ruff_blocked = False
        ruff_timed_out = False
        if check_ruff and syntax_ok:
            ruff_check = await self._run_ruff_check(target_file)
            ruff_ok = ruff_check.ok
            ruff_errors = ruff_check.errors
            ruff_output = ruff_check.output
            ruff_blocked = ruff_check.blocked
            ruff_timed_out = ruff_check.timed_out
            if ruff_check.blocked:
                issues.append("ruff 실행 불능")
            elif not ruff_ok:
                issues.append(f"ruff: {ruff_errors}건 에러")

        # 3. mypy check
        mypy_ok = True
        mypy_errors = 0
        mypy_output = ""
        mypy_blocked = False
        mypy_timed_out = False
        if check_mypy and syntax_ok:
            mypy_check = await self._run_mypy_check(target_file)
            mypy_ok = mypy_check.ok
            mypy_errors = mypy_check.errors
            mypy_output = mypy_check.output
            mypy_blocked = mypy_check.blocked
            mypy_timed_out = mypy_check.timed_out
            if mypy_check.blocked:
                issues.append("mypy 실행 불능")
            elif not mypy_ok:
                issues.append(f"mypy: {mypy_errors}건 에러")

        return VerifyResult(
            passed=(
                syntax_ok
                and ruff_ok
                and mypy_ok
                and not ruff_blocked
                and not mypy_blocked
            ),
            syntax_ok=syntax_ok,
            ruff_ok=ruff_ok,
            ruff_errors=ruff_errors,
            ruff_output=ruff_output,
            mypy_ok=mypy_ok,
            mypy_errors=mypy_errors,
            mypy_output=mypy_output,
            issues=issues,
            blocked=ruff_blocked or mypy_blocked,
            timed_out=ruff_timed_out or mypy_timed_out,
        )

    async def _run_ruff_check(self, target_file: str) -> ToolCheck:
        """ruff check 실행."""
        command = (sys.executable, "-m", "ruff", "check", "--no-fix", target_file)
        run = await self._run_tool(command, timeout_s=self._timeout_s)
        stdout = run.stdout.strip()
        stderr = run.stderr_sanitized.strip()
        output = stdout or stderr
        if run.timed_out:
            return ToolCheck(False, 0, output, blocked=True, timed_out=True)
        if run.exit_code == 0:
            return ToolCheck(True, 0, output)
        if run.exit_code == 1 and stdout:
            return ToolCheck(False, _count_nonempty_lines(stdout), output)
        return ToolCheck(False, 0, output, blocked=True)

    async def _run_mypy_check(self, target_file: str) -> ToolCheck:
        """mypy check 실행."""
        command = (sys.executable, "-m", "mypy", target_file, "--no-error-summary")
        run = await self._run_tool(command, timeout_s=self._timeout_s)
        stdout = run.stdout.strip()
        stderr = run.stderr_sanitized.strip()
        output = stdout or stderr
        if run.timed_out:
            return ToolCheck(False, 0, output, blocked=True, timed_out=True)
        if run.exit_code == 0:
            return ToolCheck(True, 0, output)
        if run.exit_code == 1 and stdout:
            return ToolCheck(False, stdout.count(": error"), output)
        return ToolCheck(False, 0, output, blocked=True)

    async def _run_tool(
        self,
        command: Sequence[str],
        *,
        timeout_s: float,
    ) -> ToolRun:
        return await run_subprocess_tool(
            command,
            cwd=Path.cwd(),
            timeout_s=timeout_s,
        )

    async def _run_ruff(self, target_file: str) -> tuple[bool, int, str]:
        """ruff check 실행. (ok, error_count, output)"""
        check = await self._run_ruff_check(target_file)
        return check.ok, check.errors, check.output

    async def _run_mypy(self, target_file: str) -> tuple[bool, int, str]:
        """mypy check 실행. (ok, error_count, output)"""
        check = await self._run_mypy_check(target_file)
        return check.ok, check.errors, check.output


def _count_nonempty_lines(output: str) -> int:
    return len([line for line in output.splitlines() if line.strip()])
