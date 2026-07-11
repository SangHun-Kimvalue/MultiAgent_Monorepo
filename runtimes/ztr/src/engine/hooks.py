"""알림 훅 시스템 — 리뷰 완료 시 사용자에게 알림.

플러그인 구조: IHook 프로토콜을 구현하면 새 훅 추가 가능.
MVP: FileHook (JSON 저장) + ToastHook (Windows 토스트).
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class IHook(Protocol):
    """알림 훅 프로토콜."""

    def notify(self, event: dict[str, Any]) -> None:
        """이벤트를 사용자에게 전달한다."""
        ...


class FileHook:
    """결과를 JSON 파일로 저장하는 훅.

    다른 도구(IDE, 파일 감시자, CI)가 이 파일을 감시하여
    리뷰 완료를 감지할 수 있다.
    """

    def __init__(self, output_dir: Path | str = ".ztr/results") -> None:
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    def notify(self, event: dict[str, Any]) -> None:
        enriched = {
            "timestamp": datetime.now().isoformat(),
            **event,
        }
        # latest.json (항상 덮어쓰기)
        latest = self._output_dir / "latest.json"
        latest.write_text(
            json.dumps(enriched, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # history.jsonl (append)
        history = self._output_dir / "history.jsonl"
        with history.open("a", encoding="utf-8") as f:
            f.write(json.dumps(enriched, ensure_ascii=False) + "\n")

        logger.info("FileHook: %s 저장", latest)


class ToastHook:
    """Windows 토스트 알림 훅.

    Nitpicker 포맷 기반:
        Title: "{icon} ZTR: {verdict}"
        Body:  "{summary}"
        Detail: "{target_file} | {rounds}R | {elapsed}ms"

    클릭 시 latest.json을 열도록 launch URL 설정.
    Windows가 아닌 환경에서는 자동으로 비활성화.
    """

    _ICONS = {
        "pass": "[PASS]", "conditional": "[COND]",
        "fail": "[FAIL]", "timeout": "[TIMEOUT]",
        "error": "[ERROR]",
    }

    def notify(self, event: dict[str, Any]) -> None:
        if sys.platform != "win32":
            logger.debug("ToastHook: Windows가 아니므로 건너뜀")
            return

        verdict = event.get("verdict", "?")
        summary = event.get("summary", "")[:200]
        target = event.get("target_file", "")
        rounds = event.get("rounds", 0)
        elapsed = event.get("elapsed_ms", 0)
        icon = self._ICONS.get(verdict, "[?]")

        title = f"{icon} ZTR: {verdict.upper()}"
        # 본문: summary (에러 또는 reasoning)
        body = summary if summary else event.get("task", "")[:100]
        # 디테일: 파일 | 라운드 | 시간
        detail = f"{Path(target).name if target else '?'} | {rounds}R | {elapsed:.0f}ms"
        # latest.json 경로 (클릭 시 열기)
        latest_path = str(Path(".ztr/results/latest.json").resolve())

        # ToastText04 템플릿 사용 (title + 2줄 body)
        ps_script = (
            f'[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, '
            f'ContentType = WindowsRuntime] | Out-Null; '
            f'$xml = @"\\n'
            f'<toast launch="{latest_path}">\\n'
            f'  <visual>\\n'
            f'    <binding template="ToastText04">\\n'
            f'      <text id="1">{self._escape(title)}</text>\\n'
            f'      <text id="2">{self._escape(body)}</text>\\n'
            f'      <text id="3">{self._escape(detail)}</text>\\n'
            f'    </binding>\\n'
            f'  </visual>\\n'
            f'</toast>\\n'
            f'"@; '
            f'$xd = New-Object Windows.Data.Xml.Dom.XmlDocument; '
            f'$xd.LoadXml($xml); '
            f'$toast = [Windows.UI.Notifications.ToastNotification]::new($xd); '
            f'[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("ZTR").Show($toast)'
        )

        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                capture_output=True,
                timeout=5,
            )
            logger.debug("ToastHook: 알림 전송 완료")
        except Exception as exc:
            logger.warning("ToastHook 실패: %s", exc)

    @staticmethod
    def _escape(text: str) -> str:
        """XML 특수문자 이스케이프."""
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;")
        )


class HookManager:
    """훅 관리자. 여러 훅을 등록하고 일괄 실행."""

    def __init__(self) -> None:
        self._hooks: list[IHook] = []

    def add(self, hook: IHook) -> None:
        self._hooks.append(hook)

    def notify_all(self, event: dict[str, Any]) -> None:
        for hook in self._hooks:
            try:
                hook.notify(event)
            except Exception:
                logger.exception("훅 실행 실패: %s", type(hook).__name__)
