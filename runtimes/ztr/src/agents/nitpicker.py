"""NitpickerAgent — 내부 static review 엔진의 IAgent 어댑터.

Phase 2부터 외부 Nitpicker Daemon(jemmin) import 의존을 제거한다.
실제 판정은 src.engine.static_review의 ruff/mypy 실행 결과만 따른다.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, ClassVar

from src.agents.base import (
    AgentResponse,
    AgentRole,
    HealthStatus,
    IAgent,
    Prompt,
    RateLimitStatus,
)
from src.engine.static_review import run_static_review

logger = logging.getLogger(__name__)


class NitpickerAgent(IAgent):
    """Mechanical prefilter 역할의 내부 정적 분석 어댑터."""

    agent_type: ClassVar[str] = "nitpicker"

    def __init__(self) -> None:
        self._agent_id = ""
        self._mode = "prefilter"
        self._target_project_path = Path.cwd()
        self._roles: set[AgentRole] = set()
        self._timeout_s = 60.0
        self._initialized = False

    async def initialize(self, config: dict[str, Any]) -> None:
        """외부 경로 import 없이 로컬 프로젝트 기준으로 초기화한다."""
        self._agent_id = config.get("id", "nitpicker-local")
        self._mode = config.get("mode", "prefilter")
        self._timeout_s = float(config.get("timeout_sec", 60.0))
        self._target_project_path = Path(
            config.get("target_project_path", ".")
        ).resolve()

        role_names = config.get("roles", ["critic"])
        role_map = {role.value: role for role in AgentRole}
        self._roles = {
            role_map[name] for name in role_names
            if name in role_map
        }

        self._initialized = True
        logger.info(
            "NitpickerAgent 초기화: id=%s, mode=%s, target=%s",
            self._agent_id,
            self._mode,
            self._target_project_path,
        )

    async def shutdown(self) -> None:
        self._initialized = False

    async def invoke(self, prompt: Prompt) -> AgentResponse:
        """Prompt.context의 target_file/targets를 내부 정적 리뷰로 검사한다."""
        start = time.monotonic()
        if not self._initialized:
            return AgentResponse(
                content="",
                agent_id=self._agent_id,
                role=prompt.role,
                success=False,
                error="NitpickerAgent가 초기화되지 않았습니다",
            )

        targets = self._extract_targets(prompt)
        report = await run_static_review(
            targets,
            cwd=self._target_project_path,
            timeout_s=self._timeout_s,
        )
        payload = report.as_review_payload(review_text=None)
        elapsed = (time.monotonic() - start) * 1000
        return AgentResponse(
            content=json.dumps(payload, ensure_ascii=False),
            agent_id=self._agent_id,
            role=prompt.role,
            success=report.verdict.value != "BLOCKED",
            latency_ms=elapsed,
            raw={
                "mode": self._mode,
                "verdict": report.verdict.value,
                "exit_code": report.exit_code,
                "findings_count": len(report.findings),
            },
            error="" if report.verdict.value != "BLOCKED" else "정적 리뷰 실행 실패",
        )

    async def health_check(self) -> HealthStatus:
        if not self._initialized:
            return HealthStatus.DEGRADED
        if not self._target_project_path.exists():
            return HealthStatus.QUARANTINED
        return HealthStatus.HEALTHY

    def rate_limit_status(self) -> RateLimitStatus:
        """로컬 ruff/mypy 실행이므로 rate limit 없음."""
        return RateLimitStatus()

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def capabilities(self) -> set[AgentRole]:
        return self._roles

    def _extract_targets(self, prompt: Prompt) -> list[str]:
        raw_targets = prompt.context.get("targets")
        if isinstance(raw_targets, list):
            return [str(target) for target in raw_targets if str(target)]

        target_file = prompt.context.get("target_file")
        if isinstance(target_file, str) and target_file:
            return [target_file]

        return []
