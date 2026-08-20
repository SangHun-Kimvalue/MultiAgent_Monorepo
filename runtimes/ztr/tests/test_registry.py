"""AgentRegistry 단위 테스트."""
from __future__ import annotations

import pytest

from src.agents.base import AgentRole, _DEFERRED_REGISTRATIONS
from src.agents.registry import AgentRegistry
from tests.conftest import FakeAgent


class TestDiscover:
    def test_discover_drains_deferred(self) -> None:
        _DEFERRED_REGISTRATIONS.append(("test_discover", FakeAgent))
        count = AgentRegistry.discover()
        assert count == 1
        assert "test_discover" in AgentRegistry._class_registry

    def test_discover_empty(self) -> None:
        count = AgentRegistry.discover()
        assert count == 0


class TestCreate:
    async def test_create_new_agent(self) -> None:
        agent = await AgentRegistry.create("test-1", "fake", {"roles": ["writer"]})
        assert agent.agent_id == "test-1"

    async def test_create_idempotent(self) -> None:
        a1 = await AgentRegistry.create("test-1", "fake", {})
        a2 = await AgentRegistry.create("test-1", "fake", {})
        assert a1 is a2

    async def test_create_unknown_type_raises(self) -> None:
        with pytest.raises(ValueError, match="알 수 없는 에이전트 타입"):
            await AgentRegistry.create("x", "nonexistent", {})


class TestGet:
    async def test_get_existing(self) -> None:
        await AgentRegistry.create("test-1", "fake", {})
        agent = AgentRegistry.get("test-1")
        assert agent is not None
        assert agent.agent_id == "test-1"

    def test_get_nonexistent(self) -> None:
        assert AgentRegistry.get("does-not-exist") is None


class TestGetByRole:
    async def test_get_writers(self) -> None:
        await AgentRegistry.create("writer-1", "fake", {})
        writers = AgentRegistry.get_by_role(AgentRole.WRITER)
        assert len(writers) == 1

    async def test_get_empty_role(self) -> None:
        await AgentRegistry.create("writer-1", "fake", {})
        managers = AgentRegistry.get_by_role(AgentRole.MANAGER)
        assert len(managers) == 0


class TestShutdownAll:
    async def test_shutdown_clears_instances(self) -> None:
        agent = await AgentRegistry.create("test-1", "fake", {})
        await AgentRegistry.shutdown_all()
        assert AgentRegistry.get("test-1") is None
        assert isinstance(agent, FakeAgent)
        assert agent._shutdown_called is True


class TestReset:
    async def test_reset_clears_everything(self) -> None:
        await AgentRegistry.create("test-1", "fake", {})
        AgentRegistry.reset()
        assert AgentRegistry.get("test-1") is None
        assert len(AgentRegistry._class_registry) == 0
