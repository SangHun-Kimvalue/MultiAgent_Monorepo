"""에이전트 자동 디스커버리.

agents/ 디렉토리의 모든 .py 파일을 자동으로 import하여
__init_subclass__ 등록을 트리거한다.

이 모듈 덕분에 새 에이전트 추가 시:
    1. agents/ 디렉토리에 .py 파일 생성
    2. class MyAgent(IAgent): agent_type = "my_agent" 선언
    3. agents.config.yaml에 설정 추가
    4. 끝. runner.py 수정 불필요.
"""
from __future__ import annotations

import importlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_AGENTS_DIR = Path(__file__).resolve().parent
_SKIP_FILES = {"__init__.py", "base.py", "registry.py", "discovery.py"}


def discover_agents() -> int:
    """agents/ 디렉토리의 모든 에이전트 모듈을 자동 import한다.

    base.py, registry.py, discovery.py, __init__.py는 건너뛴다.

    Returns:
        import된 모듈 수.
    """
    count = 0
    for py_file in sorted(_AGENTS_DIR.glob("*.py")):
        if py_file.name in _SKIP_FILES:
            continue

        module_name = f"src.agents.{py_file.stem}"
        try:
            importlib.import_module(module_name)
            count += 1
            logger.debug("에이전트 모듈 로드: %s", module_name)
        except Exception:
            logger.exception("에이전트 모듈 로드 실패: %s", module_name)

    logger.info("에이전트 디스커버리: %d개 모듈 로드", count)
    return count
