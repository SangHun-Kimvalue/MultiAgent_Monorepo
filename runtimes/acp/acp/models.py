"""acp/models.py — 공개 데이터 모델 (PHASE.md §2 시그니처와 1:1).

변경 시 PHASE.md §2 공개 API 결정 로그도 함께 갱신할 것.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class SessionState(StrEnum):
    """세션 생존 상태 — design.md 상태전이 테이블과 동기."""
    LIVE = "live"
    RUNNING = "running"
    IDLE = "idle"
    # 무응답(stalled): hold 임계를 넘도록 소식이 없음. 사람의 응답을 기다리는 상태로
    # 읽으면 안 된다 — 실측 어휘에 approval 계열 이벤트가 없어 승인 분기는
    # 발화하지 않는다(T14 S2 D5).
    # 사람이 지금 확인해야 하는 축(action)에 속한다.
    HOLDING = "holding"
    # 사실상 종료된 좀비. stale_ttl 초과. 조치 대상이 아니라 **정리 대상**(cleanup 축).
    STALE = "stale"
    ERROR = "error"
    DONE = "done"
    UNKNOWN = "unknown"   # 파싱실패/스키마불일치 — silent fallback 금지(C3)


def state_vocabulary() -> tuple[str, ...]:
    """서버가 **아는** 상태 어휘(계약 어휘) — 단일 출처(T14 S4c-2 D6).

    필터 검증·응답 노출·422의 `accepted`가 전부 여기서 나온다. 두 곳에서 만들면
    "필터는 받는데 목록에는 없는" 상태가 생긴다 — 대시보드가 겪던 결함을 서버 안에
    옮겨 심는 꼴이다.

    **이것은 "저장소에 있는 상태"가 아니다.** 그 사실은 `observed_states`가 말한다.
    """
    return tuple(state.value for state in SessionState)


class SessionRecord(BaseModel):
    """수집기가 생산하는 정규화 레코드.

    모든 필드는 Optional — 앱마다 줄 수 없는 필드가 다름.
    None = 아직 모름(UNKNOWN으로 처리). 추측 채움 금지(C1).
    """
    model_config = ConfigDict(frozen=True)

    schema_version: str = "1.0"
    app: str                          # "codex" | "claude" | "cursor" | "fake"
    session_id: str                   # 앱 네이티브 id (Codex=conversationId 등)
    project_path: str | None = None   # cwd 또는 workspace 폴더
    model: str | None = None          # LLM 모델명
    last_activity: datetime | None = None  # 마지막 활동 시각
    running_pid: int | None = None    # 실행 중인 osPid (Codex chat_processes)
    running_cmd: str | None = None    # 실행 중인 명령어 (Codex chat_processes)
    raw_status: str | None = None     # 앱이 준 원시 상태 표식
    last_event: str | None = None     # jsonl 마지막 event_msg payload.type (Codex 전용)
    source_file: str = ""             # 수집 아티팩트 경로 (감사용)
    # 사용자가 원본 앱에서 보관(archive)한 세션인가(T14 S4a D2).
    # 수집기는 archived를 **건너뛰지 않고** 이 플래그로 표시해 넘긴다 — 건너뛰면 이미
    # 저장된 과거 행이 영원히 분류되지 않아 총계가 거짓이 된다.
    archived: bool = False
