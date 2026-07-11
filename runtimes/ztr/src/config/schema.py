"""Pydantic V2 설정 모델.

agents.config.yaml의 구조를 타입 안전하게 정의한다.
ConfigDict(extra="forbid")로 오타 방지.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

_VALID_ROLE_BINDING_KEYS = {
    "discovery",
    "planner",
    "implementer",
    "implementer-reviewer",
    "reviewer",
    "mechanical",
    "human",
}


class AgentConfig(BaseModel):
    """단일 에이전트 설정.

    Attributes:
        id: 고유 식별자 (예: "ollama-local")
        type: 에이전트 타입 (예: "ollama")
        enabled: 활성화 여부
        priority: 우선순위 (높을수록 우선, 0~1000)
        roles: 수행 가능한 역할 목록
        config: 에이전트 타입별 상세 설정
    """
    model_config = ConfigDict(extra="forbid")

    id: str
    type: str
    enabled: bool = True
    priority: int = 50
    roles: list[str]
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("roles")
    @classmethod
    def validate_roles(cls, v: list[str]) -> list[str]:
        valid = {"writer", "critic", "manager", "summarizer"}
        for role in v:
            if role not in valid:
                raise ValueError(
                    f"유효하지 않은 역할: {role!r}. 허용: {sorted(valid)}"
                )
        return v

    @field_validator("priority")
    @classmethod
    def validate_priority(cls, v: int) -> int:
        if not (0 <= v <= 1000):
            raise ValueError(f"priority는 0~1000 사이여야 합니다: {v}")
        return v


class RoleBindingConfig(BaseModel):
    """MM 역할명 기반 백엔드 바인딩.

    Attributes:
        backend: 실행 백엔드 이름 (예: claude_cli, ollama)
        model: 기본 모델 이름
        call_type: 호출 방식 (headless, local, api, manual)
        l2_model: L2 이상에서 사용할 선택 모델
    """
    model_config = ConfigDict(extra="forbid")

    backend: str
    model: str
    call_type: str
    l2_model: str | None = None

    @field_validator("call_type")
    @classmethod
    def validate_call_type(cls, v: str) -> str:
        valid = {"headless", "local", "api", "manual"}
        if v not in valid:
            raise ValueError(
                f"유효하지 않은 call_type: {v!r}. 허용: {sorted(valid)}"
            )
        return v


class SessionConfig(BaseModel):
    """세션 DB 설정.

    Attributes:
        db_path: SQLite DB 파일 경로
        enable_metrics: 에이전트별 메트릭 기록 여부
        enable_session_log: 세션 로그 기록 여부
    """
    model_config = ConfigDict(extra="forbid")

    db_path: str = ".ztr/sessions.db"
    enable_metrics: bool = True
    enable_session_log: bool = True


class RoundtableConfig(BaseModel):
    """최상위 설정 모델.

    agents.config.yaml 전체 구조와 1:1 매핑.

    Attributes:
        roles: MM 역할명 기반 바인딩
        agents: 에이전트 설정 목록
        session: 세션 DB 설정
    """
    model_config = ConfigDict(extra="forbid")

    roles: dict[str, RoleBindingConfig] = Field(default_factory=dict)
    agents: list[AgentConfig]
    session: SessionConfig = Field(default_factory=SessionConfig)

    @field_validator("roles")
    @classmethod
    def validate_role_binding_keys(
        cls,
        value: dict[str, RoleBindingConfig],
    ) -> dict[str, RoleBindingConfig]:
        invalid = sorted(set(value) - _VALID_ROLE_BINDING_KEYS)
        if invalid:
            allowed = sorted(_VALID_ROLE_BINDING_KEYS)
            raise ValueError(
                f"유효하지 않은 역할 바인딩 키: {invalid}. 허용: {allowed}"
            )
        return value

    def get_agent(self, agent_id: str) -> AgentConfig | None:
        """ID로 에이전트 설정을 찾는다."""
        return next((a for a in self.agents if a.id == agent_id), None)

    def get_enabled_agents(self) -> list[AgentConfig]:
        """활성화된 에이전트 설정 목록을 반환한다."""
        return [a for a in self.agents if a.enabled]

    def get_role_binding(self, role_name: str) -> RoleBindingConfig | None:
        """MM 역할명으로 백엔드 바인딩을 찾는다."""
        return self.roles.get(role_name)
