from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "cross_runtime_lint.py"
SPEC = importlib.util.spec_from_file_location("cross_runtime_lint_under_test", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
cross_runtime_lint = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cross_runtime_lint
SPEC.loader.exec_module(cross_runtime_lint)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "master")
    _git(root, "config", "user.name", "lint-test")
    _git(root, "config", "user.email", "lint@test.local")
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "base.txt")
    _git(root, "commit", "-m", "init")
    return root


def _mk_runtime(repo: Path) -> None:
    for runtime in ("acp", "ztr"):
        exe = repo / f"runtimes/{runtime}/.venv/Scripts/python.exe"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("fake", encoding="utf-8")
    (repo / "runtimes/acp/acp").mkdir(parents=True, exist_ok=True)
    (repo / "runtimes/ztr/src").mkdir(parents=True, exist_ok=True)


def _touch(repo: Path, *rels: str) -> None:
    """대상 .py를 실제로 만든다 — missing_on_disk 분리(리뷰 P2) 후 lint 대상은 실존해야 한다."""
    for rel in rels:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n", encoding="utf-8")


def _fake_run(returncodes: dict[str, int]):
    def run(cmd: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
        module = cmd[2]
        runtime = "acp" if "runtimes\\acp" in str(cwd) or "runtimes/acp" in str(cwd) else "ztr"
        rc = returncodes[f"{runtime}:{module}"]
        return subprocess.CompletedProcess(cmd, rc, stdout=f"{runtime} {module}\n", stderr="")

    return run


def _last_json(capsys) -> dict:
    out = capsys.readouterr().out.strip().splitlines()
    assert out
    return json.loads(out[-1])


def test_mapping_classifies_runtime_targets_and_skips(repo: Path) -> None:
    _touch(repo, "runtimes/acp/acp/orch_runs.py", "runtimes/ztr/src/ztr/phase.py")
    targets, skipped = cross_runtime_lint.classify_targets(
        repo,
        [
            "runtimes/acp/acp/orch_runs.py",
            "runtimes/ztr/src/ztr/phase.py",
            "methodology/lint/cross_runtime_lint.py",
            "README.md",
        ],
    )

    assert targets["acp"] == ["runtimes/acp/acp/orch_runs.py"]
    assert targets["ztr"] == ["runtimes/ztr/src/ztr/phase.py"]
    assert {"file": "methodology/lint/cross_runtime_lint.py", "reason": "outside_runtime"} in skipped
    assert {"file": "README.md", "reason": "not_python"} in skipped


def test_exit_contract_all_green_returns_zero(repo: Path, monkeypatch, capsys) -> None:
    _mk_runtime(repo)
    _touch(repo, "runtimes/acp/acp/a.py", "runtimes/ztr/src/b.py")
    monkeypatch.setattr(
        cross_runtime_lint,
        "_run_subprocess",
        _fake_run({"acp:ruff": 0, "acp:mypy": 0, "ztr:ruff": 0, "ztr:mypy": 0}),
    )

    rc = cross_runtime_lint.main(
        ["--repo", str(repo), "--files", "runtimes/acp/acp/a.py", "runtimes/ztr/src/b.py"]
    )

    payload = _last_json(capsys)
    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["runtimes"]["acp"]["ruff"] == 0
    assert payload["runtimes"]["ztr"]["mypy"] == 0


def test_exit_contract_ruff_fail_returns_one(repo: Path, monkeypatch, capsys) -> None:
    _mk_runtime(repo)
    monkeypatch.setattr(
        cross_runtime_lint,
        "_run_subprocess",
        _fake_run({"acp:ruff": 1, "acp:mypy": 0, "ztr:ruff": 0, "ztr:mypy": 0}),
    )
    _touch(repo, "runtimes/acp/acp/a.py")

    rc = cross_runtime_lint.main(["--repo", str(repo), "--files", "runtimes/acp/acp/a.py"])

    payload = _last_json(capsys)
    assert rc == 1
    assert payload["status"] == "FAIL"
    assert payload["runtimes"]["acp"]["ruff"] == 1


def test_exit_contract_mypy_fail_returns_one(repo: Path, monkeypatch, capsys) -> None:
    _mk_runtime(repo)
    monkeypatch.setattr(
        cross_runtime_lint,
        "_run_subprocess",
        _fake_run({"acp:ruff": 0, "acp:mypy": 1, "ztr:ruff": 0, "ztr:mypy": 0}),
    )
    _touch(repo, "runtimes/acp/acp/a.py")

    rc = cross_runtime_lint.main(["--repo", str(repo), "--files", "runtimes/acp/acp/a.py"])

    payload = _last_json(capsys)
    assert rc == 1
    assert payload["status"] == "FAIL"
    assert payload["runtimes"]["acp"]["mypy"] == 1


def test_exit_contract_oserror_returns_two(repo: Path, monkeypatch, capsys) -> None:
    _mk_runtime(repo)

    def boom(cmd: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
        raise OSError("missing")

    monkeypatch.setattr(cross_runtime_lint, "_run_subprocess", boom)
    _touch(repo, "runtimes/acp/acp/a.py")

    rc = cross_runtime_lint.main(["--repo", str(repo), "--files", "runtimes/acp/acp/a.py"])

    payload = _last_json(capsys)
    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert payload["runtimes"]["acp"]["ruff"] is None
    assert payload["runtimes"]["acp"]["ruff_detail"]["error"] == "oserror"


def test_exit_contract_missing_python_returns_two(repo: Path, capsys) -> None:
    _touch(repo, "runtimes/acp/acp/a.py")
    rc = cross_runtime_lint.main(["--repo", str(repo), "--files", "runtimes/acp/acp/a.py"])

    payload = _last_json(capsys)
    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert payload["runtimes"]["acp"]["ruff_detail"]["error"] == "missing_python"


def test_no_targets_returns_zero_and_records_no_targets(repo: Path, capsys) -> None:
    rc = cross_runtime_lint.main(["--repo", str(repo), "--files", "methodology/lint/x.py"])

    payload = _last_json(capsys)
    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["no_targets"] is True
    assert payload["skipped"] == [{"file": "methodology/lint/x.py", "reason": "outside_runtime"}]


def test_diff_collection_includes_changed_and_untracked_files(repo: Path) -> None:
    tracked = repo / "runtimes/acp/acp/tracked.py"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("old\n", encoding="utf-8")
    _git(repo, "add", "runtimes/acp/acp/tracked.py")
    _git(repo, "commit", "-m", "tracked")
    tracked.write_text("new\n", encoding="utf-8")
    untracked = repo / "runtimes/ztr/src/new_file.py"
    untracked.parent.mkdir(parents=True)
    untracked.write_text("new\n", encoding="utf-8")

    files, err = cross_runtime_lint.collect_changed_files(repo, "HEAD")

    assert err is None
    assert "runtimes/acp/acp/tracked.py" in files
    assert "runtimes/ztr/src/new_file.py" in files


def test_real_acp_venv_ruff_subprocess_on_clean_temp_file(tmp_path: Path) -> None:
    real_repo = SCRIPT.parents[2]
    acp_python = real_repo / "runtimes/acp/.venv/Scripts/python.exe"
    if not acp_python.exists():
        pytest.skip("acp venv python 없음")
    sample = tmp_path / "clean.py"
    sample.write_text("VALUE = 1\n", encoding="utf-8")

    run = cross_runtime_lint._run_subprocess(
        [str(acp_python), "-m", "ruff", "check", str(sample)],
        cwd=real_repo,
        timeout=60,
    )

    assert run.returncode == 0


def test_stdout_last_line_is_valid_json(repo: Path, capsys) -> None:
    rc = cross_runtime_lint.main(["--repo", str(repo), "--files", "README.md"])

    payload = _last_json(capsys)
    assert rc == 0
    assert payload["status"] == "PASS"


def test_deleted_runtime_py_is_skipped_not_lint_failed(repo: Path, monkeypatch, capsys) -> None:
    """리뷰 P2: 삭제된 .py(diff에 잔존)가 ruff E902 rc=1로 lint FAIL 오분류되면 안 된다."""
    _mk_runtime(repo)
    monkeypatch.setattr(
        cross_runtime_lint,
        "_run_subprocess",
        _fake_run({"acp:ruff": 0, "acp:mypy": 0, "ztr:ruff": 0, "ztr:mypy": 0}),
    )
    # 파일을 만들지 않음 = 삭제된 tracked 파일이 diff에 남은 상황과 동일(디스크 부재)

    rc = cross_runtime_lint.main(
        ["--repo", str(repo), "--files", "runtimes/acp/acp/deleted.py"]
    )

    payload = _last_json(capsys)
    assert rc == 0
    assert payload["status"] == "PASS"
    assert {"file": "runtimes/acp/acp/deleted.py", "reason": "missing_on_disk"} in payload["skipped"]
    assert payload["no_targets"] is True


def test_empty_files_flag_does_not_fall_back_to_diff(repo: Path, capsys) -> None:
    """리뷰 P2: --files 빈 리스트는 '대상 없음' 의도 — diff 모드로 조용히 폴백하면 안 된다."""
    # diff에 잡힐 미커밋 변경을 만들어 둔다(폴백했다면 대상에 들어갔을 것).
    _touch(repo, "runtimes/acp/acp/would_be_scanned.py")

    rc = cross_runtime_lint.main(["--repo", str(repo), "--files"])

    payload = _last_json(capsys)
    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["mode"] == "files"
    assert payload["no_targets"] is True
    assert payload["targets"]["acp"] == []  # 폴백 안 함 — 변경 파일이 검사되지 않음


def test_payload_records_diff_mode(repo: Path, capsys) -> None:
    rc = cross_runtime_lint.main(["--repo", str(repo)])  # clean repo — diff 대상 없음

    payload = _last_json(capsys)
    assert rc == 0
    assert payload["mode"] == "diff"


def test_git_launch_failure_is_blocked_json_not_traceback(repo: Path, monkeypatch, capsys) -> None:
    """리뷰 P2: git 바이너리 부재(OSError)가 traceback/exit 1이 아니라 BLOCKED JSON + 2."""

    def no_git(repo_arg, *args):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(cross_runtime_lint, "_git", no_git)

    rc = cross_runtime_lint.main(["--repo", str(repo)])  # diff 모드 → _git 호출

    payload = _last_json(capsys)
    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert "FileNotFoundError" in (payload["collection_error"] or "")


def test_missing_tool_module_returns_two(repo: Path, monkeypatch, capsys) -> None:
    """리뷰 P3: venv는 있으나 ruff 모듈 부재('No module named') → BLOCKED 2."""
    _mk_runtime(repo)
    _touch(repo, "runtimes/acp/acp/a.py")

    def no_module(cmd: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="No module named ruff")

    monkeypatch.setattr(cross_runtime_lint, "_run_subprocess", no_module)

    rc = cross_runtime_lint.main(["--repo", str(repo), "--files", "runtimes/acp/acp/a.py"])

    payload = _last_json(capsys)
    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert payload["runtimes"]["acp"]["ruff_detail"]["error"] == "missing_tool"


def test_tool_timeout_returns_two(repo: Path, monkeypatch, capsys) -> None:
    """리뷰 P3: 도구 timeout → BLOCKED 2 (TimeoutExpired 경로)."""
    _mk_runtime(repo)
    _touch(repo, "runtimes/acp/acp/a.py")

    def slow(cmd: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(cross_runtime_lint, "_run_subprocess", slow)

    rc = cross_runtime_lint.main(["--repo", str(repo), "--files", "runtimes/acp/acp/a.py"])

    payload = _last_json(capsys)
    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert payload["runtimes"]["acp"]["ruff_detail"]["error"] == "timeout"
