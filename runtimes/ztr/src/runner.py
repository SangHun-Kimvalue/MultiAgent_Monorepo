"""Zero-Token Roundtable 최소 CLI 엔트리포인트.

Usage:
    python -m src invoke --agent ollama-local --role manager --prompt "say hello"
    python -m src health --agent ollama-local
    python -m src list-agents
    python -m src list-agents --config path/to/config.yaml
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import io
import json
import logging
import math
import os
import shutil
import sys
import time
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, NamedTuple, Protocol, cast

# Windows UTF-8 stdout (한국어 출력 깨짐 방지)
if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

from src.agents.base import AgentRole, Prompt
from src.agents.discovery import discover_agents
from src.agents.ollama import OllamaAgent
from src.agents.registry import AgentRegistry
from src.config.loader import load_config
from src.config.schema import RoundtableConfig
from src.engine.static_review import (
    StaticReviewReport,
    collect_changed_paths,
    collect_git_diff,
    resolve_repo_root,
    run_static_review,
)
from src.engine.quality_gate import OutputQualityGate, QualityResult
from src.engine.post_merge_verifier import PostMergeVerifier, VerifyResult
from src.engine.invariants import InvariantEngine
from src.engine.phase_relay import PhaseRelay, PhaseRelayReport, RelayCommand
from src.engine.fix_feedback import (
    build_fix_resume_prompt,
    extract_findings,
    load_report_payload,
    select_accepted_findings,
)
from src.engine.reapply_ledger import (
    DEFAULT_MAX_ROUNDS,
    ReapplyLedger,
    command_digest,
    decide_terminal_state,
    final_verify_gate_check,
    findings_digest,
    gate_check,
    validate_max_rounds,
)
from src.engine.exec_adapter import RenderError, render_argv
from src.engine.event_emit import EmitError, build_orch_event, write_orch_event
from src.engine.goal_intent_ledger import (
    GoalIntentError,
    GoalIntentPreflight,
    canonical_json_line,
    capture_fingerprints,
    changed_fingerprints,
    persist_and_append,
    prepare_preflight,
)
from src.engine.resume_chain import (
    ResumeCoordinator,
    ResumeProfile,
    ResumeSpec,
    SessionMap,
    normalize_policy,
)
from src.engine.session_store import SessionStore
from src.envelope import (
    Envelope,
    INTERNAL_ERROR_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    Verdict,
    exit_code_for_verdict,
    redact_stderr,
)

# 에이전트 자동 디스커버리: agents/ 디렉토리의 모든 .py를 자동 import
# 새 에이전트 추가 시 runner.py 수정 불필요 (OCP 완전 달성)
discover_agents()

logger = logging.getLogger(__name__)


class RecordHandle(NamedTuple):
    """SessionStore 관측 기록 핸들."""

    store: SessionStore
    session_id: int


class FixRoundBlockedError(ValueError):
    """fix-round 입력/동일-thread 불변 위반으로 사람 에스컬레이션이 필요한 오류."""


class TargetInputError(ValueError):
    """review/verify target 선택 또는 사전조건 입력 오류."""


class PhaseProjectionBlockedError(RuntimeError):
    """Opt-in phase manifest/report 배선 실패."""


class InitManifestFn(Protocol):
    """methodology.phase.manifest.init_manifest 계약."""

    def __call__(self, path: Path, phase_id: str, base_sha: str) -> int: ...


class AppendEntryFn(Protocol):
    """methodology.phase.manifest.append_entry 계약."""

    def __call__(
        self, path: Path, entry: dict[str, Any], dry_run: bool = False
    ) -> int: ...


class ProjectReportFn(Protocol):
    """methodology.phase.report.project_report 계약."""

    def __call__(self, manifest: Path, out: Path) -> int: ...


class TargetSelection(NamedTuple):
    """target 표현과 실행 기준을 입력 모드별로 함께 고정한다."""

    paths: tuple[str, ...]
    execution_root: Path
    git_backed: bool


async def _candidate_digest(base_sha: str, *, cwd: Path | None = None) -> tuple[str, Path]:
    """tracked diff와 정렬된 untracked 내용 스냅샷을 SHA-256으로 묶는다."""
    if not isinstance(base_sha, str) or not base_sha.strip():
        raise FixRoundBlockedError("--base-sha는 비어 있지 않은 값이어야 합니다")
    process_cwd = cwd or Path.cwd()
    try:
        repo_root = await resolve_repo_root(cwd=process_cwd)
    except Exception as exc:
        raise FixRoundBlockedError(f"candidate repository root 확인 실패: {exc}") from exc
    git = shutil.which("git")
    if git is None:
        raise FixRoundBlockedError("git 실행 파일을 찾을 수 없습니다")
    try:
        diff_proc = await asyncio.create_subprocess_exec(
            git,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            base_sha,
            cwd=str(repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        diff_stdout_b, diff_stderr_b = await asyncio.wait_for(
            diff_proc.communicate(), timeout=30.0
        )
    except TimeoutError as exc:
        if "diff_proc" in locals() and diff_proc.returncode is None:
            diff_proc.kill()
            await diff_proc.communicate()
        raise FixRoundBlockedError("candidate git diff가 30초 안에 끝나지 않았습니다") from exc
    except OSError as exc:
        raise FixRoundBlockedError(f"candidate git diff 실행 실패: {exc}") from exc
    if diff_proc.returncode != 0:
        detail = diff_stderr_b.decode("utf-8", errors="replace").strip()
        raise FixRoundBlockedError(detail or "candidate git diff가 실패했습니다")

    try:
        untracked_proc = await asyncio.create_subprocess_exec(
            git,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            cwd=str(repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        untracked_stdout_b, untracked_stderr_b = await asyncio.wait_for(
            untracked_proc.communicate(), timeout=30.0
        )
    except TimeoutError as exc:
        if "untracked_proc" in locals() and untracked_proc.returncode is None:
            untracked_proc.kill()
            await untracked_proc.communicate()
        raise FixRoundBlockedError(
            "candidate untracked 목록 확인이 30초 안에 끝나지 않았습니다"
        ) from exc
    except OSError as exc:
        raise FixRoundBlockedError(f"candidate untracked 목록 확인 실패: {exc}") from exc
    if untracked_proc.returncode != 0:
        detail = untracked_stderr_b.decode("utf-8", errors="replace").strip()
        raise FixRoundBlockedError(detail or "candidate untracked 목록 확인이 실패했습니다")

    untracked_paths = sorted(
        path_bytes for path_bytes in untracked_stdout_b.split(b"\0") if path_bytes
    )
    digest = hashlib.sha256()

    def update_record(record_type: bytes, payload: bytes) -> None:
        digest.update(len(record_type).to_bytes(8, "big"))
        digest.update(record_type)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)

    update_record(b"tracked-diff", diff_stdout_b)
    update_record(b"untracked-count", len(untracked_paths).to_bytes(8, "big"))
    try:
        for path_bytes in untracked_paths:
            relative_path = path_bytes.decode("utf-8", errors="surrogateescape")
            candidate_path = repo_root / relative_path
            update_record(b"untracked-path", path_bytes)
            if candidate_path.is_symlink():
                update_record(
                    b"untracked-symlink-target",
                    os.fsencode(os.readlink(candidate_path)),
                )
            else:
                update_record(b"untracked-file-content", candidate_path.read_bytes())
    except (OSError, UnicodeError) as exc:
        raise FixRoundBlockedError(f"candidate untracked 내용 확인 실패: {exc}") from exc
    return digest.hexdigest(), repo_root


def _verification_command_records(
    args: argparse.Namespace,
) -> list[tuple[str, str, bool, float | None]]:
    """implementer/autofix를 제외한 치환 전 gating 명령 사실을 보존한다."""
    timeouts = _validated_leg_timeout_overrides(args)
    records: list[tuple[str, str, bool, float | None]] = []
    for name, attribute, timeout_name in (
        ("mechanical-review", "mechanical_cmd", "mechanical"),
        ("test", "test_cmd", "test"),
        ("implementer-reviewer", "reviewer_cmd", "reviewer"),
    ):
        value = getattr(args, attribute, "")
        if value:
            records.append((name, value, True, timeouts[timeout_name]))
    return records


def _verification_commands_from_args(args: argparse.Namespace) -> list[RelayCommand]:
    """candidate를 바꾸지 않는 mechanical/test/reviewer 명령만 만든다."""
    timeouts = _validated_leg_timeout_overrides(args)
    commands: list[RelayCommand] = []
    if getattr(args, "mechanical_cmd", ""):
        commands.append(RelayCommand.from_text(
            name="mechanical-review",
            value=args.mechanical_cmd,
            timeout_s=timeouts["mechanical"],
        ))
    if getattr(args, "test_cmd", ""):
        commands.append(RelayCommand.from_text(
            name="test",
            value=args.test_cmd,
            timeout_s=timeouts["test"],
        ))
    if getattr(args, "reviewer_cmd", ""):
        commands.append(RelayCommand.from_text(
            name="implementer-reviewer",
            value=args.reviewer_cmd,
            verdict_source=getattr(args, "reviewer_verdict_source", "stdout_token"),
            timeout_s=timeouts["reviewer"],
        ))
    return commands


def _apply_focused_commands(
    args: argparse.Namespace,
    commands: list[RelayCommand],
) -> tuple[list[RelayCommand], bool]:
    """호출자가 명시한 focused 값만 해당 command argv로 치환한다."""
    replacements = (
        ("focused_mechanical_cmd", "mechanical-review"),
        ("focused_test_cmd", "test"),
    )
    updated = list(commands)
    focused = False
    for attribute, target_name in replacements:
        value = getattr(args, attribute, None)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise FixRoundBlockedError(f"--{attribute.replace('_', '-')} 값이 비어 있습니다")
        target_index = next(
            (index for index, command in enumerate(updated) if command.name == target_name),
            None,
        )
        if target_index is None:
            raise FixRoundBlockedError(f"focused 치환 대상 {target_name} leg가 없습니다")
        current = updated[target_index]
        replacement = RelayCommand.from_text(
            name=current.name,
            value=value,
            verdict_source=current.verdict_source,
            gating=current.gating,
            timeout_s=current.timeout_s,
        )
        updated[target_index] = replace(replacement, cwd=current.cwd)
        focused = True
    return updated, focused


def _gating_leg_facts(report: PhaseRelayReport) -> list[dict[str, Any]]:
    """자연어 없이 relay의 구조화 step 필드만 추출한다."""
    return [
        {
            "name": step.name,
            "status": step.status.value,
            "skipped": step.skipped,
        }
        for step in report.steps
        if step.gating
    ]


def _verification_result_error(
    report: PhaseRelayReport,
    commands: list[RelayCommand],
) -> str | None:
    expected = [command.name for command in commands]
    actual = [step.name for step in report.steps if step.gating]
    if actual != expected:
        return "final-verify gating step 결과가 명령셋과 일치하지 않습니다"
    if any(step.skipped for step in report.steps if step.gating):
        return "final-verify gating step 결과에 skipped가 있습니다"
    return None


async def cmd_invoke(args: argparse.Namespace) -> None:
    """에이전트에 프롬프트를 전송하고 응답을 출력한다."""
    config = load_config(args.config)
    AgentRegistry.discover()

    agent_cfg = config.get_agent(args.agent)
    if agent_cfg is None:
        print(f"에이전트를 찾을 수 없습니다: {args.agent}", file=sys.stderr)
        sys.exit(1)

    if not agent_cfg.enabled:
        print(f"에이전트가 비활성 상태입니다: {args.agent}", file=sys.stderr)
        sys.exit(1)

    try:
        agent = await AgentRegistry.create(
            agent_id=agent_cfg.id,
            agent_type=agent_cfg.type,
            config={"roles": agent_cfg.roles, **agent_cfg.config},
        )
    except (ValueError, FileNotFoundError) as exc:
        print(f"에이전트 생성 실패: {exc}", file=sys.stderr)
        sys.exit(1)

    prompt = Prompt(content=args.prompt, role=AgentRole(args.role))
    response = await agent.invoke(prompt)

    output: dict[str, Any] = {
        "agent_id": response.agent_id,
        "success": response.success,
        "latency_ms": round(response.latency_ms, 1),
        "tokens_used": response.tokens_used,
    }

    if response.success:
        output["content"] = response.content[:2000]
    else:
        output["error"] = response.error[:500]

    print(json.dumps(output, ensure_ascii=False, indent=2))
    await AgentRegistry.shutdown_all()


async def cmd_health(args: argparse.Namespace) -> None:
    """에이전트 헬스체크를 수행한다."""
    config = load_config(args.config)
    AgentRegistry.discover()

    agent_cfg = config.get_agent(args.agent)
    if agent_cfg is None:
        print(f"에이전트를 찾을 수 없습니다: {args.agent}", file=sys.stderr)
        sys.exit(1)

    try:
        agent = await AgentRegistry.create(
            agent_id=agent_cfg.id,
            agent_type=agent_cfg.type,
            config={"roles": agent_cfg.roles, **agent_cfg.config},
        )
    except (ValueError, FileNotFoundError) as exc:
        print(f"에이전트 생성 실패: {exc}", file=sys.stderr)
        sys.exit(1)

    status = await agent.health_check()
    status_emoji = {
        "healthy": "HEALTHY",
        "degraded": "DEGRADED",
        "quarantined": "QUARANTINED",
    }
    print(f"{args.agent}: {status_emoji.get(status.value, status.value)}")
    await AgentRegistry.shutdown_all()


def cmd_history(args: argparse.Namespace) -> None:
    """최근 세션 기록을 출력한다."""
    from src.engine.session_store import SessionStore

    config = load_config(args.config)
    store = SessionStore(config.session.db_path)

    sessions = store.list_sessions(limit=args.limit)
    if not sessions:
        print("  기록된 세션이 없습니다.")
        store.close()
        return

    print(f"\n  {'ID':>4}  {'STATUS':<10} {'VERDICT':<12} {'ROUNDS':>6}  {'TASK':<30} {'STARTED'}")
    print(f"  {'─'*4}  {'─'*10} {'─'*12} {'─'*6}  {'─'*30} {'─'*19}")

    for s in sessions:
        task = (s["task"] or "")[:30]
        started = (s["started_at"] or "")[:19]
        verdict = s["final_verdict"] or "-"
        print(
            f"  {s['id']:>4}  {s['status']:<10} {verdict:<12} "
            f"{s['rounds'] or 0:>6}  {task:<30} {started}"
        )

    print()
    store.close()


def cmd_stats(args: argparse.Namespace) -> None:
    """에이전트별 통계를 출력한다."""
    from src.engine.session_store import SessionStore

    config = load_config(args.config)
    store = SessionStore(config.session.db_path)

    stats = store.agent_stats()
    if not stats:
        print("  기록된 메트릭이 없습니다.")
        store.close()
        return

    print(f"\n  {'AGENT':<22} {'CALLS':>6} {'SUCCESS':>8} {'AVG_MS':>10} {'TOKENS':>10}")
    print(f"  {'─'*22} {'─'*6} {'─'*8} {'─'*10} {'─'*10}")

    for s in stats:
        print(
            f"  {s['agent_id']:<22} {s['total_calls']:>6} "
            f"{s['success_rate']:>7.1f}% "
            f"{s['avg_latency_ms']:>9.0f} "
            f"{s['total_tokens']:>10}"
        )

    print()
    store.close()


def cmd_feedback(args: argparse.Namespace) -> None:
    """세션에 사용자 피드백을 기록한다."""
    from src.engine.session_store import SessionStore

    config = load_config(args.config)
    store = SessionStore(config.session.db_path)

    session = store.get_session(args.session)
    if not session:
        print(f"  세션을 찾을 수 없습니다: #{args.session}", file=sys.stderr)
        store.close()
        sys.exit(1)

    if args.override:
        store.set_feedback(args.session, feedback="override", override_verdict=args.override)
        print(f"  Session #{args.session}: override -> {args.override}")
    elif args.disagree:
        store.set_feedback(args.session, feedback="disagree")
        print(f"  Session #{args.session}: disagree (Critic 판정: {session['final_verdict']})")
    else:
        store.set_feedback(args.session, feedback="agree")
        print(f"  Session #{args.session}: agree (Critic 판정: {session['final_verdict']})")

    # 누적 통계 표시
    fb_stats = store.feedback_stats()
    print(f"\n  피드백 통계: {fb_stats['total']}건 중 "
          f"동의 {fb_stats['agrees']}건 ({fb_stats['agree_rate']}%), "
          f"반대 {fb_stats['disagrees']}건, "
          f"오버라이드 {fb_stats['overrides']}건")
    store.close()


async def cmd_list(args: argparse.Namespace) -> None:
    """설정된 에이전트 목록을 출력한다."""
    config = load_config(args.config)

    print(f"\n  {'ID':<22} {'TYPE':<22} {'PRI':>4}  {'STATUS':<8} ROLES")
    print(f"  {'─'*22} {'─'*22} {'─'*4}  {'─'*8} {'─'*20}")

    for a in config.agents:
        status = "활성" if a.enabled else "비활성"
        roles = ", ".join(a.roles)
        print(f"  {a.id:<22} {a.type:<22} {a.priority:>4}  {status:<8} {roles}")
    print()


async def cmd_review(args: argparse.Namespace) -> None:
    """Phase 2 mechanical review를 실행하고 단일 Envelope JSON을 출력한다."""
    start = time.monotonic()
    cwd = Path.cwd()
    record_handle: RecordHandle | None = None
    try:
        config = load_config(getattr(args, "config", None))
        selection = await _resolve_review_targets(args, cwd=cwd)
        targets = list(selection.paths)
        if args.verbose:
            print(f"review targets: {targets or ['<ruff-skip>']}", file=sys.stderr)
        record_handle = _start_recording(
            args,
            config=config,
            task="ztr review",
            target_file=", ".join(targets) if targets else "<ruff-skip>",
            metadata={"command": "review", "targets": targets},
        )

        report = await run_static_review(
            targets,
            cwd=selection.execution_root,
            mypy_cwd=_runtime_root(),
            timeout_s=float(args.timeout),
        )
        diff_text = (
            await collect_git_diff(targets, cwd=selection.execution_root)
            if selection.git_backed and targets
            else ""
        )
        review_text, not_claimed = await _invoke_optional_ollama_review(
            config,
            report=report,
            diff_text=diff_text,
        )

        role_binding = config.get_role_binding("mechanical")
        backend = role_binding.backend if role_binding is not None else "internal"
        model = role_binding.model if role_binding is not None else "static-review"
        # Phase 9: 트리 정리를 보장 못 했으면 **외부 소비자가 보게** 흘린다.
        # ⚠ `Envelope.not_claimed`(제약 없는 list[str])이지 `GoalIntentContext.not_claimed`
        # (선언된 claim ID 전용)가 아니다 — 이름만 같고 계약이 다르다.
        envelope = report.as_envelope(
            backend=backend,
            model=model,
            duration_s=time.monotonic() - start,
            review_text=review_text,
            fallback_used=False,
            not_claimed=[*not_claimed, *report.cleanup_tokens()],
        )
    except TargetInputError as exc:
        envelope = _review_blocked_envelope(
            start=start,
            exc=exc,
            exit_code=exit_code_for_verdict(Verdict.BLOCKED),
        )
    except Exception as exc:
        envelope = _review_blocked_envelope(
            start=start,
            exc=exc,
            exit_code=INTERNAL_ERROR_EXIT_CODE,
        )

    _finish_recording(record_handle, envelope, rounds=1)
    print(json.dumps(envelope.as_stdout_payload(), ensure_ascii=False))
    sys.exit(envelope.exit_code)


async def cmd_gate(args: argparse.Namespace) -> None:
    """구조화된 Writer/Critic result JSON을 H4 품질 게이트로 검사한다."""
    start = time.monotonic()
    try:
        input_path = Path(args.result_file)
        data = _load_gate_input(input_path)
        kind = data["kind"]
        gate = OutputQualityGate()
        if kind == "critic":
            shape_issues = _critic_m2_shape_issues(data["findings"])
            result = gate.validate_critic(
                findings=_normalise_gate_findings(data["findings"]),
                verdict=str(data["verdict"]).lower(),
            )
        else:
            shape_issues = []
            result = gate.validate_writer(
                code=str(data["content"]),
                task=str(data.get("task", input_path.name)),
            )

        all_issue_texts = [*shape_issues, *result.issues]
        gate_result = QualityResult(
            passed=result.passed and not shape_issues,
            issues=all_issue_texts,
            retry_requested=result.retry_requested or bool(shape_issues),
        )
        issues = [_gate_issue_payload(issue) for issue in gate_result.issues]
        status = Verdict.PASS if gate_result.passed else Verdict.CHANGES_REQUESTED
        payload = _gate_payload(
            input_path=input_path,
            data=data,
            result=gate_result,
            issues=issues,
        )
        envelope = Envelope.from_verdict(
            status=status,
            backend="internal",
            model="output-quality-gate",
            duration_s=time.monotonic() - start,
            stdout=json.dumps(payload, ensure_ascii=False),
        )
    except Exception as exc:
        payload = {
            "gate": {
                "passed": False,
                "retry_requested": False,
                "issues_count": 0,
            },
            "issues": [],
            "input": {"path": str(getattr(args, "result_file", ""))},
        }
        envelope = Envelope.from_verdict(
            status=Verdict.BLOCKED,
            backend="internal",
            model="output-quality-gate",
            duration_s=time.monotonic() - start,
            stdout=json.dumps(payload, ensure_ascii=False),
            stderr=str(exc),
        )

    print(json.dumps(envelope.as_stdout_payload(), ensure_ascii=False))
    sys.exit(envelope.exit_code)


async def cmd_verify(args: argparse.Namespace) -> None:
    """H6 post-merge verifier를 실행하고 Envelope JSON을 출력한다."""
    start = time.monotonic()
    record_handle: RecordHandle | None = None
    rounds = 0
    try:
        config = load_config(getattr(args, "config", None))
        if not args.post_merge:
            raise TargetInputError("현재 verify는 --post-merge 모드만 지원합니다")
        cwd = Path.cwd()
        selection = await _resolve_verify_targets(args, cwd=cwd)
        targets = list(selection.paths)
        if not targets:
            raise TargetInputError("verify 대상이 없습니다")
        rounds = len(targets)
        record_handle = _start_recording(
            args,
            config=config,
            task="ztr verify --post-merge",
            target_file=", ".join(targets),
            metadata={"command": "verify", "post_merge": True, "targets": targets},
        )

        verifier = PostMergeVerifier(timeout_s=float(args.timeout))
        results: list[tuple[str, VerifyResult]] = []
        for target in targets:
            execution_target = (
                str(selection.execution_root / target)
                if selection.git_backed
                else target
            )
            result = await verifier.verify(execution_target)
            results.append((target, result))

        blocked = any(result.blocked or result.timed_out for _, result in results)
        failed = any(not result.passed for _, result in results)
        if blocked:
            status = Verdict.BLOCKED
        elif failed:
            status = Verdict.CHANGES_REQUESTED
        else:
            status = Verdict.PASS

        payload = _verify_payload(results)
        envelope = Envelope.from_verdict(
            status=status,
            backend="internal",
            model="post-merge-verifier",
            duration_s=time.monotonic() - start,
            stdout=json.dumps(payload, ensure_ascii=False),
        )
    except TargetInputError as exc:
        envelope = Envelope.from_verdict(
            status=Verdict.BLOCKED,
            backend="internal",
            model="post-merge-verifier",
            duration_s=time.monotonic() - start,
            stdout=json.dumps(_verify_blocked_payload(), ensure_ascii=False),
            stderr=str(exc),
        )
    except Exception as exc:
        envelope = Envelope(
            status=Verdict.BLOCKED,
            exit_code=INTERNAL_ERROR_EXIT_CODE,
            backend="internal",
            model="post-merge-verifier",
            duration_s=time.monotonic() - start,
            stdout=json.dumps(_verify_blocked_payload(), ensure_ascii=False),
            stderr_sanitized=redact_stderr(str(exc)),
            fallback_used=False,
            not_claimed=[],
        )

    _finish_recording(record_handle, envelope, rounds=rounds)
    print(json.dumps(envelope.as_stdout_payload(), ensure_ascii=False))
    sys.exit(envelope.exit_code)


async def cmd_invariants(args: argparse.Namespace) -> None:
    """MM/ZRT invariant 사실 검사를 실행하고 Envelope JSON을 출력한다."""
    start = time.monotonic()
    try:
        engine = InvariantEngine(root=Path.cwd(), since=args.since)
        report = await engine.run(paths=args.paths)
        envelope = Envelope.from_verdict(
            status=report.verdict,
            backend="internal",
            model="invariants",
            duration_s=time.monotonic() - start,
            stdout=json.dumps(report.as_payload(), ensure_ascii=False),
        )
    except Exception as exc:
        payload = {
            "checks": [],
            "summary": {
                "verdict": Verdict.BLOCKED.value,
                "counts": {"pass": 0, "changes_requested": 0, "blocked": 1},
                "changed_paths": [],
            },
        }
        envelope = Envelope.from_verdict(
            status=Verdict.BLOCKED,
            backend="internal",
            model="invariants",
            duration_s=time.monotonic() - start,
            stdout=json.dumps(payload, ensure_ascii=False),
            stderr=str(exc),
        )

    print(json.dumps(envelope.as_stdout_payload(), ensure_ascii=False))
    sys.exit(envelope.exit_code)


async def _resolve_goal_intent_repo_root(process_cwd: Path) -> Path:
    """T13 opt-in root lookup 실패를 닫힌 writer taxonomy로 정규화한다."""
    try:
        return await resolve_repo_root(cwd=process_cwd)
    except Exception as exc:
        raise GoalIntentError(
            "GOAL_INTENT_CONTEXT_INVALID", "repository root resolution failed"
        ) from exc


async def cmd_run_phase(args: argparse.Namespace) -> None:
    """Phase 5 deterministic relay를 실행하고 Envelope JSON을 출력한다."""
    start = time.monotonic()
    record_handle: RecordHandle | None = None
    rounds = 0
    goal_preflight: GoalIntentPreflight | None = None
    emitted_bytes: bytes | None = None
    try:
        config = load_config(getattr(args, "config", None))
        phase_paths = _init_phase_projection(args)
        if getattr(args, "goal_intent_context_file", None):
            process_cwd = Path.cwd()
            repo_root = await _resolve_goal_intent_repo_root(process_cwd)
            goal_preflight = await prepare_preflight(
                args,
                kind="run-phase",
                config=config,
                process_cwd=process_cwd,
                repo_root=repo_root,
            )
        commands = _relay_commands_from_args(args)
        resume_coordinator = _resume_coordinator_from_args(args)
        if goal_preflight is not None:
            goal_preflight.assert_inputs_unchanged()
        record_handle = _start_recording(
            args,
            config=config,
            task=f"ztr run-phase {args.phase_id}",
            target_file=str(args.prompt_file),
            metadata={
                "command": "run-phase",
                "phase_id": args.phase_id,
                "prompt_file": str(args.prompt_file),
                "legs": [command.name for command in commands],
                "session_map": getattr(args, "session_map", "") or None,
                "resume": {
                    "implementer": getattr(args, "implementer_resume", "new"),
                    "reviewer": getattr(args, "reviewer_resume", "new"),
                    "implementer_profile": getattr(
                        args,
                        "implementer_resume_profile",
                        "none",
                    ),
                    "reviewer_profile": getattr(
                        args,
                        "reviewer_resume_profile",
                        "none",
                    ),
                },
            },
        )
        before_fingerprints: dict[str, str] | None = None
        if goal_preflight is not None:
            goal_preflight.assert_inputs_unchanged()
            before_fingerprints = await capture_fingerprints(
                goal_preflight.repo_root,
                excluded_paths=goal_preflight.excluded_paths,
            )
        report = await _run_phase_once(
            args,
            commands=commands,
            resume_coordinator=resume_coordinator,
        )
        if phase_paths is not None:
            _complete_phase_projection(report, *phase_paths)
        after_fingerprints: dict[str, str] | None = None
        if goal_preflight is not None:
            after_fingerprints = await capture_fingerprints(
                goal_preflight.repo_root,
                excluded_paths=goal_preflight.excluded_paths,
            )
        rounds = len(report.steps)
        envelope = Envelope(
            status=report.status,
            exit_code=report.exit_code,
            backend="phase-relay",
            model="external-cli",
            duration_s=time.monotonic() - start,
            stdout=json.dumps(report.as_payload(), ensure_ascii=False),
            stderr_sanitized="",
            fallback_used=report.resume_fallback_used,
            # Phase 9: 정리 열화를 외부 경계까지 흘린다(출력 R1 P1).
            not_claimed=["full-e2e", *report.cleanup_tokens()],
        )
        if goal_preflight is not None:
            if before_fingerprints is None or after_fingerprints is None:
                raise GoalIntentError("GOAL_INTENT_BASELINE_FAILED")
            goal_preflight.assert_inputs_unchanged(after_relay=True)
            emitted_bytes = persist_and_append(
                goal_preflight,
                report_payload=report.as_payload(),
                envelope=envelope,
                changed_paths=changed_fingerprints(
                    before_fingerprints, after_fingerprints
                ),
            )
    except PhaseProjectionBlockedError as exc:
        blocked_exit_code = exit_code_for_verdict(Verdict.BLOCKED)
        payload = {
            "phase": {
                "id": getattr(args, "phase_id", "phase"),
                "prompt_path": str(getattr(args, "prompt_file", "")),
                "run_dir": None,
            },
            "steps": [],
            "summary": {
                "verdict": Verdict.BLOCKED.value,
                "exit_code": blocked_exit_code,
                "completed": 0,
                "total": 0,
                "failed_step": None,
            },
        }
        envelope = Envelope.from_verdict(
            status=Verdict.BLOCKED,
            backend="phase-relay",
            model="relay",
            duration_s=time.monotonic() - start,
            stdout=json.dumps(payload, ensure_ascii=False),
            stderr=str(exc),
            not_claimed=["full-e2e"],
        )
    except Exception as exc:
        payload = {
            "phase": {
                "id": getattr(args, "phase_id", "phase"),
                "prompt_path": str(getattr(args, "prompt_file", "")),
                "run_dir": None,
            },
            "steps": [],
            "summary": {
                "verdict": Verdict.BLOCKED.value,
                "exit_code": INTERNAL_ERROR_EXIT_CODE,
                "completed": 0,
                "total": 0,
                "failed_step": None,
            },
        }
        envelope = Envelope(
            status=Verdict.BLOCKED,
            exit_code=INTERNAL_ERROR_EXIT_CODE,
            backend="phase-relay",
            model="relay",
            duration_s=time.monotonic() - start,
            stdout=json.dumps(payload, ensure_ascii=False),
            stderr_sanitized=redact_stderr(str(exc)),
            fallback_used=False,
            not_claimed=["full-e2e"],
        )

    _finish_recording(record_handle, envelope, rounds=rounds)
    if getattr(args, "goal_intent_context_file", None):
        sys.stdout.write(
            (emitted_bytes or canonical_json_line(envelope.as_stdout_payload())).decode(
                "utf-8"
            )
        )
    else:
        print(json.dumps(envelope.as_stdout_payload(), ensure_ascii=False))
    sys.exit(envelope.exit_code)


async def _run_phase_once(
    args: argparse.Namespace,
    *,
    commands: list[RelayCommand],
    resume_coordinator: ResumeCoordinator | None,
) -> PhaseRelayReport:
    """run-phase와 fix-round가 공유하는 단일 relay 실행 부착점."""
    relay = PhaseRelay(
        prompt_path=Path(args.prompt_file),
        commands=commands,
        output_dir=Path(args.output_dir),
        phase_id=args.phase_id,
        timeout_s=float(args.timeout),
        resume_coordinator=resume_coordinator,
        forbidden_paths=getattr(args, "implementer_forbidden_path", None),
    )
    return await relay.run()


def _require_phase_output(path: Path, operation: str, exit_code: int) -> None:
    """Projection API의 반환 코드와 필수 산출물을 fail-closed로 확인한다."""
    if exit_code != 0:
        raise PhaseProjectionBlockedError(
            f"{operation} failed with exit code {exit_code}"
        )
    if not path.is_file():
        raise PhaseProjectionBlockedError(
            f"{operation} reported success without output: {path}"
        )


def _run_phase_projection_operation(
    path: Path,
    operation: str,
    callback: Callable[[], int],
) -> None:
    """내부 API 출력을 격리하고 run-phase 단일 Envelope stdout을 보존한다."""
    try:
        with redirect_stdout(io.StringIO()):
            exit_code = callback()
    except Exception as exc:
        raise PhaseProjectionBlockedError(f"{operation} failed: {exc}") from exc
    _require_phase_output(path, operation, exit_code)


def _load_phase_projection_api() -> tuple[
    InitManifestFn, AppendEntryFn, ProjectReportFn
]:
    """Opt-in projection이 활성일 때만 methodology API를 로드한다."""
    try:
        manifest_module = importlib.import_module("methodology.phase.manifest")
        report_module = importlib.import_module("methodology.phase.report")
        init_manifest = cast(InitManifestFn, manifest_module.init_manifest)
        append_entry = cast(AppendEntryFn, manifest_module.append_entry)
        project_report = cast(ProjectReportFn, report_module.project_report)
    except (ImportError, AttributeError) as exc:
        raise PhaseProjectionBlockedError(
            f"phase projection API import failed: {exc}; --phase-dir opt-in에는 "
            "methodology import가 필요합니다. 저장소 루트에서 실행하거나 저장소 "
            "루트를 Python import 경로에 포함하세요"
        ) from exc
    return init_manifest, append_entry, project_report


def _init_phase_projection(args: argparse.Namespace) -> tuple[Path, Path] | None:
    """Opt-in manifest를 relay 전에 초기화하고 capture 경계를 고정한다."""
    raw_phase_dir = getattr(args, "phase_dir", None)
    if raw_phase_dir is None:
        return None
    base_sha = getattr(args, "base_sha", None)
    if base_sha is None:
        raise PhaseProjectionBlockedError(
            "--phase-dir 사용 시 --base-sha가 필요합니다"
        )
    init_manifest, _, _ = _load_phase_projection_api()

    phase_dir = Path(raw_phase_dir).resolve()
    expected_output_dir = (phase_dir / "runs").resolve()
    actual_output_dir = Path(args.output_dir).resolve()
    if actual_output_dir != expected_output_dir:
        raise PhaseProjectionBlockedError(
            "--phase-dir 사용 시 --output-dir은 정확히 <phase-dir>/runs여야 합니다"
        )

    manifest_path = phase_dir / "phase-manifest.json"
    report_path = phase_dir / "PHASE_REPORT.md"
    if report_path.exists():
        raise PhaseProjectionBlockedError(
            "phase projection output already exists; --phase-dir must be new"
        )

    phase_dir.mkdir(parents=True, exist_ok=True)
    _run_phase_projection_operation(
        manifest_path,
        "init_manifest",
        lambda: init_manifest(manifest_path, args.phase_id, base_sha),
    )
    return manifest_path, report_path


def _phase_manifest_entry(step: Any, index: int, phase_dir: Path) -> dict[str, Any]:
    """Relay step을 manifest 닫힌 스키마의 한 entry로 변환한다."""
    if step.skipped:
        return {
            "kind": "validation_fact",
            "name": step.name,
            "result": "SKIPPED",
        }
    if step.name == "implementer-scope-guard":
        return {
            "kind": "validation_fact",
            "name": step.name,
            "result": "BLOCKED",
        }
    if (
        step.name == "implementer"
        or step.name == "autofix"
        or step.name.startswith("autofix-")
    ):
        return {"kind": "command", "argv": step.command}

    subjects = {
        "mechanical-review": "mechanical",
        "test": "test",
        "implementer-reviewer": "diff",
    }
    subject = subjects.get(step.name)
    if subject is None:
        raise PhaseProjectionBlockedError(
            f"unsupported relay step for phase manifest: {step.name}"
        )
    if step.envelope_path is None:
        raise PhaseProjectionBlockedError(
            f"relay step has no envelope artifact: {step.name}"
        )
    envelope_path = step.envelope_path.resolve()
    try:
        artifact_ref = envelope_path.relative_to(phase_dir).as_posix()
        content_sha256 = hashlib.sha256(envelope_path.read_bytes()).hexdigest()
    except (OSError, ValueError) as exc:
        raise PhaseProjectionBlockedError(
            f"relay step envelope is unavailable inside phase directory: {step.name}"
        ) from exc
    entry = {
        "kind": "verdict",
        "verdict": step.status.value,
        "exit_code": exit_code_for_verdict(step.status),
        "artifact_ref": artifact_ref,
        "content_sha256": content_sha256,
        "attempt_id": f"{step.name}-{index}",
        "stage": "implementation_review",
        "subject": subject,
    }
    if step.exit_code == TIMEOUT_EXIT_CODE:
        entry["failure_reason"] = "timeout"
    return entry


def _read_manifest_state(
    manifest_path: Path,
) -> list[dict[str, Any]]:
    """append 결과 검증용 manifest 상태를 fail-closed로 재독한다."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PhaseProjectionBlockedError(
            f"append_entry output is unavailable or invalid JSON: {manifest_path}: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise PhaseProjectionBlockedError(
            f"append_entry output has invalid top-level fields: {manifest_path}"
        )
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not all(
        isinstance(item, dict) for item in entries
    ):
        raise PhaseProjectionBlockedError(
            f"append_entry output has invalid entries: {manifest_path}"
        )
    return entries


def _complete_phase_projection(
    relay_report: PhaseRelayReport,
    manifest_path: Path,
    report_path: Path,
) -> None:
    """완성된 relay report를 순서대로 append한 뒤 단일 보고서를 투영한다."""
    _, append_entry, project_report = _load_phase_projection_api()
    phase_dir = manifest_path.parent.resolve()
    for index, step in enumerate(relay_report.steps):
        entry = _phase_manifest_entry(step, index, phase_dir)
        entries_before = _read_manifest_state(manifest_path)
        _run_phase_projection_operation(
            manifest_path,
            "append_entry",
            lambda: append_entry(manifest_path, entry),
        )
        entries_after = _read_manifest_state(manifest_path)
        appended_entry = entries_after[-1] if entries_after else {}
        appended_projection = {key: appended_entry.get(key) for key in entry}
        if (
            len(entries_after) != len(entries_before) + 1
            or appended_projection != entry
        ):
            raise PhaseProjectionBlockedError(
                "append_entry reported success without appending the expected entry"
            )
    _run_phase_projection_operation(
        report_path,
        "project_report",
        lambda: project_report(manifest_path, report_path),
    )


async def cmd_fix_round(args: argparse.Namespace) -> None:
    """CHANGES_REQUESTED finding을 같은 implementer thread에서 정확히 1회 재검증한다."""
    start = time.monotonic()
    ledger: ReapplyLedger | None = None
    round_started = False
    round_index = 0
    input_digest = ""
    fix_path: Path | None = None
    report_payload: dict[str, Any] | None = None
    report: PhaseRelayReport | None = None
    envelope: Envelope | None = None
    record_handle: RecordHandle | None = None
    goal_preflight: GoalIntentPreflight | None = None
    before_fingerprints: dict[str, str] | None = None
    after_fingerprints: dict[str, str] | None = None
    emitted_bytes: bytes | None = None
    focused = False
    candidate_digest_value = ""
    command_digest_value = ""
    base_sha = ""
    started_at = datetime.now().astimezone().isoformat()
    try:
        original_prompt_path = str(args.prompt_file)
        config = load_config(getattr(args, "config", None))
        if getattr(args, "goal_intent_context_file", None):
            process_cwd = Path.cwd()
            repo_root = await _resolve_goal_intent_repo_root(process_cwd)
            goal_preflight = await prepare_preflight(
                args,
                kind="fix-round",
                config=config,
                process_cwd=process_cwd,
                repo_root=repo_root,
            )
            goal_preflight.assert_inputs_unchanged()
        record_handle = _start_recording(
            args,
            config=config,
            task=f"ztr fix-round {args.phase_id}",
            target_file=original_prompt_path,
            metadata={
                "command": "fix-round",
                "phase_id": args.phase_id,
                "prompt_file": original_prompt_path,
                "report_file": str(args.report_file),
                "ledger": str(args.ledger),
                "max_rounds": args.max_rounds,
                "accept_leg": getattr(args, "accept_leg", None),
                "session_map": str(args.session_map),
                "resume": {
                    "implementer_profile": args.implementer_resume_profile,
                    "reviewer_profile": args.reviewer_resume_profile,
                },
            },
        )
        original_prompt = Path(original_prompt_path).read_text(encoding="utf-8-sig")
        report_payload = load_report_payload(Path(args.report_file))
        summary = report_payload.get("summary")
        if not isinstance(summary, dict):
            raise ValueError("직전 report summary object가 없습니다")
        try:
            prior_verdict = Verdict(summary.get("verdict"))
        except (TypeError, ValueError) as exc:
            raise ValueError("직전 report summary.verdict가 없거나 알 수 없는 값입니다") from exc

        if prior_verdict in {Verdict.PASS, Verdict.BLOCKED}:
            ledger = ReapplyLedger.load(args.ledger)
            if ledger.phase_id != args.phase_id:
                raise FixRoundBlockedError("phase_id가 원장과 일치하지 않습니다")
            if ledger.terminal_state is None:
                ledger.terminal_state = (
                    "CONVERGED" if prior_verdict == Verdict.PASS else "ESCALATED_BLOCKED"
                )
                ledger.save()
        if prior_verdict == Verdict.PASS:
            envelope = Envelope.from_verdict(
                status=Verdict.PASS,
                backend="phase-relay",
                model="fix-round",
                duration_s=time.monotonic() - start,
                stderr="",
                not_claimed=["full-e2e"],
            )
            print("[fix-round] 직전 verdict가 PASS — 고칠 것이 없어 실행하지 않습니다.", file=sys.stderr)
            sys.exit(0)
        if prior_verdict == Verdict.BLOCKED:
            escalation_reason = (
                "[fix-round] 직전 verdict가 BLOCKED — 코드 finding이 아니므로 "
                "사람에게 에스컬레이션합니다."
            )
            envelope = Envelope.from_verdict(
                status=Verdict.BLOCKED,
                backend="phase-relay",
                model="fix-round",
                duration_s=time.monotonic() - start,
                stderr=escalation_reason,
                not_claimed=["full-e2e"],
            )
            print(escalation_reason, file=sys.stderr)
            sys.exit(exit_code_for_verdict(Verdict.BLOCKED))

        try:
            findings = select_accepted_findings(
                report_payload, getattr(args, "accept_leg", None)
            )
        except ValueError as exc:
            raise FixRoundBlockedError(str(exc)) from exc
        if not findings or any(f.status != Verdict.CHANGES_REQUESTED.value for f in findings):
            raise FixRoundBlockedError(
                "CHANGES_REQUESTED summary와 findings가 불일치합니다 "
                "(빈 findings 또는 mixed/non-CHANGES finding)"
            )

        input_digest = findings_digest(findings)
        validate_max_rounds(args.max_rounds)
        ledger = ReapplyLedger.load_or_create(
            args.ledger,
            phase_id=args.phase_id,
            max_rounds=args.max_rounds,
        )
        reason = gate_check(
            ledger,
            phase_id=args.phase_id,
            approve_round=args.approve_round,
            approve_findings=args.approve_findings,
            input_digest=input_digest,
            max_rounds=args.max_rounds,
        )
        if reason is not None:
            raise FixRoundBlockedError(reason)

        if not args.session_map:
            raise FixRoundBlockedError("fix-round에는 implementer id가 든 --session-map이 필요합니다")
        session_map = SessionMap.load(Path(args.session_map))
        implementer_id = session_map.get("implementer")
        if implementer_id is None:
            raise FixRoundBlockedError("session-map에 implementer id가 없어 같은 thread resume을 보장할 수 없습니다")

        round_index = ledger.next_round_index
        commands = _relay_commands_from_args(args)
        command_records = _verification_command_records(args)
        commands, focused = _apply_focused_commands(args, commands)
        if focused:
            base_sha = getattr(args, "base_sha", None) or ""
            command_digest_value = command_digest(command_records)
            # relay가 파일을 바꾸기 전에 base가 유효하고 Git 경로가 살아 있는지 닫힌 게이트로 확인한다.
            await _candidate_digest(base_sha)
        fix_prompt = build_fix_resume_prompt(original_prompt, findings)
        fix_path = Path(args.output_dir) / f"{args.phase_id}-round-{round_index}-fix-prompt.md"
        fix_path.parent.mkdir(parents=True, exist_ok=True)
        fix_path.write_text(fix_prompt, encoding="utf-8")

        args.prompt_file = str(fix_path)
        args.implementer_resume = implementer_id
        coordinator = _resume_coordinator_from_args(args)
        if goal_preflight is not None:
            goal_preflight.assert_inputs_unchanged()
            before_fingerprints = await capture_fingerprints(
                goal_preflight.repo_root,
                excluded_paths=goal_preflight.excluded_paths,
            )
        round_started = True
        try:
            report = await _run_phase_once(
                args, commands=commands, resume_coordinator=coordinator
            )
            _validate_fix_round_resume(report, expected_id=implementer_id)
        finally:
            if goal_preflight is not None and before_fingerprints is not None:
                after_fingerprints = await capture_fingerprints(
                    goal_preflight.repo_root,
                    excluded_paths=goal_preflight.excluded_paths,
                )
        if focused:
            candidate_digest_value, _ = await _candidate_digest(base_sha)
        envelope = Envelope(
            status=report.status,
            exit_code=report.exit_code,
            backend="phase-relay",
            model="external-cli",
            duration_s=time.monotonic() - start,
            stdout=json.dumps(report.as_payload(), ensure_ascii=False),
            stderr_sanitized="",
            fallback_used=report.resume_fallback_used,
            not_claimed=["full-e2e", *(["full-regression"] if focused else [])],
        )
    except SystemExit as exc:
        if envelope is None:
            raw_code = exc.code
            exit_code = raw_code if isinstance(raw_code, int) else 1
            status = Verdict.PASS if exit_code == 0 else Verdict.BLOCKED
            envelope = Envelope(
                status=status,
                exit_code=exit_code,
                backend="phase-relay",
                model="fix-round",
                duration_s=time.monotonic() - start,
                stdout="",
                stderr_sanitized="fix-round exited before producing an envelope",
                fallback_used=False,
                not_claimed=["full-e2e"],
            )
        _finish_recording(record_handle, envelope, rounds=1 if round_started else 0)
        raise
    except FixRoundBlockedError as exc:
        envelope = Envelope.from_verdict(
            status=Verdict.BLOCKED,
            backend="phase-relay",
            model="fix-round",
            duration_s=time.monotonic() - start,
            stderr=str(exc),
            not_claimed=["full-e2e"],
        )
    except Exception as exc:
        envelope = Envelope(
            status=Verdict.BLOCKED,
            exit_code=INTERNAL_ERROR_EXIT_CODE,
            backend="phase-relay",
            model="fix-round",
            duration_s=time.monotonic() - start,
            stdout="",
            stderr_sanitized=redact_stderr(str(exc)),
            fallback_used=False,
            not_claimed=["full-e2e"],
        )
    if round_started and ledger is not None and envelope is not None:
        try:
            result_payload = report.as_payload() if report is not None else None
            result_digest = (
                findings_digest(extract_findings(result_payload))
                if result_payload is not None
                else None
            )
        except Exception:
            result_payload = None
            result_digest = None
        report_path = ""
        if result_payload is not None:
            report_file = Path(args.output_dir) / f"{args.phase_id}-round-{round_index}-report.json"
            try:
                report_file.parent.mkdir(parents=True, exist_ok=True)
                report_file.write_text(
                    json.dumps(result_payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                report_path = str(report_file)
            except OSError:
                report_path = ""
        previous_digest = (
            ledger.rounds[-1].get("result_digest") if ledger.rounds else None
        )
        round_entry: dict[str, Any] = {
            "index": round_index,
            "verdict": envelope.status.value,
            "exit_code": envelope.exit_code,
            "input_digest": input_digest,
            "result_digest": result_digest,
            "report_path": report_path,
            "fix_prompt_path": str(fix_path) if fix_path is not None else "",
            "started_at": started_at,
            "duration_s": time.monotonic() - start,
            "note": "",
        }
        if focused:
            round_entry.update({
                "focused": True,
                "legs": _gating_leg_facts(report) if report is not None else [],
                "candidate_digest": candidate_digest_value,
                "base_sha": base_sha,
                "command_digest": command_digest_value,
            })
        ledger.rounds.append(round_entry)
        ledger.terminal_state = decide_terminal_state(
            verdict=envelope.status,
            rounds_used=len(ledger.rounds),
            max_rounds=ledger.max_rounds,
            prev_result_digest=previous_digest,
            this_result_digest=result_digest,
            focused=focused,
        )
        if (
            envelope.status == Verdict.CHANGES_REQUESTED
            and ledger.terminal_state in {"NO_PROGRESS", "TIMEBOX_EXHAUSTED"}
        ):
            envelope = Envelope.from_verdict(
                status=Verdict.BLOCKED,
                backend=envelope.backend,
                model=envelope.model,
                duration_s=envelope.duration_s,
                stdout=envelope.stdout,
                stderr=envelope.stderr_sanitized,
                fallback_used=envelope.fallback_used,
                not_claimed=envelope.not_claimed,
            )
        outer_file = Path(args.output_dir) / f"{args.phase_id}-round-{round_index}-envelope.json"
        try:
            outer_file.parent.mkdir(parents=True, exist_ok=True)
            outer_file.write_text(
                json.dumps(envelope.as_stdout_payload(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass
        ledger.save()
    if (
        goal_preflight is not None
        and round_started
        and report is not None
        and envelope is not None
    ):
        try:
            if before_fingerprints is None or after_fingerprints is None:
                raise GoalIntentError("GOAL_INTENT_BASELINE_FAILED")
            goal_preflight.assert_inputs_unchanged(after_relay=True)
            emitted_bytes = persist_and_append(
                goal_preflight,
                report_payload=report.as_payload(),
                envelope=envelope,
                changed_paths=changed_fingerprints(
                    before_fingerprints, after_fingerprints
                ),
            )
        except GoalIntentError as exc:
            envelope = Envelope(
                status=Verdict.BLOCKED,
                exit_code=INTERNAL_ERROR_EXIT_CODE,
                backend="phase-relay",
                model="fix-round",
                duration_s=time.monotonic() - start,
                stdout=envelope.stdout,
                stderr_sanitized=redact_stderr(str(exc)),
                fallback_used=envelope.fallback_used,
                not_claimed=envelope.not_claimed,
            )
    if envelope is None:
        envelope = Envelope(
            status=Verdict.BLOCKED,
            exit_code=INTERNAL_ERROR_EXIT_CODE,
            backend="phase-relay",
            model="fix-round",
            duration_s=time.monotonic() - start,
            stdout="",
            stderr_sanitized="fix-round finished without envelope",
            fallback_used=False,
            not_claimed=["full-e2e"],
        )
    _finish_recording(record_handle, envelope, rounds=1 if round_started else 0)
    if getattr(args, "goal_intent_context_file", None):
        sys.stdout.write(
            (emitted_bytes or canonical_json_line(envelope.as_stdout_payload())).decode(
                "utf-8"
            )
        )
    else:
        print(json.dumps(envelope.as_stdout_payload(), ensure_ascii=False))
    sys.exit(envelope.exit_code)


async def cmd_final_verify(args: argparse.Namespace) -> None:
    """focused candidate에 묶인 full 검증 명령을 정확히 한 번 소비한다."""
    start = time.monotonic()
    ledger: ReapplyLedger | None = None
    report: PhaseRelayReport | None = None
    envelope: Envelope | None = None
    record_handle: RecordHandle | None = None
    try:
        config = load_config(getattr(args, "config", None))
        record_handle = _start_recording(
            args,
            config=config,
            task=f"ztr final-verify {args.phase_id}",
            target_file=str(args.prompt_file),
            metadata={
                "command": "final-verify",
                "phase_id": args.phase_id,
                "prompt_file": str(args.prompt_file),
                "ledger": str(args.ledger),
                "base_sha": str(getattr(args, "base_sha", "")),
            },
        )
        ledger = ReapplyLedger.load(args.ledger)
        base_sha = getattr(args, "base_sha", None) or ""
        candidate_digest_value, _ = await _candidate_digest(base_sha)
        command_digest_value = command_digest(_verification_command_records(args))
        gate_error = final_verify_gate_check(
            ledger,
            base_sha=base_sha,
            candidate_digest=candidate_digest_value,
            command_digest_value=command_digest_value,
        )
        if gate_error is not None:
            raise FixRoundBlockedError(gate_error)

        commands = _verification_commands_from_args(args)
        report = await _run_phase_once(
            args,
            commands=commands,
            resume_coordinator=None,
        )

        result_error = _verification_result_error(report, commands)
        if result_error is not None:
            raise FixRoundBlockedError(result_error)
        after_candidate_digest, _ = await _candidate_digest(base_sha)
        if after_candidate_digest != candidate_digest_value:
            raise FixRoundBlockedError(
                "final-verify 실행 중 candidate_digest가 변경되었습니다"
            )

        gating_steps = [step for step in report.steps if step.gating]
        all_steps_passed = all(
            step.status == Verdict.PASS and step.exit_code == 0
            for step in gating_steps
        )
        if report.status == Verdict.PASS and report.exit_code == 0 and not all_steps_passed:
            raise FixRoundBlockedError(
                "final-verify summary PASS와 gating step PASS/0 사실이 일치하지 않습니다"
            )
        envelope = Envelope(
            status=report.status,
            exit_code=report.exit_code,
            backend="phase-relay",
            model="final-verify",
            duration_s=time.monotonic() - start,
            stdout=json.dumps(report.as_payload(), ensure_ascii=False),
            stderr_sanitized="",
            fallback_used=report.resume_fallback_used,
            not_claimed=["full-e2e"],
        )
        if report.status == Verdict.PASS and report.exit_code == 0 and all_steps_passed:
            ledger.append_full_verification({
                "base_sha": base_sha,
                "candidate_digest": candidate_digest_value,
                "command_digest": command_digest_value,
                "legs": _gating_leg_facts(report),
                "completed_at": datetime.now().astimezone().isoformat(),
            })
            ledger.terminal_state = "CONVERGED"
            ledger.save()
    except FixRoundBlockedError as exc:
        envelope = Envelope.from_verdict(
            status=Verdict.BLOCKED,
            backend="phase-relay",
            model="final-verify",
            duration_s=time.monotonic() - start,
            stderr=str(exc),
            not_claimed=["full-e2e"],
        )
    except Exception as exc:
        envelope = Envelope(
            status=Verdict.BLOCKED,
            exit_code=INTERNAL_ERROR_EXIT_CODE,
            backend="phase-relay",
            model="final-verify",
            duration_s=time.monotonic() - start,
            stdout="",
            stderr_sanitized=redact_stderr(str(exc)),
            fallback_used=False,
            not_claimed=["full-e2e"],
        )

    if envelope is None:
        envelope = Envelope(
            status=Verdict.BLOCKED,
            exit_code=INTERNAL_ERROR_EXIT_CODE,
            backend="phase-relay",
            model="final-verify",
            duration_s=time.monotonic() - start,
            stderr_sanitized="final-verify finished without envelope",
            not_claimed=["full-e2e"],
        )
    _finish_recording(record_handle, envelope, rounds=1 if report is not None else 0)
    print(json.dumps(envelope.as_stdout_payload(), ensure_ascii=False))
    sys.exit(envelope.exit_code)


def cmd_reapply_status(args: argparse.Namespace) -> None:
    """재적용 원장을 변경하지 않고 기존 Envelope 계약으로 조회한다."""
    start = time.monotonic()
    try:
        ledger = ReapplyLedger.load(args.ledger)
        if ledger.terminal_state == "CONVERGED":
            status = Verdict.PASS
        elif ledger.terminal_state is None:
            status = Verdict.CHANGES_REQUESTED
        else:
            status = Verdict.BLOCKED
        payload = {
            "phase_id": ledger.phase_id,
            "max_rounds": ledger.max_rounds,
            "rounds": ledger.rounds,
            "terminal_state": ledger.terminal_state,
        }
        envelope = Envelope.from_verdict(
            status=status,
            backend="reapply-ledger",
            model="deterministic",
            duration_s=time.monotonic() - start,
            stdout=json.dumps(payload, ensure_ascii=False),
        )
    except Exception as exc:
        envelope = Envelope(
            status=Verdict.BLOCKED,
            exit_code=INTERNAL_ERROR_EXIT_CODE,
            backend="reapply-ledger",
            model="deterministic",
            duration_s=time.monotonic() - start,
            stderr_sanitized=redact_stderr(str(exc)),
        )
    print(json.dumps(envelope.as_stdout_payload(), ensure_ascii=False))
    sys.exit(envelope.exit_code)


def _validate_fix_round_resume(report: PhaseRelayReport, *, expected_id: str) -> None:
    """D1 네 조건을 구조화 resume 메타데이터로 fail-closed 검증한다."""
    if report.resume_fallback_used:
        raise FixRoundBlockedError("resume fallback이 사용되어 같은 thread 보장이 깨졌습니다")
    implementer = next((step for step in report.steps if step.name == "implementer"), None)
    resume = implementer.resume if implementer is not None else None
    if not isinstance(resume, dict):
        raise FixRoundBlockedError("implementer resume 메타데이터가 없습니다")
    if not (
        resume.get("resumed") is True
        and resume.get("requested_id") == expected_id
        and resume.get("captured_session_id") == expected_id
    ):
        raise FixRoundBlockedError("implementer resume D1 검증(resumed/requested/captured id)에 실패했습니다")


def cmd_fix_prompt(args: argparse.Namespace) -> None:
    """(ㄱ) 휴먼-게이트 fix-resume 프롬프트를 만든다.

    직전 run-phase 결과(--report-file)에서 non-PASS gating leg findings를 추출해 원본
    프롬프트(--prompt-file)에 합친 resume 프롬프트를 --out(또는 stdout)으로 낸다. **사람이
    명시 트리거**하는 유틸이며 자동 재시도 루프가 아니다(R5/휴먼게이트). 다음 라운드 실행은
    사람이 findings를 확인한 뒤 별도로 발행한다.

    exit code: 0=프롬프트 생성, 2=non-PASS gating findings 없음(주입 불필요), 70=입력 오류.
    """
    try:
        original_prompt = Path(args.prompt_file).read_text(encoding="utf-8-sig")
        report_payload = load_report_payload(Path(args.report_file))
    except (OSError, ValueError) as exc:  # json.JSONDecodeError ⊂ ValueError
        print(f"fix-prompt 입력 오류: {exc}", file=sys.stderr)
        sys.exit(INTERNAL_ERROR_EXIT_CODE)

    findings = extract_findings(report_payload)
    if not findings:
        print(
            "[fix-prompt] non-PASS gating leg findings가 없습니다 — fix-resume 불필요.",
            file=sys.stderr,
        )
        sys.exit(exit_code_for_verdict(Verdict.BLOCKED))

    fix_prompt = build_fix_resume_prompt(original_prompt, findings)
    if args.out:
        out_path = Path(args.out)
        out_path.write_text(fix_prompt, encoding="utf-8")
        legs = ", ".join(f"{f.leg}:{f.status}" for f in findings)
        print(f"[fix-prompt] {out_path} 작성 ({len(findings)} findings: {legs})", file=sys.stderr)
    else:
        sys.stdout.write(fix_prompt)
    sys.exit(0)


async def _resolve_review_targets(
    args: argparse.Namespace,
    *,
    cwd: Path,
) -> TargetSelection:
    return await _resolve_targets(args, cwd=cwd, command="review")


async def _resolve_verify_targets(
    args: argparse.Namespace,
    *,
    cwd: Path,
) -> TargetSelection:
    return await _resolve_targets(args, cwd=cwd, command="verify")


async def _resolve_targets(
    args: argparse.Namespace,
    *,
    cwd: Path,
    command: str,
) -> TargetSelection:
    """git-backed changed mode와 cwd-backed explicit mode를 분리한다."""
    if args.changed:
        try:
            repo_root = await resolve_repo_root(cwd=cwd)
        except RuntimeError as exc:
            raise TargetInputError(
                f"{command} --changed에는 git repository가 필요합니다: {exc}"
            ) from exc
        paths = await collect_changed_paths(cwd=repo_root)
        return TargetSelection(tuple(paths), repo_root, True)

    paths = [str(path) for path in args.paths]
    if not paths:
        raise TargetInputError(
            f"{command}에는 --changed 또는 하나 이상의 경로가 필요합니다"
        )
    resolved_paths = tuple(str((cwd / path).resolve()) for path in paths)
    missing = [
        path
        for path, resolved in zip(paths, resolved_paths)
        if not Path(resolved).exists()
    ]
    if missing:
        raise TargetInputError(f"{command} 대상 경로를 찾을 수 없습니다: {missing}")
    return TargetSelection(resolved_paths, cwd.resolve(), False)


def _review_blocked_envelope(
    *,
    start: float,
    exc: Exception,
    exit_code: int,
) -> Envelope:
    payload: dict[str, Any] = {
        "findings": [],
        "tool_results": {"ruff": {}, "mypy": {}},
        "review_text": None,
        "summary": {
            "verdict": Verdict.BLOCKED.value,
            "counts": {"ruff": 0, "mypy": 0, "total": 0},
        },
    }
    return Envelope(
        status=Verdict.BLOCKED,
        exit_code=exit_code,
        backend="internal",
        model="static-review",
        duration_s=time.monotonic() - start,
        stdout=json.dumps(payload, ensure_ascii=False),
        stderr_sanitized=redact_stderr(str(exc)),
        fallback_used=False,
        not_claimed=["ollama-review"],
    )


def _verify_blocked_payload() -> dict[str, Any]:
    return {
        "verified": [],
        "summary": {
            "verdict": Verdict.BLOCKED.value,
            "total": 0,
            "passed": 0,
            "failed": 0,
            "blocked": 1,
        },
    }


def _load_gate_input(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"result JSON 파싱 실패: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("result JSON 최상위 값은 object여야 합니다")

    kind = data.get("kind")
    if kind not in {"critic", "writer"}:
        raise ValueError("kind는 critic 또는 writer여야 합니다")
    if "verdict" not in data or not isinstance(data["verdict"], str):
        raise ValueError("verdict 문자열이 필요합니다")
    if "findings" not in data or not isinstance(data["findings"], list):
        raise ValueError("findings 배열이 필요합니다")
    if "content" not in data or not isinstance(data["content"], str):
        raise ValueError("content 문자열이 필요합니다")
    return data


def _normalise_gate_findings(findings: list[Any]) -> list[dict[str, str]]:
    normalised: list[dict[str, str]] = []
    for item in findings:
        if not isinstance(item, dict):
            raise ValueError("finding 항목은 object여야 합니다")
        finding_text = _as_text(item.get("finding", item.get("message", "")))
        normalised.append({
            "severity": _as_text(item.get("severity", "")),
            "message": finding_text,
            "recommendation": _as_text(item.get("recommendation", "")),
        })
    return normalised


def _critic_m2_shape_issues(findings: list[Any]) -> list[str]:
    required = (
        "severity",
        "finding",
        "evidence_or_repro",
        "impact",
        "recommendation",
    )
    issues: list[str] = []
    for index, item in enumerate(findings):
        if not isinstance(item, dict):
            raise ValueError("finding 항목은 object여야 합니다")
        missing = [
            key for key in required
            if not isinstance(item.get(key), str) or not item.get(key, "").strip()
        ]
        if missing:
            issues.append(
                f"finding[{index}] M2 필드 누락 또는 공백: {', '.join(missing)}"
            )
    return issues


def _gate_issue_payload(issue: str) -> dict[str, str]:
    return {
        "severity": "major",
        "finding": issue,
        "evidence_or_repro": "OutputQualityGate deterministic check",
        "impact": "산출물의 기계적 품질 계약을 신뢰하기 어렵습니다.",
        "recommendation": "result JSON 생성 주체가 산출물을 보강한 뒤 gate를 다시 실행하세요.",
    }


def _gate_payload(
    *,
    input_path: Path,
    data: dict[str, Any],
    result: QualityResult,
    issues: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "gate": {
            "passed": result.passed,
            "retry_requested": result.retry_requested,
            "issues_count": len(issues),
        },
        "issues": issues,
        "input": {
            "path": str(input_path),
            "kind": data["kind"],
            "verdict": data["verdict"],
            "findings_count": len(data["findings"]),
            "content_chars": len(data["content"]),
        },
    }


def _verify_payload(results: list[tuple[str, VerifyResult]]) -> dict[str, Any]:
    verified = [
        {
            "path": path,
            "passed": result.passed,
            "syntax_ok": result.syntax_ok,
            "ruff_ok": result.ruff_ok,
            "ruff_errors": result.ruff_errors,
            "mypy_ok": result.mypy_ok,
            "mypy_errors": result.mypy_errors,
            "blocked": result.blocked,
            "timed_out": result.timed_out,
            "issues": result.issues,
        }
        for path, result in results
    ]
    blocked_count = sum(1 for _, result in results if result.blocked or result.timed_out)
    passed_count = sum(1 for _, result in results if result.passed)
    failed_count = len(results) - passed_count - blocked_count
    if blocked_count:
        verdict = Verdict.BLOCKED
    elif failed_count:
        verdict = Verdict.CHANGES_REQUESTED
    else:
        verdict = Verdict.PASS
    return {
        "verified": verified,
        "summary": {
            "verdict": verdict.value,
            "total": len(results),
            "passed": passed_count,
            "failed": failed_count,
            "blocked": blocked_count,
        },
    }


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _start_recording(
    args: argparse.Namespace,
    *,
    config: RoundtableConfig,
    task: str,
    target_file: str,
    metadata: dict[str, Any],
) -> RecordHandle | None:
    """--record가 켜진 경우 SessionStore 세션을 시작한다."""
    if not getattr(args, "record", False):
        return None
    store: SessionStore | None = None
    try:
        store = SessionStore(config.session.db_path)
        session_id = store.create_session(
            task=task,
            target_file=target_file,
            config_snapshot=metadata,
        )
        return RecordHandle(store=store, session_id=session_id)
    except Exception as exc:
        if store is not None:
            try:
                store.close()
            except Exception as close_exc:
                print(f"record warning: close failed: {close_exc}", file=sys.stderr)
        print(f"record warning: start failed: {exc}", file=sys.stderr)
        return None


def _finish_recording(
    handle: RecordHandle | None,
    envelope: Envelope,
    *,
    rounds: int,
) -> None:
    """SessionStore 기록 실패가 명령 verdict를 오염시키지 않도록 격리한다."""
    if handle is None:
        return
    try:
        handle.store.finish_session(
            handle.session_id,
            verdict=envelope.status.value,
            rounds=rounds,
            error=envelope.stderr_sanitized,
        )
    except Exception as exc:
        print(f"record warning: finish failed: {exc}", file=sys.stderr)
    finally:
        try:
            handle.store.close()
        except Exception as exc:
            print(f"record warning: close failed: {exc}", file=sys.stderr)


def _relay_commands_from_args(args: argparse.Namespace) -> list[RelayCommand]:
    timeout_overrides = _validated_leg_timeout_overrides(args)
    # leg 순서: implementer → mechanical → test → reviewer.
    # reviewer(독립 구현리뷰)를 마지막에 둬서 mechanical/test의 green 증거를 본 뒤 리뷰하게 한다.
    # (골모드 run gm-c2b finding: reviewer가 test보다 먼저 실행되면 test green 봉투를 못 봐 false-negative.)
    commands = [
        RelayCommand.from_text(
            name="implementer",
            value=args.implementer_cmd,
            timeout_s=timeout_overrides["implementer"],
        )
    ]
    # (ㄴ) autofix leg: implementer 직후·mechanical 전, non-gating(파일 변형 전용).
    # 결정론 정정(예: ruff --fix, ruff format)으로 codex가 self-lint 못 한 결함을 제거해
    # mechanical false-CHANGES를 줄인다. verdict를 내지 않고 실패해도 게이트하지 않는다.
    # 여러 번 지정하면 순서대로 삽입한다(예: ruff check --fix → ruff format).
    for offset, autofix_cmd in enumerate(getattr(args, "autofix_cmd", None) or []):
        name = "autofix" if offset == 0 else f"autofix-{offset + 1}"
        commands.append(
            RelayCommand.from_text(
                name=name,
                value=autofix_cmd,
                gating=False,
                timeout_s=timeout_overrides["autofix"],
            )
        )
    if args.mechanical_cmd:
        commands.append(
            RelayCommand.from_text(
                name="mechanical-review",
                value=args.mechanical_cmd,
                timeout_s=timeout_overrides["mechanical"],
            )
        )
    if getattr(args, "test_cmd", ""):
        commands.append(
            RelayCommand.from_text(
                name="test",
                value=args.test_cmd,
                timeout_s=timeout_overrides["test"],
            )
        )
    if args.reviewer_cmd:
        commands.append(
            RelayCommand.from_text(
                name="implementer-reviewer",
                value=args.reviewer_cmd,
                verdict_source=getattr(args, "reviewer_verdict_source", "stdout_token"),
                timeout_s=timeout_overrides["reviewer"],
            )
        )
    return commands


def _validated_leg_timeout_overrides(
    args: argparse.Namespace,
) -> dict[str, float | None]:
    """지정됐지만 command가 없는 leg까지 spawn 전에 fail-closed 검증한다."""
    overrides: dict[str, float | None] = {}
    for leg in ("implementer", "autofix", "mechanical", "test", "reviewer"):
        value = getattr(args, f"{leg}_timeout", None)
        if value is not None:
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"--{leg}-timeout은 양의 유한수여야 합니다")
            value = float(value)
        overrides[leg] = value
    return overrides


def _add_leg_timeout_arguments(parser: argparse.ArgumentParser) -> None:
    """run-phase/fix-round에 동일한 optional per-leg timeout 표면을 추가한다."""
    for leg in ("implementer", "autofix", "mechanical", "test", "reviewer"):
        parser.add_argument(
            f"--{leg}-timeout",
            type=float,
            default=None,
            help=f"{leg} leg 타임아웃 초 (--timeout보다 우선, 미지정 시 global fallback)",
        )


def _runtime_root() -> Path:
    """설치된 ztr 패키지의 프로젝트 루트."""
    return Path(__file__).resolve().parents[1]


def _load_json_arg(inline: str, path: str, name: str) -> object:
    """inline JSON 문자열 또는 파일(우선, utf-8-sig 허용)에서 JSON을 읽는다."""
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if inline:
        return json.loads(inline)
    raise ValueError(f"--{name} 또는 --{name}-file 중 하나가 필요합니다")


def cmd_render_argv(args: argparse.Namespace) -> None:
    """argv 템플릿을 context로 치환해 완성 argv를 stdout JSON으로 출력한다.

    run-phase는 치환하지 않는다(EXECUTION_ADAPTER_CONTRACT §1). 오케스트레이터가
    이 유틸로 LLM 수작업 없이 argv를 조립한다(§10 step 14). 미정의 토큰 등은 exit 2(BLOCKED).
    """
    try:
        template = _load_json_arg(args.template, args.template_file, "template")
        context = _load_json_arg(args.context, args.context_file, "context")
    except (ValueError, OSError) as exc:
        print(f"render-argv 입력 오류: {exc}", file=sys.stderr)
        sys.exit(INTERNAL_ERROR_EXIT_CODE)

    if not isinstance(template, list) or not all(isinstance(t, str) for t in template):
        print("template은 문자열 배열이어야 합니다", file=sys.stderr)
        sys.exit(INTERNAL_ERROR_EXIT_CODE)
    if not isinstance(context, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in context.items()
    ):
        print("context는 문자열→문자열 object여야 합니다", file=sys.stderr)
        sys.exit(INTERNAL_ERROR_EXIT_CODE)

    try:
        argv = render_argv(template, context)
    except RenderError as exc:
        print(f"render BLOCKED: {exc}", file=sys.stderr)
        sys.exit(2)
    print(json.dumps(argv, ensure_ascii=False))


def cmd_emit_event(args: argparse.Namespace) -> None:
    """orchestrator phase 이벤트를 계약-conforming JSON 파일로 emit한다(§10 step 10).

    ACP collector가 폴링할 events_dir에 파일을 쓴다(파일-폴링 transport). AD-3: acp 모델을
    import하지 않고 문서화된 필드만 쓴다. 미지원 type은 exit 2(BLOCKED), 입력 오류는 exit 70.
    """
    try:
        if args.payload or args.payload_file:
            payload: object = _load_json_arg(args.payload, args.payload_file, "payload")
        else:
            payload = {}
        ts: datetime | None = None
        if args.ts:
            ts = datetime.fromisoformat(args.ts.replace("Z", "+00:00"))
    except (ValueError, OSError) as exc:
        print(f"emit-event 입력 오류: {exc}", file=sys.stderr)
        sys.exit(INTERNAL_ERROR_EXIT_CODE)

    if not isinstance(payload, dict) or not all(isinstance(k, str) for k in payload):
        print("payload는 문자열 키 JSON object여야 합니다", file=sys.stderr)
        sys.exit(INTERNAL_ERROR_EXIT_CODE)

    try:
        event = build_orch_event(
            event_type=args.type,
            project_id=args.project_id,
            phase_id=args.phase_id,
            payload=payload,
            ts=ts,
        )
    except EmitError as exc:
        print(f"emit BLOCKED: {exc}", file=sys.stderr)
        sys.exit(2)

    path = write_orch_event(Path(args.out_dir), event)
    print(json.dumps({"event_path": str(path), "type": event["type"]}, ensure_ascii=False))


def _resume_coordinator_from_args(args: argparse.Namespace) -> ResumeCoordinator | None:
    session_map_arg = getattr(args, "session_map", "")
    implementer_resume = getattr(args, "implementer_resume", "new")
    reviewer_resume = getattr(args, "reviewer_resume", "new")
    implementer_profile = _resume_profile(
        getattr(args, "implementer_resume_profile", "none")
    )
    reviewer_profile = _resume_profile(
        getattr(args, "reviewer_resume_profile", "none")
    )
    if (
        not session_map_arg
        and implementer_profile == "none"
        and reviewer_profile == "none"
        and implementer_resume == "new"
        and reviewer_resume == "new"
    ):
        return None

    session_map = (
        SessionMap.load(Path(session_map_arg))
        if session_map_arg
        else None
    )
    specs = {
        "implementer": ResumeSpec(
            role="implementer",
            policy=normalize_policy(implementer_resume),
            profile=implementer_profile,
        ),
        "reviewer": ResumeSpec(
            role="reviewer",
            policy=normalize_policy(reviewer_resume),
            profile=reviewer_profile,
        ),
    }
    return ResumeCoordinator(session_map=session_map, specs=specs)


def _resume_profile(value: str) -> ResumeProfile:
    if value not in {"none", "claude", "codex"}:
        raise ValueError(f"지원하지 않는 resume profile입니다: {value}")
    return value  # type: ignore[return-value]


async def _invoke_optional_ollama_review(
    config: RoundtableConfig,
    *,
    report: StaticReviewReport,
    diff_text: str,
) -> tuple[str | None, list[str]]:
    role_binding = config.get_role_binding("mechanical")
    if role_binding is None or role_binding.backend != "ollama":
        return None, ["ollama-review"]

    ollama_cfg = next(
        (
            agent
            for agent in config.get_enabled_agents()
            if agent.type == "ollama"
        ),
        None,
    )
    if ollama_cfg is None:
        return None, ["ollama-review"]

    agent = OllamaAgent()
    try:
        await agent.initialize({
            "id": ollama_cfg.id,
            "roles": ollama_cfg.roles,
            **ollama_cfg.config,
            "model": role_binding.model,
        })
        response = await agent.invoke(
            Prompt(
                content=_build_ollama_review_prompt(report, diff_text),
                role=AgentRole.CRITIC,
                context={
                    "system_prompt": (
                        "You are an optional mechanical reviewer. "
                        "Return concise review text only. Do not change verdict."
                    )
                },
            )
        )
    except Exception as exc:
        logger.warning("Ollama 보조 리뷰 실패: %s", exc)
        return None, ["ollama-review"]
    finally:
        try:
            await agent.shutdown()
        except Exception as exc:
            logger.debug("Ollama shutdown 실패: %s", exc)

    if not response.success or not response.content.strip():
        return None, ["ollama-review"]
    return response.content, []


def _build_ollama_review_prompt(
    report: StaticReviewReport,
    diff_text: str,
) -> str:
    payload = report.as_review_payload(review_text=None)
    return (
        "다음 변경분을 보조 리뷰해 주세요. verdict는 ruff/mypy 결과로 이미 "
        "결정되었으니 바꾸지 마세요. finding을 제시할 때는 "
        "severity / finding / evidence_or_repro / impact / recommendation "
        "5필드 형식으로만 간결하게 적어 주세요.\n\n"
        f"STATIC_RESULT:\n{json.dumps(payload, ensure_ascii=False)}\n\n"
        f"DIFF:\n{diff_text[:12000]}"
    )


def main() -> None:
    """CLI 메인 엔트리포인트."""
    parser = argparse.ArgumentParser(
        description="Zero-Token Roundtable — Mechanical 검사 런타임",
    )
    parser.add_argument(
        "--config", type=str, default=None,
        help="설정 파일 경로 (기본: src/config/agents.config.yaml)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="상세 로그 출력",
    )

    sub = parser.add_subparsers(dest="command")

    # invoke
    p_invoke = sub.add_parser("invoke", help="에이전트에 프롬프트 전송")
    p_invoke.add_argument("--agent", required=True, help="에이전트 ID")
    p_invoke.add_argument(
        "--role",
        choices=[role.value for role in AgentRole],
        default=AgentRole.MANAGER.value,
        help="진단 호출 역할 (기본: manager)",
    )
    p_invoke.add_argument("--prompt", required=True, help="프롬프트 텍스트")

    # health
    p_health = sub.add_parser("health", help="에이전트 헬스체크")
    p_health.add_argument("--agent", required=True, help="에이전트 ID")

    # list-agents
    sub.add_parser("list-agents", help="설정된 에이전트 목록")

    # review — Phase 2 mechanical review
    p_review = sub.add_parser("review", help="ruff/mypy 기반 Mechanical 리뷰")
    p_review.add_argument(
        "--changed",
        action="store_true",
        help="git diff 기반 변경 파일을 리뷰 대상으로 선택",
    )
    p_review.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="각 정적 분석 도구 타임아웃 초 (기본: 60)",
    )
    p_review.add_argument(
        "--record",
        action="store_true",
        help="SessionStore에 v2 review 결과를 관측 기록",
    )
    p_review.add_argument("paths", nargs="*", help="명시 리뷰 대상 경로")

    # gate — Phase 3 H4
    p_gate = sub.add_parser("gate", help="구조화된 result JSON 품질 게이트")
    p_gate.add_argument("result_file", help="검사할 result JSON 파일")

    # verify — Phase 3 H6
    p_verify = sub.add_parser("verify", help="사후 검증 실행")
    p_verify.add_argument(
        "--post-merge",
        action="store_true",
        required=True,
        help="PostMergeVerifier 기반 검증 모드",
    )
    p_verify.add_argument(
        "--changed",
        action="store_true",
        help="git diff 기반 변경 파일을 검증 대상으로 선택",
    )
    p_verify.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="각 검증 도구 타임아웃 초 (기본: 60)",
    )
    p_verify.add_argument(
        "--record",
        action="store_true",
        help="SessionStore에 v2 verify 결과를 관측 기록",
    )
    p_verify.add_argument("paths", nargs="*", help="명시 검증 대상 경로")

    # invariants — Phase 4
    p_invariants = sub.add_parser("invariants", help="MM/ZRT 인바리언트 사실 검사")
    p_invariants.add_argument(
        "--since",
        default="HEAD",
        help="비교 기준 git ref (기본: HEAD)",
    )
    p_invariants.add_argument(
        "--paths",
        nargs="*",
        default=None,
        help="검사 변경 경로 필터",
    )

    # run-phase — Phase 5 deterministic relay
    p_run_phase = sub.add_parser("run-phase", help="Phase 구현/리뷰 CLI 릴레이")
    p_run_phase.add_argument(
        "--prompt-file",
        required=True,
        help="첫 leg stdin으로 전달할 구현 프롬프트 파일",
    )
    p_run_phase.add_argument(
        "--goal-intent-context-file",
        default=None,
        help="T13 Goal/Intent writer canonical context (repo-relative, opt-in)",
    )
    p_run_phase.add_argument(
        "--phase-id",
        default="phase",
        help="캡처 디렉터리 식별자 (기본: phase)",
    )
    p_run_phase.add_argument(
        "--phase-dir",
        default=None,
        help="opt-in phase manifest/report 출력 디렉터리",
    )
    p_run_phase.add_argument(
        "--base-sha",
        default=None,
        help="phase manifest 기준 40자리 git SHA (--phase-dir 사용 시 필수)",
    )
    p_run_phase.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="각 relay leg 타임아웃 초 (기본: 600). 헤드리스 LLM leg(claude/codex)는 158~337s+ 소요 — 120은 빠듯해 타임아웃(골모드 run gm-c2 실측). 복잡 페이즈는 더 올린다.",
    )
    _add_leg_timeout_arguments(p_run_phase)
    p_run_phase.add_argument(
        "--output-dir",
        default=".ztr/run-phase",
        help="stdin/stdout/stderr/envelope 캡처 디렉터리",
    )
    p_run_phase.add_argument(
        "--implementer-cmd",
        required=True,
        help="구현 leg 명령. JSON 문자열 배열 또는 shell-like 문자열",
    )
    p_run_phase.add_argument(
        "--reviewer-cmd",
        default="",
        help="구현 리뷰 leg 명령. JSON 문자열 배열 또는 shell-like 문자열",
    )
    p_run_phase.add_argument(
        "--autofix-cmd",
        action="append",
        default=None,
        help=(
            "결정론 autofix leg 명령(예: ruff check --fix, ruff format). implementer "
            "직후·mechanical 전 실행. **non-gating**: 파일만 변형하고 verdict를 내지 않으며 "
            "실패해도 BLOCKED가 아니다(실행오류는 봉투에 기록). 여러 번 지정하면 순서대로 "
            "삽입(예: --autofix-cmd <ruff check --fix> --autofix-cmd <ruff format>). 미지정 시 "
            "삽입 안 함(opt-in). JSON 문자열 배열 또는 shell-like 문자열."
        ),
    )
    p_run_phase.add_argument(
        "--mechanical-cmd",
        default="",
        help="기계 리뷰 leg 명령. JSON 문자열 배열 또는 shell-like 문자열",
    )
    p_run_phase.add_argument(
        "--test-cmd",
        default="",
        help="테스트 leg 명령(예: pytest). mechanical 뒤 실행. exit 0=green이 루프 PASS의 일부",
    )
    p_run_phase.add_argument(
        "--session-map",
        default="",
        help="역할별 session id를 저장할 JSON 파일 경로",
    )
    p_run_phase.add_argument(
        "--implementer-resume",
        default="new",
        help="implementer resume 정책: new, auto 또는 명시 session id",
    )
    p_run_phase.add_argument(
        "--reviewer-resume",
        default="new",
        help="reviewer resume 정책: new, auto 또는 명시 session id",
    )
    p_run_phase.add_argument(
        "--implementer-resume-profile",
        choices=["none", "claude", "codex"],
        default="none",
        help="implementer argv resume 변형 profile",
    )
    p_run_phase.add_argument(
        "--reviewer-resume-profile",
        choices=["none", "claude", "codex"],
        default="none",
        help="reviewer argv resume 변형 profile",
    )
    p_run_phase.add_argument(
        "--reviewer-verdict-source",
        choices=["stdout_token", "exit_code"],
        default="stdout_token",
        help=(
            "reviewer leg verdict 소스. stdout_token(기본): stdout의 ZTR_VERDICT "
            "토큰으로 분기(claude -p 등은 verdict=BLOCKED여도 exit 0이라 exit_code "
            "신뢰 불가). 토큰 없으면 fail-closed BLOCKED. exit_code: 구(舊) 동작."
        ),
    )
    p_run_phase.add_argument(
        "--record",
        action="store_true",
        help="SessionStore에 v2 run-phase 결과를 관측 기록",
    )
    p_run_phase.add_argument(
        "--implementer-forbidden-path",
        action="append",
        default=None,
        help=(
            "implementer 완료 직후 현재 변경 집합에서 차단할 repo 상대 경로/glob. "
            "여러 번 지정 가능하며 미지정 시 범위 가드는 비활성"
        ),
    )

    # fix-round — T10-A 결정론 1라운드 fix→재검증 프리미티브
    p_fix_round = sub.add_parser(
        "fix-round",
        help="CHANGES_REQUESTED findings를 같은 implementer thread에서 정확히 1회 재검증",
    )
    p_fix_round.add_argument("--report-file", required=True, help="직전 run-phase 결과 JSON")
    p_fix_round.add_argument("--prompt-file", required=True, help="원본 구현 프롬프트 파일")
    p_fix_round.add_argument("--out", default="", help="fix-resume 프롬프트 출력 경로")
    p_fix_round.add_argument("--ledger", required=True, help="호출자가 소유하는 재적용 원장 경로")
    p_fix_round.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS)
    p_fix_round.add_argument("--approve-round", type=int, default=None)
    p_fix_round.add_argument("--approve-findings", default=None)
    p_fix_round.add_argument(
        "--accept-leg",
        action="append",
        default=None,
        help="exact CHANGES_REQUESTED gating leg 선택(대소문자 구분, 반복 가능)",
    )
    p_fix_round.add_argument(
        "--record",
        action="store_true",
        help="SessionStore에 v2 fix-round 결과를 관측 기록",
    )
    p_fix_round.add_argument("--phase-id", default="fix-round", help="캡처 디렉터리 식별자")
    p_fix_round.add_argument("--timeout", type=float, default=600.0, help="각 relay leg 타임아웃 초")
    _add_leg_timeout_arguments(p_fix_round)
    p_fix_round.add_argument("--output-dir", default=".ztr/run-phase", help="relay 캡처 디렉터리")
    p_fix_round.add_argument(
        "--goal-intent-context-file",
        default=None,
        help="T13 Goal/Intent writer canonical context (repo-relative, opt-in)",
    )
    p_fix_round.add_argument("--implementer-cmd", required=True, help="구현 leg 명령")
    p_fix_round.add_argument("--reviewer-cmd", default="", help="구현 리뷰 leg 명령")
    p_fix_round.add_argument("--autofix-cmd", action="append", default=None, help="non-gating autofix leg")
    p_fix_round.add_argument("--mechanical-cmd", default="", help="기계 리뷰 leg 명령")
    p_fix_round.add_argument("--test-cmd", default="", help="테스트 leg 명령")
    p_fix_round.add_argument(
        "--focused-mechanical-cmd",
        default=None,
        help="이번 라운드에서만 mechanical-review value를 치환",
    )
    p_fix_round.add_argument(
        "--focused-test-cmd",
        default=None,
        help="이번 라운드에서만 test value를 치환",
    )
    p_fix_round.add_argument(
        "--base-sha",
        default="",
        help="focused candidate digest의 git diff 기준 SHA",
    )
    p_fix_round.add_argument("--session-map", required=True, help="implementer id가 든 session map")
    p_fix_round.add_argument("--implementer-resume", default="new", help="호환 인자; session-map id로 강제됨")
    p_fix_round.add_argument("--reviewer-resume", default="new", help="reviewer resume 정책")
    p_fix_round.add_argument(
        "--implementer-resume-profile",
        choices=["claude", "codex"],
        required=True,
        help="같은 thread resume을 위한 implementer profile",
    )

    p_reapply_status = sub.add_parser("reapply-status", help="재적용 원장 상태를 읽기 전용으로 조회")
    p_reapply_status.add_argument("--ledger", required=True, help="조회할 재적용 원장 경로")
    p_fix_round.add_argument(
        "--reviewer-resume-profile",
        choices=["none", "claude", "codex"],
        default="none",
        help="reviewer argv resume 변형 profile",
    )
    p_fix_round.add_argument(
        "--reviewer-verdict-source",
        choices=["stdout_token", "exit_code"],
        default="stdout_token",
        help="reviewer leg verdict 소스",
    )

    p_final_verify = sub.add_parser(
        "final-verify",
        help="focused candidate에 묶인 full 검증 명령을 정확히 1회 실행",
    )
    p_final_verify.add_argument("--prompt-file", required=True, help="검증 leg stdin 프롬프트 파일")
    p_final_verify.add_argument("--ledger", required=True, help="focused 라운드가 든 재적용 원장")
    p_final_verify.add_argument("--base-sha", required=True, help="candidate git diff 기준 SHA")
    p_final_verify.add_argument("--phase-id", default="final-verify", help="캡처 디렉터리 식별자")
    p_final_verify.add_argument("--timeout", type=float, default=600.0, help="각 검증 leg 타임아웃 초")
    _add_leg_timeout_arguments(p_final_verify)
    p_final_verify.add_argument("--output-dir", default=".ztr/run-phase", help="검증 캡처 디렉터리")
    p_final_verify.add_argument("--mechanical-cmd", default="", help="full 기계 리뷰 leg 명령")
    p_final_verify.add_argument("--test-cmd", default="", help="full 테스트 leg 명령")
    p_final_verify.add_argument("--reviewer-cmd", default="", help="full 구현 리뷰 leg 명령")
    p_final_verify.add_argument(
        "--reviewer-verdict-source",
        choices=["stdout_token", "exit_code"],
        default="stdout_token",
        help="reviewer leg verdict 소스",
    )
    p_final_verify.add_argument(
        "--record",
        action="store_true",
        help="SessionStore에 final-verify 결과를 관측 기록",
    )

    # fix-prompt — (ㄱ) 휴먼-게이트 fix-resume 프롬프트 빌더(사람 트리거, 자동 루프 아님)
    p_fix_prompt = sub.add_parser(
        "fix-prompt",
        help="직전 run-phase findings를 원본 프롬프트에 합친 fix-resume 프롬프트 생성(사람 트리거)",
    )
    p_fix_prompt.add_argument(
        "--prompt-file",
        required=True,
        help="원본 구현 프롬프트 파일(이 위에 findings를 주입)",
    )
    p_fix_prompt.add_argument(
        "--report-file",
        required=True,
        help="직전 run-phase 결과 JSON(바깥 Envelope 또는 report payload). non-PASS gating leg findings 추출",
    )
    p_fix_prompt.add_argument(
        "--out",
        default="",
        help="fix-resume 프롬프트 출력 파일. 미지정 시 stdout.",
    )

    # render-argv — 실행 어댑터(템플릿 {name} 치환 → 완성 argv JSON). run-phase는 치환 안 함.
    p_render = sub.add_parser(
        "render-argv",
        help="project.config argv 템플릿을 context로 치환해 완성 argv JSON 출력 (실행 어댑터)",
    )
    p_render.add_argument("--template", default="", help="argv 템플릿 JSON 배열(인라인)")
    p_render.add_argument("--template-file", default="", help="argv 템플릿 JSON 배열 파일(우선)")
    p_render.add_argument("--context", default="", help="치환 context JSON object(인라인)")
    p_render.add_argument("--context-file", default="", help="치환 context JSON object 파일(우선)")

    # emit-event — orchestrator phase 이벤트를 계약-conforming JSON 파일로 emit(ACP 폴링용).
    p_emit = sub.add_parser(
        "emit-event",
        help="orchestrator phase 이벤트를 events_dir에 JSON 파일로 emit (ACP 이벤트 계약)",
    )
    p_emit.add_argument(
        "--type", required=True,
        help="이벤트 type (phase.started|leg.result|gate.waiting|phase.verdict)",
    )
    p_emit.add_argument("--project-id", required=True, help="프로젝트 식별자")
    p_emit.add_argument("--phase-id", required=True, help="페이즈 식별자")
    p_emit.add_argument("--out-dir", required=True, help="이벤트 파일 출력 디렉터리(events_dir)")
    p_emit.add_argument("--payload", default="", help="이벤트 payload JSON object(인라인, 선택)")
    p_emit.add_argument("--payload-file", default="", help="payload JSON object 파일(우선, 선택)")
    p_emit.add_argument("--ts", default="", help="ISO8601 시각(선택, 기본 now UTC)")

    # history — 세션 기록
    p_history = sub.add_parser("history", help="최근 세션 기록 조회")
    p_history.add_argument("--limit", type=int, default=20, help="표시 개수 (기본: 20)")

    # stats — 에이전트 통계
    sub.add_parser("stats", help="에이전트별 통계 조회")

    # issues — 이슈 관리
    p_issues = sub.add_parser("issues", help="이슈 목록 조회")
    p_issues.add_argument("--status", default="", help="상태 필터 (open/resolved/all)")

    # decisions — 의사결정 조회
    p_decisions = sub.add_parser("decisions", help="최근 의사결정 조회")
    p_decisions.add_argument("--limit", type=int, default=10, help="표시 개수")

    # feedback — 사용자 피드백
    p_fb = sub.add_parser("feedback", help="세션에 사용자 피드백 기록")
    p_fb.add_argument("--session", type=int, required=True, help="세션 ID")
    p_fb.add_argument("--agree", action="store_true", default=True, help="Critic 판정 동의 (기본값)")
    p_fb.add_argument("--disagree", action="store_true", help="Critic 판정 반대")
    p_fb.add_argument("--override", type=str, default="", help="판정 오버라이드 (pass/conditional/fail)")

    # web — 대시보드 서버
    p_web = sub.add_parser("web", help="ZTR 대시보드 웹 서버 시작")
    p_web.add_argument("--port", type=int, default=8000, help="포트 (기본: 8000)")
    p_web.add_argument("--host", default="127.0.0.1", help="호스트 (기본: 127.0.0.1)")

    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.command == "invoke":
        asyncio.run(cmd_invoke(args))
    elif args.command == "health":
        asyncio.run(cmd_health(args))
    elif args.command == "list-agents":
        asyncio.run(cmd_list(args))
    elif args.command == "review":
        asyncio.run(cmd_review(args))
    elif args.command == "gate":
        asyncio.run(cmd_gate(args))
    elif args.command == "verify":
        asyncio.run(cmd_verify(args))
    elif args.command == "invariants":
        asyncio.run(cmd_invariants(args))
    elif args.command == "run-phase":
        asyncio.run(cmd_run_phase(args))
    elif args.command == "fix-round":
        asyncio.run(cmd_fix_round(args))
    elif args.command == "final-verify":
        asyncio.run(cmd_final_verify(args))
    elif args.command == "reapply-status":
        cmd_reapply_status(args)
    elif args.command == "fix-prompt":
        cmd_fix_prompt(args)
    elif args.command == "render-argv":
        cmd_render_argv(args)
    elif args.command == "emit-event":
        cmd_emit_event(args)
    elif args.command == "history":
        cmd_history(args)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "issues":
        from src.engine.session_store import SessionStore
        config = load_config(args.config)
        store = SessionStore(config.session.db_path)
        issues = store.list_issues(status=args.status)
        if not issues:
            print("  등록된 이슈가 없습니다.")
        else:
            print(f"\n  {'ID':<20} {'STATUS':<12} {'TITLE':<30} {'FILE'}")
            print(f"  {'─'*20} {'─'*12} {'─'*30} {'─'*30}")
            for i in issues:
                print(f"  {i['id']:<20} {i['status']:<12} {(i['title'] or '')[:30]:<30} {i['target_file'] or '-'}")
        print()
        store.close()
    elif args.command == "decisions":
        from src.engine.session_store import SessionStore
        config = load_config(args.config)
        store = SessionStore(config.session.db_path)
        decisions = store.get_recent_decisions(limit=args.limit)
        if not decisions:
            print("  기록된 의사결정이 없습니다.")
        else:
            print(f"\n  {'ID':>4}  {'ISSUE':<16} {'DECISION':<50} {'BY'}")
            print(f"  {'─'*4}  {'─'*16} {'─'*50} {'─'*30}")
            for d in decisions:
                print(f"  {d['id']:>4}  {(d.get('issue_title') or '-')[:16]:<16} {d['decision'][:50]:<50} {(d.get('decided_by') or '-')[:30]}")
        print()
        store.close()
    elif args.command == "feedback":
        cmd_feedback(args)
    elif args.command == "web":
        from src.web.app import main as web_main
        web_main()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
