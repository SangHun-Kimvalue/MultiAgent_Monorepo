"""acp/poller.py — asyncio 주기 폴링 루프.

등록된 Collector들을 poll_interval마다 실행하고,
SessionRecord → derive_state → store.upsert → broadcaster.publish 파이프라인을 구동.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from acp.collectors.base import BaseCollector
from acp.collectors.orch_collector import OrchEventCollector
from acp.config import AppConfig
from acp.dedupe import NotificationDedupe
from acp.join import join_phase
from acp.liveness import derive_state
from acp.models import SessionState
from acp.notify import Notifier, StateTransitionEvent
from acp.proc import is_pid_alive
from acp.store import SessionStore

if TYPE_CHECKING:
    from acp.web.app import EventBroadcaster

logger = logging.getLogger(__name__)


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
        # 이미 경고/브로드캐스트한 실패 파일 경로 — 매 tick 재경고 spam 방지(C3 표면화는 1회).
        self._seen_orch_failures: set[str] = set()

    def register(self, collector: BaseCollector) -> None:
        self._collectors.append(collector)
        logger.info("Collector 등록: %s", collector.app_name)

    def register_orch_collector(self, collector: OrchEventCollector) -> None:
        """orchestrator 이벤트 수집기 등록(단일). SessionRecord 파이프라인과 별도 처리."""
        self._orch_collector = collector
        logger.info("Orch 이벤트 수집기 등록: %s", collector.app_name)

    async def run(self) -> None:
        """영구 루프 — asyncio.Task로 실행."""
        logger.info("Poller 시작 (interval=%.1fs)", self._cfg.poll_interval)
        while True:
            await self._tick()
            await asyncio.sleep(self._cfg.poll_interval)

    async def _tick(self) -> None:
        now = datetime.now(timezone.utc)
        for collector in self._collectors:
            try:
                records = collector.collect()
            except Exception as e:
                # C3: 예외는 삼키지 않고 로깅. 수집기 하나가 죽어도 루프 지속.
                logger.warning("Collector '%s' 실패: %s", collector.app_name, e)
                continue

            for record in records:
                try:
                    state = derive_state(record, now, self._cfg.liveness, is_alive=is_pid_alive)
                    phase = join_phase(record, state)
                    prev = self._store.get_session_for_record(record)
                    prev_state = SessionState(prev["state"]) if prev else None

                    self._store.upsert_session(record, state, phase=phase)

                    # 상태 변경 시 이벤트 발행
                    if prev_state is None or prev_state != state:
                        event_session_id = f"{record.app}:{record.session_id}"
                        self._store.append_event(
                            event_session_id,
                            "state_change",
                            {"from": prev_state.value if prev_state else None, "to": state.value},
                        )
                        await self._broadcaster.publish({
                            "type": "state_change",
                            "session_id": event_session_id,
                            "native_session_id": record.session_id,
                            "app": record.app,
                            "state": state.value,
                            "project_path": record.project_path,
                        })
                        if self._dedupe.should_reset(prev, state):
                            self._store.clear_notification_marker(event_session_id)
                        if self._notifier and prev_state is not None and self._dedupe.should_notify(prev, state, now):
                            notification = StateTransitionEvent(
                                session_id=event_session_id,
                                native_session_id=record.session_id,
                                app=record.app,
                                project_path=record.project_path,
                                from_state=prev_state.value if prev_state else None,
                                to_state=state,
                                created_at=now,
                            )
                            try:
                                self._notifier.notify(notification)
                            except Exception as notify_error:
                                logger.warning(
                                    "알림 발행 실패 [%s]: %s",
                                    event_session_id,
                                    notify_error,
                                )
                            else:
                                payload = {
                                    "from": notification.from_state,
                                    "to": notification.to_state.value,
                                    "title": notification.title,
                                    "body": notification.body,
                                    "detail": notification.detail,
                                    "app": notification.app,
                                    "native_session_id": notification.native_session_id,
                                    "project_path": notification.project_path,
                                }
                                self._store.append_event(event_session_id, "notification_sent", payload)
                                self._store.mark_notified(event_session_id, state, now)
                                await self._broadcaster.publish({
                                    "type": "notification",
                                    "session_id": event_session_id,
                                    **payload,
                                })
                except Exception as e:
                    logger.warning(
                        "레코드 처리 실패 [%s:%s]: %s",
                        record.app,
                        record.session_id,
                        e,
                    )

        await self._collect_orch_events()

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
