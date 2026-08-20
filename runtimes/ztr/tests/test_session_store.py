"""Phase 5-2 단위 테스트 — SessionStore CRUD + 메트릭 + 통계."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.engine.session_store import SessionStore


@pytest.fixture()
def store(tmp_path: Path) -> SessionStore:
    """임시 DB로 SessionStore를 생성한다."""
    db = tmp_path / "test_sessions.db"
    s = SessionStore(str(db))
    yield s  # type: ignore[misc]
    s.close()


# ── 세션 CRUD ──


class TestSessionCRUD:
    def test_create_session(self, store: SessionStore) -> None:
        sid = store.create_session(task="JWT 갱신", target_file="src/auth.py")
        assert sid >= 1

        session = store.get_session(sid)
        assert session is not None
        assert session["task"] == "JWT 갱신"
        assert session["target_file"] == "src/auth.py"
        assert session["status"] == "running"
        assert session["final_verdict"] is None

    def test_create_with_config_snapshot(self, store: SessionStore) -> None:
        sid = store.create_session(
            task="테스트",
            config_snapshot={"max_rounds": 5, "agents": ["gemini"]},
        )
        session = store.get_session(sid)
        assert session is not None
        assert '"max_rounds": 5' in session["config_snapshot"]

    def test_finish_session_pass(self, store: SessionStore) -> None:
        sid = store.create_session(task="패스 테스트")
        store.finish_session(sid, verdict="pass", rounds=2)

        session = store.get_session(sid)
        assert session is not None
        assert session["status"] == "completed"
        assert session["final_verdict"] == "pass"
        assert session["rounds"] == 2
        assert session["finished_at"] is not None

    def test_finish_session_fail(self, store: SessionStore) -> None:
        sid = store.create_session(task="실패 테스트")
        store.finish_session(sid, verdict="fail", rounds=5, error="합의 실패")

        session = store.get_session(sid)
        assert session is not None
        assert session["status"] == "failed"
        assert session["error"] == "합의 실패"

    def test_finish_session_timeout(self, store: SessionStore) -> None:
        sid = store.create_session(task="타임아웃")
        store.finish_session(sid, verdict="timeout", rounds=5)
        session = store.get_session(sid)
        assert session is not None
        assert session["status"] == "failed"

    def test_cancel_session(self, store: SessionStore) -> None:
        sid = store.create_session(task="취소 테스트")
        store.cancel_session(sid)

        session = store.get_session(sid)
        assert session is not None
        assert session["status"] == "cancelled"
        assert session["finished_at"] is not None

    def test_get_nonexistent_session(self, store: SessionStore) -> None:
        assert store.get_session(9999) is None

    def test_list_sessions_ordering(self, store: SessionStore) -> None:
        sid1 = store.create_session(task="첫번째")
        sid2 = store.create_session(task="두번째")
        sid3 = store.create_session(task="세번째")

        sessions = store.list_sessions(limit=10)
        assert len(sessions) == 3
        # 최신순
        assert sessions[0]["id"] == sid3
        assert sessions[1]["id"] == sid2
        assert sessions[2]["id"] == sid1

    def test_list_sessions_limit(self, store: SessionStore) -> None:
        for i in range(5):
            store.create_session(task=f"태스크-{i}")

        sessions = store.list_sessions(limit=3)
        assert len(sessions) == 3


# ── 메트릭 기록 ──


class TestMetrics:
    def test_log_metric(self, store: SessionStore) -> None:
        sid = store.create_session(task="메트릭 테스트")
        store.log_metric(
            sid,
            round_number=1,
            agent_id="gemini-pro",
            role="writer",
            latency_ms=1500.5,
            tokens_used=350,
            success=True,
        )

        metrics = store.get_session_metrics(sid)
        assert len(metrics) == 1
        m = metrics[0]
        assert m["agent_id"] == "gemini-pro"
        assert m["role"] == "writer"
        assert m["latency_ms"] == 1500.5
        assert m["tokens_used"] == 350
        assert m["success"] == 1

    def test_log_metric_failure(self, store: SessionStore) -> None:
        sid = store.create_session(task="실패 메트릭")
        store.log_metric(
            sid,
            round_number=1,
            agent_id="claude-primary",
            role="critic",
            latency_ms=300.0,
            success=False,
            error_type="timeout",
        )

        metrics = store.get_session_metrics(sid)
        assert len(metrics) == 1
        assert metrics[0]["success"] == 0
        assert metrics[0]["error_type"] == "timeout"

    def test_multiple_metrics_per_session(self, store: SessionStore) -> None:
        sid = store.create_session(task="멀티 메트릭")
        for i in range(4):
            store.log_metric(
                sid,
                round_number=(i // 2) + 1,
                agent_id=f"agent-{i}",
                role="writer" if i % 2 == 0 else "critic",
                latency_ms=100.0 * (i + 1),
            )

        metrics = store.get_session_metrics(sid)
        assert len(metrics) == 4
        # round_number 순서
        assert metrics[0]["round_number"] == 1
        assert metrics[-1]["round_number"] == 2

    def test_metrics_empty_session(self, store: SessionStore) -> None:
        sid = store.create_session(task="빈 세션")
        assert store.get_session_metrics(sid) == []


# ── 통계 ──


class TestAgentStats:
    def test_agent_stats(self, store: SessionStore) -> None:
        sid = store.create_session(task="통계 테스트")

        # gemini: 3 calls, 2 success, 1 fail
        store.log_metric(sid, round_number=1, agent_id="gemini", role="writer",
                         latency_ms=1000, tokens_used=100, success=True)
        store.log_metric(sid, round_number=2, agent_id="gemini", role="writer",
                         latency_ms=2000, tokens_used=200, success=True)
        store.log_metric(sid, round_number=3, agent_id="gemini", role="writer",
                         latency_ms=500, success=False, error_type="429")

        # claude: 1 call
        store.log_metric(sid, round_number=1, agent_id="claude", role="critic",
                         latency_ms=3000, tokens_used=500, success=True)

        stats = store.agent_stats()
        assert len(stats) == 2

        gemini_stat = next(s for s in stats if s["agent_id"] == "gemini")
        assert gemini_stat["total_calls"] == 3
        assert gemini_stat["success_rate"] == pytest.approx(66.7, abs=0.1)
        assert gemini_stat["total_tokens"] == 300  # 100 + 200 + 0

        claude_stat = next(s for s in stats if s["agent_id"] == "claude")
        assert claude_stat["total_calls"] == 1
        assert claude_stat["success_rate"] == 100.0
        assert claude_stat["total_tokens"] == 500

    def test_stats_empty_db(self, store: SessionStore) -> None:
        assert store.agent_stats() == []


# ── WAL + 재접속 ──


class TestDurability:
    def test_wal_mode(self, store: SessionStore) -> None:
        """WAL 모드가 활성화되어 있는지."""
        row = store._conn.execute("PRAGMA journal_mode").fetchone()
        assert row[0] == "wal"

    def test_data_survives_reconnect(self, tmp_path: Path) -> None:
        """DB를 닫고 다시 열어도 데이터가 유지되는지."""
        db = str(tmp_path / "persist.db")
        s1 = SessionStore(db)
        sid = s1.create_session(task="영속성 테스트")
        s1.log_metric(sid, round_number=1, agent_id="test", role="writer",
                      latency_ms=100)
        s1.finish_session(sid, verdict="pass", rounds=1)
        s1.close()

        s2 = SessionStore(db)
        session = s2.get_session(sid)
        assert session is not None
        assert session["task"] == "영속성 테스트"
        assert session["final_verdict"] == "pass"

        metrics = s2.get_session_metrics(sid)
        assert len(metrics) == 1
        s2.close()


# ── 사용자 피드백 (H3 전환 자산 — 판정 이력·정확도 추적, v1 test_harness.py에서 salvage) ──


class TestFeedback:
    """사용자 피드백 기록 + 통계."""

    def test_set_feedback_agree(self, store: SessionStore) -> None:
        sid = store.create_session(task="agree test")
        store.finish_session(sid, verdict="pass", rounds=1)
        store.set_feedback(sid, feedback="agree")

        session = store.get_session(sid)
        assert session is not None
        assert session["user_feedback"] == "agree"

    def test_set_feedback_override(self, store: SessionStore) -> None:
        sid = store.create_session(task="override test")
        store.finish_session(sid, verdict="conditional", rounds=1)
        store.set_feedback(sid, feedback="override", override_verdict="pass")

        session = store.get_session(sid)
        assert session is not None
        assert session["user_feedback"] == "override"
        assert session["user_override_verdict"] == "pass"

    def test_feedback_stats(self, store: SessionStore) -> None:
        for i in range(5):
            sid = store.create_session(task=f"task-{i}")
            store.finish_session(sid, verdict="pass", rounds=1)

        # 3 agree, 1 disagree, 1 override
        store.set_feedback(1, feedback="agree")
        store.set_feedback(2, feedback="agree")
        store.set_feedback(3, feedback="agree")
        store.set_feedback(4, feedback="disagree")
        store.set_feedback(5, feedback="override", override_verdict="fail")

        stats = store.feedback_stats()
        assert stats["total"] == 5
        assert stats["agrees"] == 3
        assert stats["disagrees"] == 1
        assert stats["overrides"] == 1
        assert stats["agree_rate"] == 60.0

    def test_feedback_stats_empty(self, store: SessionStore) -> None:
        stats = store.feedback_stats()
        assert stats["total"] == 0
