"""T10-O1a T1: 추출 전 ``cmd_commit`` 관측 동작의 바이트 characterization."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parent / "t8_guard.py"
NL = os.linesep.encode("utf-8")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, check=False)


def _git_ok(repo: Path, *args: str) -> bytes:
    run = _git(repo, *args)
    assert run.returncode == 0, run.stderr.decode("utf-8", errors="replace")
    return run.stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "characterization-repo"
    root.mkdir()
    _git_ok(root, "init", "-b", "master")
    _git_ok(root, "config", "user.name", "t8-characterization")
    _git_ok(root, "config", "user.email", "t8-characterization@test.local")
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    _git_ok(root, "add", "base.txt")
    _git_ok(root, "commit", "-m", "init")
    return root


def _guard(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), *args],
        cwd=repo,
        capture_output=True,
        check=False,
        env=env,
    )


def _preflight(repo: Path) -> None:
    run = _guard(repo, "preflight")
    assert run.returncode == 0, run.stderr.decode("utf-8", errors="replace")


def _commit(repo: Path, *files: str) -> subprocess.CompletedProcess[bytes]:
    return _guard(repo, "commit", "-m", "characterize", "--files", *files)


def _assert_rejected(
    repo: Path,
    files: tuple[str, ...],
    expected_stderr: str,
) -> None:
    _preflight(repo)
    run = _commit(repo, *files)
    assert run.returncode == 1
    assert run.stdout == b""
    assert run.stderr == expected_stderr.encode("utf-8") + NL


def _assert_committed(repo: Path, files: tuple[str, ...], expected_paths: list[str]) -> None:
    _preflight(repo)
    run = _commit(repo, *files)
    assert run.returncode == 0
    head = _git_ok(repo, "rev-parse", "HEAD").decode("ascii").strip()[:9]
    expected = json.dumps(
        {"committed": expected_paths, "head": head}, ensure_ascii=False
    ).encode("utf-8") + NL
    assert run.stdout == expected
    assert run.stderr == b""


def test_t1_rejects_absolute_path_outside_repo(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside-absolute.txt"
    outside.write_text("outside\n", encoding="utf-8")
    raw = str(outside)
    _assert_rejected(repo, (raw,), f"T8_VIOLATION: repo 밖 경로 선언 금지: {raw}")


def test_t1_rejects_relative_escape(repo: Path) -> None:
    _assert_rejected(
        repo,
        ("../outside-relative.txt",),
        "T8_VIOLATION: repo 밖 경로 선언 금지: ../outside-relative.txt",
    )


def test_t1_rejects_symlink_escape(repo: Path, tmp_path: Path) -> None:
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "through-link.txt").write_text("outside\n", encoding="utf-8")
    link = repo / "outside-link"
    try:
        link.symlink_to(outside_dir, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        junction = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside_dir)],
            capture_output=True,
            check=False,
        )
        assert junction.returncode == 0, junction.stderr.decode(errors="replace")
    raw = "outside-link/through-link.txt"
    _assert_rejected(repo, (raw,), f"T8_VIOLATION: repo 밖 경로 선언 금지: {raw}")


@pytest.mark.parametrize("raw", ["*.txt", "file?.txt", "[ab].txt", ":(top)base.txt"])
def test_t1_rejects_glob_and_pathspec_magic(repo: Path, raw: str) -> None:
    _assert_rejected(
        repo,
        (raw,),
        f"T8_VIOLATION: glob/pathspec magic 금지(구체 파일만): {raw}",
    )


def test_t1_rejects_directory_prefix(repo: Path) -> None:
    directory = repo / "pkg"
    directory.mkdir()
    (directory / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git_ok(repo, "add", "pkg/tracked.txt")
    _git_ok(repo, "commit", "-m", "add tracked directory")
    _assert_rejected(
        repo,
        ("pkg",),
        "T8_VIOLATION: 디렉토리/프리픽스 선언 금지(구체 파일만): pkg",
    )


def test_t1_rejects_nonexistent_path(repo: Path) -> None:
    _assert_rejected(
        repo,
        ("missing.txt",),
        "T8_VIOLATION: 구체 파일 아님(미존재·비정규): missing.txt",
    )


def test_t1_canonicalizes_mixed_backslashes(repo: Path) -> None:
    nested = repo / "nested"
    nested.mkdir()
    (nested / "mixed.txt").write_text("mixed\n", encoding="utf-8")
    _assert_committed(repo, ("./nested\\mixed.txt",), ["nested/mixed.txt"])


def test_t1_records_case_only_path_behavior(repo: Path) -> None:
    path = repo / "CaseSensitive.txt"
    path.write_text("original\n", encoding="utf-8")
    _git_ok(repo, "add", "CaseSensitive.txt")
    _git_ok(repo, "commit", "-m", "add case path")
    path.write_text("changed\n", encoding="utf-8")
    _assert_committed(repo, ("casesensitive.txt",), ["CaseSensitive.txt"])


def test_t1_commits_space_and_non_ascii_filename(repo: Path) -> None:
    filename = "공백 파일.txt"
    (repo / filename).write_text("내용\n", encoding="utf-8")
    _assert_committed(repo, (filename,), [filename])


def test_t1_rejects_unchanged_tracked_file(repo: Path) -> None:
    _assert_rejected(
        repo,
        ("base.txt",),
        "T8_VIOLATION: 변경 없는 파일 선언(무효 스코프): base.txt",
    )


@pytest.mark.parametrize(
    "files",
    [
        ("*.txt", "../outside.txt"),
        ("../outside.txt", "*.txt"),
    ],
)
def test_t1_normalization_of_all_inputs_precedes_path_checks(
    repo: Path, files: tuple[str, str]
) -> None:
    _assert_rejected(
        repo,
        files,
        "T8_VIOLATION: repo 밖 경로 선언 금지: ../outside.txt",
    )


def test_t1_deduplicates_canonical_paths(repo: Path) -> None:
    directory = repo / "a"
    directory.mkdir()
    (directory / "a.txt").write_text("deduplicated\n", encoding="utf-8")
    _assert_committed(
        repo,
        ("a/a.txt", "./a/a.txt", "a\\a.txt"),
        ["a/a.txt"],
    )


def test_t1_per_path_rejections_use_sorted_canonical_order(repo: Path) -> None:
    _assert_rejected(
        repo,
        ("z-missing.txt", "a-missing.txt"),
        "T8_VIOLATION: 구체 파일 아님(미존재·비정규): a-missing.txt",
    )
