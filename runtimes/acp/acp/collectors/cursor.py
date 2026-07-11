"""acp/collectors/cursor.py — Cursor/VSCode 워크스페이스 세션 수집기 (P2).

소스: %APPDATA%/Cursor/User/workspaceStorage/<hash>/workspace.json (+ 같은 폴더 state.vscdb)
실측 형상(2026-06-10):
  - workspace.json = {"folder": "<uri>"} 단일 키.
  - folder uri 2종: file:///c:/...(로컬) / vscode-remote://ssh-remote%2B<host>/...(원격 SSH).
  - state.vscdb 내부엔 단일 '마지막 활동시각' 필드 없음(ItemTable만; history.entries뿐)
    → DB 미오픈, state.vscdb 파일 mtime을 last_activity로 채택(R-002 회피, YAGNI).

불변 원칙:
  - read-only. state.vscdb를 열지 않음(잠금 위험 0).
  - 원격 uri → project_path엔 원문 보존하되 조인 단계에서 no-phase-file(로컬 탐색 불가).
  - 파싱 실패 → 그 레코드만 skip + 경고. 전체 수집 중단 금지(C3).
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from acp.collectors.base import BaseCollector
from acp.models import SessionRecord
from acp.timeutil import mtime_to_dt

logger = logging.getLogger(__name__)

# file:///c:/... 형태의 선행 슬래시+드라이브 패턴
_WIN_DRIVE_RE = re.compile(r"^/[a-zA-Z]:")


def _folder_to_project_path(folder: str) -> str | None:
    """workspace.json의 folder uri → project_path 문자열.

    - file:// → 로컬 경로로 디코드(드라이브 복원). 로컬 탐색 가능.
    - vscode-remote:// 등 기타 스킴 → 원문 그대로 반환(조인 단계가 '://'로 원격 판별 → no-phase-file).
    - 빈 값 → None.
    """
    if not folder:
        return None
    if folder.startswith("file://"):
        parts = urlsplit(folder)
        p = unquote(parts.path)
        # Windows: '/c:/Users/...' → 'c:/Users/...'
        if _WIN_DRIVE_RE.match(p):
            p = p[1:]
        try:
            return str(Path(p))
        except (ValueError, OSError):
            return p
    # 원격/기타 스킴: 원문 보존 (조인이 '://'로 원격 판별)
    return folder


class CursorWorkspaceCollector(BaseCollector):
    """Cursor/VSCode workspaceStorage 수집기.

    workspace_base: %APPDATA%/Cursor/User/workspaceStorage
    app_name 파라미터로 'cursor'/'vscode' 구분(같은 포맷).
    """

    def __init__(self, workspace_base: Path, app_name: str = "cursor") -> None:
        self._workspace_base = workspace_base
        self._app_name = app_name

    @property
    def app_name(self) -> str:
        return self._app_name

    def collect(self) -> list[SessionRecord]:
        if not self._workspace_base.exists():
            logger.warning("%s workspaceStorage 폴더 없음: %s", self._app_name, self._workspace_base)
            return []

        records: list[SessionRecord] = []
        for wsjson in self._workspace_base.glob("*/workspace.json"):
            rec = self._parse_one(wsjson)
            if rec is not None:
                records.append(rec)

        logger.debug("%s Collector: %d 워크스페이스 수집", self._app_name, len(records))
        return records

    def _parse_one(self, wsjson: Path) -> SessionRecord | None:
        try:
            data = json.loads(wsjson.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("%s workspace.json 파싱 실패(skip): %s — %s", self._app_name, wsjson, e)
            return None

        folder = data.get("folder") if isinstance(data, dict) else None
        project_path = _folder_to_project_path(folder) if folder else None

        ws_hash = wsjson.parent.name
        # 활동시각: state.vscdb mtime 우선, 없으면 workspace.json mtime (DB 미오픈)
        db = wsjson.parent / "state.vscdb"
        last_activity = mtime_to_dt(db) or mtime_to_dt(wsjson)
        source = str(db if db.exists() else wsjson)

        try:
            return SessionRecord(
                app=self._app_name,
                session_id=ws_hash,
                project_path=project_path,
                model=None,
                last_activity=last_activity,
                running_pid=None,
                running_cmd=None,
                last_event=None,
                source_file=source,
            )
        except Exception as e:
            logger.warning("%s SessionRecord 생성 실패(skip): %s — %s", self._app_name, wsjson, e)
            return None
