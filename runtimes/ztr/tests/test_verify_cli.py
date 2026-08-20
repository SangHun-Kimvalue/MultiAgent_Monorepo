"""ztr verify --post-merge CLI 계약 테스트."""
from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from src.engine.post_merge_verifier import VerifyResult
from src.engine.static_review import ToolRun


async def test_cmd_verify_pass_outputs_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    monkeypatch.setattr(runner_mod, "PostMergeVerifier", _PassVerifier)
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)

    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_verify(_args(["src/envelope.py"]))

    assert raised.value.code == 0
    outer = json.loads(stdout.getvalue())
    inner = json.loads(outer["stdout"])
    assert outer["status"] == "PASS"
    assert set(inner) == {"verified", "summary"}
    assert inner["summary"]["verdict"] == "PASS"
    assert inner["verified"][0]["passed"] is True
    assert inner["verified"][0]["path"] == str(
        (Path.cwd() / "src/envelope.py").resolve()
    )


async def test_cmd_verify_changes_requested_for_failed_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    monkeypatch.setattr(runner_mod, "PostMergeVerifier", _FailVerifier)
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)

    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_verify(_args(["src/envelope.py"]))

    assert raised.value.code == 1
    outer = json.loads(stdout.getvalue())
    inner = json.loads(outer["stdout"])
    assert outer["status"] == "CHANGES_REQUESTED"
    assert inner["summary"]["failed"] == 1


async def test_cmd_verify_blocked_for_tool_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    monkeypatch.setattr(runner_mod, "PostMergeVerifier", _BlockedVerifier)
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)

    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_verify(_args(["src/envelope.py"]))

    assert raised.value.code == 2
    outer = json.loads(stdout.getvalue())
    inner = json.loads(outer["stdout"])
    assert outer["status"] == "BLOCKED"
    assert inner["summary"]["blocked"] == 1


async def test_cmd_verify_blocked_for_empty_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)

    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_verify(_args([]))

    assert raised.value.code == 2
    outer = json.loads(stdout.getvalue())
    assert outer["status"] == "BLOCKED"


async def test_cmd_verify_uses_real_verifier_tool_path(
    tmp_path: Path,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod
    from src.engine.post_merge_verifier import PostMergeVerifier

    async def fake_tool(
        self: PostMergeVerifier,
        command: Sequence[str],
        *,
        timeout_s: float,
    ) -> ToolRun:
        del self
        assert timeout_s == 5.0
        return ToolRun(
            command=tuple(command),
            exit_code=0,
            stdout="",
            stderr_sanitized="",
            duration_s=0.01,
        )

    subdirectory = Path(__file__).resolve().parent
    target_dir = subdirectory / "_tmp"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"__{tmp_path.name}_ok.py"
    target.write_text("def ok() -> bool:\n    return True\n", encoding="utf-8")
    request.addfinalizer(lambda: target.unlink(missing_ok=True))
    monkeypatch.setattr(PostMergeVerifier, "_run_tool", fake_tool)
    monkeypatch.chdir(subdirectory)
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)

    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_verify(_args([str(target)]))

    assert raised.value.code == 0
    outer = json.loads(stdout.getvalue())
    inner = json.loads(outer["stdout"])
    assert outer["status"] == "PASS"
    assert inner["summary"]["passed"] == 1
    assert inner["verified"][0]["path"] == str(target.resolve())


async def test_cmd_verify_accepts_repo_external_path_and_passes_absolute_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    target = tmp_path / "outside.py"
    target.write_text("def ok() -> bool:\n    return True\n", encoding="utf-8")
    captured: list[str] = []

    class CapturingVerifier:
        def __init__(self, *, timeout_s: float) -> None:
            assert timeout_s == 5.0

        async def verify(self, value: str) -> VerifyResult:
            captured.append(value)
            return VerifyResult(passed=True)

    monkeypatch.setattr(runner_mod, "PostMergeVerifier", CapturingVerifier)
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)

    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_verify(_args([str(target)]))

    assert raised.value.code == 0
    outer = json.loads(stdout.getvalue())
    inner = json.loads(outer["stdout"])
    assert outer["status"] == "PASS"
    assert captured == [str(target.resolve())]
    assert inner["verified"][0]["path"] == str(target.resolve())


async def test_explicit_verify_paths_are_normalized_absolute_without_git() -> None:
    from src import runner as runner_mod

    cwd = Path.cwd()
    absolute = (cwd / "src" / "envelope.py").resolve()
    selection = await runner_mod._resolve_verify_targets(
        argparse.Namespace(
            changed=False,
            paths=["src/envelope.py", str(absolute)],
        ),
        cwd=cwd,
    )

    assert selection.paths == (str(absolute), str(absolute))
    assert selection.execution_root == cwd.resolve()
    assert selection.git_backed is False


async def test_cmd_verify_missing_target_is_input_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)

    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_verify(_args(["missing.py"]))

    assert raised.value.code == 2
    outer = json.loads(stdout.getvalue())
    assert outer["status"] == "BLOCKED"
    assert outer["exit_code"] == 2


async def test_cmd_verify_non_git_changed_is_input_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    monkeypatch.chdir(tmp_path)
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)

    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_verify(
            argparse.Namespace(
                config=None,
                post_merge=True,
                changed=True,
                paths=[],
                timeout=5.0,
            )
        )

    assert raised.value.code == 2
    assert json.loads(stdout.getvalue())["exit_code"] == 2


async def test_cmd_verify_unexpected_internal_error_exits_seventy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    async def fail(*_: object, **__: object) -> object:
        raise RuntimeError("unexpected internal failure")

    monkeypatch.setattr(runner_mod, "_resolve_verify_targets", fail)
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)

    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_verify(_args(["src/envelope.py"]))

    assert raised.value.code == 70
    outer = json.loads(stdout.getvalue())
    assert outer["status"] == "BLOCKED"
    assert outer["exit_code"] == 70


def test_verify_subprocess_outputs_single_envelope_json() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "src", "verify", "--post-merge", "src/envelope.py"],
        cwd=Path.cwd(),
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert proc.returncode in {0, 2}
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    outer = json.loads(lines[0])
    inner = json.loads(outer["stdout"])
    assert outer["status"] in {"PASS", "BLOCKED"}
    assert inner["summary"]["total"] == 1


def _args(paths: list[str]) -> argparse.Namespace:
    return argparse.Namespace(
        post_merge=True,
        changed=False,
        paths=paths,
        timeout=5.0,
    )


class _PassVerifier:
    def __init__(self, *, timeout_s: float) -> None:
        self.timeout_s = timeout_s

    async def verify(self, target: str) -> VerifyResult:
        return VerifyResult(passed=True, issues=[])


class _FailVerifier:
    def __init__(self, *, timeout_s: float) -> None:
        self.timeout_s = timeout_s

    async def verify(self, target: str) -> VerifyResult:
        return VerifyResult(
            passed=False,
            syntax_ok=False,
            issues=[f"문법 에러: {target}"],
        )


class _BlockedVerifier:
    def __init__(self, *, timeout_s: float) -> None:
        self.timeout_s = timeout_s

    async def verify(self, target: str) -> VerifyResult:
        return VerifyResult(
            passed=False,
            issues=[f"mypy 실행 불능: {target}"],
            blocked=True,
        )
