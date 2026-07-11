"""SessionStore - SQLite WAL 기반 세션/메트릭 영속 저장소.

DESIGN.md 7장 스키마 그대로 구현.
동기 sqlite3 사용 (IO 부하 미미, aiosqlite 의존성 추가 불필요).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS roundtable_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task        TEXT NOT NULL,
    target_file TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'running'
                CHECK (status IN ('running', 'completed', 'failed', 'cancelled')),
    rounds      INTEGER DEFAULT 0,
    final_verdict TEXT,
    started_at  TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    config_snapshot TEXT,
    error       TEXT DEFAULT '',
    user_feedback TEXT,
    user_override_verdict TEXT
);

CREATE TABLE IF NOT EXISTS agent_metrics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES roundtable_sessions(id),
    round_number INTEGER NOT NULL,
    agent_id    TEXT NOT NULL,
    role        TEXT NOT NULL,
    latency_ms  REAL,
    tokens_used INTEGER,
    success     INTEGER NOT NULL DEFAULT 1,
    error_type  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS round_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    INTEGER NOT NULL REFERENCES roundtable_sessions(id),
    round_number  INTEGER NOT NULL,
    writer_id     TEXT NOT NULL DEFAULT '',
    writer_content TEXT DEFAULT '',
    nitpicker_status TEXT DEFAULT '',
    nitpicker_content TEXT DEFAULT '',
    critic_id     TEXT NOT NULL DEFAULT '',
    critic_content TEXT DEFAULT '',
    verdict       TEXT DEFAULT '',
    reasoning     TEXT DEFAULT '',
    parse_method  TEXT DEFAULT '',
    findings_json TEXT DEFAULT '[]',
    elapsed_ms    REAL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS issues (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    description TEXT DEFAULT '',
    target_file TEXT DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'in_progress', 'resolved', 'wont_fix')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id    TEXT REFERENCES issues(id),
    session_id  INTEGER REFERENCES roundtable_sessions(id),
    decision    TEXT NOT NULL,
    reasoning   TEXT DEFAULT '',
    decided_by  TEXT DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sessions_status ON roundtable_sessions(status);
CREATE INDEX IF NOT EXISTS idx_metrics_session ON agent_metrics(session_id);
CREATE INDEX IF NOT EXISTS idx_metrics_agent ON agent_metrics(agent_id);
CREATE INDEX IF NOT EXISTS idx_round_logs_session ON round_logs(session_id);
CREATE INDEX IF NOT EXISTS idx_issues_status ON issues(status);
CREATE INDEX IF NOT EXISTS idx_decisions_issue ON decisions(issue_id);
"""


class SessionStore:
    """세션 + 메트릭 저장소.

    Usage:
        store = SessionStore(".ztr/sessions.db")
        sid = store.create_session(task="JWT 갱신", target_file="src/auth.py")
        store.log_metric(sid, round_number=1, agent_id="gemini-pro", ...)
        store.finish_session(sid, verdict="pass", rounds=2)

        # 조회
        sessions = store.list_sessions(limit=10)
        stats = store.agent_stats()
    """

    def __init__(self, db_path: str = ".ztr/sessions.db") -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = str(path)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        # WAL 모드 + 외래 키 활성화
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()
        logger.info("SessionStore 초기화: %s", self._db_path)

    def close(self) -> None:
        self._conn.close()

    # ── 세션 CRUD ──

    def create_session(
        self,
        task: str,
        target_file: str = "",
        config_snapshot: dict[str, Any] | None = None,
    ) -> int:
        """새 세션을 생성하고 session_id를 반환한다."""
        snapshot_json = json.dumps(
            config_snapshot, ensure_ascii=False
        ) if config_snapshot else None

        cur = self._conn.execute(
            "INSERT INTO roundtable_sessions (task, target_file, config_snapshot) "
            "VALUES (?, ?, ?)",
            (task, target_file, snapshot_json),
        )
        self._conn.commit()
        session_id = cur.lastrowid
        assert session_id is not None
        logger.debug("세션 생성: id=%d, task=%r", session_id, task[:50])
        return session_id

    def finish_session(
        self,
        session_id: int,
        *,
        verdict: str,
        rounds: int,
        error: str = "",
    ) -> None:
        """세션을 완료 상태로 갱신한다."""
        status = "failed" if verdict in ("fail", "timeout", "error") else "completed"
        self._conn.execute(
            "UPDATE roundtable_sessions "
            "SET status = ?, final_verdict = ?, rounds = ?, "
            "    finished_at = datetime('now'), error = ? "
            "WHERE id = ?",
            (status, verdict, rounds, error, session_id),
        )
        self._conn.commit()
        logger.debug("세션 완료: id=%d, verdict=%s", session_id, verdict)

    def cancel_session(self, session_id: int) -> None:
        """세션을 취소한다."""
        self._conn.execute(
            "UPDATE roundtable_sessions SET status = 'cancelled', "
            "finished_at = datetime('now') WHERE id = ?",
            (session_id,),
        )
        self._conn.commit()

    # ── 라운드 로그 기록 ──

    def log_round(
        self,
        session_id: int,
        *,
        round_number: int,
        writer_id: str = "",
        writer_content: str = "",
        nitpicker_status: str = "",
        nitpicker_content: str = "",
        critic_id: str = "",
        critic_content: str = "",
        verdict: str = "",
        reasoning: str = "",
        parse_method: str = "",
        findings: list[dict[str, str]] | None = None,
        elapsed_ms: float = 0.0,
    ) -> None:
        """라운드 전체 로그를 기록한다 (Writer/Critic 응답 전문 포함)."""
        findings_json = json.dumps(
            findings or [], ensure_ascii=False,
        )
        self._conn.execute(
            "INSERT INTO round_logs "
            "(session_id, round_number, writer_id, writer_content, "
            " nitpicker_status, nitpicker_content, critic_id, critic_content, "
            " verdict, reasoning, parse_method, findings_json, elapsed_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id, round_number, writer_id, writer_content,
                nitpicker_status, nitpicker_content, critic_id, critic_content,
                verdict, reasoning, parse_method, findings_json, elapsed_ms,
            ),
        )
        self._conn.commit()

    # ── 메트릭 기록 ──

    def log_metric(
        self,
        session_id: int,
        *,
        round_number: int,
        agent_id: str,
        role: str,
        latency_ms: float = 0.0,
        tokens_used: int | None = None,
        success: bool = True,
        error_type: str = "",
    ) -> None:
        """에이전트 호출 메트릭을 기록한다."""
        self._conn.execute(
            "INSERT INTO agent_metrics "
            "(session_id, round_number, agent_id, role, latency_ms, "
            " tokens_used, success, error_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id, round_number, agent_id, role,
                latency_ms, tokens_used,
                1 if success else 0,
                error_type or None,
            ),
        )
        self._conn.commit()

    # ── 조회 ──

    def get_session(self, session_id: int) -> dict[str, Any] | None:
        """단일 세션 조회."""
        row = self._conn.execute(
            "SELECT * FROM roundtable_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        """최근 세션 목록."""
        rows = self._conn.execute(
            "SELECT * FROM roundtable_sessions "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_round_logs(self, session_id: int) -> list[dict[str, Any]]:
        """세션의 라운드 로그 목록 (Writer/Critic 응답 전문 포함)."""
        rows = self._conn.execute(
            "SELECT * FROM round_logs WHERE session_id = ? "
            "ORDER BY round_number",
            (session_id,),
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            # findings_json -> findings 파싱
            try:
                d["findings"] = json.loads(d.get("findings_json", "[]"))
            except (json.JSONDecodeError, TypeError):
                d["findings"] = []
            results.append(d)
        return results

    def get_session_metrics(self, session_id: int) -> list[dict[str, Any]]:
        """세션의 메트릭 목록."""
        rows = self._conn.execute(
            "SELECT * FROM agent_metrics WHERE session_id = ? "
            "ORDER BY round_number, id",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def set_feedback(
        self,
        session_id: int,
        *,
        feedback: str = "agree",
        override_verdict: str = "",
    ) -> None:
        """사용자 피드백을 기록한다.

        Args:
            feedback: "agree" | "disagree" | "override"
            override_verdict: feedback가 "override"일 때 사용자가 지정한 verdict
        """
        self._conn.execute(
            "UPDATE roundtable_sessions "
            "SET user_feedback = ?, user_override_verdict = ? "
            "WHERE id = ?",
            (feedback, override_verdict or None, session_id),
        )
        self._conn.commit()
        logger.debug("피드백 기록: session=%d, feedback=%s", session_id, feedback)

    def feedback_stats(self) -> dict[str, Any]:
        """Critic 판정 vs 사용자 피드백 일치율 통계."""
        rows = self._conn.execute(
            "SELECT final_verdict, user_feedback, user_override_verdict "
            "FROM roundtable_sessions "
            "WHERE user_feedback IS NOT NULL",
        ).fetchall()

        total = len(rows)
        if total == 0:
            return {"total": 0, "agree_rate": 0.0, "overrides": 0}

        agrees = sum(1 for r in rows if r["user_feedback"] == "agree")
        overrides = sum(1 for r in rows if r["user_feedback"] == "override")
        disagrees = sum(1 for r in rows if r["user_feedback"] == "disagree")

        return {
            "total": total,
            "agrees": agrees,
            "disagrees": disagrees,
            "overrides": overrides,
            "agree_rate": round(agrees / total * 100, 1),
        }

    def agent_stats(self) -> list[dict[str, Any]]:
        """에이전트별 통계 요약.

        Returns:
            [{agent_id, total_calls, success_rate, avg_latency_ms, total_tokens}]
        """
        rows = self._conn.execute(
            "SELECT "
            "  agent_id, "
            "  COUNT(*) as total_calls, "
            "  ROUND(AVG(success) * 100, 1) as success_rate, "
            "  ROUND(AVG(latency_ms), 1) as avg_latency_ms, "
            "  SUM(COALESCE(tokens_used, 0)) as total_tokens "
            "FROM agent_metrics "
            "GROUP BY agent_id "
            "ORDER BY total_calls DESC",
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Issues ──

    def create_issue(
        self,
        issue_id: str,
        title: str,
        *,
        description: str = "",
        target_file: str = "",
    ) -> str:
        """이슈를 등록한다. 동일 ID 존재 시 무시."""
        self._conn.execute(
            "INSERT OR IGNORE INTO issues (id, title, description, target_file) "
            "VALUES (?, ?, ?, ?)",
            (issue_id, title, description, target_file),
        )
        self._conn.commit()
        return issue_id

    def resolve_issue(self, issue_id: str) -> None:
        """이슈를 해결 상태로 변경."""
        self._conn.execute(
            "UPDATE issues SET status = 'resolved', resolved_at = datetime('now') "
            "WHERE id = ?",
            (issue_id,),
        )
        self._conn.commit()

    def get_issue(self, issue_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM issues WHERE id = ?", (issue_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_issues(self, status: str = "") -> list[dict[str, Any]]:
        """이슈 목록. status 필터 가능."""
        if status:
            rows = self._conn.execute(
                "SELECT * FROM issues WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM issues ORDER BY created_at DESC",
            ).fetchall()
        return [dict(r) for r in rows]

    def get_open_issues_for_file(self, target_file: str) -> list[dict[str, Any]]:
        """특정 파일에 대한 열린 이슈 목록."""
        rows = self._conn.execute(
            "SELECT * FROM issues WHERE target_file = ? AND status IN ('open', 'in_progress') "
            "ORDER BY created_at DESC",
            (target_file,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Decisions ──

    def add_decision(
        self,
        decision: str,
        *,
        issue_id: str = "",
        session_id: int | None = None,
        reasoning: str = "",
        decided_by: str = "",
    ) -> int:
        """의사결정을 기록한다."""
        cur = self._conn.execute(
            "INSERT INTO decisions (issue_id, session_id, decision, reasoning, decided_by) "
            "VALUES (?, ?, ?, ?, ?)",
            (issue_id or None, session_id, decision, reasoning, decided_by),
        )
        self._conn.commit()
        return cur.lastrowid or 0

    def get_decisions_for_file(self, target_file: str) -> list[dict[str, Any]]:
        """특정 파일 관련 의사결정 목록 (이슈를 통해 연결)."""
        rows = self._conn.execute(
            "SELECT d.*, i.title as issue_title, i.target_file "
            "FROM decisions d "
            "LEFT JOIN issues i ON d.issue_id = i.id "
            "WHERE i.target_file = ? "
            "ORDER BY d.created_at DESC",
            (target_file,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_recent_decisions(self, limit: int = 10) -> list[dict[str, Any]]:
        """최근 의사결정 목록."""
        rows = self._conn.execute(
            "SELECT d.*, i.title as issue_title, i.target_file "
            "FROM decisions d "
            "LEFT JOIN issues i ON d.issue_id = i.id "
            "ORDER BY d.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
