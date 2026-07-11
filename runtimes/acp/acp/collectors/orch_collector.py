"""acp/collectors/orch_collector.py — orchestrator phase 이벤트 수집기.

orchestrator가 events_dir에 파일로 emit한 phase lifecycle 이벤트(*.json)를 읽어
검증된 OrchPhaseEvent 목록으로 반환한다(계약: ACP_EVENT_CONTRACT_DRAFT §4 transport (b),
파일-폴링 — 기존 SessionRecord 수집기와 동형).

불변 원칙:
- read-only(C7): 이벤트 파일을 수정/삭제하지 않는다.
- 파싱 실패는 **조용히 버리지 않는다**(C3 / 계약 §3) — 실패를 failures로 보고한다.
- BaseCollector(SessionRecord 반환)와 형태가 다르므로(이벤트는 phase 단위) 별도 수집기다.
- R5: 이벤트 사실(verdict/exit/경로/시각)만 수집. 의미 판정은 LLM/사람.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from acp.orch_events import OrchPhaseEvent, parse_orch_event

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrchParseFailure:
    """검증/읽기 실패한 이벤트 파일 — 조용히 버리지 않고 보고하기 위한 기록(C3)."""

    path: str
    reason: str


@dataclass
class OrchCollectResult:
    """수집 결과 — 유효 이벤트와 실패를 함께 반환(실패 silent drop 금지)."""

    events: list[OrchPhaseEvent] = field(default_factory=list)
    failures: list[OrchParseFailure] = field(default_factory=list)


class OrchEventCollector:
    """orchestrator phase 이벤트 파일 수집기(read-only)."""

    def __init__(self, events_dir: Path) -> None:
        self._events_dir = events_dir

    @property
    def app_name(self) -> str:
        return "orch"

    def collect(self) -> OrchCollectResult:
        """events_dir의 *.json을 읽어 검증된 이벤트 + 실패 목록을 반환한다."""
        result = OrchCollectResult()
        if not self._events_dir.exists():
            logger.debug("orch events_dir 없음: %s", self._events_dir)
            return result

        for jf in sorted(self._events_dir.glob("*.json")):
            try:
                raw = json.loads(jf.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("orch 이벤트 읽기/JSON 실패: %s — %s", jf.name, e)
                result.failures.append(OrchParseFailure(str(jf), f"read/json: {e}"))
                continue

            try:
                event = parse_orch_event(raw)
            except ValueError as e:
                # C3: 스키마 불일치를 조용히 버리지 않고 실패로 보고(계약 §3).
                logger.warning("orch 이벤트 검증 실패(버리지 않고 보고): %s — %s", jf.name, e)
                result.failures.append(OrchParseFailure(str(jf), f"validate: {e}"))
                continue

            result.events.append(event)

        logger.debug(
            "OrchEventCollector: %d 이벤트, %d 실패", len(result.events), len(result.failures)
        )
        return result
