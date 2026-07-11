"""acp/liveness.py — 세션 상태 판정 (순수함수, 결정적).

핵심 불변식:
- derive_state(record, now, cfg, is_alive) 는 **순수함수**.
  - 내부에서 datetime.now() / OS 프로세스 조회 호출 금지 → 주입(테스트 mock 가능).
  - 부작용 없음, DB 접근 없음.
- P1.5 결정테이블 (last_event + is_alive + age):
  last_event                        | is_alive | age          | → state
  ───────────────────────────────────────────────────────────────────────
  error / task_aborted              |    —     |     —        | ERROR
  *_approval_request                |    —     |     —        | HOLDING
  task_complete                     |    —     | < idle       | LIVE
  task_complete                     |    —     | idle~hold    | IDLE
  task_complete                     |    —     | hold~stale   | HOLDING (ZTR 망각)
  task_complete                     |    —     | ≥ stale      | STALE
  in-turn (task_started/agent_message/  |   True   |     —        | RUNNING
   token_count/patch_apply_end/     |  False   | > hold       | HOLDING
   context_compacted/user_message/  |  False   | ≤ hold       | RUNNING(잠정)
   그 외 완료·에러·승인 이외의 event_msg)
  last_event=None, raw_status 있음  |    —     |     —        | DONE/ERROR (폴백)
  last_event=None, 시간 기반        |    —     | < idle       | LIVE
                                    |    —     | idle~hold    | IDLE
                                    |    —     | else         | UNKNOWN

  ★ P1.5 핵심 수정: "in-turn"은 task_started 리터럴이 아니라
    "task_complete/error/aborted/approval 이외의 모든 non-None event_msg"로 일반화.
    실제 진행중 세션의 마지막 event_msg는 대개 agent_message/token_count 등이기 때문.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from acp.config import LivenessConfig
from acp.models import SessionRecord, SessionState

# task_complete로 확정 종료된 이벤트 타입
_COMPLETE_EVENTS = frozenset({"task_complete"})
# 에러/중단 이벤트 타입
_ERROR_EVENTS = frozenset({"error", "task_aborted"})


def _utc_now_if_naive(dt: datetime) -> datetime:
    """naive datetime을 UTC로 간주."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def derive_state(
    record: SessionRecord,
    now: datetime,
    cfg: LivenessConfig,
    is_alive: Callable[[int | None], bool] = lambda _: False,
    # ^ 기본값 False = 주입 누락 시 PID 신호 없음(보수적 판정). 테스트에서 명시 주입 권장.
) -> SessionState:
    """SessionRecord + 현재 시각 + 설정 + PID 생존 조회로 SessionState를 결정적으로 반환.

    Args:
        record:   수집된 세션 레코드.
        now:      현재 UTC 시각 (주입 — 내부 datetime.now() 호출 금지).
        cfg:      임계값 설정.
        is_alive: PID 생존 여부 확인 콜러블 (테스트에서 fake 주입 가능).
                  내부 OS 호출 금지 — 콜러블로 위임.
                  기본값 False = 주입 누락 시 PID 신호 없음(보수적, RUNNING 잠정).
    """
    last_ev = record.last_event

    # ── 1. 에러/중단 (즉시 반환) ─────────────────────────────────────────
    if last_ev in _ERROR_EVENTS:
        return SessionState.ERROR

    # ── 2. 승인대기 → HOLDING (즉시 반환) ───────────────────────────────
    if last_ev and "approval_request" in last_ev:
        return SessionState.HOLDING

    # ── 3. last_activity 없음 → UNKNOWN ──────────────────────────────────
    if record.last_activity is None:
        return SessionState.UNKNOWN

    last = _utc_now_if_naive(record.last_activity)
    current = _utc_now_if_naive(now)
    elapsed = (current - last).total_seconds()

    # ── 4. 시계 불일치 방어 (C3) ─────────────────────────────────────────
    if elapsed < 0:
        return SessionState.UNKNOWN

    # ── 5. task_complete → 나이 기반 (ZTR 망각 케이스 포함) ──────────────
    if last_ev == "task_complete":
        if elapsed < cfg.idle_threshold:
            return SessionState.LIVE
        elif elapsed < cfg.hold_threshold:
            return SessionState.IDLE
        elif elapsed < cfg.stale_ttl:
            return SessionState.HOLDING  # ZTR 망각 케이스
        else:
            return SessionState.STALE

    # ── 6. in-turn: 완료·에러·승인 이외의 모든 non-None event_msg ────────
    # task_started / agent_message / token_count / patch_apply_end /
    # context_compacted / user_message 등 진행중 턴의 마지막 신호.
    # ★ task_started 리터럴 한정이 아님 — 실제 진행중 세션은 대개
    #   agent_message / token_count 등이 마지막 event_msg.
    if last_ev is not None:
        if is_alive(record.running_pid):
            return SessionState.RUNNING
        elif elapsed > cfg.hold_threshold:
            return SessionState.HOLDING
        else:
            return SessionState.RUNNING  # 잠정 (PID 없지만 아직 hold 미만)

    # ── 7. last_event=None: raw_status 폴백 (FakeCollector/non-Codex 하위호환) ─
    # last_event가 있으면 6번에서 이미 반환됨. 여기 도달 = last_ev is None.
    if record.raw_status:
        status_lower = record.raw_status.lower()
        if any(k in status_lower for k in ("done", "complete", "finished")):
            return SessionState.DONE
        if any(k in status_lower for k in ("error", "fail", "exception")):
            return SessionState.ERROR

    # ── 8. 시간 기반 폴백 (last_event=None + raw_status 없음) ────────────
    # FakeCollector/P0 하위 호환 + last_event 미지원 앱(P2 Claude/Cursor 초기).
    if elapsed < cfg.idle_threshold:
        return SessionState.LIVE
    elif elapsed < cfg.hold_threshold:
        return SessionState.IDLE
    else:
        return SessionState.UNKNOWN
