"""acp/collectors/fake.py — 테스트·개발용 고정 픽스처 수집기.

P0에서 실제 앱 수집 없이 전체 파이프라인 골격을 검증하기 위해 사용.
실 앱 수집은 P1(Codex), P2(Claude/Cursor)에서 구현.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os

from acp.collectors.base import BaseCollector
from acp.models import SessionRecord


def _dt(offset_seconds: float = 0.0) -> datetime:
    """UTC now 기준 offset 적용 datetime."""
    return datetime.now(timezone.utc) - timedelta(seconds=offset_seconds)


class FakeCollector(BaseCollector):
    """고정 픽스처 레코드를 반환. LIVE / IDLE / HOLDING 상태를 섞어 파이프라인 검증."""

    def __init__(self) -> None:
        self._collect_count = 0
        self._transition_after = _env_int("ACP_FAKE_TRANSITION_AFTER", 0)
        self._transition_age_seconds = _env_float("ACP_FAKE_TRANSITION_AGE_SECONDS", 1200.0)

    @property
    def app_name(self) -> str:
        return "fake"

    def collect(self) -> list[SessionRecord]:
        self._collect_count += 1
        live_age = 30.0
        if self._transition_after > 0 and self._collect_count >= self._transition_after:
            live_age = self._transition_age_seconds

        return [
            SessionRecord(
                app="fake",
                session_id="fake-live-001",
                project_path="C:/Users/shkim/Desktop/Todo/AgentControlPlane",
                model="gpt-5-fake",
                last_activity=_dt(live_age),    # 30초 전 활동 → LIVE, e2e 전이 옵션 시 HOLDING
                running_pid=12345,
                running_cmd="python -m acp web",
                last_event="task_complete",
                source_file="fake://live-001",
            ),
            SessionRecord(
                app="fake",
                session_id="fake-idle-002",
                project_path="C:/Users/shkim/Desktop/Todo/ZeroTokenRoundtable",
                model="claude-fake",
                last_activity=_dt(360),          # 6분 전 활동 → IDLE
                last_event="task_complete",
                source_file="fake://idle-002",
            ),
            SessionRecord(
                app="fake",
                session_id="fake-holding-003",
                project_path="C:/workspace/project",
                model="codex-fake",
                last_activity=_dt(1200),         # 20분 전 task_complete → HOLDING
                running_pid=None,
                last_event="task_complete",
                source_file="fake://holding-003",
            ),
            SessionRecord(
                app="fake",
                session_id="fake-unknown-004",
                project_path=None,               # 프로젝트 미바인딩
                model=None,
                last_activity=None,              # 활동시각 없음 → UNKNOWN
                source_file="fake://unknown-004",
            ),
            SessionRecord(
                app="fake",
                session_id="fake-stale-005",
                project_path="C:/Users/shkim/Desktop/Todo/AgentControlPlane",
                model="gpt-5-fake",
                last_activity=_dt(3900),         # stale_ttl 초과 → STALE
                last_event="task_complete",
                source_file="fake://stale-005",
            ),
            SessionRecord(
                app="fake",
                session_id="fake-error-006",
                project_path="C:/Users/shkim/Desktop/Todo/AgentControlPlane",
                model="gpt-5-fake",
                last_activity=_dt(10),
                last_event="error",
                source_file="fake://error-006",
            ),
        ]


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return default if value is None else int(value)


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return default if value is None else float(value)
