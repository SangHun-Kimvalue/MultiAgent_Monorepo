"""CircuitBreaker — 에이전트 장애 감지 + fallback 자동 전환.

3-state: CLOSED → OPEN → HALF_OPEN.
적응형 cooldown: 연속 OPEN 전이 시 cooldown이 exponential로 증가 (최대 cap).
상태 이력 추적: 전이 기록을 리스트로 보관 (UI에서 시각화용).
"""
from __future__ import annotations

import logging
import time
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = "closed"        # 정상. 에러 카운트 누적 중.
    OPEN = "open"            # 차단. cooldown 타이머 진행 중.
    HALF_OPEN = "half_open"  # cooldown 후 1회 시도 허용.


class CircuitBreaker:
    """에이전트별 3-state 서킷 브레이커.

    CLOSED: 정상 운영. 연속 실패가 threshold에 도달하면 → OPEN
    OPEN: 에이전트 차단. cooldown 경과 후 → HALF_OPEN
    HALF_OPEN: 1회 시도 허용. 성공 → CLOSED, 실패 → OPEN

    적응형 cooldown:
        첫 OPEN: base_cooldown (60s)
        HALF_OPEN에서 재실패: cooldown * 2 (120s)
        또 재실패: cooldown * 2 (240s)
        ...최대 max_cooldown (600s)
        CLOSED로 복귀하면 cooldown 리셋
    """

    def __init__(
        self,
        agent_id: str = "",
        failure_threshold: int = 3,
        cooldown_sec: float = 60.0,
        max_cooldown_sec: float = 600.0,
    ) -> None:
        self.agent_id = agent_id
        self._threshold = failure_threshold
        self._base_cooldown = cooldown_sec
        self._max_cooldown = max_cooldown_sec
        self._current_cooldown = cooldown_sec
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._total_failures = 0
        self._total_successes = 0
        self._consecutive_opens = 0  # 연속 OPEN 횟수
        self._state_history: list[dict[str, Any]] = []

    def _record_transition(self, from_state: str, to_state: str, reason: str) -> None:
        """상태 전이를 기록한다."""
        self._state_history.append({
            "from": from_state,
            "to": to_state,
            "reason": reason,
            "cooldown": self._current_cooldown,
            "timestamp": time.monotonic(),
        })
        # 최근 50건만 보관
        if len(self._state_history) > 50:
            self._state_history = self._state_history[-50:]

    @property
    def state(self) -> CircuitState:
        """현재 상태 (시간 경과에 따라 자동 전이 포함)."""
        if self._state == CircuitState.OPEN:
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self._current_cooldown:
                old = self._state.value
                self._state = CircuitState.HALF_OPEN
                self._record_transition(old, "half_open", f"{elapsed:.1f}s elapsed")
                logger.info(
                    "CircuitBreaker[%s]: OPEN -> HALF_OPEN (%.1fs, cooldown=%.0fs)",
                    self.agent_id, elapsed, self._current_cooldown,
                )
        return self._state

    @property
    def current_cooldown(self) -> float:
        """현재 적용 중인 cooldown 시간."""
        return self._current_cooldown

    def can_execute(self) -> bool:
        """현재 에이전트를 호출해도 되는지 판단."""
        current = self.state  # 시간 기반 전이 포함
        return current in (CircuitState.CLOSED, CircuitState.HALF_OPEN)

    def record_success(self) -> None:
        """호출 성공 기록. HALF_OPEN → CLOSED 전이. cooldown 리셋."""
        self._failure_count = 0
        self._total_successes += 1
        old = self._state.value
        if self._state == CircuitState.HALF_OPEN:
            self._record_transition(old, "closed", "success")
            logger.info("CircuitBreaker[%s]: HALF_OPEN -> CLOSED (성공)", self.agent_id)
        self._state = CircuitState.CLOSED
        # cooldown 리셋
        self._current_cooldown = self._base_cooldown
        self._consecutive_opens = 0

    def record_failure(self) -> None:
        """호출 실패 기록. threshold 도달 시 OPEN 전이. 적응형 cooldown 적용."""
        self._failure_count += 1
        self._total_failures += 1

        if self._state == CircuitState.HALF_OPEN:
            # HALF_OPEN에서 실패 → OPEN + cooldown 2배
            old = self._state.value
            self._state = CircuitState.OPEN
            self._last_failure_time = time.monotonic()
            self._consecutive_opens += 1
            self._current_cooldown = min(
                self._base_cooldown * (2 ** (self._consecutive_opens - 1)),
                self._max_cooldown,
            )
            self._record_transition(old, "open", f"re-fail (cooldown={self._current_cooldown:.0f}s)")
            logger.warning(
                "CircuitBreaker[%s]: HALF_OPEN -> OPEN (재실패, cooldown=%.0fs)",
                self.agent_id, self._current_cooldown,
            )
        elif self._failure_count >= self._threshold:
            old = self._state.value
            self._state = CircuitState.OPEN
            self._last_failure_time = time.monotonic()
            self._consecutive_opens += 1
            self._current_cooldown = min(
                self._base_cooldown * (2 ** (self._consecutive_opens - 1)),
                self._max_cooldown,
            )
            self._record_transition(old, "open", f"{self._failure_count} failures")
            logger.warning(
                "CircuitBreaker[%s]: CLOSED -> OPEN (연속 %d회, cooldown=%.0fs)",
                self.agent_id, self._failure_count, self._current_cooldown,
            )

    def reset(self) -> None:
        """상태 초기화 (테스트용)."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._current_cooldown = self._base_cooldown
        self._consecutive_opens = 0

    def stats(self) -> dict[str, object]:
        """통계 반환."""
        return {
            "agent_id": self.agent_id,
            "state": self.state.value,
            "failure_count": self._failure_count,
            "total_failures": self._total_failures,
            "total_successes": self._total_successes,
            "current_cooldown": self._current_cooldown,
            "consecutive_opens": self._consecutive_opens,
            "history": self._state_history[-10:],  # 최근 10건
        }
