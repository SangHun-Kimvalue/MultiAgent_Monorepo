"""acp/config.py — 설정 로드 (paths.yaml + 런타임 파라미터)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class LivenessConfig:
    """생존 판정 임계값 (초 단위)."""
    idle_threshold: float = 300.0     # 5분: 활동 없으면 IDLE
    hold_threshold: float = 900.0     # 15분: 홀딩 판정
    stale_ttl: float = 3600.0         # 60분: STALE(좀비) 판정


@dataclass
class NotifyConfig:
    """상태 전이 알림 설정."""
    toast_enabled: bool = True
    webhook_url: str = ""
    webhook_format: str = "slack"
    notify_cooldown: float = 3600.0


def _expand(path: str) -> str:
    """%ENVVAR% 및 ~ 를 실제 경로로 치환."""
    return os.path.expandvars(os.path.expanduser(path))


@dataclass
class AppConfig:
    """애플리케이션 전체 설정."""
    poll_interval: float = 15.0       # 초
    db_path: str = ".acp/acp.db"
    events_log: str = ".acp/events.jsonl"
    orch_events_dir: str = ""         # orchestrator 이벤트 폴링 디렉터리(빈 값=비활성)
    paths_yaml: str = "config/paths.yaml"
    host: str = "127.0.0.1"
    port: int = 8900
    liveness: LivenessConfig = field(default_factory=LivenessConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    app_paths: dict[str, str] = field(default_factory=dict)  # paths.yaml 내용

    def get_path(self, key: str) -> Path:
        """app_paths[key]를 환경변수 치환 후 Path로 반환. 누락은 명시 실패(C3)."""
        if key not in self.app_paths or not self.app_paths.get(key):
            raise KeyError(f"app_paths.{key} 누락")
        raw = self.app_paths[key]
        return Path(_expand(raw))

    @classmethod
    def load(cls, paths_yaml: str = "config/paths.yaml") -> "AppConfig":
        cfg = cls(paths_yaml=paths_yaml)
        p = Path(paths_yaml)
        if p.exists():
            raw: dict[str, Any] = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            cfg.app_paths = raw.get("app_paths", {})
            timing = raw.get("liveness", {})
            if timing:
                cfg.liveness = LivenessConfig(
                    idle_threshold=timing.get("idle_threshold", 300.0),
                    hold_threshold=timing.get("hold_threshold", 900.0),
                    stale_ttl=timing.get("stale_ttl", 3600.0),
                )
                cfg.poll_interval = timing.get("poll_interval", 15.0)
            notify = raw.get("notify", {})
            if notify:
                cfg.notify = NotifyConfig(
                    toast_enabled=notify.get("toast_enabled", True),
                    webhook_url=notify.get("webhook_url", ""),
                    webhook_format=notify.get("webhook_format", "slack"),
                    notify_cooldown=notify.get("notify_cooldown", 3600.0),
                )
            cfg.orch_events_dir = raw.get("orch_events_dir", "")
        return cfg
