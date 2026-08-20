"""ztr invariants CLI 계약 테스트."""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.engine.invariants import (
    ChangedPath,
    InvariantCheck,
    InvariantIssue,
    InvariantReport,
)
from src.envelope import Verdict
from tests.test_invariants import _init_fixture_repo


async def test_cmd_invariants_pass_outputs_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    monkeypatch.setattr(runner_mod, "InvariantEngine", _engine_factory(_pass_report()))
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)

    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_invariants(_args())

    assert raised.value.code == 0
    outer = json.loads(stdout.getvalue())
    inner = json.loads(outer["stdout"])
    assert outer["status"] == "PASS"
    assert set(inner) == {"checks", "summary"}
    assert inner["summary"]["verdict"] == "PASS"


async def test_cmd_invariants_changes_requested_exit_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    monkeypatch.setattr(runner_mod, "InvariantEngine", _engine_factory(_cr_report()))
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)

    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_invariants(_args(paths=["docs/ROADMAP_V2.md"]))

    assert raised.value.code == 1
    outer = json.loads(stdout.getvalue())
    inner = json.loads(outer["stdout"])
    assert outer["status"] == "CHANGES_REQUESTED"
    assert inner["checks"][0]["issues"][0]["severity"] == "major"


async def test_cmd_invariants_blocked_exit_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    class BrokenEngine:
        def __init__(self, *, root: object, since: str) -> None:
            del root, since

        async def run(self, *, paths: list[str] | None) -> InvariantReport:
            del paths
            raise RuntimeError("git failed")

    monkeypatch.setattr(runner_mod, "InvariantEngine", BrokenEngine)
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)

    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_invariants(_args())

    assert raised.value.code == 2
    outer = json.loads(stdout.getvalue())
    inner = json.loads(outer["stdout"])
    assert outer["status"] == "BLOCKED"
    assert inner["summary"]["verdict"] == "BLOCKED"


def test_invariants_subprocess_pass_outputs_single_envelope_json(
    tmp_path: Path,
) -> None:
    _init_fixture_repo(tmp_path)

    proc = subprocess.run(
        [sys.executable, "-m", "src", "invariants"],
        cwd=tmp_path,
        env=_subprocess_env(),
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert proc.returncode == 0
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    outer = json.loads(lines[0])
    inner = json.loads(outer["stdout"])
    assert outer["status"] == "PASS"
    assert inner["summary"]["verdict"] == "PASS"


def test_invariants_subprocess_changes_requested_outputs_single_envelope_json(
    tmp_path: Path,
) -> None:
    _init_fixture_repo(tmp_path)
    (tmp_path / "src" / "engine" / "invariants.py").write_text(
        "# phase work without roadmap update\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [sys.executable, "-m", "src", "invariants"],
        cwd=tmp_path,
        env=_subprocess_env(),
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert proc.returncode == 1
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    outer = json.loads(lines[0])
    inner = json.loads(outer["stdout"])
    assert outer["status"] == "CHANGES_REQUESTED"
    assert inner["summary"]["verdict"] == "CHANGES_REQUESTED"


def test_invariants_subprocess_blocked_outputs_single_envelope_json(
    tmp_path: Path,
) -> None:
    _init_fixture_repo(tmp_path)

    proc = subprocess.run(
        [sys.executable, "-m", "src", "invariants", "--since", "missing-ref"],
        cwd=tmp_path,
        env=_subprocess_env(),
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert proc.returncode == 2
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    outer = json.loads(lines[0])
    inner = json.loads(outer["stdout"])
    assert outer["status"] == "BLOCKED"
    assert inner["checks"][0]["issues"][0]["severity"] == "blocker"


def _args(
    *,
    since: str = "HEAD",
    paths: list[str] | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(since=since, paths=paths)


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    repo_root = str(Path.cwd())
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = repo_root if not existing else f"{repo_root}{os.pathsep}{existing}"
    return env


def _engine_factory(report: InvariantReport) -> type[object]:
    class FakeEngine:
        def __init__(self, *, root: object, since: str) -> None:
            del root, since

        async def run(self, *, paths: list[str] | None) -> InvariantReport:
            del paths
            return report

    return FakeEngine


def _pass_report() -> InvariantReport:
    return InvariantReport(
        checks=[InvariantCheck(name="x", status=Verdict.PASS)],
        changed_paths=[],
    )


def _cr_report() -> InvariantReport:
    issue = InvariantIssue(
        severity="major",
        finding="ROADMAP 미갱신",
        evidence_or_repro="docs/ROADMAP_V2.md",
        impact="페이즈 기록 누락",
        recommendation="ROADMAP을 갱신하세요.",
    )
    return InvariantReport(
        checks=[
            InvariantCheck(
                name="roadmap_phase_record",
                status=Verdict.CHANGES_REQUESTED,
                issues=[issue],
            )
        ],
        changed_paths=[ChangedPath("src/engine/invariants.py", "A")],
    )
