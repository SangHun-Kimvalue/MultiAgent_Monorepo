"""acp/proc.py — 프로세스 조회 (OS 격리 경계).

is_pid_alive(pid)는 liveness.derive_state에 콜러블로 주입.
→ 테스트에서 OS 의존 없이 fake 주입 가능 (순수함수 불변식 유지).

Windows: ctypes OpenProcess (외부 의존성 없음).
Unix:    os.kill(pid, 0).

프로세스 **열거**(T14 S3 D1)는 세 가지를 계약에 넣는다:

1. **실패를 빈 목록으로 축약하지 않는다.** `ProcessSnapshot.status`가 성공(0개)과
   실패/타임아웃/미지원을 구분한다. 구분되지 않으면 "세션 없음"과 "관측 실패"가
   같은 값이 되어 관측이 거짓말한다.
2. **호출 비용을 폴링 주기에서 분리한다.** 캐시 TTL + 단일 비행 + hard timeout.
   탐지 지연은 TTL과 같다(`is_alive`는 이미 아는 PID의 종료만 보고 새 프로세스를
   발견하지 못한다).
3. **argv를 밖으로 내보내지 않는다.** 커맨드라인에는 토큰·프롬프트·로컬 경로가 들어갈
   수 있다. 이 모듈 밖으로 나가는 것은 추출된 사실(pid·session_uuid·model)뿐이다.
"""
from __future__ import annotations

import logging
import re
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

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


# ════════════════════════════════════════
# 프로세스 스냅샷 (T14 S3 D1)
# ════════════════════════════════════════

class SnapshotStatus(str, Enum):
    """스냅샷 결과. 0개 성공과 관측 실패를 절대 같은 값으로 두지 않는다."""

    OK = "ok"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class CliProcess:
    """CLI 프로세스에서 **추출된 사실만**. 원문 커맨드라인은 담지 않는다."""

    pid: int
    session_uuid: str | None
    model: str | None


@dataclass(frozen=True)
class ProcessSnapshot:
    status: SnapshotStatus
    observed_at: datetime
    processes: tuple[CliProcess, ...] = ()
    error: str | None = None
    # `--resume` 값이 UUID 형식이 아니어서 버린 건수. 조용히 버리면 사실이 사라진다.
    malformed_uuid: int = 0

    @property
    def ok(self) -> bool:
        return self.status is SnapshotStatus.OK


_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# `--resume=<v>` / `--resume <v>` / `-r <v>` 와 `--model=<v>` / `--model <v>`.
_RESUME_FLAGS = ("--resume", "-r")
_MODEL_FLAGS = ("--model", "-m")


def split_command_line(command_line: str) -> list[str]:
    """커맨드라인을 argv로 분해(따옴표·백슬래시 이스케이프 처리).

    정규식으로 값만 긁으면 따옴표 안의 공백·이스케이프에서 깨진다. 토큰으로 나눈 뒤
    **exact match**로 읽는다(T14 S3 D2).
    """
    argv: list[str] = []
    current: list[str] = []
    in_quotes = False
    backslashes = 0
    has_token = False

    for ch in command_line:
        if ch == "\\":
            backslashes += 1
            continue
        if ch == '"':
            # Windows 규칙: 백슬래시 2개 = 리터럴 1개, 홀수면 따옴표 이스케이프
            current.append("\\" * (backslashes // 2))
            if backslashes % 2 == 1:
                current.append('"')
            else:
                in_quotes = not in_quotes
            backslashes = 0
            has_token = True
            continue
        if backslashes:
            current.append("\\" * backslashes)
            backslashes = 0
        if ch.isspace() and not in_quotes:
            if has_token or current:
                argv.append("".join(current))
                current = []
                has_token = False
            continue
        current.append(ch)
        has_token = True

    if backslashes:
        current.append("\\" * backslashes)
    if has_token or current:
        argv.append("".join(current))
    return argv


def _read_flag(argv: list[str], flags: tuple[str, ...]) -> str | None:
    """`--flag=value`와 `--flag value` 양쪽을 exact token으로 읽는다."""
    for index, token in enumerate(argv):
        for flag in flags:
            if token == flag:
                if index + 1 < len(argv):
                    return argv[index + 1]
                return None
            prefix = f"{flag}="
            if token.startswith(prefix):
                return token[len(prefix) :]
    return None


def _has_resume_flag(command_line: str) -> bool:
    """resume 플래그가 **있었는지**만 본다.

    값이 없는 bare `--resume`도 "의도는 있었으나 못 읽었다"이므로 malformed로 센다.
    값 유무로 판별하면 그 케이스가 조용히 사라진다.
    """
    argv = split_command_line(command_line)
    for token in argv:
        if token in _RESUME_FLAGS:
            return True
        if any(token.startswith(f"{flag}=") for flag in _RESUME_FLAGS):
            return True
    return False


def parse_cli_process(pid: int, command_line: str) -> CliProcess:
    """커맨드라인에서 세션 uuid와 model만 추출한다. **원문은 반환하지 않는다.**"""
    argv = split_command_line(command_line)
    raw_uuid = _read_flag(argv, _RESUME_FLAGS)
    session_uuid = raw_uuid.lower() if raw_uuid and _UUID_RE.match(raw_uuid) else None
    model = _read_flag(argv, _MODEL_FLAGS) or None
    return CliProcess(pid=pid, session_uuid=session_uuid, model=model)


# PowerShell 스크립트 블록에 `{ }`가 있어 str.format을 쓸 수 없다(치환은 문자열 결합으로).
_PS_QUERY_HEAD = "Get-CimInstance Win32_Process -Filter \"Name='"
_PS_QUERY_TAIL = "'\" | ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"


def _ps_query(process_name: str) -> str:
    # 프로세스 이름은 내부 상수에서만 오지만, 따옴표를 막아 주입 경로를 원천 차단한다.
    safe = process_name.replace("'", "")
    return _PS_QUERY_HEAD + safe + _PS_QUERY_TAIL


def _enumerate_windows(process_name: str, timeout: float) -> ProcessSnapshot:
    """PowerShell/CIM **fallback** 경로. 네이티브 열거는 별도 spike(NOT CLAIMED)."""
    now = datetime.now(timezone.utc)
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _ps_query(process_name),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ProcessSnapshot(SnapshotStatus.TIMEOUT, now, error=f"timeout>{timeout}s")
    except OSError as exc:
        return ProcessSnapshot(SnapshotStatus.FAILED, now, error=f"{type(exc).__name__}: {exc}")

    if completed.returncode != 0:
        return ProcessSnapshot(
            SnapshotStatus.FAILED, now, error=f"exit={completed.returncode}"
        )

    processes: list[CliProcess] = []
    malformed = 0
    for line in completed.stdout.splitlines():
        pid_text, _, command_line = line.partition("\t")
        try:
            pid = int(pid_text.strip())
        except ValueError:
            continue
        proc = parse_cli_process(pid, command_line)
        # resume 플래그는 있었는데 값이 UUID가 아니면 **버린 사실을 센다**.
        if proc.session_uuid is None and _has_resume_flag(command_line):
            malformed += 1
        processes.append(proc)
    # 원문 stdout(=argv 포함)은 여기서 폐기된다. 로그에도 남기지 않는다.
    return ProcessSnapshot(
        SnapshotStatus.OK, now, tuple(processes), malformed_uuid=malformed
    )


class ProcessSnapshotCache:
    """TTL 캐시 + 단일 비행. 폴링 주기마다 외부 프로세스를 띄우지 않는다.

    **탐지 지연 = TTL**이다. `is_pid_alive`는 이미 아는 PID의 종료만 확인할 뿐
    새 프로세스를 발견하지 못하므로, TTL이 길수록 "실행 중인데 안 잡힘" 구간이 길어진다.
    """

    def __init__(
        self,
        process_name: str = "claude.exe",
        ttl_seconds: float = 30.0,
        timeout_seconds: float = 5.0,
        enumerator: Callable[[str, float], ProcessSnapshot] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._process_name = process_name
        self._ttl = ttl_seconds
        self._timeout = timeout_seconds
        self._enumerator = enumerator or _enumerate_windows
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._cached: ProcessSnapshot | None = None

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    def get(self) -> ProcessSnapshot:
        if sys.platform != "win32" and self._enumerator is _enumerate_windows:
            return ProcessSnapshot(
                SnapshotStatus.UNSUPPORTED, self._clock(), error=f"platform={sys.platform}"
            )
        # 단일 비행: 동시 호출이 있어도 외부 프로세스는 한 번만 뜬다.
        with self._lock:
            now = self._clock()
            cached = self._cached
            if cached is not None and (now - cached.observed_at).total_seconds() < self._ttl:
                return cached
            snapshot = self._enumerator(self._process_name, self._timeout)
            self._cached = snapshot
            return snapshot
