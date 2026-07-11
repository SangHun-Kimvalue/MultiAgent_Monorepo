"""acp/dedupe.py — 상태 전이 알림 중복 억제."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from acp.config import NotifyConfig
from acp.models import SessionState

NOTIFY_STATES = frozenset({SessionState.HOLDING, SessionState.STALE, SessionState.ERROR})
RESET_STATES = frozenset({SessionState.LIVE, SessionState.RUNNING, SessionState.IDLE})


def _state_value(state: SessionState | str | None) -> str | None:
    if state is None:
        return None
    return state.value if isinstance(state, SessionState) else str(state)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class NotificationDedupe:
    """세션별 마지막 알림 상태/시각을 기준으로 전이 알림 발행 여부를 결정."""

    config: NotifyConfig

    def should_notify(self, previous_row: dict[str, Any] | None, to_state: SessionState, now: datetime) -> bool:
        """알림 대상 상태이고, 동일 상태가 쿨다운 내 발행되지 않았으면 True."""
        if to_state not in NOTIFY_STATES:
            return False

        if not previous_row:
            return True

        state_value = to_state.value
        last_state = previous_row.get("last_notified_state")
        last_at = _parse_iso(previous_row.get("last_notified_at"))
        if last_state != state_value or last_at is None:
            return True

        cooldown = max(float(self.config.notify_cooldown), 0.0)
        return (now - last_at).total_seconds() >= cooldown

    def should_reset(self, previous_row: dict[str, Any] | None, to_state: SessionState) -> bool:
        """복귀 상태 진입 시 같은 알림 상태를 새 이벤트로 인정하기 위한 reset 여부."""
        if to_state not in RESET_STATES:
            return False
        return bool(previous_row and previous_row.get("last_notified_state"))


def is_notification_state(state: SessionState | str | None) -> bool:
    """상태가 P3 알림 대상인지 반환."""
    value = _state_value(state)
    return any(value == state.value for state in NOTIFY_STATES)
