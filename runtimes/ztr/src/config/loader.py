"""YAML 설정 파일 로더.

agents.config.yaml을 로드하고 Pydantic으로 검증한다.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from src.config.schema import RoundtableConfig

logger = logging.getLogger(__name__)

# 기본 설정 파일 위치
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "agents.config.yaml"


def load_config(path: Path | str | None = None) -> RoundtableConfig:
    """YAML 설정 파일을 로드하고 Pydantic으로 검증한다.

    Args:
        path: YAML 파일 경로. None이면 기본 경로 사용.

    Returns:
        검증된 RoundtableConfig 인스턴스.

    Raises:
        FileNotFoundError: 파일이 없을 때.
        yaml.YAMLError: YAML 문법 오류.
        pydantic.ValidationError: 스키마 검증 실패.
        ValueError: 파일이 비어있을 때.
    """
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH

    if not config_path.exists():
        raise FileNotFoundError(
            f"설정 파일을 찾을 수 없습니다: {config_path}\n"
            f"기본 설정 파일 위치: {DEFAULT_CONFIG_PATH}"
        )

    raw_text = config_path.read_text(encoding="utf-8")
    data: dict[str, Any] | None = yaml.safe_load(raw_text)

    # yaml.safe_load는 빈 파일에서 None을 반환함
    if data is None:
        raise ValueError(f"설정 파일이 비어있습니다: {config_path}")

    config = RoundtableConfig.model_validate(data)

    enabled_count = sum(1 for a in config.agents if a.enabled)
    logger.info(
        "설정 로드 완료: 에이전트 %d개 (활성 %d개), 파일: %s",
        len(config.agents), enabled_count, config_path,
    )
    return config
