from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import acp.web.app as webapp
from acp.orch_runs import (
    InvalidRunStateError,
    OrchRunManager,
    OrchRunState,
    OrchRunStartRequest,
    SegmentResult,
    SegmentStatus,
)
from acp.orch_events import OrchEventType, OrchPhaseEvent
from acp.store import SessionStore
from acp.web.app import EventBroadcaster, app, init_app


@pytest.fixture(autouse=True)
def _restore_app_globals():
    # F1(리뷰): 테스트가 init_app으로 모듈 전역을 세팅하므로 종료 후 복원해
    # 닫힌 store가 전역에 남지 않게 한다(후속 web 테스트 footgun 방지).
    prev_store = webapp._store
    prev_broadcaster = webapp._broadcaster
    prev_run_manager = webapp._run_manager
    yield
    webapp._store = prev_store
    webapp._broadcaster = prev_broadcaster
    webapp._run_manager = prev_run_manager


def _store(tmp_path) -> SessionStore:
    return SessionStore(str(tmp_path / "t.db"), str(tmp_path / "e.jsonl"))


def _record_event(store: SessionStore, phase_id: str) -> None:
    event = OrchPhaseEvent(
        project_id="TestProject",
        phase_id=phase_id,
        type=OrchEventType.PHASE_VERDICT,
        ts=datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc),
        payload={"status": "PASS"},
    )
    store.record_orch_event(event)


class CaptureBroadcaster(EventBroadcaster):
    def __init__(self) -> None:
        super().__init__()
        self.published: list[dict[str, object]] = []

    async def publish(self, event: dict[str, object]) -> None:
        self.published.append(event)
        await super().publish(event)


def test_api_orch_events_empty_store_returns_empty_list(tmp_path):
    store = _store(tmp_path)
    init_app(store, EventBroadcaster())
    client = TestClient(app)

    response = client.get("/api/orch-events")

    assert response.status_code == 200
    assert response.json() == []

    store.close()


def test_api_orch_events_returns_recorded_events(tmp_path):
    store = _store(tmp_path)
    init_app(store, EventBroadcaster())
    client = TestClient(app)
    _record_event(store, "P1")
    _record_event(store, "P2")

    response = client.get("/api/orch-events")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    assert {event["phase_id"] for event in body} == {"P1", "P2"}

    store.close()


def test_api_orch_events_filters_by_phase_id(tmp_path):
    store = _store(tmp_path)
    init_app(store, EventBroadcaster())
    client = TestClient(app)
    _record_event(store, "P1")
    _record_event(store, "P2")

    response = client.get("/api/orch-events?phase_id=P1")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["phase_id"] == "P1"

    store.close()


def test_api_orch_events_payload_is_deserialized_dict(tmp_path):
    # F2(리뷰 nit): payload가 API 응답에서 dict로 역직렬화돼 오는지 라우트 레벨 확인
    store = _store(tmp_path)
    init_app(store, EventBroadcaster())
    client = TestClient(app)
    _record_event(store, "P1")

    body = client.get("/api/orch-events").json()

    assert body[0]["payload"] == {"status": "PASS"}

    store.close()


def test_home_page_renders_orch_events_section(tmp_path):
    # 프론트: 홈 템플릿이 orch 이벤트 섹션을 렌더하는지(테스트ID 존재) 확인
    store = _store(tmp_path)
    init_app(store, EventBroadcaster())
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert 'data-testid="orch-events-list"' in response.text

    store.close()


def test_home_page_renders_orch_run_panel(tmp_path):
    # Phase 2: 홈 템플릿이 구동 패널(입력·Run·Approve·status·error)을 렌더하는지 확인
    store = _store(tmp_path)
    init_app(store, EventBroadcaster())
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    text = response.text
    for testid in (
        "orch-run-panel",
        "orch-run-prompt",
        "orch-run-project",
        "orch-run-phase",
        "orch-run-start",
        "orch-run-approve",
        "orch-run-status",
        "orch-run-error",
    ):
        assert f'data-testid="{testid}"' in text, f"{testid} 미렌더"

    store.close()


def test_api_orch_run_returns_run_id_and_waits_for_gate(tmp_path):
    store = _store(tmp_path)
    init_app(store, EventBroadcaster())
    client = TestClient(app)

    response = client.post(
        "/api/orch/run",
        json={"prompt": "drive phase one", "project_id": "T2", "phase_id": "P1"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"].startswith("orch-run-")
    assert body["project_id"] == "T2"
    assert body["phase_id"] == "P1"
    assert body["status"] == SegmentStatus.AWAITING_GATE.value
    assert body["resume_token"]

    events = client.get("/api/orch-events?phase_id=P1").json()
    event_types = {event["type"] for event in events}
    assert event_types == {
        OrchEventType.PHASE_STARTED.value,
        OrchEventType.GATE_WAITING.value,
    }
    assert OrchEventType.PHASE_VERDICT.value not in event_types

    store.close()


def test_api_orch_run_publishes_orch_events_to_broadcaster(tmp_path):
    store = _store(tmp_path)
    broadcaster = CaptureBroadcaster()
    init_app(store, broadcaster)
    client = TestClient(app)

    response = client.post("/api/orch/run", json={"prompt": "publish events"})

    assert response.status_code == 200
    assert [event["type"] for event in broadcaster.published] == ["orch_event", "orch_event"]
    assert {event["event_type"] for event in broadcaster.published} == {
        OrchEventType.PHASE_STARTED.value,
        OrchEventType.GATE_WAITING.value,
    }

    store.close()


def test_api_orch_run_rejects_concurrent_active_run(tmp_path):
    store = _store(tmp_path)
    init_app(store, EventBroadcaster())
    client = TestClient(app)

    first = client.post("/api/orch/run", json={"prompt": "first"})
    second = client.post("/api/orch/run", json={"prompt": "second"})

    assert first.status_code == 200
    assert second.status_code == 409
    assert "already active" in second.json()["detail"]

    store.close()


def test_api_orch_run_approve_advances_only_after_human_action(tmp_path):
    store = _store(tmp_path)
    init_app(store, EventBroadcaster())
    client = TestClient(app)

    start = client.post("/api/orch/run", json={"prompt": "needs approval", "phase_id": "P1"}).json()
    before_approve = client.get("/api/orch-events?phase_id=P1").json()
    before_types = {event["type"] for event in before_approve}
    assert before_types == {
        OrchEventType.PHASE_STARTED.value,
        OrchEventType.GATE_WAITING.value,
    }

    approved = client.post(f"/api/orch/runs/{start['run_id']}/approve")

    assert approved.status_code == 200
    body = approved.json()
    assert body["status"] == SegmentStatus.DONE.value
    assert body["resume_token"] is None

    after_approve = client.get("/api/orch-events?phase_id=P1").json()
    assert {event["type"] for event in after_approve} == {
        OrchEventType.PHASE_STARTED.value,
        OrchEventType.GATE_WAITING.value,
        OrchEventType.PHASE_VERDICT.value,
    }

    store.close()


def test_api_orch_run_approve_unknown_run_returns_404(tmp_path):
    store = _store(tmp_path)
    init_app(store, EventBroadcaster())
    client = TestClient(app)

    response = client.post("/api/orch/runs/missing/approve")

    assert response.status_code == 404

    store.close()


# ── P2 회귀: 동시 approve가 중복 phase.verdict를 만들지 않음 ──


class _GatedResumeDriver:
    """첫 segment는 gate에서 멈추고, resume segment는 release를 기다린다.

    두 approve가 동시에 in-flight되도록 강제해 P2 race window를 재현한다. verdict payload는
    호출 횟수로 구별해 race가 열려 있었다면 store에 2행이 남도록 만든다(dedup이 race를
    가리지 않게 함).
    """

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.resume_calls = 0

    async def run_segment(
        self, *, prompt, project_id, phase_id, run_id, resume_token
    ) -> SegmentResult:
        if resume_token is None:
            return SegmentResult(
                status=SegmentStatus.AWAITING_GATE,
                resume_token=f"{run_id}:gate",
                events=(
                    OrchPhaseEvent(
                        project_id=project_id,
                        phase_id=phase_id,
                        type=OrchEventType.GATE_WAITING,
                        ts=datetime(2026, 6, 20, 8, 0, tzinfo=timezone.utc),
                        payload={"run_id": run_id},
                    ),
                ),
            )
        self.resume_calls += 1
        call_n = self.resume_calls
        await self.release.wait()
        return SegmentResult(
            status=SegmentStatus.DONE,
            events=(
                OrchPhaseEvent(
                    project_id=project_id,
                    phase_id=phase_id,
                    type=OrchEventType.PHASE_VERDICT,
                    ts=datetime(2026, 6, 20, 9, 0, tzinfo=timezone.utc),
                    payload={"run_id": run_id, "call": call_n},
                ),
            ),
        )


def test_concurrent_approve_does_not_duplicate_phase_verdict(tmp_path):
    store = _store(tmp_path)
    driver = _GatedResumeDriver()
    manager = OrchRunManager(store=store, broadcaster=EventBroadcaster(), driver=driver)

    async def scenario():
        start = await manager.start_run(
            OrchRunStartRequest(prompt="needs approval", phase_id="P1")
        )
        assert start.status == SegmentStatus.AWAITING_GATE
        t1 = asyncio.create_task(manager.approve_run(start.run_id))
        t2 = asyncio.create_task(manager.approve_run(start.run_id))
        # 두 approve가 각자 첫 await(또는 raise)에 도달할 때까지 양보한다.
        for _ in range(50):
            if t2.done() and driver.resume_calls >= 1:
                break
            await asyncio.sleep(0)
        driver.release.set()
        return await asyncio.gather(t1, t2, return_exceptions=True)

    results = asyncio.run(scenario())

    states = [r for r in results if isinstance(r, OrchRunState)]
    errors = [r for r in results if isinstance(r, InvalidRunStateError)]
    assert len(states) == 1
    assert states[0].status == SegmentStatus.DONE
    assert len(errors) == 1
    # 두 번째 approve는 driver에 절대 도달하지 않는다.
    assert driver.resume_calls == 1
    verdicts = [
        event
        for event in store.list_orch_events(phase_id="P1")
        if event["type"] == OrchEventType.PHASE_VERDICT.value
    ]
    assert len(verdicts) == 1

    store.close()


# ── P3 회귀: store가 dedup한 이벤트는 SSE live duplicate로 나가지 않음 ──


class _DuplicateEventDriver:
    """start/resume에서 동일 내용 이벤트를 방출해 store dedup을 유발한다."""

    def _dup(self, project_id, phase_id) -> OrchPhaseEvent:
        return OrchPhaseEvent(
            project_id=project_id,
            phase_id=phase_id,
            type=OrchEventType.PHASE_STARTED,
            ts=datetime(2026, 6, 20, 7, 0, tzinfo=timezone.utc),
            payload={"marker": "dup"},
        )

    async def run_segment(
        self, *, prompt, project_id, phase_id, run_id, resume_token
    ) -> SegmentResult:
        if resume_token is None:
            return SegmentResult(
                status=SegmentStatus.AWAITING_GATE,
                resume_token=f"{run_id}:gate",
                events=(self._dup(project_id, phase_id),),
            )
        return SegmentResult(
            status=SegmentStatus.DONE,
            events=(self._dup(project_id, phase_id),),
        )


def test_deduped_event_not_published_as_sse_live_duplicate(tmp_path):
    store = _store(tmp_path)
    broadcaster = CaptureBroadcaster()
    driver = _DuplicateEventDriver()
    manager = OrchRunManager(store=store, broadcaster=broadcaster, driver=driver)

    async def scenario():
        start = await manager.start_run(
            OrchRunStartRequest(prompt="dup events", phase_id="P1")
        )
        return await manager.approve_run(start.run_id)

    final = asyncio.run(scenario())

    assert final.status == SegmentStatus.DONE
    # store: 동일 내용 이벤트는 1행으로 멱등 수렴.
    stored = [
        event
        for event in store.list_orch_events(phase_id="P1")
        if event["payload"].get("marker") == "dup"
    ]
    assert len(stored) == 1
    # SSE: 신규 삽입(start)만 1회 publish — resume의 중복은 live로 나가지 않는다.
    published = [
        event
        for event in broadcaster.published
        if event["payload"].get("marker") == "dup"
    ]
    assert len(published) == 1

    store.close()


# ── 추가 계약 테스트 ──


def test_api_orch_run_reapprove_after_done_returns_409(tmp_path):
    store = _store(tmp_path)
    init_app(store, EventBroadcaster())
    client = TestClient(app)

    start = client.post("/api/orch/run", json={"prompt": "x"}).json()
    first = client.post(f"/api/orch/runs/{start['run_id']}/approve")
    assert first.status_code == 200
    assert first.json()["status"] == SegmentStatus.DONE.value

    second = client.post(f"/api/orch/runs/{start['run_id']}/approve")
    assert second.status_code == 409

    store.close()


def test_api_orch_run_allows_new_run_after_done(tmp_path):
    store = _store(tmp_path)
    init_app(store, EventBroadcaster())
    client = TestClient(app)

    start = client.post("/api/orch/run", json={"prompt": "first"}).json()
    client.post(f"/api/orch/runs/{start['run_id']}/approve")

    second = client.post("/api/orch/run", json={"prompt": "second"})
    assert second.status_code == 200
    assert second.json()["run_id"] != start["run_id"]

    store.close()


def test_api_orch_run_empty_prompt_returns_422(tmp_path):
    store = _store(tmp_path)
    init_app(store, EventBroadcaster())
    client = TestClient(app)

    response = client.post("/api/orch/run", json={"prompt": ""})

    assert response.status_code == 422

    store.close()


def test_api_orch_run_extra_field_returns_422(tmp_path):
    store = _store(tmp_path)
    init_app(store, EventBroadcaster())
    client = TestClient(app)

    response = client.post("/api/orch/run", json={"prompt": "x", "rogue": True})

    assert response.status_code == 422

    store.close()
