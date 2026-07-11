"""OllamaAgent — 로컬 Ollama REST API를 호출하는 에이전트.

완전 무료, rate limit 없음. 로컬 sLLM(qwen2.5-coder:7b 등) 활용.
Manager/Summarizer 역할에 적합 (빠르고 무료).
"""
from __future__ import annotations

import logging
import time
from typing import Any, ClassVar

import httpx

from src.agents.base import (
    AgentResponse,
    AgentRole,
    HealthStatus,
    IAgent,
    Prompt,
    RateLimitStatus,
)

logger = logging.getLogger(__name__)


class OllamaAgent(IAgent):
    """로컬 Ollama REST API 에이전트.

    http://localhost:11434/api/generate 엔드포인트를 사용한다.
    완전 무료이며 rate limit이 없다.
    """

    agent_type: ClassVar[str] = "ollama"

    def __init__(self) -> None:
        self._agent_id = ""
        self._endpoint = "http://localhost:11434"
        self._model = "qwen2.5-coder:7b"
        self._timeout_sec = 60.0
        self._roles: set[AgentRole] = set()
        self._initialized = False

    async def initialize(self, config: dict[str, Any]) -> None:
        """설정 기반 초기화.

        config 필드:
            endpoint: Ollama 서버 주소 (기본: http://localhost:11434)
            model: 모델 이름 (기본: qwen2.5-coder:7b)
            timeout_sec: 타임아웃 (기본: 60)
        """
        self._agent_id = config.get("id", "ollama-unknown")
        self._endpoint = config.get("endpoint", "http://localhost:11434").rstrip("/")
        self._model = config.get("model", "qwen2.5-coder:7b")
        self._timeout_sec = float(config.get("timeout_sec", 60))

        role_names = config.get("roles", ["manager", "summarizer"])
        role_map = {r.value: r for r in AgentRole}
        self._roles = {role_map[n] for n in role_names if n in role_map}

        self._initialized = True
        logger.info(
            "OllamaAgent 초기화: id=%s, endpoint=%s, model=%s",
            self._agent_id, self._endpoint, self._model,
        )

    async def shutdown(self) -> None:
        self._initialized = False

    async def invoke(self, prompt: Prompt) -> AgentResponse:
        """Ollama API를 호출하고 응답을 반환한다."""
        start = time.monotonic()

        if not self._initialized:
            return AgentResponse(
                content="", agent_id=self._agent_id,
                role=prompt.role, success=False,
                error="OllamaAgent가 초기화되지 않았습니다",
            )

        # 시스템 프롬프트 처리
        system_prompt = prompt.context.get("system_prompt", "")

        try:
            async with httpx.AsyncClient(timeout=self._timeout_sec) as client:
                payload: dict[str, Any] = {
                    "model": self._model,
                    "prompt": prompt.content,
                    "stream": False,
                }
                if system_prompt:
                    payload["system"] = system_prompt

                resp = await client.post(
                    f"{self._endpoint}/api/generate",
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()

        except httpx.ConnectError as exc:
            elapsed = (time.monotonic() - start) * 1000
            return AgentResponse(
                content="", agent_id=self._agent_id,
                role=prompt.role, success=False,
                latency_ms=elapsed,
                error=f"Ollama 연결 실패: {self._endpoint} — {exc}",
            )
        except httpx.TimeoutException:
            elapsed = (time.monotonic() - start) * 1000
            return AgentResponse(
                content="", agent_id=self._agent_id,
                role=prompt.role, success=False,
                latency_ms=elapsed,
                error=f"Ollama 타임아웃: {self._timeout_sec}초 초과",
            )
        except httpx.HTTPStatusError as exc:
            elapsed = (time.monotonic() - start) * 1000
            return AgentResponse(
                content="", agent_id=self._agent_id,
                role=prompt.role, success=False,
                latency_ms=elapsed,
                error=f"Ollama HTTP 에러: {exc.response.status_code}",
            )

        elapsed = (time.monotonic() - start) * 1000
        result_text = data.get("response", "")

        # 토큰 추출
        tokens_used = None
        prompt_eval = data.get("prompt_eval_count", 0)
        eval_count = data.get("eval_count", 0)
        if prompt_eval or eval_count:
            tokens_used = prompt_eval + eval_count

        return AgentResponse(
            content=result_text,
            agent_id=self._agent_id,
            role=prompt.role,
            success=True,
            tokens_used=tokens_used,
            latency_ms=elapsed,
            raw={"model": self._model, "total_duration": data.get("total_duration")},
        )

    async def health_check(self) -> HealthStatus:
        """Ollama 서버의 /api/tags 엔드포인트로 상태 확인."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self._endpoint}/api/tags")
                if resp.status_code == 200:
                    return HealthStatus.HEALTHY
                return HealthStatus.DEGRADED
        except (httpx.ConnectError, httpx.TimeoutException):
            return HealthStatus.QUARANTINED

    def rate_limit_status(self) -> RateLimitStatus:
        """로컬 실행이므로 rate limit 없음."""
        return RateLimitStatus()

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def capabilities(self) -> set[AgentRole]:
        return self._roles
