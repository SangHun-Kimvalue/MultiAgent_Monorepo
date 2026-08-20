"""acp/poller.py — asyncio 주기 폴링 루프.

등록된 Collector들을 poll_interval마다 실행하고,
SessionRecord → derive_state → store.upsert → broadcaster.publish 파이프라인을 구동.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from acp.collectors.base import (
    PROCESS_SIGNAL_OK,
    STATUS_COMPLETED_UNKNOWN,
    STATUS_FAILED,
    STATUS_PARTIAL,
    SOURCE_SYNTHESIZED,
    BaseCollector,
    all_unknown_scopes,
)
from acp.collectors.orch_collector import OrchEventCollector
from acp.config import AppConfig
from acp.dedupe import NotificationDedupe
from acp.join import join_phase
from acp.liveness import ERROR_EVENTS, derive_state
from acp.models import SessionRecord, SessionState
from acp.notify import Notifier, StateTransitionEvent
from acp.notify_view import notification_event, notification_payload
from acp.proc import is_pid_alive
from acp.store import SessionStore

if TYPE_CHECKING:
    from acp.web.app import EventBroadcaster

logger = logging.getLogger(__name__)


@dataclass
class _Transition:
    """한 세션의 상태 전이 사실. 스레드 단계가 모으고 루프 단계가 발행한다."""

    record: SessionRecord
    prev: dict[str, Any] | None
    prev_value: str | None
    state: SessionState


@dataclass
class _TickOutcome:
    """스레드 단계가 돌려주는 것 — **사실만** 담고 발행하지 않는다."""

    app: str
    cycle_row: dict[str, object] | None = None
    transitions: list[_Transition] = field(default_factory=list)
    # 저장에 실패한 레코드 수. 0이 아니면 이 틱은 완결이 아니다.
    persist_failed: int = 0


def _parse_state(value: object) -> SessionState | None:
    """저장된 상태 문자열 → enum. **계약 밖 값에서 예외를 던지지 않는다**(T14 S4c-2).

    라이브 실측: 어휘 밖 상태가 저장된 행(다른 버전이 쓴 행·enum rename 이후 잔존)을
    만나면 `SessionState(...)`가 `ValueError`를 내 그 레코드의 처리 **전체가 중단**됐다
    (`레코드 처리 실패` 경고). 그러면 그 세션은 매 틱 갱신에 실패해 영원히 옛 상태로
    남는다 — 모르는 **이전 값** 하나가 현재 판정까지 막는 것이다(LESSON-006 rule 8).

    모르면 "이전 상태를 모른다"(`None`)로 둔다. 그 결과 이번 판정이 전이로 기록되는데,
    그것이 사실이다 — 우리는 이 행의 직전 상태를 모른다.
    """
    try:
        return SessionState(str(value))
    except ValueError:
        logger.warning("계약 밖 상태 문자열(이전 상태 미상으로 처리): %r", value)
        return None


def _as_utc(value: datetime) -> datetime:
    """naive datetime을 UTC로 간주(liveness와 같은 규약)."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class Poller:
    """비동기 주기 폴링 루프."""

    def __init__(
        self,
        store: SessionStore,
        config: AppConfig,
        broadcaster: "EventBroadcaster",
        notifier: Notifier | None = None,
    ) -> None:
        self._store = store
        self._cfg = config
        self._broadcaster = broadcaster
        self._notifier = notifier
        self._dedupe = NotificationDedupe(config.notify)
        self._collectors: list[BaseCollector] = []
        self._orch_collector: OrchEventCollector | None = None
        # 정지 신호. 취소가 아니라 **신호**인 이유는 `request_stop()` 주석 참조.
        self._stop = asyncio.Event()
        # 이미 경고/브로드캐스트한 실패 파일 경로 — 매 tick 재경고 spam 방지(C3 표면화는 1회).
        self._seen_orch_failures: set[str] = set()

    def register(self, collector: BaseCollector) -> None:
        self._collectors.append(collector)
        logger.info("Collector 등록: %s", collector.app_name)

    def register_orch_collector(self, collector: OrchEventCollector) -> None:
        """orchestrator 이벤트 수집기 등록(단일). SessionRecord 파이프라인과 별도 처리."""
        self._orch_collector = collector
        logger.info("Orch 이벤트 수집기 등록: %s", collector.app_name)

    def request_stop(self) -> None:
        """다음 틱을 시작하지 말라고 알린다. **진행 중인 틱은 끝까지 간다.**

        `task.cancel()`로는 부족하다(T14 S6 감사 P1) — 수집·쓰기는 `asyncio.to_thread`의
        워커 스레드에서 도는데, 태스크를 취소해도 **그 스레드는 멈추지 않는다**. 곧바로
        `store.close()`를 부르면 워커가 **닫힌 연결에 쓰거나 쓰는 중에 연결이 닫힌다**.
        스레드로 옮기면서 내가 만든 종료 경로의 새 실패 모드다.
        """
        self._stop.set()

    async def run(self) -> None:
        """정지 신호가 올 때까지 도는 루프 — asyncio.Task로 실행."""
        logger.info("Poller 시작 (interval=%.1fs)", self._cfg.poll_interval)
        while not self._stop.is_set():
            await self._tick()
            # 대기는 정지 신호로 **즉시 깨어난다**. `sleep`으로 두면 종료가 최대
            # poll_interval만큼 늦어지고, 그 지연이 취소 유혹을 만든다.
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._cfg.poll_interval)
            except asyncio.TimeoutError:
                pass
        logger.info("Poller 정지(진행 중이던 틱은 완료됨)")

    async def _tick(self) -> None:
        """한 수집 주기.

        **두 단계로 나눈다**(T14 S6 D3). 예전에는 수집·판정·쓰기가 전부 동기 함수인데
        이벤트 루프 안에서 돌아, 틱 하나가 **15초마다 3.7~4.7초씩 루프를 막았다**(실측).
        그동안 SSE와 HTTP가 통째로 대기했다.

          A 스레드: 수집 + 상태 판정 + **세션 쓰기**  — 세션 전용 연결만 만진다
          B 루프  : 사이클 기록 · 이벤트 · 알림 · SSE — 주 연결만 만진다

        두 단계가 **서로 다른 연결**을 쓰므로 같은 연결을 두 스레드가 동시에 쓰지 않는다.
        발행은 항상 **DB 반영 뒤**다 — 섞으면 "화면엔 있는데 DB엔 없는" 구간이 생긴다.
        """
        now = datetime.now(timezone.utc)
        for collector in self._collectors:
            outcome = await asyncio.to_thread(self._collect_and_store, collector, now)
            await self._publish_outcome(outcome, now)

        await self._collect_orch_events()

    def _collect_and_store(self, collector: BaseCollector, now: datetime) -> "_TickOutcome":
        """단계 A — **스레드에서** 돈다. 세션 전용 연결 외의 저장소 경로를 만지지 않는다.

        여기서 사이클을 기록하지 않는 이유(설계검토 P2): `record_collector_cycle()`은
        주 연결에 쓰므로 스레드에서 부르면 소유권 불변이 깨진다. 사실만 모아 돌려주고
        기록은 루프가 한다.
        """
        outcome = _TickOutcome(app=collector.app_name)
        try:
            records = collector.collect()
        except Exception as e:
            # C3: 예외는 삼키지 않고 로깅. 수집기 하나가 죽어도 루프 지속.
            logger.warning("Collector '%s' 실패: %s", collector.app_name, e)
            # 실패도 **사이클로 기록**한다(T14 S3 D4). 기록하지 않으면 API에는 이전
            # 성공 사이클이 계속 최신 건강도로 보여 현재 실패가 은폐된다.
            # 예외로 중단됐으니 **아무 축도 세지 못했다** — 카운트를 0으로 적으면
            # "실패 0건"과 "셀 수 없었음"이 같은 값이 된다(T14 S4b D2c).
            outcome.cycle_row = {
                "app": collector.app_name,
                "observed_at": now.isoformat(),
                "status": STATUS_FAILED,
                "process_signal": None,
                "process_observed_at": None,
                "count_scopes": all_unknown_scopes(),
                "source": SOURCE_SYNTHESIZED,
            }
            return outcome

        # 완결성은 수집기가 말한다(T14 S4b D2a — `last_cycle`은 추상 계약).
        # 계약을 어긴 구현이 있어도 기록은 남아야 하므로 방어 합성을 둔다. 단
        # **합성은 절대 `success_complete`를 만들지 않는다** — 폴러가 아는 것은
        # "호출이 예외 없이 반환됐다"뿐이고 그것은 완결성이 아니다.
        try:
            cycle_obj = collector.last_cycle
            row = cycle_obj.as_row() if cycle_obj is not None else None
        except Exception as exc:
            logger.warning("last_cycle 접근 실패 [%s]: %s", collector.app_name, exc)
            row = None
        if row is None:
            logger.warning(
                "수집기가 사이클을 선언하지 않았다(완결성 미확인): %s", collector.app_name
            )
            row = {
                "app": collector.app_name,
                "observed_at": now.isoformat(),
                "status": STATUS_COMPLETED_UNKNOWN,
                "process_signal": None,
                "process_observed_at": None,
                "count_scopes": all_unknown_scopes(),
                "source": SOURCE_SYNTHESIZED,
            }
        outcome.cycle_row = row
        # **관측했다고 말한 경우에만** 가용이다(T14 S4c-1 D1c). 예전 `!= "unavailable"`은
        # 넓어진 어휘와 합성 경로의 `None`을 전부 가용으로 읽었고, 그것이 stale 구간을
        # STALE로 **확정**시켰다 — 화면은 `확인 불가`인데 상태는 `STALE`이던 경로다.
        process_signal_available = row.get("process_signal") == PROCESS_SIGNAL_OK

        persist_failed = 0
        for record in records:
            try:
                prev = self._store.get_session_for_record(record)
                # 두 형태를 함께 든다(구현리뷰 R2 P1): 판정 분기에 쓸 **enum**과, 사실
                # 보고에 쓸 **원문**. 어휘 밖 상태를 enum으로 못 만든다고 해서 "이전
                # 상태가 없었다"고 말하면 저장소에 남은 사실을 발행 경로에서 다시 잃는다.
                prev_value = str(prev["state"]) if prev else None
                prev_state = _parse_state(prev_value) if prev_value is not None else None

                if record.archived:
                    # 보관 세션은 **관제 대상이 아니다**(T14 S4a D5). 마지막으로 알던
                    # 상태를 **원문 그대로** 보존한다 — enum으로 강제하면 `unknown`으로
                    # 덮어써서 알던 사실이 사라진다(구현리뷰 R1 P1).
                    state_value = (
                        prev_value if prev_value is not None else SessionState.UNKNOWN.value
                    )
                    phase_state = prev_state if prev_state is not None else SessionState.UNKNOWN
                    self._store.upsert_session(
                        record, state_value, phase=join_phase(record, phase_state)
                    )
                    continue

                state = derive_state(
                    record,
                    now,
                    self._cfg.liveness,
                    is_alive=is_pid_alive,
                    process_signal_available=process_signal_available,
                )
                self._store.upsert_session(record, state, phase=join_phase(record, state))

                # 전이는 **사실만 모아** 두고 발행은 루프가 한다. 비교·보고 모두 원문
                # 기준이다 — 어휘 밖 이전 상태를 `None`으로 축약하면 실제 전이가
                # `from: null`로 기록된다(구현리뷰 R2 P1).
                if prev_value != state.value:
                    outcome.transitions.append(
                        _Transition(record=record, prev=prev, prev_value=prev_value, state=state)
                    )
            except Exception as e:
                persist_failed += 1
                logger.warning(
                    "레코드 처리 실패 [%s:%s]: %s", record.app, record.session_id, e
                )
        if persist_failed:
            # **반영하지 못한 틱을 성공으로 고지하지 않는다**(구현리뷰 P1 — 내가 만든 회귀).
            # 전용 쓰기 연결을 도입하면서 `busy_timeout` 초과 같은 새 실패 경로가 생겼는데,
            # 수집기가 선언한 `success_complete`를 그대로 두면 세션이 반영되지 않은 틱도
            # 정상 관측으로 읽힌다 — 이 트랙이 고쳐 온 "실패를 정상으로 세탁"이다.
            # 수집 자체는 성공했으므로 `failed`(수집 실패 건수) 축은 건드리지 않는다.
            logger.warning(
                "세션 반영 실패 %d건 — 사이클을 partial로 낮춘다 [%s]",
                persist_failed, collector.app_name,
            )
            outcome.persist_failed = persist_failed
            if outcome.cycle_row is not None:
                outcome.cycle_row["status"] = STATUS_PARTIAL
        return outcome

    async def _publish_outcome(self, outcome: "_TickOutcome", now: datetime) -> None:
        """단계 B — **루프에서** 돈다. 주 연결만 만지고, 발행은 DB 반영 뒤에 한다."""
        if outcome.cycle_row is not None:
            try:
                self._store.record_collector_cycle(outcome.cycle_row)
            except Exception as exc:  # 기록 실패가 수집을 죽이지 않는다(C3)
                logger.warning("수집 사이클 기록 실패 [%s]: %s", outcome.app, exc)

        for transition in outcome.transitions:
            record, prev = transition.record, transition.prev
            prev_value, state = transition.prev_value, transition.state
            event_session_id = f"{record.app}:{record.session_id}"
            try:
                self._store.append_event(
                    event_session_id,
                    "state_change",
                    {"from": prev_value, "to": state.value},
                )
                await self._broadcaster.publish({
                    "type": "state_change",
                    "session_id": event_session_id,
                    "native_session_id": record.session_id,
                    "app": record.app,
                    "state": state.value,
                    "project_path": record.project_path,
                    "last_activity": (
                        record.last_activity.isoformat() if record.last_activity else None
                    ),
                })
                if self._dedupe.should_reset(prev, state):
                    self._store.clear_notification_marker(event_session_id)
                # 재분류 알림 폭주 차단(T14 S2 D7): 판정 규칙이 바뀌면 오래 멈춰 있던
                # 세션이 한 틱에 무더기로 상태를 옮기는데, 그건 사건이 아니라 재분류다.
                elapsed = (
                    (now - _as_utc(record.last_activity)).total_seconds()
                    if record.last_activity is not None
                    else None
                )
                if (
                    self._notifier
                    # 이전 상태가 **있었다는 사실**이 기준이다(구현리뷰 R2 P1).
                    and prev_value is not None
                    and self._dedupe.should_notify(
                        prev,
                        state,
                        now,
                        elapsed=elapsed,
                        stale_ttl=self._cfg.liveness.stale_ttl,
                        explicit_event=record.last_event in ERROR_EVENTS,
                    )
                ):
                    notification = StateTransitionEvent(
                        session_id=event_session_id,
                        native_session_id=record.session_id,
                        app=record.app,
                        project_path=record.project_path,
                        from_state=prev_value,
                        to_state=state,
                        created_at=now,
                    )
                    try:
                        self._notifier.notify(notification)
                    except Exception as notify_error:
                        logger.warning(
                            "알림 발행 실패 [%s]: %s", event_session_id, notify_error
                        )
                    else:
                        payload = notification_payload(notification)
                        self._store.append_event(event_session_id, "notification_sent", payload)
                        self._store.mark_notified(event_session_id, state, now)
                        await self._broadcaster.publish(
                            notification_event(event_session_id, payload)
                        )
            except Exception as e:
                logger.warning("전이 발행 실패 [%s]: %s", event_session_id, e)

    async def _collect_orch_events(self) -> None:
        """orchestrator 이벤트를 collect→store(멱등)→broadcast. SessionRecord와 독립.

        valid 이벤트는 store가 내용 해시로 멱등 dedup → 신규만 broadcast.
        파싱 실패는 조용히 버리지 않고(C3 / 계약 §3) 파일당 1회 경고+broadcast한다.
        """
        if self._orch_collector is None:
            return
        try:
            result = self._orch_collector.collect()
        except Exception as e:
            logger.warning("Orch 수집기 실패: %s", e)
            return

        for event in result.events:
            try:
                is_new = self._store.record_orch_event(event)
            except Exception as e:
                logger.warning("orch 이벤트 저장 실패 [%s/%s]: %s", event.phase_id, event.type, e)
                continue
            if is_new:
                await self._broadcaster.publish({
                    "type": "orch_event",
                    "event_type": event.type.value,
                    "project_id": event.project_id,
                    "phase_id": event.phase_id,
                    "ts": event.ts.isoformat(),
                    "payload": event.payload,
                })

        for failure in result.failures:
            if failure.path in self._seen_orch_failures:
                continue
            self._seen_orch_failures.add(failure.path)
            logger.warning("orch 이벤트 파싱 실패(표면화): %s — %s", failure.path, failure.reason)
            await self._broadcaster.publish({
                "type": "orch_event_failure",
                "path": failure.path,
                "reason": failure.reason,
            })
