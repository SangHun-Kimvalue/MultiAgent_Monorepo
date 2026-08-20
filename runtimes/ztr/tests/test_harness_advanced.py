"""H4/H6 하네스 단위 테스트 — Quality Gate, Post-Merge Verifier."""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from src.engine.static_review import ToolRun
from src.engine.quality_gate import OutputQualityGate
from src.envelope import TIMEOUT_EXIT_CODE


# ════════════════════════════════════════════
# H4: Output Quality Gate
# ════════════════════════════════════════════


class TestCriticQualityGate:
    """Critic 출력 품질 검증."""

    def _gate(self) -> OutputQualityGate:
        return OutputQualityGate()

    def test_all_praise_findings(self) -> None:
        """findings 전부 칭찬 -> 실질 리뷰 아님."""
        g = self._gate()
        r = g.validate_critic(
            findings=[
                {"severity": "minor", "message": "Fail-Fast 잘 구현됨", "recommendation": "훌륭합니다"},
                {"severity": "minor", "message": "타입 검사 잘 준수", "recommendation": "모범적입니다"},
                {"severity": "minor", "message": "O(n) 효율적으로 잘 작성됨", "recommendation": "우수"},
            ],
            verdict="pass",
        )
        assert not r.passed
        assert r.retry_requested

    def test_mixed_findings_ok(self) -> None:
        """칭찬 + 실제 지적 = OK."""
        g = self._gate()
        r = g.validate_critic(
            findings=[
                {"severity": "major", "message": "Protocol 부재", "recommendation": "추상화 필요"},
                {"severity": "minor", "message": "잘 구현됨", "recommendation": ""},
            ],
            verdict="conditional",
        )
        assert r.passed

    def test_pass_with_major_inconsistency(self) -> None:
        """PASS인데 major -> 불일치."""
        g = self._gate()
        r = g.validate_critic(
            findings=[
                {"severity": "major", "message": "에러 처리 미흡"},
            ],
            verdict="pass",
        )
        assert not r.passed
        assert any("불일치" in i for i in r.issues)

    def test_pass_with_blocker_inconsistency(self) -> None:
        """PASS인데 blocker -> 불일치."""
        g = self._gate()
        r = g.validate_critic(
            findings=[{"severity": "blocker", "message": "보안 취약점"}],
            verdict="pass",
        )
        assert not r.passed

    def test_empty_findings_warn(self) -> None:
        """findings 0건 + PASS -> 부실."""
        g = self._gate()
        r = g.validate_critic(findings=[], verdict="pass")
        assert not r.passed
        assert r.retry_requested

    def test_conditional_with_findings_ok(self) -> None:
        """CONDITIONAL + major findings = 일관성 OK."""
        g = self._gate()
        r = g.validate_critic(
            findings=[{"severity": "major", "message": "SRP 위반"}],
            verdict="conditional",
        )
        assert r.passed


class TestWriterQualityGate:
    """Writer 출력 품질 검증."""

    def _gate(self) -> OutputQualityGate:
        return OutputQualityGate()

    def test_valid_code_passes(self) -> None:
        g = self._gate()
        r = g.validate_writer(
            code="def fibonacci(n: int) -> int:\n    if n <= 1: return n\n    a, b = 0, 1\n    for _ in range(n-1): a, b = b, a+b\n    return b",
            task="fibonacci 구현",
        )
        assert r.passed

    def test_too_short_fails(self) -> None:
        g = self._gate()
        r = g.validate_writer(code="x = 1", task="fibonacci")
        assert not r.passed
        assert r.retry_requested

    def test_empty_functions_fail(self) -> None:
        g = self._gate()
        r = g.validate_writer(
            code="def foo(n: int) -> int: pass\ndef bar(): pass",
            task="foo bar",
        )
        assert not r.passed

    def test_imports_only_fail(self) -> None:
        g = self._gate()
        r = g.validate_writer(
            code="import os\nimport sys\nfrom pathlib import Path",
            task="something",
        )
        assert not r.passed


# ════════════════════════════════════════════
# H6: Post-Merge Verifier
# ════════════════════════════════════════════


class TestPostMergeVerifier:
    """파일 반영 후 검증."""

    async def test_valid_python_passes(self, tmp_path: Path) -> None:
        from src.engine.post_merge_verifier import PostMergeVerifier
        f = tmp_path / "good.py"
        f.write_text("def hello() -> str:\n    return 'world'\n", encoding="utf-8")
        v = PostMergeVerifier()
        r = await v.verify(str(f), check_ruff=False, check_mypy=False)
        assert r.passed
        assert r.syntax_ok

    async def test_syntax_error_fails(self, tmp_path: Path) -> None:
        from src.engine.post_merge_verifier import PostMergeVerifier
        f = tmp_path / "bad.py"
        f.write_text("def broken(:\n", encoding="utf-8")
        v = PostMergeVerifier()
        r = await v.verify(str(f), check_ruff=False, check_mypy=False)
        assert not r.passed
        assert not r.syntax_ok

    async def test_missing_file_fails(self) -> None:
        from src.engine.post_merge_verifier import PostMergeVerifier
        v = PostMergeVerifier()
        r = await v.verify("/nonexistent/file.py")
        assert not r.passed

    async def test_non_python_skips(self, tmp_path: Path) -> None:
        from src.engine.post_merge_verifier import PostMergeVerifier
        f = tmp_path / "data.json"
        f.write_text("{}", encoding="utf-8")
        v = PostMergeVerifier()
        r = await v.verify(str(f))
        assert r.passed  # non-python은 스킵

    async def test_tool_timeout_blocks(self, tmp_path: Path) -> None:
        from src.engine.post_merge_verifier import PostMergeVerifier

        class TimeoutVerifier(PostMergeVerifier):
            async def _run_tool(
                self,
                command: Sequence[str],
                *,
                timeout_s: float,
            ) -> ToolRun:
                del command, timeout_s
                return ToolRun(
                    command=(),
                    exit_code=TIMEOUT_EXIT_CODE,
                    stdout="",
                    stderr_sanitized="",
                    duration_s=30.0,
                    timed_out=True,
                )

        f = tmp_path / "good.py"
        f.write_text("def hello() -> str:\n    return 'world'\n", encoding="utf-8")
        r = await TimeoutVerifier().verify(str(f), check_mypy=False)
        assert not r.passed
        assert r.blocked
        assert r.timed_out

    async def test_tool_stderr_only_failure_blocks(self, tmp_path: Path) -> None:
        from src.engine.post_merge_verifier import PostMergeVerifier

        class MissingToolVerifier(PostMergeVerifier):
            async def _run_tool(
                self,
                command: Sequence[str],
                *,
                timeout_s: float,
            ) -> ToolRun:
                del command, timeout_s
                return ToolRun(
                    command=(),
                    exit_code=1,
                    stdout="",
                    stderr_sanitized="No module named mypy",
                    duration_s=0.01,
                )

        f = tmp_path / "good.py"
        f.write_text("def hello() -> str:\n    return 'world'\n", encoding="utf-8")
        r = await MissingToolVerifier().verify(str(f), check_ruff=False)
        assert not r.passed
        assert r.blocked
        assert not r.timed_out
        assert "mypy 실행 불능" in r.issues
        assert all("0건 에러" not in issue for issue in r.issues)
