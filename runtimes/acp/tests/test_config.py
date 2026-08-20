"""tests/test_config.py — AppConfig path guard 테스트."""
from __future__ import annotations

import pytest

from acp.config import AppConfig


def test_get_path_raises_for_missing_required_key():
    cfg = AppConfig(app_paths={})

    with pytest.raises(KeyError, match="app_paths.codex_sessions"):
        cfg.get_path("codex_sessions")


def test_get_path_raises_for_empty_required_key():
    cfg = AppConfig(app_paths={"codex_sessions": ""})

    with pytest.raises(KeyError):
        cfg.get_path("codex_sessions")


def test_load_notify_config(tmp_path):
    cfg_file = tmp_path / "paths.yaml"
    cfg_file.write_text(
        """
notify:
  toast_enabled: false
  webhook_url: https://example.test/hook
  webhook_format: discord
  notify_cooldown: 120
""",
        encoding="utf-8",
    )

    cfg = AppConfig.load(str(cfg_file))

    assert cfg.notify.toast_enabled is False
    assert cfg.notify.webhook_url == "https://example.test/hook"
    assert cfg.notify.webhook_format == "discord"
    assert cfg.notify.notify_cooldown == 120


def test_notify_config_defaults_to_toast_only():
    cfg = AppConfig()

    assert cfg.notify.toast_enabled is True
    assert cfg.notify.webhook_url == ""
    assert cfg.notify.webhook_format == "slack"
    assert cfg.notify.notify_cooldown == 3600.0
