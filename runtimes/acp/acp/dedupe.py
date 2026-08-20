"""acp/dedupe.py — 상태 전이 알림 중복 억제."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from acp.config import NotifyConfig
from acp.models import SessionState

# 알림 대상 = **지금 사람이 손대야 하는** 상태(action 등급).
# STALE은 "사실상 종료 = 정리 대상"(cleanup 등급)이라 제외한다 — 30일 전 멈춘 세션의
# 상태 이름이 바뀐 것을 알릴 이유가 없다. 화면에는 그대로 표시되므로 침묵이 아니라
# 채널 분리다(T14 S2 D4).
NOTIFY_STATES = frozenset({SessionState.HOLDING, SessionState.ERROR})
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

    def should_notify(
        self,
        previous_row: dict[str, Any] | None,
        to_state: SessionState,
        now: datetime,
        *,
        elapsed: float | None = None,
        stale_ttl: float | None = None,
        explicit_event: bool = False,
    ) -> bool:
        """알림 대상 상태이고, 동일 상태가 쿨다운 내 발행되지 않았으면 True.

        `elapsed`(마지막 활동 이후 초)와 `explicit_event`는 **재분류 알림 폭주**를 막는다
        (T14 S2 D7). 판정 규칙이 바뀌면 오래 멈춰 있던 세션 수백 건이 한 틱에 상태를
        옮기는데, 그건 "지금 일어난 사건"이 아니라 재분류다.

        - `explicit_event=True`(명시적 오류 이벤트 등) 또는 `elapsed`/`stale_ttl`이 없으면
          freshness 게이트를 **적용하지 않는다** — 정말 알려야 할 사건을 침묵시키지 않는다.
        - 그 외 시간 기반 판정: `elapsed < stale_ttl`일 때만 알린다.

        `stale_ttl`은 liveness 설정값을 호출자가 그대로 넘긴다(여기서 다시 정의하면
        임계값이 두 곳에 생겨 드리프트한다).
        """
        if to_state not in NOTIFY_STATES:
            return False

        if not explicit_event and elapsed is not None and stale_ttl is not None:
            if elapsed >= max(float(stale_ttl), 0.0):
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
