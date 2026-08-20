"""OllamaAgent 단위 테스트.

실제 API 호출 없이 mock 기반으로 테스트한다.
"""
from __future__ import annotations

from src.agents.base import AgentRole, HealthStatus, Prompt
from src.agents.ollama import OllamaAgent


# ── OllamaAgent ──

class TestOllamaAgent:
    async def test_successful_init(self) -> None:
        agent = OllamaAgent()
        await agent.initialize({
            "id": "test-ollama",
            "endpoint": "http://localhost:11434",
            "model": "llama3.1:8b",
            "roles": ["manager", "summarizer"],
        })
        assert agent.agent_id == "test-ollama"
        assert AgentRole.MANAGER in agent.capabilities
        assert AgentRole.SUMMARIZER in agent.capabilities

    async def test_invoke_connection_error(self) -> None:
        """Ollama 서버가 안 돌아갈 때 깔끔한 에러 반환."""
        agent = OllamaAgent()
        await agent.initialize({
            "id": "test",
            "endpoint": "http://localhost:99999",  # 없는 포트
            "model": "test",
        })
        resp = await agent.invoke(Prompt(content="hello", role=AgentRole.MANAGER))
        assert resp.success is False
        assert "연결 실패" in resp.error or "error" in resp.error.lower()

    async def test_health_quarantined_when_offline(self) -> None:
        agent = OllamaAgent()
        await agent.initialize({
            "id": "test",
            "endpoint": "http://localhost:99999",
        })
        status = await agent.health_check()
        assert status == HealthStatus.QUARANTINED

    async def test_uninitialised_returns_error(self) -> None:
        agent = OllamaAgent()
        resp = await agent.invoke(Prompt(content="test", role=AgentRole.MANAGER))
        assert resp.success is False

    def test_rate_limit_unlimited(self) -> None:
        agent = OllamaAgent()
        s = agent.rate_limit_status()
        assert s.is_limited is False
        assert s.requests_remaining is None

    def test_auto_registration(self) -> None:
        from src.agents.registry import AgentRegistry
        AgentRegistry.register_class("ollama", OllamaAgent)
        assert "ollama" in AgentRegistry._class_registry
