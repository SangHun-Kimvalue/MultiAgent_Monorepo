"""T8 멀티세션 worktree 조율 — 강제 메커니즘 CLI.

`MULTI_SESSION_WORKTREE_PROTOCOL.md` §3의 완료조건을 규율(사람이 지킴)에서
메커니즘(구조적으로 위반 불가)으로 승격한다. R5: 모든 판정은 git 사실
(HEAD/branch/porcelain/name-only)과 exit code만 사용한다.

exit code 계약:
  0 = PASS
  1 = 스코프/프로토콜 위반 (브랜치 전환·스코프 불일치 등 — 사람이 판단할 위반)
  2 = 전제/환경 실패 (스냅샷 없음·git 실패·worktree 충돌 — fail-closed)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

SNAPSHOT_NAME = "t8-snapshot.json"  # git-dir 내부 = 비추적, repo 무오염
_GLOB_MAGIC = ("*", "?", "[")  # pathspec 확장 문자 — 파일-한정 원칙과 충돌(독립 리뷰 P1)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # core.quotepath=false: 한글/특수문자 경로를 C-quote 없이 원문 UTF-8로 출력(독립 리뷰 P2).
    # --literal-pathspecs: ":(top)" 등 pathspec magic·glob 해석을 구조적으로 봉쇄(독립 리뷰 2R P1)
    # — declared 파일은 항상 리터럴 경로로만 취급된다.
    # diff.renames=false: post-verify(name-only)가 rename을 old(삭제)+new(추가) 두 경로로 표기
    # → rename은 old·new 둘 다 선언해야 한다(결정론 비교).
    return subprocess.run(
        ["git", "--literal-pathspecs", "-c", "core.quotepath=false", "-c", "diff.renames=false", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _git_or_die(repo: Path, *args: str) -> str:
    run = _git(repo, *args)
    if run.returncode != 0:
        print(f"T8_BLOCKED: git {' '.join(args)} failed: {run.stderr.strip()}", file=sys.stderr)
        raise SystemExit(2)
    return run.stdout.strip()


def _current_branch(repo: Path) -> str:
    branch = _git_or_die(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        # detached HEAD — 공유 worktree 작업은 브랜치 위에서만(§2). fail-closed(독립 리뷰 2R P2).
        print("T8_BLOCKED: detached HEAD — 브랜치 체크아웃 후 재실행", file=sys.stderr)
        raise SystemExit(2)
    return branch


def _git_dir(repo: Path) -> Path:
    """linked worktree의 `.git`은 파일이다 — 실제 git dir을 rev-parse로 해석(독립 리뷰 P2)."""
    raw = _git_or_die(repo, "rev-parse", "--git-dir")
    path = Path(raw)
    return path if path.is_absolute() else (repo / path).resolve()


def _snapshot_path(repo: Path) -> Path:
    return _git_dir(repo) / SNAPSHOT_NAME


def _staged_files(repo: Path) -> set[str]:
    return {
        line.strip()
        for line in _git_or_die(repo, "diff", "--cached", "--name-only").splitlines()
        if line.strip()
    }


def cmd_preflight(repo: Path) -> int:
    """§3① 시작 전 스냅샷: HEAD·브랜치·dirty 목록을 기록하고 표준출력으로 보고."""
    snapshot = _write_snapshot(repo)
    print(json.dumps(snapshot, ensure_ascii=False))
    dirty = snapshot["dirty"]
    if isinstance(dirty, list) and dirty:
        # 타세션 WIP 가능성 고지(차단 아님 — 소유 판단은 사람. §4 체크리스트 항목).
        print(f"T8_WARN: dirty files present ({len(dirty)}) — 소유 확인 후 진행", file=sys.stderr)
    return 0


def _write_snapshot(repo: Path) -> dict[str, object]:
    head_full = _git_or_die(repo, "rev-parse", "HEAD")
    branch = _current_branch(repo)
    dirty = [line for line in _git_or_die(repo, "status", "--short").splitlines() if line.strip()]
    snapshot: dict[str, object] = {
        "head": head_full[:9],
        "head_full": head_full,
        "branch": branch,
        "dirty": dirty,
    }
    _snapshot_path(repo).write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return snapshot


def _load_snapshot(repo: Path) -> dict[str, object]:
    snapshot_path = _snapshot_path(repo)
    if not snapshot_path.exists():
        print("T8_BLOCKED: snapshot 없음 — 먼저 `t8_guard preflight`를 실행하라(§3①)", file=sys.stderr)
        raise SystemExit(2)
    try:
        loaded = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        # 손상 snapshot도 전제 실패 = exit 2로 계약 통일(독립 리뷰 2R P2). traceback 유출 금지.
        print(f"T8_BLOCKED: snapshot 손상/읽기 실패 — preflight 재실행: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if not isinstance(loaded, dict):
        print("T8_BLOCKED: snapshot 형식 오류 — preflight 재실행", file=sys.stderr)
        raise SystemExit(2)
    return loaded


def cmd_commit(repo: Path, message: str, files: list[str]) -> int:
    """§3③ 파일-한정 커밋 강제.

    - preflight 스냅샷 필수(없으면 exit 2).
    - 스냅샷 이후 브랜치 전환 감지 시 거부(exit 1, §2 불변).
    - `git commit -- <files>`(pathspec-한정)로 커밋: 타세션이 index에 stage해 둔
      다른 파일은 **구조적으로 편입 불가**(위험모드 2)하고 staged 상태 그대로 보존(위험모드 1).
    - 커밋 후 `git show --name-only`로 스코프를 재검증(선언 외 파일 유입 시 exit 1).
    """
    snapshot = _load_snapshot(repo)
    branch = _current_branch(repo)
    if branch != snapshot.get("branch"):
        print(
            f"T8_VIOLATION: 브랜치 전환 감지 (snapshot={snapshot.get('branch')} → now={branch}) — "
            "공유 worktree에서 브랜치 전환 금지(§2)",
            file=sys.stderr,
        )
        return 1
    if not files:
        print("T8_VIOLATION: --files 필수 — 파일-한정 staging만 허용(§3③)", file=sys.stderr)
        return 1
    # P2(3R): preflight 이후 타세션이 같은 브랜치에 커밋했으면 snapshot 전제가 stale — fail-closed.
    head_now = _git_or_die(repo, "rev-parse", "HEAD")
    if head_now != snapshot.get("head_full"):
        print(
            f"T8_BLOCKED: HEAD 이동 감지 (snapshot={snapshot.get('head_full')} → now={head_now}) — "
            "타세션 커밋 가능성. preflight 재실행으로 최신 base를 실측한 뒤 재개(§3①)",
            file=sys.stderr,
        )
        return 2

    # P1(6R): 절대경로 선언은 post-verify 표현 불일치로 "커밋 후 위반"이 된다 — 커밋 전에
    # repo-relative로 정규화(밖이면 거부).
    declared: set[str] = set()
    for f in files:
        rel = _to_repo_relative(repo, f)
        if rel is None:
            print(f"T8_VIOLATION: repo 밖 경로 선언 금지: {f}", file=sys.stderr)
            return 1
        declared.add(rel)
    # P1: 디렉토리/glob pathspec은 타세션 staged를 쓸어담을 수 있다 — 사전 거부(사후 감지로는
    # 이미 잘못된 커밋이 만들어진 뒤라 늦다). 파일-한정 원칙은 문자 그대로 "구체 파일"만.
    for f in sorted(declared):
        if f.startswith(":") or any(ch in f for ch in _GLOB_MAGIC):
            # --literal-pathspecs가 구조 차단하지만, 의도 오류를 이른 시점에 명확히 거부(이중 방어).
            print(f"T8_VIOLATION: glob/pathspec magic 금지(구체 파일만): {f}", file=sys.stderr)
            return 1
        # P1(3R): 워킹트리 is_dir만으로는 "삭제된 tracked 디렉토리"를 못 잡는다 — index 기준 판정.
        # tracked == {f} = 구체 tracked 파일(삭제 커밋 포함 OK) / tracked ⊋ = 디렉토리 프리픽스 → 거부 /
        # untracked면 실존 정규 파일만 허용.
        tracked_under = {
            line.strip()
            for line in _git_or_die(repo, "ls-files", "--", f).splitlines()
            if line.strip()
        }
        if tracked_under and tracked_under != {f}:
            print(f"T8_VIOLATION: 디렉토리/프리픽스 선언 금지(구체 파일만): {f}", file=sys.stderr)
            return 1
        if not tracked_under and not (repo / f).is_file():
            print(f"T8_VIOLATION: 구체 파일 아님(미존재·비정규): {f}", file=sys.stderr)
            return 1
        # P2(4R): 변경 없는 파일 선언은 커밋에서 조용히 빠져 committed⊊declared silent-PASS가 된다
        # — 커밋 생성 전 무효 스코프로 거부.
        if not _git(repo, "status", "--porcelain", "--", f).stdout.strip():
            print(f"T8_VIOLATION: 변경 없는 파일 선언(무효 스코프): {f}", file=sys.stderr)
            return 1
    # P1: declared가 이미 index에 staged면 타세션 WIP일 수 있다 — add가 그 내용을 덮거나
    # 내 커밋에 편입시키므로 fail-closed 거부. 내 것이 맞으면 unstage 후 재실행.
    already_staged = declared & _staged_files(repo)
    if already_staged:
        print(
            f"T8_VIOLATION: 이미 staged인 파일 선언(타세션 WIP 가능): {sorted(already_staged)} — "
            "소유 확인 후 `git restore --staged`로 내려서 재실행",
            file=sys.stderr,
        )
        return 1

    # untracked 신규 파일은 pathspec 커밋이 못 집는다 → 선언 파일만 명시 add(파일-한정 staging, §3③).
    add_run = _git(repo, "add", "--", *sorted(declared))
    if add_run.returncode != 0:
        print(f"T8_BLOCKED: git add 실패: {add_run.stderr.strip()}", file=sys.stderr)
        return 2
    # pathspec-한정 커밋: 타세션이 stage해 둔 다른 파일은 구조적으로 편입 불가.
    # 선언 파일에 변경이 없으면 git이 거부 — 그 사실을 그대로 표면화.
    commit_run = _git(repo, "commit", "-m", message, "--", *sorted(declared))
    if commit_run.returncode != 0:
        # P2: add는 이미 성공했으므로 declared가 index에 남는다 — 다음 세션 관점 오염을 명시 보고.
        remnant = sorted(declared & _staged_files(repo))
        print(
            "T8_BLOCKED: git commit 실패: "
            f"{commit_run.stderr.strip() or commit_run.stdout.strip()}"
            + (f" | index 잔류 staged: {remnant}" if remnant else ""),
            file=sys.stderr,
        )
        return 2

    # P1(5R): 체크→커밋 사이 TOCTOU(타세션이 그 찰나 브랜치 전환)는 프로세스 수준 원자성이 없어
    # 사전 차단 불가 — 커밋 직후 branch/parent를 재검증해 위반을 정직 보고한다(silent-PASS 금지).
    # 구조적 차단이 필요한 동시운용은 §3④ isolate(물리 격리)가 정답.
    branch_after = _current_branch(repo)
    parent_after = _git_or_die(repo, "rev-parse", "HEAD~1")
    if branch_after != snapshot.get("branch") or parent_after != snapshot.get("head_full"):
        print(
            f"T8_VIOLATION: 커밋 원자성 위반 감지 — branch {snapshot.get('branch')}→{branch_after}, "
            f"parent {snapshot.get('head_full')}→{parent_after}. 커밋이 의도치 않은 base/브랜치에 "
            "생성됐을 수 있음: HEAD 확인 후 수동 정리 필요. 동시운용은 isolate(§3④)를 사용하라",
            file=sys.stderr,
        )
        return 1

    committed = {
        line.strip()
        for line in _git_or_die(repo, "show", "--name-only", "--format=", "HEAD").splitlines()
        if line.strip()
    }
    extra = committed - declared
    if extra:
        # 사전 거부(디렉토리/glob/staged 교집합)로 도달 불가가 정상이나, 도달하면 정직 보고(silent-PASS 금지).
        print(f"T8_VIOLATION: 선언 외 파일이 커밋에 유입: {sorted(extra)}", file=sys.stderr)
        return 1
    missing = declared - committed
    if missing:
        # 사전 무변경-거부로 도달 불가가 정상 — 도달 시 선언⊃커밋 불일치를 정직 보고(4R P2 양방향).
        print(f"T8_VIOLATION: 선언 파일이 커밋에서 누락: {sorted(missing)}", file=sys.stderr)
        return 1
    # 연속 가드 커밋이 가능하도록 snapshot을 새 HEAD로 갱신(내 커밋은 stale 아님 — 타세션 커밋만 감지).
    refreshed = _write_snapshot(repo)
    print(
        json.dumps({"committed": sorted(committed), "head": refreshed["head"]}, ensure_ascii=False)
    )
    return 0


def _normalize(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _to_repo_relative(repo: Path, path: str) -> str | None:
    """선언 경로를 canonical repo-relative POSIX 표현으로 정규화. repo 밖이면 None(거부).

    상대경로도 canonical화한다(7R P1: `dir/../new.txt` 같은 비정규 표현이 git 출력과
    불일치해 post-verify를 "커밋 후 위반"으로 만들던 것을 커밋 전에 봉쇄).
    """
    normalized = _normalize(path)
    candidate = Path(normalized)
    resolved = candidate.resolve() if candidate.is_absolute() else (repo / candidate).resolve()
    try:
        return resolved.relative_to(repo.resolve()).as_posix()
    except ValueError:
        return None


def cmd_isolate(repo: Path, branch: str, dest: Path | None) -> int:
    """§3④ 강제 격리: 별도 git worktree를 만들어 물리적으로 충돌 불가하게 한다.

    브랜치는 존재해야 한다(암묵 생성 없음 — 결정론). 이미 다른 worktree에
    체크아웃된 브랜치는 git이 거부하며 그 사실을 exit 2로 표면화한다.
    """
    branch_check = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    if branch_check.returncode != 0:
        print(f"T8_BLOCKED: 브랜치 없음: {branch} (암묵 생성 금지)", file=sys.stderr)
        return 2
    if dest is None:
        dest = repo.parent / f"{repo.name}-t8-{branch.replace('/', '-')}"
    add_run = _git(repo, "worktree", "add", str(dest), branch)
    if add_run.returncode != 0:
        print(f"T8_BLOCKED: worktree add 실패: {add_run.stderr.strip()}", file=sys.stderr)
        return 2
    print(json.dumps({"worktree": str(dest), "branch": branch}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="T8 멀티세션 worktree 조율 강제 CLI")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="대상 repo 루트")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("preflight", help="§3① 스냅샷 기록 + dirty 보고")

    p_commit = sub.add_parser("commit", help="§3③ 파일-한정 커밋 강제")
    p_commit.add_argument("-m", "--message", required=True)
    p_commit.add_argument("--files", nargs="+", required=True)

    p_isolate = sub.add_parser("isolate", help="§3④ worktree 격리")
    p_isolate.add_argument("--branch", required=True)
    p_isolate.add_argument("--dest", type=Path, default=None)

    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    if not (repo / ".git").exists():
        print(f"T8_BLOCKED: git repo 아님: {repo}", file=sys.stderr)
        return 2

    try:
        if args.command == "preflight":
            return cmd_preflight(repo)
        if args.command == "commit":
            return cmd_commit(repo, args.message, list(args.files))
        if args.command == "isolate":
            return cmd_isolate(repo, args.branch, args.dest)
    except SystemExit as exc:  # _git_or_die/_load_snapshot의 fail-closed를 반환 계약으로 통일
        return int(exc.code) if isinstance(exc.code, int) else 2
    return 2  # unreachable — argparse required=True


if __name__ == "__main__":
    raise SystemExit(main())
