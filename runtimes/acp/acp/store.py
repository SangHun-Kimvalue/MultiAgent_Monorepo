"""acp/store.py — SQLite WAL 기반 세션 상태 저장소.

ZTR session_store.py의 connect/PRAGMA/executescript 패턴을 차용.
단일 커넥션 직렬화 — 폴링 루프와 웹 요청이 store 내부 메서드를 통해서만 접근.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from acp.models import SessionRecord, SessionState

if TYPE_CHECKING:
    from acp.join import PhaseJoin
    from acp.orch_events import OrchPhaseEvent

logger = logging.getLogger(__name__)

_PHASE_COLUMNS: dict[str, str] = {
    "native_session_id": "TEXT",
    "phase_flag": "TEXT",
    "plan_stale": "INTEGER NOT NULL DEFAULT 0",
    "current_phase": "TEXT",
    "phase_status": "TEXT",
    "owner_session": "TEXT",
    "phases_done": "INTEGER NOT NULL DEFAULT 0",
    "phases_total": "INTEGER NOT NULL DEFAULT 0",
    "phase_source": "TEXT",
}

_NOTIFY_COLUMNS: dict[str, str] = {
    "last_notified_state": "TEXT",
    "last_notified_at": "TEXT",
}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    native_session_id TEXT,
    app          TEXT NOT NULL,
    project_path TEXT,
    model        TEXT,
    last_activity TEXT,
    running_pid  INTEGER,
    running_cmd  TEXT,
    raw_status   TEXT,
    last_event   TEXT,
    state        TEXT NOT NULL DEFAULT 'unknown',
    source_file  TEXT,
    phase_flag   TEXT,
    plan_stale   INTEGER NOT NULL DEFAULT 0,
    current_phase TEXT,
    phase_status TEXT,
    owner_session TEXT,
    phases_done  INTEGER NOT NULL DEFAULT 0,
    phases_total INTEGER NOT NULL DEFAULT 0,
    phase_source TEXT,
    last_notified_state TEXT,
    last_notified_at TEXT,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    payload      TEXT,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orch_events (
    event_key    TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL,
    phase_id     TEXT NOT NULL,
    type         TEXT NOT NULL,
    ts           TEXT NOT NULL,
    payload      TEXT,
    ingested_at  TEXT NOT NULL
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def session_key(app: str, session_id: str) -> str:
    """앱 네임스페이스를 포함한 저장소 내부 세션 키."""
    return f"{app}:{session_id}"


def _orch_event_key(event: "OrchPhaseEvent") -> str:
    """이벤트 내용의 **정규화** 해시 — 멱등 dedup 키.

    payload 키 순서·ts tz 표기에 불변이어야 같은 논리적 이벤트가 1행으로 수렴한다
    (계약 §3 phase 단위 dedup). sort_keys로 중첩 dict까지 정렬, ts는 UTC로 정규화.
    """
    ts = event.ts
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    canonical = json.dumps(
        {
            "schema_version": event.schema_version,
            "project_id": event.project_id,
            "phase_id": event.phase_id,
            "type": event.type.value,
            "ts": ts.astimezone(timezone.utc).isoformat(),
            "payload": event.payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SessionStore:
    """세션 상태와 이벤트를 SQLite WAL로 영속화.

    Usage:
        store = SessionStore(".acp/acp.db")
        store.upsert_session(record, state=SessionState.LIVE)
        rows = store.list_sessions()
        store.append_event(session_id, "state_change", {"from": "live", "to": "idle"})
        store.close()
    """

    def __init__(self, db_path: str = ".acp/acp.db", events_log: str = ".acp/events.jsonl") -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = str(path)
        self._events_log = Path(events_log)
        self._events_log.parent.mkdir(parents=True, exist_ok=True)

        # ZTR 패턴: 단일 커넥션 + WAL + foreign_keys
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA_SQL)
        self._ensure_phase_columns()
        self._ensure_notify_columns()
        self._ensure_session_keys()
        self._conn.commit()
        logger.info("SessionStore 초기화: %s", self._db_path)

    def close(self) -> None:
        self._conn.close()

    def _ensure_phase_columns(self) -> None:
        """기존 DB에도 P2 컬럼을 멱등 추가."""
        cur = self._conn.execute("PRAGMA table_info(sessions)")
        existing = {row["name"] for row in cur.fetchall()}
        for name, sql_type in _PHASE_COLUMNS.items():
            if name not in existing:
                self._conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {sql_type}")

    def _ensure_notify_columns(self) -> None:
        """기존 DB에도 P3 알림 dedupe 컬럼을 멱등 추가."""
        cur = self._conn.execute("PRAGMA table_info(sessions)")
        existing = {row["name"] for row in cur.fetchall()}
        for name, sql_type in _NOTIFY_COLUMNS.items():
            if name not in existing:
                self._conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {sql_type}")

    def _ensure_session_keys(self) -> None:
        """P2 멀티앱 저장 키를 app:session_id로 정규화."""
        self._conn.execute(
            "UPDATE sessions SET native_session_id = session_id WHERE native_session_id IS NULL"
        )
        self._conn.execute(
            """
            UPDATE sessions
            SET session_id = app || ':' || session_id
            WHERE instr(session_id, ':') = 0
            """
        )

    def upsert_session(
        self,
        record: SessionRecord,
        state: SessionState,
        phase: "PhaseJoin | None" = None,
    ) -> None:
        """세션을 INSERT OR REPLACE."""
        last_act = record.last_activity.isoformat() if record.last_activity else None
        key = session_key(record.app, record.session_id)
        phase_values = {
            "phase_flag": phase.flag if phase else None,
            "plan_stale": int(phase.plan_stale) if phase else 0,
            "current_phase": phase.current_phase if phase else None,
            "phase_status": phase.phase_status if phase else None,
            "owner_session": phase.owner_session if phase else None,
            "phases_done": phase.phases_done if phase else 0,
            "phases_total": phase.phases_total if phase else 0,
            "phase_source": phase.phase_source if phase else None,
        }
        self._conn.execute(
            """
            INSERT INTO sessions
                (session_id, native_session_id, app, project_path, model, last_activity,
                 running_pid, running_cmd, raw_status, last_event, state, source_file,
                 phase_flag, plan_stale, current_phase, phase_status, owner_session,
                 phases_done, phases_total, phase_source, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(session_id) DO UPDATE SET
                native_session_id=excluded.native_session_id,
                app=excluded.app,
                project_path=excluded.project_path,
                model=excluded.model,
                last_activity=excluded.last_activity,
                running_pid=excluded.running_pid,
                running_cmd=excluded.running_cmd,
                raw_status=excluded.raw_status,
                last_event=excluded.last_event,
                state=excluded.state,
                source_file=excluded.source_file,
                phase_flag=excluded.phase_flag,
                plan_stale=excluded.plan_stale,
                current_phase=excluded.current_phase,
                phase_status=excluded.phase_status,
                owner_session=excluded.owner_session,
                phases_done=excluded.phases_done,
                phases_total=excluded.phases_total,
                phase_source=excluded.phase_source,
                updated_at=excluded.updated_at
            """,
            (
                key, record.session_id, record.app, record.project_path,
                record.model, last_act, record.running_pid, record.running_cmd,
                record.raw_status, record.last_event, state.value, record.source_file,
                phase_values["phase_flag"], phase_values["plan_stale"],
                phase_values["current_phase"], phase_values["phase_status"],
                phase_values["owner_session"], phase_values["phases_done"],
                phase_values["phases_total"], phase_values["phase_source"], _now_iso(),
            ),
        )
        self._conn.commit()

    def list_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        """최근 갱신 순으로 세션 목록 반환."""
        cur = self._conn.execute(
            "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,)
        )
        return [dict(row) for row in cur.fetchall()]

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        cur = self._conn.execute(
            """
            SELECT * FROM sessions
            WHERE session_id = ? OR native_session_id = ?
            ORDER BY CASE WHEN session_id = ? THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (session_id, session_id, session_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def get_session_for_record(self, record: SessionRecord) -> dict[str, Any] | None:
        """SessionRecord의 앱 네임스페이스 키로 저장된 세션 조회."""
        return self.get_session(session_key(record.app, record.session_id))

    def mark_notified(self, session_id: str, state: SessionState | str, notified_at: datetime) -> None:
        """세션의 마지막 알림 상태/시각을 기록."""
        state_value = state.value if isinstance(state, SessionState) else state
        self._conn.execute(
            """
            UPDATE sessions
            SET last_notified_state = ?, last_notified_at = ?
            WHERE session_id = ?
            """,
            (state_value, notified_at.isoformat(), session_id),
        )
        self._conn.commit()

    def clear_notification_marker(self, session_id: str) -> None:
        """복귀 상태 진입 시 같은 상태 재알림을 허용하도록 dedupe 마커 초기화."""
        self._conn.execute(
            """
            UPDATE sessions
            SET last_notified_state = NULL, last_notified_at = NULL
            WHERE session_id = ?
            """,
            (session_id,),
        )
        self._conn.commit()

    def append_event(
        self, session_id: str, event_type: str, payload: dict[str, Any] | None = None
    ) -> None:
        """이벤트를 DB + JSONL 감사로그에 append."""
        now = _now_iso()
        payload_json = json.dumps(payload or {}, ensure_ascii=False)
        self._conn.execute(
            "INSERT INTO events (session_id, event_type, payload, created_at) VALUES (?,?,?,?)",
            (session_id, event_type, payload_json, now),
        )
        self._conn.commit()

        # append-only JSONL 감사로그
        line = json.dumps(
            {"session_id": session_id, "event_type": event_type, "payload": payload or {}, "created_at": now},
            ensure_ascii=False,
        )
        with self._events_log.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def list_events(self, limit: int = 50, event_type: str | None = None) -> list[dict[str, Any]]:
        """최근 이벤트 목록 반환. payload는 dict로 역직렬화."""
        if event_type:
            cur = self._conn.execute(
                """
                SELECT * FROM events
                WHERE event_type = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (event_type, limit),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM events ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            )
        rows: list[dict[str, Any]] = []
        for row in cur.fetchall():
            item = dict(row)
            try:
                item["payload"] = json.loads(item.get("payload") or "{}")
            except json.JSONDecodeError:
                item["payload"] = {}
            rows.append(item)
        return rows

    def record_orch_event(self, event: "OrchPhaseEvent") -> bool:
        """orchestrator phase 이벤트를 멱등 저장. 신규 삽입이면 True.

        event_key = 이벤트 내용 해시 → 같은 이벤트 재수집은 무시(dedup).
        R5: 토큰/payload 의미를 해석하지 않고 사실만 기록한다.
        """
        event_key = _orch_event_key(event)
        payload_json = json.dumps(event.payload, ensure_ascii=False)
        cur = self._conn.execute(
            """
            INSERT INTO orch_events (event_key, project_id, phase_id, type, ts, payload, ingested_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(event_key) DO NOTHING
            """,
            (
                event_key, event.project_id, event.phase_id, event.type.value,
                event.ts.isoformat(), payload_json, _now_iso(),
            ),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def list_orch_events(
        self, limit: int = 50, phase_id: str | None = None
    ) -> list[dict[str, Any]]:
        """최근 orchestrator 이벤트 목록 반환(ts 내림차순). payload는 dict로 역직렬화."""
        if phase_id:
            cur = self._conn.execute(
                "SELECT * FROM orch_events WHERE phase_id = ? "
                "ORDER BY ts DESC, ingested_at DESC LIMIT ?",
                (phase_id, limit),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM orch_events ORDER BY ts DESC, ingested_at DESC LIMIT ?",
                (limit,),
            )
        rows: list[dict[str, Any]] = []
        for row in cur.fetchall():
            item = dict(row)
            try:
                item["payload"] = json.loads(item.get("payload") or "{}")
            except json.JSONDecodeError:
                item["payload"] = {}
            rows.append(item)
        return rows
