"""acp/notify.py — ACP 상태 전이 알림 채널."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
import subprocess
import sys
from typing import Any, Callable

import httpx

from acp.config import NotifyConfig
from acp.models import SessionState

logger = logging.getLogger(__name__)

WebhookPost = Callable[..., httpx.Response]
ToastSender = Callable[[str, str, str], None]

_STATE_MESSAGE: dict[SessionState, tuple[str, str]] = {
    SessionState.HOLDING: ("세션 홀딩 감지", "세션이 입력 대기 또는 장시간 무응답 상태입니다."),
    SessionState.STALE: ("세션 좀비 승급", "홀딩 상태가 오래 지속되어 STALE로 승급되었습니다."),
    SessionState.ERROR: ("세션 오류 감지", "세션에서 오류 또는 중단 이벤트가 감지되었습니다."),
}


@dataclass(frozen=True)
class StateTransitionEvent:
    """상태 전이 알림에 필요한 최소 정보."""

    session_id: str
    native_session_id: str
    app: str
    project_path: str | None
    from_state: str | None
    to_state: SessionState
    created_at: datetime

    @property
    def title(self) -> str:
        return _STATE_MESSAGE.get(self.to_state, ("세션 상태 전이", ""))[0]

    @property
    def body(self) -> str:
        return _STATE_MESSAGE.get(self.to_state, ("", "세션 상태가 변경되었습니다."))[1]

    @property
    def detail(self) -> str:
        project = self.project_path or "no-project"
        before = self.from_state or "none"
        return f"{self.app}:{self.native_session_id} | {before} -> {self.to_state.value} | {project}"

    @property
    def status(self) -> str:
        return self.to_state.value


def _color_for_state(state: SessionState) -> str:
    if state == SessionState.STALE:
        return "#6e7681"
    return "#dc3545"


def build_webhook_payload(fmt: str, event: StateTransitionEvent) -> dict[str, Any]:
    """Slack/Discord/Generic webhook payload를 생성."""
    title = event.title
    body = event.body
    detail = event.detail
    fmt = (fmt or "generic").lower()

    if fmt == "slack":
        return {
            "attachments": [{
                "color": _color_for_state(event.to_state),
                "title": title,
                "text": body,
                "footer": detail,
                "mrkdwn_in": ["text"],
            }]
        }
    if fmt == "discord":
        color = int(_color_for_state(event.to_state).lstrip("#"), 16)
        return {
            "embeds": [{
                "title": title,
                "description": body,
                "color": color,
                "footer": {"text": detail},
            }]
        }
    return {
        "source": "Agent Control Plane",
        "event": "acp.state_transition",
        "title": title,
        "body": body,
        "detail": detail,
        "status": event.status,
        "session_id": event.session_id,
        "native_session_id": event.native_session_id,
        "app": event.app,
        "project_path": event.project_path,
        "from_state": event.from_state,
        "to_state": event.to_state.value,
        "created_at": event.created_at.isoformat(),
    }


class Notifier:
    """Windows toast와 webhook으로 ACP 상태 전이를 알리는 얇은 서비스."""

    def __init__(
        self,
        config: NotifyConfig,
        *,
        webhook_post: WebhookPost | None = None,
        toast_sender: ToastSender | None = None,
    ) -> None:
        self._cfg = config
        self._post = webhook_post or httpx.post
        self._toast_sender = toast_sender

    def notify(self, event: StateTransitionEvent) -> None:
        """설정된 채널로 상태 전이 알림을 발행."""
        if self._cfg.toast_enabled:
            self._send_toast(event.title, event.body, event.detail)
        if self._cfg.webhook_url.strip():
            self._send_webhook(event)
        else:
            logger.info("Webhook URL 미설정 — webhook 알림 skip: %s", event.session_id)

    def _send_toast(self, title: str, body: str, detail: str) -> None:
        if self._toast_sender:
            self._toast_sender(title, body, detail)
            return

        if sys.platform != "win32":
            logger.info("Windows가 아니어서 toast 알림 skip: %s", title)
            return

        try:
            text_lines = body if not detail else f"{body}\n{detail}"
            ps_script = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
                "ContentType = WindowsRuntime] > $null; "
                "$template = [Windows.UI.Notifications.ToastNotificationManager]"
                "::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
                "$text = $template.GetElementsByTagName('text'); "
                f"$text.Item(0).AppendChild($template.CreateTextNode('{_ps_escape(title)}')) > $null; "
                f"$text.Item(1).AppendChild($template.CreateTextNode('{_ps_escape(text_lines)}')) > $null; "
                "$toast = [Windows.UI.Notifications.ToastNotification]::new($template); "
                "[Windows.UI.Notifications.ToastNotificationManager]"
                "::CreateToastNotifier('Agent Control Plane').Show($toast)"
            )
            subprocess.Popen(
                ["powershell", "-NoProfile", "-Command", ps_script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            logger.warning("Toast 알림 발행 실패: %s", exc)

    def _send_webhook(self, event: StateTransitionEvent) -> None:
        payload = build_webhook_payload(self._cfg.webhook_format, event)
        try:
            response = self._post(self._cfg.webhook_url.strip(), json=payload, timeout=5.0)
            response.raise_for_status()
        except Exception as exc:
            logger.warning("Webhook 알림 발행 실패 [%s]: %s", event.session_id, exc)
            raise


def _ps_escape(text: str) -> str:
    """PowerShell 작은따옴표/줄바꿈 이스케이프."""
    return text.replace("'", "''").replace("\n", "`n")
