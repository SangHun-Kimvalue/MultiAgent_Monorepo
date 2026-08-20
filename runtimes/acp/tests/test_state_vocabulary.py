"""tests/test_state_vocabulary.py — 상태 어휘 단일 출처 게이트(T14 S4c-2).

잠그는 결함:
  - 어휘가 클라 리터럴 배열에 있어, 서버와 어긋나면 어휘 밖 상태가 **고지 없이 증발**하고
    필터로도 고를 수 없었다(도달 불가 — LESSON-002 rule 8 회귀).
  - 필터 수용 어휘가 계약 enum뿐이라, **저장소에 실제로 있는 상태**로 거르는 질의가
    422로 막혔다. 답할 수 있는 질의를 오류라고 부른 것이다.
  - 422가 산문 `detail`만 줘서, 소비자가 어떤 값이 거절됐는지 알려면 **문장을 파싱**해야
    했다(R5 위반을 소비자에게 강요).

각 검사는 설계 §4의 V번호와 대응한다. 되돌리면 실패하는지를 mutation으로 실측했다.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from acp.models import SessionRecord, SessionState, state_vocabulary
from acp.store import SessionStore, UnknownStateFilter
from acp.web.app import STATE_DISPLAY_ORDER, EventBroadcaster, app, init_app


@pytest.fixture()
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(str(tmp_path / "acp.db"))


@pytest.fixture()
def client(store: SessionStore):
    init_app(store, EventBroadcaster(), poll_interval=15.0)
    with TestClient(app) as test_client:
        yield test_client


def _put(store: SessionStore, session_id: str, state: str, *, archived: bool = False) -> None:
    """임의 상태 문자열로 행을 만든다.

    `SessionState`는 enum이라 계약 밖 상태를 정상 경로로는 만들 수 없다 — 그래서
    **저장 후 직접 갱신**한다. 이 트랙이 다루는 것은 "enum이 바뀐 뒤 남은 옛 행"이나
    "다른 버전이 쓴 행"처럼 **어휘와 데이터가 어긋난 실제 상황**이다.
    """
    store.upsert_session(
        SessionRecord(app="fake", session_id=session_id, source_file="test"),
        SessionState.UNKNOWN,
    )
    with store._conn:
        store._conn.execute(
            "UPDATE sessions SET state = ?, archive_observed_at = ? WHERE session_id = ?",
            (state, "2026-07-28T00:00:00+00:00" if archived else None, f"fake:{session_id}"),
        )


# ── V1 · V2 — 어휘 단일 출처와 순열 불변 ──────────────────────────────


def test_v1_vocabulary_has_one_source() -> None:
    """어휘는 `SessionState`에서만 나온다. 두 곳에서 만들면 드리프트한다."""
    assert state_vocabulary() == tuple(state.value for state in SessionState)


def test_v1b_response_carries_vocabulary_and_order(client, store: SessionStore) -> None:
    _put(store, "s1", "live")
    summary = client.get("/api/sessions").json()["summary"]
    assert summary["state_vocabulary"] == list(state_vocabulary())
    assert summary["state_display_order"] == list(STATE_DISPLAY_ORDER)
    # 세 축은 다른 사실이다 — 같은 값이어도 각자 실린다.
    assert "observed_states" in summary


def test_v2_display_order_is_a_permutation_of_the_vocabulary() -> None:
    """현재 값이 순열이다(사실 확인)."""
    assert sorted(STATE_DISPLAY_ORDER) == sorted(state_vocabulary())
    assert len(STATE_DISPLAY_ORDER) == len(set(STATE_DISPLAY_ORDER))


def test_v2b_drifted_display_order_fails_at_import(monkeypatch) -> None:
    """어휘가 늘었는데 순서가 그대로면 **기동이 실패**한다.

    앞의 검사는 "현재 값이 같다"만 말하므로, 기동 검사를 지워도 통과한다
    (구현리뷰 R1 P2 — 판별력이 없었다). 여기서는 어휘를 실제로 드리프트시킨 뒤
    모듈을 **다시 임포트**해 그 검사가 실제로 발화하는지 본다.

    mutation: `app.py`의 순열 검사를 지우면 reload가 성공해 → 실패.
    """
    import importlib

    import acp.web.app as webapp

    monkeypatch.setattr(
        "acp.models.state_vocabulary", lambda: (*state_vocabulary(), "brand_new_state")
    )
    with pytest.raises(RuntimeError, match="순열"):
        importlib.reload(webapp)
    # 다른 테스트가 쓰는 모듈 상태를 원래대로 되돌린다(어휘 패치는 fixture가 해제).
    monkeypatch.undo()
    importlib.reload(webapp)


# ── V5 계열 — 관측된 상태는 **도달 가능**해야 한다 ────────────────────


def test_v5_observed_state_is_filterable(client, store: SessionStore) -> None:
    """어휘 밖이어도 **저장소에 있으면** 거를 수 있다.

    저장소에 있는 값으로 거르는 질의는 답할 수 있는 질의다. 422로 막으면
    그 행들은 표시 창 밖일 때 **열람 자체가 불가능**해진다.
    """
    _put(store, "q1", "quarantined")
    payload = client.get("/api/sessions").json()
    assert "quarantined" in payload["summary"]["observed_states"]

    filtered = client.get("/api/sessions?states=quarantined")
    assert filtered.status_code == 200
    assert [item["session_id"] for item in filtered.json()["items"]] == ["fake:q1"]


def test_v5b_state_only_on_archived_rows_is_filterable(client, store: SessionStore) -> None:
    """**보관 행에만** 있는 상태도 도달 가능해야 한다.

    S4a가 분포를 둘로 나눈 뒤 `by_state`는 보관 미관측 행만 뜻한다. 거기서만 관측
    어휘를 유도하면, 기본 범위(`archived=include`)로 조회하면 나오는 행을 그 상태로는
    못 거르는 모순이 생긴다.
    """
    _put(store, "a1", "quarantined", archived=True)
    summary = client.get("/api/sessions").json()["summary"]
    assert "quarantined" not in summary["by_state"]
    assert "quarantined" in summary["archived_by_last_known_state"]
    assert "quarantined" in summary["observed_states"]

    filtered = client.get("/api/sessions?states=quarantined")
    assert filtered.status_code == 200
    assert [item["session_id"] for item in filtered.json()["items"]] == ["fake:a1"]


@pytest.mark.parametrize("scope", ["include", "exclude", "only"])
def test_v5c_acceptance_does_not_depend_on_archived_scope(
    client, store: SessionStore, scope: str
) -> None:
    """같은 값이 `archived` scope에 따라 422가 되기도 200이 되기도 하면 안 된다.

    빈 결과는 오류가 아니다 — 조건에 맞는 행이 없다는 **사실**이며 `matched`가 말한다.
    """
    _put(store, "a1", "quarantined", archived=True)
    response = client.get(f"/api/sessions?states=quarantined&archived={scope}")
    assert response.status_code == 200
    assert response.json()["matched"] == (0 if scope == "exclude" else 1)


# ── V6 — 진짜 미지 값은 구조화 422로 거절 ────────────────────────────


def test_v6_unknown_value_is_rejected_with_structured_detail(client, store: SessionStore) -> None:
    """계약에도 데이터에도 없는 값만 거절한다. 그리고 **구조로** 말한다.

    산문만 주면 소비자가 어떤 값이 거절됐는지 알려면 문장을 파싱해야 한다(R5 위반).
    """
    _put(store, "s1", "live")
    response = client.get("/api/sessions?states=quarantnied")  # 오타
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "unknown_state_filter"
    assert detail["invalid_values"] == ["quarantnied"]
    # 수용 집합도 함께 준다 — 소비자가 무엇을 고를 수 있는지 알아야 회복한다.
    assert set(state_vocabulary()) <= set(detail["accepted"])
    assert "live" in detail["accepted"]


def test_v6b_mixed_valid_and_invalid_reports_only_the_invalid(
    client, store: SessionStore
) -> None:
    _put(store, "s1", "live")
    response = client.get("/api/sessions?states=live&states=nope")
    assert response.status_code == 422
    assert response.json()["detail"]["invalid_values"] == ["nope"]


def test_v6c_store_raises_domain_error_not_http(store: SessionStore) -> None:
    """저장소는 HTTP를 모른다 — 도메인 예외만 올리고 상태 코드는 웹 계층이 정한다(D7)."""
    with pytest.raises(UnknownStateFilter) as excinfo:
        store.sessions_view(limit=10, states=["nope"])
    assert excinfo.value.invalid_values == ["nope"]
    assert set(state_vocabulary()) <= set(excinfo.value.accepted)


# ── V14 — 판정과 응답은 같은 스냅샷 ──────────────────────────────────


def test_v14_acceptance_and_response_share_one_snapshot(store: SessionStore) -> None:
    """수용 판정에 쓰인 관측 집합이 **응답에 실린 것과 같다**.

    검증을 트랜잭션 밖에서 하면 "수용했는데 응답 분포엔 없는 상태"(또는 그 반대)가
    생겨, 봉투가 한 시점을 나타낸다는 S1의 주장이 거짓이 된다.
    """
    _put(store, "s1", "quarantined")
    payload = store.sessions_view(limit=10, states=["quarantined"])
    observed = payload["summary"]["observed_states"]
    # 수용된 값은 응답이 말하는 관측 집합 안에 있다.
    assert "quarantined" in observed
    # 그 관측 집합은 같은 스냅샷의 두 분포에서 나왔다(추가 조회 아님).
    merged = set(payload["summary"]["by_state"]) | set(
        payload["summary"]["archived_by_last_known_state"]
    )
    assert set(observed) == merged


def test_v14b_acceptance_queries_stay_inside_the_snapshot(
    store: SessionStore, monkeypatch
) -> None:
    """수용 판정에 쓰이는 조회가 **스냅샷 트랜잭션 밖으로 새지 않는다**.

    두 가지를 함께 잠근다:
      1. 쓰기 연결(`self._conn`)은 `sessions_view` 동안 **아무 것도 실행하지 않는다**.
         거기서 관측 집합을 구하면 판정과 응답이 다른 시점을 본다(D7 위반).
      2. 읽기 연결에도 별도 `DISTINCT state` 스캔이 없다 — 이미 계산된 두 분포에서
         유도하므로 추가 스캔이 필요 없다(V14b 원래 의도).

    처음 쓴 검사는 **읽기 연결만** 추적해서, 쓰기 연결로 새는 mutation을 통과시켰다.
    그 사실을 실측으로 확인하고 게이트를 넓혔다.
    """
    _put(store, "s1", "quarantined")
    read_sql: list[str] = []
    write_sql: list[str] = []
    real_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):  # type: ignore[no-untyped-def]
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(lambda sql: read_sql.append(" ".join(str(sql).split())))
        return conn

    # `sessions_view`는 자기 read 연결을 직접 연다 — 그 연결에 trace를 걸어야 보인다.
    monkeypatch.setattr("acp.store.sqlite3.connect", traced_connect)
    # 이미 열려 있는 쓰기 연결은 별도로 건다(patch 이전에 생성됐다).
    store._conn.set_trace_callback(lambda sql: write_sql.append(" ".join(str(sql).split())))
    try:
        store.sessions_view(limit=10)
    finally:
        store._conn.set_trace_callback(None)

    assert read_sql, "조회 SQL을 하나도 관측하지 못했다(추적이 깨졌다)"
    assert not write_sql, f"스냅샷 밖(쓰기 연결)에서 조회했다: {write_sql}"
    assert not [sql for sql in read_sql if "DISTINCT state" in sql], read_sql


def test_v14c_matched_zero_is_not_an_error(client, store: SessionStore) -> None:
    """조건에 맞는 행이 0건인 것은 **정직한 빈 결과**다(오류가 아니다)."""
    _put(store, "s1", "live")
    response = client.get("/api/sessions?states=done")
    assert response.status_code == 200
    assert response.json()["matched"] == 0


@pytest.mark.asyncio
async def test_v15_out_of_vocabulary_stored_state_does_not_block_the_record(
    store: SessionStore,
) -> None:
    """어휘 밖 **이전 상태**가 그 레코드의 처리를 막지 않는다(라이브에서 잡은 결함).

    `SessionState(prev["state"])`가 `ValueError`를 내면 폴러의 레코드 루프가 중단돼
    그 세션은 매 틱 갱신에 실패하고 영원히 옛 상태로 남는다. 이 슬라이스가 어휘 밖
    상태를 **도달 가능**하게 만든 이상, 소비자도 그 값을 견뎌야 한다.

    mutation: `_parse_state`를 `SessionState(...)`로 되돌리면 상태가 갱신되지 않아 실패.
    """
    from datetime import datetime, timezone

    from acp.collectors.base import (
        CAPABILITY_SUPPORTED,
        PROCESS_SIGNAL_OK,
        BaseCollector,
        CollectCycle,
    )
    from acp.config import AppConfig, LivenessConfig, NotifyConfig
    from acp.poller import Poller

    _put(store, "s1", "retired")

    record = SessionRecord(
        app="fake",
        session_id="s1",
        source_file="test",
        last_activity=datetime.now(timezone.utc),
    )

    class _Collector(BaseCollector):
        def __init__(self) -> None:
            self._cycle = CollectCycle(
                app="fake",
                process_signal_capability=CAPABILITY_SUPPORTED,
                process_signal=PROCESS_SIGNAL_OK,
            )
            self._cycle.declare(collected=1, failed=0)

        @property
        def app_name(self) -> str:
            return "fake"

        @property
        def last_cycle(self) -> CollectCycle:
            return self._cycle

        def collect(self) -> list[SessionRecord]:
            return [record]

    class _Broadcaster:
        def __init__(self) -> None:
            self.events: list[dict] = []

        async def publish(self, event: dict) -> None:
            self.events.append(event)

    cfg = AppConfig(
        poll_interval=1,
        liveness=LivenessConfig(idle_threshold=10, hold_threshold=20, stale_ttl=40),
        notify=NotifyConfig(toast_enabled=False, webhook_url="", notify_cooldown=3600),
    )
    poller = Poller(store, cfg, _Broadcaster())
    poller.register(_Collector())
    await poller._tick()

    # 처리가 중단되지 않았다면 상태가 새로 판정돼 계약 어휘 안으로 들어온다.
    assert store.get_session("fake:s1")["state"] == SessionState.LIVE


@pytest.mark.asyncio
async def test_v16_archived_row_keeps_its_out_of_vocabulary_state(store: SessionStore) -> None:
    """보관 행의 **마지막으로 알던 상태**는 계약 어휘 밖이어도 그대로 보존된다.

    보존은 원문 그대로 쓸 때만 보존이다(구현리뷰 R1 P1). enum으로 강제하면 `unknown`
    으로 덮어써서 알던 사실이 사라지고, 그 상태는 `observed_states`에서도 빠져
    **도달 불가**가 된다 — 이 슬라이스가 닫은 결함의 재발이다.

    mutation: 보관 분기를 `prev_state ... else UNKNOWN`(enum)으로 되돌리면 실패.
    """
    from datetime import datetime, timezone

    from acp.collectors.base import (
        CAPABILITY_SUPPORTED,
        PROCESS_SIGNAL_OK,
        BaseCollector,
        CollectCycle,
    )
    from acp.config import AppConfig, LivenessConfig, NotifyConfig
    from acp.poller import Poller

    _put(store, "arch", "retired", archived=True)
    record = SessionRecord(
        app="fake",
        session_id="arch",
        source_file="test",
        last_activity=datetime.now(timezone.utc),
        archived=True,
    )

    class _Collector(BaseCollector):
        def __init__(self) -> None:
            self._cycle = CollectCycle(
                app="fake",
                process_signal_capability=CAPABILITY_SUPPORTED,
                process_signal=PROCESS_SIGNAL_OK,
            )
            self._cycle.declare(collected=1, failed=0)

        @property
        def app_name(self) -> str:
            return "fake"

        @property
        def last_cycle(self) -> CollectCycle:
            return self._cycle

        def collect(self) -> list[SessionRecord]:
            return [record]

    class _Broadcaster:
        def __init__(self) -> None:
            self.events: list[dict] = []

        async def publish(self, event: dict) -> None:
            self.events.append(event)

    cfg = AppConfig(
        poll_interval=1,
        liveness=LivenessConfig(idle_threshold=10, hold_threshold=20, stale_ttl=40),
        notify=NotifyConfig(toast_enabled=False, webhook_url="", notify_cooldown=3600),
    )
    poller = Poller(store, cfg, _Broadcaster())
    poller.register(_Collector())
    await poller._tick()

    assert store.get_session("fake:arch")["state"] == "retired"
    # 보존됐으므로 여전히 관측 어휘에 있고, 그 상태로 도달할 수 있다.
    payload = store.sessions_view(limit=10, states=["retired"])
    assert "retired" in payload["summary"]["observed_states"]
    assert payload["matched"] == 1


@pytest.mark.asyncio
async def test_v17_transition_from_out_of_vocabulary_state_keeps_the_fact(
    store: SessionStore,
) -> None:
    """어휘 밖 **이전 상태**에서의 전이가 `from: null`로 뭉개지지 않는다.

    저장소에는 그 문자열이 남아 있는데 이벤트가 "이전 상태 없음"이라 적으면, 방금
    `upsert_session` 확장으로 보존한 사실을 이벤트·알림 경로에서 다시 잃는다
    (구현리뷰 R2 P1). 알림 게이트도 "이전 상태가 있었다"는 사실을 기준으로 삼아야
    명시적 오류 전이가 조용히 사라지지 않는다.

    mutation: `from`을 `prev_state.value if prev_state else None`으로 되돌리면 실패.
    """
    from datetime import datetime, timezone

    from acp.collectors.base import (
        CAPABILITY_SUPPORTED,
        PROCESS_SIGNAL_OK,
        BaseCollector,
        CollectCycle,
    )
    from acp.config import AppConfig, LivenessConfig, NotifyConfig
    from acp.poller import Poller

    _put(store, "s1", "retired")
    record = SessionRecord(
        app="fake",
        session_id="s1",
        source_file="test",
        last_activity=datetime.now(timezone.utc),
        last_event="error",
    )

    class _Collector(BaseCollector):
        def __init__(self) -> None:
            self._cycle = CollectCycle(
                app="fake",
                process_signal_capability=CAPABILITY_SUPPORTED,
                process_signal=PROCESS_SIGNAL_OK,
            )
            self._cycle.declare(collected=1, failed=0)

        @property
        def app_name(self) -> str:
            return "fake"

        @property
        def last_cycle(self) -> CollectCycle:
            return self._cycle

        def collect(self) -> list[SessionRecord]:
            return [record]

    class _Broadcaster:
        def __init__(self) -> None:
            self.events: list[dict] = []

        async def publish(self, event: dict) -> None:
            self.events.append(event)

    class _Notifier:
        def __init__(self) -> None:
            self.events: list = []

        def notify(self, event) -> None:  # type: ignore[no-untyped-def]
            self.events.append(event)

    cfg = AppConfig(
        poll_interval=1,
        liveness=LivenessConfig(idle_threshold=10, hold_threshold=20, stale_ttl=40),
        notify=NotifyConfig(toast_enabled=False, webhook_url="", notify_cooldown=3600),
    )
    notifier = _Notifier()
    poller = Poller(store, cfg, _Broadcaster(), notifier=notifier)
    poller.register(_Collector())
    await poller._tick()

    assert store.get_session("fake:s1")["state"] == SessionState.ERROR
    changes = [
        event
        for event in store.list_events(limit=20, event_type="state_change")
        if event["session_id"] == "fake:s1"
    ]
    assert changes, "전이 이벤트가 없다"
    payload = changes[0]["payload"]
    assert payload["from"] == "retired", payload
    assert payload["to"] == "error"
    # 이전 상태가 **있었다는 사실**이 기준이므로 명시적 오류 전이는 알린다.
    assert [event.to_state for event in notifier.events] == [SessionState.ERROR]
    assert notifier.events[0].from_state == "retired"


def test_v14d_empty_store_still_accepts_contract_vocabulary(client) -> None:
    """행이 하나도 없어도 계약 어휘는 수용한다 — 데이터 유무가 어휘를 좁히지 않는다."""
    response = client.get("/api/sessions?states=live")
    assert response.status_code == 200
    assert response.json()["summary"]["observed_states"] == []
