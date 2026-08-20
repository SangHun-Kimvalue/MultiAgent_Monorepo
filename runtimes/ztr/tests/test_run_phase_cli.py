"""ztr run-phase CLI 계약 테스트."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.engine.phase_relay import RelayCommand
from src.runner import _relay_commands_from_args, _run_phase_once


async def test_run_phase_forbidden_paths_are_forwarded_to_relay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    class FakeRelay:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        async def run(self) -> object:
            return SimpleNamespace(status="unused")

    monkeypatch.setattr("src.runner.PhaseRelay", FakeRelay)
    args = Namespace(
        prompt_file=str(tmp_path / "prompt.md"),
        output_dir=str(tmp_path / "runs"),
        phase_id="f2",
        timeout=5,
        implementer_forbidden_path=["methodology/docs", "**/HANDOFF*.md"],
    )
    await _run_phase_once(
        args,
        commands=[RelayCommand(name="implementer", argv=[sys.executable, "fake.py"])],
        resume_coordinator=None,
    )
    assert captured["forbidden_paths"] == ["methodology/docs", "**/HANDOFF*.md"]


def test_run_phase_reviewer_missing_verdict_token_fails_closed(tmp_path: Path) -> None:
    """CLI 기본 --reviewer-verdict-source=stdout_token: reviewer가 토큰 없이 exit 0이면 BLOCKED."""
    prompt, fake_cli = _fixture(tmp_path)

    proc = _run_phase(
        tmp_path,
        prompt=prompt,
        implementer=[sys.executable, str(fake_cli), "pass"],
        reviewer=[sys.executable, str(fake_cli), "pass"],  # exit 0, ZTR_VERDICT 토큰 없음
    )

    assert proc.returncode == 2  # fail-closed BLOCKED
    outer = _single_envelope(proc.stdout)
    assert outer["status"] == "BLOCKED"


def test_run_phase_subprocess_pass_outputs_single_envelope_json(tmp_path: Path) -> None:
    prompt, fake_cli = _fixture(tmp_path)

    proc = _run_phase(
        tmp_path,
        prompt=prompt,
        implementer=[sys.executable, str(fake_cli), "pass"],
        reviewer=[sys.executable, str(fake_cli), "verdict_pass"],
    )

    assert proc.returncode == 0
    outer = _single_envelope(proc.stdout)
    inner = json.loads(outer["stdout"])
    assert outer["status"] == "PASS"
    assert outer["model"] == "external-cli"
    assert inner["summary"]["verdict"] == "PASS"
    assert inner["summary"]["completed"] == 2
    assert len(inner["steps"]) == 2
    assert [step["timeout_s"] for step in inner["steps"]] == [5.0, 5.0]
    assert Path(inner["steps"][0]["stdout_path"]).exists()


def test_run_phase_default_global_timeout_remains_600(tmp_path: Path) -> None:
    prompt, fake_cli = _fixture(tmp_path)

    proc = _run_phase(
        tmp_path,
        prompt=prompt,
        implementer=[sys.executable, str(fake_cli), "pass"],
        timeout=None,
    )

    assert proc.returncode == 0
    inner = json.loads(_single_envelope(proc.stdout)["stdout"])
    assert inner["steps"][0]["timeout_s"] == 600.0


def test_run_phase_subprocess_changes_requested_exit_one(tmp_path: Path) -> None:
    prompt, fake_cli = _fixture(tmp_path)

    proc = _run_phase(
        tmp_path,
        prompt=prompt,
        implementer=[sys.executable, str(fake_cli), "changes"],
        reviewer=[sys.executable, str(fake_cli), "verdict_pass"],
    )

    assert proc.returncode == 1
    outer = _single_envelope(proc.stdout)
    inner = json.loads(outer["stdout"])
    assert outer["status"] == "CHANGES_REQUESTED"
    assert inner["summary"]["failed_step"] == "implementer"
    assert inner["steps"][1]["skipped"] is True


def test_run_phase_subprocess_blocked_exit_two(tmp_path: Path) -> None:
    prompt, fake_cli = _fixture(tmp_path)

    proc = _run_phase(
        tmp_path,
        prompt=prompt,
        implementer=[sys.executable, str(fake_cli), "blocked"],
    )

    assert proc.returncode == 2
    outer = _single_envelope(proc.stdout)
    inner = json.loads(outer["stdout"])
    assert outer["status"] == "BLOCKED"
    assert inner["steps"][0]["child_exit_code"] == 2


def test_run_phase_subprocess_timeout_exit_124(tmp_path: Path) -> None:
    prompt, fake_cli = _fixture(tmp_path)

    proc = _run_phase(
        tmp_path,
        prompt=prompt,
        implementer=[sys.executable, str(fake_cli), "sleep"],
        timeout="0.2",
    )

    assert proc.returncode == 124
    outer = _single_envelope(proc.stdout)
    inner = json.loads(outer["stdout"])
    assert outer["status"] == "BLOCKED"
    assert inner["summary"]["exit_code"] == 124
    assert inner["steps"][0]["timed_out"] is True


def test_run_phase_leg_override_precedes_global_and_skips_later_leg(tmp_path: Path) -> None:
    prompt, fake_cli = _fixture(tmp_path)

    proc = _run_phase(
        tmp_path,
        prompt=prompt,
        implementer=[sys.executable, str(fake_cli), "pass"],
        test=[sys.executable, str(fake_cli), "sleep"],
        reviewer=[sys.executable, str(fake_cli), "verdict_pass"],
        timeout="2",
        timeout_overrides={"implementer": "0.5", "test": "0.1", "reviewer": "0.5"},
    )

    assert proc.returncode == 124
    inner = json.loads(_single_envelope(proc.stdout)["stdout"])
    assert [step["name"] for step in inner["steps"]] == [
        "implementer", "test", "implementer-reviewer"
    ]
    assert inner["steps"][0]["timeout_s"] == 0.5
    assert inner["steps"][1]["timed_out"] is True
    assert inner["steps"][1]["timeout_s"] == 0.1
    assert inner["steps"][2]["skipped"] is True
    assert inner["steps"][2]["timeout_s"] is None


def test_run_phase_invalid_forbidden_path_blocks_before_implementer(tmp_path: Path) -> None:
    prompt, fake_cli = _fixture(tmp_path)
    invalid_pattern = r"C:\Users\x\methodology\docs"

    proc = _run_phase(
        tmp_path,
        prompt=prompt,
        implementer=[sys.executable, str(fake_cli), "pass"],
        forbidden_paths=[invalid_pattern],
    )

    assert proc.returncode == 70
    outer = _single_envelope(proc.stdout)
    inner = json.loads(outer["stdout"])
    assert outer["status"] == "BLOCKED"
    assert repr(invalid_pattern) in outer["stderr_sanitized"]
    assert inner["steps"] == []
    assert inner["summary"]["completed"] == 0


def test_run_phase_test_leg_runs_after_other_legs_and_gates_green(tmp_path: Path) -> None:
    prompt, fake_cli = _fixture(tmp_path)

    proc = _run_phase(
        tmp_path,
        prompt=prompt,
        implementer=[sys.executable, str(fake_cli), "pass"],
        reviewer=[sys.executable, str(fake_cli), "verdict_pass"],
        test=[sys.executable, str(fake_cli), "pass"],
    )

    assert proc.returncode == 0
    outer = _single_envelope(proc.stdout)
    inner = json.loads(outer["stdout"])
    assert outer["status"] == "PASS"
    names = [step["name"] for step in inner["steps"]]
    assert names == ["implementer", "test", "implementer-reviewer"]  # reviewer는 마지막(test green 본 뒤)
    assert inner["summary"]["completed"] == 3


def test_run_phase_failing_test_leg_is_changes_requested(tmp_path: Path) -> None:
    prompt, fake_cli = _fixture(tmp_path)

    proc = _run_phase(
        tmp_path,
        prompt=prompt,
        implementer=[sys.executable, str(fake_cli), "pass"],
        test=[sys.executable, str(fake_cli), "changes"],
    )

    assert proc.returncode == 1
    outer = _single_envelope(proc.stdout)
    inner = json.loads(outer["stdout"])
    assert outer["status"] == "CHANGES_REQUESTED"
    assert inner["summary"]["failed_step"] == "test"


def test_run_phase_leg_order_is_impl_mechanical_test_reviewer(tmp_path: Path) -> None:
    prompt, fake_cli = _fixture(tmp_path)

    proc = _run_phase(
        tmp_path,
        prompt=prompt,
        implementer=[sys.executable, str(fake_cli), "pass"],
        mechanical=[sys.executable, str(fake_cli), "pass"],
        test=[sys.executable, str(fake_cli), "pass"],
        reviewer=[sys.executable, str(fake_cli), "verdict_pass"],
    )

    assert proc.returncode == 0
    outer = _single_envelope(proc.stdout)
    inner = json.loads(outer["stdout"])
    names = [step["name"] for step in inner["steps"]]
    # reviewer는 항상 마지막(mechanical/test green 본 뒤 리뷰) — gm-c2b finding 회귀 방지
    assert names == ["implementer", "mechanical-review", "test", "implementer-reviewer"]


def test_run_phase_autofix_leg_corrects_file_so_mechanical_passes(tmp_path: Path) -> None:
    """(ㄴ) autofix leg가 결함 파일을 정정 → mechanical이 정정된 파일을 보고 PASS.

    autofix가 잔여 위반으로 non-zero exit여도(ruff --fix 실측 패턴) non-gating이라 게이트하지
    않고, mechanical은 디스크에서 고쳐진 파일을 본다.
    """
    prompt, fake_cli = _fixture(tmp_path)
    target = tmp_path / "target.txt"
    target.write_text("BROKEN", encoding="utf-8")

    autofix_cli = tmp_path / "autofix.py"
    autofix_cli.write_text(
        "\n".join([
            "import sys",
            "from pathlib import Path",
            "Path(sys.argv[1]).write_text('FIXED', encoding='utf-8')",
            "raise SystemExit(1)",  # 잔여 위반으로 non-zero — non-gating 검증
        ]),
        encoding="utf-8",
    )
    mechanical_cli = tmp_path / "mechanical.py"
    mechanical_cli.write_text(
        "\n".join([
            "import sys",
            "from pathlib import Path",
            "content = Path(sys.argv[1]).read_text(encoding='utf-8')",
            "raise SystemExit(0 if content == 'FIXED' else 1)",
        ]),
        encoding="utf-8",
    )

    proc = _run_phase(
        tmp_path,
        prompt=prompt,
        implementer=[sys.executable, str(fake_cli), "pass"],
        autofix=[[sys.executable, str(autofix_cli), str(target)]],
        mechanical=[sys.executable, str(mechanical_cli), str(target)],
    )

    assert proc.returncode == 0
    outer = _single_envelope(proc.stdout)
    inner = json.loads(outer["stdout"])
    assert outer["status"] == "PASS"
    names = [step["name"] for step in inner["steps"]]
    assert names == ["implementer", "autofix", "mechanical-review"]
    autofix_step = next(step for step in inner["steps"] if step["name"] == "autofix")
    assert autofix_step["gating"] is False
    assert autofix_step["child_exit_code"] == 1  # non-zero지만 게이트 안 함
    assert inner["summary"]["failed_step"] is None
    assert target.read_text(encoding="utf-8") == "FIXED"


def test_run_phase_autofix_timeout_remains_non_gating(tmp_path: Path) -> None:
    prompt, fake_cli = _fixture(tmp_path)

    proc = _run_phase(
        tmp_path,
        prompt=prompt,
        implementer=[sys.executable, str(fake_cli), "pass"],
        autofix=[[sys.executable, str(fake_cli), "sleep"]],
        mechanical=[sys.executable, str(fake_cli), "pass"],
        timeout="2",
        timeout_overrides={"autofix": "0.1"},
    )

    assert proc.returncode == 0
    inner = json.loads(_single_envelope(proc.stdout)["stdout"])
    autofix_step = inner["steps"][1]
    assert autofix_step["gating"] is False
    assert autofix_step["timed_out"] is True
    assert autofix_step["timeout_s"] == 0.1
    assert inner["steps"][2]["skipped"] is False
    assert inner["steps"][2]["timeout_s"] == 2.0


def test_relay_commands_bind_all_leg_timeout_overrides() -> None:
    args = Namespace(
        implementer_cmd='["impl"]',
        autofix_cmd=['["fix-1"]', '["fix-2"]'],
        mechanical_cmd='["mech"]',
        test_cmd='["test"]',
        reviewer_cmd='["review"]',
        reviewer_verdict_source="stdout_token",
        implementer_timeout=1.0,
        autofix_timeout=2.0,
        mechanical_timeout=3.0,
        test_timeout=4.0,
        reviewer_timeout=5.0,
    )

    commands = _relay_commands_from_args(args)

    assert [command.name for command in commands] == [
        "implementer", "autofix", "autofix-2", "mechanical-review", "test",
        "implementer-reviewer",
    ]
    assert [command.timeout_s for command in commands] == [1.0, 2.0, 2.0, 3.0, 4.0, 5.0]


@pytest.mark.parametrize("invalid", ["0", "-1", "nan", "inf"])
def test_run_phase_invalid_leg_timeout_blocks_before_child_spawn(
    tmp_path: Path, invalid: str
) -> None:
    prompt, fake_cli = _fixture(tmp_path)

    proc = _run_phase(
        tmp_path,
        prompt=prompt,
        implementer=[sys.executable, str(fake_cli), "pass"],
        timeout_overrides={"implementer": invalid},
    )

    assert proc.returncode == 70
    outer = _single_envelope(proc.stdout)
    assert outer["status"] == "BLOCKED"
    inner = json.loads(outer["stdout"])
    assert inner["steps"] == []


def test_run_phase_invalid_override_for_absent_leg_still_blocks_before_spawn(
    tmp_path: Path,
) -> None:
    prompt, fake_cli = _fixture(tmp_path)

    proc = _run_phase(
        tmp_path,
        prompt=prompt,
        implementer=[sys.executable, str(fake_cli), "pass"],
        timeout_overrides={"reviewer": "0"},
    )

    assert proc.returncode == 70
    inner = json.loads(_single_envelope(proc.stdout)["stdout"])
    assert inner["steps"] == []


def test_run_phase_goal_intent_missing_context_blocks_before_relay_output(
    tmp_path: Path,
) -> None:
    prompt, fake_cli = _fixture(tmp_path)

    proc = _run_phase(
        tmp_path,
        prompt=prompt,
        implementer=[sys.executable, str(fake_cli), "pass"],
        context_file="missing-goal-intent-context.json",
    )

    assert proc.returncode == 70
    outer = _single_envelope(proc.stdout)
    assert outer["status"] == "BLOCKED"
    assert "GOAL_INTENT_CONTEXT_INVALID" in outer["stderr_sanitized"]
    assert not (tmp_path / "runs").exists()


def _run_phase(
    tmp_path: Path,
    *,
    prompt: Path,
    implementer: list[str],
    reviewer: list[str] | None = None,
    mechanical: list[str] | None = None,
    test: list[str] | None = None,
    autofix: list[list[str]] | None = None,
    forbidden_paths: list[str] | None = None,
    timeout: str | None = "5",
    timeout_overrides: dict[str, str] | None = None,
    context_file: str | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        sys.executable,
        "-m",
        "src",
        "run-phase",
        "--prompt-file",
        str(prompt),
        "--phase-id",
        "phase5-test",
        "--output-dir",
        str(tmp_path / "runs"),
        "--implementer-cmd",
        json.dumps(implementer),
    ]
    if timeout is not None:
        cmd.extend(["--timeout", timeout])
    if context_file is not None:
        cmd.extend(["--goal-intent-context-file", context_file])
    for leg, value in (timeout_overrides or {}).items():
        cmd.extend([f"--{leg}-timeout", value])
    for autofix_cmd in autofix or []:
        cmd.extend(["--autofix-cmd", json.dumps(autofix_cmd)])
    for forbidden_path in forbidden_paths or []:
        cmd.extend(["--implementer-forbidden-path", forbidden_path])
    if reviewer is not None:
        cmd.extend(["--reviewer-cmd", json.dumps(reviewer)])
    if mechanical is not None:
        cmd.extend(["--mechanical-cmd", json.dumps(mechanical)])
    if test is not None:
        cmd.extend(["--test-cmd", json.dumps(test)])
    return subprocess.run(
        cmd,
        cwd=Path.cwd(),
        env=_subprocess_env(),
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=30,
        check=False,
    )


def _single_envelope(stdout: str) -> dict[str, Any]:
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert isinstance(data, dict)
    return data


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("phase prompt", encoding="utf-8")
    fake_cli = tmp_path / "fake_cli.py"
    fake_cli.write_text(
        "\n".join([
            "from __future__ import annotations",
            "import sys",
            "import time",
            "mode = sys.argv[1]",
            "data = sys.stdin.read()",
            "if mode == 'sleep':",
            "    time.sleep(2)",
            "print(f'mode={mode}; stdin={len(data)}')",
            # 리뷰어는 verdict를 exit code가 아니라 stdout ZTR_VERDICT 토큰으로 신호한다.
            "if mode == 'verdict_pass':",
            "    print('ZTR_VERDICT: PASS')",
            "if mode == 'changes':",
            "    raise SystemExit(1)",
            "if mode == 'blocked':",
            "    raise SystemExit(2)",
        ]),
        encoding="utf-8",
    )
    return prompt, fake_cli


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    runtime_root = str(Path(__file__).resolve().parents[1])
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        runtime_root if not existing else f"{runtime_root}{os.pathsep}{existing}"
    )
    return env
