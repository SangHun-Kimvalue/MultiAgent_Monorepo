from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
REPO_ROOT = Path(__file__).resolve().parents[2]
ZTR_ROOT = REPO_ROOT / "runtimes" / "ztr"
ZTR_PYTHON = ZTR_ROOT / ".venv" / "Scripts" / "python.exe"
ACTOR = Path(__file__).resolve().parent / "fixtures" / "remediation_e2e_actor.py"
sys.path.insert(0, str(TOOLS_DIR))

import remediation_adapter as adapter  # noqa: E402


def _finding(
    finding_id: str,
    disposition: str = "ACCEPT",
    *,
    corrective_round: str | None = "round-1",
    fix_instruction: str | None = "Replace BAD_ACCEPTED with FIXED_ACCEPTED.",
) -> dict[str, Any]:
    return {
        "id": finding_id,
        "reviewer": "fixture-reviewer",
        "severity": "medium",
        "finding": f"{finding_id}_BODY",
        "evidence_or_repro": f"{finding_id}_REPRO",
        "impact": f"{finding_id}_IMPACT",
        "recommendation": f"{finding_id}_RECOMMENDATION",
        "disposition": disposition,
        "disposition_evidence": f"{finding_id}_DISPOSITION_EVIDENCE",
        "rationale": f"{finding_id}_RATIONALE",
        "owner": "separate-implementer",
        "corrective_round": corrective_round,
        "fix_instruction": fix_instruction,
    }


def _artifact(findings: list[dict[str, Any]], *, approved: bool = True) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "phase_id": "p3-test",
        "human_trigger": {
            "approved": approved,
            "actor": "test-human",
            "approved_at": "2026-07-20T12:00:00+09:00",
            "scope": "round-1 accepted-only",
            "evidence_ref": "chat/test/continue-1",
        },
        "findings": findings,
    }


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _invoke(
    repo_root: Path,
    artifact: dict[str, Any],
    *,
    extra: list[str] | None = None,
    output_name: str = "report.json",
    replace_file: adapter.ReplaceFile = os.replace,
) -> tuple[dict[str, Any], int, Path, Path]:
    disposition = repo_root / "disposition.json"
    output = repo_root / output_name
    _write_json(disposition, artifact)
    argv = [
        "--disposition",
        str(disposition),
        "--out-report",
        str(output),
        "--human-triggered",
        *(extra or []),
    ]
    payload, code = adapter.execute(argv, repo_root=repo_root, replace_file=replace_file)
    return payload, code, disposition, output


def _mixed_findings() -> list[dict[str, Any]]:
    return [
        _finding("ACCEPTED"),
        _finding(
            "REJECTED",
            "REJECT_FALSE_POSITIVE",
            corrective_round=None,
            fix_instruction=None,
        ),
        _finding(
            "DEFERRED",
            "DEFER_OUT_OF_SCOPE",
            corrective_round=None,
            fix_instruction=None,
        ),
        _finding(
            "OVERENGINEERED",
            "REJECT_OVERENGINEERING",
            corrective_round=None,
            fix_instruction=None,
        ),
    ]


def test_mixed_dispositions_emit_only_accepted_compatible_report(tmp_path: Path) -> None:
    payload, code, _, output = _invoke(tmp_path, _artifact(_mixed_findings()))

    assert code == 0
    assert payload["status"] == "PASS"
    assert payload["accepted_ids"] == ["ACCEPTED"]
    assert payload["accepted_count"] == 1
    assert payload["mutations_performed"] == ["only_out_report"]
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"] == {
        "verdict": "CHANGES_REQUESTED",
        "failed_step": "orchestrator-accepted-review",
    }
    assert report["steps"][0]["name"] == "orchestrator-accepted-review"
    assert report["steps"][0]["status"] == "CHANGES_REQUESTED"
    assert report["steps"][0]["gating"] is True
    assert report["steps"][0]["skipped"] is False


def test_non_accepted_ids_bodies_and_recommendations_are_byte_absent(tmp_path: Path) -> None:
    _, code, _, output = _invoke(tmp_path, _artifact(_mixed_findings()))

    assert code == 0
    raw = output.read_bytes()
    for forbidden in (
        b"REJECTED",
        b"REJECTED_BODY",
        b"REJECTED_RECOMMENDATION",
        b"DEFERRED",
        b"DEFERRED_BODY",
        b"DEFERRED_RECOMMENDATION",
        b"OVERENGINEERED",
        b"OVERENGINEERED_BODY",
        b"OVERENGINEERED_RECOMMENDATION",
    ):
        assert forbidden not in raw


@pytest.mark.parametrize(
    ("approved", "include_flag"),
    [(True, False), (False, True)],
)
def test_both_human_markers_are_required_before_any_write(
    tmp_path: Path, approved: bool, include_flag: bool
) -> None:
    disposition = tmp_path / "disposition.json"
    output = tmp_path / "report.json"
    _write_json(disposition, _artifact([_finding("A")], approved=approved))
    replace_calls = 0

    def unexpected_replace(source: str, target: str) -> None:
        del source, target
        nonlocal replace_calls
        replace_calls += 1

    argv = ["--disposition", str(disposition), "--out-report", str(output)]
    if include_flag:
        argv.append("--human-triggered")
    payload, code = adapter.execute(argv, repo_root=tmp_path, replace_file=unexpected_replace)

    assert code == 2
    assert payload["status"] == "BLOCKED"
    assert payload["mutations_performed"] == []
    assert replace_calls == 0
    assert not output.exists()


def _remove_nested(value: dict[str, Any], dotted: str) -> None:
    parent: dict[str, Any] = value
    parts = dotted.split(".")
    for part in parts[:-1]:
        next_value = parent[part]
        assert isinstance(next_value, dict)
        parent = next_value
    del parent[parts[-1]]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["findings"][0].pop("disposition"),
        lambda value: value["findings"][0].pop("disposition_evidence"),
        lambda value: value["findings"][0].pop("owner"),
        lambda value: value["findings"][0].update(disposition="INVALID"),
        lambda value: value["findings"].append(copy.deepcopy(value["findings"][0])),
        lambda value: value["findings"][0].update(corrective_round=None),
        lambda value: value["findings"][0].update(fix_instruction=""),
        lambda value: value["findings"][0].update(
            disposition="DEFER_OUT_OF_SCOPE",
            corrective_round="round-1",
            fix_instruction="must-not-exist",
        ),
        lambda value: value.update(unknown="field"),
        lambda value: value["findings"][0].update(owner=""),
    ],
)
def test_invalid_artifact_is_all_or_nothing_blocked(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], Any]
) -> None:
    artifact = _artifact([_finding("A")])
    mutate(artifact)

    payload, code, _, output = _invoke(tmp_path, artifact)

    assert code == 2
    assert payload["status"] == "BLOCKED"
    assert payload["mutations_performed"] == []
    assert not output.exists()


def test_duplicate_json_key_is_blocked_without_output(tmp_path: Path) -> None:
    disposition = tmp_path / "disposition.json"
    output = tmp_path / "report.json"
    disposition.write_text(
        '{"schema_version":1,"schema_version":1,"phase_id":"p3",'
        '"human_trigger":{},"findings":[]}',
        encoding="utf-8",
    )

    payload, code = adapter.execute(
        [
            "--disposition",
            str(disposition),
            "--out-report",
            str(output),
            "--human-triggered",
        ],
        repo_root=tmp_path,
    )

    assert code == 2
    assert "duplicate JSON key" in payload["diagnostic"]
    assert not output.exists()


def test_zero_accept_is_diagnostic_blocked_without_output(tmp_path: Path) -> None:
    rejected = _finding(
        "R", "REJECT_FALSE_POSITIVE", corrective_round=None, fix_instruction=None
    )

    payload, code, _, output = _invoke(tmp_path, _artifact([rejected]))

    assert code == 2
    assert payload["accepted_count"] == 0
    assert "emission unnecessary" in payload["diagnostic"]
    assert not output.exists()


def test_relative_and_outside_paths_are_blocked(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.json"
    inside = repo / "inside.json"
    _write_json(outside, _artifact([_finding("A")]))
    _write_json(inside, _artifact([_finding("A")]))
    cases = [
        ["--disposition", "relative.json", "--out-report", str(repo / "out.json")],
        ["--disposition", str(outside), "--out-report", str(repo / "out.json")],
        ["--disposition", str(inside), "--out-report", "relative.json"],
        ["--disposition", str(inside), "--out-report", str(tmp_path / "out.json")],
        [
            "--disposition",
            str(inside),
            "--out-report",
            str(repo / "missing" / "out.json"),
        ],
    ]

    for argv in cases:
        payload, code = adapter.execute([*argv, "--human-triggered"], repo_root=repo)
        assert code == 2, payload
        assert payload["mutations_performed"] == []


def test_existing_output_requires_overwrite_and_owned_strict_shape(tmp_path: Path) -> None:
    artifact = _artifact([_finding("A")])
    output = tmp_path / "report.json"
    output.write_text("preserve unrelated", encoding="utf-8")
    original = output.read_bytes()

    payload, code, _, _ = _invoke(tmp_path, artifact)
    assert code == 2
    assert output.read_bytes() == original

    payload, code, _, _ = _invoke(tmp_path, artifact, extra=["--overwrite"])
    assert code == 2
    assert "preserving" in payload["diagnostic"]
    assert output.read_bytes() == original

    output.write_text(json.dumps({"steps": [], "summary": {}}), encoding="utf-8")
    malformed_shape = output.read_bytes()
    payload, code, _, _ = _invoke(tmp_path, artifact, extra=["--overwrite"])
    assert code == 2
    assert output.read_bytes() == malformed_shape

    output.write_bytes(b"\xffnot-utf8")
    malformed_utf8 = output.read_bytes()
    payload, code, _, _ = _invoke(tmp_path, artifact, extra=["--overwrite"])
    assert code == 2
    assert output.read_bytes() == malformed_utf8


def test_owned_output_can_be_explicitly_overwritten(tmp_path: Path) -> None:
    first = _artifact([_finding("A")])
    _, first_code, _, output = _invoke(tmp_path, first)
    assert first_code == 0
    second = _artifact([_finding("B")])

    payload, code, _, _ = _invoke(tmp_path, second, extra=["--overwrite"])

    assert code == 0
    assert payload["accepted_ids"] == ["B"]
    report = json.loads(output.read_text(encoding="utf-8"))
    assert json.loads(report["steps"][0]["stdout_preview"])["id"] == "B"


def test_atomic_replace_failure_preserves_existing_output(tmp_path: Path) -> None:
    _, first_code, _, output = _invoke(tmp_path, _artifact([_finding("A")]))
    assert first_code == 0
    original = output.read_bytes()

    def fail_replace(source: str, target: str) -> None:
        del source, target
        raise OSError("injected atomic failure")

    payload, code, _, _ = _invoke(
        tmp_path,
        _artifact([_finding("B")]),
        extra=["--overwrite"],
        replace_file=fail_replace,
    )

    assert code == 70
    assert payload["status"] == "INTERNAL_ERROR"
    assert payload["mutations_performed"] == []
    assert output.read_bytes() == original
    assert not list(tmp_path.glob(".report.json.*.tmp"))


def test_stdout_is_one_utf8_json_line_without_traceback(tmp_path: Path, capsys: Any) -> None:
    disposition = tmp_path / "disposition.json"
    output = tmp_path / "report.json"
    _write_json(disposition, _artifact([_finding("A")]))

    code = adapter.main(
        [
            "--disposition",
            str(disposition),
            "--out-report",
            str(output),
            "--human-triggered",
        ],
        repo_root=tmp_path,
    )
    captured = capsys.readouterr()

    assert code == 0
    lines = captured.out.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["status"] == "PASS"
    assert "Traceback" not in captured.out
    assert captured.err == ""

    blocked_code = adapter.main(
        ["--disposition", str(disposition), "--out-report", str(output)],
        repo_root=tmp_path,
    )
    blocked_capture = capsys.readouterr()
    blocked_lines = blocked_capture.out.splitlines()
    assert blocked_code == 2
    assert len(blocked_lines) == 1
    assert json.loads(blocked_lines[0])["status"] == "BLOCKED"
    assert "Traceback" not in blocked_capture.out
    assert blocked_capture.err == ""


def test_preview_over_4000_characters_is_blocked_without_truncation(tmp_path: Path) -> None:
    finding = _finding("A")
    finding["finding"] = "x" * 4001

    payload, code, _, output = _invoke(tmp_path, _artifact([finding]))

    assert code == 2
    assert "4000" in payload["diagnostic"]
    assert not output.exists()


def test_mixed_accepted_corrective_rounds_are_blocked(tmp_path: Path) -> None:
    second = _finding("B", corrective_round="round-2")

    payload, code, _, output = _invoke(tmp_path, _artifact([_finding("A"), second]))

    assert code == 2
    assert "one corrective_round" in payload["diagnostic"]
    assert not output.exists()


def test_missing_human_evidence_ref_is_blocked(tmp_path: Path) -> None:
    artifact = _artifact([_finding("A")])
    del artifact["human_trigger"]["evidence_ref"]

    payload, code, _, output = _invoke(tmp_path, artifact)

    assert code == 2
    assert "evidence_ref" in payload["diagnostic"]
    assert not output.exists()


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(ZTR_ROOT) if not existing else f"{ZTR_ROOT}{os.pathsep}{existing}"
    return env


def _run_actor(mode: str, target: Path, prompt: Path | None = None) -> subprocess.CompletedProcess[str]:
    argv = [str(ZTR_PYTHON), str(ACTOR), mode, "--target", str(target)]
    if prompt is not None:
        argv.extend(["--prompt", str(prompt)])
    return subprocess.run(
        argv,
        cwd=REPO_ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_full_deterministic_accepted_only_remediation_e2e(tmp_path: Path) -> None:
    assert ZTR_PYTHON.is_file()
    accepted = _finding("ACCEPTED")
    rejected = _finding(
        "REJECTED",
        "REJECT_FALSE_POSITIVE",
        corrective_round=None,
        fix_instruction=None,
    )
    rejected["finding"] = "REJECTED_BODY_SECRET"
    deferred = _finding(
        "DEFERRED",
        "DEFER_OUT_OF_SCOPE",
        corrective_round=None,
        fix_instruction=None,
    )
    deferred["finding"] = "DEFERRED_BODY_SECRET"
    payload, code, _, report = _invoke(tmp_path, _artifact([accepted, rejected, deferred]))
    assert code == 0, payload

    original_prompt = tmp_path / "original.md"
    original_prompt.write_text("Fix only the explicitly accepted fixture token.\n", encoding="utf-8")
    fix_prompt = tmp_path / "fix.md"
    fix_proc = subprocess.run(
        [
            str(ZTR_PYTHON),
            "-m",
            "src",
            "fix-prompt",
            "--prompt-file",
            str(original_prompt),
            "--report-file",
            str(report),
            "--out",
            str(fix_prompt),
        ],
        cwd=ZTR_ROOT,
        env=_subprocess_env(),
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert fix_proc.returncode == 0, fix_proc.stderr
    fix_text = fix_prompt.read_text(encoding="utf-8")
    assert "Replace BAD_ACCEPTED with FIXED_ACCEPTED." in fix_text
    assert "REJECTED_BODY_SECRET" not in fix_text
    assert "DEFERRED_BODY_SECRET" not in fix_text

    target = tmp_path / "target.txt"
    target.write_text("BAD_ACCEPTED\nKEEP_REJECTED\nKEEP_DEFERRED\n", encoding="utf-8")
    implementer = _run_actor("implement", target, fix_prompt)
    assert implementer.returncode == 0, implementer.stdout + implementer.stderr
    assert json.loads(implementer.stdout)["corrective_rounds"] == 1

    reviewer = _run_actor("review", target, fix_prompt)
    assert reviewer.returncode == 0, reviewer.stdout + reviewer.stderr
    assert reviewer.stdout.splitlines()[-1] == "ZTR_VERDICT: PASS"

    first_gate = _run_actor("gate", target)
    second_gate = _run_actor("gate", target)
    assert first_gate.returncode == 0
    assert second_gate.returncode == 0
    assert json.loads(first_gate.stdout)["status"] == "PASS"
    assert json.loads(second_gate.stdout)["status"] == "PASS"
    assert target.read_text(encoding="utf-8") == (
        "FIXED_ACCEPTED\nKEEP_REJECTED\nKEEP_DEFERRED\n"
    )
