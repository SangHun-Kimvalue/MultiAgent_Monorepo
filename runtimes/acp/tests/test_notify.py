"""tests/test_notify.py — Notifier payload/채널 테스트."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from acp.config import NotifyConfig
from acp.models import SessionState
from acp.notify import Notifier, StateTransitionEvent, build_webhook_payload


def _event(state: SessionState = SessionState.HOLDING) -> StateTransitionEvent:
    return StateTransitionEvent(
        session_id="codex:s1",
        native_session_id="s1",
        app="codex",
        project_path="C:/repo",
        from_state="live",
        to_state=state,
        created_at=datetime.now(timezone.utc),
    )


class _Response:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def raise_for_status(self) -> None:
        if self.fail:
            raise RuntimeError("webhook failed")


def test_webhook_payload_builders_include_transition_identity():
    event = _event(SessionState.ERROR)

    slack = build_webhook_payload("slack", event)
    discord = build_webhook_payload("discord", event)
    generic = build_webhook_payload("generic", event)

    assert slack["attachments"][0]["title"] == "세션 오류 감지"
    assert "live -> error" in slack["attachments"][0]["footer"]
    assert discord["embeds"][0]["footer"]["text"].startswith("codex:s1")
    assert generic["event"] == "acp.state_transition"
    assert generic["session_id"] == "codex:s1"
    assert generic["to_state"] == "error"


def test_notifier_sends_toast_and_webhook():
    posts: list[dict] = []
    toasts: list[tuple[str, str, str]] = []

    def fake_post(url: str, **kwargs):
        posts.append({"url": url, **kwargs})
        return _Response()

    notifier = Notifier(
        NotifyConfig(toast_enabled=True, webhook_url="https://example.test/hook", webhook_format="generic"),
        webhook_post=fake_post,
        toast_sender=lambda title, body, detail: toasts.append((title, body, detail)),
    )
    notifier.notify(_event())

    assert len(toasts) == 1
    assert posts[0]["url"] == "https://example.test/hook"
    assert posts[0]["json"]["status"] == "holding"
    assert posts[0]["timeout"] == 5.0


def test_notifier_skips_unset_webhook_but_keeps_toast(caplog):
    caplog.set_level("INFO")
    toasts: list[tuple[str, str, str]] = []
    notifier = Notifier(
        NotifyConfig(toast_enabled=True, webhook_url=""),
        toast_sender=lambda title, body, detail: toasts.append((title, body, detail)),
    )

    notifier.notify(_event())

    assert len(toasts) == 1
    assert "Webhook URL 미설정" in caplog.text


def test_notifier_raises_on_webhook_failure():
    notifier = Notifier(
        NotifyConfig(toast_enabled=False, webhook_url="https://example.test/hook"),
        webhook_post=lambda *_args, **_kwargs: _Response(fail=True),
    )

    with pytest.raises(RuntimeError, match="webhook failed"):
        notifier.notify(_event())
