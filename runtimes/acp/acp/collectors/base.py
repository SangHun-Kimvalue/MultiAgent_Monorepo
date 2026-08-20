"""acp/collectors/base.py — BaseCollector 추상 경계 + 수집 사이클 공유 타입.

규칙(C7):
- collect()는 read-only. 절대 외부 파일/DB 수정 금지.
- 예외는 삼키지 않고 전파 또는 로깅(C3). 호출자가 처리.
- 새 앱 = 이 ABC를 구현하는 클래스 1개 추가. Core는 무변경.

**완결성은 수집기 경계가 말한다(T14 S4b D2).** `collect()`가 예외 없이 반환했다는 사실은
"수집이 완결됐다"를 뜻하지 않는다 — 이 계약은 전면 실패를 `[]` 반환으로 표현하는 것을
허용하고, 실제로 cursor·codex가 그렇게 한다. 그래서 폴러가 "예외 없음 → 정상"으로 번역하면
**전면 수집 실패가 건강 단언으로 세탁**된다. 완결성은 소스 부재를 아는 수집기만 말할 수
있으므로 `last_cycle`을 **추상 프로퍼티로 요구**한다.
"""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from acp.models import SessionRecord

logger = logging.getLogger(__name__)

# 사이클이 세는 축. `collector_cycles`의 카운트 컬럼과 **1:1**이어야 한다
# (테스트가 이 대응을 고정한다). 축을 늘리면 scope도 반드시 함께 늘어난다.
COUNT_AXES: tuple[str, ...] = (
    "collected",
    "failed",
    "excluded_archived",
    "matched",
    "unmatched",
    "ambiguous",
    "malformed_uuid",
)

# 축별 **지식 범위**. 단일 플래그로는 "collected는 아는데 failed는 모름"을 표현할 수 없다.
SCOPE_DECLARED = "declared"              # 수집기가 실제로 센 값 → 숫자 그대로 노출
SCOPE_UNKNOWN = "unknown"                # 세려 했으나 못 셌다 → null
SCOPE_NOT_APPLICABLE = "not_applicable"  # 이 앱에 해당 축이 없다 → null (unknown과 다른 사실)
COUNT_SCOPE_VALUES: frozenset[str] = frozenset(
    {SCOPE_DECLARED, SCOPE_UNKNOWN, SCOPE_NOT_APPLICABLE}
)

# 실행 신호를 **줄 수 있는가**(구조적 가능성, T14 S4c-1 D1). 이번 사이클의 관측 결과인
# `process_signal`과 다른 축이다 — 섞으면 "줄 수 없는 앱"과 "이번엔 못 얻은 앱"이 같아진다.
CAPABILITY_SUPPORTED = "supported"        # 이 앱은 실행 신호를 줄 수 있다
CAPABILITY_UNSUPPORTED = "unsupported"    # 구조적으로 줄 수 없다(예: 워크스페이스 파일만 읽는 앱)
CAPABILITY_UNKNOWN = "unknown"            # 선언되지 않았다 — 기본값
PROCESS_SIGNAL_CAPABILITIES: frozenset[str] = frozenset(
    {CAPABILITY_SUPPORTED, CAPABILITY_UNSUPPORTED, CAPABILITY_UNKNOWN}
)

# 이번 사이클에 실행 신호 소스를 **관측했는가**(T14 S4c-1 D1b).
PROCESS_SIGNAL_OK = "ok"                          # 정상 관측(0건이어도 관측은 했다)
PROCESS_SIGNAL_UNAVAILABLE = "unavailable"        # 소스 부재·형식 오류·읽기 실패
PROCESS_SIGNAL_UNKNOWN = "unknown"                # 아무도 선언하지 않았다 — 기본값
PROCESS_SIGNAL_NOT_APPLICABLE = "not_applicable"  # capability가 unsupported라 축이 무의미
PROCESS_SIGNAL_VALUES: frozenset[str] = frozenset(
    {
        PROCESS_SIGNAL_OK,
        PROCESS_SIGNAL_UNAVAILABLE,
        PROCESS_SIGNAL_UNKNOWN,
        PROCESS_SIGNAL_NOT_APPLICABLE,
    }
)

# 수집기가 완결성에 대해 말할 수 있는 값.
STATUS_SUCCESS = "success_complete"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
# 폴러가 계약 위반 수집기를 만났을 때만 쓰는 방어값. **수집기는 쓰지 않는다.**
STATUS_COMPLETED_UNKNOWN = "completed_unknown"

SOURCE_DECLARED = "declared"
SOURCE_SYNTHESIZED = "synthesized"


def all_unknown_scopes() -> dict[str, str]:
    """모든 축을 `unknown`으로. 확인 못 한 것을 0으로 단언하지 않기 위한 기본값."""
    return {axis: SCOPE_UNKNOWN for axis in COUNT_AXES}


class CountScopeError(ValueError):
    """`count_scopes` 계약 위반. 조용히 넘기지 않는다(fail-closed)."""


def validate_count_scopes(scopes: dict[str, Any], counts: dict[str, Any]) -> dict[str, str]:
    """쓰기 직전 계약 검증. 위반은 예외 — 조용한 기본값 승격 경로를 만들지 않는다.

    검사:
      1. 키 집합이 `COUNT_AXES`와 **정확히 일치**(누락도 여분도 거부).
      2. 값이 enum 안에 있다.
      3. `declared`인 축은 **값이 실제로 제공**된다 — 없거나 None이거나 정수가 아니거나
         음수면 거부. `dict.get(axis, 0)` 폴백이 **재지 않은 0을 확인된 0으로 승격**하던
         경로를 여기서 끊는다(T14 S4b D2c).

    `bool`은 `int`의 하위 타입이라 `isinstance`로는 True/False가 1/0으로 통과한다 →
    `type(...) is int`로 못박는다.
    """
    if set(scopes) != set(COUNT_AXES):
        missing = sorted(set(COUNT_AXES) - set(scopes))
        extra = sorted(set(scopes) - set(COUNT_AXES))
        raise CountScopeError(f"count_scopes 키 불일치: 누락={missing} 여분={extra}")
    for axis, scope in scopes.items():
        if scope not in COUNT_SCOPE_VALUES:
            raise CountScopeError(f"알 수 없는 count scope: {axis}={scope!r}")
        if scope != SCOPE_DECLARED:
            continue
        value = counts.get(axis)
        if type(value) is not int or value < 0:
            raise CountScopeError(
                f"'{axis}'는 declared인데 값이 없거나 유효하지 않다: {value!r}"
            )
    return {axis: str(scopes[axis]) for axis in COUNT_AXES}


def parse_count_scopes(raw: Any, *, context: str = "") -> dict[str, str]:
    """저장된 JSON → 축별 scope. **읽기는 fail-closed**.

    `NULL`(마이그레이션 이전 행)·파싱 실패·계약 위반은 전부 **전 축 `unknown`**으로 읽는다.
    절대 `declared`로 보정하지 않는다 — 확인 못 한 것을 확인된 값으로 승격하는 것이
    이 트랙이 고쳐 온 결함 그 자체다. 진단이 가능하도록 사유와 대상을 로그에 남긴다.
    """
    if raw is None:
        # 마이그레이션 이전 행. 정직한 처리지만 **조용히** 하지 않는다 — 전 축을
        # unknown으로 내리는 정책은 진단 범위가 넓어, 사유를 남기지 않으면 원인을 못 짚는다.
        logger.warning("count_scopes(null) 전 축 unknown 처리 [%s]", context or "?")
        return all_unknown_scopes()
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        logger.warning("count_scopes(parse-fail) 전 축 unknown 처리 [%s]", context or "?")
        return all_unknown_scopes()
    if not isinstance(parsed, dict) or set(parsed) != set(COUNT_AXES):
        logger.warning("count_scopes(key-mismatch) 전 축 unknown 처리 [%s]", context or "?")
        return all_unknown_scopes()
    if any(value not in COUNT_SCOPE_VALUES for value in parsed.values()):
        logger.warning("count_scopes(bad-enum) 전 축 unknown 처리 [%s]", context or "?")
        return all_unknown_scopes()
    return {axis: str(parsed[axis]) for axis in COUNT_AXES}


@dataclass
class CollectCycle:
    """수집 사이클의 **사실**(T14 S3 D4 · S4b에서 공유 타입으로 승격).

    `success_complete, collected=0`(정말 세션이 없음)과 `failed`(관측 실패)는 절대 같은
    값이 아니다. 이 구분이 없으면 후속 `missing` 판정이 관측 실패를 데이터 부재로 오인한다.

    카운트 필드는 전부 `int`지만 **그 값을 믿어도 되는지는 `count_scopes`가 말한다**.
    선언하지 않은 축의 0은 "0건"이 아니라 "모름"이다.
    """

    app: str
    status: str = STATUS_SUCCESS
    collected: int = 0
    failed: int = 0
    # 이번 스캔에서 **본** archived 파일 수(T14 S4a 이후 "제외"가 아니다 — 저장은 되고
    # 플래그가 붙는다). DB 컬럼 이름은 append-only 감사 이력이라 유지한다.
    excluded_archived: int = 0
    # **기본값은 `unknown`이다**(T14 S4c-1 D1b). 예전 기본값 `"ok"`는 아무도 선언하지
    # 않아도 "정상 관측"으로 읽혀, 실행 신호 소스를 읽지 못한 사이클이 건강한 사이클과
    # 구분되지 않았다 — 미확인이 사실로 승격되던 경로다.
    process_signal: str = PROCESS_SIGNAL_UNKNOWN
    # 구조적 가능성. 선언하지 않으면 `unknown` — `supported`로 두면 "줄 수 있는데 안 왔다"
    # (=안 돌고 있음)로 읽힌다.
    process_signal_capability: str = CAPABILITY_UNKNOWN
    # 실행 신호 조인의 정직성 카운트(T14 S3 D2). 조용히 버리지 않는다.
    matched: int = 0
    unmatched: int = 0
    ambiguous: int = 0
    malformed_uuid: int = 0
    signal_quality: str = "session-file"
    process_observed_at: datetime | None = None
    observed_at: datetime | None = None
    # 축별 지식 범위. 기본은 **전 축 unknown** — 선언하지 않으면 아무 것도 주장하지 않는다.
    count_scopes: dict[str, str] = field(default_factory=all_unknown_scopes)
    # 이 사이클을 누가 만들었나. `declared`=수집기, `synthesized`=폴러 방어선.
    source: str = SOURCE_DECLARED

    def declare(self, **counts: int) -> None:
        """센 축만 값과 함께 선언한다. 선언하지 않은 축은 `unknown`으로 남는다."""
        for axis, value in counts.items():
            if axis not in COUNT_AXES:
                raise CountScopeError(f"알 수 없는 카운트 축: {axis}")
            setattr(self, axis, value)
            self.count_scopes[axis] = SCOPE_DECLARED

    def mark_not_applicable(self, *axes: str) -> None:
        """이 앱에 존재하지 않는 축을 표시한다. `unknown`(못 셈)과 다른 사실이다."""
        for axis in axes:
            if axis not in COUNT_AXES:
                raise CountScopeError(f"알 수 없는 카운트 축: {axis}")
            self.count_scopes[axis] = SCOPE_NOT_APPLICABLE

    def as_row(self) -> dict[str, object]:
        return {
            "app": self.app,
            "status": self.status,
            "collected": self.collected,
            "failed": self.failed,
            "excluded_archived": self.excluded_archived,
            "process_signal": self.process_signal,
            "process_signal_capability": self.process_signal_capability,
            "matched": self.matched,
            "unmatched": self.unmatched,
            "ambiguous": self.ambiguous,
            "malformed_uuid": self.malformed_uuid,
            "signal_quality": self.signal_quality,
            "process_observed_at": (
                self.process_observed_at.isoformat() if self.process_observed_at else None
            ),
            "observed_at": (self.observed_at or datetime.now(timezone.utc)).isoformat(),
            "count_scopes": dict(self.count_scopes),
            "source": self.source,
        }


class BaseCollector(ABC):
    """앱별 세션 수집기 추상 경계."""

    @property
    @abstractmethod
    def app_name(self) -> str:
        """수집기가 담당하는 앱 이름 (예: 'codex', 'claude', 'cursor')."""
        ...

    @property
    @abstractmethod
    def last_cycle(self) -> CollectCycle:
        """직전 `collect()`의 사이클 사실.

        **추상이다**(T14 S4b D2a). 선택 계약이던 시절에는 선언하지 않은 수집기가 침묵으로
        통과했고, 침묵은 소비자에게 "건강함"으로 읽혔다. 완결성은 소스 부재를 아는
        수집기만 말할 수 있으므로 여기서 요구한다.

        모르는 축은 **선언하지 않는다** — `declare()`로 센 것만 밝히고 나머지는 `unknown`.
        """
        ...

    @abstractmethod
    def collect(self) -> list[SessionRecord]:
        """앱 로컬 아티팩트를 읽어 정규화된 SessionRecord 목록을 반환.

        - read-only: 파일/DB 수정 금지.
        - 파싱 실패한 레코드는 건너뛰되 로깅(C3)하고 **`last_cycle`에 건수를 남긴다**.
        - 전체 실패(파일 접근 불가 등)는 예외를 전파하거나 `[]`를 반환하되,
          후자라면 **`last_cycle.status`를 `failed`로 선언해야 한다**(빈 결과로 축약 금지).
        - 반환 리스트는 비어있어도 OK.
        """
        ...
