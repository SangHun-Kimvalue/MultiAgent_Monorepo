"""tests/test_reclassification_notify.py — 재분류 알림 폭주 차단 회귀 (T14 S2 D7).

판정 규칙이 바뀌면 오래 멈춰 있던 세션 수백 건이 한 틱에 상태를 옮긴다. 그건
"지금 일어난 사건"이 아니라 재분류이므로 알리지 않는다. 다만 명시적 오류 사건은
활동 시각과 무관하게 알린다 — 정말 알려야 할 것을 침묵시키면 그 자체가 결함이다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from acp.config import NotifyConfig
from acp.dedupe import NotificationDedupe
from acp.models import SessionState

NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
STALE_TTL = 3600.0
HOLD = 900.0

_DEDUPE = NotificationDedupe(NotifyConfig(notify_cooldown=3600))


def _notify(elapsed, state=SessionState.HOLDING, explicit=False, previous=None):
    return _DEDUPE.should_notify(
        previous,
        state,
        NOW,
        elapsed=elapsed,
        stale_ttl=STALE_TTL,
        explicit_event=explicit,
    )


def test_recent_transition_still_notifies():
    """`hold <= elapsed < stale`의 전이는 정상 알림 — 게이트가 삼키면 안 된다."""
    assert _notify(HOLD) is True
    assert _notify(STALE_TTL - 1) is True


def test_reclassified_old_session_is_silent():
    """활동이 stale 임계를 넘긴 세션의 전이는 재분류 — 알리지 않는다."""
    assert _notify(STALE_TTL) is False
    assert _notify(STALE_TTL * 100) is False


def test_bulk_reclassification_produces_no_notifications():
    """오래된 UNKNOWN 다건이 HOLDING으로 일괄 재판정돼도 알림은 0건."""
    elapsed_values = [STALE_TTL + i for i in range(300)]
    assert sum(1 for e in elapsed_values if _notify(e)) == 0


def test_explicit_error_event_bypasses_freshness_gate():
    """명시적 오류는 활동 시각과 무관하게 알린다(계산 불가 포함)."""
    assert _notify(STALE_TTL * 10, state=SessionState.ERROR, explicit=True) is True
    assert _notify(None, state=SessionState.ERROR, explicit=True) is True
    # elapsed를 계산할 수 없으면 게이트를 적용하지 않는다.
    assert _notify(None, state=SessionState.HOLDING) is True


def test_stale_is_never_notified_regardless_of_freshness():
    """STALE은 cleanup 등급이라 알림 축이 아니다(T14 S2 D4)."""
    assert _notify(1.0, state=SessionState.STALE) is False
    assert _notify(None, state=SessionState.STALE, explicit=True) is False


def test_gate_absent_when_thresholds_not_supplied():
    """호출자가 임계값을 주지 않으면 게이트를 적용하지 않는다(하위호환)."""
    assert _DEDUPE.should_notify(None, SessionState.HOLDING, NOW) is True


def test_cooldown_still_applies_within_fresh_window():
    """freshness를 통과해도 기존 쿨다운 계약은 그대로다."""
    previous = {
        "last_notified_state": "holding",
        "last_notified_at": (NOW - timedelta(seconds=60)).isoformat(),
    }
    assert _notify(HOLD, previous=previous) is False
