"""tests/test_poller.py — Poller 레코드 단위 실패 격리 테스트."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from acp.collectors.base import (
    CAPABILITY_SUPPORTED,
    PROCESS_SIGNAL_OK,
    BaseCollector,
    CollectCycle,
)
from acp.collectors.orch_collector import OrchEventCollector
from acp.config import AppConfig, LivenessConfig, NotifyConfig
from acp.join import PhaseJoin
from acp.models import SessionRecord, SessionState
from acp.poller import Poller


def _observed_cycle() -> CollectCycle:
    """실행 신호를 **관측한** 사이클(T14 S4c-1 D1c).

    가용성 판정이 `== "ok"`로 좁혀진 뒤로는, 신호를 선언하지 않은 수집기의 세션은
    STALE로 확정되지 않는다(UNKNOWN). 여기서 다루려는 것은 상태 전이·알림이므로
    신호를 관측했다고 **명시 선언**한다 — 기본값에 기대면 무엇을 검사하는지 흐려진다.
    """
    return CollectCycle(
        app="fake",
        process_signal_capability=CAPABILITY_SUPPORTED,
        process_signal=PROCESS_SIGNAL_OK,
    )


class _Collector(BaseCollector):
    """T14 S4b: 수집기는 완결성을 **선언해야** 한다(추상 계약)."""

    def __init__(self) -> None:
        self._cycle = CollectCycle(app="fake")

    @property
    def app_name(self) -> str:
        return "fake"

    @property
    def last_cycle(self) -> CollectCycle:
        return self._cycle

    def collect(self) -> list[SessionRecord]:
        self._cycle = CollectCycle(app="fake")
        self._cycle.declare(collected=2, failed=0)
        return [
            SessionRecord(app="fake", session_id="bad", last_activity=datetime.now(timezone.utc), source_file="test"),
            SessionRecord(app="fake", session_id="ok", last_activity=datetime.now(timezone.utc), source_file="test"),
        ]


class _Broadcaster:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def publish(self, event: dict) -> None:
        self.events.append(event)


class _MutableCollector(BaseCollector):
    def __init__(self) -> None:
        self.record = _state_record("mutable", seconds_old=1)
        self._cycle = _observed_cycle()

    @property
    def app_name(self) -> str:
        return "fake"

    @property
    def last_cycle(self) -> CollectCycle:
        return self._cycle

    def collect(self) -> list[SessionRecord]:
        self._cycle = _observed_cycle()
        self._cycle.declare(collected=1, failed=0)
        return [self.record]


class _Notifier:
    def __init__(self) -> None:
        self.events: list = []

    def notify(self, event) -> None:
        self.events.append(event)


def _state_record(session_id: str, *, seconds_old: int) -> SessionRecord:
    return SessionRecord(
        app="fake",
        session_id=session_id,
        project_path="C:/repo",
        last_activity=datetime.now(timezone.utc) - timedelta(seconds=seconds_old),
        last_event="task_complete",
        source_file="test",
    )


@pytest.mark.asyncio
async def test_poller_continues_after_record_level_join_failure(tmp_store, monkeypatch):
    def fake_join(record: SessionRecord, _state):
        if record.session_id == "bad":
            raise RuntimeError("boom")
        return PhaseJoin(flag="no-phase-file")

    monkeypatch.setattr("acp.poller.join_phase", fake_join)
    cfg = AppConfig(liveness=LivenessConfig(idle_threshold=120, hold_threshold=300, stale_ttl=1800))
    broadcaster = _Broadcaster()
    poller = Poller(tmp_store, cfg, broadcaster)
    poller.register(_Collector())

    await poller._tick()

    assert tmp_store.get_session("fake:bad") is None
    assert tmp_store.get_session("fake:ok") is not None
    assert [event["session_id"] for event in broadcaster.events] == ["fake:ok"]


@pytest.mark.asyncio
async def test_poller_notifies_only_target_transitions_and_resets_after_recovery(tmp_store):
    cfg = AppConfig(
        liveness=LivenessConfig(idle_threshold=10, hold_threshold=20, stale_ttl=40),
        notify=NotifyConfig(toast_enabled=False, webhook_url="", notify_cooldown=3600),
    )
    broadcaster = _Broadcaster()
    notifier = _Notifier()
    collector = _MutableCollector()
    poller = Poller(tmp_store, cfg, broadcaster, notifier=notifier)
    poller.register(collector)

    await poller._tick()
    assert tmp_store.get_session("fake:mutable")["state"] == SessionState.LIVE
    assert notifier.events == []

    collector.record = _state_record("mutable", seconds_old=25)
    await poller._tick()
    assert [event.to_state for event in notifier.events] == [SessionState.HOLDING]

    await poller._tick()
    assert [event.to_state for event in notifier.events] == [SessionState.HOLDING]

    # STALE로 넘어가도 **알림은 추가되지 않는다** — 정리 대상(cleanup)이지 조치 대상이
    # 아니고, freshness 게이트(elapsed >= stale_ttl)에도 걸린다(T14 S2 D4·D7).
    collector.record = _state_record("mutable", seconds_old=45)
    await poller._tick()
    assert tmp_store.get_session("fake:mutable")["state"] == SessionState.STALE
    assert [event.to_state for event in notifier.events] == [SessionState.HOLDING]

    collector.record = _state_record("mutable", seconds_old=1)
    await poller._tick()
    assert tmp_store.get_session("fake:mutable")["last_notified_state"] is None

    collector.record = _state_record("mutable", seconds_old=25)
    await poller._tick()
    # 복귀 후 다시 HOLDING이 되면 새 사건이므로 알림이 하나 더 붙는다.
    # (중간의 STALE 전이는 알림 축이 아니므로 목록에 없다.)
    assert [event.to_state for event in notifier.events] == [
        SessionState.HOLDING,
        SessionState.HOLDING,
    ]

    notifications = tmp_store.list_events(event_type="notification_sent")
    assert [row["payload"]["to"] for row in reversed(notifications)] == ["holding", "holding"]
    assert [event["type"] for event in broadcaster.events if event["type"] == "notification"] == [
        "notification",
        "notification",
    ]


@pytest.mark.asyncio
async def test_poller_does_not_notify_on_initial_target_state(tmp_store):
    cfg = AppConfig(
        liveness=LivenessConfig(idle_threshold=10, hold_threshold=20, stale_ttl=40),
        notify=NotifyConfig(toast_enabled=False, webhook_url="", notify_cooldown=3600),
    )
    broadcaster = _Broadcaster()
    notifier = _Notifier()
    collector = _MutableCollector()
    collector.record = _state_record("initial-stale", seconds_old=45)
    poller = Poller(tmp_store, cfg, broadcaster, notifier=notifier)
    poller.register(collector)

    await poller._tick()

    assert tmp_store.get_session("fake:initial-stale")["state"] == SessionState.STALE
    assert notifier.events == []
    assert tmp_store.list_events(event_type="notification_sent") == []
    assert [event["type"] for event in broadcaster.events] == ["state_change"]


def _orch_cfg() -> AppConfig:
    return AppConfig(liveness=LivenessConfig(idle_threshold=120, hold_threshold=300, stale_ttl=1800))


def _write_orch(events_dir: Path, name: str, obj: object) -> None:
    events_dir.mkdir(parents=True, exist_ok=True)
    (events_dir / name).write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


@pytest.mark.asyncio
async def test_poller_collects_stores_and_broadcasts_orch_events(tmp_store, tmp_path):
    events_dir = tmp_path / "orch"
    _write_orch(events_dir, "01.json", {
        "schema_version": "orch/1.0", "project_id": "Demo", "phase_id": "P1",
        "type": "phase.verdict", "ts": "2026-06-14T12:00:00+00:00", "payload": {"status": "PASS"},
    })
    broadcaster = _Broadcaster()
    poller = Poller(tmp_store, _orch_cfg(), broadcaster)
    poller.register_orch_collector(OrchEventCollector(events_dir))

    await poller._tick()

    rows = tmp_store.list_orch_events()
    assert len(rows) == 1 and rows[0]["phase_id"] == "P1"
    orch = [e for e in broadcaster.events if e["type"] == "orch_event"]
    assert len(orch) == 1 and orch[0]["phase_id"] == "P1"

    # 두 번째 tick: 멱등 — store 1행 유지, 재broadcast 없음
    await poller._tick()
    assert len(tmp_store.list_orch_events()) == 1
    assert len([e for e in broadcaster.events if e["type"] == "orch_event"]) == 1


@pytest.mark.asyncio
async def test_poller_surfaces_orch_parse_failure_once(tmp_store, tmp_path):
    events_dir = tmp_path / "orch"
    events_dir.mkdir(parents=True)
    (events_dir / "bad.json").write_text("{ not json", encoding="utf-8")
    broadcaster = _Broadcaster()
    poller = Poller(tmp_store, _orch_cfg(), broadcaster)
    poller.register_orch_collector(OrchEventCollector(events_dir))

    await poller._tick()
    await poller._tick()  # 같은 실패 파일 — 재표면화 금지(spam 방지)

    failures = [e for e in broadcaster.events if e["type"] == "orch_event_failure"]
    assert len(failures) == 1  # 파일당 1회만 표면화(C3)
    assert failures[0]["path"].endswith("bad.json")


class _RaisingOrchCollector(OrchEventCollector):
    def __init__(self) -> None:
        super().__init__(Path("."))

    def collect(self):
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_poller_orch_collector_failure_does_not_break_tick(tmp_store):
    # orch 수집기 예외가 폴링 루프/세션 파이프라인을 죽이지 않는다(C3 격리)
    broadcaster = _Broadcaster()
    poller = Poller(tmp_store, _orch_cfg(), broadcaster)
    poller.register(_Collector())              # 세션 수집기
    poller.register_orch_collector(_RaisingOrchCollector())

    await poller._tick()                        # 예외가 새지 않고 완료

    assert tmp_store.get_session("fake:ok") is not None  # 세션 파이프라인 정상
