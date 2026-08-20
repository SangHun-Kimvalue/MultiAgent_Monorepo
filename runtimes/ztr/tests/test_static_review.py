"""Phase 2 static review 엔진 테스트."""
from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

from src.engine.static_review import (
    ToolRun,
    collect_git_diff,
    collect_changed_paths,
    parse_mypy_jsonl,
    parse_ruff_json,
    run_static_review,
    run_subprocess_tool,
)
from src.engine.phase_relay import _scope_violations
from src.envelope import TIMEOUT_EXIT_CODE, Verdict


def test_parse_ruff_json_to_m2_finding() -> None:
    stdout = json.dumps([
        {
            "filename": "src/example.py",
            "location": {"row": 3, "column": 5},
            "code": "F401",
            "message": "unused import",
        }
    ])
    findings = parse_ruff_json(stdout)
    assert findings[0].severity == "major"
    assert findings[0].finding == "ruff: unused import"
    assert findings[0].evidence_or_repro == "src/example.py:3:5 [F401]"


def test_parse_mypy_jsonl_to_m2_finding() -> None:
    stdout = (
        '{"file":"src/example.py","line":7,"column":2,'
        '"code":"attr-defined","severity":"error",'
        '"message":"Module has no attribute x"}\n'
    )
    findings = parse_mypy_jsonl(stdout)
    assert findings[0].severity == "major"
    assert findings[0].finding == "mypy: Module has no attribute x"
    assert findings[0].evidence_or_repro == "src/example.py:7:2 [attr-defined]"


async def test_pass_verdict_when_ruff_and_mypy_clean(tmp_path: Path) -> None:
    report = await run_static_review(
        ["src/example.py"],
        cwd=tmp_path,
        runner=_FakeRunner(ruff_stdout="[]", mypy_stdout=""),
    )
    assert report.verdict == Verdict.PASS
    assert report.exit_code == 0
    assert report.findings == []


async def test_changes_requested_when_diagnostics_exist(tmp_path: Path) -> None:
    ruff_stdout = json.dumps([
        {
            "filename": "src/example.py",
            "location": {"row": 1, "column": 1},
            "code": "F401",
            "message": "unused import",
        }
    ])
    report = await run_static_review(
        ["src/example.py"],
        cwd=tmp_path,
        runner=_FakeRunner(ruff_stdout=ruff_stdout, ruff_exit=1, mypy_stdout=""),
    )
    assert report.verdict == Verdict.CHANGES_REQUESTED
    assert report.exit_code == 1
    assert len(report.findings) == 1


async def test_blocked_when_tool_times_out(tmp_path: Path) -> None:
    report = await run_static_review(
        ["src/example.py"],
        cwd=tmp_path,
        runner=_FakeRunner(ruff_stdout="[]", mypy_timeout=True),
    )
    assert report.verdict == Verdict.BLOCKED
    assert report.exit_code == TIMEOUT_EXIT_CODE
    assert report.tool_results["mypy"].execution_failed is True


async def test_blocked_when_output_contract_breaks(tmp_path: Path) -> None:
    report = await run_static_review(
        ["src/example.py"],
        cwd=tmp_path,
        runner=_FakeRunner(ruff_stdout="{}", mypy_stdout=""),
    )
    assert report.verdict == Verdict.BLOCKED
    assert report.tool_results["ruff"].parse_error


async def test_blocked_when_nonzero_has_no_diagnostics(tmp_path: Path) -> None:
    report = await run_static_review(
        ["src/example.py"],
        cwd=tmp_path,
        runner=_FakeRunner(ruff_stdout="[]", mypy_stdout="", mypy_exit=1),
    )
    assert report.verdict == Verdict.BLOCKED
    assert report.tool_results["mypy"].execution_failed is True


async def test_payload_serialization_contains_fixed_contract(tmp_path: Path) -> None:
    report = await run_static_review(
        ["src/example.py"],
        cwd=tmp_path,
        runner=_FakeRunner(ruff_stdout="[]", mypy_stdout=""),
    )
    envelope = report.as_envelope(
        backend="ollama",
        model="qwen2.5-coder:7b",
        duration_s=0.02,
        review_text=None,
        fallback_used=False,
        not_claimed=["ollama-review"],
    )
    outer = envelope.as_stdout_payload()
    inner = json.loads(outer["stdout"])
    assert set(inner) == {"findings", "tool_results", "review_text", "summary"}
    assert inner["tool_results"]["ruff"]["diagnostics_count"] == 0
    assert inner["summary"]["verdict"] == "PASS"
    assert outer["not_claimed"] == ["ollama-review"]
    assert outer["fallback_used"] is False


async def test_run_static_review_can_use_distinct_mypy_cwd(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    runtime_root = tmp_path / "runtime"
    repo_root.mkdir()
    runtime_root.mkdir()
    runner = _RecordingRunner()

    report = await run_static_review(
        ["src/example.py"],
        cwd=repo_root,
        mypy_cwd=runtime_root,
        runner=runner,
    )

    assert report.verdict == Verdict.PASS
    assert runner.calls[0][1] == repo_root
    assert runner.calls[1][1] == runtime_root


async def test_collect_changed_paths_includes_untracked(
    tmp_path: Path,
) -> None:
    tracked = tmp_path / "src" / "tracked.py"
    untracked = tmp_path / "tests" / "test_new.py"
    tracked.parent.mkdir()
    untracked.parent.mkdir()
    tracked.write_text("", encoding="utf-8")
    untracked.write_text("", encoding="utf-8")
    runner = _GitFakeRunner([
        f"{tmp_path}\n",
        "src/tracked.py\0",
        "",
        "tests/test_new.py\0",
    ])

    paths = await collect_changed_paths(cwd=tmp_path, runner=runner)

    assert paths == ["src/tracked.py", "tests/test_new.py"]


async def test_collect_commands_each_use_nul_and_disable_quotepath(
    tmp_path: Path,
) -> None:
    for include_deleted in (False, True):
        runner = _GitFakeRunner([f"{tmp_path}\n", "", "", ""])

        await collect_changed_paths(
            cwd=tmp_path,
            runner=runner,
            include_deleted=include_deleted,
        )

        collect_calls = runner.calls[1:]
        assert len(collect_calls) == 3
        for command in collect_calls:
            assert command[0:3] == ("git", "-c", "core.quotepath=false")
            assert command.count("-z") == 1
        if include_deleted:
            assert "--name-status" in collect_calls[0]
            assert "--cached" in collect_calls[1]
            assert "--name-status" in collect_calls[1]
            assert "--full-name" in collect_calls[2]
        else:
            assert "--name-only" in collect_calls[0]
            assert "--cached" in collect_calls[1]
            assert "--name-only" in collect_calls[1]
            assert "--full-name" not in collect_calls[2]


async def test_collect_non_ascii_paths_from_subdirectory_and_scope_guard(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    relay_cwd = repo_root / "runtimes" / "ztr"
    relay_cwd.mkdir(parents=True)
    _init_git_repo(repo_root)
    tracked = repo_root / "methodology" / "docs" / "설계메모.md"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_root, check=True)
    _commit_git_repo(repo_root)
    tracked.write_text("changed\n", encoding="utf-8")
    untracked = tracked.parent / "새파일.md"
    untracked.write_text("new\n", encoding="utf-8")
    spaced = repo_root / "docs" / "space name.md"
    spaced.parent.mkdir()
    spaced.write_text("space\n", encoding="utf-8")

    changed = await collect_changed_paths(cwd=relay_cwd, include_deleted=True)

    expected = {
        "methodology/docs/설계메모.md",
        "methodology/docs/새파일.md",
        "docs/space name.md",
    }
    assert expected <= set(changed)
    assert all('"' not in path and "\\354" not in path for path in changed)
    violations = _scope_violations(changed, ["methodology/**"])
    assert {item["path"] for item in violations} == {
        "methodology/docs/설계메모.md",
        "methodology/docs/새파일.md",
    }


async def test_collect_default_staged_non_ascii_path_from_subdirectory(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    relay_cwd = repo_root / "runtimes" / "ztr"
    relay_cwd.mkdir(parents=True)
    _init_git_repo(repo_root)
    baseline = repo_root / "README.md"
    baseline.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_root, check=True)
    _commit_git_repo(repo_root)
    staged = repo_root / "methodology" / "docs" / "스테이지.md"
    staged.parent.mkdir(parents=True)
    staged.write_text("staged\n", encoding="utf-8")
    subprocess.run(["git", "add", str(staged)], cwd=repo_root, check=True)

    changed = await collect_changed_paths(cwd=relay_cwd)

    assert "methodology/docs/스테이지.md" in changed
    assert all('"' not in path and "\\354" not in path for path in changed)


def test_test_tmp_directory_is_git_ignored() -> None:
    runtime_root = Path(__file__).resolve().parents[1]
    probe = "tests/_tmp/probe.py"

    ignored = subprocess.run(
        ["git", "check-ignore", probe],
        cwd=runtime_root,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "--", probe],
        cwd=runtime_root,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )

    assert ignored.returncode == 0
    assert untracked.returncode == 0
    assert untracked.stdout == ""


async def test_subdirectory_changed_paths_run_ruff_from_repo_root(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    relay_cwd = repo_root / "runtimes" / "ztr"
    relay_cwd.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True)
    bad_target = repo_root / "other" / "bad.py"
    bad_target.parent.mkdir()
    bad_target.write_text("import os\n", encoding="utf-8")

    targets = await collect_changed_paths(cwd=relay_cwd)
    runner = _RealRuffRecordingRunner()
    report = await run_static_review(targets, cwd=repo_root, runner=runner)

    evidence = "\n".join(item.evidence_or_repro for item in report.findings)
    findings = "\n".join(item.finding for item in report.findings)
    assert "E902" not in evidence
    assert "경로를 찾을 수 없습니다" not in findings
    assert "F401" in evidence
    assert runner.calls[0][1] == repo_root


async def test_collect_git_diff_includes_untracked_content(
    tmp_path: Path,
) -> None:
    new_file = tmp_path / "tests" / "test_new.py"
    new_file.parent.mkdir()
    new_file.write_text("def test_new() -> None:\n    assert True\n", encoding="utf-8")
    runner = _GitFakeRunner(["", "", ""], exit_by_call=[0, 0, 1])

    diff = await collect_git_diff(["tests/test_new.py"], cwd=tmp_path, runner=runner)

    assert "diff --git a/tests/test_new.py b/tests/test_new.py" in diff
    assert "+def test_new() -> None:" in diff


class _FakeRunner:
    def __init__(
        self,
        *,
        ruff_stdout: str,
        mypy_stdout: str = "",
        ruff_exit: int = 0,
        mypy_exit: int = 0,
        mypy_timeout: bool = False,
    ) -> None:
        self._ruff_stdout = ruff_stdout
        self._mypy_stdout = mypy_stdout
        self._ruff_exit = ruff_exit
        self._mypy_exit = mypy_exit
        self._mypy_timeout = mypy_timeout

    async def __call__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        timeout_s: float,
    ) -> ToolRun:
        del cwd, timeout_s
        if "ruff" in command:
            return ToolRun(
                command=tuple(command),
                exit_code=self._ruff_exit,
                stdout=self._ruff_stdout,
                stderr_sanitized="",
                duration_s=0.01,
            )
        if "mypy" in command and self._mypy_timeout:
            return ToolRun(
                command=tuple(command),
                exit_code=TIMEOUT_EXIT_CODE,
                stdout="",
                stderr_sanitized="",
                duration_s=60.0,
                timed_out=True,
            )
        return ToolRun(
            command=tuple(command),
            exit_code=self._mypy_exit,
            stdout=self._mypy_stdout,
            stderr_sanitized="",
            duration_s=0.01,
        )


class _RealRuffRecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], Path]] = []

    async def __call__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        timeout_s: float,
    ) -> ToolRun:
        self.calls.append((tuple(command), cwd))
        if "mypy" in command:
            return ToolRun(
                command=tuple(command),
                exit_code=0,
                stdout="",
                stderr_sanitized="",
                duration_s=0.01,
            )
        return await run_subprocess_tool(command, cwd=cwd, timeout_s=timeout_s)


class _RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], Path]] = []

    async def __call__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        timeout_s: float,
    ) -> ToolRun:
        del timeout_s
        self.calls.append((tuple(command), cwd))
        stdout = "[]" if "ruff" in command else ""
        return ToolRun(
            command=tuple(command),
            exit_code=0,
            stdout=stdout,
            stderr_sanitized="",
            duration_s=0.01,
        )


class _GitFakeRunner:
    def __init__(
        self,
        stdout_by_call: list[str],
        *,
        exit_by_call: list[int] | None = None,
    ) -> None:
        self._stdout_by_call = stdout_by_call
        self._exit_by_call = exit_by_call or [0 for _ in stdout_by_call]
        self.calls: list[tuple[str, ...]] = []

    async def __call__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        timeout_s: float,
    ) -> ToolRun:
        del cwd, timeout_s
        self.calls.append(tuple(command))
        index = len(self.calls) - 1
        stdout = self._stdout_by_call[index]
        exit_code = self._exit_by_call[index]
        return ToolRun(
            command=tuple(command),
            exit_code=exit_code,
            stdout=stdout,
            stderr_sanitized="",
            duration_s=0.01,
        )


def _init_git_repo(repo_root: Path) -> None:
    repo_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True)


def _commit_git_repo(repo_root: Path) -> None:
    subprocess.run(
        [
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", "base",
        ],
        cwd=repo_root,
        check=True,
    )
