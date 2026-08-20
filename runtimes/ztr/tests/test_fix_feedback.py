"""(ㄱ) 휴먼-게이트 fix-resume 프롬프트 빌더 테스트."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.engine.fix_feedback import (
    Finding,
    build_fix_resume_prompt,
    extract_findings,
    load_report_payload,
    select_accepted_findings,
)


def _report_with_steps(tmp_path: Path) -> dict:
    """gating mechanical(CHANGES) + non-gating autofix(CHANGES) + skipped reviewer 구성."""
    mech_stdout = tmp_path / "mech.stdout.txt"
    mech_stdout.write_text("target.py:1:1: I001 import block is un-sorted", encoding="utf-8")
    return {
        "steps": [
            {"name": "implementer", "status": "PASS", "gating": True, "skipped": False},
            {
                "name": "autofix",
                "status": "CHANGES_REQUESTED",
                "gating": False,
                "skipped": False,
                "stdout_preview": "autofix noise",
            },
            {
                "name": "mechanical-review",
                "status": "CHANGES_REQUESTED",
                "gating": True,
                "skipped": False,
                "stdout_path": str(mech_stdout),
                "stdout_preview": "preview-not-used",
                "stderr_sanitized": "",
            },
            {
                "name": "implementer-reviewer",
                "status": "BLOCKED",
                "gating": True,
                "skipped": True,
            },
        ],
        "summary": {"verdict": "CHANGES_REQUESTED", "failed_step": "mechanical-review"},
    }


def test_extract_findings_only_gating_non_pass_non_skipped(tmp_path: Path) -> None:
    report = _report_with_steps(tmp_path)

    findings = extract_findings(report)

    # non-gating autofix(verdict 없음) + skipped reviewer + PASS implementer 제외 → mechanical만.
    assert [f.leg for f in findings] == ["mechanical-review"]
    assert findings[0].status == "CHANGES_REQUESTED"
    # stdout_path 파일 본문이 우선(preview 아님).
    assert "I001 import block is un-sorted" in findings[0].text
    assert "preview-not-used" not in findings[0].text


def test_extract_findings_falls_back_to_preview_without_path(tmp_path: Path) -> None:
    report = {
        "steps": [
            {
                "name": "mechanical-review",
                "status": "CHANGES_REQUESTED",
                "gating": True,
                "skipped": False,
                "stdout_preview": "preview findings body",
            },
        ],
    }

    findings = extract_findings(report)

    assert "preview findings body" in findings[0].text


def test_build_fix_resume_prompt_combines_original_and_findings() -> None:
    findings = [Finding(leg="mechanical-review", status="CHANGES_REQUESTED", text="fix I001")]

    prompt = build_fix_resume_prompt("원본 작업 지시", findings)

    assert "원본 작업 지시" in prompt
    assert "fix-resume" in prompt
    assert "[mechanical-review] CHANGES_REQUESTED" in prompt
    assert "fix I001" in prompt
    # 사람 트리거/비-자동루프 불변이 프롬프트 본문에 명시되어야 한다.
    assert "자동 재시도 루프가 아니다" in prompt


def test_build_fix_resume_prompt_empty_findings_raises() -> None:
    try:
        build_fix_resume_prompt("원본", [])
    except ValueError as exc:
        assert "findings가 없습니다" in str(exc)
    else:
        raise AssertionError("빈 findings는 ValueError를 내야 한다")


def test_load_report_payload_accepts_envelope_and_report(tmp_path: Path) -> None:
    report = {"steps": [], "summary": {}}
    report_file = tmp_path / "report.json"
    report_file.write_text(json.dumps(report), encoding="utf-8")
    assert load_report_payload(report_file) == report

    envelope = {"status": "CHANGES_REQUESTED", "stdout": json.dumps(report)}
    envelope_file = tmp_path / "envelope.json"
    envelope_file.write_text(json.dumps(envelope), encoding="utf-8")
    assert load_report_payload(envelope_file) == report


def test_fix_prompt_cli_writes_injected_prompt(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("원본 구현 프롬프트", encoding="utf-8")
    report_file = tmp_path / "report.json"
    report_file.write_text(json.dumps(_report_with_steps(tmp_path)), encoding="utf-8")
    out = tmp_path / "fix.md"

    proc = _run_cli(["fix-prompt", "--prompt-file", str(prompt),
                     "--report-file", str(report_file), "--out", str(out)])

    assert proc.returncode == 0
    body = out.read_text(encoding="utf-8")
    assert "원본 구현 프롬프트" in body
    assert "[mechanical-review] CHANGES_REQUESTED" in body
    assert "I001 import block is un-sorted" in body


def test_fix_prompt_cli_no_findings_exits_blocked(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("원본", encoding="utf-8")
    report_file = tmp_path / "report.json"
    # 모두 PASS → 추출할 findings 없음.
    report_file.write_text(
        json.dumps({"steps": [{"name": "implementer", "status": "PASS", "gating": True}]}),
        encoding="utf-8",
    )

    proc = _run_cli(["fix-prompt", "--prompt-file", str(prompt), "--report-file", str(report_file)])

    assert proc.returncode == 2  # fail-closed: 주입 불필요
    assert "fix-resume 불필요" in proc.stderr


def test_fix_prompt_cli_bad_report_exits_internal_error(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("원본", encoding="utf-8")
    report_file = tmp_path / "report.json"
    report_file.write_text("{ not json", encoding="utf-8")

    proc = _run_cli(["fix-prompt", "--prompt-file", str(prompt), "--report-file", str(report_file)])

    assert proc.returncode == 70


def test_select_accepted_findings_preserves_report_order_and_exact_bytes() -> None:
    payload = {
        "steps": [
            {"name": "leg-b", "status": "CHANGES_REQUESTED", "gating": True,
             "skipped": False, "stdout_preview": "B\r\nbytes"},
            {"name": "ignored", "status": "BLOCKED", "gating": True,
             "skipped": False, "stdout_preview": "must-not-enter"},
            {"name": "leg-a", "status": "CHANGES_REQUESTED", "gating": True,
             "skipped": False, "stdout_preview": "A bytes"},
        ]
    }
    selected = select_accepted_findings(payload, ["leg-a", "leg-b"])
    assert [finding.leg for finding in selected] == ["leg-b", "leg-a"]
    assert [finding.text for finding in selected] == ["B\r\nbytes", "A bytes"]


@pytest.mark.parametrize("accept_legs", [[""], ["leg", "leg"], ["Leg"]])
def test_select_accepted_findings_rejects_empty_duplicate_or_case_mismatch(
    accept_legs: list[str],
) -> None:
    payload = {"steps": [{"name": "leg", "status": "CHANGES_REQUESTED",
                           "gating": True, "skipped": False}]}
    with pytest.raises(ValueError):
        select_accepted_findings(payload, accept_legs)


def _run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "src", *args],
        cwd=Path.cwd(),
        env=_subprocess_env(),
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=30,
        check=False,
    )


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    repo_root = str(Path.cwd())
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = repo_root if not existing else f"{repo_root}{os.pathsep}{existing}"
    return env
