"""acp/timeutil.py — 시각 변환 공유 헬퍼 (additive util, P2).

codex.py의 `_ms_to_dt` 로직을 공개 헬퍼로 추출. claude/cursor collector가 재사용
(사설 import 금지 원칙). Core 공개 시그니처는 무변경 — 순수 추가 모듈.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def ms_to_dt(ms: int | float | None) -> datetime | None:
    """epoch milliseconds → tz-aware UTC datetime. 실패/None → None."""
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        # `Infinity`(JSON이 기본 허용하는 비표준 값)는 `int()`에서 OverflowError를 낸다.
        # 잡지 않으면 예외가 호출자까지 올라가 **그 레코드가 통째로 탈락**했다 —
        # 시각 하나를 몰라서 세션 자체를 잃는 것은 버린 사실이 된다(LESSON-004).
        return None


def mtime_to_dt(path: Path) -> datetime | None:
    """파일 mtime → tz-aware UTC datetime. 파일 없음/접근불가 → None.

    Cursor last_activity 1차 신호(state.vscdb mtime)로 사용 — DB 미오픈(R-002 회피).
    """
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None
