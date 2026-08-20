"""tests/test_dedupe.py — 알림 dedupe 결정 테스트."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from acp.config import NotifyConfig
from acp.dedupe import NotificationDedupe, is_notification_state
from acp.models import SessionState


def test_notification_state_matrix():
    """알림은 action 등급(HOLDING/ERROR)만. STALE은 cleanup 등급이라 제외(T14 S2 D4).

    STALE은 "사실상 종료 = 정리 대상"이라, 30일 전 멈춘 세션의 상태 이름이 바뀐 것을
    알릴 이유가 없다. 화면에는 그대로 표시되므로 침묵이 아니라 채널 분리다.
    """
    assert is_notification_state(SessionState.HOLDING)
    assert is_notification_state(SessionState.ERROR)
    assert not is_notification_state(SessionState.STALE)

    for state in (SessionState.LIVE, SessionState.RUNNING, SessionState.IDLE, SessionState.DONE, SessionState.UNKNOWN):
        assert not is_notification_state(state)


def test_dedupe_suppresses_same_state_within_cooldown():
    now = datetime.now(timezone.utc)
    dedupe = NotificationDedupe(NotifyConfig(notify_cooldown=3600))
    previous = {
        "last_notified_state": "holding",
        "last_notified_at": (now - timedelta(seconds=60)).isoformat(),
    }

    assert not dedupe.should_notify(previous, SessionState.HOLDING, now)


def test_dedupe_allows_state_escalation_and_cooldown_expiry():
    now = datetime.now(timezone.utc)
    dedupe = NotificationDedupe(NotifyConfig(notify_cooldown=3600))
    previous = {
        "last_notified_state": "holding",
        "last_notified_at": (now - timedelta(seconds=60)).isoformat(),
    }

    # HOLDING → ERROR 승격은 쿨다운 안이어도 알린다(다른 상태이므로).
    assert dedupe.should_notify(previous, SessionState.ERROR, now)
    # STALE은 알림 축이 아니다.
    assert not dedupe.should_notify(previous, SessionState.STALE, now)

    expired = {
        "last_notified_state": "holding",
        "last_notified_at": (now - timedelta(seconds=3601)).isoformat(),
    }
    assert dedupe.should_notify(expired, SessionState.HOLDING, now)


def test_dedupe_reset_only_on_recovery_states():
    dedupe = NotificationDedupe(NotifyConfig())
    previous = {"last_notified_state": "holding", "last_notified_at": "2026-06-11T00:00:00+00:00"}

    assert dedupe.should_reset(previous, SessionState.LIVE)
    assert dedupe.should_reset(previous, SessionState.RUNNING)
    assert dedupe.should_reset(previous, SessionState.IDLE)
    assert not dedupe.should_reset(previous, SessionState.STALE)
    assert not dedupe.should_reset({}, SessionState.LIVE)
