"""tests/test_session_visibility.py — 세션 조회 창·총계 정직성 회귀.

이 파일이 잠그는 실제 결함(2026-07-27 실측):
  - `list_sessions`가 `updated_at`(폴러 write 시각)로 정렬해, 마지막 수집기의
    행만 창에 남고 활성 세션이 전부 밀려났다(1005행 중 상위 100 = 100% unknown).
  - KPI가 잘린 배열에서 파생돼 "전체 100"(실제 1005)·"정상"(실제 action 632)을
    단언했다. 절단 사실은 어느 경계에서도 신호되지 않았다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import acp.web.app as webapp
from acp.models import SessionRecord, SessionState
from acp.store import SessionStore
from acp.web.app import EventBroadcaster, app, init_app

NOW = datetime(2026, 7, 27, 8, 30, tzinfo=timezone.utc)


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


def _seed_write_order_trap(store: SessionStore) -> None:
    """실제 결함 재현: 활성 세션을 **먼저** 쓰고 비활성 세션을 **나중에** 쓴다.

    폴러가 수집기를 순회하며 upsert하므로 `updated_at`은 쓰기 순서를 따른다.
    `updated_at` 정렬 + LIMIT은 나중에 쓰인 비활성 행만 남긴다.
    """
    store.upsert_session(
        _record("live-1", "codex", last_activity=NOW),
        SessionState.LIVE,
    )
    store.upsert_session(
        _record("holding-1", "codex", last_activity=NOW - timedelta(minutes=20)),
        SessionState.HOLDING,
    )
    for i in range(5):
        store.upsert_session(
            _record(f"old-{i}", "claude", last_activity=NOW - timedelta(days=30 + i)),
            SessionState.UNKNOWN,
        )


def test_list_sessions_orders_by_activity_not_write_order(tmp_store):
    """창이 잘려도 **활성 세션이 먼저** 남는다(정렬키가 last_activity)."""
    _seed_write_order_trap(tmp_store)

    window = tmp_store.list_sessions(limit=2)

    assert [row["session_id"] for row in window] == ["codex:live-1", "codex:holding-1"]


def test_list_sessions_tie_break_is_deterministic(tmp_store):
    """동률 last_activity에서도 순서가 결정론적이어야 페이지네이션이 안전하다."""
    for sid in ("c", "a", "b"):
        tmp_store.upsert_session(
            _record(sid, "fake", last_activity=NOW), SessionState.LIVE
        )

    first = [row["session_id"] for row in tmp_store.list_sessions(limit=3)]
    second = [row["session_id"] for row in tmp_store.list_sessions(limit=3)]

    assert first == second == ["fake:a", "fake:b", "fake:c"]


def test_list_sessions_puts_null_activity_last(tmp_store):
    """last_activity 없는 행(정보 없음)이 활성 행을 밀어내지 않는다."""
    tmp_store.upsert_session(_record("no-activity"), SessionState.UNKNOWN)
    tmp_store.upsert_session(
        _record("has-activity", last_activity=NOW), SessionState.LIVE
    )

    window = tmp_store.list_sessions(limit=1)

    assert [row["session_id"] for row in window] == ["fake:has-activity"]


def test_session_summary_counts_full_table_not_window(tmp_store):
    """집계는 전량 기준 — 창 크기와 무관하다."""
    _seed_write_order_trap(tmp_store)

    summary = tmp_store.session_summary()

    assert summary["total"] == 7
    assert tmp_store.count_sessions() == 7
    assert summary["by_state"]["live"] == 1
    assert summary["by_state"]["holding"] == 1
    assert summary["by_state"]["unknown"] == 5
    assert summary["by_app"] == {"codex": 2, "claude": 5}


def _client(store: SessionStore) -> TestClient:
    init_app(store, EventBroadcaster(), poll_interval=15.0)
    return TestClient(app)


def test_api_sessions_reports_true_total_and_truncation(tmp_store):
    """응답이 창·총계·절단 여부를 함께 싣는다(거짓 총계 방지)."""
    _seed_write_order_trap(tmp_store)
    client = _client(tmp_store)

    payload = client.get("/api/sessions", params={"limit": 2}).json()

    assert payload["returned"] == 2
    assert payload["total"] == 7
    assert payload["truncated"] is True
    assert payload["limit"] == 2
    assert len(payload["items"]) == 2
    # 잘린 창에도 불구하고 집계는 전량 기준
    assert payload["summary"]["total"] == 7
    assert payload["summary"]["action_required"] == 1  # holding 1


def test_api_sessions_not_truncated_when_window_covers_all(tmp_store):
    _seed_write_order_trap(tmp_store)
    client = _client(tmp_store)

    payload = client.get("/api/sessions", params={"limit": 100}).json()

    assert payload["truncated"] is False
    assert payload["returned"] == payload["total"] == 7


def test_api_sessions_grades_survive_truncation(tmp_store):
    """창 밖 세션도 집계에 잡히고, **두 축이 분리**돼 보고된다(T14 S2 D4).

    과거에는 창 안 action=0이면 "정상"이라 단언했고, 이후에는 stale까지 action에
    섞여 "확인 필요"의 대부분이 죽은 세션이었다.
    """
    tmp_store.upsert_session(
        _record("recent", last_activity=NOW), SessionState.LIVE
    )
    for i in range(3):
        tmp_store.upsert_session(
            _record(f"stale-{i}", last_activity=NOW - timedelta(days=5 + i)),
            SessionState.STALE,
        )
    tmp_store.upsert_session(
        _record("holding-1", last_activity=NOW - timedelta(minutes=30)),
        SessionState.HOLDING,
    )
    client = _client(tmp_store)

    payload = client.get("/api/sessions", params={"limit": 1}).json()
    summary = payload["summary"]

    assert payload["returned"] == 1
    assert [row["state"] for row in payload["items"]] == ["live"]
    assert summary["action_required"] == 1  # holding만
    assert summary["cleanup_required"] == 3  # stale
    assert set(summary["action_states"]) == {"holding", "error"}
    assert set(summary["cleanup_states"]) == {"stale"}


@pytest.mark.parametrize("bad_limit", [0, -1, -100])
def test_api_sessions_rejects_non_positive_limit(tmp_store, bad_limit):
    """`limit=-1`이 전량 조회 뒷문으로 쓰이던 경로를 닫는다(SQLite는 음수를 무제한으로 읽음)."""
    client = _client(tmp_store)

    assert client.get("/api/sessions", params={"limit": bad_limit}).status_code == 422


def test_api_sessions_rejects_oversized_limit(tmp_store):
    client = _client(tmp_store)

    assert client.get("/api/sessions", params={"limit": 10**9}).status_code == 422


def _seed_reachability_trap(store: SessionStore) -> None:
    """활성 다수 + 조치 대상 소수, 단 조치 대상은 **활동이 오래된** 배치.

    `last_activity` 정렬은 stale/holding/error와 반상관이라, 창을 활동순으로만
    자르면 조치 대상이 구조적으로 창 밖에 남는다.
    """
    for i in range(10):
        store.upsert_session(
            _record(f"live-{i}", "codex", last_activity=NOW - timedelta(seconds=i)),
            SessionState.LIVE,
        )
    store.upsert_session(
        _record("dead-1", "claude", last_activity=NOW - timedelta(days=30)),
        SessionState.ERROR,
    )
    store.upsert_session(
        _record("dead-2", "claude", last_activity=NOW - timedelta(days=31)),
        SessionState.STALE,
    )


def test_action_sessions_are_reachable_through_server_filter(tmp_store):
    """KPI가 세는 조치 대상에 **실제로 도달**할 수 있어야 한다.

    창을 활동순으로만 자르면 error/stale은 항상 창 밖이라, 총계만 정직해지고
    사용자는 그 세션을 열람할 방법이 없다(리뷰 P1). 필터는 서버에서 전량에 건다.
    """
    _seed_reachability_trap(tmp_store)

    # 필터 없는 활동순 창(2건)에는 조치 대상이 없다 — 이게 정렬키의 성질이다.
    unfiltered = tmp_store.sessions_view(limit=2)
    assert all(row["state"] == "live" for row in unfiltered["items"])

    # 같은 창 크기라도 서버 필터를 걸면 조치 대상에 도달한다.
    filtered = tmp_store.sessions_view(limit=2, states=["error", "stale"])

    assert [row["session_id"] for row in filtered["items"]] == ["claude:dead-1", "claude:dead-2"]
    assert filtered["matched"] == 2
    assert filtered["filtered"] is True
    assert filtered["truncated"] is False
    # 집계는 필터와 무관하게 전량 기준이어야 KPI가 흔들리지 않는다.
    assert filtered["summary"]["total"] == 12
    assert filtered["total"] == 12


def test_app_filter_reaches_apps_outside_activity_window(tmp_store):
    """창 밖 앱도 필터로 도달 가능해야 한다(창에만 있는 앱을 옵션으로 주면 안 됨)."""
    _seed_reachability_trap(tmp_store)

    view = tmp_store.sessions_view(limit=2, apps=["claude"])

    assert {row["app"] for row in view["items"]} == {"claude"}
    assert view["matched"] == 2
    assert view["summary"]["by_app"] == {"codex": 10, "claude": 2}


def test_matched_is_filter_scoped_while_total_stays_full(tmp_store):
    """`matched`(조건 일치)와 `total`(전량)이 분리돼 절단 고지가 정확해진다."""
    _seed_reachability_trap(tmp_store)

    view = tmp_store.sessions_view(limit=1, states=["live"])

    assert view["returned"] == 1
    assert view["matched"] == 10
    assert view["total"] == 12
    assert view["truncated"] is True


def test_api_rejects_unknown_state_filter(tmp_store):
    """알 수 없는 상태를 조용히 무시하면 '필터를 걸었는데 전량'이 된다."""
    client = _client(tmp_store)

    assert client.get("/api/sessions", params={"states": "bogus"}).status_code == 422
    assert client.get("/api/sessions", params={"states": ["live", "nope"]}).status_code == 422
    assert client.get("/api/sessions", params={"states": "live"}).status_code == 200


def test_api_exposes_action_states_for_client_parity(tmp_store):
    """행동필요 상태 목록을 응답에 실어 클라가 따로 정의하지 않게 한다(드리프트 방지)."""
    _seed_reachability_trap(tmp_store)
    client = _client(tmp_store)

    payload = client.get("/api/sessions").json()
    summary = payload["summary"]
    action_states = summary["action_states"]
    cleanup_states = summary["cleanup_states"]

    assert set(action_states) == {"holding", "error"}
    assert set(cleanup_states) == {"stale"}
    # 각 축의 목록으로 서버 필터를 걸면 그 축의 집계와 일치해야 한다.
    by_action = client.get("/api/sessions", params={"states": action_states}).json()
    assert by_action["matched"] == summary["action_required"] == 1  # dead-1(error)
    by_cleanup = client.get("/api/sessions", params={"states": cleanup_states}).json()
    assert by_cleanup["matched"] == summary["cleanup_required"] == 1  # dead-2(stale)
    # **두 축 모두 서버 필터로 도달 가능해야 한다** — 한쪽을 빼면 그 세션들이
    # 화면에서 닿지 않는다(Slice 1의 도달 가능성 회귀 방지).
    both = client.get(
        "/api/sessions", params={"states": [*action_states, *cleanup_states]}
    ).json()
    assert both["matched"] == 2


def test_sessions_view_is_internally_consistent(tmp_store):
    """봉투 안의 수치들이 서로 모순되지 않는다(같은 스냅샷에서 나왔다)."""
    _seed_write_order_trap(tmp_store)

    view = tmp_store.sessions_view(limit=3)

    assert sum(view["summary"]["by_state"].values()) == view["total"]
    assert sum(view["summary"]["by_app"].values()) == view["total"]
    assert view["returned"] == len(view["items"]) == 3
    assert view["truncated"] is (view["returned"] < view["total"])


def test_sessions_view_window_and_summary_share_one_snapshot(tmp_path, monkeypatch):
    """창 조회와 집계 조회 **사이에** 쓰기가 끼어도 봉투가 한 시점을 나타낸다.

    창 읽기 직후 다른 연결이 기존 행의 state를 바꾸도록 강제 주입한다.
    단일 read 스냅샷이면 집계는 그 변경을 보지 못하고, 창의 상태 분포와
    `by_state`가 정확히 일치한다. 창·집계를 각각 autocommit으로 조회하는
    구현으로 되돌리면 집계만 변경을 보게 되어 이 동치가 깨진다.
    """
    from collections import Counter

    db = str(tmp_path / "snap.db")
    log = str(tmp_path / "snap.jsonl")
    store = SessionStore(db, log)
    writer = SessionStore(db, log)  # 폴러 역할 — 별도 연결
    try:
        for i in range(4):
            store.upsert_session(
                _record(f"s{i}", last_activity=NOW - timedelta(minutes=i)),
                SessionState.LIVE,
            )

        original = SessionStore.list_sessions
        interleaved: list[bool] = []

        def list_then_mutate(
            self, limit=100, *, states=None, apps=None, archived="include", conn=None
        ):
            rows = original(
                self, limit, states=states, apps=apps, archived=archived, conn=conn
            )
            if not interleaved:  # 창 읽기 직후 정확히 한 번만 끼어든다
                interleaved.append(True)
                writer.upsert_session(
                    _record("s0", last_activity=NOW), SessionState.ERROR
                )
            return rows

        monkeypatch.setattr(SessionStore, "list_sessions", list_then_mutate)

        # 창이 전량을 덮으므로, 일관 스냅샷이면 창 분포 == 집계 분포
        view = store.sessions_view(limit=10)

        assert interleaved == [True], "쓰기 주입이 실제로 일어나야 테스트가 유효하다"
        assert view["returned"] == view["total"] == 4
        assert dict(Counter(row["state"] for row in view["items"])) == view["summary"]["by_state"]
        assert view["summary"]["by_state"] == {"live": 4}
    finally:
        writer.close()
        store.close()


def test_dashboard_page_ships_session_meta(tmp_store):
    """초기 렌더도 메타를 실어야 첫 페인트 KPI가 거짓말하지 않는다."""
    _seed_write_order_trap(tmp_store)
    client = _client(tmp_store)

    body = client.get("/").text

    assert 'id="initial-session-meta"' in body
    assert '"total": 7' in body or '"total":7' in body
