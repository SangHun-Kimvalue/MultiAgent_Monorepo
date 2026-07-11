"""acp/proc.py — 프로세스 생존 조회 (OS 격리 경계).

is_pid_alive(pid)는 liveness.derive_state에 콜러블로 주입.
→ 테스트에서 OS 의존 없이 fake 주입 가능 (순수함수 불변식 유지).

Windows: ctypes OpenProcess (외부 의존성 없음).
Unix:    os.kill(pid, 0).
"""
from __future__ import annotations

import sys
import logging

logger = logging.getLogger(__name__)


def is_pid_alive(pid: int | None) -> bool:
    """pid 프로세스가 실행 중이면 True. None 또는 조회 불가 → False.

    이 함수는 liveness.derive_state의 is_alive 콜러블로 주입한다.
    osPid는 Codex 툴콜 서브프로세스 PID — RUNNING 확인 양성 신호로만 사용.
    """
    if pid is None:
        return False

    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False

    try:
        if sys.platform == "win32":
            import ctypes
            # SYNCHRONIZE 접근으로 프로세스 존재 여부만 확인 (종료 권한 불필요)
            SYNCHRONIZE = 0x00100000
            handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        else:
            import os
            os.kill(pid, 0)
            return True
    except (OSError, PermissionError):
        return False
    except Exception as e:
        logger.debug("is_pid_alive(%s) 조회 예외: %s", pid, e)
        return False
