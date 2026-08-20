"""tests/test_cli_help.py — CLI help must not start runtime services."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _run_acp_help(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    package_root = Path(__file__).resolve().parents[1]
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(package_root)
        if not existing_pythonpath
        else f"{package_root}{os.pathsep}{existing_pythonpath}"
    )
    return subprocess.run(
        [sys.executable, "-m", "acp", *args],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )


def test_module_help_exits_zero_without_runtime_side_effects(tmp_path: Path) -> None:
    result = _run_acp_help(tmp_path, "--help")

    assert result.returncode == 0
    assert "사용법: python -m acp web" in result.stdout
    assert "--db-path PATH" in result.stdout
    assert result.stderr == ""
    assert not (tmp_path / ".acp").exists()


def test_web_help_exits_zero_without_runtime_side_effects(tmp_path: Path) -> None:
    result = _run_acp_help(tmp_path, "web", "--help")

    assert result.returncode == 0
    assert "사용법: python -m acp web" in result.stdout
    assert "--orch-events-dir PATH" in result.stdout
    assert "--orch-driver mock|claude-cli|codex-cli" in result.stdout
    assert result.stderr == ""
    assert not (tmp_path / ".acp").exists()


def test_web_help_with_other_flags_still_avoids_runtime_side_effects(tmp_path: Path) -> None:
    result = _run_acp_help(tmp_path, "web", "--fake", "--help")

    assert result.returncode == 0
    assert "사용법: python -m acp web" in result.stdout
    assert result.stderr == ""
    assert not (tmp_path / ".acp").exists()


def test_unknown_orch_driver_rejected_before_build_driver(tmp_path: Path) -> None:
    # unknown driver는 build_driver()/런타임 진입 전에 CLI validation으로 차단돼야 한다
    # (build_driver의 ValueError 누수가 아니라 정렬된 exit 1 + stderr 메시지).
    result = _run_acp_help(tmp_path, "web", "--orch-driver", "gpt-cli")

    assert result.returncode == 1
    assert "--orch-driver 값은 mock|claude-cli|codex-cli 여야 합니다" in result.stderr
    # build_driver의 fail-loud 메시지가 새어나오지 않는다(검증이 먼저 닫았다는 증거).
    assert "unknown orch driver kind" not in result.stderr
    # 런타임 부작용(.acp/uvicorn) 없이 닫힌다.
    assert not (tmp_path / ".acp").exists()
