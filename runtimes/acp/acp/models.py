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
    HOLDING = "holding"   # ZTR형 입력대기. 활동멈춤 + osPid 부재 + 미완료
    STALE = "stale"       # HOLDING이 stale_TTL 초과 → 좀비
    ERROR = "error"
    DONE = "done"
    UNKNOWN = "unknown"   # 파싱실패/스키마불일치 — silent fallback 금지(C3)


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
