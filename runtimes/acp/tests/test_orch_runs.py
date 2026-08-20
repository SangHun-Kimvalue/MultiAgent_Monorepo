from __future__ import annotations

import asyncio

from datetime import datetime, timezone

from acp.orch_events import OrchEventType, OrchPhaseEvent
from acp.orch_runs import (
    MockGateDriver,
    OrchRunManager,
    OrchRunStartRequest,
    SegmentResult,
    SegmentStatus,
)
from acp.store import SessionStore


class _NoopBroadcaster:
    async def publish(self, event: dict[str, object]) -> None:
        return None


class _RecordingBroadcaster:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def publish(self, event: dict[str, object]) -> None:
        self.events.append(event)


def _verdicts(bc: _RecordingBroadcaster) -> list[dict[str, object]]:
    return [e for e in bc.events if e["event_type"] == OrchEventType.PHASE_VERDICT.value]


def _store(tmp_path) -> SessionStore:
    return SessionStore(str(tmp_path / "t.db"), str(tmp_path / "e.jsonl"))


class _ScriptedDriver:
    def __init__(self, results: list[SegmentResult | Exception]) -> None:
        self.results = results
        self.calls = 0

    async def run_segment(
        self, *, prompt, project_id, phase_id, run_id, resume_token
    ) -> SegmentResult:
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _RaiseOnResumeDriver:
    async def run_segment(
        self, *, prompt, project_id, phase_id, run_id, resume_token
    ) -> SegmentResult:
        if resume_token is None:
            return SegmentResult(
                status=SegmentStatus.AWAITING_GATE,
                resume_token=f"{run_id}:gate",
            )
        raise RuntimeError("resume exploded")


def test_start_run_blocks_and_releases_active_run_when_driver_raises(tmp_path):
    store = _store(tmp_path)
    driver = _ScriptedDriver([RuntimeError("start exploded")])
    manager = OrchRunManager(store=store, broadcaster=_NoopBroadcaster(), driver=driver)

    state = asyncio.run(
        manager.start_run(OrchRunStartRequest(prompt="go", phase_id="P1"))
    )

    assert state.status == SegmentStatus.BLOCKED
    assert state.resume_token is None
    assert state.message == "segment failed: RuntimeError: start exploded"
    assert manager.get_run(state.run_id).status == SegmentStatus.BLOCKED
    assert manager._active_run_id is None
    store.close()


def test_approve_run_blocks_and_releases_active_run_when_driver_raises(tmp_path):
    store = _store(tmp_path)
    manager = OrchRunManager(
        store=store, broadcaster=_NoopBroadcaster(), driver=_RaiseOnResumeDriver()
    )

    async def scenario():
        start = await manager.start_run(OrchRunStartRequest(prompt="go", phase_id="P1"))
        assert start.status == SegmentStatus.AWAITING_GATE
        return await manager.approve_run(start.run_id)

    state = asyncio.run(scenario())

    assert state.status == SegmentStatus.BLOCKED
    assert state.resume_token is None
    assert state.message == "segment failed: RuntimeError: resume exploded"
    assert manager.get_run(state.run_id).status == SegmentStatus.BLOCKED
    assert manager._active_run_id is None
    store.close()


def test_driver_exception_does_not_deadlock_next_start_run(tmp_path):
    store = _store(tmp_path)
    driver = _ScriptedDriver(
        [
            RuntimeError("first failed"),
            SegmentResult(status=SegmentStatus.AWAITING_GATE, resume_token="second:gate"),
        ]
    )
    manager = OrchRunManager(store=store, broadcaster=_NoopBroadcaster(), driver=driver)

    async def scenario():
        first = await manager.start_run(OrchRunStartRequest(prompt="first", phase_id="P1"))
        assert first.status == SegmentStatus.BLOCKED
        assert manager._active_run_id is None
        second = await manager.start_run(
            OrchRunStartRequest(prompt="second", phase_id="P2")
        )
        return first, second

    first, second = asyncio.run(scenario())

    assert second.status == SegmentStatus.AWAITING_GATE
    assert second.run_id != first.run_id
    assert manager._active_run_id == second.run_id
    assert driver.calls == 2
    store.close()


def test_blocked_segment_result_keeps_existing_fail_closed_behavior(tmp_path):
    store = _store(tmp_path)
    driver = _ScriptedDriver(
        [
            SegmentResult(status=SegmentStatus.BLOCKED, message="driver blocked"),
            SegmentResult(status=SegmentStatus.AWAITING_GATE, resume_token="next:gate"),
        ]
    )
    manager = OrchRunManager(store=store, broadcaster=_NoopBroadcaster(), driver=driver)

    async def scenario():
        first = await manager.start_run(OrchRunStartRequest(prompt="first", phase_id="P1"))
        assert first.status == SegmentStatus.BLOCKED
        assert first.message == "driver blocked"
        assert manager._active_run_id is None
        return await manager.start_run(OrchRunStartRequest(prompt="next", phase_id="P2"))

    second = asyncio.run(scenario())

    assert second.status == SegmentStatus.AWAITING_GATE
    assert manager._active_run_id == second.run_id
    store.close()


def test_store_exception_blocks_and_releases_active_run(tmp_path, monkeypatch):
    # 독립 리뷰 P1: driver가 아니라 store.record_orch_event(events 루프)가 raise해도
    # active_run이 해제돼 데드락이 없어야 한다(예외가 try 밖이던 잔존 경로).
    from acp.orch_runs import MockGateDriver

    store = _store(tmp_path)
    real_record = store.record_orch_event
    calls = {"n": 0}

    def flaky_record(event):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("store down")
        return real_record(event)

    monkeypatch.setattr(store, "record_orch_event", flaky_record)
    manager = OrchRunManager(
        store=store, broadcaster=_NoopBroadcaster(), driver=MockGateDriver()
    )

    first = asyncio.run(manager.start_run(OrchRunStartRequest(prompt="go", phase_id="P1")))
    assert first.status == SegmentStatus.BLOCKED
    assert manager._active_run_id is None

    # active가 해제됐으므로 다음 start_run이 409 없이 진행된다(데드락 없음).
    nxt = asyncio.run(manager.start_run(OrchRunStartRequest(prompt="go2", phase_id="P2")))
    assert nxt.status == SegmentStatus.AWAITING_GATE
    store.close()


def test_blocked_result_emits_terminal_verdict_event(tmp_path):
    # P2 경로 A: 드라이버가 events 없이 BLOCKED로 fail-closed 반환 → 매니저가 terminal
    # PHASE_VERDICT(status=blocked)를 합성해 store/SSE로 표면화한다(대시보드 관측 가능).
    store = _store(tmp_path)
    bc = _RecordingBroadcaster()
    driver = _ScriptedDriver([SegmentResult(status=SegmentStatus.BLOCKED, message="driver blocked")])
    manager = OrchRunManager(store=store, broadcaster=bc, driver=driver)

    state = asyncio.run(manager.start_run(OrchRunStartRequest(prompt="go", phase_id="P1")))

    assert state.status == SegmentStatus.BLOCKED
    verdicts = _verdicts(bc)
    assert len(verdicts) == 1
    assert verdicts[0]["payload"]["status"] == SegmentStatus.BLOCKED.value
    assert verdicts[0]["payload"]["run_id"] == state.run_id
    store.close()


def test_driver_exception_emits_terminal_verdict_event(tmp_path):
    # P2 경로 B: 드라이버 예외 → _block_driver_exception도 BLOCKED verdict를 남긴다.
    store = _store(tmp_path)
    bc = _RecordingBroadcaster()
    driver = _ScriptedDriver([RuntimeError("boom")])
    manager = OrchRunManager(store=store, broadcaster=bc, driver=driver)

    state = asyncio.run(manager.start_run(OrchRunStartRequest(prompt="go", phase_id="P1")))

    assert state.status == SegmentStatus.BLOCKED
    verdicts = [e for e in _verdicts(bc) if e["payload"]["status"] == SegmentStatus.BLOCKED.value]
    assert len(verdicts) == 1
    store.close()


def test_blocked_verdict_emit_failure_is_swallowed(tmp_path, monkeypatch):
    # best-effort 불변: BLOCKED 관측 emit이 항상 실패해도(store-down) 예외가 전파되지 않고,
    # BLOCKED 전이·active 해제는 확정된 채 유지된다.
    store = _store(tmp_path)

    def always_raise(event):
        raise RuntimeError("store down")

    monkeypatch.setattr(store, "record_orch_event", always_raise)
    manager = OrchRunManager(
        store=store, broadcaster=_NoopBroadcaster(), driver=_ScriptedDriver([RuntimeError("boom")])
    )

    state = asyncio.run(manager.start_run(OrchRunStartRequest(prompt="go", phase_id="P1")))

    assert state.status == SegmentStatus.BLOCKED
    assert manager._active_run_id is None
    store.close()


def test_done_flow_emits_single_verdict_without_blocked(tmp_path):
    # 회귀: 정상 DONE 경로는 드라이버 PHASE_VERDICT(done) 하나만 — 매니저가 blocked를 덧붙이지 않는다.
    store = _store(tmp_path)
    bc = _RecordingBroadcaster()
    manager = OrchRunManager(store=store, broadcaster=bc, driver=MockGateDriver())

    async def scenario():
        start = await manager.start_run(OrchRunStartRequest(prompt="go", phase_id="P1"))
        return await manager.approve_run(start.run_id)

    state = asyncio.run(scenario())

    assert state.status == SegmentStatus.DONE
    verdicts = _verdicts(bc)
    assert len(verdicts) == 1
    assert verdicts[0]["payload"]["status"] == SegmentStatus.DONE.value
    store.close()


def test_blocked_result_with_existing_verdict_not_duplicated(tmp_path):
    # 이중 emit 가드: 드라이버가 BLOCKED인데 이미 PHASE_VERDICT를 포함하면 매니저는 합성하지 않는다.
    store = _store(tmp_path)
    bc = _RecordingBroadcaster()
    existing = OrchPhaseEvent(
        project_id="MAM",
        phase_id="P1",
        type=OrchEventType.PHASE_VERDICT,
        ts=datetime.now(timezone.utc),
        payload={"run_id": "pre", "status": SegmentStatus.BLOCKED.value, "source": "driver"},
    )
    driver = _ScriptedDriver(
        [SegmentResult(status=SegmentStatus.BLOCKED, message="m", events=(existing,))]
    )
    manager = OrchRunManager(store=store, broadcaster=bc, driver=driver)

    state = asyncio.run(manager.start_run(OrchRunStartRequest(prompt="go", phase_id="P1")))

    assert state.status == SegmentStatus.BLOCKED
    verdicts = _verdicts(bc)
    assert len(verdicts) == 1
    assert verdicts[0]["payload"]["source"] == "driver"
    store.close()


class _SlowBackgroundDriver:
    """run_in_background capability를 선언한 장주기 driver 시뮬."""

    run_in_background = True

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.fail = False

    async def run_segment(
        self, *, prompt, project_id, phase_id, run_id, resume_token
    ) -> SegmentResult:
        if resume_token is not None:
            return SegmentResult(status=SegmentStatus.DONE, message="closed")
        await self.release.wait()
        if self.fail:
            raise RuntimeError("relay exploded")
        return SegmentResult(
            status=SegmentStatus.AWAITING_GATE, resume_token=f"{run_id}:gate"
        )


def test_background_driver_returns_running_immediately(tmp_path):
    # 독립 리뷰 P1: 장주기 driver는 완주를 기다리지 않고 RUNNING+run_id를 즉시 반환하고,
    # PHASE_STARTED가 선-emit돼 GUI가 진행을 관측할 수 있어야 한다.
    store = _store(tmp_path)
    bc = _RecordingBroadcaster()
    driver = _SlowBackgroundDriver()
    manager = OrchRunManager(store=store, broadcaster=bc, driver=driver)

    async def scenario():
        first = await manager.start_run(OrchRunStartRequest(prompt="go", phase_id="P6"))
        assert first.status == SegmentStatus.RUNNING  # 즉시 반환 — driver 완주 전
        started = [e for e in bc.events if e["event_type"] == "phase.started"]
        assert len(started) == 1  # 선-emit이 완주 전에 이미 관측됨
        assert manager.get_run(first.run_id).status == SegmentStatus.RUNNING
        driver.release.set()  # relay 완주 시뮬
        await manager._runs[first.run_id].task
        assert manager.get_run(first.run_id).status == SegmentStatus.AWAITING_GATE
        done = await manager.approve_run(first.run_id)
        return done

    done = asyncio.run(scenario())

    assert done.status == SegmentStatus.DONE
    store.close()


def test_background_driver_exception_blocks_and_releases(tmp_path):
    store = _store(tmp_path)
    bc = _RecordingBroadcaster()
    driver = _SlowBackgroundDriver()
    driver.fail = True
    manager = OrchRunManager(store=store, broadcaster=bc, driver=driver)

    async def scenario():
        first = await manager.start_run(OrchRunStartRequest(prompt="go", phase_id="P6"))
        assert first.status == SegmentStatus.RUNNING
        driver.release.set()
        await manager._runs[first.run_id].task
        return first.run_id

    run_id = asyncio.run(scenario())

    state = manager.get_run(run_id)
    assert state.status == SegmentStatus.BLOCKED
    assert manager._active_run_id is None  # Phase4 fail-closed 유지
    blocked_verdicts = [
        e for e in bc.events
        if e["event_type"] == "phase.verdict" and e["payload"].get("status") == "blocked"
    ]
    assert len(blocked_verdicts) == 1  # Phase5 관측성 유지
    store.close()


def test_sync_driver_path_unchanged_by_background_feature(tmp_path):
    # 회귀: capability 없는 기존 driver는 종전대로 동기 완주 후 반환(선-emit 없음).
    store = _store(tmp_path)
    bc = _RecordingBroadcaster()
    manager = OrchRunManager(store=store, broadcaster=bc, driver=MockGateDriver())

    first = asyncio.run(manager.start_run(OrchRunStartRequest(prompt="go", phase_id="P1")))

    assert first.status == SegmentStatus.AWAITING_GATE  # RUNNING 아님 — 동기 경로
    started = [e for e in bc.events if e["event_type"] == "phase.started"]
    assert len(started) == 1  # driver 자신의 started 하나뿐(매니저 선-emit 중복 없음)
    store.close()
