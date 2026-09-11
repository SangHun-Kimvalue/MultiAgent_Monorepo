"""ztr run-phase CLI 계약 테스트."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from hashlib import sha256
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.engine.phase_relay import (
    PhaseRelayReport,
    RelayCommand,
    RelayStepResult,
)
from src.envelope import (
    EXIT_CODE_BY_VERDICT,
    INTERNAL_ERROR_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    Verdict,
)
from src.runner import (
    _phase_manifest_entry,
    _relay_commands_from_args,
    _run_phase_once,
    cmd_run_phase,
)


_BASE_SHA = "98aa137a9fc624c3ef21349809e5127531e42e58"

_VERDICT_MAPPING_CASES: tuple[
    tuple[str, Verdict, int, str, str | None], ...
] = (
    ("mechanical-review", Verdict.PASS, 0, "mechanical", None),
    ("test", Verdict.CHANGES_REQUESTED, 1, "test", None),
    ("implementer-reviewer", Verdict.BLOCKED, 2, "diff", None),
    (
        "mechanical-review",
        Verdict.BLOCKED,
        INTERNAL_ERROR_EXIT_CODE,
        "mechanical",
        None,
    ),
    ("test", Verdict.BLOCKED, TIMEOUT_EXIT_CODE, "test", "timeout"),
)


def _mapping_step(
    *,
    name: str,
    command: list[str],
    status: Verdict,
    exit_code: int,
    timed_out: bool = False,
    skipped: bool = False,
    envelope_path: Path | None = None,
) -> RelayStepResult:
    return RelayStepResult(
        name=name,
        command=command,
        status=status,
        exit_code=exit_code,
        child_exit_code=None,
        duration_s=0.0,
        timed_out=timed_out,
        skipped=skipped,
        envelope_path=envelope_path,
    )


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


def test_run_phase_without_phase_dir_works_when_methodology_unavailable(
    tmp_path: Path,
) -> None:
    prompt, fake_cli = _fixture(tmp_path)
    runtime_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(runtime_root)
    env["PYTHONNOUSERSITE"] = "1"

    unavailable = subprocess.run(
        [sys.executable, "-c", "import methodology"],
        cwd=runtime_root,
        env=env,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert unavailable.returncode != 0
    assert "No module named 'methodology'" in unavailable.stderr

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "src",
            "run-phase",
            "--prompt-file",
            str(prompt),
            "--phase-id",
            "phase5-disabled-projection",
            "--output-dir",
            str(tmp_path / "runs"),
            "--implementer-cmd",
            json.dumps([sys.executable, str(fake_cli), "pass"]),
            "--timeout",
            "5",
        ],
        cwd=runtime_root,
        env=env,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert proc.returncode == 0
    outer = _single_envelope(proc.stdout)
    inner = json.loads(outer["stdout"])
    assert outer["status"] == "PASS"
    assert inner["summary"]["verdict"] == "PASS"


def test_run_phase_with_phase_dir_blocks_before_relay_from_ztr_cwd(
    tmp_path: Path,
) -> None:
    prompt, fake_cli = _fixture(tmp_path)
    phase_dir = tmp_path / "phase-opt-in-unavailable"
    runtime_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(runtime_root)
    env["PYTHONNOUSERSITE"] = "1"

    proc = _run_phase_projection_cli(
        prompt=prompt,
        phase_dir=phase_dir,
        base_sha=_BASE_SHA,
        output_dir=phase_dir / "runs",
        implementer=[sys.executable, str(fake_cli), "pass"],
        cwd=runtime_root,
        env=env,
    )

    _assert_projection_blocked_two(proc)
    error = _single_envelope(proc.stdout)["stderr_sanitized"]
    assert "No module named 'methodology'" in error
    assert "저장소 루트에서 실행" in error
    assert "Python import 경로에 포함" in error
    assert not phase_dir.exists()


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


def test_run_phase_disabled_path_matches_inline_base_oracle(tmp_path: Path) -> None:
    prompt, fake_cli = _fixture(tmp_path)
    implementer = [sys.executable, str(fake_cli), "pass"]
    mechanical = [sys.executable, str(fake_cli), "pass"]
    test = [sys.executable, str(fake_cli), "pass"]
    reviewer = [sys.executable, str(fake_cli), "verdict_pass"]
    command_args = Namespace(
        implementer_cmd=json.dumps(implementer),
        autofix_cmd=None,
        mechanical_cmd=json.dumps(mechanical),
        test_cmd=json.dumps(test),
        reviewer_cmd=json.dumps(reviewer),
        reviewer_verdict_source="stdout_token",
        implementer_timeout=None,
        autofix_timeout=None,
        mechanical_timeout=None,
        test_timeout=None,
        reviewer_timeout=None,
    )

    commands = _relay_commands_from_args(command_args)
    assert [
        (command.name, command.argv, command.gating, command.timeout_s)
        for command in commands
    ] == [
        ("implementer", implementer, True, None),
        ("mechanical-review", mechanical, True, None),
        ("test", test, True, None),
        ("implementer-reviewer", reviewer, True, None),
    ]

    proc = _run_phase_projection_cli(
        prompt=prompt,
        output_dir=tmp_path / "runs",
        implementer=implementer,
        mechanical=mechanical,
        test=test,
        reviewer=reviewer,
    )

    outer = _single_envelope(proc.stdout)
    inner = json.loads(outer["stdout"])
    assert {
        key: outer[key]
        for key in (
            "status",
            "exit_code",
            "backend",
            "model",
            "fallback_used",
            "not_claimed",
        )
    } == {
        "status": "PASS",
        "exit_code": 0,
        "backend": "phase-relay",
        "model": "external-cli",
        "fallback_used": False,
        "not_claimed": ["full-e2e"],
    }
    assert proc.returncode == 0
    assert [
        (
            step["name"],
            step["status"],
            step["exit_code"],
            step["child_exit_code"],
            step["timed_out"],
            step["gating"],
            step["skipped"],
        )
        for step in inner["steps"]
    ] == [
        ("implementer", "PASS", 0, 0, False, True, False),
        ("mechanical-review", "PASS", 0, 0, False, True, False),
        ("test", "PASS", 0, 0, False, True, False),
        ("implementer-reviewer", "PASS", 0, 0, False, True, False),
    ]
    assert list(tmp_path.rglob("phase-manifest.json")) == []
    assert list(tmp_path.rglob("PHASE_REPORT.md")) == []


def test_run_phase_opt_in_projects_closed_mapping_and_report(tmp_path: Path) -> None:
    prompt, fake_cli = _fixture(tmp_path)
    phase_dir = tmp_path / "phase-opt-in"
    proc = _run_phase_projection_cli(
        prompt=prompt,
        phase_dir=phase_dir,
        base_sha=_BASE_SHA,
        output_dir=phase_dir / "runs",
        implementer=[sys.executable, str(fake_cli), "pass"],
        mechanical=[sys.executable, str(fake_cli), "pass"],
        test=[sys.executable, str(fake_cli), "pass"],
        reviewer=[sys.executable, str(fake_cli), "verdict_pass"],
    )

    assert proc.returncode == 0
    outer = _single_envelope(proc.stdout)
    assert outer["status"] == "PASS"
    steps = json.loads(outer["stdout"])["steps"]
    manifest_path = phase_dir / "phase-manifest.json"
    report_path = phase_dir / "PHASE_REPORT.md"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest["entries"]
    assert manifest["base_sha"] == _BASE_SHA
    assert len(entries) == 4
    assert entries[0]["kind"] == "command"
    assert entries[0]["argv"] == steps[0]["command"]

    expected_subjects = ["mechanical", "test", "diff"]
    for index, (entry, step, subject) in enumerate(
        zip(entries[1:], steps[1:], expected_subjects, strict=True),
        start=1,
    ):
        artifact_path = Path(step["envelope_path"])
        assert {
            "kind": entry["kind"],
            "verdict": entry["verdict"],
            "exit_code": entry["exit_code"],
            "artifact_ref": entry["artifact_ref"],
            "content_sha256": entry["content_sha256"],
            "attempt_id": entry["attempt_id"],
            "stage": entry["stage"],
            "subject": entry["subject"],
        } == {
            "kind": "verdict",
            "verdict": step["status"],
            "exit_code": step["exit_code"],
            "artifact_ref": artifact_path.resolve()
            .relative_to(phase_dir.resolve())
            .as_posix(),
            "content_sha256": sha256(artifact_path.read_bytes()).hexdigest(),
            "attempt_id": f"{step['name']}-{index}",
            "stage": "implementation_review",
            "subject": subject,
        }
    assert report_path.is_file()
    assert all(entry.get("stage") != "final_verification" for entry in entries)


def test_run_phase_opt_in_records_skipped_reviewer_after_changes_requested(
    tmp_path: Path,
) -> None:
    prompt, fake_cli = _fixture(tmp_path)
    phase_dir = tmp_path / "phase-changes-requested"
    proc = _run_phase_projection_cli(
        prompt=prompt,
        phase_dir=phase_dir,
        base_sha=_BASE_SHA,
        output_dir=phase_dir / "runs",
        implementer=[sys.executable, str(fake_cli), "pass"],
        mechanical=[sys.executable, str(fake_cli), "pass"],
        test=[sys.executable, str(fake_cli), "changes"],
        reviewer=[sys.executable, str(fake_cli), "verdict_pass"],
    )

    assert proc.returncode == 1
    outer = _single_envelope(proc.stdout)
    assert outer["status"] == "CHANGES_REQUESTED"
    steps = json.loads(outer["stdout"])["steps"]
    assert [
        (step["name"], step["status"], step["exit_code"], step["skipped"])
        for step in steps
    ] == [
        ("implementer", "PASS", 0, False),
        ("mechanical-review", "PASS", 0, False),
        ("test", "CHANGES_REQUESTED", 1, False),
        ("implementer-reviewer", "BLOCKED", 2, True),
    ]
    entries = json.loads(
        (phase_dir / "phase-manifest.json").read_text(encoding="utf-8")
    )["entries"]
    assert [
        (entry["kind"], entry.get("verdict"), entry.get("exit_code"))
        for entry in entries[:3]
    ] == [
        ("command", None, None),
        ("verdict", "PASS", 0),
        ("verdict", "CHANGES_REQUESTED", 1),
    ]
    assert {
        "kind": entries[3]["kind"],
        "name": entries[3]["name"],
        "result": entries[3]["result"],
    } == {
        "kind": "validation_fact",
        "name": "implementer-reviewer",
        "result": "SKIPPED",
    }


def test_run_phase_opt_in_records_two_skipped_legs_after_blocked(
    tmp_path: Path,
) -> None:
    prompt, fake_cli = _fixture(tmp_path)
    phase_dir = tmp_path / "phase-blocked"
    proc = _run_phase_projection_cli(
        prompt=prompt,
        phase_dir=phase_dir,
        base_sha=_BASE_SHA,
        output_dir=phase_dir / "runs",
        implementer=[sys.executable, str(fake_cli), "pass"],
        mechanical=[sys.executable, str(fake_cli), "blocked"],
        test=[sys.executable, str(fake_cli), "pass"],
        reviewer=[sys.executable, str(fake_cli), "verdict_pass"],
    )

    assert proc.returncode == 2
    outer = _single_envelope(proc.stdout)
    assert outer["status"] == "BLOCKED"
    steps = json.loads(outer["stdout"])["steps"]
    assert [
        (step["name"], step["status"], step["exit_code"], step["skipped"])
        for step in steps
    ] == [
        ("implementer", "PASS", 0, False),
        ("mechanical-review", "BLOCKED", 2, False),
        ("test", "BLOCKED", 2, True),
        ("implementer-reviewer", "BLOCKED", 2, True),
    ]
    entries = json.loads(
        (phase_dir / "phase-manifest.json").read_text(encoding="utf-8")
    )["entries"]
    assert [
        (entry["kind"], entry.get("verdict"), entry.get("exit_code"))
        for entry in entries[:2]
    ] == [
        ("command", None, None),
        ("verdict", "BLOCKED", 2),
    ]
    assert [
        (entry["kind"], entry["name"], entry["result"])
        for entry in entries[2:]
    ] == [
        ("validation_fact", "test", "SKIPPED"),
        ("validation_fact", "implementer-reviewer", "SKIPPED"),
    ]
def test_run_phase_opt_in_projects_two_autofix_commands_in_order(
    tmp_path: Path,
) -> None:
    prompt, fake_cli = _fixture(tmp_path)
    phase_dir = tmp_path / "phase-autofix"
    commands = [
        [sys.executable, str(fake_cli), "pass"],
        [sys.executable, str(fake_cli), "pass", "fix-1"],
        [sys.executable, str(fake_cli), "pass", "fix-2"],
    ]
    proc = _run_phase_projection_cli(
        prompt=prompt,
        phase_dir=phase_dir,
        base_sha=_BASE_SHA,
        output_dir=phase_dir / "runs",
        implementer=commands[0],
        autofix=commands[1:],
    )

    assert proc.returncode == 0
    steps = json.loads(_single_envelope(proc.stdout)["stdout"])["steps"]
    entries = json.loads(
        (phase_dir / "phase-manifest.json").read_text(encoding="utf-8")
    )["entries"]
    assert [step["name"] for step in steps] == [
        "implementer", "autofix", "autofix-2"
    ]
    assert [(entry["kind"], entry["argv"]) for entry in entries] == [
        ("command", command) for command in commands
    ]


def test_phase_manifest_verdict_matrix_covers_all_relay_exit_codes() -> None:
    tested_exit_codes = {case[2] for case in _VERDICT_MAPPING_CASES}
    source_exit_codes = {
        *EXIT_CODE_BY_VERDICT.values(),
        INTERNAL_ERROR_EXIT_CODE,
        TIMEOUT_EXIT_CODE,
    }
    assert tested_exit_codes == source_exit_codes


@pytest.mark.parametrize(
    ("name", "command"),
    [
        ("implementer", ["impl"]),
        ("autofix", ["fix", "one"]),
        ("autofix-2", ["fix", "two"]),
    ],
)
def test_phase_manifest_command_branch_preserves_argv_only(
    tmp_path: Path,
    name: str,
    command: list[str],
) -> None:
    step = _mapping_step(name=name, command=command, status=Verdict.PASS, exit_code=0)

    assert _phase_manifest_entry(step, 0, tmp_path) == {
        "kind": "command",
        "argv": command,
    }


@pytest.mark.parametrize(
    ("name", "status", "relay_exit_code", "subject", "failure_reason"),
    _VERDICT_MAPPING_CASES,
)
def test_phase_manifest_verdict_branch_uses_canonical_exit_code(
    tmp_path: Path,
    name: str,
    status: Verdict,
    relay_exit_code: int,
    subject: str,
    failure_reason: str | None,
) -> None:
    envelope_path = tmp_path / f"{name}-{relay_exit_code}.json"
    envelope_path.write_text("{}", encoding="utf-8")
    step = _mapping_step(
        name=name,
        command=[name],
        status=status,
        exit_code=relay_exit_code,
        timed_out=relay_exit_code == TIMEOUT_EXIT_CODE,
        envelope_path=envelope_path,
    )

    entry = _phase_manifest_entry(step, 3, tmp_path)

    assert entry["kind"] == "verdict"
    assert entry["verdict"] == status.value
    assert entry["exit_code"] == EXIT_CODE_BY_VERDICT[status]
    assert entry["subject"] == subject
    assert entry.get("failure_reason") == failure_reason
    assert ("failure_reason" in entry) == (failure_reason is not None)


@pytest.mark.parametrize(
    ("step", "expected"),
    [
        (
            _mapping_step(
                name="implementer-scope-guard",
                command=[],
                status=Verdict.BLOCKED,
                exit_code=2,
            ),
            {
                "kind": "validation_fact",
                "name": "implementer-scope-guard",
                "result": "BLOCKED",
            },
        ),
        (
            _mapping_step(
                name="test",
                command=["test"],
                status=Verdict.BLOCKED,
                exit_code=2,
                skipped=True,
            ),
            {"kind": "validation_fact", "name": "test", "result": "SKIPPED"},
        ),
    ],
)
def test_phase_manifest_envelope_less_branches_are_validation_facts(
    tmp_path: Path,
    step: RelayStepResult,
    expected: dict[str, Any],
) -> None:
    assert _phase_manifest_entry(step, 0, tmp_path) == expected


def test_run_phase_opt_in_projects_actual_scope_guard_and_preserves_blocked(
    tmp_path: Path,
) -> None:
    prompt, _ = _fixture(tmp_path)
    fake_implementer = _write_file_implementer(tmp_path)
    repo_root = Path(__file__).resolve().parents[3]
    base_probe = repo_root / f"scope-guard-base-{tmp_path.name}.txt"
    opt_in_probe = repo_root / f"scope-guard-opt-in-{tmp_path.name}.txt"
    phase_dir = tmp_path / "scope-guard-phase"
    for probe in (base_probe, opt_in_probe):
        assert not probe.exists()

    try:
        base = _run_phase_projection_cli(
            prompt=prompt,
            output_dir=tmp_path / "scope-guard-base-runs",
            implementer=[sys.executable, str(fake_implementer), str(base_probe)],
            forbidden_paths=[base_probe.relative_to(repo_root).as_posix()],
        )
        opt_in = _run_phase_projection_cli(
            prompt=prompt,
            phase_dir=phase_dir,
            base_sha=_BASE_SHA,
            output_dir=phase_dir / "runs",
            implementer=[sys.executable, str(fake_implementer), str(opt_in_probe)],
            forbidden_paths=[opt_in_probe.relative_to(repo_root).as_posix()],
        )
    finally:
        base_probe.unlink(missing_ok=True)
        opt_in_probe.unlink(missing_ok=True)

    base_outer = _single_envelope(base.stdout)
    opt_in_outer = _single_envelope(opt_in.stdout)
    assert (base.returncode, base_outer["status"], base_outer["exit_code"]) == (
        2,
        "BLOCKED",
        2,
    )
    assert (
        opt_in.returncode,
        opt_in_outer["status"],
        opt_in_outer["exit_code"],
    ) == (base.returncode, base_outer["status"], base_outer["exit_code"])
    steps = json.loads(opt_in_outer["stdout"])["steps"]
    assert [step["name"] for step in steps] == [
        "implementer-scope-guard",
        "implementer",
    ]
    entries = _phase_entries(phase_dir)
    assert {
        "kind": entries[0]["kind"],
        "name": entries[0]["name"],
        "result": entries[0]["result"],
    } == {
        "kind": "validation_fact",
        "name": "implementer-scope-guard",
        "result": "BLOCKED",
    }
    assert entries[1]["kind"] == "command"


def test_run_phase_opt_in_projects_timeout_with_canonical_blocked_exit(
    tmp_path: Path,
) -> None:
    prompt, fake_cli = _fixture(tmp_path)
    phase_dir = tmp_path / "timeout-phase"
    commands = {
        "implementer": [sys.executable, str(fake_cli), "pass"],
        "test": [sys.executable, str(fake_cli), "sleep"],
    }
    base = _run_phase_projection_cli(
        prompt=prompt,
        output_dir=tmp_path / "timeout-base-runs",
        implementer=commands["implementer"],
        test=commands["test"],
        timeout_overrides={"test": "0.1"},
    )
    opt_in = _run_phase_projection_cli(
        prompt=prompt,
        phase_dir=phase_dir,
        base_sha=_BASE_SHA,
        output_dir=phase_dir / "runs",
        implementer=commands["implementer"],
        test=commands["test"],
        timeout_overrides={"test": "0.1"},
    )

    base_outer = _single_envelope(base.stdout)
    opt_in_outer = _single_envelope(opt_in.stdout)
    assert (base.returncode, base_outer["status"], base_outer["exit_code"]) == (
        TIMEOUT_EXIT_CODE,
        "BLOCKED",
        TIMEOUT_EXIT_CODE,
    )
    assert (
        opt_in.returncode,
        opt_in_outer["status"],
        opt_in_outer["exit_code"],
    ) == (base.returncode, base_outer["status"], base_outer["exit_code"])
    timeout_entry = _phase_entries(phase_dir)[1]
    assert (
        timeout_entry["kind"],
        timeout_entry["verdict"],
        timeout_entry["exit_code"],
        timeout_entry["failure_reason"],
    ) == ("verdict", "BLOCKED", 2, "timeout")


def test_run_phase_opt_in_projects_internal_error_without_failure_reason(
    tmp_path: Path,
) -> None:
    prompt, fake_cli = _fixture(tmp_path)
    phase_dir = tmp_path / "internal-error-phase"
    proc = _run_phase_projection_cli(
        prompt=prompt,
        phase_dir=phase_dir,
        base_sha=_BASE_SHA,
        output_dir=phase_dir / "runs",
        implementer=[sys.executable, str(fake_cli), "pass"],
        test=["__missing_ztr_t10_o4a_s2_executable__"],
    )

    outer = _single_envelope(proc.stdout)
    assert (proc.returncode, outer["status"], outer["exit_code"]) == (
        INTERNAL_ERROR_EXIT_CODE,
        "BLOCKED",
        INTERNAL_ERROR_EXIT_CODE,
    )
    internal_error_entry = _phase_entries(phase_dir)[1]
    assert (
        internal_error_entry["kind"],
        internal_error_entry["verdict"],
        internal_error_entry["exit_code"],
    ) == ("verdict", "BLOCKED", 2)
    assert "failure_reason" not in internal_error_entry


def test_run_phase_opt_in_full_pass_entry_contract_is_unchanged(
    tmp_path: Path,
) -> None:
    prompt, fake_cli = _fixture(tmp_path)
    phase_dir = tmp_path / "full-pass-regression"
    proc = _run_phase_projection_cli(
        prompt=prompt,
        phase_dir=phase_dir,
        base_sha=_BASE_SHA,
        output_dir=phase_dir / "runs",
        implementer=[sys.executable, str(fake_cli), "pass"],
        mechanical=[sys.executable, str(fake_cli), "pass"],
        test=[sys.executable, str(fake_cli), "pass"],
        reviewer=[sys.executable, str(fake_cli), "verdict_pass"],
    )

    assert proc.returncode == 0
    assert [
        (entry["kind"], entry.get("verdict"), entry.get("exit_code"))
        for entry in _phase_entries(phase_dir)
    ] == [
        ("command", None, None),
        ("verdict", "PASS", 0),
        ("verdict", "PASS", 0),
        ("verdict", "PASS", 0),
    ]


def test_run_phase_opt_in_missing_base_sha_is_blocked_two(tmp_path: Path) -> None:
    prompt, fake_cli = _fixture(tmp_path)
    phase_dir = tmp_path / "missing-base"
    proc = _run_phase_projection_cli(
        prompt=prompt,
        phase_dir=phase_dir,
        output_dir=phase_dir / "runs",
        implementer=[sys.executable, str(fake_cli), "pass"],
    )

    _assert_projection_blocked_two(proc)
    assert not (phase_dir / "runs").exists()


@pytest.mark.parametrize("location", ["outside", "inside"])
def test_run_phase_opt_in_wrong_output_dir_is_blocked_before_relay(
    tmp_path: Path,
    location: str,
) -> None:
    prompt, fake_cli = _fixture(tmp_path)
    phase_dir = tmp_path / f"wrong-output-{location}"
    output_dir = (
        tmp_path / "outside-runs"
        if location == "outside"
        else phase_dir / "other-runs"
    )

    proc = _run_phase_projection_cli(
        prompt=prompt,
        phase_dir=phase_dir,
        base_sha=_BASE_SHA,
        output_dir=output_dir,
        implementer=[sys.executable, str(fake_cli), "pass"],
    )

    _assert_projection_blocked_two(proc)
    assert not output_dir.exists()


@pytest.mark.asyncio
async def test_run_phase_unknown_step_is_blocked_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prompt, _ = _fixture(tmp_path)
    phase_dir = tmp_path / "unknown-step"
    args = _projection_namespace(prompt, phase_dir)
    unknown_report = PhaseRelayReport(
        phase_id=args.phase_id,
        prompt_path=prompt,
        run_dir=phase_dir / "runs" / "fake-run",
        steps=[
            RelayStepResult(
                name="unexpected-step",
                command=["fake"],
                status=Verdict.PASS,
                exit_code=0,
                child_exit_code=0,
                duration_s=0.0,
                timed_out=False,
                skipped=False,
            )
        ],
    )

    async def fake_run_phase_once(*_args: object, **_kwargs: object) -> PhaseRelayReport:
        return unknown_report

    monkeypatch.setattr("src.runner._run_phase_once", fake_run_phase_once)
    with pytest.raises(SystemExit) as raised:
        await cmd_run_phase(args)

    assert raised.value.code == 2
    outer = _single_envelope(capsys.readouterr().out)
    assert outer["status"] == "BLOCKED"
    assert outer["exit_code"] == 2
    assert json.loads(outer["stdout"])["summary"]["exit_code"] == 2


@pytest.mark.asyncio
async def test_run_phase_projection_does_not_call_promote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from methodology.phase import report as phase_report

    prompt, _ = _fixture(tmp_path)
    phase_dir = tmp_path / "no-promote"
    args = _projection_namespace(prompt, phase_dir)
    relay_report = PhaseRelayReport(
        phase_id=args.phase_id,
        prompt_path=prompt,
        run_dir=phase_dir / "runs" / "fake-run",
        steps=[
            RelayStepResult(
                name="implementer",
                command=["fake"],
                status=Verdict.PASS,
                exit_code=0,
                child_exit_code=0,
                duration_s=0.0,
                timed_out=False,
                skipped=False,
            )
        ],
    )
    promote_called = False

    async def fake_run_phase_once(*_args: object, **_kwargs: object) -> PhaseRelayReport:
        return relay_report

    def forbidden_promote(*_args: object, **_kwargs: object) -> int:
        nonlocal promote_called
        promote_called = True
        raise AssertionError("promote must not be called")

    monkeypatch.setattr("src.runner._run_phase_once", fake_run_phase_once)
    monkeypatch.setattr(phase_report, "promote", forbidden_promote)
    with pytest.raises(SystemExit) as raised:
        await cmd_run_phase(args)

    assert raised.value.code == 0
    assert _single_envelope(capsys.readouterr().out)["status"] == "PASS"
    assert not promote_called
    assert (phase_dir / "PHASE_REPORT.md").is_file()


@pytest.mark.parametrize(
    ("operation", "module_name"),
    [
        ("init_manifest", "methodology.phase.manifest"),
        ("append_entry", "methodology.phase.manifest"),
        ("project_report", "methodology.phase.report"),
    ],
)
@pytest.mark.parametrize("failure_mode", ["nonzero", "exception", "missing_output"])
@pytest.mark.asyncio
async def test_run_phase_projection_api_failure_blocks_relay_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operation: str,
    module_name: str,
    failure_mode: str,
) -> None:
    module = __import__(module_name, fromlist=[operation])
    prompt, _ = _fixture(tmp_path)
    phase_dir = tmp_path / f"{operation}-{failure_mode}"

    def injected_failure(*_args: object, **_kwargs: object) -> int:
        if failure_mode == "exception":
            raise RuntimeError(f"injected {operation} exception")
        return 1 if failure_mode == "nonzero" else 0

    monkeypatch.setattr(module, operation, injected_failure)

    process_exit_code, outer = await _run_projection_with_pass_relay(
        _projection_namespace(prompt, phase_dir),
        prompt,
        phase_dir,
        monkeypatch,
        capsys,
    )
    _assert_relay_pass_is_blocked(process_exit_code, outer)


@pytest.mark.parametrize("existing_name", ["PHASE_REPORT.md"])
@pytest.mark.asyncio
async def test_run_phase_projection_blocks_preexisting_output_before_init(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    existing_name: str,
) -> None:
    from methodology.phase import manifest as phase_manifest

    prompt, _ = _fixture(tmp_path)
    phase_dir = tmp_path / f"preexisting-{existing_name}"
    phase_dir.mkdir()
    (phase_dir / existing_name).write_text("pre-existing\n", encoding="utf-8")
    init_called = False

    def tracked_init(*_args: object) -> int:
        nonlocal init_called
        init_called = True
        return 0

    monkeypatch.setattr(phase_manifest, "init_manifest", tracked_init)

    process_exit_code, outer = await _run_projection_with_pass_relay(
        _projection_namespace(prompt, phase_dir),
        prompt,
        phase_dir,
        monkeypatch,
        capsys,
    )

    _assert_relay_pass_is_blocked(process_exit_code, outer)
    assert not init_called


async def _run_projection_with_pass_relay(
    args: Namespace,
    prompt: Path,
    phase_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, dict[str, Any]]:
    relay_report = PhaseRelayReport(
        phase_id=args.phase_id,
        prompt_path=prompt,
        run_dir=phase_dir / "runs" / "fake-run",
        steps=[
            RelayStepResult(
                name="implementer",
                command=["fake"],
                status=Verdict.PASS,
                exit_code=0,
                child_exit_code=0,
                duration_s=0.0,
                timed_out=False,
                skipped=False,
            )
        ],
    )

    async def fake_run_phase_once(*_args: object, **_kwargs: object) -> PhaseRelayReport:
        return relay_report

    monkeypatch.setattr("src.runner._run_phase_once", fake_run_phase_once)
    with pytest.raises(SystemExit) as raised:
        await cmd_run_phase(args)

    return int(raised.value.code), _single_envelope(capsys.readouterr().out)


def _assert_relay_pass_is_blocked(
    process_exit_code: int, outer: dict[str, Any]
) -> None:
    inner = json.loads(outer["stdout"])
    assert process_exit_code == outer["exit_code"] == 2
    assert outer["status"] == inner["summary"]["verdict"] == "BLOCKED"
    assert inner["summary"]["exit_code"] == 2
    assert inner["steps"] == []


def _run_phase_projection_cli(
    *,
    prompt: Path,
    output_dir: Path,
    implementer: list[str],
    phase_dir: Path | None = None,
    base_sha: str | None = None,
    autofix: list[list[str]] | None = None,
    mechanical: list[str] | None = None,
    test: list[str] | None = None,
    reviewer: list[str] | None = None,
    forbidden_paths: list[str] | None = None,
    timeout_overrides: dict[str, str] | None = None,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        sys.executable,
        "-m",
        "src",
        "run-phase",
        "--prompt-file",
        str(prompt),
        "--phase-id",
        "phase-projection-test",
        "--output-dir",
        str(output_dir),
        "--implementer-cmd",
        json.dumps(implementer),
        "--timeout",
        "10",
    ]
    if phase_dir is not None:
        cmd.extend(["--phase-dir", str(phase_dir)])
    if base_sha is not None:
        cmd.extend(["--base-sha", base_sha])
    for autofix_cmd in autofix or []:
        cmd.extend(["--autofix-cmd", json.dumps(autofix_cmd)])
    if mechanical is not None:
        cmd.extend(["--mechanical-cmd", json.dumps(mechanical)])
    if test is not None:
        cmd.extend(["--test-cmd", json.dumps(test)])
    if reviewer is not None:
        cmd.extend(["--reviewer-cmd", json.dumps(reviewer)])
    for forbidden_path in forbidden_paths or []:
        cmd.extend(["--implementer-forbidden-path", forbidden_path])
    for leg, value in (timeout_overrides or {}).items():
        cmd.extend([f"--{leg}-timeout", value])
    return subprocess.run(
        cmd,
        cwd=cwd or Path.cwd(),
        env=env or _subprocess_env(),
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=60,
        check=False,
    )

def _phase_entries(phase_dir: Path) -> list[dict[str, Any]]:
    manifest = json.loads(
        (phase_dir / "phase-manifest.json").read_text(encoding="utf-8")
    )
    entries = manifest["entries"]
    assert isinstance(entries, list)
    return entries


def _write_file_implementer(tmp_path: Path) -> Path:
    script = tmp_path / "write_file_implementer.py"
    script.write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "import sys",
                "sys.stdin.read()",
                "Path(sys.argv[1]).write_text('scope violation', encoding='utf-8')",
            ]
        ),
        encoding="utf-8",
    )
    return script


def _projection_namespace(prompt: Path, phase_dir: Path) -> Namespace:
    return Namespace(
        config=None,
        phase_dir=str(phase_dir),
        base_sha=_BASE_SHA,
        output_dir=str(phase_dir / "runs"),
        phase_id="phase-projection-test",
        prompt_file=str(prompt),
        goal_intent_context_file=None,
        implementer_cmd='["fake"]',
        autofix_cmd=None,
        mechanical_cmd="",
        test_cmd="",
        reviewer_cmd="",
        reviewer_verdict_source="stdout_token",
        implementer_timeout=None,
        autofix_timeout=None,
        mechanical_timeout=None,
        test_timeout=None,
        reviewer_timeout=None,
        session_map="",
        implementer_resume="new",
        reviewer_resume="new",
        implementer_resume_profile="none",
        reviewer_resume_profile="none",
        record=False,
        implementer_forbidden_path=None,
        timeout=10.0,
    )


def _assert_projection_blocked_two(proc: subprocess.CompletedProcess[str]) -> None:
    assert proc.returncode == 2
    outer = _single_envelope(proc.stdout)
    assert outer["status"] == "BLOCKED"
    assert outer["exit_code"] == 2
    assert json.loads(outer["stdout"])["summary"]["exit_code"] == 2


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
