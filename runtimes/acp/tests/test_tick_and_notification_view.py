"""tests/test_tick_and_notification_view.py — 알림 표시·틱 비차단 게이트(T14 S6).

잠그는 결함:
  - "최근 알림"이 전부 `UNKNOWN`/`-` — 알림이 없어서가 아니라 **읽는 위치가 달랐다**
    (저장 행은 표시 필드를 `payload` 안에 두는데 화면은 최상위에서 읽는다).
  - 틱이 15초마다 3.7~4.7초씩 **이벤트 루프를 막았다**(수집·판정·쓰기가 전부 동기).
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from acp.collectors.base import (
    CAPABILITY_SUPPORTED,
    PROCESS_SIGNAL_OK,
    BaseCollector,
    CollectCycle,
)
from acp.config import AppConfig, LivenessConfig, NotifyConfig
from acp.models import SessionRecord, SessionState
from acp.notify import StateTransitionEvent
from acp.notify_view import notification_event, notification_payload, notification_view
from acp.poller import Poller
from acp.store import SessionStore


@pytest.fixture()
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(str(tmp_path / "acp.db"), str(tmp_path / "events.jsonl"))


class _Broadcaster:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def publish(self, event: dict) -> None:
        self.events.append(event)


def _cfg() -> AppConfig:
    return AppConfig(
        poll_interval=1,
        liveness=LivenessConfig(idle_threshold=10, hold_threshold=20, stale_ttl=40),
        notify=NotifyConfig(toast_enabled=False, webhook_url="", notify_cooldown=0),
    )


class _Collector(BaseCollector):
    """테스트가 레코드와 **동기 지연**을 지정하는 수집기."""

    def __init__(self, records: list[SessionRecord], *, delay: float = 0.0) -> None:
        self.records = records
        self._delay = delay

    @property
    def app_name(self) -> str:
        return "fake"

    @property
    def last_cycle(self) -> CollectCycle:
        cycle = CollectCycle(
            app="fake",
            process_signal_capability=CAPABILITY_SUPPORTED,
            process_signal=PROCESS_SIGNAL_OK,
        )
        cycle.declare(collected=len(self.records), failed=0)
        return cycle

    def collect(self) -> list[SessionRecord]:
        if self._delay:
            time.sleep(self._delay)  # 동기 지연 — 루프를 막는지 보기 위한 주입
        return self.records


# ── V1 · V2 · V3 — 알림 표시 모양 ────────────────────────────────────


def test_v1_stored_notification_is_flattened_for_the_screen() -> None:
    """저장 행의 표시 필드가 **최상위**로 올라온다(화면이 읽는 위치)."""
    row = {
        "session_id": "claude:abc",
        "event_type": "notification_sent",
        "created_at": "2026-07-28T08:49:09+00:00",
        "payload": {"from": "idle", "to": "holding", "title": "홀딩 진입", "detail": "15분"},
    }
    view = notification_view(row)
    assert view["to"] == "holding"
    assert view["title"] == "홀딩 진입"
    assert view["detail"] == "15분"
    assert view["payload"] == row["payload"]  # 원본도 남는다(진단·기존 소비자)


def test_v2_sse_and_initial_load_share_one_shape() -> None:
    """SSE 발행 모양과 초기 로드 모양이 **같은 함수**에서 나온다.

    예전에는 SSE만 평평해서 한쪽 경로만 깨졌다 — 그래서 여태 드러나지 않았다.
    """
    event = StateTransitionEvent(
        session_id="claude:abc",
        native_session_id="abc",
        app="claude",
        project_path="C:/repo",
        from_state="idle",
        to_state=SessionState.HOLDING,
        created_at=datetime.now(timezone.utc),
    )
    payload = notification_payload(event)
    sse = notification_event("claude:abc", payload)
    stored = notification_view(
        {"session_id": "claude:abc", "created_at": "t", "payload": payload}
    )
    for key in ("to", "title", "detail", "app", "project_path", "native_session_id"):
        assert sse[key] == stored[key], key


def test_v2b_initial_load_route_actually_flattens(tmp_path: Path) -> None:
    """**실제 배선**을 통과시킨다(구현리뷰 P2).

    헬퍼끼리 비교하는 검사는 초기 로드 경로가 헬퍼 사용을 그만둬도 통과한다 —
    그건 "두 경로 한 계약" 게이트가 아니다. 여기서는 대시보드 라우트가 내려주는
    값을 직접 본다.
    """
    from fastapi.testclient import TestClient

    from acp.web.app import EventBroadcaster, app, init_app

    store = SessionStore(str(tmp_path / "acp.db"), str(tmp_path / "events.jsonl"))
    store.append_event(
        "claude:abc",
        "notification_sent",
        {"from": "idle", "to": "holding", "title": "세션 홀딩 감지", "detail": "d"},
    )
    init_app(store, EventBroadcaster(), poll_interval=15.0)
    with TestClient(app) as client:
        html = client.get("/").text
    marker = 'id="initial-notifications" type="application/json">'
    payload = html[html.index(marker) + len(marker):]
    payload = payload[: payload.index("</script>")]
    items = json.loads(payload)
    assert items and items[0]["to"] == "holding", items
    assert items[0]["title"] == "세션 홀딩 감지"


@pytest.mark.parametrize("payload", [None, "문자열", 42, ["a"]])
def test_v3_no_payload_means_no_invented_fields(payload: object) -> None:
    """`payload`가 dict가 아니면 표시 필드를 **만들지 않는다**(지어내지 않기)."""
    view = notification_view({"session_id": "s", "created_at": "t", "payload": payload})
    assert "to" not in view and "title" not in view
    assert view["session_id"] == "s"


def test_v3b_payload_cannot_overwrite_row_facts() -> None:
    """행의 사실을 payload가 덮어쓰지 못한다 — 누가 말한 사실인지가 다르다."""
    inner = {
        "session_id": "payload-lies",
        "created_at": "payload-time",
        # 원본 자체를 덮어쓰려는 값 — 진단용 원본이 사라지면 무엇이 저장돼 있었는지
        # 알 수 없게 된다(구현리뷰 P3).
        "payload": "덮어쓰기 시도",
        "to": "error",
    }
    view = notification_view({
        "session_id": "row-owns-this",
        "created_at": "row-time",
        "payload": inner,
    })
    assert view["session_id"] == "row-owns-this"
    assert view["created_at"] == "row-time"
    assert view["to"] == "error"
    assert view["payload"] == inner, "진단용 원본 payload가 덮였다"


# ── V4 — `synchronous` 범위 ──────────────────────────────────────────


def test_v4_only_the_session_connection_lowers_durability(store: SessionStore) -> None:
    """세션 전용 연결만 NORMAL이고 **주 연결은 FULL**이다.

    `synchronous`는 연결 단위 설정이다. 주 연결에는 승인 감사처럼 **재유도되지 않는**
    기록이 함께 흐르므로 그 내구성을 낮추면 안 된다.

    mutation: 주 연결에 NORMAL을 걸면 → 실패.
    """
    main = store._conn.execute("PRAGMA synchronous").fetchone()[0]
    session = store._session_conn.execute("PRAGMA synchronous").fetchone()[0]
    assert main == 2, f"주 연결이 FULL이 아니다: {main}"
    assert session == 1, f"세션 연결이 NORMAL이 아니다: {session}"


def test_v5_session_writes_are_fast(store: SessionStore) -> None:
    """세션 쓰기가 행당 밀리초 단위로 떨어진다(FULL이면 약 2ms/행이었다).

    측정 기준: 300행이 **300ms 미만**(실측 기준선 약 15ms). 환경 편차를 감안해
    느슨하게 잡되, FULL로 되돌리면(약 600ms) 넘어가도록 둔다.
    """
    records = [
        SessionRecord(app="fake", session_id=f"s{i}", source_file="t")
        for i in range(300)
    ]
    start = time.perf_counter()
    for record in records:
        store.upsert_session(record, SessionState.UNKNOWN)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.3, f"300행 쓰기가 {elapsed*1000:.0f}ms 걸렸다"


# ── V6 — 루프 비차단 ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_v6_tick_does_not_block_the_event_loop(store: SessionStore) -> None:
    """틱이 도는 동안 **다른 코루틴이 계속 돈다**.

    측정법(설계 §V6): `collect()`에서 0.5초 동기 지연을 주는 수집기를 등록하고,
    10ms 간격 heartbeat의 **최대 인접 간격**을 잰다. 동기로 되돌리면 그 간격이
    지연(0.5초) 이상으로 벌어진다.
    """
    poller = Poller(store, _cfg(), _Broadcaster())
    poller.register(
        _Collector(
            [SessionRecord(app="fake", session_id="s1", source_file="t")],
            delay=0.5,
        )
    )

    ticks: list[float] = []
    stop = False

    async def heartbeat() -> None:
        while not stop:
            ticks.append(time.perf_counter())
            await asyncio.sleep(0.01)

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)  # heartbeat를 먼저 띄워 틱과 겹치게 한다
    await poller._tick()
    # 차단이 끝난 **뒤에도 한 번 더 뛰게** 둔다. 곧바로 stop을 세우면 heartbeat가
    # 깨어나자마자 루프를 빠져나가 **큰 간격이 기록되지 않는다** — 처음 쓴 검사가
    # 그래서 동기 회귀를 못 잡았다(mutation으로 확인하고 고쳤다).
    await asyncio.sleep(0.05)
    stop = True
    await beat

    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert gaps, "heartbeat가 돌지 않았다(측정 실패)"
    assert max(gaps) < 0.2, f"루프가 {max(gaps)*1000:.0f}ms 막혔다"


# ── V7 — 발행 순서 ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_v7_publish_happens_after_the_row_is_stored(store: SessionStore) -> None:
    """SSE는 **그 행이 DB에 반영된 뒤** 나간다.

    발행이 먼저면 "화면엔 있는데 DB엔 없는" 구간이 생긴다.
    """
    seen: list[str | None] = []

    class _Checking(_Broadcaster):
        async def publish(self, event: dict) -> None:
            row = store.get_session("fake:s1")
            seen.append(row["state"] if row else None)
            await super().publish(event)

    record = SessionRecord(
        app="fake",
        session_id="s1",
        source_file="t",
        last_activity=datetime.now(timezone.utc),
    )
    poller = Poller(store, _cfg(), _Checking())
    poller.register(_Collector([record]))
    await poller._tick()

    assert seen and seen[0] == SessionState.LIVE, f"발행 시점의 DB 상태: {seen}"


@pytest.mark.asyncio
async def test_v7b_cycle_is_recorded_on_the_main_connection(store: SessionStore) -> None:
    """사이클 기록은 루프 단계(주 연결)에서 일어난다 — 소유권 불변.

    스레드 단계에서 부르면 주 연결을 두 스레드가 쓰게 된다(설계검토 P2).
    """
    seen_threads: set[int] = set()
    store._conn.set_trace_callback(lambda _sql: seen_threads.add(threading.get_ident()))
    try:
        poller = Poller(store, _cfg(), _Broadcaster())
        poller.register(
            _Collector([SessionRecord(app="fake", session_id="s1", source_file="t")])
        )
        await poller._tick()
    finally:
        store._conn.set_trace_callback(None)

    assert "fake" in store.latest_collector_cycles()
    # 존재 확인만으로는 스레드로 옮겨도 통과한다(구현리뷰 P2). 주 연결이 **어느
    # 스레드에서** 쓰였는지를 본다 — 워커에서 쓰면 소유권 불변이 깨진다.
    assert seen_threads, "주 연결 사용을 관측하지 못했다(추적이 깨졌다)"
    assert seen_threads == {threading.get_ident()}, f"주 연결이 워커에서 쓰였다: {seen_threads}"


@pytest.mark.asyncio
async def test_v8_persist_failure_is_not_reported_as_success(store: SessionStore) -> None:
    """세션 반영에 실패한 틱을 `success_complete`로 고지하지 않는다.

    전용 쓰기 연결을 도입하며 `busy_timeout` 초과 같은 새 실패 경로가 생겼다.
    수집기가 선언한 성공을 그대로 두면 **반영되지 않은 틱이 정상 관측으로 읽힌다**
    (구현리뷰 P1 — 내가 만든 회귀).
    """

    def boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    poller = Poller(store, _cfg(), _Broadcaster())
    poller.register(_Collector([SessionRecord(app="fake", session_id="s1", source_file="t")]))
    original = store.upsert_session
    store.upsert_session = boom  # type: ignore[method-assign]
    try:
        await poller._tick()
    finally:
        store.upsert_session = original  # type: ignore[method-assign]

    cycle = store.latest_collector_cycles()["fake"]
    assert cycle["status"] != "success_complete", cycle


def test_v7c_close_closes_both_connections(tmp_path: Path) -> None:
    """`close()`가 두 연결을 모두 닫는다 — 하나만 닫으면 파일 핸들이 남는다."""
    store = SessionStore(str(tmp_path / "acp.db"), str(tmp_path / "events.jsonl"))
    store.close()
    for name, conn in (("주", store._conn), ("세션", store._session_conn)):
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


def test_v7d_stale_record_still_derives(store: SessionStore) -> None:
    """스레드 이동 후에도 판정이 그대로다(무회귀)."""

    async def run() -> None:
        record = SessionRecord(
            app="fake",
            session_id="old",
            source_file="t",
            last_activity=datetime.now(timezone.utc) - timedelta(days=1),
            last_event="task_complete",
        )
        poller = Poller(store, _cfg(), _Broadcaster())
        poller.register(_Collector([record]))
        await poller._tick()

    asyncio.run(run())
    assert store.get_session("fake:old")["state"] == SessionState.STALE


# ── 종료 경로 — 워커가 도는 중에 연결을 닫지 않는다 (T14 S6 감사 P1) ──


@pytest.mark.asyncio
async def test_shutdown_waits_for_the_in_flight_tick(store: SessionStore) -> None:
    """정지 요청은 **진행 중인 틱을 끝까지 보낸다**.

    `task.cancel()`로는 부족하다 — 수집·쓰기는 `asyncio.to_thread` 워커에서 도는데
    태스크를 취소해도 그 스레드는 멈추지 않는다. 곧바로 연결을 닫으면 워커가 닫힌
    연결에 쓴다. 스레드로 옮기면서 만든 종료 경로의 실패 모드다.

    mutation: `run()`이 정지 신호를 보지 않고 `task.cancel()`에 의존하면 → 실패.
    """
    record = SessionRecord(
        app="fake",
        session_id="s1",
        source_file="t",
        last_activity=datetime.now(timezone.utc),
    )
    poller = Poller(store, _cfg(), _Broadcaster())
    poller.register(_Collector([record], delay=0.3))

    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.05)          # 틱이 시작되도록
    poller.request_stop()               # 워커가 도는 중에 정지 요청
    await asyncio.wait_for(task, timeout=5)

    # 정지 요청이 진행 중인 틱을 끊지 않았다 = 그 틱의 쓰기가 반영돼 있다.
    assert store.get_session("fake:s1") is not None


@pytest.mark.asyncio
async def test_stop_wakes_the_loop_without_waiting_a_full_interval(store: SessionStore) -> None:
    """정지 신호는 **즉시** 깨운다 — `sleep(poll_interval)`로 두면 종료가 그만큼 늦어진다.

    늦은 종료가 곧 `cancel()` 유혹이 되고, 그 취소가 위 레이스를 만든다.
    """
    cfg = _cfg()
    cfg.poll_interval = 30  # 이 값을 기다리면 테스트가 타임아웃한다
    poller = Poller(store, cfg, _Broadcaster())
    poller.register(_Collector([]))

    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.05)
    poller.request_stop()
    await asyncio.wait_for(task, timeout=3)   # 30초를 기다리면 여기서 실패
