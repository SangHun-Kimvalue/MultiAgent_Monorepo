"""NitpickerAgent 내부 static review 어댑터 테스트."""
from __future__ import annotations

import json

from src.agents.base import AgentRole, HealthStatus, Prompt
from src.agents.nitpicker import NitpickerAgent


class TestInitialize:
    async def test_successful_init_without_external_daemon(self) -> None:
        agent = NitpickerAgent()
        await agent.initialize({
            "id": "test-nitpicker",
            "mode": "prefilter",
            "roles": ["critic"],
            "target_project_path": ".",
        })
        assert agent.agent_id == "test-nitpicker"
        assert AgentRole.CRITIC in agent.capabilities


class TestInvoke:
    async def test_uninitialised_returns_error(self) -> None:
        agent = NitpickerAgent()
        response = await agent.invoke(Prompt(content="test", role=AgentRole.CRITIC))
        assert response.success is False
        assert "초기화" in response.error

    async def test_invoke_returns_review_payload(
        self,
        monkeypatch: object,
    ) -> None:
        from src.agents import nitpicker as nitpicker_mod
        from src.engine.static_review import StaticReviewReport, ToolReviewResult
        from src.envelope import Verdict

        async def fake_review(
            targets: list[str],
            **_: object,
        ) -> StaticReviewReport:
            assert targets == ["src/envelope.py"]
            return StaticReviewReport(
                tool_results={
                    "ruff": ToolReviewResult("ruff", _fake_run()),
                    "mypy": ToolReviewResult("mypy", _fake_run()),
                },
                findings=[],
                verdict=Verdict.PASS,
                exit_code=0,
                duration_s=0.01,
            )

        monkeypatch.setattr(nitpicker_mod, "run_static_review", fake_review)

        agent = NitpickerAgent()
        await agent.initialize({"id": "test", "target_project_path": "."})
        response = await agent.invoke(
            Prompt(
                content="review",
                role=AgentRole.CRITIC,
                context={"target_file": "src/envelope.py"},
            )
        )

        assert response.success is True
        payload = json.loads(response.content)
        assert payload["summary"]["verdict"] == "PASS"
        assert response.raw is not None
        assert response.raw["mode"] == "prefilter"


class TestHealthCheck:
    async def test_healthy_after_init(self) -> None:
        agent = NitpickerAgent()
        await agent.initialize({"id": "test", "target_project_path": "."})
        assert await agent.health_check() == HealthStatus.HEALTHY

    async def test_degraded_without_init(self) -> None:
        agent = NitpickerAgent()
        assert await agent.health_check() == HealthStatus.DEGRADED


class TestRegistration:
    def test_auto_registration(self) -> None:
        from src.agents.registry import AgentRegistry

        AgentRegistry.register_class("nitpicker", NitpickerAgent)
        assert "nitpicker" in AgentRegistry._class_registry


def _fake_run() -> object:
    from src.engine.static_review import ToolRun

    return ToolRun(
        command=(),
        exit_code=0,
        stdout="",
        stderr_sanitized="",
        duration_s=0.0,
    )
