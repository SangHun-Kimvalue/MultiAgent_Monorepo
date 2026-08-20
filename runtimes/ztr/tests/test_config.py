"""Config schema + loader 단위 테스트."""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.config.loader import load_config
from src.config.schema import (
    AgentConfig,
    RoleBindingConfig,
    RoundtableConfig,
)


class TestAgentConfig:
    def test_valid_creation(self) -> None:
        cfg = AgentConfig(
            id="test-1",
            type="ollama",
            roles=["writer", "critic"],
        )
        assert cfg.id == "test-1"
        assert cfg.enabled is True
        assert cfg.priority == 50

    def test_invalid_role_raises(self) -> None:
        with pytest.raises(ValidationError, match="유효하지 않은 역할"):
            AgentConfig(
                id="x",
                type="x",
                roles=["invalid_role"],
            )

    def test_invalid_priority_raises(self) -> None:
        with pytest.raises(ValidationError, match="0~1000"):
            AgentConfig(
                id="x",
                type="x",
                roles=["writer"],
                priority=9999,
            )

    def test_extra_field_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            AgentConfig(
                id="x",
                type="x",
                roles=["writer"],
                unknown_field="oops",  # type: ignore[call-arg]
            )


class TestRoleBindingConfig:
    def test_defaults(self) -> None:
        cfg = RoleBindingConfig(
            backend="claude_cli",
            model="sonnet",
            call_type="headless",
        )
        assert cfg.backend == "claude_cli"
        assert cfg.model == "sonnet"
        assert cfg.call_type == "headless"
        assert cfg.l2_model is None

    def test_l2_model(self) -> None:
        cfg = RoleBindingConfig(
            backend="claude_cli",
            model="sonnet",
            call_type="headless",
            l2_model="opus",
        )
        assert cfg.l2_model == "opus"

    def test_invalid_call_type_raises(self) -> None:
        with pytest.raises(ValidationError, match="call_type"):
            RoleBindingConfig(
                backend="x",
                model="y",
                call_type="invalid",
            )


class TestRoundtableConfig:
    def test_minimal_creation(self) -> None:
        cfg = RoundtableConfig(agents=[
            AgentConfig(id="a", type="t", roles=["writer"]),
        ])
        assert len(cfg.agents) == 1
        assert cfg.roles == {}

    def test_roles_creation(self) -> None:
        cfg = RoundtableConfig(
            roles={
                "implementer-reviewer": RoleBindingConfig(
                    backend="claude_cli",
                    model="sonnet",
                    call_type="headless",
                    l2_model="opus",
                )
            },
            agents=[AgentConfig(id="a", type="t", roles=["critic"])],
        )
        binding = cfg.get_role_binding("implementer-reviewer")
        assert binding is not None
        assert binding.l2_model == "opus"

    def test_invalid_role_binding_key_raises(self) -> None:
        with pytest.raises(ValidationError, match="역할 바인딩 키"):
            RoundtableConfig(
                roles={
                    "sonnet": RoleBindingConfig(
                        backend="claude_cli",
                        model="sonnet",
                        call_type="headless",
                    )
                },
                agents=[AgentConfig(id="a", type="t", roles=["critic"])],
            )

    def test_get_agent(self) -> None:
        cfg = RoundtableConfig(agents=[
            AgentConfig(id="a", type="t", roles=["writer"]),
            AgentConfig(id="b", type="t", roles=["critic"]),
        ])
        assert cfg.get_agent("a") is not None
        assert cfg.get_agent("a").id == "a"  # type: ignore[union-attr]
        assert cfg.get_agent("nonexistent") is None

    def test_get_enabled_agents(self) -> None:
        cfg = RoundtableConfig(agents=[
            AgentConfig(id="a", type="t", roles=["writer"], enabled=True),
            AgentConfig(id="b", type="t", roles=["critic"], enabled=False),
        ])
        enabled = cfg.get_enabled_agents()
        assert len(enabled) == 1
        assert enabled[0].id == "a"

    def test_round_trip(self) -> None:
        original = RoundtableConfig(agents=[
            AgentConfig(id="a", type="t", roles=["writer"]),
        ])
        data = original.model_dump()
        restored = RoundtableConfig.model_validate(data)
        assert restored.agents[0].id == original.agents[0].id


class TestLoader:
    def test_load_default_config(self) -> None:
        cfg = load_config()
        assert cfg.get_agent("nitpicker-prefilter") is not None
        assert cfg.get_agent("ollama-local") is not None
        assert cfg.get_agent("claude-primary") is None
        assert cfg.get_agent("gemini-writer") is None
        assert cfg.get_role_binding("implementer-reviewer") is not None
        assert cfg.get_role_binding("mechanical") is not None
        assert cfg.get_role_binding("mechanical").model == "qwen2.5-coder:7b"  # type: ignore[union-attr]

    def test_load_nonexistent_raises(self) -> None:
        with pytest.raises(FileNotFoundError, match="찾을 수 없습니다"):
            load_config("/nonexistent/path.yaml")

    def test_load_empty_file_raises(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.yaml"
        empty.write_text("", encoding="utf-8")
        with pytest.raises(ValueError, match="비어있습니다"):
            load_config(empty)

    def test_load_invalid_schema_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            'agents:\n  - id: "x"\n    type: "t"\n    roles: ["invalid"]\n',
            encoding="utf-8",
        )
        with pytest.raises(ValidationError):
            load_config(bad)

    def test_load_custom_path(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "test.yaml"
        cfg_file.write_text(
            'agents:\n'
            '  - id: "test"\n'
            '    type: "fake"\n'
            '    roles: ["writer"]\n',
            encoding="utf-8",
        )
        cfg = load_config(cfg_file)
        assert cfg.agents[0].id == "test"
