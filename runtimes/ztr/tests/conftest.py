"""테스트용 공유 fixture.

FakeAgent: IAgent의 완전한 테스트 더블.
reset_registry: 테스트 간 레지스트리 격리.
"""
from __future__ import annotations

from typing import Any, ClassVar

import pytest

from src.agents.base import (
    AgentResponse,
    AgentRole,
    HealthStatus,
    IAgent,
    Prompt,
    RateLimitStatus,
)
from src.agents.registry import AgentRegistry


class FakeAgent(IAgent):
    """테스트용 가짜 에이전트."""

    agent_type: ClassVar[str] = "fake"

    def __init__(self) -> None:
        self._id = "fake-001"
        self._initialized = False
        self._shutdown_called = False
        self._invoke_count = 0
        self._health = HealthStatus.HEALTHY

    async def initialize(self, config: dict[str, Any]) -> None:
        self._id = config.get("id", self._id)
        self._initialized = True

    async def shutdown(self) -> None:
        self._shutdown_called = True

    async def invoke(self, prompt: Prompt) -> AgentResponse:
        self._invoke_count += 1
        return AgentResponse(
            content=f"Fake response #{self._invoke_count}: {prompt.content[:50]}",
            agent_id=self._id,
            role=prompt.role,
        )

    async def health_check(self) -> HealthStatus:
        return self._health

    def rate_limit_status(self) -> RateLimitStatus:
        return RateLimitStatus()

    @property
    def agent_id(self) -> str:
        return self._id

    @property
    def capabilities(self) -> set[AgentRole]:
        return {AgentRole.WRITER, AgentRole.CRITIC}


@pytest.fixture(autouse=True)
def reset_registry() -> None:  # type: ignore[misc]
    """테스트마다 레지스트리를 초기화한다."""
    AgentRegistry.reset()
    # FakeAgent 재등록 (reset이 class_registry도 지우므로)
    AgentRegistry.register_class("fake", FakeAgent)
