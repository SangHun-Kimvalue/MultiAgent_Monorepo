"""acp/collectors/base.py — BaseCollector 추상 경계.

규칙(C7):
- collect()는 read-only. 절대 외부 파일/DB 수정 금지.
- 예외는 삼키지 않고 전파 또는 로깅(C3). 호출자가 처리.
- 새 앱 = 이 ABC를 구현하는 클래스 1개 추가. Core는 무변경.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from acp.models import SessionRecord


class BaseCollector(ABC):
    """앱별 세션 수집기 추상 경계."""

    @property
    @abstractmethod
    def app_name(self) -> str:
        """수집기가 담당하는 앱 이름 (예: 'codex', 'claude', 'cursor')."""
        ...

    @abstractmethod
    def collect(self) -> list[SessionRecord]:
        """앱 로컬 아티팩트를 읽어 정규화된 SessionRecord 목록을 반환.

        - read-only: 파일/DB 수정 금지.
        - 파싱 실패한 레코드는 건너뛰되 로깅(C3).
        - 전체 실패(파일 접근 불가 등)는 예외를 전파 또는 [] 반환 후 경고.
        - 반환 리스트는 비어있어도 OK.
        """
        ...
