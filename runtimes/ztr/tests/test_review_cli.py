"""ztr review CLI 계약 테스트."""
from __future__ import annotations

import argparse
import io
import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from src.engine.static_review import ToolRun
from src.envelope import Verdict


async def test_cmd_review_outputs_single_envelope_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod
    from src.engine import static_review as static_review_mod

    async def fake_tool(
        command: Sequence[str],
        *,
        cwd: Path,
        timeout_s: float,
    ) -> ToolRun:
        del cwd, timeout_s
        if "ruff" in command:
            return ToolRun(
                command=tuple(command),
                exit_code=0,
                stdout="[]",
                stderr_sanitized="",
                duration_s=0.01,
            )
        if "mypy" in command:
            return ToolRun(
                command=tuple(command),
                exit_code=0,
                stdout="",
                stderr_sanitized="",
                duration_s=0.01,
            )
        raise AssertionError(f"unexpected command: {command}")

    async def fake_ollama(*_: object, **__: object) -> tuple[str, list[str]]:
        return "mock ollama review", []

    monkeypatch.setattr(static_review_mod, "run_subprocess_tool", fake_tool)
    monkeypatch.setattr(runner_mod, "collect_git_diff", pytest.fail)
    monkeypatch.setattr(
        runner_mod,
        "_invoke_optional_ollama_review",
        fake_ollama,
    )
    stdout = io.StringIO()
    monkeypatch.setattr(runner_mod.sys, "stdout", stdout)

    args = argparse.Namespace(
        config=None,
        changed=False,
        paths=["src/envelope.py"],
        timeout=5.0,
        verbose=False,
    )
    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_review(args)

    assert raised.value.code == 0
    lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1
    outer = json.loads(lines[0])
    assert outer["status"] == "PASS"
    inner = json.loads(outer["stdout"])
    assert inner["review_text"] == "mock ollama review"
    assert inner["summary"]["verdict"] == "PASS"


async def test_cmd_review_preserves_static_verdict_when_ollama_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    class BrokenOllama:
        async def initialize(self, config: dict[str, object]) -> None:
            del config
            raise RuntimeError("ollama unavailable")

        async def shutdown(self) -> None:
            return None

    monkeypatch.setattr(runner_mod, "run_static_review", _fake_pass_report)
    monkeypatch.setattr(runner_mod, "collect_git_diff", _fake_diff)
    monkeypatch.setattr(runner_mod, "OllamaAgent", BrokenOllama)
    stdout = io.StringIO()
    monkeypatch.setattr(runner_mod.sys, "stdout", stdout)

    args = argparse.Namespace(
        config=None,
        changed=False,
        paths=["src/envelope.py"],
        timeout=5.0,
        verbose=False,
    )
    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_review(args)

    assert raised.value.code == 0
    outer = json.loads(stdout.getvalue())
    inner = json.loads(outer["stdout"])
    assert outer["status"] == "PASS"
    assert outer["not_claimed"] == ["ollama-review"]
    assert outer["fallback_used"] is False
    assert inner["summary"]["verdict"] == "PASS"


async def test_cmd_review_exits_one_for_static_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    monkeypatch.setattr(runner_mod, "run_static_review", _fake_changes_report)
    monkeypatch.setattr(runner_mod, "collect_git_diff", _fake_diff)
    monkeypatch.setattr(
        runner_mod,
        "_invoke_optional_ollama_review",
        _fake_not_claimed_ollama,
    )
    stdout = io.StringIO()
    monkeypatch.setattr(runner_mod.sys, "stdout", stdout)

    args = argparse.Namespace(
        config=None,
        changed=False,
        paths=["src/envelope.py"],
        timeout=5.0,
        verbose=False,
    )
    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_review(args)

    assert raised.value.code == 1
    outer = json.loads(stdout.getvalue())
    inner = json.loads(outer["stdout"])
    assert outer["status"] == "CHANGES_REQUESTED"
    assert inner["summary"]["counts"]["total"] == 1


async def test_cmd_review_explicit_uses_invocation_cwd_and_runtime_root_for_mypy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    class Config:
        def get_role_binding(self, role: str) -> object | None:
            del role
            return None

    captured: dict[str, object] = {}

    async def fake_review(*_: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return await _fake_pass_report()

    monkeypatch.setattr(runner_mod, "load_config", lambda _: Config())
    monkeypatch.setattr(runner_mod, "run_static_review", fake_review)
    monkeypatch.setattr(runner_mod, "collect_git_diff", _fake_diff)
    monkeypatch.setattr(
        runner_mod,
        "_invoke_optional_ollama_review",
        _fake_not_claimed_ollama,
    )
    stdout = io.StringIO()
    monkeypatch.setattr(runner_mod.sys, "stdout", stdout)

    args = argparse.Namespace(
        config=None,
        changed=False,
        paths=["pyproject.toml"],
        timeout=5.0,
        verbose=False,
    )
    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_review(args)

    assert raised.value.code == 0
    assert captured["cwd"] == Path.cwd().resolve()
    assert captured["mypy_cwd"] == runner_mod._runtime_root()


async def test_explicit_review_paths_are_normalized_absolute_without_git() -> None:
    from src import runner as runner_mod

    cwd = Path.cwd()
    absolute = (cwd / "src" / "envelope.py").resolve()
    args = argparse.Namespace(
        changed=False,
        paths=["src/envelope.py", str(absolute)],
    )

    selection = await runner_mod._resolve_review_targets(args, cwd=cwd)

    assert selection.paths == (str(absolute), str(absolute))
    assert selection.execution_root == cwd.resolve()
    assert selection.git_backed is False


async def test_cmd_review_accepts_repo_external_path_without_git_diff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    target = tmp_path / "outside.py"
    target.write_text("", encoding="utf-8")
    monkeypatch.setattr(runner_mod, "run_static_review", _fake_pass_report)
    monkeypatch.setattr(runner_mod, "collect_git_diff", pytest.fail)
    monkeypatch.setattr(
        runner_mod,
        "_invoke_optional_ollama_review",
        _fake_not_claimed_ollama,
    )
    stdout = io.StringIO()
    monkeypatch.setattr(runner_mod.sys, "stdout", stdout)
    args = argparse.Namespace(
        config=None,
        changed=False,
        paths=[str(target)],
        timeout=5.0,
        verbose=False,
    )

    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_review(args)

    assert raised.value.code == 0
    assert json.loads(stdout.getvalue())["status"] == "PASS"


@pytest.mark.parametrize("paths", [[], ["missing.py"]])
async def test_cmd_review_target_input_errors_exit_two(
    paths: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    stdout = io.StringIO()
    monkeypatch.setattr(runner_mod.sys, "stdout", stdout)
    args = argparse.Namespace(
        config=None,
        changed=False,
        paths=paths,
        timeout=5.0,
        verbose=False,
    )

    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_review(args)

    assert raised.value.code == 2
    outer = json.loads(stdout.getvalue())
    assert outer["status"] == "BLOCKED"
    assert outer["exit_code"] == 2


async def test_cmd_review_non_git_changed_is_input_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    monkeypatch.chdir(tmp_path)
    stdout = io.StringIO()
    monkeypatch.setattr(runner_mod.sys, "stdout", stdout)
    args = argparse.Namespace(
        config=None,
        changed=True,
        paths=[],
        timeout=5.0,
        verbose=False,
    )

    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_review(args)

    assert raised.value.code == 2
    assert json.loads(stdout.getvalue())["exit_code"] == 2


async def test_cmd_review_unexpected_internal_error_exits_seventy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    async def fail(*_: object, **__: object) -> object:
        raise RuntimeError("unexpected internal failure")

    monkeypatch.setattr(runner_mod, "_resolve_review_targets", fail)
    stdout = io.StringIO()
    monkeypatch.setattr(runner_mod.sys, "stdout", stdout)
    args = argparse.Namespace(
        config=None,
        changed=False,
        paths=["src/envelope.py"],
        timeout=5.0,
        verbose=False,
    )

    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_review(args)

    assert raised.value.code == 70
    outer = json.loads(stdout.getvalue())
    assert outer["status"] == "BLOCKED"
    assert outer["exit_code"] == 70


async def test_cmd_review_changed_collection_failure_exits_seventy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    async def fake_repo_root(*, cwd: Path) -> Path:
        return cwd.resolve()

    async def fail_collect(*, cwd: Path) -> list[str]:
        del cwd
        raise RuntimeError("git collection failed after root resolution")

    monkeypatch.setattr(runner_mod, "resolve_repo_root", fake_repo_root)
    monkeypatch.setattr(runner_mod, "collect_changed_paths", fail_collect)
    stdout = io.StringIO()
    monkeypatch.setattr(runner_mod.sys, "stdout", stdout)
    args = argparse.Namespace(
        config=None,
        changed=True,
        paths=[],
        timeout=5.0,
        verbose=False,
    )

    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_review(args)

    assert raised.value.code == 70
    assert json.loads(stdout.getvalue())["exit_code"] == 70


async def test_changed_review_preserves_repo_relative_targets_and_collector_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    repo_root = Path.cwd().resolve().parents[1]
    calls: list[Path] = []

    async def fake_repo_root(*, cwd: Path) -> Path:
        assert cwd == Path.cwd()
        return repo_root

    async def fake_collect(*, cwd: Path) -> list[str]:
        calls.append(cwd)
        return ["runtimes/ztr/src/envelope.py"]

    monkeypatch.setattr(runner_mod, "resolve_repo_root", fake_repo_root)
    monkeypatch.setattr(runner_mod, "collect_changed_paths", fake_collect)
    args = argparse.Namespace(changed=True, paths=["ignored.py"])

    selection = await runner_mod._resolve_review_targets(args, cwd=Path.cwd())

    assert selection.paths == ("runtimes/ztr/src/envelope.py",)
    assert selection.execution_root == repo_root
    assert selection.git_backed is True
    assert calls == [repo_root]


async def _fake_diff(*_: object, **__: object) -> str:
    return "diff --git a/src/envelope.py b/src/envelope.py\n"


async def _fake_not_claimed_ollama(
    *_: object,
    **__: object,
) -> tuple[str | None, list[str]]:
    return None, ["ollama-review"]


async def _fake_pass_report(*_: object, **__: object) -> object:
    from src.engine.static_review import StaticReviewReport, ToolReviewResult

    return StaticReviewReport(
        tool_results={
            "ruff": ToolReviewResult("ruff", _tool_run()),
            "mypy": ToolReviewResult("mypy", _tool_run()),
        },
        findings=[],
        verdict=Verdict.PASS,
        exit_code=0,
        duration_s=0.01,
    )


async def _fake_changes_report(*_: object, **__: object) -> object:
    from src.engine.static_review import M2Finding, StaticReviewReport, ToolReviewResult

    finding = M2Finding(
        severity="major",
        finding="ruff: unused import",
        evidence_or_repro="src/example.py:1:1 [F401]",
        impact="정적 품질 게이트 위반",
        recommendation="ruff 지적 사항을 수정하세요.",
    )
    return StaticReviewReport(
        tool_results={
            "ruff": ToolReviewResult("ruff", _tool_run(exit_code=1), [finding]),
            "mypy": ToolReviewResult("mypy", _tool_run()),
        },
        findings=[finding],
        verdict=Verdict.CHANGES_REQUESTED,
        exit_code=1,
        duration_s=0.01,
    )


def _tool_run(*, exit_code: int = 0) -> ToolRun:
    return ToolRun(
        command=(),
        exit_code=exit_code,
        stdout="",
        stderr_sanitized="",
        duration_s=0.01,
    )
