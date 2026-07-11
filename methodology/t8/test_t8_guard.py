"""T8 guard 재발 시뮬 테스트 — 프로토콜 §1 위험모드를 temp repo에서 재현해 차단을 검증."""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "t8_guard.py"
SPEC = importlib.util.spec_from_file_location("t8_guard_under_test", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
t8_guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(t8_guard)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """커밋 1개 있는 temp git repo (identity는 repo-local로 고정 — 환경 무의존)."""
    root = tmp_path / "shared-repo"
    root.mkdir()
    _git(root, "init", "-b", "master")
    _git(root, "config", "user.name", "t8-test")
    _git(root, "config", "user.email", "t8@test.local")
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "base.txt")
    _git(root, "commit", "-m", "init")
    return root


def _preflight(repo_path: Path) -> int:
    return t8_guard.main(["--repo", str(repo_path), "preflight"])


def test_preflight_writes_snapshot_and_reports_dirty(repo: Path, capsys) -> None:
    (repo / "wip.txt").write_text("other session wip\n", encoding="utf-8")

    assert _preflight(repo) == 0

    snapshot = json.loads((repo / ".git" / "t8-snapshot.json").read_text(encoding="utf-8"))
    assert snapshot["branch"] == "master"
    assert any("wip.txt" in line for line in snapshot["dirty"])
    out = capsys.readouterr()
    assert json.loads(out.out)["head"] == snapshot["head"]
    assert "T8_WARN" in out.err  # dirty 고지(차단 아님)


def test_commit_requires_preflight_snapshot(repo: Path, capsys) -> None:
    (repo / "mine.txt").write_text("mine\n", encoding="utf-8")

    rc = t8_guard.main(["--repo", str(repo), "commit", "-m", "x", "--files", "mine.txt"])

    assert rc == 2  # fail-closed: §3① 스냅샷 없이 커밋 불가
    assert "preflight" in capsys.readouterr().err


def test_commit_excludes_other_sessions_staged_files(repo: Path) -> None:
    """위험모드 1·2 재현: 타세션이 stage해 둔 파일이 내 커밋에 편입되지도, 소실되지도 않는다."""
    # 세션 A(타세션): fileA를 작성하고 stage까지 해 둠(미커밋 WIP).
    (repo / "session_a.txt").write_text("A wip\n", encoding="utf-8")
    _git(repo, "add", "session_a.txt")
    # 세션 B(나): preflight 후 내 파일만 가드 커밋.
    (repo / "session_b.txt").write_text("B work\n", encoding="utf-8")
    assert _preflight(repo) == 0

    rc = t8_guard.main(
        ["--repo", str(repo), "commit", "-m", "B only", "--files", "session_b.txt"]
    )

    assert rc == 0
    committed = _git(repo, "show", "--name-only", "--format=", "HEAD").stdout
    assert "session_b.txt" in committed
    assert "session_a.txt" not in committed  # 편입 차단(위험모드 2)
    staged = _git(repo, "diff", "--cached", "--name-only").stdout
    assert "session_a.txt" in staged  # 타세션 staged 보존(위험모드 1: 소실 없음)


def test_commit_refuses_branch_switch_after_snapshot(repo: Path, capsys) -> None:
    """§2 불변: 스냅샷 이후 브랜치가 바뀌었으면 커밋 거부(공유 worktree 전환 감지)."""
    (repo / "mine.txt").write_text("mine\n", encoding="utf-8")
    assert _preflight(repo) == 0
    _git(repo, "checkout", "-b", "feature")

    rc = t8_guard.main(["--repo", str(repo), "commit", "-m", "x", "--files", "mine.txt"])

    assert rc == 1
    assert "브랜치 전환" in capsys.readouterr().err
    assert "mine.txt" not in _git(repo, "show", "--name-only", "--format=", "HEAD").stdout


def test_commit_with_no_changes_is_rejected_before_commit(repo: Path, capsys) -> None:
    """4R P2: 변경 없는 파일 선언은 커밋 생성 전 무효 스코프로 거부(silent 누락 방지)."""
    assert _preflight(repo) == 0
    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    rc = t8_guard.main(["--repo", str(repo), "commit", "-m", "x", "--files", "base.txt"])

    assert rc == 1
    assert "변경 없는" in capsys.readouterr().err
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head_before


def test_isolate_creates_worktree_and_refuses_checked_out_branch(
    repo: Path, tmp_path: Path, capsys
) -> None:
    """§3④: worktree 격리 성공 + 이미 체크아웃된 브랜치는 git 거부를 exit 2로 표면화."""
    _git(repo, "branch", "feature")
    dest = tmp_path / "isolated"

    rc = t8_guard.main(
        ["--repo", str(repo), "isolate", "--branch", "feature", "--dest", str(dest)]
    )

    assert rc == 0
    assert (dest / "base.txt").exists()
    payload = json.loads(capsys.readouterr().out)
    assert payload["worktree"] == str(dest)

    # 같은 브랜치 재격리 → git이 거부(이미 checked out) → exit 2.
    rc2 = t8_guard.main(
        ["--repo", str(repo), "isolate", "--branch", "feature", "--dest", str(tmp_path / "dup")]
    )
    assert rc2 == 2


def test_commit_rejects_directory_declaration(repo: Path, capsys) -> None:
    """독립 리뷰 P1: 디렉토리 pathspec은 타세션 staged를 쓸어담는다 — 커밋 생성 전 사전 거부."""
    sub = repo / "pkg"
    sub.mkdir()
    (sub / "mine.txt").write_text("mine\n", encoding="utf-8")
    (sub / "theirs.txt").write_text("theirs\n", encoding="utf-8")
    _git(repo, "add", "pkg/theirs.txt")  # 타세션 staged
    assert _preflight(repo) == 0
    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    rc = t8_guard.main(["--repo", str(repo), "commit", "-m", "x", "--files", "pkg"])

    assert rc == 1
    assert "디렉토리" in capsys.readouterr().err
    # 사후 감지가 아니라 사전 거부 — 잘못된 커밋 자체가 생성되지 않아야 한다.
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head_before


def test_commit_rejects_glob_pathspec(repo: Path, capsys) -> None:
    assert _preflight(repo) == 0
    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    rc = t8_guard.main(["--repo", str(repo), "commit", "-m", "x", "--files", "*.txt"])

    assert rc == 1
    assert "glob" in capsys.readouterr().err
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head_before


def test_commit_rejects_declared_file_already_staged_by_other_session(
    repo: Path, capsys
) -> None:
    """독립 리뷰 P1: declared == 타세션 staged 동일 파일 — add가 그 내용을 덮으므로 fail-closed 거부."""
    shared = repo / "shared.txt"
    shared.write_text("theirs (staged)\n", encoding="utf-8")
    _git(repo, "add", "shared.txt")  # 타세션이 stage해 둠
    shared.write_text("mine (worktree)\n", encoding="utf-8")  # 내 세션이 이후 덮어씀
    assert _preflight(repo) == 0
    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    rc = t8_guard.main(["--repo", str(repo), "commit", "-m", "x", "--files", "shared.txt"])

    assert rc == 1
    assert "이미 staged" in capsys.readouterr().err
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head_before
    # 타세션 staged 내용이 index에 그대로 보존돼야 한다(add로 덮지 않음).
    staged_content = _git(repo, "show", ":shared.txt").stdout
    assert "theirs" in staged_content


def test_commit_partial_declared_rejected_before_commit(repo: Path, capsys) -> None:
    """4R P2: declared 중 일부만 변경돼 있으면(committed⊊declared 예정) 커밋 전 거부."""
    (repo / "changed.txt").write_text("changed\n", encoding="utf-8")
    assert _preflight(repo) == 0
    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    rc = t8_guard.main(
        ["--repo", str(repo), "commit", "-m", "x", "--files", "changed.txt", "base.txt"]
    )

    assert rc == 1  # base.txt 무변경 → 무효 스코프 — partial silent-PASS 금지
    assert "변경 없는" in capsys.readouterr().err
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head_before  # 커밋 미생성


def test_commit_korean_filename_post_verify(repo: Path) -> None:
    """독립 리뷰 P2: core.quotepath=false로 한글 경로 post-verify가 오탐하지 않는다."""
    (repo / "한글파일.txt").write_text("내용\n", encoding="utf-8")
    assert _preflight(repo) == 0

    rc = t8_guard.main(
        ["--repo", str(repo), "commit", "-m", "한글 커밋", "--files", "한글파일.txt"]
    )

    assert rc == 0
    committed = _git(repo, "-c", "core.quotepath=false", "show", "--name-only", "--format=", "HEAD").stdout
    assert "한글파일.txt" in committed


def test_preflight_works_inside_isolated_worktree(repo: Path, tmp_path: Path) -> None:
    """독립 리뷰 P2: linked worktree(.git=파일)에서도 snapshot이 실제 git dir에 저장된다."""
    _git(repo, "branch", "feature")
    dest = tmp_path / "linked"
    assert (
        t8_guard.main(["--repo", str(repo), "isolate", "--branch", "feature", "--dest", str(dest)])
        == 0
    )
    (dest / "in_wt.txt").write_text("wt\n", encoding="utf-8")

    rc = t8_guard.main(["--repo", str(dest), "preflight"])

    assert rc == 0
    assert not (dest / ".git" / "t8-snapshot.json").exists() or (dest / ".git").is_dir()
    # commit까지 동작(스냅샷 로드 포함)해야 진짜 통합 증명.
    rc2 = t8_guard.main(["--repo", str(dest), "commit", "-m", "wt", "--files", "in_wt.txt"])
    assert rc2 == 0


def test_commit_rejects_pathspec_magic(repo: Path, capsys) -> None:
    """독립 리뷰 2R P1: ':(top)pkg' 같은 pathspec magic이 편입 차단을 우회하면 안 된다."""
    sub = repo / "pkg"
    sub.mkdir()
    (sub / "theirs.txt").write_text("theirs\n", encoding="utf-8")
    _git(repo, "add", "pkg/theirs.txt")  # 타세션 staged
    assert _preflight(repo) == 0
    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    rc = t8_guard.main(["--repo", str(repo), "commit", "-m", "x", "--files", ":(top)pkg"])

    assert rc == 1  # 사전 거부 — 커밋 자체가 생성되지 않는다
    assert "magic" in capsys.readouterr().err
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head_before
    assert "pkg/theirs.txt" in _git(repo, "diff", "--cached", "--name-only").stdout  # staged 보존


def test_detached_head_is_blocked(repo: Path, capsys) -> None:
    """독립 리뷰 2R P2: detached HEAD에서 preflight/commit이 'HEAD'를 브랜치로 오인하면 안 된다."""
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "--detach", head)

    rc = t8_guard.main(["--repo", str(repo), "preflight"])

    assert rc == 2
    assert "detached" in capsys.readouterr().err


def test_corrupted_snapshot_is_blocked_not_traceback(repo: Path, capsys) -> None:
    """독립 리뷰 2R P2: 손상 snapshot은 traceback이 아니라 exit 2 계약으로 표면화."""
    (repo / "mine.txt").write_text("mine\n", encoding="utf-8")
    assert _preflight(repo) == 0
    git_dir = Path(_git(repo, "rev-parse", "--git-dir").stdout.strip())
    if not git_dir.is_absolute():
        git_dir = repo / git_dir
    (git_dir / "t8-snapshot.json").write_text("{corrupt", encoding="utf-8")

    rc = t8_guard.main(["--repo", str(repo), "commit", "-m", "x", "--files", "mine.txt"])

    assert rc == 2
    assert "snapshot 손상" in capsys.readouterr().err


def test_commit_rejects_deleted_tracked_directory(repo: Path, capsys) -> None:
    """독립 리뷰 3R P1: 삭제된 tracked 디렉토리 선언은 is_dir()로 안 잡힌다 — index 기준 사전 거부."""
    sub = repo / "pkg"
    sub.mkdir()
    (sub / "a.txt").write_text("a\n", encoding="utf-8")
    (sub / "b.txt").write_text("b\n", encoding="utf-8")
    _git(repo, "add", "pkg")
    _git(repo, "commit", "-m", "pkg in")
    import shutil

    shutil.rmtree(sub)  # 디렉토리 통째 삭제 — 워킹트리엔 없지만 index/HEAD엔 tracked
    assert _preflight(repo) == 0
    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    rc = t8_guard.main(["--repo", str(repo), "commit", "-m", "x", "--files", "pkg"])

    assert rc == 1  # 사전 거부 — 삭제 스윕 커밋이 생성되지 않는다
    assert "디렉토리" in capsys.readouterr().err
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head_before


def test_commit_blocked_when_other_session_committed_after_snapshot(
    repo: Path, capsys
) -> None:
    """독립 리뷰 3R P2: preflight 후 타세션이 커밋해 HEAD가 이동하면 stale base — fail-closed."""
    (repo / "mine.txt").write_text("mine\n", encoding="utf-8")
    assert _preflight(repo) == 0
    # 타세션 커밋 시뮬
    (repo / "foreign.txt").write_text("foreign\n", encoding="utf-8")
    _git(repo, "add", "foreign.txt")
    _git(repo, "commit", "-m", "foreign session commit")

    rc = t8_guard.main(["--repo", str(repo), "commit", "-m", "x", "--files", "mine.txt"])

    assert rc == 2
    assert "HEAD 이동" in capsys.readouterr().err


def test_sequential_guard_commits_refresh_snapshot(repo: Path) -> None:
    """내 가드 커밋 뒤에는 snapshot이 갱신돼 연속 커밋이 stale로 오탐되지 않는다."""
    (repo / "one.txt").write_text("1\n", encoding="utf-8")
    (repo / "two.txt").write_text("2\n", encoding="utf-8")
    assert _preflight(repo) == 0

    rc1 = t8_guard.main(["--repo", str(repo), "commit", "-m", "one", "--files", "one.txt"])
    rc2 = t8_guard.main(["--repo", str(repo), "commit", "-m", "two", "--files", "two.txt"])

    assert (rc1, rc2) == (0, 0)


def test_commit_accepts_absolute_path_inside_repo(repo: Path) -> None:
    """6R P1: repo 내부 절대경로 선언은 relative로 정규화돼 정상 커밋된다(사후 위반 아님)."""
    (repo / "mine.txt").write_text("mine\n", encoding="utf-8")
    assert _preflight(repo) == 0

    rc = t8_guard.main(
        ["--repo", str(repo), "commit", "-m", "abs", "--files", str(repo / "mine.txt")]
    )

    assert rc == 0
    assert "mine.txt" in _git(repo, "show", "--name-only", "--format=", "HEAD").stdout


def test_commit_rejects_absolute_path_outside_repo(repo: Path, tmp_path: Path, capsys) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("x\n", encoding="utf-8")
    assert _preflight(repo) == 0
    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    rc = t8_guard.main(["--repo", str(repo), "commit", "-m", "x", "--files", str(outside)])

    assert rc == 1
    assert "repo 밖" in capsys.readouterr().err
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head_before


def test_commit_canonicalizes_non_normal_relative_path(repo: Path) -> None:
    """7R P1: `dir/../new.txt` 같은 비정규 상대경로도 canonical화돼 정상 커밋(사후 위반 아님)."""
    sub = repo / "dir"
    sub.mkdir()
    (repo / "new.txt").write_text("n\n", encoding="utf-8")
    assert _preflight(repo) == 0

    rc = t8_guard.main(
        ["--repo", str(repo), "commit", "-m", "canon", "--files", "dir/../new.txt"]
    )

    assert rc == 0
    assert "new.txt" in _git(repo, "show", "--name-only", "--format=", "HEAD").stdout


def test_commit_rejects_relative_escape_outside_repo(repo: Path, capsys) -> None:
    assert _preflight(repo) == 0
    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    rc = t8_guard.main(["--repo", str(repo), "commit", "-m", "x", "--files", "../escape.txt"])

    assert rc == 1
    assert "repo 밖" in capsys.readouterr().err
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head_before


def test_commit_atomicity_violation_is_reported(repo: Path, monkeypatch, capsys) -> None:
    """5R P1: 체크→커밋 찰나에 타세션이 브랜치를 전환하면 사후 재검증이 위반을 보고한다(silent-PASS 금지)."""
    (repo / "mine.txt").write_text("mine\n", encoding="utf-8")
    assert _preflight(repo) == 0
    real_git = t8_guard._git

    def racing_git(r: Path, *args: str):
        if args and args[0] == "commit":
            # 사전 체크는 이미 통과한 뒤, git commit 직전에 타세션이 브랜치 전환(시뮬).
            real_git(r, "checkout", "-b", "hijack")
        return real_git(r, *args)

    monkeypatch.setattr(t8_guard, "_git", racing_git)

    rc = t8_guard.main(["--repo", str(repo), "commit", "-m", "x", "--files", "mine.txt"])

    assert rc == 1
    assert "원자성" in capsys.readouterr().err


def test_isolate_refuses_missing_branch(repo: Path, capsys) -> None:
    rc = t8_guard.main(["--repo", str(repo), "isolate", "--branch", "nope"])

    assert rc == 2  # 암묵 생성 금지(결정론)
    assert "브랜치 없음" in capsys.readouterr().err


def test_non_git_dir_is_blocked(tmp_path: Path, capsys) -> None:
    rc = t8_guard.main(["--repo", str(tmp_path), "preflight"])

    assert rc == 2
    assert "git repo 아님" in capsys.readouterr().err
