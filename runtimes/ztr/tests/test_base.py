"""IAgent ABC + 공통 타입 단위 테스트."""
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
    _DEFERRED_REGISTRATIONS,
)


class TestHealthStatus:
    def test_values(self) -> None:
        assert HealthStatus.HEALTHY.value == "healthy"
        assert HealthStatus.DEGRADED.value == "degraded"
        assert HealthStatus.QUARANTINED.value == "quarantined"

    def test_member_count(self) -> None:
        assert len(HealthStatus) == 3


class TestAgentRole:
    def test_values(self) -> None:
        assert AgentRole.WRITER.value == "writer"
        assert AgentRole.CRITIC.value == "critic"
        assert AgentRole.MANAGER.value == "manager"
        assert AgentRole.SUMMARIZER.value == "summarizer"

    def test_member_count(self) -> None:
        assert len(AgentRole) == 4


class TestPrompt:
    def test_creation_with_defaults(self) -> None:
        p = Prompt(content="hello")
        assert p.content == "hello"
        assert p.role == AgentRole.WRITER
        assert p.context == {}

    def test_creation_with_custom(self) -> None:
        p = Prompt(
            content="review this",
            role=AgentRole.CRITIC,
            context={"issue_id": "ISSUE-001"},
        )
        assert p.role == AgentRole.CRITIC
        assert p.context["issue_id"] == "ISSUE-001"

    def test_frozen(self) -> None:
        p = Prompt(content="test")
        with pytest.raises(AttributeError):
            p.content = "changed"  # type: ignore[misc]


class TestAgentResponse:
    def test_defaults(self) -> None:
        r = AgentResponse(content="ok", agent_id="a1", role=AgentRole.WRITER)
        assert r.success is True
        assert r.tokens_used is None
        assert r.latency_ms == 0.0
        assert r.error == ""

    def test_error_response(self) -> None:
        r = AgentResponse(
            content="",
            agent_id="a1",
            role=AgentRole.WRITER,
            success=False,
            error="타임아웃",
        )
        assert r.success is False
        assert r.error == "타임아웃"


class TestRateLimitStatus:
    def test_defaults(self) -> None:
        s = RateLimitStatus()
        assert s.is_limited is False
        assert s.requests_remaining is None

    def test_limited(self) -> None:
        s = RateLimitStatus(is_limited=True, requests_remaining=0)
        assert s.is_limited is True


class TestIAgentSubclassing:
    def test_cannot_instantiate_abc(self) -> None:
        with pytest.raises(TypeError):
            IAgent()  # type: ignore[abstract]

    def test_subclass_without_type_not_registered(self) -> None:
        initial = len(_DEFERRED_REGISTRATIONS)

        class NoTypeAgent(IAgent):
            # agent_type = "" (기본값) → 등록 안 됨
            async def initialize(self, config: dict[str, Any]) -> None: ...
            async def shutdown(self) -> None: ...
            async def invoke(self, prompt: Prompt) -> AgentResponse: ...
            async def health_check(self) -> HealthStatus: ...
            def rate_limit_status(self) -> RateLimitStatus: ...
            @property
            def agent_id(self) -> str: return ""
            @property
            def capabilities(self) -> set[AgentRole]: return set()

        assert len(_DEFERRED_REGISTRATIONS) == initial

    def test_subclass_with_type_registered(self) -> None:
        initial = len(_DEFERRED_REGISTRATIONS)

        class TestAgent(IAgent):
            agent_type: ClassVar[str] = "_test_registration_"

            async def initialize(self, config: dict[str, Any]) -> None: ...
            async def shutdown(self) -> None: ...
            async def invoke(self, prompt: Prompt) -> AgentResponse: ...
            async def health_check(self) -> HealthStatus: ...
            def rate_limit_status(self) -> RateLimitStatus: ...
            @property
            def agent_id(self) -> str: return ""
            @property
            def capabilities(self) -> set[AgentRole]: return set()

        assert len(_DEFERRED_REGISTRATIONS) == initial + 1
        name, klass = _DEFERRED_REGISTRATIONS[-1]
        assert name == "_test_registration_"
        assert klass is TestAgent

    def test_missing_abstract_method_raises(self) -> None:
        with pytest.raises(TypeError):

            class IncompleteAgent(IAgent):  # type: ignore[abstract]
                agent_type: ClassVar[str] = "incomplete"

            IncompleteAgent()
