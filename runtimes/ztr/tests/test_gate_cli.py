"""ztr gate CLI 계약 테스트."""
from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest


async def test_cmd_gate_pass_outputs_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    result_file = tmp_path / "critic.json"
    result_file.write_text(
        json.dumps({
            "kind": "critic",
            "verdict": "conditional",
            "findings": [
                {
                    "severity": "major",
                    "finding": "SRP 위반",
                    "evidence_or_repro": "src/example.py:10",
                    "impact": "유지보수 비용 증가",
                    "recommendation": "책임을 분리하세요.",
                }
            ],
            "content": "structured critic result",
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)

    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_gate(
            argparse.Namespace(result_file=str(result_file))
        )

    assert raised.value.code == 0
    outer = json.loads(stdout.getvalue())
    inner = json.loads(outer["stdout"])
    assert outer["status"] == "PASS"
    assert inner["gate"]["passed"] is True
    assert inner["issues"] == []
    assert inner["input"]["kind"] == "critic"


async def test_cmd_gate_changes_requested_for_incomplete_m2_finding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    result_file = tmp_path / "critic_incomplete.json"
    result_file.write_text(
        json.dumps({
            "kind": "critic",
            "verdict": "conditional",
            "findings": [{"severity": "major"}],
            "content": "structured critic result",
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)

    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_gate(
            argparse.Namespace(result_file=str(result_file))
        )

    assert raised.value.code == 1
    outer = json.loads(stdout.getvalue())
    inner = json.loads(outer["stdout"])
    assert outer["status"] == "CHANGES_REQUESTED"
    assert "M2 필드" in inner["issues"][0]["finding"]


async def test_cmd_gate_changes_requested_for_writer_issue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    result_file = tmp_path / "writer.json"
    result_file.write_text(
        json.dumps({
            "kind": "writer",
            "verdict": "PASS",
            "findings": [],
            "content": "x = 1",
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)

    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_gate(
            argparse.Namespace(result_file=str(result_file))
        )

    assert raised.value.code == 1
    outer = json.loads(stdout.getvalue())
    inner = json.loads(outer["stdout"])
    assert outer["status"] == "CHANGES_REQUESTED"
    assert inner["gate"]["issues_count"] == 1
    assert set(inner["issues"][0]) == {
        "severity",
        "finding",
        "evidence_or_repro",
        "impact",
        "recommendation",
    }


async def test_cmd_gate_blocked_for_invalid_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    result_file = tmp_path / "broken.json"
    result_file.write_text("{not json", encoding="utf-8")
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)

    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_gate(
            argparse.Namespace(result_file=str(result_file))
        )

    assert raised.value.code == 2
    outer = json.loads(stdout.getvalue())
    inner = json.loads(outer["stdout"])
    assert outer["status"] == "BLOCKED"
    assert inner["gate"]["passed"] is False


async def test_cmd_gate_accepts_utf8_bom_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import runner as runner_mod

    result_file = tmp_path / "critic_bom.json"
    result_file.write_text(
        "\ufeff" + json.dumps({
            "kind": "critic",
            "verdict": "conditional",
            "findings": [
                {
                    "severity": "major",
                    "finding": "경계 설명 부족",
                    "evidence_or_repro": "docs/design.md:1",
                    "impact": "검증 근거 약화",
                    "recommendation": "근거를 추가하세요.",
                }
            ],
            "content": "structured critic result",
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)

    with pytest.raises(SystemExit) as raised:
        await runner_mod.cmd_gate(
            argparse.Namespace(result_file=str(result_file))
        )

    assert raised.value.code == 0
    outer = json.loads(stdout.getvalue())
    assert outer["status"] == "PASS"


def test_gate_subprocess_outputs_single_envelope_json(tmp_path: Path) -> None:
    result_file = tmp_path / "critic.json"
    result_file.write_text(
        json.dumps({
            "kind": "critic",
            "verdict": "conditional",
            "findings": [
                {
                    "severity": "major",
                    "finding": "경계 설명 부족",
                    "evidence_or_repro": "docs/design.md:1",
                    "impact": "검증 근거 약화",
                    "recommendation": "근거를 추가하세요.",
                }
            ],
            "content": "structured critic result",
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [sys.executable, "-m", "src", "gate", str(result_file)],
        cwd=Path.cwd(),
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
    assert outer["status"] == "PASS"
