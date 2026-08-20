"""Phase 7 deterministic E2E smoke tests."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


def test_cli_review_verify_invariants_e2e_chain(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    config_path = _write_config(tmp_path, tmp_path / "sessions.db")
    target_dir = Path(__file__).resolve().parent / "_tmp"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"__{tmp_path.name}_e2e_target.py"
    request.addfinalizer(lambda: target.unlink(missing_ok=True))
    target.write_text("import os\n\ndef ok() -> bool:\n    return True\n", encoding="utf-8")

    first = _run("--config", str(config_path), "review", str(target))
    assert first.returncode == 1
    first_outer = _single_envelope(first.stdout)
    assert first_outer["status"] == "CHANGES_REQUESTED"
    first_inner = json.loads(first_outer["stdout"])
    assert first_inner["summary"]["verdict"] == "CHANGES_REQUESTED"
    assert any(
        "[F401]" in finding["evidence_or_repro"]
        for finding in first_inner["findings"]
    )

    target.write_text("def ok() -> bool:\n    return True\n", encoding="utf-8")

    second = _run("--config", str(config_path), "review", str(target))
    assert second.returncode == 0
    second_outer = _single_envelope(second.stdout)
    assert second_outer["status"] == "PASS"

    verify = _run(
        "verify",
        "--post-merge",
        str(target),
        prefix_args=("--config", str(config_path)),
    )
    assert verify.returncode == 0
    verify_outer = _single_envelope(verify.stdout)
    assert verify_outer["status"] == "PASS"

    invariants = _run("invariants", "--paths", "__phase7_e2e_no_changes__")
    assert invariants.returncode == 0
    invariants_outer = _single_envelope(invariants.stdout)
    assert invariants_outer["status"] == "PASS"


def test_cli_gate_e2e_chain(tmp_path: Path) -> None:
    broken = tmp_path / "critic_broken.json"
    broken.write_text(
        json.dumps({
            "kind": "critic",
            "verdict": "conditional",
            "findings": [{"severity": "major"}],
            "content": "structured critic result",
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    failed = _run("gate", str(broken))
    assert failed.returncode == 1
    failed_outer = _single_envelope(failed.stdout)
    assert failed_outer["status"] == "CHANGES_REQUESTED"

    valid = tmp_path / "critic_valid.json"
    valid.write_text(
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

    passed = _run("gate", str(valid))
    assert passed.returncode == 0
    passed_outer = _single_envelope(passed.stdout)
    assert passed_outer["status"] == "PASS"


def test_cli_explicit_paths_work_from_non_git_cwd(
    tmp_path: Path,
) -> None:
    config_path = _write_config(tmp_path, tmp_path / "sessions.db")
    target = tmp_path / "target.py"
    target.write_text("def ok() -> bool:\n    return True\n", encoding="utf-8")
    absolute = str(target.resolve())

    for supplied_path in (target.name, absolute):
        review = _run(
            "review",
            supplied_path,
            prefix_args=("--config", str(config_path)),
            cwd=tmp_path,
        )
        assert review.returncode == 0, review.stderr
        review_outer = _single_envelope(review.stdout)
        assert review_outer["status"] == "PASS"

        verify = _run(
            "verify",
            "--post-merge",
            supplied_path,
            cwd=tmp_path,
        )
        assert verify.returncode == 0, verify.stderr
        verify_outer = _single_envelope(verify.stdout)
        assert verify_outer["status"] == "PASS"
        verify_inner = json.loads(verify_outer["stdout"])
        assert verify_inner["verified"][0]["path"] == absolute


def _run(
    *args: str,
    prefix_args: tuple[str, ...] = (),
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    runtime_root = Path(__file__).resolve().parents[1]
    return subprocess.run(
        [sys.executable, "-m", "src", *prefix_args, *args],
        cwd=cwd or runtime_root,
        env=_subprocess_env(runtime_root),
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=60,
        check=False,
    )


def _single_envelope(stdout: str) -> dict[str, Any]:
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert isinstance(data, dict)
    inner = json.loads(data["stdout"])
    assert isinstance(inner, dict)
    expected_exit = {
        "PASS": 0,
        "CHANGES_REQUESTED": 1,
        "BLOCKED": 2,
    }[data["status"]]
    assert data["exit_code"] == expected_exit
    return data


def _write_config(tmp_path: Path, db_path: Path) -> Path:
    path = tmp_path / "agents.config.yaml"
    db_text = str(db_path).replace("\\", "/")
    path.write_text(
        "\n".join([
            "roles:",
            "  mechanical:",
            "    backend: ollama",
            "    model: qwen2.5-coder:7b",
            "    call_type: local",
            "agents: []",
            "session:",
            f"  db_path: {db_text}",
            "  enable_metrics: true",
            "  enable_session_log: true",
        ]),
        encoding="utf-8",
    )
    return path


def _subprocess_env(runtime_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    repo_root = str(runtime_root)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = repo_root if not existing else f"{repo_root}{os.pathsep}{existing}"
    return env
