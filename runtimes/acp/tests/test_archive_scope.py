"""tests/test_archive_scope.py — 보관(archived) 사실의 정직한 표현 회귀 (T14 S4a).

이 파일이 잠그는 실제 결함(2026-07-28 실측, `.acp/acp.db`):
  - 수집기의 archived 제외가 **새 upsert만** 막아, 이미 저장된 246행이 `sessions`에
    영구 잔존하며 `total=1073`에 그대로 포함됐다(claude 358 = 수집 112 + "제외" 246).
  - 그런데 API는 `summary.excluded_archived=246`을 `total` 옆에 실어, 일어나지 않은
    제외를 일어났다고 **단언**했다. 침묵보다 나쁘다 — 침묵은 모름을 남기지만
    이 고지는 거짓을 남긴다.
  - 보관 세션을 계속 재판정하면 이미 정리한 세션이 stale/error로 옮겨가 알림을 만들고
    "조치 필요" 수에 섞인다(S2가 656→3으로 고친 결함의 재발).
  - 얼린 상태를 `by_state`에 두면 몇 달 전 `running`이 현재 실행 중처럼 집계된다.

각 테스트는 설계 §4의 검증 항목(V1~V21)에 대응하며, **구현을 되돌리면 실패**하도록
썼다(mutation gate). 판별력 없는 통과 기준은 게이트가 아니다.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import acp.web.app as webapp
from acp.collectors.base import (
    CAPABILITY_SUPPORTED,
    PROCESS_SIGNAL_OK,
    CollectCycle,
)
from acp.models import SessionRecord, SessionState
from acp.store import ARCHIVED_SCOPES, STATE_DISTRIBUTION_SCOPE, SessionStore
from acp.web.app import EventBroadcaster, app, init_app

NOW = datetime(2026, 7, 28, 9, 0, tzinfo=timezone.utc)


def _record(sid: str, app_name: str = "fake", **kwargs) -> SessionRecord:
    base = dict(app=app_name, session_id=sid, source_file="test")
    base.update(kwargs)
    return SessionRecord(**base)


@pytest.fixture(autouse=True)
def _restore_app_globals():
    prev_store = webapp._store
    prev_broadcaster = webapp._broadcaster
    prev_run_manager = webapp._run_manager
    yield
    webapp._store = prev_store
    webapp._broadcaster = prev_broadcaster
    webapp._run_manager = prev_run_manager


@pytest.fixture
def store(tmp_path):
    s = SessionStore(str(tmp_path / "acp.db"), str(tmp_path / "events.jsonl"))
    yield s
    s.close()


def _seed_mixed(store: SessionStore) -> None:
    """보관/비보관 혼합 fixture.

    보관 행에 **action 상태(error)와 cleanup 상태(stale)를 모두** 넣는다 — 그래야
    "보관은 action/cleanup에 세지 않는다"는 등식이 판별력을 갖는다(5R P2-2).
    """
    store.upsert_session(_record("live-1", last_activity=NOW), SessionState.LIVE)
    store.upsert_session(
        _record("err-1", last_activity=NOW - timedelta(hours=2)), SessionState.ERROR
    )
    store.upsert_session(
        _record("stale-1", last_activity=NOW - timedelta(days=9)), SessionState.STALE
    )
    store.upsert_session(
        _record("arch-err", archived=True, last_activity=NOW - timedelta(days=30)),
        SessionState.ERROR,
    )
    store.upsert_session(
        _record("arch-stale", archived=True, last_activity=NOW - timedelta(days=40)),
        SessionState.STALE,
    )
    store.upsert_session(
        _record("arch-run", archived=True, last_activity=NOW - timedelta(days=60)),
        SessionState.RUNNING,
    )


def _client(store: SessionStore) -> TestClient:
    init_app(store, EventBroadcaster())
    return TestClient(app)


# ══════════════════════════════════════
# V1 — 마이그레이션 (기존 DB에 멱등 적용)
# ══════════════════════════════════════

def test_v1_migration_is_idempotent_and_preserves_rows(tmp_path):
    """기존 DB에 컬럼을 멱등 추가하고, 사이클 감사 컬럼·값을 보존한다."""
    db = str(tmp_path / "legacy.db")
    log = str(tmp_path / "events.jsonl")

    # T14 S4a 이전 모양: sessions에 archive_observed_at이 없는 DB
    raw = sqlite3.connect(db)
    raw.executescript(
        """
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY, native_session_id TEXT, app TEXT NOT NULL,
            project_path TEXT, model TEXT, last_activity TEXT, running_pid INTEGER,
            running_cmd TEXT, raw_status TEXT, last_event TEXT,
            state TEXT NOT NULL DEFAULT 'unknown', source_file TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE collector_cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT, app TEXT NOT NULL,
            observed_at TEXT NOT NULL, status TEXT NOT NULL,
            collected INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
            excluded_archived INTEGER NOT NULL DEFAULT 0, process_signal TEXT
        );
        INSERT INTO sessions (session_id, app, state, updated_at)
        VALUES ('claude:old', 'claude', 'stale', '2026-07-01T00:00:00+00:00');
        INSERT INTO collector_cycles (app, observed_at, status, collected, excluded_archived)
        VALUES ('claude', '2026-07-01T00:00:00+00:00', 'success_complete', 112, 246);
        """
    )
    raw.commit()
    raw.close()

    first = SessionStore(db, log)
    first.close()
    second = SessionStore(db, log)  # 재적용 무해해야 한다
    second.close()

    check = sqlite3.connect(db)
    check.row_factory = sqlite3.Row
    cols = [r["name"] for r in check.execute("PRAGMA table_info(sessions)")]
    assert cols.count("archive_observed_at") == 1, "재적용해도 컬럼은 하나"
    added = next(
        r for r in check.execute("PRAGMA table_info(sessions)")
        if r["name"] == "archive_observed_at"
    )
    assert added["type"] == "TEXT"
    assert added["dflt_value"] is None, "기본은 NULL — 기존 행을 보관으로 단정하지 않는다"

    kept = check.execute("SELECT * FROM sessions WHERE session_id='claude:old'").fetchone()
    assert kept["state"] == "stale"
    assert kept["archive_observed_at"] is None

    # 이름 폐기는 **API projection 한정**이다. 사이클 감사 컬럼과 값은 보존된다(V12 대응).
    cycle_cols = [r["name"] for r in check.execute("PRAGMA table_info(collector_cycles)")]
    assert "excluded_archived" in cycle_cols
    row = check.execute("SELECT excluded_archived FROM collector_cycles").fetchone()
    assert row["excluded_archived"] == 246
    check.close()


# ══════════════════════════════════════
# V2·V18 — 분류와 분포 등식
# ══════════════════════════════════════

def test_v2_totals_partition_exactly(store):
    _seed_mixed(store)
    summary = store.session_summary()
    assert summary["archive_not_observed_total"] == 3
    assert summary["archived_total"] == 3
    assert (
        summary["archive_not_observed_total"] + summary["archived_total"]
        == summary["total"]
        == 6
    )


def test_v18_distributions_split_and_action_sums_match(store):
    """등식 5개를 **각각** assert한다. 하나의 mutation이 전부를 깨야 할 이유는 없다."""
    _seed_mixed(store)
    payload = webapp._sessions_payload(store, limit=50)
    summary = payload["summary"]

    by_state = summary["by_state"]
    archived_dist = summary["archived_by_last_known_state"]

    # ① 현재 분포는 보관 미관측 행만
    assert sum(by_state.values()) == summary["archive_not_observed_total"] == 3
    # ② 보관 분포는 보관 행만
    assert sum(archived_dist.values()) == summary["archived_total"] == 3
    # ③ 둘의 합이 전량
    assert sum(by_state.values()) + sum(archived_dist.values()) == summary["total"]
    # ④⑤ action/cleanup은 현재 분포의 부분합과 정확히 일치 — 클라이언트가 재계산해도 같다
    assert summary["action_required"] == sum(
        by_state.get(s, 0) for s in summary["action_states"]
    ) == 1
    assert summary["cleanup_required"] == sum(
        by_state.get(s, 0) for s in summary["cleanup_states"]
    ) == 1

    # 보관 행이 섞였다면 error 2 / stale 2가 됐을 것 — 그게 S2 결함의 재발이다.
    assert archived_dist == {"error": 1, "stale": 1, "running": 1}
    assert "running" not in by_state, "몇 달 전 running이 현재 분포에 있으면 안 된다"


def test_v19_by_state_excludes_archived_and_declares_scope(store):
    """`by_state` 의미 변경을 **테스트가 고정**한다(파괴적 변경의 명시)."""
    _seed_mixed(store)
    summary = store.session_summary()
    assert summary["state_distribution_scope"] == STATE_DISTRIBUTION_SCOPE
    assert summary["by_state"] == {"live": 1, "error": 1, "stale": 1}
    assert summary["total"] == 6, "총계(행 수)는 의미 불변"


# ══════════════════════════════════════
# V3·V4·V5 — 저장 규칙
# ══════════════════════════════════════

def test_v3_archived_row_is_kept_and_flagged(store):
    store.upsert_session(_record("a1", archived=True), SessionState.UNKNOWN)
    row = store.get_session("fake:a1")
    assert row is not None, "보관이라고 행을 버리면 분류 자체가 불가능하다"
    assert row["archive_observed_at"] is not None


def test_v4_unarchive_returns_to_active(store):
    store.upsert_session(_record("a1", archived=True), SessionState.UNKNOWN)
    assert store.session_summary()["archived_total"] == 1
    store.upsert_session(_record("a1", archived=False), SessionState.LIVE)
    row = store.get_session("fake:a1")
    assert row["archive_observed_at"] is None, "보관 해제가 되돌아와야 한다"
    assert store.session_summary()["archived_total"] == 0
    assert store.session_summary()["by_state"] == {"live": 1}


def test_v5_first_observation_time_is_preserved(store):
    store.upsert_session(_record("a1", archived=True), SessionState.UNKNOWN)
    first = store.get_session("fake:a1")["archive_observed_at"]
    store.upsert_session(_record("a1", archived=True), SessionState.UNKNOWN)
    again = store.get_session("fake:a1")["archive_observed_at"]
    assert again == first, "매 사이클 갱신하면 '언제부터 보관인가'가 사라진다"


def test_v6_row_without_archive_signal_stays_unclassified(store):
    """알려진 한계(characterization test — mutation gate 아님).

    파일이 사라진 보관 세션은 다시 관측되지 않으므로 영원히
    `archive_not_observed_total`에 남는다. 숨기지 않고 테스트로 명시한다.
    이 수를 "살아있는 세션 수"라 부르지 않는 이유이기도 하다.
    """
    store.upsert_session(_record("ghost"), SessionState.STALE)
    summary = store.session_summary()
    assert summary["archive_not_observed_total"] == 1
    assert summary["archived_total"] == 0


# ══════════════════════════════════════
# V7~V10 — API 범위 계약
# ══════════════════════════════════════

def test_v7_default_scope_is_include_no_behavior_change(store):
    """기본 동작을 바꾸지 않는다 — 파괴적 기본 변경을 테스트가 막는다."""
    _seed_mixed(store)
    resp = _client(store).get("/api/sessions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["archived_scope"] == "include"
    assert body["matched"] == 6
    assert {item["session_id"] for item in body["items"]} >= {"fake:arch-err"}


@pytest.mark.parametrize(
    "scope,expected",
    [("exclude", {"fake:live-1", "fake:err-1", "fake:stale-1"}),
     ("only", {"fake:arch-err", "fake:arch-stale", "fake:arch-run"})],
)
def test_v8_scopes_are_reachable_and_counted_on_full_population(store, scope, expected):
    """창이 아니라 **전량**에 적용돼야 한다 — 창에만 걸면 matched가 거짓이 된다."""
    _seed_mixed(store)
    body = _client(store).get(f"/api/sessions?archived={scope}&limit=2").json()
    assert body["archived_scope"] == scope
    assert body["matched"] == 3, "matched는 절단 전 전량 기준"
    assert body["returned"] == 2 and body["truncated"] is True
    full = _client(store).get(f"/api/sessions?archived={scope}").json()
    assert {item["session_id"] for item in full["items"]} == expected


def test_v9_unknown_scope_is_422(store):
    resp = _client(store).get("/api/sessions?archived=bogus")
    assert resp.status_code == 422, "모르는 값을 기본값으로 삼키면 요청과 응답이 달라진다"
    assert "bogus" in resp.json()["detail"]


def test_v10_summary_is_independent_of_scope(store):
    _seed_mixed(store)
    client = _client(store)
    a = client.get("/api/sessions?archived=exclude").json()["summary"]
    b = client.get("/api/sessions?archived=only").json()["summary"]
    for key in ("total", "by_state", "archived_total", "archived_by_last_known_state"):
        assert a[key] == b[key], f"{key}는 요청 범위와 무관해야 한다"


def test_scope_vocabulary_is_single_sourced():
    assert ARCHIVED_SCOPES == ("include", "exclude", "only")


# ══════════════════════════════════════
# V11 — 봉투 단일 read 스냅샷
# ══════════════════════════════════════

def test_v11_cycles_are_read_in_the_same_transaction(store, monkeypatch):
    """사이클이 세션과 **같은 연결**에서 읽혀야 한다.

    S3는 이 조회가 다른 연결로 나가 있었는데도 봉투 주석은 "단일 스냅샷"이라 적어
    두었다 — 주석이 거짓이었다. 쓰기측 원자성은 이 게이트가 증명하지 않는다(S4d).
    """
    seen: list[bool] = []
    original = SessionStore.latest_collector_cycles

    def spy(self, *, conn=None):
        seen.append(conn is not None)
        return original(self, conn=conn)

    monkeypatch.setattr(SessionStore, "latest_collector_cycles", spy)
    store.sessions_view(limit=10)
    assert seen == [True], "주입된 연결 없이 읽으면 별도 시점이 된다"


# ══════════════════════════════════════
# V12·V13 — 이름과 기존 계약
# ══════════════════════════════════════

def _iter_keys(node):
    """중첩 구조의 모든 키를 훑는다."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _iter_keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_keys(item)


def test_v12_api_retires_the_misleading_name(store):
    """옛 이름은 **중첩 경로까지** 없어야 한다.

    구현리뷰 P1: 최상위 `summary`만 검사하던 초판은 `collector_health.*.excluded_archived`가
    같은 거짓 의미를 한 단계 아래에서 그대로 유지하는 것을 놓쳤다.
    """
    _seed_mixed(store)
    # 사이클 기록이 있어야 collector_health가 비지 않는다 — 빈 dict를 통과로 삼으면
    # 이 테스트가 아무것도 고정하지 않는다.
    cycle = CollectCycle(app="fake", observed_at=NOW)
    cycle.declare(collected=6, failed=0, excluded_archived=3)
    store.record_collector_cycle(cycle.as_row())
    body = _client(store).get("/api/sessions").json()
    summary = body["summary"]

    assert summary["collector_health"], "건강도가 비어 있으면 이 검사는 무의미하다"
    assert "excluded_archived" not in set(_iter_keys(body)), (
        "빠지지도 않은 행을 '제외'라 부르는 이름은 어느 깊이에도 남지 않는다"
    )
    assert summary["archived_seen_in_latest_cycle"] == 3
    assert summary["collector_health"]["fake"]["archived_seen"] == 3


def test_v13_total_keeps_its_meaning(store):
    """`total`은 저장 전량 — 보관을 섞어도 의미가 바뀌지 않는다."""
    _seed_mixed(store)
    body = _client(store).get("/api/sessions").json()
    assert body["total"] == 6
    assert body["summary"]["total"] == 6
    assert body["summary"]["by_app"] == {"fake": 6}, "by_app도 행 수 사실이라 전량"


# ══════════════════════════════════════
# V21 — 최소 UI 소비 계약(D8)의 서버측 전제
# ══════════════════════════════════════

def test_v21_envelope_carries_everything_the_ui_needs(store):
    """UI가 범위를 말하려면 봉투가 먼저 말해야 한다(JS 계약은 별도 파일)."""
    _seed_mixed(store)
    summary = _client(store).get("/api/sessions").json()["summary"]
    assert summary["archived_total"] == 3
    assert summary["state_distribution_scope"] == STATE_DISTRIBUTION_SCOPE
    assert summary["action_scope"] == STATE_DISTRIBUTION_SCOPE
    assert summary["archived_by_last_known_state"]


# ══════════════════════════════════════
# V14·V20 — 관제 경계 (폴러)
# ══════════════════════════════════════

class _Broadcaster:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def publish(self, event: dict) -> None:
        self.events.append(event)


class _Notifier:
    def __init__(self) -> None:
        self.events: list = []

    def notify(self, event) -> None:
        self.events.append(event)


class _ScriptedCollector:
    """수집 결과를 테스트가 갈아끼우는 수집기."""

    def __init__(self, records) -> None:
        self.records = records
        # 실행 신호를 **관측했다고 선언**한다(T14 S4c-1 D1c). 선언하지 않으면 폴러가
        # 가용성을 False로 보고 stale 구간을 UNKNOWN으로 남기므로, 보관 여부를 다루는
        # 이 파일의 검사가 신호 미선언 때문에 흐려진다.
        self._cycle = CollectCycle(
            app="fake",
            process_signal_capability=CAPABILITY_SUPPORTED,
            process_signal=PROCESS_SIGNAL_OK,
        )

    @property
    def app_name(self) -> str:
        return "fake"

    @property
    def last_cycle(self) -> CollectCycle:
        return self._cycle

    def collect(self):
        self._cycle.declare(collected=len(self.records), failed=0)
        return self.records


def _long_idle(session_id: str, *, archived: bool) -> SessionRecord:
    """stale 판정을 받고도 남을 만큼 오래 멈춘 레코드."""
    return SessionRecord(
        app="fake",
        session_id=session_id,
        project_path="C:/repo",
        last_activity=datetime.now(timezone.utc) - timedelta(days=30),
        source_file="test",
        archived=archived,
    )


def _poller(store, broadcaster, notifier=None):
    from acp.config import AppConfig, LivenessConfig, NotifyConfig
    from acp.poller import Poller

    cfg = AppConfig(
        liveness=LivenessConfig(idle_threshold=10, hold_threshold=20, stale_ttl=40),
        notify=NotifyConfig(toast_enabled=False, webhook_url="", notify_cooldown=3600),
    )
    return Poller(store, cfg, broadcaster, notifier=notifier)


@pytest.mark.asyncio
async def test_v14_archived_is_outside_the_control_plane(store):
    """여섯 경로를 **각각** 확인한다 — 알림만 막으면 나머지 다섯을 놓친다.

    ①재판정 없음 ②처음 보는 보관 행은 UNKNOWN ③state_change 없음 ④SSE 없음
    ⑤action_required 미포함 ⑥cleanup_required 미포함.
    """
    broadcaster, notifier = _Broadcaster(), _Notifier()
    poller = _poller(store, broadcaster, notifier)

    # 먼저 비보관으로 LIVE 상태를 만든 뒤 보관으로 전환한다.
    active = SessionRecord(
        app="fake", session_id="s1", source_file="test",
        last_activity=datetime.now(timezone.utc),
    )
    poller.register(_ScriptedCollector([active]))
    await poller._tick()
    assert store.get_session("fake:s1")["state"] == SessionState.LIVE
    before_events = len(broadcaster.events)

    # 같은 세션이 보관됐고, 활동은 30일 전이다 — 재판정하면 stale로 떨어질 조건.
    poller._collectors[0].records = [_long_idle("s1", archived=True)]
    poller._collectors[0].records.append(_long_idle("s2", archived=True))
    await poller._tick()

    row = store.get_session("fake:s1")
    assert row["state"] == SessionState.LIVE, "① 보관 행을 재판정하면 안 된다"
    assert row["archive_observed_at"] is not None
    fresh = store.get_session("fake:s2")
    assert fresh["state"] == SessionState.UNKNOWN, "② 상태를 지어내지 않는다"

    types = [e for e in broadcaster.events[before_events:]]
    assert types == [], "③④ 보관 전환은 상태 전이 사건이 아니다(이벤트·SSE 없음)"
    assert notifier.events == [], "알림도 없다"

    summary = webapp._sessions_payload(store, limit=50)["summary"]
    assert summary["action_required"] == 0, "⑤ 이미 정리한 세션을 '조치 필요'라 하지 않는다"
    assert summary["cleanup_required"] == 0, "⑥ 정리 대상에도 세지 않는다"
    assert summary["archived_total"] == 2


@pytest.mark.asyncio
async def test_v14_non_archived_twin_still_flows(store):
    """대조군 — 같은 조건의 비보관 행은 전이·알림이 **일어나야** 한다.

    이게 없으면 위 테스트는 "아무 일도 안 일어남"만 확인하는 무의미한 통과가 된다.
    """
    broadcaster, notifier = _Broadcaster(), _Notifier()
    poller = _poller(store, broadcaster, notifier)
    poller.register(_ScriptedCollector([
        SessionRecord(
            app="fake", session_id="t1", source_file="test",
            last_activity=datetime.now(timezone.utc),
        )
    ]))
    await poller._tick()
    poller._collectors[0].records = [_long_idle("t1", archived=False)]
    await poller._tick()

    assert store.get_session("fake:t1")["state"] == SessionState.STALE
    assert [e["type"] for e in broadcaster.events][-1] == "state_change"
    summary = webapp._sessions_payload(store, limit=50)["summary"]
    assert summary["cleanup_required"] == 1


@pytest.mark.asyncio
async def test_v20_unarchive_is_rederived_in_the_same_cycle(store, monkeypatch):
    """보관 해제는 **같은 사이클에서** 정상 재판정된다 — 얼린 상태 노출 창 0.

    관측점: ①해제 저장 ②정상 재판정 ③state_change/SSE ④집계. 네 지점 어디에서도
    얼린 옛 상태(LIVE)가 보이면 안 된다.
    """
    broadcaster, notifier = _Broadcaster(), _Notifier()
    poller = _poller(store, broadcaster, notifier)
    poller.register(_ScriptedCollector([
        SessionRecord(
            app="fake", session_id="u1", source_file="test",
            last_activity=datetime.now(timezone.utc),
        )
    ]))
    await poller._tick()  # LIVE

    poller._collectors[0].records = [_long_idle("u1", archived=True)]
    await poller._tick()  # 보관 — LIVE가 얼어붙는다
    assert store.get_session("fake:u1")["state"] == SessionState.LIVE
    assert webapp._sessions_payload(store, limit=50)["summary"]["by_state"] == {}

    # 관측점을 **실제로 잡는다**: 해제 저장과 재판정이 둘로 갈리면(먼저 NULL만 쓰고
    # 다음 사이클에 재판정) 그 사이 얼린 LIVE가 `by_state`·SSE·KPI에 노출된다.
    # 최종 결과만 보면 그 창을 못 잡으므로, upsert에 실린 상태를 가로채 확인한다.
    upserts: list[tuple[str, object]] = []
    original_upsert = SessionStore.upsert_session

    def spy_upsert(self, record, state, phase=None):
        if record.session_id == "u1":
            upserts.append((("archived" if record.archived else "active"), state))
        return original_upsert(self, record, state, phase=phase)

    monkeypatch.setattr(SessionStore, "upsert_session", spy_upsert)
    poller._collectors[0].records = [_long_idle("u1", archived=False)]
    await poller._tick()  # 해제

    assert upserts == [("active", SessionState.STALE)], (
        "해제 사이클의 쓰기는 **한 번**이고, 그 한 번이 이미 재판정된 상태를 싣는다 — "
        "NULL만 먼저 쓰고 나중에 재판정하면 그 사이에 얼린 LIVE가 보인다"
    )
    row = store.get_session("fake:u1")
    assert row["archive_observed_at"] is None, "① 해제가 저장됐다"
    assert row["state"] == SessionState.STALE, "② 같은 사이클에서 재판정됐다(LIVE 아님)"
    assert broadcaster.events[-1]["state"] == "stale", "③ 전이는 재판정된 상태로 발행"
    summary = webapp._sessions_payload(store, limit=50)["summary"]
    assert summary["by_state"] == {"stale": 1}, "④ 집계에도 얼린 값이 남지 않는다"
    assert summary["archived_total"] == 0


# ══════════════════════════════════════
# V15 — 설정 deprecation (D6)
# ══════════════════════════════════════

@pytest.mark.parametrize("value", [True, False])
def test_v15_include_archived_warns_regardless_of_value(tmp_path, caplog, value):
    """키가 있으면 **값과 무관하게** 경고한다.

    실제로 무의미해지는 값은 오히려 기본값이자 대다수인 `False`다. `True`에만
    경고하면 "제외되고 있다"고 믿는 대다수 사용자가 아무 경고도 못 받는다 —
    조용한 no-op은 설정이 먹히는 줄 아는 사용자를 속이는 것이다.
    """
    from acp.config import AppConfig

    cfg_file = tmp_path / "paths.yaml"
    cfg_file.write_text(f"include_archived: {str(value).lower()}\n", encoding="utf-8")
    with caplog.at_level("WARNING", logger="acp.config"):
        cfg = AppConfig.load(str(cfg_file))
    assert cfg.include_archived is value
    assert any("include_archived" in r.message for r in caplog.records)


def test_v15_absent_key_does_not_warn(tmp_path, caplog):
    """선언하지 않은 사용자는 속일 것이 없으므로 경고하지 않는다(소음 방지)."""
    from acp.config import AppConfig

    cfg_file = tmp_path / "paths.yaml"
    cfg_file.write_text("orch_events_dir: ''\n", encoding="utf-8")
    with caplog.at_level("WARNING", logger="acp.config"):
        AppConfig.load(str(cfg_file))
    assert not any("include_archived" in r.message for r in caplog.records)


# ══════════════════════════════════════
# 자원 캐시 무효화 (D8 배포 보장)
# ══════════════════════════════════════

def test_static_assets_are_content_versioned(store):
    """자원 URL이 고정이면 낡은 JS + 새 HTML 조합이 만들어진다.

    라이브에서 실측한 결함: 그 조합이 화면에 `보관 undefined건`을 찍고 KPI를 창 파생
    값(`전체 300`)으로 퇴행시켰다. 어느 쪽 버전보다도 나쁜 상태다. "API와 UI를 한
    배포 단위로"(게이트 G)는 브라우저가 옛 자원을 계속 쓰면 지켜지지 않는다.
    """
    html = _client(store).get("/").text
    assert "/static/dashboard.js?v=" in html
    assert "/static/style.css?v=" in html


def test_asset_version_tracks_content(tmp_path, monkeypatch):
    """내용이 바뀌면 토큰도 바뀐다 — 고정 문자열이면 캐시가 그대로 적중한다."""
    import acp.web.app as mod

    static = tmp_path / "static"
    static.mkdir()
    (static / "dashboard.js").write_text("a", encoding="utf-8")
    (static / "style.css").write_text("b", encoding="utf-8")
    monkeypatch.setattr(mod, "_STATIC_DIR", static)

    first = mod._asset_version()
    (static / "dashboard.js").write_text("a2", encoding="utf-8")
    assert mod._asset_version() != first


def test_v21_archived_distribution_is_rendered_and_reachable(store):
    """보관 분포가 **마크업으로** 도달 가능해야 한다.

    구현리뷰 P2: JS 계약은 getter가 엔트리를 돌려주는지만 봤고, 화면에 그 블록이
    실제로 있는지는 아무도 고정하지 않았다. 집계에서 뺀 것을 **도달까지** 막으면
    S1의 도달 가능성 규칙 위반이다.

    한계(정직하게): 이 검사는 마크업과 바인딩의 존재를 고정할 뿐, 브라우저에서
    펼쳐지는 동작까지 증명하지 않는다(e2e는 NOT CLAIMED).
    """
    _seed_mixed(store)
    html = _client(store).get("/").text
    assert 'data-testid="archived-last-known"' in html
    assert "archivedLastKnownEntries" in html, "분포 바인딩이 있어야 도달 가능하다"
    assert "kpis.archivedTotal" in html
    assert 'data-testid="kpi-archived-scope"' in html
    assert 'data-testid="kpi-state-scope"' in html
