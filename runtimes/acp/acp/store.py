"""acp/store.py — SQLite WAL 기반 세션 상태 저장소.

ZTR session_store.py의 connect/PRAGMA/executescript 패턴을 차용.
단일 커넥션 직렬화 — 폴링 루프와 웹 요청이 store 내부 메서드를 통해서만 접근.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from acp.collectors.base import (
    CAPABILITY_UNKNOWN,
    COUNT_AXES,
    CountScopeError,
    PROCESS_SIGNAL_CAPABILITIES,
    PROCESS_SIGNAL_UNKNOWN,
    PROCESS_SIGNAL_VALUES,
    SCOPE_DECLARED,
    SOURCE_DECLARED,
    parse_count_scopes,
    validate_count_scopes,
)
from acp.models import SessionRecord, SessionState, state_vocabulary

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

# 기존 DB에도 멱등 추가해야 하는 사이클 컬럼(T14 S3). `CREATE TABLE IF NOT EXISTS`는
# 이미 있는 테이블을 바꾸지 않으므로, 이 목록 없이는 기존 설치에서 INSERT가 깨진다.
_CYCLE_COLUMNS: dict[str, str] = {
    "signal_quality": "TEXT",
    "matched": "INTEGER NOT NULL DEFAULT 0",
    "unmatched": "INTEGER NOT NULL DEFAULT 0",
    "ambiguous": "INTEGER NOT NULL DEFAULT 0",
    "malformed_uuid": "INTEGER NOT NULL DEFAULT 0",
    "process_observed_at": "TEXT",
    # 축별 지식 범위(JSON) · 사이클 발화 주체(T14 S4b). NULL = 마이그레이션 이전 행이며
    # **전 축 unknown으로 읽는다** — 과거 숫자를 새 계약의 declared로 소급 인증하지 않는다.
    "count_scopes": "TEXT",
    "source": "TEXT",
    # 실행 신호를 줄 수 있는가(T14 S4c-1 D1). NULL = 선언 이전 행이며 **unknown으로 읽는다**
    # — 과거 사이클을 새 계약의 `supported`로 소급 인증하지 않는다.
    "process_signal_capability": "TEXT",
}

# 기존 DB에도 멱등 추가해야 하는 세션 컬럼(T14 S4a). 위 사이클 컬럼과 같은 이유.
_SESSION_COLUMNS: dict[str, str] = {
    "archive_observed_at": "TEXT",
}

# archived 범위 어휘. route 검증·store 쿼리·직렬화가 **같은 상수**를 쓴다(T14 S4a D3).
ARCHIVED_SCOPES: tuple[str, ...] = ("include", "exclude", "only")
ARCHIVED_SCOPE_DEFAULT = "include"
# `by_state`가 어느 집합의 분포인지 봉투가 말한다(T14 S4a D7).
STATE_DISTRIBUTION_SCOPE = "archive_not_observed"


class UnknownStateFilter(ValueError):
    """상태 필터가 **계약 어휘에도 저장소에도** 없는 값을 요구했다(T14 S4c-2 D5).

    저장소는 HTTP를 모른다 — 거절 사실과 그 근거만 도메인 예외로 올리고, 상태 코드로
    옮기는 것은 웹 계층의 몫이다(D7). 소비자가 산문을 파싱하지 않도록 **구조화된 값**을
    들고 다닌다: 무엇이 거절됐고(`invalid_values`) 무엇이 수용되는가(`accepted`).
    """

    def __init__(self, invalid_values: Sequence[str], accepted: Sequence[str]) -> None:
        self.invalid_values = list(invalid_values)
        self.accepted = list(accepted)
        super().__init__(f"알 수 없는 state: {self.invalid_values}")

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
    archive_observed_at TEXT,
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
CREATE TABLE IF NOT EXISTS approval_challenges (
    challenge_id TEXT PRIMARY KEY,
    nonce_hash TEXT NOT NULL UNIQUE,
    phase_id TEXT NOT NULL,
    round INTEGER NOT NULL,
    findings_digest TEXT NOT NULL,
    session_id TEXT NOT NULL,
    issuer_id TEXT NOT NULL,
    key_id TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'consumed')),
    consumed_at TEXT,
    external_approval_id TEXT
);
CREATE TABLE IF NOT EXISTS approval_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL CHECK (event_type IN ('ISSUE', 'CONSUME')),
    challenge_id TEXT NOT NULL,
    issuer_id TEXT NOT NULL,
    external_approval_id TEXT,
    phase_id TEXT NOT NULL,
    round INTEGER NOT NULL,
    findings_digest TEXT NOT NULL,
    session_id TEXT NOT NULL,
    key_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    claim_level TEXT NOT NULL,
    result TEXT,
    FOREIGN KEY(challenge_id) REFERENCES approval_challenges(challenge_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_approval_consume_external
ON approval_audit(issuer_id, external_approval_id)
WHERE event_type = 'CONSUME' AND external_approval_id IS NOT NULL;

-- 수집 사이클 사실(T14 S3 D4). append-only.
-- `SUCCESS_COMPLETE, collected=0`(정말 없음)과 `FAILED`(관측 실패)를 구분해 남긴다.
-- 이 구분이 없으면 후속 missing/prune 판정이 관측 실패를 데이터 부재로 오인한다.
CREATE TABLE IF NOT EXISTS collector_cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    status TEXT NOT NULL,
    collected INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    excluded_archived INTEGER NOT NULL DEFAULT 0,
    process_signal TEXT,
    -- 앱별 신호 품질. **수집기가 선언**한 값을 그대로 저장한다(web 계층이 중복 정의하지 않음).
    signal_quality TEXT,
    matched INTEGER NOT NULL DEFAULT 0,
    malformed_uuid INTEGER NOT NULL DEFAULT 0,
    unmatched INTEGER NOT NULL DEFAULT 0,
    ambiguous INTEGER NOT NULL DEFAULT 0,
    -- 참고용 관측 사실. **liveness 투입 금지**이며 세션 API에 노출하지 않는다.
    process_observed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_collector_cycles_app_time
ON collector_cycles(app, observed_at DESC);
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
        self._ensure_cycle_columns()
        self._ensure_session_columns()
        self._ensure_session_keys()
        self._conn.commit()
        # 세션 쓰기 **전용 연결**(T14 S6 D2). 스키마·마이그레이션을 주 연결에서 커밋한 뒤
        # 연다 — 순서를 뒤집으면 전용 연결이 아직 없는 컬럼을 보게 된다.
        self._session_conn = self._open_session_connection()
        logger.info("SessionStore 초기화: %s", self._db_path)

    def _open_session_connection(self) -> sqlite3.Connection:
        """`sessions` 테이블 쓰기·읽기 전용 연결.

        **왜 연결을 나누는가**(T14 S6 D2): `synchronous`는 테이블이 아니라 **연결 단위**
        설정이다. 주 연결에는 승인 감사(`approval_audit`)처럼 **소스에서 재유도되지 않는**
        기록이 함께 흐르므로, 그 연결의 내구성을 낮추면 안 된다. 세션 행은 매 틱 소스
        파일에서 다시 만들어지므로 이 연결에서만 `NORMAL`을 감당할 수 있다.

        실측(360행 upsert): `FULL` 682ms → `NORMAL` 17ms. 그 차이가 폴링 틱을 15초마다
        수 초씩 잡아먹고 이벤트 루프를 막고 있었다.

        폴러의 스레드 단계는 **이 연결만** 만진다(D3 소유권 규칙). 주 연결은 루프 단계
        전용이라 같은 연결을 두 스레드가 동시에 쓰는 일이 없다.
        """
        conn = sqlite3.connect(self._db_path, check_same_thread=False, timeout=5.0)
        conn.row_factory = sqlite3.Row
        # 경합은 "처리"되는 게 아니라 재시도될 뿐이다(검토 P2). 한도를 명시하고,
        # 그래도 실패하면 호출자가 그 틱의 사실로 표면화한다.
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def close(self) -> None:
        self._session_conn.close()
        self._conn.close()

    def _approval_connection(self) -> sqlite3.Connection:
        """consume 전용 연결. 호출자가 transaction과 close를 소유한다."""
        conn = sqlite3.connect(self._db_path, isolation_level=None, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def insert_approval_challenge(self, values: dict[str, Any]) -> None:
        """challenge와 redacted ISSUE 사실을 먼저 원자 기록한다."""
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO approval_challenges
                    (challenge_id, nonce_hash, phase_id, round, findings_digest, session_id,
                     issuer_id, key_id, issued_at, expires_at, state)
                VALUES (?,?,?,?,?,?,?,?,?,?,'pending')
                """,
                tuple(values[key] for key in (
                    "challenge_id", "nonce_hash", "phase_id", "round", "findings_digest",
                    "session_id", "issuer_id", "key_id", "issued_at", "expires_at",
                )),
            )
            self._conn.execute(
                """
                INSERT INTO approval_audit
                    (event_type, challenge_id, issuer_id, external_approval_id, phase_id,
                     round, findings_digest, session_id, key_id, created_at, claim_level, result)
                VALUES ('ISSUE',?,?,NULL,?,?,?,?,?,?,'DETERMINISTIC_VERIFIER',NULL)
                """,
                (
                    values["challenge_id"], values["issuer_id"], values["phase_id"],
                    values["round"], values["findings_digest"], values["session_id"],
                    values["key_id"], values["issued_at"],
                ),
            )

    def consume_approval_challenge(self, values: dict[str, Any]) -> bool:
        """pending 조건부 갱신과 CONSUME 감사를 단일 immediate transaction으로 처리."""
        conn: sqlite3.Connection | None = None
        began = False
        try:
            conn = self._approval_connection()
            conn.execute("BEGIN IMMEDIATE")
            began = True
            row = conn.execute(
                "SELECT * FROM approval_challenges WHERE challenge_id = ?",
                (values["challenge_id"],),
            ).fetchone()
            match_keys = (
                "nonce_hash", "phase_id", "round", "findings_digest", "session_id",
                "issuer_id", "key_id", "issued_at", "expires_at",
            )
            if row is None or row["state"] != "pending" or any(
                row[key] != values[key] for key in match_keys
            ):
                conn.execute("ROLLBACK")
                began = False
                return False
            cur = conn.execute(
                """
                UPDATE approval_challenges
                SET state='consumed', consumed_at=?, external_approval_id=?
                WHERE challenge_id=? AND state='pending'
                """,
                (values["consumed_at"], values["external_approval_id"], values["challenge_id"]),
            )
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                began = False
                return False
            conn.execute(
                """
                INSERT INTO approval_audit
                    (event_type, challenge_id, issuer_id, external_approval_id, phase_id,
                     round, findings_digest, session_id, key_id, created_at, claim_level, result)
                VALUES ('CONSUME',?,?,?,?,?,?,?,?,?,'DETERMINISTIC_VERIFIER','APPROVAL_VALID')
                """,
                (
                    values["challenge_id"], values["issuer_id"],
                    values["external_approval_id"], values["phase_id"], values["round"],
                    values["findings_digest"], values["session_id"], values["key_id"],
                    values["consumed_at"],
                ),
            )
            conn.execute("COMMIT")
            began = False
            return True
        except sqlite3.Error:
            if conn is not None and began:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            return False
        finally:
            if conn is not None:
                conn.close()

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

    def _ensure_session_columns(self) -> None:
        """기존 DB에도 보관 컬럼을 멱등 추가(T14 S4a).

        `CREATE TABLE IF NOT EXISTS`는 이미 있는 테이블을 바꾸지 않는다. 이 훅이 없으면
        기존 설치에서 INSERT가 깨지고, 그러면 "제외했다"는 고지만 남고 실제 분류는
        일어나지 않는다 — 이 슬라이스가 고치려는 바로 그 거짓이 재생산된다.
        """
        cur = self._conn.execute("PRAGMA table_info(sessions)")
        existing = {row["name"] for row in cur.fetchall()}
        for name, sql_type in _SESSION_COLUMNS.items():
            if name not in existing:
                self._conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {sql_type}")

    def _ensure_cycle_columns(self) -> None:
        """기존 DB에도 수집 사이클 컬럼을 멱등 추가(T14 S3)."""
        cur = self._conn.execute("PRAGMA table_info(collector_cycles)")
        existing = {row["name"] for row in cur.fetchall()}
        for name, sql_type in _CYCLE_COLUMNS.items():
            if name not in existing:
                self._conn.execute(
                    f"ALTER TABLE collector_cycles ADD COLUMN {name} {sql_type}"
                )

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
        state: SessionState | str,
        phase: "PhaseJoin | None" = None,
    ) -> None:
        """세션을 INSERT OR REPLACE.

        `state`는 보통 enum이지만 **문자열도 받는다**(T14 S4c-2). 보관 행의 마지막으로
        알던 상태가 계약 어휘 밖일 수 있고(다른 버전이 쓴 행·enum rename 잔존),
        그것을 enum으로 강제하면 `unknown`으로 덮어써 **실제로 알던 사실이 사라진다**.
        보존은 원문 그대로 쓸 때만 보존이다.
        """
        # 세션 쓰기는 **전용 연결**을 쓴다(T14 S6 D2 — 이 연결만 `synchronous=NORMAL`).
        conn = self._session_conn
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
        conn.execute(
            """
            INSERT INTO sessions
                (session_id, native_session_id, app, project_path, model, last_activity,
                 running_pid, running_cmd, raw_status, last_event, state, source_file,
                 phase_flag, plan_stale, current_phase, phase_status, owner_session,
                 phases_done, phases_total, phase_source, archive_observed_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                -- 최초 관측 시각을 보존한다: 이미 보관으로 관측된 행을 매 사이클
                -- 덮어쓰면 "언제부터 보관인가"라는 사실이 사라진다. 보관 해제(NULL)는
                -- 그대로 반영해 **되돌아올 수 있게** 한다(T14 S4a D2).
                archive_observed_at=CASE
                    WHEN excluded.archive_observed_at IS NULL THEN NULL
                    ELSE COALESCE(sessions.archive_observed_at, excluded.archive_observed_at)
                END,
                updated_at=excluded.updated_at
            """,
            (
                key, record.session_id, record.app, record.project_path,
                record.model, last_act, record.running_pid, record.running_cmd,
                record.raw_status, record.last_event,
                state.value if isinstance(state, SessionState) else str(state),
                record.source_file,
                phase_values["phase_flag"], phase_values["plan_stale"],
                phase_values["current_phase"], phase_values["phase_status"],
                phase_values["owner_session"], phase_values["phases_done"],
                phase_values["phases_total"], phase_values["phase_source"],
                _now_iso() if record.archived else None, _now_iso(),
            ),
        )
        conn.commit()

    @staticmethod
    def _session_filter(
        states: Sequence[str] | None,
        apps: Sequence[str] | None,
        archived: str = ARCHIVED_SCOPE_DEFAULT,
    ) -> tuple[str, list[str]]:
        """상태/앱/보관 범위를 WHERE 절과 파라미터로 만든다(값은 항상 바인딩).

        `archived`는 어휘 상수(`ARCHIVED_SCOPES`)로만 들어온다. 알 수 없는 값은
        조용히 무시하지 않고 예외로 세운다 — 무시하면 요청자가 건 범위와 응답이
        달라지고, 그게 이 트랙이 고쳐 온 "무엇을 세는지 모르는 수"다.
        """
        if archived not in ARCHIVED_SCOPES:
            raise ValueError(f"알 수 없는 archived 범위: {archived!r}")
        clauses: list[str] = []
        params: list[str] = []
        if states:
            clauses.append(f"state IN ({','.join('?' for _ in states)})")
            params.extend(states)
        if apps:
            clauses.append(f"app IN ({','.join('?' for _ in apps)})")
            params.extend(apps)
        if archived == "exclude":
            clauses.append("archive_observed_at IS NULL")
        elif archived == "only":
            clauses.append("archive_observed_at IS NOT NULL")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    def record_collector_cycle(self, cycle: dict[str, Any]) -> None:
        """수집 사이클 사실을 append-only로 남긴다(T14 S3 D4 · S4b 검증 승급).

        **쓰기 검증은 여기 한 곳**이다(T14 S4b). 호출자에 흩으면 하나만 빠뜨려도 계약이
        무너진다. `declared`인 축은 값이 실제로 제공돼야 하며, 이전의 `cycle.get(axis, 0)`
        폴백이 **재지 않은 0을 확인된 0으로 승격**하던 경로를 여기서 끊는다.
        """
        if "count_scopes" not in cycle:
            # 자동 생성하면 계약을 빠뜨린 호출자가 조용히 통과한다(구현리뷰 P2).
            # ABC 승급의 강제력이 여기서 새지 않도록 fail-closed로 막는다.
            raise CountScopeError("count_scopes 누락 — 사이클 기록은 지식 범위를 요구한다")
        scopes = validate_count_scopes(dict(cycle["count_scopes"]), cycle)
        # capability도 쓰기 한 곳에서 검증한다. 미선언(키 부재/None)은 `unknown`으로
        # 받아들이되, **계약 밖 값은 거절**한다 — 조용히 통과시키면 읽기 쪽 fail-closed가
        # 그 값을 unknown으로 덮어써서 잘못된 선언이 영영 드러나지 않는다.
        capability = cycle.get("process_signal_capability") or CAPABILITY_UNKNOWN
        if capability not in PROCESS_SIGNAL_CAPABILITIES:
            raise CountScopeError(f"알 수 없는 process_signal_capability: {capability!r}")
        # 관측 축도 **같은 강도로** 거절한다(구현리뷰 R1 P1). 한 축만 검사하면 계약 밖 값이
        # 저장된 뒤 읽기의 fail-closed가 그것을 `unknown`으로 덮어써서, 잘못된 수집기 선언이
        # 영원히 드러나지 않는다 — 읽기 방어가 쓰기 게이트를 무력화하는 형태다.
        # `None`은 폴러의 방어 합성 경로가 쓰는 정당한 값이라 허용한다(그 자체가 "모름").
        signal = cycle.get("process_signal")
        if signal is not None and signal not in PROCESS_SIGNAL_VALUES:
            raise CountScopeError(f"알 수 없는 process_signal: {signal!r}")
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO collector_cycles
                    (app, observed_at, status, collected, failed, excluded_archived,
                     process_signal, signal_quality, matched, unmatched, ambiguous,
                     malformed_uuid, process_observed_at, count_scopes, source,
                     process_signal_capability)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(cycle["app"]),
                    str(cycle["observed_at"]),
                    str(cycle["status"]),
                    # scope가 declared가 아닌 축은 저장값이 **의미 없다**(읽기가 null로
                    # 표현한다). 그 사실을 분명히 하려고 0으로 정규화해 둔다 — raw DB를
                    # 직접 읽는 소비자가 생겨도 "센 값"으로 오해하지 않게.
                    *(
                        int(cycle[axis]) if scopes[axis] == SCOPE_DECLARED else 0
                        for axis in ("collected", "failed", "excluded_archived")
                    ),
                    cycle.get("process_signal"),
                    cycle.get("signal_quality"),
                    *(
                        int(cycle[axis]) if scopes[axis] == SCOPE_DECLARED else 0
                        for axis in ("matched", "unmatched", "ambiguous", "malformed_uuid")
                    ),
                    cycle.get("process_observed_at"),
                    json.dumps(scopes, sort_keys=True),
                    str(cycle.get("source") or SOURCE_DECLARED),
                    capability,
                ),
            )

    def preview_app_rows(self, app: str) -> dict[str, Any]:
        """정리 대상 **미리보기**. 삭제하지 않는다(T14 S3 D4 안전계약 ①②)."""
        cur = self._conn.execute(
            "SELECT COUNT(*) AS n, MIN(updated_at) AS oldest, MAX(updated_at) AS newest "
            "FROM sessions WHERE app = ?",
            (app,),
        )
        row = cur.fetchone()
        return {
            "app": app,
            "count": int(row["n"]),
            "oldest": row["oldest"],
            "newest": row["newest"],
        }

    def purge_app_rows(self, app: str, *, confirmed: bool) -> int:
        """`app` **정확 일치** 행만 삭제한다. 사용자 승인 없이는 실행하지 않는다.

        안전 계약(T14 S3 D4): 미리보기 → 정확 일치 범위 → 명시적 승인 → 감사 기록.
        `confirmed=False`면 삭제하지 않고 0을 반환한다.
        """
        if not confirmed:
            return 0
        preview = self.preview_app_rows(app)
        now = _now_iso()
        # 삭제는 비가역이다. 감사 기록이 실패해 "지웠는데 기록이 없는" 상태가 생기지 않도록
        # **같은 트랜잭션**에서 지우고 남긴다.
        with self._conn:
            cur = self._conn.execute("DELETE FROM sessions WHERE app = ?", (app,))
            deleted = int(cur.rowcount or 0)
            payload = {"app": app, "deleted": deleted, "previewed": preview["count"]}
            self._conn.execute(
                "INSERT INTO events (session_id, event_type, payload, created_at) VALUES (?,?,?,?)",
                (
                    f"maintenance:{app}",
                    "rows_purged",
                    json.dumps(payload, ensure_ascii=False),
                    now,
                ),
            )
        # JSONL 감사로그는 DB 커밋 이후에 덧붙인다(파일 실패가 DB 사실을 되돌리지 않는다).
        line = json.dumps(
            {
                "session_id": f"maintenance:{app}",
                "event_type": "rows_purged",
                "payload": payload,
                "created_at": now,
            },
            ensure_ascii=False,
        )
        with self._events_log.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        logger.info("정리 완료: app=%s 삭제 %d행", app, deleted)
        return deleted

    def latest_collector_cycles(
        self, *, conn: sqlite3.Connection | None = None
    ) -> dict[str, dict[str, Any]]:
        """앱별 **최근** 사이클. 수집 건강도 고지의 소스."""
        cur = (conn or self._conn).execute(
            """
            SELECT c.* FROM collector_cycles c
            JOIN (SELECT app, MAX(id) AS max_id FROM collector_cycles GROUP BY app) latest
              ON c.id = latest.max_id
            """
        )
        return {str(row["app"]): self._normalize_cycle(dict(row)) for row in cur.fetchall()}

    @staticmethod
    def _normalize_cycle(row: dict[str, Any]) -> dict[str, Any]:
        """저장 행 → **정규화된** 사이클 사실(T14 S4b).

        정규화는 **이 한 경계에서만** 한다. 웹 계층이 raw JSON을 다시 해석하면 두 곳에서
        해석이 생기고, 둘이 어긋나면 어느 쪽이 참인지 알 수 없다.
        `declared`가 아닌 축은 `None`으로 투영한다 — 저장된 0은 "0건"이 아니라 "모름"이다.
        """
        scopes = parse_count_scopes(
            row.get("count_scopes"), context=f"app={row.get('app')} id={row.get('id')}"
        )
        normalized = dict(row)
        for axis in COUNT_AXES:
            if scopes[axis] != SCOPE_DECLARED:
                normalized[axis] = None
        normalized["count_scopes"] = scopes
        normalized["source"] = row.get("source") or SOURCE_DECLARED
        # 실행 신호의 두 축도 **읽기에서 fail-closed**로 닫는다(T14 S4c-1). NULL(선언 이전
        # 행)·계약 밖 값은 `unknown`이다 — 모르는 것을 "정상 관측"·"줄 수 있음"으로
        # 승격하면 화면이 없는 사실을 단언한다.
        capability = row.get("process_signal_capability")
        normalized["process_signal_capability"] = (
            capability if capability in PROCESS_SIGNAL_CAPABILITIES else CAPABILITY_UNKNOWN
        )
        signal = row.get("process_signal")
        normalized["process_signal"] = (
            signal if signal in PROCESS_SIGNAL_VALUES else PROCESS_SIGNAL_UNKNOWN
        )
        return normalized

    def list_sessions(
        self,
        limit: int = 100,
        *,
        states: Sequence[str] | None = None,
        apps: Sequence[str] | None = None,
        archived: str = ARCHIVED_SCOPE_DEFAULT,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        """최근 **활동** 순으로 세션 목록 반환(절단 창).

        정렬 키는 `updated_at`(=폴러가 행을 쓴 시각)이 아니라 `last_activity`다.
        `updated_at`은 수집기 순회 순서를 반영할 뿐이라, 그것으로 자르면 마지막
        수집기의 행만 창에 남고 실제 활성 세션이 밀려난다(LESSON-002).
        `last_activity` NULL은 정보가 없는 행이므로 뒤로 보내고, 동률은
        `session_id`로 결정론적으로 깬다(같은 조건이면 같은 창을 돌려준다).

        **필터는 서버에서 건다**: `last_activity` 정렬은 `stale`/`holding`과
        반상관이라, 클라이언트가 창 안에서만 거르면 "KPI는 652건이라는데
        표에는 없다"가 된다(LESSON-002 rule 8). 상태/앱 조건은 전량에 적용한 뒤
        자른다.

        총계는 이 반환값 길이가 아니라 `session_summary()`/`sessions_view()`로 얻는다.
        """
        where, params = self._session_filter(states, apps, archived)
        cur = (conn or self._conn).execute(
            f"""
            SELECT * FROM sessions{where}
            ORDER BY last_activity IS NULL, last_activity DESC, session_id ASC
            LIMIT ?
            """,
            (*params, limit),
        )
        return [dict(row) for row in cur.fetchall()]

    def count_sessions(
        self,
        *,
        states: Sequence[str] | None = None,
        apps: Sequence[str] | None = None,
        archived: str = ARCHIVED_SCOPE_DEFAULT,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """조건에 맞는 세션 수(절단 전). 조건이 없으면 저장된 전체."""
        where, params = self._session_filter(states, apps, archived)
        row = (conn or self._conn).execute(
            f"SELECT COUNT(*) AS n FROM sessions{where}", params
        ).fetchone()
        return int(row["n"])

    def sessions_view(
        self,
        limit: int,
        *,
        states: Sequence[str] | None = None,
        apps: Sequence[str] | None = None,
        archived: str = ARCHIVED_SCOPE_DEFAULT,
    ) -> dict[str, Any]:
        """창 + 조건 일치 수 + 전량 집계 + 수집 사이클을 **단일 read 스냅샷**으로 반환.

        창과 집계를 따로 조회하면 그 사이 폴러 upsert가 끼어들어 `returned`·
        `total`·`sum(by_state)`가 서로 다른 시점을 가리킬 수 있다(리뷰 P2).
        WAL에서 `BEGIN DEFERRED`로 연 읽기 전용 연결은 트랜잭션 동안 일관된
        스냅샷을 보므로, 봉투 전체가 한 시점을 나타낸다.
        쓰기 연결(`self._conn`)을 재사용하지 않는 이유는 폴러의 커밋이 이
        읽기 트랜잭션을 끝내버리기 때문이다(`_approval_connection`과 같은 이유).

        `matched`(조건 일치 수)와 `total`(전량)을 분리해 싣는다. 필터가 걸린
        화면에서 "300 / 1026"만 보여주면 무엇이 잘렸는지 알 수 없다.
        `summary`는 요청 필터와 무관하다 — KPI의 권위이므로 필터를 켤 때마다 총계가
        바뀌면 그것 자체가 거짓 보고가 된다. 단 `summary` 안의 항목별 **범위는 다르며**,
        그 사실을 `state_distribution_scope`가 말한다(T14 S4a D7).

        `items`/`matched`/`truncated`는 `archived_scope`가 정의한 집합을 뜻한다.
        기본은 `include`(현행 동작 무변경) — 목록에서 무엇을 뺄지는 **표시 정책**이므로
        API 기본값을 조용히 바꾸지 않는다.

        `states`가 계약 어휘에도 저장소에도 없는 값을 담으면 `UnknownStateFilter`를
        올린다(T14 S4c-2 D5·D7). **수용 어휘는 `archived` scope에 의존하지 않는다** —
        의존시키면 같은 `states=X`가 다른 파라미터 때문에 거절되기도 수용되기도 한다.
        조건에 맞는 행이 없으면 그것은 오류가 아니라 `matched=0`이라는 사실이다.
        """
        conn = self._read_connection()
        try:
            conn.execute("BEGIN DEFERRED")
            # **검증도 이 트랜잭션 안에서** 한다(T14 S4c-2 D7). 밖에서 별도 조회로
            # 수용 어휘를 구하면 판정과 응답이 다른 시점을 보게 되어, 같은 요청에서
            # "수용했는데 분포엔 없는 상태" 또는 그 반대가 나온다 — 봉투가 한 시점이라는
            # S1의 주장이 거짓이 된다.
            summary = self.session_summary(conn=conn)
            observed = self._observed_states(summary)
            accepted = sorted(set(state_vocabulary()) | set(observed))
            invalid = sorted(set(states or ()) - set(accepted))
            if invalid:
                # 저장소에 있는 값으로 거르는 질의는 **답할 수 있는 질의**다(D2).
                # 여기 걸리는 것은 계약에도 데이터에도 없는 값뿐이다.
                raise UnknownStateFilter(invalid, accepted)
            summary["state_vocabulary"] = list(state_vocabulary())
            summary["observed_states"] = observed
            items = self.list_sessions(
                limit, states=states, apps=apps, archived=archived, conn=conn
            )
            matched = self.count_sessions(
                states=states, apps=apps, archived=archived, conn=conn
            )
            # 사이클도 **같은 트랜잭션에서** 읽는다(T14 S4a D4). S3에서는 이 조회가
            # 다른 연결(`self._conn`)로 나가 있어, 봉투가 "단일 스냅샷"이라 적어 두고
            # 실제로는 서로 다른 시점의 두 수를 실었다 — 주석이 거짓이었다.
            cycles = self.latest_collector_cycles(conn=conn)
            conn.execute("COMMIT")
        finally:
            conn.close()
        return {
            "items": items,
            "returned": len(items),
            "matched": matched,
            "total": int(summary["total"]),
            "limit": limit,
            "filtered": bool(states or apps),
            "archived_scope": archived,
            "truncated": len(items) < matched,
            "summary": summary,
            "cycles": cycles,
        }

    def _read_connection(self) -> sqlite3.Connection:
        """스냅샷 읽기 전용 연결. 호출자가 transaction과 close를 소유한다."""
        conn = sqlite3.connect(self._db_path, isolation_level=None, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def session_summary(self, *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        """전체 테이블 기준 집계.

        UI KPI가 절단된 창에서 파생되면 "전체 N"·"정상" 같은 거짓 단언이 나온다.
        집계는 언제나 전량 기준으로 서버가 계산해 내려준다.

        **분포는 두 개다(T14 S4a D7).** `by_state`는 *현재* 상태 분포를 주장하는 이름인데,
        보관된 세션의 현재 상태를 우리는 모른다(재판정하지 않으므로 마지막으로 알던 값이
        얼어 있다). 얼린 값을 현재 분포에 넣으면 몇 달 전 `running`이 지금 실행 중인 것처럼
        집계된다. 그래서 `by_state`는 **보관 미관측 행**만 세고, 보관 행은
        `archived_by_last_known_state`라는 **이름이 현재를 주장하지 않는** 분포로 분리한다.

        불변식: `sum(by_state) == archive_not_observed_total`,
        `sum(archived_by_last_known_state) == archived_total`, 둘의 합 `== total`.
        `total`·`by_app`은 **행 수 사실**이므로 전량 그대로다(의미 불변).
        """
        source = conn or self._conn
        by_state = {
            str(row["state"]): int(row["n"])
            for row in source.execute(
                "SELECT state, COUNT(*) AS n FROM sessions "
                "WHERE archive_observed_at IS NULL GROUP BY state"
            )
        }
        archived_by_state = {
            str(row["state"]): int(row["n"])
            for row in source.execute(
                "SELECT state, COUNT(*) AS n FROM sessions "
                "WHERE archive_observed_at IS NOT NULL GROUP BY state"
            )
        }
        by_app = {
            str(row["app"]): int(row["n"])
            for row in source.execute(
                "SELECT app, COUNT(*) AS n FROM sessions GROUP BY app"
            )
        }
        projects = source.execute(
            "SELECT COUNT(DISTINCT COALESCE(project_path, '')) AS n FROM sessions"
        ).fetchone()
        return {
            "total": self.count_sessions(conn=source),
            "archive_not_observed_total": self.count_sessions(
                archived="exclude", conn=source
            ),
            "archived_total": self.count_sessions(archived="only", conn=source),
            "by_state": by_state,
            "state_distribution_scope": STATE_DISTRIBUTION_SCOPE,
            "archived_by_last_known_state": archived_by_state,
            "by_app": by_app,
            "projects": int(projects["n"]),
        }

    @staticmethod
    def _observed_states(summary: dict[str, Any]) -> list[str]:
        """**저장소에 실제로 있는** 상태 — 보관 여부와 무관한 전량(T14 S4c-2 D2a).

        `by_state`만 쓰면 안 된다. S4a가 분포를 둘로 나눈 뒤로 그건 **보관 미관측 행**의
        분포이고, `/api/sessions`의 기본 범위는 보관을 **포함**한다 — 조회하면 나오는
        행을 그 상태로는 못 거르는 모순이 생긴다.

        두 분포의 키 합집합으로 유도한다: 이미 계산된 값이라 **추가 스캔이 없고**,
        같은 트랜잭션 안이라 판정과 응답이 한 시점을 본다(D7).
        """
        return sorted(
            set(summary.get("by_state") or {})
            | set(summary.get("archived_by_last_known_state") or {})
        )

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
        """SessionRecord의 앱 네임스페이스 키로 저장된 세션 조회.

        **폴러 전용 경로**라 세션 전용 연결에서 읽는다(T14 S6 D3 소유권 규칙) — 폴러의
        스레드 단계가 주 연결을 만지면 루프 단계의 이벤트·승인 쓰기와 같은 연결을 두
        스레드가 동시에 쓰게 된다.
        """
        key = session_key(record.app, record.session_id)
        cur = self._session_conn.execute(
            "SELECT * FROM sessions WHERE session_id = ? OR native_session_id = ?",
            (key, key),
        )
        row = cur.fetchone()
        return dict(row) if row else None

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
