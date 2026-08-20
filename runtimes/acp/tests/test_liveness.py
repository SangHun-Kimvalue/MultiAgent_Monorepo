"""tests/test_liveness.py — derive_state 결정적 단위 테스트 (시간 mock)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


from acp.config import LivenessConfig
from acp.liveness import derive_state
from acp.models import SessionRecord, SessionState

# P0 호환 테스트는 P0 임계값 사용 (기존 테스트 보존)
CFG_P0 = LivenessConfig(idle_threshold=120.0, hold_threshold=300.0, stale_ttl=1800.0)

# P1 테스트는 P1 임계값 사용 (idle=300, hold=900, stale=3600)
CFG = LivenessConfig(idle_threshold=300.0, hold_threshold=900.0, stale_ttl=3600.0)

_NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)


def _record(**kwargs) -> SessionRecord:
    base = dict(app="fake", session_id="test-001", source_file="")
    base.update(kwargs)
    return SessionRecord(**base)


def _dt(offset_seconds: float) -> datetime:
    return _NOW - timedelta(seconds=offset_seconds)


# ════════════════════════════════════════
# P0 호환 테스트 (last_event 없음, 시간 기반 폴백)
# ════════════════════════════════════════

def test_live_recent_activity():
    r = _record(last_activity=_dt(30))
    assert derive_state(r, _NOW, CFG_P0) == SessionState.LIVE


def test_idle_medium_gap():
    r = _record(last_activity=_dt(180))  # 120s < 180 < 300s
    assert derive_state(r, _NOW, CFG_P0) == SessionState.IDLE


def test_time_fallback_hold_band_is_holding():
    """시간 폴백에서 `[hold, stale)`은 HOLDING (과거에는 전부 UNKNOWN으로 뭉갰다).

    "정보 없음"과 "오래 멈춤"을 한 값으로 보고하지 않는다(T14 S2 D3).
    """
    r = _record(last_activity=_dt(600))  # hold=300 초과, stale=1800 미만
    assert derive_state(r, _NOW, CFG_P0) == SessionState.HOLDING


def test_time_fallback_beyond_stale_is_stale():
    r = _record(last_activity=_dt(1800))  # == stale_ttl
    assert derive_state(r, _NOW, CFG_P0) == SessionState.STALE


def test_unknown_no_activity():
    r = _record(last_activity=None)
    assert derive_state(r, _NOW, CFG_P0) == SessionState.UNKNOWN


def test_done_raw_status():
    r = _record(last_activity=_dt(30), raw_status="task_complete_done")
    assert derive_state(r, _NOW, CFG_P0) == SessionState.DONE


def test_error_raw_status():
    r = _record(last_activity=_dt(30), raw_status="REVIEW_REJECTED_error")
    assert derive_state(r, _NOW, CFG_P0) == SessionState.ERROR


def test_exact_idle_boundary():
    """idle_threshold 경계: 119s = LIVE, 120s = IDLE (P0 임계값)."""
    r_just_live = _record(last_activity=_dt(119))
    r_just_idle = _record(last_activity=_dt(120))
    assert derive_state(r_just_live, _NOW, CFG_P0) == SessionState.LIVE
    assert derive_state(r_just_idle, _NOW, CFG_P0) == SessionState.IDLE


def test_clock_skew_future_activity():
    """미래 시각 last_activity (시계 불일치) → UNKNOWN."""
    r = _record(last_activity=_NOW + timedelta(seconds=60))
    assert derive_state(r, _NOW, CFG_P0) == SessionState.UNKNOWN


# ════════════════════════════════════════
# P1 결정테이블 테스트 (last_event 기반)
# ════════════════════════════════════════

# ── Error / Abort ──

def test_error_last_event():
    r = _record(last_event="error", last_activity=_dt(30))
    assert derive_state(r, _NOW, CFG) == SessionState.ERROR


def test_abort_events_route_to_turn_end_not_error():
    """abort는 "완료 없이 끝났다"는 사실만 말한다 — 장애로 단언하지 않는다(T14 S2 D1).

    실측 어휘는 `turn_aborted`이고 `task_aborted`는 0건이지만, 같은 뜻의 두 철자가
    서로 다른 축으로 가면 그 자체가 불일치이므로 함께 턴 종료로 묶는다.
    """
    for event in ("turn_aborted", "task_aborted"):
        fresh = _record(last_event=event, last_activity=_dt(30))
        assert derive_state(fresh, _NOW, CFG) == SessionState.LIVE, event
        old = _record(last_event=event, last_activity=_dt(7200))
        assert derive_state(old, _NOW, CFG) == SessionState.STALE, event


def test_error_no_activity_still_error():
    """last_event=error는 last_activity 없어도 ERROR."""
    r = _record(last_event="error", last_activity=None)
    assert derive_state(r, _NOW, CFG) == SessionState.ERROR


# ── Approval Request → HOLDING ──

def test_exec_approval_request_holding():
    r = _record(last_event="exec_approval_request", last_activity=_dt(30))
    assert derive_state(r, _NOW, CFG, is_alive=lambda p: False) == SessionState.HOLDING


def test_patch_approval_request_holding():
    r = _record(last_event="patch_approval_request", last_activity=_dt(30))
    assert derive_state(r, _NOW, CFG) == SessionState.HOLDING


# ── task_started ──

def test_task_started_pid_alive_running():
    """task_started + PID 살아있음 → RUNNING."""
    r = _record(last_event="task_started", last_activity=_dt(60), running_pid=12345)
    assert derive_state(r, _NOW, CFG, is_alive=lambda p: True) == SessionState.RUNNING


def test_task_started_pid_dead_over_hold_holding():
    """task_started + PID 없음 + age > hold_threshold → HOLDING."""
    r = _record(last_event="task_started", last_activity=_dt(1000))  # 1000 > 900
    assert derive_state(r, _NOW, CFG, is_alive=lambda p: False) == SessionState.HOLDING


def test_task_started_pid_dead_under_hold_running():
    """task_started + PID 없음 + age < hold_threshold → RUNNING (잠정)."""
    r = _record(last_event="task_started", last_activity=_dt(400))  # 300 < 400 < 900
    assert derive_state(r, _NOW, CFG, is_alive=lambda p: False) == SessionState.RUNNING


# ── task_complete + age ──

def test_task_complete_live():
    """task_complete + age < idle_threshold → LIVE."""
    r = _record(last_event="task_complete", last_activity=_dt(100))  # 100 < 300
    assert derive_state(r, _NOW, CFG) == SessionState.LIVE


def test_task_complete_idle():
    """task_complete + idle~hold → IDLE."""
    r = _record(last_event="task_complete", last_activity=_dt(500))  # 300 < 500 < 900
    assert derive_state(r, _NOW, CFG) == SessionState.IDLE


def test_task_complete_holding_ztr():
    """task_complete + hold~stale → HOLDING (ZTR 망각 케이스)."""
    r = _record(last_event="task_complete", last_activity=_dt(1200))  # 900 < 1200 < 3600
    assert derive_state(r, _NOW, CFG) == SessionState.HOLDING


def test_task_complete_stale():
    """task_complete + age >= stale_ttl → STALE."""
    r = _record(last_event="task_complete", last_activity=_dt(4000))  # 4000 > 3600
    assert derive_state(r, _NOW, CFG) == SessionState.STALE


# ── UNKNOWN ──

def test_unknown_no_last_event_no_activity():
    r = _record(last_event=None, last_activity=None)
    assert derive_state(r, _NOW, CFG) == SessionState.UNKNOWN


# ── 경계값 ──

def test_exact_idle_boundary_p1():
    """P1 idle_threshold 경계: 299s = LIVE, 300s = IDLE (task_complete 기반)."""
    r_live = _record(last_event="task_complete", last_activity=_dt(299))
    r_idle = _record(last_event="task_complete", last_activity=_dt(300))
    assert derive_state(r_live, _NOW, CFG) == SessionState.LIVE
    assert derive_state(r_idle, _NOW, CFG) == SessionState.IDLE


def test_exact_hold_boundary_p1():
    """P1 hold_threshold 경계: 899s = IDLE, 900s = HOLDING (task_complete 기반)."""
    r_idle = _record(last_event="task_complete", last_activity=_dt(899))
    r_hold = _record(last_event="task_complete", last_activity=_dt(900))
    assert derive_state(r_idle, _NOW, CFG) == SessionState.IDLE
    assert derive_state(r_hold, _NOW, CFG) == SessionState.HOLDING


def test_exact_stale_boundary_p1():
    """P1 stale_ttl 경계: 3599s = HOLDING, 3600s = STALE (task_complete 기반)."""
    r_hold = _record(last_event="task_complete", last_activity=_dt(3599))
    r_stale = _record(last_event="task_complete", last_activity=_dt(3600))
    assert derive_state(r_hold, _NOW, CFG) == SessionState.HOLDING
    assert derive_state(r_stale, _NOW, CFG) == SessionState.STALE


# ════════════════════════════════════════
# P1.5 MAJOR-1: in-turn 일반화 테스트
# (task_started 리터럴 한정이 아닌 모든 진행중 event_msg)
# ════════════════════════════════════════

def test_agent_message_pid_alive_running():
    """in-turn: last_event=agent_message + PID 살아있음 → RUNNING."""
    r = _record(last_event="agent_message", last_activity=_dt(60), running_pid=12345)
    assert derive_state(r, _NOW, CFG, is_alive=lambda p: True) == SessionState.RUNNING


def test_token_count_pid_dead_over_hold_holding():
    """in-turn: last_event=token_count + PID 없음 + age > hold_threshold → HOLDING."""
    r = _record(last_event="token_count", last_activity=_dt(1000))  # 1000 > 900
    assert derive_state(r, _NOW, CFG, is_alive=lambda p: False) == SessionState.HOLDING


def test_patch_apply_end_pid_dead_under_hold_running():
    """in-turn: last_event=patch_apply_end + PID 없음 + age ≤ hold_threshold → RUNNING(잠정)."""
    r = _record(last_event="patch_apply_end", last_activity=_dt(400))  # 300 < 400 < 900
    assert derive_state(r, _NOW, CFG, is_alive=lambda p: False) == SessionState.RUNNING


def test_context_compacted_pid_alive_running():
    """in-turn: last_event=context_compacted + PID 살아있음 → RUNNING."""
    r = _record(last_event="context_compacted", last_activity=_dt(30), running_pid=99)
    assert derive_state(r, _NOW, CFG, is_alive=lambda p: True) == SessionState.RUNNING


def test_user_message_pid_alive_running():
    """in-turn: last_event=user_message + PID 살아있음 → RUNNING."""
    r = _record(last_event="user_message", last_activity=_dt(15), running_pid=1)
    assert derive_state(r, _NOW, CFG, is_alive=lambda p: True) == SessionState.RUNNING


# ── in-turn이 task_complete보다 먼저 실행되지 않음을 확인 (분기 순서 회귀방지) ──

def test_task_complete_not_swallowed_by_inturn():
    """task_complete는 in-turn 분기(6번)로 빠지지 않고 나이 기반(5번)으로 처리됨."""
    r = _record(last_event="task_complete", last_activity=_dt(100))
    # in-turn이라면 is_alive=True → RUNNING 반환; 그러나 task_complete이므로 LIVE여야 함
    assert derive_state(r, _NOW, CFG, is_alive=lambda p: True) == SessionState.LIVE


# ── P1.5 MINOR-2: raw_status는 last_event=None일 때만 폴백 ──

def test_raw_status_ignored_when_last_event_present():
    """last_event=agent_message + raw_status=done → raw_status 무시, RUNNING 판정."""
    r = _record(
        last_event="agent_message",
        last_activity=_dt(60),
        running_pid=1,
        raw_status="task_complete_done",  # 과거 잔여 raw_status
    )
    assert derive_state(r, _NOW, CFG, is_alive=lambda p: True) == SessionState.RUNNING



# ════════════════════════════════════════
# T14 S2 — 구간 경계는 **모든 경로에서 동일**해야 한다 (D2·D3)
# ════════════════════════════════════════

_EPS = 0.001


def _boundary_cases(cfg):
    """(elapsed, 기대 상태) — [hold, stale)=HOLDING, [stale, ∞)=STALE."""
    return [
        (cfg.hold_threshold - _EPS, "before-hold"),
        (cfg.hold_threshold, SessionState.HOLDING),
        (cfg.stale_ttl - _EPS, SessionState.HOLDING),
        (cfg.stale_ttl, SessionState.STALE),
    ]


def test_in_turn_boundaries_match_turn_end_path():
    """in-turn 경로에도 stale 단계가 있고, hold 경계가 턴 종료 경로와 같다.

    과거 `elapsed > hold`였던 탓에 정확히 hold일 때만 RUNNING이 되어, 같은 임계값이
    이벤트 종류에 따라 열린/닫힌 구간으로 갈렸다. 또 stale 단계가 없어 완료 이벤트 없이
    닫힌 세션이 나이와 무관하게 영구 HOLDING이었다.
    """
    for elapsed, expected in _boundary_cases(CFG):
        r = _record(last_event="agent_message", last_activity=_dt(elapsed))
        actual = derive_state(r, _NOW, CFG, is_alive=lambda _: False)
        if expected == "before-hold":
            assert actual == SessionState.RUNNING, elapsed
        else:
            assert actual == expected, elapsed


def test_turn_end_boundaries():
    for elapsed, expected in _boundary_cases(CFG):
        r = _record(last_event="task_complete", last_activity=_dt(elapsed))
        actual = derive_state(r, _NOW, CFG)
        if expected == "before-hold":
            assert actual == SessionState.IDLE, elapsed
        else:
            assert actual == expected, elapsed


def test_time_fallback_boundaries():
    for elapsed, expected in _boundary_cases(CFG):
        r = _record(last_activity=_dt(elapsed))
        actual = derive_state(r, _NOW, CFG)
        if expected == "before-hold":
            assert actual == SessionState.IDLE, elapsed
        else:
            assert actual == expected, elapsed


def test_unknown_only_for_undecidable_inputs():
    """UNKNOWN이 남는 경로는 판정 불가 둘뿐 — last_activity 없음 / 시계 역전."""
    assert derive_state(_record(last_activity=None), _NOW, CFG) == SessionState.UNKNOWN
    future = _record(last_activity=_dt(-3600))  # now보다 미래 → elapsed < 0
    assert derive_state(future, _NOW, CFG) == SessionState.UNKNOWN

    # 그 외 시간대는 어떤 이벤트 축에서도 UNKNOWN이 나오지 않는다.
    for event in (None, "task_complete", "turn_aborted", "agent_message"):
        for elapsed in (0.0, CFG.idle_threshold, CFG.hold_threshold, CFG.stale_ttl, CFG.stale_ttl * 100):
            r = _record(last_event=event, last_activity=_dt(elapsed))
            assert derive_state(r, _NOW, CFG, is_alive=lambda _: False) != SessionState.UNKNOWN, (event, elapsed)
