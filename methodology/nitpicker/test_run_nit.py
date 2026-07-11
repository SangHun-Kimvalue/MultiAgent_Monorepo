"""Tests for the local run_nit wrapper."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "run_nit.py"
SPEC = importlib.util.spec_from_file_location("run_nit_under_test", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
run_nit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(run_nit)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("**STATUS: ALL PASS**\n- acceptable", "ALL PASS"),
        ("# ALL PASS\n- acceptable", "ALL PASS"),
        ("`ALL PASS`\n- acceptable", "ALL PASS"),
        ("STATUS: CHANGES_REQUESTED\n- fix required", "CHANGES_REQUESTED"),
        ("ALL PASS\n- acceptable", "ALL PASS"),
        ("RESULT: BLOCKED\n- cannot review", "BLOCKED"),
    ],
)
def test_extract_status_accepts_header_tokens(output: str, expected: str) -> None:
    assert run_nit._extract_status(output) == expected


def test_extract_status_ignores_body_token_mentions_after_status() -> None:
    output = "ALL PASS\n- This body mentions BLOCKED as a word, not a header token."

    assert run_nit._extract_status(output) == "ALL PASS"


def test_extract_status_finds_blocked_on_second_header_line() -> None:
    output = "Here is my review:\n**STATUS: BLOCKED**\n- missing context"

    assert run_nit._extract_status(output) == "BLOCKED"


def test_extract_status_blocks_when_no_header_token() -> None:
    output = "\n".join(
        [
            "Here is my review:",
            "I looked at the patch.",
            "There are no obvious findings.",
            "The implementation seems fine.",
            "That is all.",
            "ALL PASS",
        ]
    )

    assert run_nit._extract_status(output) == "BLOCKED"


def test_run_nit_repo_option_reviews_explicit_repo(tmp_path: Path) -> None:
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    module_file = repo / "mod.py"
    module_file.write_text("def answer():\n    return 41\n", encoding="utf-8")
    _git(repo, "add", "mod.py")
    _git(repo, "commit", "-m", "seed")
    module_file.write_text("def answer():\n    return 42\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(repo),
            "--provider",
            "mock",
            "--changed",
        ],
        cwd=tmp_path,
        check=False,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode == 0
    assert "===== mod.py =====" in result.stdout
    assert "Mock provider checked mod.py" in result.stdout
    assert "No reviewable files found" not in result.stdout
    assert result.stderr == ""


def test_run_nit_defaults_to_current_working_directory(tmp_path: Path) -> None:
    repo = tmp_path / "default"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    module_file = repo / "mod.py"
    module_file.write_text("def answer():\n    return 41\n", encoding="utf-8")
    _git(repo, "add", "mod.py")
    _git(repo, "commit", "-m", "seed")
    module_file.write_text("def answer():\n    return 42\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--provider", "mock", "--changed"],
        cwd=repo,
        check=False,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode == 0
    assert "===== mod.py =====" in result.stdout
    assert "Mock provider checked mod.py" in result.stdout
    assert result.stderr == ""
