"""ZTR v2 stdout 계약 모델.

모든 ztr 명령은 후속 페이즈에서 이 envelope을 stdout 계약으로 사용한다.
이번 페이즈에서는 모델과 결정적 유틸리티만 제공하고 runner에는 배선하지 않는다.

**stdout 위치 계약**: envelope JSON은 명령 stdout의 **마지막 유효 JSON 라인**이다 — 소비자
(예: ACP ZtrRelayDriver)가 이 위치로 파싱하므로, envelope 출력 뒤에 다른 JSON 라인을 추가하는
변경은 계약 위반이다(전 run이 fail-closed BLOCKED가 됨).
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Verdict(str, Enum):
    """호출자가 파싱할 수 있는 유일한 verdict 값."""

    PASS = "PASS"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    BLOCKED = "BLOCKED"


EXIT_CODE_BY_VERDICT: dict[Verdict, int] = {
    Verdict.PASS: 0,
    Verdict.CHANGES_REQUESTED: 1,
    Verdict.BLOCKED: 2,
}
TIMEOUT_EXIT_CODE = 124
INTERNAL_ERROR_EXIT_CODE = 70

_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])")


def exit_code_for_verdict(verdict: Verdict) -> int:
    """verdict에 대응하는 프로세스 exit code를 반환한다."""
    return EXIT_CODE_BY_VERDICT[verdict]


def redact_stderr(stderr: str) -> str:
    """stderr의 32자 이상 토큰을 `[REDACTED]`로 치환한다."""
    return _TOKEN_RE.sub("[REDACTED]", stderr)


class Envelope(BaseModel):
    """ZTR v2 명령 결과 envelope.

    stdout/stderr 원문 의미를 코드가 해석하지 않도록 status와 exit_code만
    결정적 계약으로 노출한다.
    """

    model_config = ConfigDict(extra="forbid")

    status: Verdict
    exit_code: int
    backend: str
    model: str
    duration_s: float = Field(ge=0.0)
    stdout: str = ""
    stderr_sanitized: str = ""
    fallback_used: bool = False
    not_claimed: list[str] = Field(default_factory=list)

    _ALLOWED_EXIT_CODES: ClassVar[set[int]] = {
        *EXIT_CODE_BY_VERDICT.values(),
        TIMEOUT_EXIT_CODE,
        INTERNAL_ERROR_EXIT_CODE,
    }

    @field_validator("exit_code")
    @classmethod
    def validate_exit_code(cls, value: int) -> int:
        if value not in cls._ALLOWED_EXIT_CODES:
            allowed = sorted(cls._ALLOWED_EXIT_CODES)
            raise ValueError(f"지원하지 않는 exit_code입니다: {value}. 허용: {allowed}")
        return value

    @model_validator(mode="after")
    def validate_status_exit_code_pair(self) -> Envelope:
        """status와 exit_code가 서로 모순되지 않는지 검증한다."""
        if self.exit_code in {TIMEOUT_EXIT_CODE, INTERNAL_ERROR_EXIT_CODE}:
            if self.status != Verdict.BLOCKED:
                raise ValueError(
                    "timeout/internal exit_code는 BLOCKED status와만 함께 쓸 수 있습니다"
                )
            return self

        expected = exit_code_for_verdict(self.status)
        if self.exit_code != expected:
            raise ValueError(
                f"status {self.status.value}에는 exit_code {expected}만 허용됩니다"
            )
        return self

    @classmethod
    def from_verdict(
        cls,
        *,
        status: Verdict,
        backend: str,
        model: str,
        duration_s: float,
        stdout: str = "",
        stderr: str = "",
        fallback_used: bool = False,
        not_claimed: list[str] | None = None,
    ) -> Envelope:
        """verdict 기반 envelope을 생성한다."""
        return cls(
            status=status,
            exit_code=exit_code_for_verdict(status),
            backend=backend,
            model=model,
            duration_s=duration_s,
            stdout=stdout,
            stderr_sanitized=redact_stderr(stderr),
            fallback_used=fallback_used,
            not_claimed=not_claimed or [],
        )

    def as_stdout_payload(self) -> dict[str, Any]:
        """stdout JSON 직렬화에 사용할 순수 dict를 반환한다."""
        return self.model_dump(mode="json")
