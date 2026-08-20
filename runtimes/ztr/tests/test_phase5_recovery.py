"""Phase 5-3 단위 테스트 — CircuitBreaker 고도화, 라운드 로그."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.engine.circuit_breaker import CircuitBreaker, CircuitState
from src.engine.session_store import SessionStore


# ════════════════════════════════════════════
# CircuitBreaker 적응형 cooldown
# ════════════════════════════════════════════


class TestAdaptiveCooldown:
    """적응형 cooldown: 연속 OPEN 시 exponential 증가, 성공 시 리셋."""

    def test_initial_cooldown(self) -> None:
        cb = CircuitBreaker(agent_id="test", cooldown_sec=10.0)
        assert cb.current_cooldown == 10.0

    def test_cooldown_doubles_on_reopen(self) -> None:
        """HALF_OPEN에서 재실패 시 cooldown 2배."""
        cb = CircuitBreaker(agent_id="test", failure_threshold=1, cooldown_sec=10.0, max_cooldown_sec=320.0)

        # 첫 실패 -> OPEN
        cb.record_failure()
        assert cb._state == CircuitState.OPEN
        assert cb.current_cooldown == 10.0

        # 강제 HALF_OPEN 전이
        cb._state = CircuitState.HALF_OPEN

        # HALF_OPEN에서 재실패 -> OPEN, cooldown 2배
        cb.record_failure()
        assert cb._state == CircuitState.OPEN
        assert cb.current_cooldown == 20.0

        # 다시 HALF_OPEN
        cb._state = CircuitState.HALF_OPEN
        cb.record_failure()
        assert cb.current_cooldown == 40.0

    def test_cooldown_max_cap(self) -> None:
        """cooldown이 max_cooldown을 초과하지 않음."""
        cb = CircuitBreaker(
            agent_id="test", failure_threshold=1,
            cooldown_sec=100.0, max_cooldown_sec=300.0,
        )
        cb.record_failure()  # OPEN, cooldown=100
        cb._state = CircuitState.HALF_OPEN
        cb.record_failure()  # OPEN, cooldown=200
        cb._state = CircuitState.HALF_OPEN
        cb.record_failure()  # OPEN, cooldown=min(400, 300)=300
        assert cb.current_cooldown == 300.0

    def test_cooldown_reset_on_success(self) -> None:
        """성공 시 cooldown과 consecutive_opens 리셋."""
        cb = CircuitBreaker(agent_id="test", failure_threshold=1, cooldown_sec=10.0)
        cb.record_failure()  # OPEN
        cb._state = CircuitState.HALF_OPEN
        cb.record_failure()  # cooldown=20
        assert cb.current_cooldown == 20.0

        cb._state = CircuitState.HALF_OPEN
        cb.record_success()  # CLOSED, cooldown 리셋
        assert cb.current_cooldown == 10.0
        assert cb._consecutive_opens == 0


class TestStateHistory:
    """상태 전이 이력 추적."""

    def test_history_recorded(self) -> None:
        cb = CircuitBreaker(agent_id="test", failure_threshold=1, cooldown_sec=10.0)
        cb.record_failure()  # CLOSED -> OPEN
        cb._state = CircuitState.HALF_OPEN  # 수동 전이 (시간 경과 시뮬)
        cb.record_success()  # HALF_OPEN -> CLOSED

        assert len(cb._state_history) >= 1

    def test_history_in_stats(self) -> None:
        cb = CircuitBreaker(agent_id="test", failure_threshold=1, cooldown_sec=10.0)
        cb.record_failure()
        stats = cb.stats()
        assert "history" in stats
        assert "current_cooldown" in stats
        assert "consecutive_opens" in stats

    def test_history_max_size(self) -> None:
        """이력이 50건을 초과하지 않음."""
        cb = CircuitBreaker(agent_id="test", failure_threshold=1, cooldown_sec=0.0)
        for _ in range(60):
            cb.record_failure()
            cb._state = CircuitState.HALF_OPEN
        assert len(cb._state_history) <= 50


# ════════════════════════════════════════════
# SessionStore 라운드 로그
# ════════════════════════════════════════════


class TestRoundLogs:
    """round_logs 테이블 CRUD."""

    @pytest.fixture()
    def store(self, tmp_path: Path) -> SessionStore:
        s = SessionStore(str(tmp_path / "test.db"))
        yield s  # type: ignore[misc]
        s.close()

    def test_log_round_and_retrieve(self, store: SessionStore) -> None:
        sid = store.create_session(task="라운드 로그 테스트")
        store.log_round(
            sid,
            round_number=1,
            writer_id="gemini-pro",
            writer_content="def greet(name): return f'Hello {name}'",
            nitpicker_status="pass",
            nitpicker_content="ruff: 0 errors",
            critic_id="claude-primary",
            critic_content="[STATUS: PASS]\n코드가 깔끔합니다.",
            verdict="pass",
            reasoning="Critic이 [STATUS: PASS] 판정",
            parse_method="status_keyword",
            findings=[{"severity": "minor", "message": "naming convention"}],
            elapsed_ms=32500.0,
        )

        logs = store.get_round_logs(sid)
        assert len(logs) == 1
        r = logs[0]
        assert r["round_number"] == 1
        assert r["writer_id"] == "gemini-pro"
        assert "greet" in r["writer_content"]
        assert r["critic_id"] == "claude-primary"
        assert r["verdict"] == "pass"
        assert r["parse_method"] == "status_keyword"
        assert len(r["findings"]) == 1
        assert r["findings"][0]["severity"] == "minor"

    def test_multiple_rounds(self, store: SessionStore) -> None:
        sid = store.create_session(task="멀티 라운드")
        for i in range(3):
            store.log_round(
                sid, round_number=i + 1,
                writer_id=f"writer-{i}", critic_id=f"critic-{i}",
                verdict="fail" if i < 2 else "pass",
            )
        logs = store.get_round_logs(sid)
        assert len(logs) == 3
        assert logs[0]["verdict"] == "fail"
        assert logs[2]["verdict"] == "pass"

    def test_empty_round_logs(self, store: SessionStore) -> None:
        sid = store.create_session(task="빈 세션")
        assert store.get_round_logs(sid) == []

    def test_findings_json_parsing(self, store: SessionStore) -> None:
        """findings_json이 올바르게 파싱되는지."""
        sid = store.create_session(task="findings 파싱")
        store.log_round(
            sid, round_number=1,
            findings=[
                {"severity": "blocker", "message": "race condition"},
                {"severity": "major", "message": "no error handling"},
            ],
        )
        logs = store.get_round_logs(sid)
        assert len(logs[0]["findings"]) == 2
        assert logs[0]["findings"][0]["severity"] == "blocker"


