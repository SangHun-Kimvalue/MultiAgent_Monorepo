"""cross-runtime(acp/ztr) 변경의 결정론 lint/type 게이트.

relay가 만든 runtime Python 변경을 각 runtime venv에서 직접 검사한다. LLM 판단이나
prose 해석 없이 exit code와 마지막 stdout JSON만 계약으로 사용한다(R5).

알려진 한계: 현재 acp mypy는 동시세션 WIP인 `orch_drivers.py` 4건 때문에 acp 대상이
포함되면 정직하게 FAIL(exit 1)할 수 있다. baseline/allowlist로 숨기지 않는다.

exit code:
  0 = PASS (대상 없음 포함, JSON에 skipped/no_targets 기록)
  1 = lint/type fail
  2 = 환경 실패 (venv/도구/git 실행 실패, OSError, timeout)

계약 각주: argparse usage 오류는 stdout JSON 없이 exit 2로 종료한다(argparse 기본) —
소비자는 "JSON 파싱 불가 = BLOCKED"로 취급하라. 그 외 모든 경로는 마지막 stdout
라인이 유효 JSON 한 줄이다(내부 예외 포함 — main이 BLOCKED JSON으로 수렴시킨다).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


@dataclass(frozen=True)
class RuntimeSpec:
    name: str
    prefix: str
    cwd: Path
    python: Path
    mypy_target: str


RUNTIMES: tuple[RuntimeSpec, ...] = (
    RuntimeSpec(
        name="acp",
        prefix="runtimes/acp/",
        cwd=Path("runtimes/acp"),
        python=Path("runtimes/acp/.venv/Scripts/python.exe"),
        mypy_target="acp",
    ),
    RuntimeSpec(
        name="ztr",
        prefix="runtimes/ztr/",
        cwd=Path("runtimes/ztr"),
        python=Path("runtimes/ztr/.venv/Scripts/python.exe"),
        mypy_target="src",
    ),
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _normalize(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _to_repo_relative(repo: Path, path: str) -> str | None:
    candidate = Path(path)
    resolved = candidate.resolve() if candidate.is_absolute() else (repo / candidate).resolve()
    try:
        return resolved.relative_to(repo.resolve()).as_posix()
    except ValueError:
        return None


def collect_changed_files(repo: Path, diff_base: str) -> tuple[list[str], str | None]:
    """tracked 변경 + untracked 파일을 repo-relative POSIX 경로로 수집한다."""
    diff = _git(repo, "diff", "--name-only", diff_base)
    if diff.returncode != 0:
        return [], f"git diff --name-only {diff_base} failed: {diff.stderr.strip()}"
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard")
    if untracked.returncode != 0:
        return [], f"git ls-files --others --exclude-standard failed: {untracked.stderr.strip()}"

    paths = [
        _normalize(line.strip())
        for line in [*diff.stdout.splitlines(), *untracked.stdout.splitlines()]
        if line.strip()
    ]
    return sorted(dict.fromkeys(paths)), None


def classify_targets(repo: Path, files: Sequence[str]) -> tuple[dict[str, list[str]], list[dict[str, str]]]:
    """runtime별 대상과 skip 사유를 결정한다. skip도 JSON에 남겨 silent fallback을 막는다."""
    targets: dict[str, list[str]] = {spec.name: [] for spec in RUNTIMES}
    skipped: list[dict[str, str]] = []
    for raw in files:
        rel = _to_repo_relative(repo, raw)
        if rel is None:
            skipped.append({"file": raw, "reason": "outside_repo"})
            continue
        rel = _normalize(rel)
        if not rel.endswith(".py"):
            skipped.append({"file": rel, "reason": "not_python"})
            continue
        matched = False
        for spec in RUNTIMES:
            if rel.startswith(spec.prefix):
                if not (repo / rel).is_file():
                    # 독립 리뷰 P2: 삭제된 .py가 diff에 남는다 — ruff E902(rc=1)로 lint FAIL
                    # 오분류되지 않게 lint 대상에서 분리·기록(조용한 누락 금지).
                    skipped.append({"file": rel, "reason": "missing_on_disk"})
                else:
                    targets[spec.name].append(rel)
                matched = True
                break
        if not matched:
            skipped.append({"file": rel, "reason": "outside_runtime"})
    return {key: sorted(dict.fromkeys(value)) for key, value in targets.items()}, skipped


def _tool_missing(run: subprocess.CompletedProcess[str], module: str) -> bool:
    combined = f"{run.stdout}\n{run.stderr}"
    return f"No module named {module}" in combined


def _run_subprocess(
    cmd: Sequence[str], cwd: Path, timeout: int
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(cmd),
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _compact(run: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return {
        "returncode": run.returncode,
        "stdout": run.stdout[-4000:],
        "stderr": run.stderr[-4000:],
    }


def _run_tool(
    repo: Path, spec: RuntimeSpec, module: str, args: Sequence[str], timeout: int
) -> tuple[int | None, dict[str, Any]]:
    python = repo / spec.python
    if not python.exists():
        return None, {"error": "missing_python", "path": str(python)}
    try:
        run = _run_subprocess(
            [str(python), "-m", module, *args],
            cwd=repo / spec.cwd,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return None, {"error": "timeout", "module": module, "timeout": timeout, "stderr": str(exc)}
    except OSError as exc:
        return None, {"error": "oserror", "module": module, "stderr": str(exc)}
    if _tool_missing(run, module):
        return None, {"error": "missing_tool", "module": module, **_compact(run)}
    return run.returncode, _compact(run)


def run_runtime_checks(
    repo: Path, targets: dict[str, list[str]], tool_timeout: int
) -> tuple[dict[str, dict[str, Any]], bool]:
    results: dict[str, dict[str, Any]] = {}
    blocked = False
    specs = {spec.name: spec for spec in RUNTIMES}
    for name, files in targets.items():
        if not files:
            results[name] = {"ruff": None, "mypy": None, "files": []}
            continue
        spec = specs[name]
        runtime_files = [str(Path(rel).relative_to(spec.cwd.as_posix())) for rel in files]

        ruff_rc, ruff_detail = _run_tool(repo, spec, "ruff", ["check", *runtime_files], tool_timeout)
        mypy_rc: int | None = None
        mypy_detail: dict[str, Any] = {}
        if ruff_rc is None:
            blocked = True
        else:
            mypy_rc, mypy_detail = _run_tool(repo, spec, "mypy", [spec.mypy_target], tool_timeout)
            if mypy_rc is None:
                blocked = True
        results[name] = {
            "ruff": ruff_rc,
            "mypy": mypy_rc,
            "files": files,
            "ruff_detail": ruff_detail,
            "mypy_detail": mypy_detail,
        }
    return results, blocked


def _status(results: dict[str, dict[str, Any]], blocked: bool) -> tuple[str, int]:
    if blocked:
        return "BLOCKED", 2
    failed = any(
        value.get("ruff") not in (None, 0) or value.get("mypy") not in (None, 0)
        for value in results.values()
    )
    if failed:
        return "FAIL", 1
    return "PASS", 0


def build_payload(
    status: str,
    repo: Path,
    targets: dict[str, list[str]],
    skipped: list[dict[str, str]],
    results: dict[str, dict[str, Any]],
    collection_error: str | None = None,
    mode: str = "diff",
) -> dict[str, Any]:
    target_count = sum(len(files) for files in targets.values())
    return {
        "status": status,
        "repo": str(repo),
        "mode": mode,
        "targets": targets,
        "skipped": skipped,
        "no_targets": target_count == 0,
        "runtimes": results,
        "collection_error": collection_error,
    }


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def main(argv: list[str] | None = None) -> int:
    # 독립 리뷰 P2: Windows 파이프 stdout 기본 인코딩(cp949)이 도구 출력의 유니코드로
    # UnicodeEncodeError→exit 1 오염을 만들 수 있다 — utf-8로 고정(프로젝트 규약).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="cross-runtime lint/type 결정론 게이트")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="repo 루트")
    parser.add_argument("--diff-base", default="HEAD", help="diff 기준 ref")
    parser.add_argument("--files", nargs="*", help="명시 대상 파일(빈 리스트=대상 없음, diff 폴백 안 함)")
    parser.add_argument("--tool-timeout", type=int, default=300, help="도구별 timeout 초")
    args = parser.parse_args(argv)

    repo = args.repo.resolve()
    empty_targets: dict[str, list[str]] = {spec.name: [] for spec in RUNTIMES}
    # 독립 리뷰 P2: --files가 주어졌으면 빈 리스트여도 명시 모드 — diff로 조용히
    # 폴백하면 "대상 없음" 의도가 "레포 전체 diff 검사(타 세션 WIP 포함)"가 된다.
    mode = "files" if args.files is not None else "diff"
    try:
        collection_error: str | None = None
        if args.files is not None:
            files = list(args.files)
        else:
            files, collection_error = collect_changed_files(repo, args.diff_base)

        if collection_error is not None:
            _emit(build_payload("BLOCKED", repo, empty_targets, [], {}, collection_error, mode))
            return 2

        targets, skipped = classify_targets(repo, files)
        results, blocked = run_runtime_checks(repo, targets, args.tool_timeout)
        status, rc = _status(results, blocked)
        _emit(build_payload(status, repo, targets, skipped, results, mode=mode))
        return rc
    except Exception as exc:
        # 독립 리뷰 P2: git 바이너리 부재(OSError) 등 어떤 내부 예외도 traceback/exit 1로
        # 새지 않고 BLOCKED JSON + 2로 수렴시킨다(fail-closed 계약).
        _emit(
            build_payload(
                "BLOCKED",
                repo,
                empty_targets,
                [],
                {},
                f"internal failure: {exc.__class__.__name__}: {exc}",
                mode=mode,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
