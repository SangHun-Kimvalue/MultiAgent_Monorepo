"""MM/ZRT v2 invariant checks.

Phase 4 범위의 파일시스템·문자열·git 사실만 검사한다.
문서나 리뷰 내용의 의미 품질은 판단하지 않는다.
"""
from __future__ import annotations

import ast
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.engine.static_review import run_subprocess_tool
from src.envelope import Verdict


M2_FIELDS = (
    "severity",
    "finding",
    "evidence_or_repro",
    "impact",
    "recommendation",
)

LESSON_RE = re.compile(r"^## LESSON-(\d{3}):", re.MULTILINE)
ROADMAP_NEXT_PHASE_RE = re.compile(r"## 3\. 다음 Phase 상세 .*?Phase\s+(\d+)")


@dataclass(frozen=True)
class ChangedPath:
    """git 변경 파일 1건."""

    path: str
    status: str
    untracked: bool = False


@dataclass(frozen=True)
class InvariantIssue:
    """M2 5-field issue."""

    severity: str
    finding: str
    evidence_or_repro: str
    impact: str
    recommendation: str

    def as_payload(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "finding": self.finding,
            "evidence_or_repro": self.evidence_or_repro,
            "impact": self.impact,
            "recommendation": self.recommendation,
        }


@dataclass(frozen=True)
class InvariantCheck:
    """개별 invariant 결과."""

    name: str
    status: Verdict
    issues: list[InvariantIssue] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "issues": [issue.as_payload() for issue in self.issues],
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class InvariantReport:
    """전체 invariant 실행 결과."""

    checks: list[InvariantCheck]
    changed_paths: list[ChangedPath]

    @property
    def verdict(self) -> Verdict:
        if any(check.status == Verdict.BLOCKED for check in self.checks):
            return Verdict.BLOCKED
        if any(check.status == Verdict.CHANGES_REQUESTED for check in self.checks):
            return Verdict.CHANGES_REQUESTED
        return Verdict.PASS

    def as_payload(self) -> dict[str, Any]:
        counts = {
            "pass": sum(1 for check in self.checks if check.status == Verdict.PASS),
            "changes_requested": sum(
                1 for check in self.checks
                if check.status == Verdict.CHANGES_REQUESTED
            ),
            "blocked": sum(
                1 for check in self.checks if check.status == Verdict.BLOCKED
            ),
        }
        return {
            "checks": [check.as_payload() for check in self.checks],
            "summary": {
                "verdict": self.verdict.value,
                "counts": counts,
                "changed_paths": [
                    {
                        "path": item.path,
                        "status": item.status,
                        "untracked": item.untracked,
                    }
                    for item in self.changed_paths
                ],
            },
        }


class InvariantEngine:
    """Phase 4 invariant runner."""

    def __init__(self, *, root: Path, since: str = "HEAD") -> None:
        self._root = root
        self._since = since
        self._git_path_prefix = self._resolve_git_path_prefix()

    async def run(self, *, paths: Sequence[str] | None = None) -> InvariantReport:
        changed_result = await self._changed_paths(paths=paths)
        if isinstance(changed_result, InvariantCheck):
            return InvariantReport(checks=[changed_result], changed_paths=[])
        changed = changed_result
        checks = [
            InvariantCheck(
                name="git_changed_paths",
                status=Verdict.PASS,
                evidence={"count": len(changed)},
            ),
            self._check_lessons(changed),
            self._check_roadmap_updated(changed),
            self._check_phase_prompt_exists(),
            self._check_m2_terms(changed),
            self._check_not_claimed_text(),
            self._check_runner_envelope_contract(),
        ]
        return InvariantReport(checks=checks, changed_paths=changed)

    async def _changed_paths(
        self,
        *,
        paths: Sequence[str] | None,
    ) -> list[ChangedPath] | InvariantCheck:
        commands = [
            ("git", "diff", "--name-status", self._since, "--"),
            ("git", "diff", "--cached", "--name-status", "--"),
            # --full-name: 서브디렉터리에서 실행해도 repo-root 상대 경로로 출력
            # (git diff와 base를 통일 — 안 하면 ls-files만 cwd 상대라 prefix 필터가 오작동).
            ("git", "ls-files", "--others", "--exclude-standard", "--full-name"),
        ]
        changed: list[ChangedPath] = []
        for command in commands:
            run = await run_subprocess_tool(command, cwd=self._root, timeout_s=10.0)
            if run.exit_code != 0:
                return _blocked(
                    "git_changed_paths",
                    run.stderr_sanitized or "git status failed",
                    "git changed path collection",
                )
            if command[1] == "ls-files":
                for line in run.stdout.splitlines():
                    if not line.strip():
                        continue
                    normalized = self._to_root_changed_path(
                        ChangedPath(path=line.strip(), status="A", untracked=True)
                    )
                    if normalized is not None:
                        changed.append(normalized)
            else:
                for item in _parse_name_status(run.stdout):
                    normalized = self._to_root_changed_path(item)
                    if normalized is not None:
                        changed.append(normalized)

        path_filter = {str(path).replace("\\", "/") for path in paths or []}
        if path_filter:
            changed = [
                item for item in changed
                if item.path in path_filter or any(
                    item.path.startswith(f"{prefix.rstrip('/')}/")
                    for prefix in path_filter
                )
            ]

        deduped: dict[str, ChangedPath] = {}
        for item in changed:
            deduped[item.path] = item
        return list(deduped.values())

    def _check_lessons(self, changed: Sequence[ChangedPath]) -> InvariantCheck:
        path = self._root / "docs" / "LESSONS_LEARNED.md"
        try:
            current = path.read_text(encoding="utf-8")
        except OSError as exc:
            return _blocked("lessons_append", str(exc), str(path))

        ids = [int(match) for match in LESSON_RE.findall(current)]
        issues: list[InvariantIssue] = []
        if len(ids) != len(set(ids)):
            issues.append(_issue(
                "LESSON 번호 중복",
                "docs/LESSONS_LEARNED.md",
                "교훈 ID 충돌은 후속 세션 라우팅을 흐립니다.",
                "중복 LESSON-NNN heading을 제거하거나 번호를 순차 조정하세요.",
            ))
        if ids != sorted(ids):
            issues.append(_issue(
                "LESSON 번호 역행",
                "docs/LESSONS_LEARNED.md",
                "append-only 교훈 기록의 시간 순서가 깨집니다.",
                "새 교훈을 파일 맨 아래에 증가 번호로 추가하세요.",
            ))

        old = self._git_show("docs/LESSONS_LEARNED.md")
        if isinstance(old, InvariantCheck):
            return old
        if old is not None and _is_changed(changed, "docs/LESSONS_LEARNED.md"):
            old_ids = [int(match) for match in LESSON_RE.findall(old)]
            if old_ids and ids[:len(old_ids)] != old_ids:
                issues.append(_issue(
                    "기존 LESSON heading 변경",
                    "docs/LESSONS_LEARNED.md",
                    "append-only 규칙 위반 가능성이 있습니다.",
                    "기존 교훈은 보존하고 새 LESSON만 맨 아래에 추가하세요.",
                ))

        return _check("lessons_append", issues, {"lesson_ids": ids})

    def _check_roadmap_updated(
        self,
        changed: Sequence[ChangedPath],
    ) -> InvariantCheck:
        # Phase 작업 신호는 특정 페이즈에 고정하지 않는다(화석화 방지).
        # ztr 코드 표면(engine/runner/envelope) 또는 페이즈 프롬프트 변경을
        # 일반적으로 Phase 작업으로 본다.
        def _is_phase_work(path: str) -> bool:
            return (
                path.startswith("src/engine/")
                or path == "src/runner.py"
                or path == "src/envelope.py"
                or path.startswith("src/config/")
                or path.startswith("docs/prompts/v2_phase")
            )

        phase_work = any(_is_phase_work(item.path) for item in changed)
        roadmap_changed = _is_changed(changed, "docs/ROADMAP_V2.md")
        issues: list[InvariantIssue] = []
        if phase_work and not roadmap_changed:
            issues.append(_issue(
                "Phase 작업 중 ROADMAP_V2.md 미갱신",
                "docs/ROADMAP_V2.md",
                "페이즈 상태와 다음 부착점 기록이 실제 변경분과 어긋날 수 있습니다.",
                "Phase 상태 또는 다음 Phase 상세을 ROADMAP_V2.md에 갱신하세요.",
            ))
        return _check(
            "roadmap_phase_record",
            issues,
            {"phase_work_detected": phase_work, "roadmap_changed": roadmap_changed},
        )

    def _check_phase_prompt_exists(self) -> InvariantCheck:
        roadmap = self._root / "docs" / "ROADMAP_V2.md"
        try:
            text = roadmap.read_text(encoding="utf-8")
        except OSError as exc:
            return _blocked("phase_prompt_exists", str(exc), str(roadmap))

        match = ROADMAP_NEXT_PHASE_RE.search(text)
        phase = int(match.group(1)) if match else 4
        prompts_dir = self._root / "docs" / "prompts"
        prompt_paths = sorted([
            *prompts_dir.glob(f"v2_phase{phase}.md"),
            *prompts_dir.glob(f"v2_phase{phase}_*.md"),
        ])
        prompt_display = (
            prompt_paths[0].relative_to(self._root).as_posix()
            if prompt_paths
            else f"docs/prompts/v2_phase{phase}.md"
        )
        issues: list[InvariantIssue] = []
        if not prompt_paths:
            issues.append(_issue(
                f"Phase {phase} 프롬프트 파일 없음",
                prompt_display,
                "다음 구현 세션이 cold-start 기준을 잃습니다.",
                f"docs/prompts/v2_phase{phase}*.md 파일을 추가하세요.",
            ))
        return _check(
            "phase_prompt_exists",
            issues,
            {"phase": phase, "paths": [
                path.relative_to(self._root).as_posix() for path in prompt_paths
            ]},
        )

    def _check_m2_terms(self, changed: Sequence[ChangedPath]) -> InvariantCheck:
        issues: list[InvariantIssue] = []
        for item in changed:
            if item.status == "D" or not _is_text_invariant_target(item.path):
                continue
            path = self._root / item.path
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                return _blocked("m2_terms", str(exc), item.path)
            if not _mentions_finding_contract(text):
                continue
            missing = [field for field in M2_FIELDS if field not in text]
            if missing:
                issues.append(_issue(
                    f"M2 finding 필드명 누락: {', '.join(missing)}",
                    item.path,
                    "finding 형식 안내가 부분적으로만 남아 호출자 해석이 흔들릴 수 있습니다.",
                    "severity/finding/evidence_or_repro/impact/recommendation 용어를 함께 유지하세요.",
                ))
        return _check("m2_finding_terms", issues, {"fields": list(M2_FIELDS)})

    def _check_not_claimed_text(self) -> InvariantCheck:
        files = [
            self._root / "docs" / "ROADMAP_V2.md",
            self._root / "docs" / "discovery" / "ztr-v2" / "validation_plan.md",
        ]
        evidence: dict[str, bool] = {}
        for path in files:
            try:
                evidence[str(path.relative_to(self._root))] = (
                    "NOT CLAIMED" in path.read_text(encoding="utf-8")
                )
            except OSError as exc:
                return _blocked("not_claimed_text", str(exc), str(path))
        issues = []
        if not any(evidence.values()):
            issues.append(_issue(
                "NOT CLAIMED 표기 없음",
                "docs/ROADMAP_V2.md",
                "미검증 항목을 PASS처럼 보이게 만들 위험이 있습니다.",
                "완료 보고 또는 검증 계획에 NOT CLAIMED 표기를 유지하세요.",
            ))
        return _check("not_claimed_text", issues, evidence)

    def _check_runner_envelope_contract(self) -> InvariantCheck:
        path = self._root / "src" / "runner.py"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            return _blocked("runner_envelope_contract", str(exc), str(path))

        issues: list[InvariantIssue] = []
        required_fragments = self._cmd_invariants_fragments(text)
        missing = [
            fragment for fragment, present in required_fragments.items()
            if not present
        ]
        if missing:
            issues.append(_issue(
                f"invariants Envelope 배선 누락: {', '.join(missing)}",
                "src/runner.py",
                "stdout 단일 envelope 계약을 호출자가 결정론적으로 확인할 수 없습니다.",
                "cmd_invariants에서 Envelope.as_stdout_payload()를 출력하도록 배선하세요.",
            ))
        return _check(
            "runner_envelope_contract",
            issues,
            {"required_fragments": required_fragments},
        )

    def _cmd_invariants_fragments(self, text: str) -> dict[str, bool]:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return {
                "cmd_invariants": False,
                "Envelope.from_verdict": False,
                "as_stdout_payload": False,
                "sys.exit": False,
                "add_parser(\"invariants\"": 'add_parser("invariants"' in text,
            }

        cmd_node = next(
            (
                node for node in tree.body
                if isinstance(node, ast.AsyncFunctionDef)
                and node.name == "cmd_invariants"
            ),
            None,
        )
        cmd_text = ast.get_source_segment(text, cmd_node) if cmd_node else ""
        if cmd_text is None:
            cmd_text = ""
        return {
            "cmd_invariants": cmd_node is not None,
            "Envelope.from_verdict": "Envelope.from_verdict" in cmd_text,
            "as_stdout_payload": "as_stdout_payload" in cmd_text,
            "sys.exit": "sys.exit" in cmd_text,
            "add_parser(\"invariants\"": 'add_parser("invariants"' in text,
        }

    def _git_show(self, path: str) -> str | None | InvariantCheck:
        git_path = self._to_git_path(path)
        try:
            proc = subprocess.run(
                ["git", "show", f"{self._since}:{git_path}"],
                cwd=self._root,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return _blocked(
                "lessons_append",
                str(exc),
                f"git show {self._since}:{git_path}",
            )
        if proc.returncode != 0:
            return None
        return proc.stdout

    def _to_root_changed_path(self, item: ChangedPath) -> ChangedPath | None:
        path = item.path.replace("\\", "/")
        prefix = self._git_path_prefix
        if prefix:
            if not path.startswith(f"{prefix}/"):
                # 모노레포 서브트리(runtimes/ztr) 밖 변경은 ztr invariants 대상이 아니다.
                # 필터링하지 않으면 self._root와 잘못 join돼 존재하지 않는 경로를 읽어 BLOCKED.
                return None
            path = path[len(prefix) + 1:]
        return ChangedPath(path=path, status=item.status, untracked=item.untracked)

    def _to_git_path(self, path: str) -> str:
        clean_path = path.replace("\\", "/")
        if not self._git_path_prefix:
            return clean_path
        return f"{self._git_path_prefix}/{clean_path}"

    def _resolve_git_path_prefix(self) -> str:
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=self._root,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if proc.returncode != 0:
            return ""
        git_root = Path(proc.stdout.strip()).resolve()
        try:
            relative = self._root.resolve().relative_to(git_root)
        except ValueError:
            return ""
        prefix = relative.as_posix()
        return "" if prefix == "." else prefix


def _parse_name_status(stdout: str) -> list[ChangedPath]:
    changed: list[ChangedPath] = []
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]
        path = parts[-1]
        changed.append(ChangedPath(path=path.replace("\\", "/"), status=status))
    return changed


def _is_changed(changed: Sequence[ChangedPath], path: str) -> bool:
    return any(item.path == path for item in changed)


def _is_text_invariant_target(path: str) -> bool:
    # M2 finding 계약은 docs(설계/프롬프트)와 .md에만 산다. tests/의 .py 픽스처는
    # 계약 문서가 아니므로 제외 — {'findings': []} 같은 픽스처가 5필드 누락으로
    # 위양성 CHANGES_REQUESTED를 내던 것을 차단(게이트 약화 아님, 오탐 제거).
    return path.startswith("docs/") or path.endswith(".md")


def _mentions_finding_contract(text: str) -> bool:
    needles = ("M2", "finding", "findings", "완료 보고", "검증")
    return any(needle in text for needle in needles)


def _check(
    name: str,
    issues: list[InvariantIssue],
    evidence: dict[str, Any] | None = None,
) -> InvariantCheck:
    status = Verdict.CHANGES_REQUESTED if issues else Verdict.PASS
    return InvariantCheck(name=name, status=status, issues=issues, evidence=evidence or {})


def _blocked(name: str, error: str, evidence: str) -> InvariantCheck:
    return InvariantCheck(
        name=name,
        status=Verdict.BLOCKED,
        issues=[
            _issue(
                f"{name} 검사 실행 불능",
                evidence,
                "인바리언트 검사 자체가 완료되지 않아 PASS를 주장할 수 없습니다.",
                error,
                severity="blocker",
            )
        ],
        evidence={"error": error, "path": evidence},
    )


def _issue(
    finding: str,
    evidence_or_repro: str,
    impact: str,
    recommendation: str,
    *,
    severity: str = "major",
) -> InvariantIssue:
    return InvariantIssue(
        severity=severity,
        finding=finding,
        evidence_or_repro=evidence_or_repro,
        impact=impact,
        recommendation=recommendation,
    )
