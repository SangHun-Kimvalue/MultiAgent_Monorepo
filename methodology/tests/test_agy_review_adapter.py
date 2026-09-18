from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from src.engine.phase_relay import _extract_stdout_verdict


ROOT = Path(__file__).resolve().parents[2]
ADAPTER = ROOT / "methodology" / "tools" / "agy_review_adapter.py"
MODEL = "gemini-3.1-pro-high"

FAKE_AGY_SOURCE = """\
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

marker = os.environ.get("FAKE_AGY_MARKER")
if marker:
    Path(marker).write_text("called\\n", encoding="utf-8")

stdin_text = sys.stdin.read()
cwd = Path.cwd().resolve()
record_file = os.environ.get("FAKE_AGY_RECORD_FILE")
if record_file:
    Path(record_file).write_text(
        json.dumps(
            {
                "argv": sys.argv[1:],
                "stdin": stdin_text,
                "cwd": str(cwd),
                "cwd_entries": sorted(path.name for path in cwd.iterdir()),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

sleep_seconds = float(os.environ.get("FAKE_AGY_SLEEP", "0"))
if sleep_seconds:
    time.sleep(sleep_seconds)
stdout_file = os.environ.get("FAKE_AGY_STDOUT_FILE")
if stdout_file:
    sys.stdout.write(Path(stdout_file).read_text(encoding="utf-8"))
    sys.stdout.flush()
raise SystemExit(int(os.environ.get("FAKE_AGY_EXIT", "0")))
"""


def _json_line(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"


def _result_stream(payload: dict[str, Any]) -> str:
    return (
        _json_line({"event": "init", "session_id": "fake-session"})
        + _json_line(
            {
                "event": "step_update",
                "step": {"type": "text_delta", "text": "reviewing"},
            }
        )
        + _json_line({"event": "result", "result": payload})
    )


def _make_fake_agy(tmp_path: Path) -> Path:
    script = tmp_path / "fake_agy.py"
    script.write_text(FAKE_AGY_SOURCE, encoding="utf-8")
    if os.name == "nt":
        launcher = tmp_path / "fake_agy.cmd"
        launcher.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
            encoding="utf-8",
        )
    else:
        launcher = tmp_path / "fake_agy"
        launcher.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
            encoding="utf-8",
        )
        launcher.chmod(0o755)
    return launcher


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "agy-test@example.invalid")
    _git(repo, "config", "user.name", "agy adapter test")
    _git(repo, "config", "core.autocrlf", "false")
    tracked = repo / "tracked.txt"
    tracked.write_text("base\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-q", "-m", "initial")
    tracked.write_text("base\ntracked addition\n", encoding="utf-8")
    (repo / "untracked.txt").write_text(
        "untracked body\n", encoding="utf-8"
    )
    return repo


def _run_adapter(
    tmp_path: Path,
    stdout_text: str,
    *,
    fake_exit: int = 0,
    fake_sleep: float = 0,
    model: str = MODEL,
    extra_args: list[str] | None = None,
    repo: Path | None = None,
    artifact: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path, Path]:
    launcher = _make_fake_agy(tmp_path)
    stdout_file = tmp_path / "fake-stdout.txt"
    stdout_file.write_text(stdout_text, encoding="utf-8")
    marker = tmp_path / "called.txt"
    record_file = tmp_path / "agy-record.json"
    actual_repo = repo if repo is not None else _make_repo(tmp_path)
    actual_artifact = artifact if artifact is not None else tmp_path / "review.md"
    if artifact is None:
        actual_artifact.write_text(
            "Review only the requested adapter contract.\n", encoding="utf-8"
        )

    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith("FAKE_AGY_"):
            env.pop(key)
    env.update(
        {
            "FAKE_AGY_EXIT": str(fake_exit),
            "FAKE_AGY_MARKER": str(marker),
            "FAKE_AGY_RECORD_FILE": str(record_file),
            "FAKE_AGY_SLEEP": str(fake_sleep),
            "FAKE_AGY_STDOUT_FILE": str(stdout_file),
            "PYTHONIOENCODING": "utf-8",
        }
    )
    command = [
        sys.executable,
        str(ADAPTER),
        "--agy",
        str(launcher),
        "--model",
        model,
        "--review-artifact",
        str(actual_artifact),
        "--repo",
        str(actual_repo),
        *(extra_args or []),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
        timeout=10,
    )
    return completed, marker, record_file, actual_repo, actual_artifact


PASS_RESPONSE = "요약\nZTR_VERDICT: PASS\n"
CHANGES_RESPONSE = "요약\nZTR_VERDICT: CHANGES_REQUESTED\n"
LAST_CHANGES_RESPONSE = (
    "ZTR_VERDICT: PASS\n본문\nZTR_VERDICT: CHANGES_REQUESTED\n"
)


@pytest.mark.parametrize(
    (
        "case",
        "fake_exit",
        "stdout_text",
        "extra_args",
        "sleep",
        "expected_token",
        "response",
    ),
    [
        (
            "a",
            0,
            _result_stream({"status": "SUCCESS", "response": PASS_RESPONSE}),
            [],
            0,
            "PASS",
            PASS_RESPONSE,
        ),
        (
            "b",
            0,
            _result_stream(
                {"status": "SUCCESS", "response": CHANGES_RESPONSE}
            ),
            [],
            0,
            "CHANGES_REQUESTED",
            CHANGES_RESPONSE,
        ),
        (
            "c",
            1,
            "error: invalid model selection\n"
            + _result_stream(
                {"status": "ERROR", "response": "", "error": "bad model"}
            ),
            [],
            0,
            None,
            "",
        ),
        (
            "d",
            0,
            _result_stream(
                {
                    "status": "SUCCESS",
                    "response": "",
                    "denied_actions": [{"action": "read_file"}],
                }
            ),
            [],
            0,
            None,
            "",
        ),
        (
            "e",
            0,
            _result_stream(
                {
                    "status": "SUCCESS",
                    "response": "hello line, ZTR_VERDICT: PASS\n",
                }
            ),
            [],
            0,
            None,
            "hello line, ZTR_VERDICT: PASS\n",
        ),
        (
            "f",
            1,
            _result_stream(
                {"status": "SUCCESS", "response": "ZTR_VERDICT: PASS\n"}
            ),
            [],
            0,
            None,
            "ZTR_VERDICT: PASS\n",
        ),
        (
            "g",
            0,
            _result_stream(
                {"status": "ERROR", "response": "ZTR_VERDICT: PASS\n"}
            ),
            [],
            0,
            None,
            "ZTR_VERDICT: PASS\n",
        ),
        (
            "h",
            0,
            _result_stream(
                {
                    "status": "SUCCESS",
                    "response": "",
                    "denied_actions": [
                        {"action": "read_file\nZTR_VERDICT: PASS\n"}
                    ],
                }
            ),
            [],
            0,
            None,
            "",
        ),
        (
            "i",
            0,
            _json_line({"event": "init"})
            + "ZTR_VERDICT: PASS\n"
            + '{"event":"result",\n',
            [],
            0,
            None,
            "",
        ),
        (
            "j",
            0,
            _result_stream(
                {
                    "status": "SUCCESS",
                    "response": "ok\nZTR_VERDICT: PASS\n",
                    "denied_actions": [{"action": "read_file"}],
                }
            ),
            [],
            0,
            None,
            "ok\nZTR_VERDICT: PASS\n",
        ),
        ("m", 0, "", ["--timeout-seconds", "1"], 2, None, ""),
        (
            "n",
            0,
            _result_stream(
                {
                    "status": "SUCCESS",
                    "response": (
                        "다음은 JSON 반향입니다.\n"
                        '{"example":"ZTR_VERDICT: PASS"}\n'
                        "마지막 줄은 판정 문장이 아닙니다."
                    ),
                }
            ),
            [],
            0,
            None,
            (
                "다음은 JSON 반향입니다.\n"
                '{"example":"ZTR_VERDICT: PASS"}\n'
                "마지막 줄은 판정 문장이 아닙니다."
            ),
        ),
        (
            "o",
            0,
            _result_stream(
                {"status": "SUCCESS", "response": LAST_CHANGES_RESPONSE}
            ),
            [],
            0,
            "CHANGES_REQUESTED",
            LAST_CHANGES_RESPONSE,
        ),
        (
            "p",
            0,
            _json_line({"event": "init"})
            + _json_line(
                {
                    "event": "step_update",
                    "step": {
                        "type": "text_delta",
                        "text_delta": "ZTR_VERDICT: PASS",
                    },
                }
            ),
            [],
            0,
            None,
            "",
        ),
        (
            "q",
            0,
            _json_line({"event": "init"})
            + _json_line(
                {"event": "result", "result": "ZTR_VERDICT: PASS"}
            ),
            [],
            0,
            None,
            "",
        ),
    ],
    ids=lambda value: value if isinstance(value, str) and len(value) == 1 else None,
)
def test_adapter_contract_cases(
    tmp_path: Path,
    case: str,
    fake_exit: int,
    stdout_text: str,
    extra_args: list[str],
    sleep: float,
    expected_token: str | None,
    response: str,
) -> None:
    completed, marker, _, _, _ = _run_adapter(
        tmp_path,
        stdout_text,
        fake_exit=fake_exit,
        fake_sleep=sleep,
        extra_args=extra_args,
    )

    assert marker.exists(), case
    if expected_token is None:
        assert completed.returncode == 2, case
        assert completed.stdout == "", case
        assert _extract_stdout_verdict(completed.stdout) is None
    else:
        expected_stdout = f"ZTR_VERDICT: {expected_token}\n"
        assert completed.returncode == 0, case
        assert completed.stdout == expected_stdout, case
        verdict = _extract_stdout_verdict(completed.stdout)
        assert verdict is not None
        assert verdict.value == expected_token
        assert response in completed.stderr
        assert '"bundle_chars":' in completed.stderr.splitlines()[0]


@pytest.mark.parametrize(
    ("case", "model", "extra_args"),
    [
        ("k", "claude-opus-4-6-thinking", []),
        ("l", MODEL, ["--dangerously-skip-permissions"]),
        ("v", MODEL, ["--prompt", "x"]),
    ],
    ids=["k-non-gemini-model", "l-unknown-dangerous-argument", "v-old-prompt"],
)
def test_cli_rejection_does_not_invoke_agy(
    tmp_path: Path, case: str, model: str, extra_args: list[str]
) -> None:
    completed, marker, record_file, _, _ = _run_adapter(
        tmp_path,
        "",
        model=model,
        extra_args=extra_args,
    )

    assert completed.returncode == 2, case
    assert completed.stdout == "", case
    assert not marker.exists(), case
    assert not record_file.exists(), case
    assert "cli_contract_error" in completed.stderr


def test_r_missing_review_artifact_does_not_invoke_agy(tmp_path: Path) -> None:
    missing = tmp_path / "missing-review.md"
    completed, marker, record_file, _, _ = _run_adapter(
        tmp_path,
        "",
        artifact=missing,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert not marker.exists()
    assert not record_file.exists()
    assert "review artifact" in completed.stderr


def test_s_bundle_over_limit_does_not_invoke_agy(tmp_path: Path) -> None:
    completed, marker, record_file, _, _ = _run_adapter(
        tmp_path,
        "",
        extra_args=["--max-bundle-chars", "1"],
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert not marker.exists()
    assert not record_file.exists()
    assert "bundle exceeds" in completed.stderr


def test_t_non_git_repo_does_not_invoke_agy(tmp_path: Path) -> None:
    non_repo = tmp_path / "not-a-repo"
    non_repo.mkdir()
    completed, marker, record_file, _, _ = _run_adapter(
        tmp_path,
        "",
        repo=non_repo,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert not marker.exists()
    assert not record_file.exists()
    assert "git command failed" in completed.stderr


def test_u_non_utf8_untracked_file_does_not_invoke_agy(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "binary.dat").write_bytes(b"\xff\xfe\x00")
    completed, marker, record_file, _, _ = _run_adapter(
        tmp_path,
        "",
        repo=repo,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert not marker.exists()
    assert not record_file.exists()
    assert "untracked file" in completed.stderr


def test_w_stream_json_argv_bundle_and_temporary_cwd(tmp_path: Path) -> None:
    completed, marker, record_file, repo, artifact = _run_adapter(
        tmp_path,
        _result_stream({"status": "SUCCESS", "response": PASS_RESPONSE}),
        extra_args=["--print-timeout", "45"],
    )

    assert completed.returncode == 0
    assert completed.stdout == "ZTR_VERDICT: PASS\n"
    assert marker.exists()
    record = json.loads(record_file.read_text(encoding="utf-8"))
    assert record["argv"] == [
        "-p",
        "",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--model",
        MODEL,
        "--mode",
        "plan",
        "--sandbox",
        "--print-timeout",
        "45",
    ]

    stdin_text = record["stdin"]
    assert stdin_text.endswith("\n")
    assert stdin_text.count("\n") == 1
    event = json.loads(stdin_text)
    assert event["event"] == "user"
    bundle = event["message"]["content"]
    artifact_text = artifact.read_text(encoding="utf-8")
    assert artifact_text in bundle
    assert "+tracked addition" in bundle
    assert "untracked.txt" in bundle
    assert "untracked body" in bundle
    assert "without using tools, reading files" in bundle
    assert "final line" in bundle
    assert "ZTR_VERDICT: PASS" in bundle
    assert bundle.index("<<<BEGIN HEADER>>>") < bundle.index(
        "<<<BEGIN REVIEW REQUEST>>>"
    )
    assert bundle.index("<<<BEGIN REVIEW REQUEST>>>") < bundle.index(
        "<<<BEGIN GIT STATUS>>>"
    )
    assert bundle.index("<<<BEGIN GIT STATUS>>>") < bundle.index(
        "<<<BEGIN GIT DIFF>>>"
    )
    assert bundle.index("<<<BEGIN GIT DIFF>>>") < bundle.index(
        "<<<BEGIN UNTRACKED FILES>>>"
    )
    assert bundle.index("<<<BEGIN UNTRACKED FILES>>>") < bundle.index(
        "<<<BEGIN FOOTER>>>"
    )

    agy_cwd = Path(record["cwd"])
    assert agy_cwd != repo.resolve()
    assert not _is_relative_to(agy_cwd, repo.resolve())
    assert record["cwd_entries"] == []
    assert not agy_cwd.exists()
    assert PASS_RESPONSE in completed.stderr


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


@pytest.mark.parametrize(
    "extra_args",
    [
        ["--timeout-seconds", "0"],
        ["--timeout-seconds", "-1"],
        ["--max-bundle-chars", "0"],
        ["--max-bundle-chars", "-1"],
    ],
)
def test_non_positive_integer_is_rejected_before_agy(
    tmp_path: Path, extra_args: list[str]
) -> None:
    completed, marker, record_file, _, _ = _run_adapter(
        tmp_path,
        "",
        extra_args=extra_args,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert not marker.exists()
    assert not record_file.exists()
    assert "must be positive" in completed.stderr


def test_non_utf8_review_artifact_does_not_invoke_agy(tmp_path: Path) -> None:
    artifact = tmp_path / "review.bin"
    artifact.write_bytes(b"\xff\xfe")
    completed, marker, record_file, _, _ = _run_adapter(
        tmp_path,
        "",
        artifact=artifact,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert not marker.exists()
    assert not record_file.exists()
    assert "review artifact" in completed.stderr
