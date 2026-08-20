"""tests/test_store.py — SessionStore upsert→list 왕복 테스트."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


from acp.join import PhaseJoin
from acp.models import SessionRecord, SessionState
from acp.store import SessionStore, session_key


def _record(sid: str = "sess-001", **kwargs) -> SessionRecord:
    base = dict(app="fake", session_id=sid, source_file="test")
    base.update(kwargs)
    return SessionRecord(**base)


def test_upsert_and_list(tmp_store):
    """upsert → list 왕복 확인."""
    r = _record("s1", project_path="/proj/a", model="gpt-x")
    tmp_store.upsert_session(r, SessionState.LIVE)
    rows = tmp_store.list_sessions()
    assert len(rows) == 1
    assert rows[0]["session_id"] == "fake:s1"
    assert rows[0]["native_session_id"] == "s1"
    assert rows[0]["state"] == "live"
    assert rows[0]["model"] == "gpt-x"


def test_upsert_updates_existing(tmp_store):
    """같은 session_id upsert → 갱신."""
    r = _record("s1")
    tmp_store.upsert_session(r, SessionState.LIVE)
    r2 = _record("s1", model="updated-model")
    tmp_store.upsert_session(r2, SessionState.IDLE)
    rows = tmp_store.list_sessions()
    assert len(rows) == 1
    assert rows[0]["session_id"] == "fake:s1"
    assert rows[0]["state"] == "idle"
    assert rows[0]["model"] == "updated-model"


def test_list_multiple_sessions(tmp_store):
    """여러 세션 저장 후 목록 반환."""
    for i in range(5):
        tmp_store.upsert_session(_record(f"s{i}"), SessionState.UNKNOWN)
    assert len(tmp_store.list_sessions()) == 5


def test_append_event_and_jsonl(tmp_store, tmp_path):
    """이벤트 append → DB + JSONL 파일 모두 기록."""
    import json
    r = _record("s1")
    tmp_store.upsert_session(r, SessionState.LIVE)
    tmp_store.append_event("s1", "state_change", {"from": None, "to": "live"})

    # JSONL 파일 확인
    from pathlib import Path
    log = Path(tmp_store._events_log)
    assert log.exists()
    line = json.loads(log.read_text(encoding="utf-8").strip())
    assert line["session_id"] == "s1"
    assert line["event_type"] == "state_change"


def test_list_events_filters_and_decodes_payload(tmp_store):
    tmp_store.append_event("s1", "state_change", {"to": "live"})
    tmp_store.append_event("s1", "notification_sent", {"to": "holding", "title": "세션 홀딩 감지"})

    rows = tmp_store.list_events(event_type="notification_sent")

    assert len(rows) == 1
    assert rows[0]["event_type"] == "notification_sent"
    assert rows[0]["payload"]["to"] == "holding"


def test_get_session(tmp_store):
    r = _record("s-get")
    tmp_store.upsert_session(r, SessionState.LIVE)
    row = tmp_store.get_session("s-get")
    assert row is not None
    assert row["session_id"] == "fake:s-get"
    assert tmp_store.get_session("fake:s-get")["native_session_id"] == "s-get"
    assert tmp_store.get_session("nonexistent") is None


def test_app_namespaces_prevent_session_id_collision(tmp_store):
    tmp_store.upsert_session(_record("same", app="codex"), SessionState.LIVE)
    tmp_store.upsert_session(_record("same", app="cursor"), SessionState.IDLE)

    rows = {row["session_id"]: row for row in tmp_store.list_sessions()}

    assert set(rows) == {"codex:same", "cursor:same"}
    assert rows["codex:same"]["state"] == "live"
    assert rows["cursor:same"]["state"] == "idle"
    assert tmp_store.get_session(session_key("codex", "same"))["app"] == "codex"


def test_notification_marker_roundtrip_and_clear(tmp_store):
    r = _record("notify")
    now = datetime.now(timezone.utc)
    tmp_store.upsert_session(r, SessionState.HOLDING)

    tmp_store.mark_notified("fake:notify", SessionState.HOLDING, now)
    row = tmp_store.get_session("fake:notify")
    assert row["last_notified_state"] == "holding"
    assert row["last_notified_at"] == now.isoformat()

    tmp_store.clear_notification_marker("fake:notify")
    cleared = tmp_store.get_session("fake:notify")
    assert cleared["last_notified_state"] is None
    assert cleared["last_notified_at"] is None


def test_upsert_session_roundtrips_phase_join(tmp_store):
    r = _record("s-phase", project_path="/repo")
    phase = PhaseJoin(
        flag="ok",
        plan_stale=True,
        current_phase="P2",
        phase_status="in_progress",
        owner_session="Implementer",
        phases_done=2,
        phases_total=4,
        phase_source="/repo/PHASE.md",
    )

    tmp_store.upsert_session(r, SessionState.IDLE, phase=phase)
    row = tmp_store.get_session("s-phase")

    assert row is not None
    assert row["session_id"] == "fake:s-phase"
    assert row["phase_flag"] == "ok"
    assert row["plan_stale"] == 1
    assert row["current_phase"] == "P2"
    assert row["phase_status"] == "in_progress"
    assert row["owner_session"] == "Implementer"
    assert row["phases_done"] == 2
    assert row["phases_total"] == 4
    assert row["phase_source"] == "/repo/PHASE.md"


def test_existing_db_gets_phase_columns_idempotently(tmp_path):
    db = tmp_path / "legacy.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            app TEXT NOT NULL,
            project_path TEXT,
            model TEXT,
            last_activity TEXT,
            running_pid INTEGER,
            running_cmd TEXT,
            raw_status TEXT,
            last_event TEXT,
            state TEXT NOT NULL DEFAULT 'unknown',
            source_file TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    con.commit()
    con.close()

    store = SessionStore(str(db), str(tmp_path / "events.jsonl"))
    store.close()
    reopened = SessionStore(str(db), str(tmp_path / "events.jsonl"))
    try:
        reopened.upsert_session(_record("legacy"), SessionState.LIVE)
        row = reopened.get_session("legacy")
    finally:
        reopened.close()

    assert row is not None
    assert "phase_flag" in row
    assert row["plan_stale"] == 0
    assert "last_notified_state" in row
    assert "last_notified_at" in row
