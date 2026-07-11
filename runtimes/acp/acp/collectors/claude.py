"""acp/collectors/claude.py — Claude Desktop(claude-code-sessions) 세션 수집기 (P2).

소스: %APPDATA%/Claude/claude-code-sessions/**/local_*.json
실측 형상(2026-06-10): top-level 키
  sessionId · cliSessionId · cwd · originCwd · createdAt · lastActivityAt(ms epoch)
  · model · isArchived · title · planPath · completedTurns

불변 원칙:
  - read-only. 앱 파일 수정 금지.
  - 개별 파일 파싱 실패 → 그 레코드만 skip + 경고. 전체 수집 중단 금지(C3).
  - running_pid/cmd 없음(Claude는 프로세스 레지스트리 없음).
  - last_event 없음(Codex 전용 신호) → derive_state는 시간기반 폴백으로 처리.
  - title/completedTurns/planPath 등 부가 필드는 미수집(YAGNI — 보드 가치는 PHASE 조인에서).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from acp.collectors.base import BaseCollector
from acp.models import SessionRecord
from acp.timeutil import ms_to_dt

logger = logging.getLogger(__name__)


class ClaudeSessionCollector(BaseCollector):
    """Claude claude-code-sessions 수집기.

    sessions_base: %APPDATA%/Claude/claude-code-sessions
    """

    def __init__(self, sessions_base: Path) -> None:
        self._sessions_base = sessions_base

    @property
    def app_name(self) -> str:
        return "claude"

    def collect(self) -> list[SessionRecord]:
        if not self._sessions_base.exists():
            logger.warning("Claude sessions 폴더 없음: %s", self._sessions_base)
            return []

        records: list[SessionRecord] = []
        for jf in self._sessions_base.rglob("local_*.json"):
            rec = self._parse_one(jf)
            if rec is not None:
                records.append(rec)

        logger.debug("ClaudeSessionCollector: %d 세션 수집", len(records))
        return records

    def _parse_one(self, path: Path) -> SessionRecord | None:
        """local_*.json 1개 → SessionRecord. 실패 시 None + 경고(C3)."""
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Claude 세션 파싱 실패(skip): %s — %s", path.name, e)
            return None

        if not isinstance(data, dict):
            logger.warning("Claude 세션 형식 불일치(skip): %s", path.name)
            return None

        session_id = data.get("sessionId")
        if not session_id:
            logger.warning("Claude 세션 sessionId 없음(skip): %s", path.name)
            return None

        try:
            return SessionRecord(
                app="claude",
                session_id=str(session_id),
                project_path=data.get("cwd"),
                model=data.get("model"),
                last_activity=ms_to_dt(data.get("lastActivityAt")),
                running_pid=None,
                running_cmd=None,
                last_event=None,
                source_file=str(path),
            )
        except Exception as e:  # pydantic 검증 등 — 레코드만 skip
            logger.warning("Claude SessionRecord 생성 실패(skip): %s — %s", path.name, e)
            return None
