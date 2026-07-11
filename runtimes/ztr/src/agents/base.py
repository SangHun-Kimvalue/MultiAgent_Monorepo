"""IAgent ABC + 공통 타입 정의.

모든 에이전트 구현체의 기반이 되는 추상 클래스와
Prompt, AgentResponse 등 공유 데이터 타입을 정의한다.

순환 import 방지:
    이 모듈은 registry.py를 import하지 않는다.
    대신 _DEFERRED_REGISTRATIONS 버퍼를 두고,
    registry.py의 AgentRegistry.discover()가 이를 드레인한다.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar

logger = logging.getLogger(__name__)


# ── Enums ──


class HealthStatus(Enum):
    """에이전트 상태."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"          # 응답 느림, 간헐적 에러
    QUARANTINED = "quarantined"    # 사용 불가 (rate limit, 장애 등)


class AgentRole(Enum):
    """에이전트가 수행할 수 있는 역할."""
    WRITER = "writer"
    CRITIC = "critic"
    MANAGER = "manager"
    SUMMARIZER = "summarizer"


# ── Data Types ──


@dataclass(frozen=True)
class Prompt:
    """에이전트에게 전달할 프롬프트.

    Attributes:
        content: 프롬프트 텍스트
        role: 이 프롬프트에서 기대하는 에이전트 역할
        context: issue_id, diff, decisions 등 구조화된 맥락
    """
    content: str
    role: AgentRole = AgentRole.WRITER
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResponse:
    """에이전트 응답.

    Attributes:
        content: 응답 텍스트 (코드, 리뷰, 요약 등)
        agent_id: 응답한 에이전트의 고유 ID
        role: 이 응답에서의 역할
        success: 호출 성공 여부
        tokens_used: 사용된 토큰 수 (제공 가능한 경우)
        latency_ms: 응답 소요 시간 (밀리초)
        raw: 원본 응답 데이터 (디버깅용)
        error: 실패 시 에러 메시지
    """
    content: str
    agent_id: str
    role: AgentRole
    success: bool = True
    tokens_used: int | None = None
    latency_ms: float = 0.0
    raw: dict[str, Any] | None = None
    error: str = ""


@dataclass
class RateLimitStatus:
    """Rate limit 현황.

    Attributes:
        requests_remaining: 남은 요청 수 (None = 무제한)
        requests_per_minute: 분당 제한
        requests_per_day: 일당 제한
        reset_at: 리셋 시각 (Unix timestamp)
        is_limited: 현재 제한 상태 여부
    """
    requests_remaining: int | None = None
    requests_per_minute: int | None = None
    requests_per_day: int | None = None
    reset_at: float | None = None
    is_limited: bool = False


# ── Deferred Registration Buffer ──

# __init_subclass__에서 등록된 에이전트 클래스를 임시 보관.
# AgentRegistry.discover()가 이 리스트를 드레인하여 실제 등록한다.
# 이 패턴으로 base.py ↔ registry.py 순환 import을 방지.
_DEFERRED_REGISTRATIONS: list[tuple[str, type[IAgent]]] = []


# ── IAgent ABC ──


class IAgent(ABC):
    """에이전트 추상 기반 클래스.

    새 에이전트 추가 절차:
        1. 이 클래스를 상속
        2. agent_type 클래스 변수를 고유 문자열로 설정
        3. 모든 추상 메서드 구현
        4. 끝 — __init_subclass__가 자동으로 등록함 (OCP)

    Example:
        class MyAgent(IAgent):
            agent_type = "my_custom_agent"

            async def invoke(self, prompt: Prompt) -> AgentResponse:
                ...
    """

    agent_type: ClassVar[str] = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.agent_type:
            _DEFERRED_REGISTRATIONS.append((cls.agent_type, cls))
            logger.debug(
                "에이전트 클래스 등록 대기: type=%s, class=%s",
                cls.agent_type, cls.__name__,
            )

    @abstractmethod
    async def initialize(self, config: dict[str, Any]) -> None:
        """설정 기반 초기화. 실패 시 예외 발생."""
        ...

    @abstractmethod
    async def shutdown(self) -> None:
        """Graceful shutdown. in-flight 요청 대기 후 리소스 정리."""
        ...

    @abstractmethod
    async def invoke(self, prompt: Prompt) -> AgentResponse:
        """프롬프트를 에이전트에 전달하고 응답을 받는다.

        타임아웃, 에러 처리를 내부에서 수행하며,
        실패 시에도 AgentResponse(success=False)를 반환한다 (예외 발생 안 함).
        """
        ...

    @abstractmethod
    async def health_check(self) -> HealthStatus:
        """현재 에이전트 상태를 확인한다."""
        ...

    @abstractmethod
    def rate_limit_status(self) -> RateLimitStatus:
        """현재 rate limit 현황을 반환한다."""
        ...

    @property
    @abstractmethod
    def agent_id(self) -> str:
        """고유 식별자 (설정 파일의 id 필드)."""
        ...

    @property
    @abstractmethod
    def capabilities(self) -> set[AgentRole]:
        """이 에이전트가 수행 가능한 역할 집합."""
        ...

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} id={self.agent_id!r} type={self.agent_type!r}>"
