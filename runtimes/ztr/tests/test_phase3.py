"""Phase 3 테스트 — CircuitBreaker."""
from __future__ import annotations

import time
from src.engine.circuit_breaker import CircuitBreaker, CircuitState


# ── CircuitBreaker ──


class TestCircuitBreaker:
    def test_initial_state_closed(self) -> None:
        cb = CircuitBreaker(agent_id="test")
        assert cb.state == CircuitState.CLOSED
        assert cb.can_execute() is True

    def test_opens_after_threshold(self) -> None:
        cb = CircuitBreaker(agent_id="test", failure_threshold=2)
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.can_execute() is False

    def test_half_open_after_cooldown(self) -> None:
        cb = CircuitBreaker(agent_id="test", failure_threshold=1, cooldown_sec=0.01)
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        time.sleep(0.02)
        assert cb.state == CircuitState.HALF_OPEN
        assert cb.can_execute() is True

    def test_success_resets_to_closed(self) -> None:
        cb = CircuitBreaker(agent_id="test", failure_threshold=1, cooldown_sec=0.01)
        cb.record_failure()
        time.sleep(0.02)
        cb.record_success()
        assert cb.state == CircuitState.CLOSED

    def test_half_open_failure_reopens(self) -> None:
        cb = CircuitBreaker(agent_id="test", failure_threshold=1, cooldown_sec=0.01)
        cb.record_failure()
        time.sleep(0.02)
        assert cb.state == CircuitState.HALF_OPEN
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_stats(self) -> None:
        cb = CircuitBreaker(agent_id="test")
        cb.record_success()
        cb.record_failure()
        s = cb.stats()
        assert s["total_successes"] == 1
        assert s["total_failures"] == 1


