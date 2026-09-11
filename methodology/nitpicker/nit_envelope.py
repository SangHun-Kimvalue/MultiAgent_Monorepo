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
- mini  스타일(mini_nitpicker.py): 0=REVIEW_PASSED / 1=PATCH·REJECTED / 2=error / 3=무검토
- runnit 스타일(run_nit.py):        0=ALL PASS    / 2=CHANGES_REQUESTED / 3=BLOCKED
→ ztr Verdict: PASS=0 / CHANGES_REQUESTED=1 / BLOCKED=2

silent fallback 금지: 매핑 불가한 exit code는 PASS로 둔갑시키지 않고
BLOCKED(exit 2)로 처리한다.

**PASS는 exit code 단독으로 주장하지 않는다 (LESSON-M046)**: exit 0만 보면
"검토했고 통과"와 "아무것도 검토하지 않았다"를 구분할 수 없다. 게이트 도구가 자체
필터(경로 스코프·타 저장소·변경 감지)로 대상을 0건으로 줄이고 exit 0을 내면
fail-open이 *판정*이 아니라 *입력 선별* 단계에서 일어나기 때문이다. 그래서 mini
스타일은 `(exit, MINI_NITPICKER_STATUS)` **튜플**로 판정하고, 리뷰 수행 증거가 없으면
PASS를 BLOCKED로 닫는다. 호출 규약 = "exit 0 + STATUS=REVIEWED + 미검토 대상 0건 = PASS".

R5 준수: 여기서 읽는 것은 stdout의 **machine token(enum·정수)** 뿐이다. 리뷰 산문은
여전히 불투명 payload이며 코드가 의미를 해석하지 않는다.
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

# 닛피커 stdout의 machine token 계약. 정본 = Nitpicker Daemon
# `src/jemmin/mini_reviewer.py`의 STATUS_TOKEN_PREFIX / TALLY_TOKEN_PREFIX.
_STATUS_TOKEN_PREFIX = "MINI_NITPICKER_STATUS"
_TALLY_TOKEN_PREFIX = "MINI_NITPICKER_TALLY"
_REVIEWED = "REVIEWED"

# "리뷰가 실제로 수행됐다"를 증명하는 토큰을 내보내는 스타일.
# run_nit.py(runnit)는 아직 내보내지 않으므로 튜플 판정이 불가능하다 — 막지 않되 기록한다.
_STYLES_WITH_STATUS_TOKEN = frozenset({"mini"})

# 무검토를 뜻하는 exit code(스타일별). BLOCKED로 가되 "미지의 코드"가 아니라
# **무검토**라고 이름 붙여야 호출자가 원인을 판별할 수 있다.
_NO_REVIEW_EXITS: dict[str, frozenset[int]] = {"mini": frozenset({3})}

# ztr Verdict ↔ exit code (EXECUTION_ADAPTER_CONTRACT §2 / envelope.py와 동일 계약)
_PASS = ("PASS", 0)
_CHANGES = ("CHANGES_REQUESTED", 1)
_BLOCKED = ("BLOCKED", 2)

# 닛피커 exit code → (status, ztr exit_code)
_STYLE_MAPS: dict[str, dict[int, tuple[str, int]]] = {
    "mini": {0: _PASS, 1: _CHANGES, 2: _BLOCKED, 3: _BLOCKED},
    "runnit": {0: _PASS, 2: _CHANGES, 3: _BLOCKED},
}


def _scan_tokens(stdout: str, prefix: str) -> list[str]:
    """`PREFIX=<value>` 줄의 값만 수집한다.

    산문 본문은 보지 않는다(R5). 접두사로 시작하는 줄만 machine token으로 취급한다.
    """
    needle = prefix + "="
    return [
        line.strip()[len(needle):].strip()
        for line in stdout.splitlines()
        if line.strip().startswith(needle)
    ]


#: TALLY 토큰이 반드시 담아야 하는 필드. 하나라도 빠지면 전량 리뷰를 증명하지 못한다.
_TALLY_FIELDS = ("requested", "reviewed", "unreviewed")


def _tally_violation(raw: str) -> str | None:
    """TALLY 토큰이 '전량 리뷰'를 증명하지 못하면 그 사유를, 증명하면 None을 반환한다.

    R3-P1: 일부 필드만 보면 `{"requested":5,"reviewed":0,"unreviewed":0}`처럼
    **모순된 집계가 완전 리뷰 증거로 통과**한다. 세 필드를 모두 요구하고 산술
    불변식까지 확인해야 "요청한 대상이 전부 리뷰됐다"가 증명된다.
    """
    try:
        counts = json.loads(raw)
    except ValueError:
        return "nitpicker-tally-unparsable"
    if not isinstance(counts, dict):
        return "nitpicker-tally-unparsable"

    values: dict[str, int] = {}
    for field in _TALLY_FIELDS:
        value = counts.get(field)
        # bool은 int의 서브클래스라 isinstance만으로는 걸러지지 않는다(`True == 1`).
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return f"nitpicker-tally-field-invalid-{field}"
        values[field] = value

    if values["requested"] <= 0:
        # 대상 0건은 "통과"가 아니라 무검토다(LESSON-M046).
        return "nitpicker-tally-requested-0"
    if values["unreviewed"] > 0:
        # 일부만 리뷰됐는데 exit 0이면 나머지는 한 번도 안 본 것이다.
        return f"nitpicker-partial-review-unreviewed-{values['unreviewed']}"
    if values["requested"] != values["reviewed"] + values["unreviewed"]:
        # 합이 안 맞으면 집계 자체를 믿을 수 없다 — 추측하지 않고 닫는다.
        return "nitpicker-tally-inconsistent"
    # 여기까지 오면 `reviewed == requested > 0`이 산술적으로 따라온다.
    return None


def _review_evidence(
    stdout: str, *, style: str, allow_missing_token: bool
) -> tuple[bool, list[str]]:
    """PASS를 주장하기 전에 '실제로 리뷰됐다'는 증거를 확인한다.

    반환 `(pass_allowed, not_claimed 마커)`. 증거가 없으면 PASS를 내주지 않는다 —
    무검토를 통과로 위장하지 않기 위해서다.
    """
    if style not in _STYLES_WITH_STATUS_TOKEN:
        # 증거 계약이 없는 백엔드. 없는 계약을 지어내 막지는 않되,
        # **검증하지 못했다는 사실을 envelope에 남긴다**(조용한 fail-open 금지).
        return True, ["nitpicker-review-unverified-no-status-token"]

    statuses = _scan_tokens(stdout, _STATUS_TOKEN_PREFIX)
    if not statuses:
        if allow_missing_token:
            # 토큰 이전 버전 닛피커를 의도적으로 허용한 경우. 열화를 기록으로 남긴다.
            return True, ["nitpicker-status-token-missing-allowed"]
        return False, ["nitpicker-status-token-missing"]
    if len(statuses) > 1:
        # 계약은 STATUS 한 줄이다. 여러 줄이면 어느 것이 진짜인지 모른다 → 닫는다.
        return False, ["nitpicker-status-token-ambiguous"]
    status = statuses[0]
    if status != _REVIEWED:
        return False, [f"nitpicker-status-not-reviewed-{status}"]

    # R3-P1: TALLY가 **없으면** "미검토 대상 0건"을 증명할 수 없다. 없는 증거를
    # 통과로 치면 PASS 규약이 STATUS 한 줄로 쪼그라든다 — 그러면 부분 리뷰가 샌다.
    # R3-P2: 두 줄 이상이면 서로 모순돼도 각각은 통과한다 → STATUS와 같게 닫는다.
    tallies = _scan_tokens(stdout, _TALLY_TOKEN_PREFIX)
    if not tallies:
        return False, ["nitpicker-tally-missing"]
    if len(tallies) > 1:
        return False, ["nitpicker-tally-ambiguous"]
    violation = _tally_violation(tallies[0])
    if violation is not None:
        return False, [violation]
    return True, []


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
        "--allow-missing-status-token",
        action="store_true",
        help=(
            "mini 스타일에서 STATUS 토큰이 없어도 PASS를 허용한다(토큰 이전 닛피커 호환). "
            "기본은 fail-closed. 허용하면 envelope not_claimed에 열화가 기록된다."
        ),
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
        if raw in _NO_REVIEW_EXITS.get(args.style, frozenset()):
            # 무검토는 BLOCKED이되 "미지의 코드"와 구분해 이름 붙인다.
            not_claimed.append(f"nitpicker-no-review-exit-{raw}")
    else:
        # silent-PASS 금지: 미지의 exit code는 BLOCKED로(PASS 둔갑 안 함)
        status, exit_code = _BLOCKED
        not_claimed.append(f"nitpicker-unexpected-exit-{raw}")

    if (status, exit_code) == _PASS:
        # exit code만으로 PASS를 주장하지 않는다 — 리뷰 수행 증거를 함께 본다.
        allowed, markers = _review_evidence(
            proc.stdout or "",
            style=args.style,
            allow_missing_token=args.allow_missing_status_token,
        )
        not_claimed.extend(markers)
        if not allowed:
            status, exit_code = _BLOCKED

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
