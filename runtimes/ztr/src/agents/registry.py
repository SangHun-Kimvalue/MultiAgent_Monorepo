"""AgentFactory + AgentRegistry.

에이전트 클래스 등록과 런타임 인스턴스 관리를 담당한다.

핵심 설계 결정:
    - __init_subclass__로 자동 등록 (OCP)
    - _DEFERRED_REGISTRATIONS 버퍼로 순환 import 방지
    - asyncio.Lock lazy 초기화로 이벤트 루프 없이도 import 가능
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, ClassVar

from src.agents.base import (
    AgentRole,
    IAgent,
    _DEFERRED_REGISTRATIONS,
)

logger = logging.getLogger(__name__)


class AgentRegistry:
    """에이전트 클래스 등록 + 런타임 인스턴스 관리.

    모든 메서드가 classmethod이며, 전역 싱글톤으로 동작한다.
    asyncio.Lock으로 동시 생성을 방지한다.

    Usage:
        # 1. 에이전트 모듈 import (자동 등록 트리거)
        import src.agents.ollama  # noqa: F401

        # 2. deferred 등록 드레인
        AgentRegistry.discover()

        # 3. 인스턴스 생성
        agent = await AgentRegistry.create("ollama-local", "ollama", config)

        # 4. 조회
        writers = AgentRegistry.get_by_role(AgentRole.WRITER)

        # 5. 정리
        await AgentRegistry.shutdown_all()
    """

    # 타입 → 클래스 매핑 (discover()로 채워짐)
    _class_registry: ClassVar[dict[str, type[IAgent]]] = {}

    # ID → 인스턴스 매핑 (create()로 채워짐)
    _instances: ClassVar[dict[str, IAgent]] = {}

    # asyncio.Lock은 이벤트 루프 안에서만 생성 가능하므로 lazy init
    _lock: ClassVar[asyncio.Lock | None] = None

    @classmethod
    def _get_lock(cls) -> asyncio.Lock:
        """Lazy lock 초기화.

        Python 3.12+에서 asyncio.Lock()을 이벤트 루프 밖에서
        호출하면 RuntimeError가 발생한다. 이를 방지하기 위해
        첫 사용 시점(항상 async 컨텍스트)에 생성한다.
        """
        if cls._lock is None:
            cls._lock = asyncio.Lock()
        return cls._lock

    @classmethod
    def register_class(cls, agent_type: str, klass: type[IAgent]) -> None:
        """에이전트 클래스를 타입 이름으로 등록한다.

        주로 __init_subclass__에서 자동 호출되며,
        테스트에서 FakeAgent 등록 시에도 직접 호출 가능.
        """
        if agent_type in cls._class_registry:
            existing = cls._class_registry[agent_type]
            if existing is not klass:
                logger.warning(
                    "에이전트 타입 %r 재등록: %s → %s",
                    agent_type, existing.__name__, klass.__name__,
                )
        cls._class_registry[agent_type] = klass
        logger.debug("에이전트 클래스 등록: %s → %s", agent_type, klass.__name__)

    @classmethod
    def discover(cls) -> int:
        """_DEFERRED_REGISTRATIONS에서 대기 중인 클래스들을 드레인한다.

        Returns:
            새로 등록된 클래스 수.
        """
        count = 0
        while _DEFERRED_REGISTRATIONS:
            agent_type, klass = _DEFERRED_REGISTRATIONS.pop(0)
            cls.register_class(agent_type, klass)
            count += 1
        if count:
            logger.info("discover: %d개 에이전트 클래스 등록 완료", count)
        return count

    @classmethod
    async def create(
        cls,
        agent_id: str,
        agent_type: str,
        config: dict[str, Any],
    ) -> IAgent:
        """설정 기반으로 에이전트 인스턴스를 생성하고 초기화한다.

        같은 agent_id로 재호출 시 캐시된 인스턴스를 반환 (idempotent).

        Args:
            agent_id: 고유 식별자
            agent_type: _class_registry에 등록된 타입 이름
            config: 에이전트별 설정 dict

        Returns:
            초기화 완료된 IAgent 인스턴스.

        Raises:
            ValueError: 알 수 없는 agent_type.
        """
        async with cls._get_lock():
            if agent_id in cls._instances:
                logger.debug("캐시된 인스턴스 반환: %s", agent_id)
                return cls._instances[agent_id]

            klass = cls._class_registry.get(agent_type)
            if klass is None:
                available = list(cls._class_registry.keys())
                raise ValueError(
                    f"알 수 없는 에이전트 타입: {agent_type!r}. "
                    f"등록된 타입: {available}"
                )

            instance = klass()
            merged_config = {"id": agent_id, **config}
            await instance.initialize(merged_config)
            cls._instances[agent_id] = instance
            logger.info("에이전트 생성 완료: %s (type=%s)", agent_id, agent_type)
            return instance

    @classmethod
    def get(cls, agent_id: str) -> IAgent | None:
        """ID로 활성 에이전트 인스턴스를 조회한다."""
        return cls._instances.get(agent_id)

    @classmethod
    def get_by_role(cls, role: AgentRole) -> list[IAgent]:
        """특정 역할을 수행 가능한 활성 에이전트 목록을 반환한다."""
        return [
            agent for agent in cls._instances.values()
            if role in agent.capabilities
        ]

    @classmethod
    def get_all(cls) -> list[IAgent]:
        """모든 활성 에이전트를 반환한다."""
        return list(cls._instances.values())

    @classmethod
    async def shutdown_all(cls) -> None:
        """모든 에이전트의 graceful shutdown을 수행한다."""
        async with cls._get_lock():
            for agent_id, agent in cls._instances.items():
                try:
                    await agent.shutdown()
                    logger.debug("에이전트 종료: %s", agent_id)
                except Exception:
                    logger.exception("에이전트 종료 실패: %s", agent_id)
            cls._instances.clear()
            logger.info("모든 에이전트 종료 완료")

    @classmethod
    def reset(cls) -> None:
        """테스트용: 모든 레지스트리 상태를 초기화한다.

        WARNING: 프로덕션에서 호출하면 안 됨.
        인스턴스의 shutdown()을 호출하지 않는다.
        """
        cls._class_registry.clear()
        cls._instances.clear()
        cls._lock = None
        _DEFERRED_REGISTRATIONS.clear()
