"""타임아웃 시 자식 **프로세스 트리**를 정리한다 (Phase 9).

`proc.kill()`은 직계 자식만 죽인다. 손자(예: CLI가 다시 띄운 프로세스)는 살아남아 고아가
되고, 상속받은 파이프를 쥐고 있으면 `communicate()`가 EOF를 못 받아 **무한 대기**한다.

수단은 P0 스파이크가 실측으로 골랐다(`P0_RESULTS.md`) — Windows Job Object.
`taskkill /T`는 **중간 부모가 먼저 종료되면 `rc=128`로 실패**해 손자를 남긴다.

**보장**: Job에 편입된 프로세스와, 편입 이후 생성되어 breakaway하지 않은 후손.
**보장하지 않음**: ① 편입 전에 생성된 후손 ② breakaway로 이탈한 후손
③ POSIX에서 스스로 `setsid()`한 후손. → `cleanup-partial`로 보고하고 통제했다 주장하지 않는다.
"""
from __future__ import annotations

import asyncio
import errno
import os
import sys
import time
from typing import Any

#: 정리 결과 토큰. **안정 enum** — 코드가 이 값으로 분기한다(R5). 자유 문자열 금지.
CLEANUP_UNVERIFIED = "cleanup-unverified"   #: 종료 확인 자체를 수행하지 못했다
CLEANUP_PARTIAL = "cleanup-partial"         #: 일부 대상/단계만 성공
DRAIN_ABANDONED = "drain-abandoned"         #: 출력 drain만 포기(종료 결과와 독립)

#: 정리 전체에 주는 시간. 단계별 상한의 합이 아니라 **총량**이다(DEC-3).
DEFAULT_CLEANUP_DEADLINE_S = 5.0

_IS_WIN = sys.platform == "win32"
#: `AssignProcessToJobObject` 에 필요한 **최소** 권한.
#: PROCESS_ALL_ACCESS 를 쓰면 제한된 환경에서 불필요하게 OpenProcess 가 실패한다.
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_JOB_ACCESS = _PROCESS_SET_QUOTA | _PROCESS_TERMINATE


def merge_cleanup(*groups: tuple[str, ...]) -> tuple[str, ...]:
    """여러 정리 단계의 토큰을 **순서 보존 + 중복 제거**로 합친다.

    ⚠ `finally` 경로의 토큰을 버리면 "비어 있으면 정리 완료"라는 계약이 거짓이 된다 —
    정상 종료 경로에서 편입·종료가 실패해도 결과가 깨끗해 보인다(출력 R1 P1).
    """
    merged: dict[str, None] = {}
    for group in groups:
        for token in group:
            merged[token] = None
    return tuple(merged)


def spawn_kwargs() -> dict[str, Any]:
    """자식을 **자기 경계 안에서** 띄우기 위한 `create_subprocess_exec` 추가 인자.

    POSIX는 spawn 시점에 새 세션을 만들어야 `killpg`로 트리를 잡을 수 있다.
    Windows는 spawn 뒤에 `attach()`로 Job에 편입한다(여기서는 줄 게 없다).
    """
    return {} if _IS_WIN else {"start_new_session": True}


def _kernel32() -> tuple[Any, Any]:
    import ctypes
    import ctypes.wintypes as wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.AssignProcessToJobObject.restype = wintypes.BOOL
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k.TerminateJobObject.restype = wintypes.BOOL
    k.TerminateJobObject.argtypes = [wintypes.HANDLE, ctypes.c_uint]
    k.CloseHandle.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    return ctypes, k


def attach(proc: asyncio.subprocess.Process) -> Any | None:
    """spawn 직후 자식을 정리 경계에 편입한다. 실패하면 `None`.

    ⚠ **fail-closed**: 편입 실패를 조용히 넘기지 않는다. `None`이면 `terminate_tree`가
    `cleanup-unverified`를 보고한다 — "죽였다"고 말하면서 안 죽이는 걸 다시 만들지 않는다.
    """
    if not _IS_WIN:
        return None  # POSIX 경계는 spawn_kwargs()가 이미 만들었다
    pid = getattr(proc, "pid", None)
    if not isinstance(pid, int):
        # 테스트가 주입하는 fake process 등 pid 가 없는 객체. 편입할 대상이 없다.
        return None
    try:
        ctypes, k = _kernel32()
    except Exception:  # noqa: BLE001 — ctypes 자체를 못 쓰는 환경
        return None
    job = k.CreateJobObjectW(None, None)
    if not job:
        return None
    # ⚠ 성공적으로 **반환할 때만** 소유권을 호출자에게 넘긴다. 그 전에는 이 함수가 소유하며
    # 어떤 경로(예외 포함)로 빠져나가도 반납한다. ctypes 경계는 예외를 던질 수 있다.
    handed_over = False
    hproc = None
    try:
        hproc = k.OpenProcess(_JOB_ACCESS, False, pid)
        if not hproc:
            return None
        if not k.AssignProcessToJobObject(job, hproc):
            # 중첩 Job 제약 등. 실패를 통과로 위장하지 않는다.
            return None
        handed_over = True
        return job
    except Exception:  # noqa: BLE001 — ctypes 호출·타입 오류까지 복구 대상
        return None
    finally:
        # ⚠ `hproc` 정리가 **반환을 취소하면 안 된다.** 여기서 예외가 나면 `return job` 이
        # 취소되는데 `handed_over` 는 True 라 Job 도 안 닫혀 **양쪽 다 놓친다**.
        if hproc:
            try:
                k.CloseHandle(hproc)
            except Exception:  # noqa: BLE001
                pass
        if not handed_over:
            try:
                k.CloseHandle(job)
            except Exception:  # noqa: BLE001
                pass


def _terminate_boundary(proc: asyncio.subprocess.Process, handle: Any | None) -> bool:
    """트리 경계 전체를 종료한다. 성공 여부를 반환한다."""
    if _IS_WIN:
        if handle is None:
            return False
        k = None
        try:
            _ctypes, k = _kernel32()
            return bool(k.TerminateJobObject(handle, 1))
        except Exception:  # noqa: BLE001
            return False
        finally:
            # TerminateJobObject 가 던져도 핸들은 반납한다(누수 금지).
            if k is not None:
                try:
                    k.CloseHandle(handle)
                except Exception:  # noqa: BLE001
                    pass
    pid = getattr(proc, "pid", None)
    if not isinstance(pid, int):
        return False
    # mypy 는 Windows stub 으로 검사하므로 POSIX 전용 심볼을 모른다.
    # 런타임 분기는 위 `_IS_WIN` 이 이미 보장한다.
    killpg = getattr(os, "killpg", None)
    if killpg is None:
        return False
    try:
        # ⚠ `os.getpgid(pid)` 로 되묻지 **않는다.** `spawn_kwargs()` 의
        # `start_new_session=True` 때문에 **자식의 pid 가 곧 pgid** 이고, 세션 리더가 먼저
        # 종료되면 `getpgid` 는 `ProcessLookupError` 로 실패해 **그룹의 손자를 못 죽인다**.
        # Windows 에서 실측한 "중간 부모 선종료"(P0 S2)와 같은 실패 모드다.
        killpg(pid, 9)
        return True
    except ProcessLookupError:
        # **그룹이 이미 없다 = 정리할 게 없다 = 성공.** 실패로 보고하면 POSIX 정상 종료마다
        # 허위 `cleanup-unverified` 가 붙어 토큰이 의미를 잃는다(출력 R2 P1).
        return True
    except OSError as exc:
        return getattr(exc, "errno", None) == errno.ESRCH
    except AttributeError:
        return False


async def terminate_tree(
    proc: asyncio.subprocess.Process,
    handle: Any | None = None,
    *,
    deadline_s: float = DEFAULT_CLEANUP_DEADLINE_S,
) -> tuple[bytes, bytes, tuple[str, ...]]:
    """트리를 종료하고 남은 출력을 **bounded**하게 회수한다.

    반환 `(stdout, stderr, tokens)`. `tokens`는 안정 enum이며 비어 있으면 정리 완료다.
    단일 monotonic deadline을 쓰고 **각 단계는 남은 시간만** 소비한다(DEC-3).
    """
    deadline = time.monotonic() + deadline_s
    tokens: list[str] = []

    if not _terminate_boundary(proc, handle):
        # 경계 종료에 실패했다 = 트리를 통제했다고 말할 수 없다.
        tokens.append(CLEANUP_UNVERIFIED)
    if proc.returncode is None:
        proc.kill()  # 최소한 직계는 확실히 닫는다

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return b"", b"", (*tokens, DRAIN_ABANDONED)
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=remaining)
    except (asyncio.TimeoutError, TimeoutError):
        # 파이프가 안 닫혔다 = 아직 누가 쥐고 있다 = 트리가 남았다.
        return b"", b"", (*tokens, CLEANUP_PARTIAL, DRAIN_ABANDONED)
    return stdout_b or b"", stderr_b or b"", tuple(tokens)
