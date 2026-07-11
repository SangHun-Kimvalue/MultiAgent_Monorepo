"""acp/join.py — 세션 × PHASE.md 조인 (P2, 이 프로젝트의 심장).

record.project_path(cwd)에서 상위로 거슬러 최근접 PHASE.md를 찾아 parse_phase_md로
현재페이즈/상태/역할/진행률을 결합하고, plan-stale(노후) 신호를 판정한다.

불변 원칙(C3):
  - PHASE.md 미존재(flag='no-phase-file')와 파싱실패(flag='unknown')는 다른 상태 — 섞지 않음.
  - 추정 채움 금지. 원격 uri/경로부재/탐색실패는 명시 flag로 표면화.
  - read-only(파일 탐색만).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from acp.models import SessionRecord, SessionState
from acp.phase import PhaseDoc, parse_phase_md

logger = logging.getLogger(__name__)

# 상위 탐색 상한 (.git/.hg 경계를 못 만날 때)
_MAX_PARENT_LEVELS = 5
# 경계 디렉토리 마커 — 만나면 그 레벨까지만 탐색
_BOUNDARY_DIRS = (".git", ".hg")
# plan-stale 임계: PHASE.md updated_at이 세션 활동보다 이만큼 과거면 노후
_PLAN_STALE_AGE = timedelta(hours=24)
# 활동중으로 보는 상태(plan-stale 판정 대상)
_ACTIVE_STATES = frozenset({SessionState.LIVE, SessionState.RUNNING, SessionState.IDLE})

PhaseFlag = Literal["ok", "no-phase-file", "unknown"]


class PhaseJoin(BaseModel):
    """세션×PHASE.md 조인 결과. flag(존재/파싱) + plan_stale(노후) 2축."""
    model_config = ConfigDict(frozen=True)

    flag: PhaseFlag = "no-phase-file"
    plan_stale: bool = False
    current_phase: str | None = None
    phase_status: str | None = None
    owner_session: str | None = None
    phases_done: int = 0
    phases_total: int = 0
    phase_source: str | None = None


def _normalize_cwd(project_path: str) -> Path | None:
    """경로 정규화: resolve(대소문자·슬래시·심볼릭 정규화). 원격 uri/실패 → None."""
    if "://" in project_path:
        return None  # vscode-remote 등 원격 uri → 로컬 탐색 불가
    try:
        resolved = Path(project_path).resolve()
    except (OSError, ValueError, RuntimeError):
        return None
    if not resolved.exists():
        return None
    return resolved


def _find_phase_md(start: Path) -> Path | None:
    """start(또는 상위)에서 최근접 PHASE.md 탐색.

    각 레벨에서 PHASE.md를 먼저 확인하고, 그 레벨에 .git/.hg 경계가 있으면
    그 위로는 올라가지 않는다. 경계 없으면 최대 5단계까지.
    """
    cur = start if start.is_dir() else start.parent
    for _ in range(_MAX_PARENT_LEVELS + 1):
        candidate = cur / "PHASE.md"
        if candidate.is_file():
            return candidate
        # 경계 디렉토리를 만나면 이 레벨까지만(상위 탐색 중단)
        if any((cur / b).exists() for b in _BOUNDARY_DIRS):
            return None
        parent = cur.parent
        if parent == cur:  # 파일시스템 루트
            break
        cur = parent
    return None


def _parse_iso(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _is_plan_stale(record: SessionRecord, state: SessionState, doc: PhaseDoc) -> bool:
    """노후 판정: 세션 활동중인데 PHASE.md가 뒤처졌을 때."""
    if state not in _ACTIVE_STATES:
        return False

    # 규칙 2: current_phase 엔트리가 done인데 세션이 아직 활동중
    for p in doc.phases:
        if p.id == doc.current_phase and p.status == "done":
            return True

    # 규칙 1: updated_at이 last_activity보다 24h+ 과거
    if record.last_activity is None or not doc.updated_at:
        return False
    updated = _parse_iso(doc.updated_at)
    if updated is None:
        return False  # 파싱 실패는 stale로 단정하지 않음(추정 금지)
    return (_aware(record.last_activity) - _aware(updated)) > _PLAN_STALE_AGE


def join_phase(record: SessionRecord, state: SessionState) -> PhaseJoin:
    """SessionRecord(+판정상태) → PhaseJoin. PHASE.md 미존재/파싱실패를 명확히 구분."""
    if not record.project_path:
        return PhaseJoin(flag="no-phase-file")

    base = _normalize_cwd(record.project_path)
    if base is None:
        return PhaseJoin(flag="no-phase-file")  # 원격/비정상 경로

    phase_path = _find_phase_md(base)
    if phase_path is None:
        return PhaseJoin(flag="no-phase-file")

    doc = parse_phase_md(phase_path)
    if doc is None:
        # 파일은 있으나 파싱/스키마 실패 → unknown (no-phase-file과 구분, C3)
        return PhaseJoin(flag="unknown", phase_source=str(phase_path))

    done = sum(1 for p in doc.phases if p.status == "done")
    return PhaseJoin(
        flag="ok",
        plan_stale=_is_plan_stale(record, state, doc),
        current_phase=doc.current_phase,
        phase_status=doc.phase_status,
        owner_session=doc.owner_session,
        phases_done=done,
        phases_total=len(doc.phases),
        phase_source=str(phase_path),
    )
