"""Phase 1 orchestrator driving run manager.

이 모듈은 ACP가 "관측 전용"에서 최소 구동 API를 갖도록 하는 얇은 PoC 계층이다.
영속 권위는 만들지 않고, 실제 표면화는 기존 orch_events 저장소와 SSE broadcaster에 위임한다.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from acp.orch_events import OrchEventType, OrchPhaseEvent
from acp.store import SessionStore


class SegmentStatus(StrEnum):
    RUNNING = "running"
    AWAITING_GATE = "awaiting_gate"
    DONE = "done"
    BLOCKED = "blocked"


class OrchRunStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1)
    project_id: str = "MAM"
    phase_id: str | None = None


class SegmentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: SegmentStatus
    resume_token: str | None = None
    events: tuple[OrchPhaseEvent, ...] = ()
    message: str | None = None


class OrchRunState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    project_id: str
    phase_id: str
    status: SegmentStatus
    resume_token: str | None = None
    message: str | None = None


class Driver(Protocol):
    async def run_segment(
        self,
        *,
        prompt: str,
        project_id: str,
        phase_id: str,
        run_id: str,
        resume_token: str | None,
    ) -> SegmentResult:
        """Run one segment and return the contract result.

        Implementations should not raise and should fail closed with
        `SegmentResult(status=BLOCKED, ...)`. The manager still defends this
        boundary and blocks the run if a driver raises.
        """


class ActiveRunConflictError(RuntimeError):
    pass


class UnknownRunError(RuntimeError):
    pass


class InvalidRunStateError(RuntimeError):
    pass


class MockGateDriver:
    """결정론적 Phase 1 driver.

    첫 segment는 반드시 gate에서 멈추고, 승인 resume_token이 들어온 뒤에만 done을 낸다.
    """

    async def run_segment(
        self,
        *,
        prompt: str,
        project_id: str,
        phase_id: str,
        run_id: str,
        resume_token: str | None,
    ) -> SegmentResult:
        if resume_token is None:
            gate_token = f"{run_id}:gate-1"
            return SegmentResult(
                status=SegmentStatus.AWAITING_GATE,
                resume_token=gate_token,
                message="awaiting human approval",
                events=(
                    _event(
                        project_id,
                        phase_id,
                        OrchEventType.PHASE_STARTED,
                        {
                            "run_id": run_id,
                            "status": SegmentStatus.RUNNING.value,
                            "driver": "mock_gate",
                        },
                    ),
                    _event(
                        project_id,
                        phase_id,
                        OrchEventType.GATE_WAITING,
                        {
                            "run_id": run_id,
                            "status": SegmentStatus.AWAITING_GATE.value,
                            "resume_token": gate_token,
                            "approval_required": True,
                        },
                    ),
                ),
            )

        return SegmentResult(
            status=SegmentStatus.DONE,
            message="approved by human gate",
            events=(
                _event(
                    project_id,
                    phase_id,
                    OrchEventType.PHASE_VERDICT,
                    {
                        "run_id": run_id,
                        "status": SegmentStatus.DONE.value,
                        "approved": True,
                    },
                ),
            ),
        )


class OrchRunManager:
    """Single-run Phase 1 manager.

    Phase 1의 의도대로 N>1 격리나 장기 영속 상태는 제공하지 않는다. 대신 동시 실행은
    409로 fail-loud 하여 조용한 자동 진행을 막는다.
    """

    def __init__(
        self,
        *,
        store: SessionStore,
        broadcaster: Any,
        driver: Driver | None = None,
    ) -> None:
        self._store = store
        self._broadcaster = broadcaster
        self._driver = driver or MockGateDriver()
        self._runs: dict[str, _RunContext] = {}
        self._active_run_id: str | None = None

    async def start_run(self, request: OrchRunStartRequest) -> OrchRunState:
        if self._active_run_id is not None:
            active = self._runs[self._active_run_id]
            if active.state.status in {SegmentStatus.RUNNING, SegmentStatus.AWAITING_GATE}:
                raise ActiveRunConflictError("an orchestrator run is already active")

        run_id = f"orch-run-{uuid4().hex[:12]}"
        phase_id = request.phase_id or run_id
        initial = OrchRunState(
            run_id=run_id,
            project_id=request.project_id,
            phase_id=phase_id,
            status=SegmentStatus.RUNNING,
        )
        self._runs[run_id] = _RunContext(prompt=request.prompt, state=initial)
        self._active_run_id = run_id

        # 독립 리뷰 P1: 장주기 driver(예: ztr relay, 수십 분)는 완주까지 API가 블록되면
        # run_id도 못 받아 관측 불능이다. driver가 run_in_background=True를 선언하면
        # PHASE_STARTED를 선-emit하고 segment를 백그라운드 task로 돌려 RUNNING을 즉시 반환한다.
        # (분기는 capability 속성 사실만 — R5. 기존 sync driver 경로는 무변경.)
        if getattr(self._driver, "run_in_background", False):
            try:
                await self._record_and_publish(
                    _event(
                        initial.project_id,
                        initial.phase_id,
                        OrchEventType.PHASE_STARTED,
                        {
                            "run_id": run_id,
                            "status": SegmentStatus.RUNNING.value,
                            "background": True,
                        },
                    )
                )
            except Exception as exc:
                return await self._block_driver_exception(run_id, exc)
            context = self._runs[run_id]
            context.task = asyncio.create_task(self._run_segment_background(run_id))
            return context.state

        try:
            return await self._run_segment(run_id)
        except _DriverSegmentError as exc:
            return await self._block_driver_exception(run_id, exc.error)

    async def _run_segment_background(self, run_id: str) -> None:
        # 백그라운드 segment: 예외도 fail-closed 경로로 — 상태는 get_run/SSE로 관측된다.
        try:
            await self._run_segment(run_id)
        except _DriverSegmentError as exc:
            await self._block_driver_exception(run_id, exc.error)

    async def approve_run(self, run_id: str) -> OrchRunState:
        context = self._get_run(run_id)
        if context.state.status != SegmentStatus.AWAITING_GATE or not context.state.resume_token:
            raise InvalidRunStateError("run is not awaiting human approval")

        # P2: driver await 이전에 동기적으로 RUNNING으로 점유한다. asyncio는 단일 스레드이고
        # 이 체크~전환 사이에 await가 없으므로, 느린 async driver에서도 동시 approve 두 번이
        # 모두 AWAITING_GATE를 통과해 중복 phase.verdict를 내는 race를 닫는다(AD-7: 자동 진행
        # 아님 — 사람 승인 1회만 점유). resume_token은 유지해 다음 segment에 전달한다.
        context.state = context.state.model_copy(update={"status": SegmentStatus.RUNNING})
        try:
            return await self._run_segment(run_id)
        except _DriverSegmentError as exc:
            return await self._block_driver_exception(run_id, exc.error)

    def get_run(self, run_id: str) -> OrchRunState:
        return self._get_run(run_id).state

    async def _run_segment(self, run_id: str) -> OrchRunState:
        context = self._get_run(run_id)
        # 독립 리뷰 P1: driver 호출뿐 아니라 store/publish·state 재구성까지 한 try로 감싼다.
        # 어느 단계 예외든 _DriverSegmentError로 래핑돼 호출자가 BLOCKED+active 해제 → events
        # 루프 예외가 _active_run_id를 영구 누수시키던 잔존 데드락 경로를 닫는다.
        try:
            result = await self._driver.run_segment(
                prompt=context.prompt,
                project_id=context.state.project_id,
                phase_id=context.state.phase_id,
                run_id=context.state.run_id,
                resume_token=context.state.resume_token,
            )
            for event in result.events:
                await self._record_and_publish(event)

            context.state = OrchRunState(
                run_id=context.state.run_id,
                project_id=context.state.project_id,
                phase_id=context.state.phase_id,
                status=result.status,
                resume_token=result.resume_token,
                message=result.message,
            )
            if result.status in {SegmentStatus.DONE, SegmentStatus.BLOCKED}:
                self._active_run_id = None
            # P2 관측성: 드라이버가 이벤트 없이 BLOCKED로 fail-closed 반환한 경우(timeout/spawn error 등),
            # 매니저가 단일 choke point에서 terminal PHASE_VERDICT(blocked)를 합성해 store/SSE로 표면화한다.
            # 이미 verdict가 있으면(미래 드라이버 대비) 이중 emit하지 않는다(R5: event type만 검사).
            if result.status is SegmentStatus.BLOCKED and not _has_verdict(result.events):
                await self._emit_blocked_verdict(context.state, result.message)
        except Exception as exc:
            raise _DriverSegmentError(exc) from exc
        return context.state

    async def _record_and_publish(self, event: OrchPhaseEvent) -> None:
        # store가 신규 삽입한 이벤트만 SSE로 publish한다(멱등 dedup — rowcount 사실만 분기, R5).
        inserted = self._store.record_orch_event(event)
        if inserted:
            await self._publish_orch_event(event)

    async def _emit_blocked_verdict(self, state: OrchRunState, message: str | None) -> None:
        # BLOCKED 관측성(P2): AWAITING_GATE/DONE과 달리 드라이버 BLOCKED·예외 경로는 이벤트를 남기지
        # 않아 대시보드가 BLOCKED를 못 봤다. 매니저가 terminal PHASE_VERDICT(status=blocked)를 합성한다.
        # best-effort: 관측 emit이 실패해도(store-down 등) fail-closed 전이·active 해제를 되돌리지 않는다.
        event = _event(
            state.project_id,
            state.phase_id,
            OrchEventType.PHASE_VERDICT,
            {
                "run_id": state.run_id,
                "status": SegmentStatus.BLOCKED.value,
                "message": message,
            },
        )
        try:
            await self._record_and_publish(event)
        except Exception:
            # 관측 실패는 삼킨다 — BLOCKED 상태·active 해제는 이미 확정됐고 되돌리지 않는다.
            return

    async def _block_driver_exception(self, run_id: str, exc: Exception) -> OrchRunState:
        context = self._get_run(run_id)
        # driver뿐 아니라 store/publish 단계 예외도 이 경로로 온다 → 중립 라벨(독립 리뷰 P2).
        message = f"segment failed: {exc.__class__.__name__}: {exc}"
        context.state = context.state.model_copy(
            update={
                "status": SegmentStatus.BLOCKED,
                "resume_token": None,
                "message": message,
            }
        )
        self._active_run_id = None
        # P2 관측성: 예외 경로 BLOCKED도 terminal 이벤트를 남긴다. state·active 확정을 **먼저** 한 뒤
        # best-effort emit — 이 예외 자체가 store 실패에서 올 수 있으므로 emit 실패를 삼켜 fail-closed를 지킨다.
        await self._emit_blocked_verdict(context.state, message)
        return context.state

    def _get_run(self, run_id: str) -> "_RunContext":
        try:
            return self._runs[run_id]
        except KeyError as exc:
            raise UnknownRunError("orchestrator run not found") from exc

    async def _publish_orch_event(self, event: OrchPhaseEvent) -> None:
        await self._broadcaster.publish(
            {
                "type": "orch_event",
                "project_id": event.project_id,
                "phase_id": event.phase_id,
                "event_type": event.type.value,
                "ts": event.ts.isoformat(),
                "payload": event.payload,
            }
        )


class _RunContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    prompt: str
    state: OrchRunState
    # 백그라운드 segment task 참조(GC 방지 + 관측). sync driver 경로에서는 None.
    task: Any = None


class _DriverSegmentError(RuntimeError):
    def __init__(self, error: Exception) -> None:
        super().__init__(str(error))
        self.error = error


def _has_verdict(events: tuple[OrchPhaseEvent, ...]) -> bool:
    # R5: event type enum만 검사(prose 판정 금지). 매니저 합성 BLOCKED verdict의 이중 emit 가드.
    return any(event.type is OrchEventType.PHASE_VERDICT for event in events)


def _event(
    project_id: str,
    phase_id: str,
    event_type: OrchEventType,
    payload: dict[str, object],
) -> OrchPhaseEvent:
    return OrchPhaseEvent(
        project_id=project_id,
        phase_id=phase_id,
        type=event_type,
        ts=datetime.now(timezone.utc),
        payload=payload,
    )
