#!/usr/bin/env python3
"""Nitpicker → ztr Envelope 어댑터 (wiring B).

닛피커 CLI(`mini_nitpicker.py` 또는 `run_nit.py`)를 subprocess로 실행하고,
그 exit code를 ztr Envelope 계약(`EXECUTION_ADAPTER_CONTRACT.md §2`)의
단일 JSON으로 변환해 stdout에 출력한다. 이로써 닛피커가
`ztr run-phase --mechanical-cmd`의 정식 Mechanical leg가 될 수 있다.

설계 경계 (ADR AD-3): 이 어댑터는 ztr 코드를 import하지 않는다. envelope
**계약(JSON shape)** 에만 결합한다. exit code 매핑만 분기하고 닛피커
리뷰 본문(stdout)은 불투명 payload로 전달한다(R5).

exit code 매핑:
- mini  스타일(mini_nitpicker.py): 0=REVIEW_PASSED / 1=PATCH·REJECTED / 2=error
- runnit 스타일(run_nit.py):        0=ALL PASS    / 2=CHANGES_REQUESTED / 3=BLOCKED
→ ztr Verdict: PASS=0 / CHANGES_REQUESTED=1 / BLOCKED=2

silent fallback 금지: 매핑 불가한 exit code는 PASS로 둔갑시키지 않고
BLOCKED(exit 2)로 처리한다.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import time

# ztr `redact_stderr`와 동등한 redaction (AD-3상 ztr import 불가 → 계약 동작만 로컬 복제).
# 32자 이상 영숫자/_/- 토큰(시크릿·세션 id 류)을 [REDACTED]로. 정본: runtimes/ztr/src/envelope.py:31
_SECRET_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])")


def _redact(text: str) -> str:
    return _SECRET_TOKEN_RE.sub("[REDACTED]", text)

# Windows cp949 콘솔에서 비-ASCII envelope 출력 크래시 방지 (LESSON-016)
if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ztr Verdict ↔ exit code (EXECUTION_ADAPTER_CONTRACT §2 / envelope.py와 동일 계약)
_PASS = ("PASS", 0)
_CHANGES = ("CHANGES_REQUESTED", 1)
_BLOCKED = ("BLOCKED", 2)

# 닛피커 exit code → (status, ztr exit_code)
_STYLE_MAPS: dict[str, dict[int, tuple[str, int]]] = {
    "mini": {0: _PASS, 1: _CHANGES, 2: _BLOCKED},
    "runnit": {0: _PASS, 2: _CHANGES, 3: _BLOCKED},
}


def _envelope(
    *,
    status: str,
    exit_code: int,
    backend: str,
    model: str,
    duration_s: float,
    stdout: str,
    stderr: str,
    not_claimed: list[str],
) -> dict:
    """ztr Envelope 계약 shape의 순수 dict (extra 키 금지)."""
    return {
        "status": status,
        "exit_code": exit_code,
        "backend": backend,
        "model": model,
        "duration_s": round(duration_s, 6),
        "stdout": stdout,
        "stderr_sanitized": _redact(stderr),
        "fallback_used": False,
        "not_claimed": list(not_claimed),
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="닛피커 CLI를 실행하고 ztr Envelope JSON으로 변환한다.",
    )
    parser.add_argument("--backend", default="nitpicker", help="envelope backend 라벨")
    parser.add_argument("--model", default="", help="envelope model 라벨(예: ollama 모델명)")
    parser.add_argument(
        "--style",
        choices=sorted(_STYLE_MAPS),
        default="mini",
        help="닛피커 exit code 스타일 (mini=mini_nitpicker 0/1/2, runnit=run_nit 0/2/3)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="닛피커 subprocess 타임아웃(초). 초과 시 BLOCKED(exit 124).",
    )
    parser.add_argument(
        "nitpicker_cmd",
        nargs=argparse.REMAINDER,
        help="실행할 닛피커 명령. 앞에 `--`로 구분. 예: -- python bin/mini_nitpicker.py --staged",
    )
    return parser.parse_args(argv)


def _strip_leading_separator(cmd: list[str]) -> list[str]:
    return cmd[1:] if cmd and cmd[0] == "--" else cmd


def run(argv: list[str]) -> int:
    args = parse_args(argv)
    cmd = _strip_leading_separator(args.nitpicker_cmd)
    if not cmd:
        env = _envelope(
            status=_BLOCKED[0], exit_code=_BLOCKED[1], backend=args.backend,
            model=args.model, duration_s=0.0, stdout="",
            stderr="닛피커 명령이 비었다. `-- <cmd...>`로 전달하라.",
            not_claimed=["nitpicker-command-missing"],
        )
        print(json.dumps(env, ensure_ascii=False))
        return _BLOCKED[1]

    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=args.timeout,
        )
    except subprocess.TimeoutExpired as exc:
        env = _envelope(
            status=_BLOCKED[0], exit_code=124, backend=args.backend, model=args.model,
            duration_s=time.monotonic() - start, stdout=exc.stdout or "",
            stderr=f"닛피커 타임아웃 {args.timeout}s 초과", not_claimed=["nitpicker-timeout"],
        )
        print(json.dumps(env, ensure_ascii=False))
        return 124
    except (OSError, ValueError) as exc:
        env = _envelope(
            status=_BLOCKED[0], exit_code=70, backend=args.backend, model=args.model,
            duration_s=time.monotonic() - start, stdout="",
            stderr=f"닛피커 실행 실패: {exc}", not_claimed=["nitpicker-spawn-failed"],
        )
        print(json.dumps(env, ensure_ascii=False))
        return 70

    duration = time.monotonic() - start
    raw = proc.returncode
    mapping = _STYLE_MAPS[args.style]
    not_claimed: list[str] = []
    if raw in mapping:
        status, exit_code = mapping[raw]
    else:
        # silent-PASS 금지: 미지의 exit code는 BLOCKED로(PASS 둔갑 안 함)
        status, exit_code = _BLOCKED
        not_claimed.append(f"nitpicker-unexpected-exit-{raw}")

    env = _envelope(
        status=status, exit_code=exit_code, backend=args.backend, model=args.model,
        duration_s=duration, stdout=proc.stdout or "",
        stderr=proc.stderr or "", not_claimed=not_claimed,
    )
    print(json.dumps(env, ensure_ascii=False))
    return exit_code


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
