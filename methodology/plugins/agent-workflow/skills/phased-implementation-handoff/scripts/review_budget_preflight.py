#!/usr/bin/env python3
"""리뷰 예산 preflight — 구현 diff 출력 게이트 호출의 단일 강제 진입점.

계약(캐논 METHODOLOGY.md §3 / PLAN.md D1~D4, R2-1·R2-2·R2-4·R2-6):

- 라운드 SoT 는 **phase 진행 문서의 단일 `review-budget` fenced block** 이다(D4).
  별도 상태 파일을 만들지 않는다. 블록이 0개거나 2개 이상이면 fail-closed.
- `budget_limit` 은 `work_grade` 에서 **파생**한다(D2). 문서 신고값을 신뢰하지 않는다.
- 실행 순서 고정(R2-6): 잠금 → 재검증 → 증가값 내구 저장 성공 → 잠금 해제 → Reviewer 시작.
  저장 실패 시 **호출 금지**. 저장 후 launch 실패는 **소비를 유지**한 채 Human Gate 로 돌린다.
  **잠금 해제 실패도 동일**하게 호출 금지 + 소비 유지로 끝낸다(R1-P2-1). 해제 실패를 삼키면
  다음 호출이 영구 차단되는데 성공을 반환하게 된다.
- `persist` 는 두 번째 스냅샷을 **완전히 재파싱**해 slice·등급·소비값 일치를 확인하고,
  치환이 정확히 1건인지 검사한 뒤에만 쓴다(R1-P2-2).
- `next_round > budget_limit` 이면 Reviewer 를 **호출하지 않고** four-way disposition 을 요구한다.
- 판정은 숫자 비교와 enum 만 사용한다. prose 의미판정 금지(R5). 결과는 JSON + exit code.

**두 축을 모두 집행한다**(캐논 §3, 2026-08-14 A8). `--gate` 로 축을 고른다:

- `output` (기본) — 구현 diff 심사. 카운터 `rounds_consumed`, 예산 L0 2 / L1 3 / L2 4.
- `input` — 문서·계획·프롬프트 심사. 카운터 `doc_rounds_consumed`, 예산 L0 1 / L1 2 / L2 3.

두 축은 **같은 문서·같은 잠금**을 쓰지만 **카운터가 분리**돼 서로의 잔여를 빌려오지 못한다.
해당 축의 카운터 줄이 없으면 fail-closed 다 — 줄을 지워 예산을 리셋하는 우회를 막는다.

NOT CLAIMED — **"한 축을 소진하고 같은 대상을 다른 축으로 재분류해 다시 심사"하는 우회는
이 도구가 막지 못한다.** 심사 대상의 동일성 판정은 의미 판단이라 숫자·enum 비교로 환원되지
않는다(R5). 그 금지는 캐논 §3 · `SKILL.md` §5.5 의 산문 규칙으로만 존재하며, 집행은 Planner 와
Human Gate 에 있다.

NOT CLAIMED — **잠금을 따르지 않는 writer 로부터는 보호하지 않는다.** `persist` 의
read-check-write 사이에 락을 무시하고 문서를 바꾸면 그 변경을 덮어쓸 수 있다(R2-P1).
이 창을 좁히지 않는 이유: 그런 writer 는 `rounds_consumed` 를 직접 `0` 으로 쓰거나 `.lock` 을
지우는 **더 쉬운 완전 우회**를 이미 갖고 있다. 이 도구가 막는 것은 *우발적·절차적 초과*이지
문서 쓰기 권한을 가진 적대적 행위자가 아니다. 락이 계약이다.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# 축별 (카운터 키, 등급→예산). 캐논 §3 A8 — 두 축은 서로의 잔여를 빌려오지 않는다.
GATES: dict[str, tuple[str, dict[str, int]]] = {
    "output": ("rounds_consumed", {"L0": 2, "L1": 3, "L2": 4}),
    "input": ("doc_rounds_consumed", {"L0": 1, "L1": 2, "L2": 3}),
}
DEFAULT_GATE = "output"
BUDGET_BY_GRADE = GATES[DEFAULT_GATE][1]  # 하위호환 별칭
FOUR_WAY = ("ACCEPT", "REJECT_FALSE_POSITIVE", "DEFER_OUT_OF_SCOPE", "REJECT_OVERENGINEERING")
# 닫는 fence 는 공백 외 잔여물을 허용하지 않는다(R1-P2-4). ```junk 를 종결로 오인하면
# "블록 정확히 1개" 검사가 무력해진다.
BLOCK_RE = re.compile(r"^```review-budget[ \t]*\r?\n(.*?)^```[ \t]*\r?$", re.MULTILINE | re.DOTALL)

EXIT_OK = 0
EXIT_CONTRACT = 2      # 문서·형식·등급 계약 위반
EXIT_EXHAUSTED = 3     # 예산 소진 — Reviewer 미호출
EXIT_PERSIST = 4       # 증가값 저장 실패 — 호출 금지
EXIT_LAUNCH = 5        # 저장 성공 후 launch 실패 — 소비 유지
EXIT_UNLOCK = 6        # 저장 성공 후 잠금 해제 실패 — 소비 유지, Reviewer 호출 금지


class ContractError(Exception):
    def __init__(self, message: str, code: int = EXIT_CONTRACT) -> None:
        super().__init__(message)
        self.code = code


def emit(payload: dict[str, Any]) -> None:
    """결과 JSON 을 출력한다. **인코딩 때문에 죽지 않는다.**

    Windows 기본 콘솔(cp949 등)에서 `ensure_ascii=False` 출력이 `UnicodeEncodeError` 로
    죽으면, 정의된 exit code(3=소진 / 2=계약위반) 대신 **1** 이 나간다. 호출자는 exit code
    로만 분기하므로(R5) 그 순간 계약이 깨진다. 읽기 좋은 출력보다 **분기 가능한 종료**가 먼저다.
    """
    try:
        print(json.dumps(payload, ensure_ascii=False))
    except UnicodeEncodeError:
        print(json.dumps(payload, ensure_ascii=True))


def counter_key(gate: str) -> str:
    if gate not in GATES:
        raise ContractError(f"unknown gate: {gate!r} (expected one of {'/'.join(GATES)})")
    return GATES[gate][0]


def parse_record(text: str, gate: str = DEFAULT_GATE) -> dict[str, str]:
    gate_key = counter_key(gate)  # 아래 파싱 루프의 `key` 와 이름이 겹치면 누락 검사가 무력해진다
    blocks = BLOCK_RE.findall(text)
    if len(blocks) != 1:
        raise ContractError(f"exactly one review-budget block required, found {len(blocks)}")
    record: dict[str, str] = {}
    for raw in blocks[0].splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ContractError(f"malformed review-budget line: {line!r}")
        key, _, value = line.partition(":")
        key = key.strip()
        if key in record:
            raise ContractError(f"duplicate review-budget key: {key}")
        record[key] = value.strip()
    for required in ("slice_id", "work_grade", gate_key):
        if required not in record:
            raise ContractError(
                f"review-budget block missing key for gate {gate!r}: {required}. "
                "add the line (starting at 0) instead of deleting it — a missing counter "
                "must not silently reset the budget"
            )
    return record


def read_state(doc: Path, expected_slice: str | None,
               gate: str = DEFAULT_GATE) -> tuple[str, str, int, int]:
    key, budget_by_grade = GATES[gate] if gate in GATES else (counter_key(gate), {})
    record = parse_record(doc.read_text(encoding="utf-8"), gate)
    grade = record["work_grade"]
    if grade not in budget_by_grade:
        raise ContractError(f"unknown work_grade: {grade!r}")
    if expected_slice is not None and record["slice_id"] != expected_slice:
        raise ContractError(
            f"slice_id mismatch: document={record['slice_id']!r} requested={expected_slice!r}"
        )
    try:
        consumed = int(record[key])
    except ValueError:
        raise ContractError(f"{key} must be an integer: {record[key]!r}")
    if consumed < 0:
        raise ContractError(f"{key} must not be negative")
    return record["slice_id"], grade, budget_by_grade[grade], consumed


def persist(doc: Path, expected_slice: str, expected_grade: str,
            expected_consumed: int, new_consumed: int, gate: str = DEFAULT_GATE) -> None:
    """증가값을 내구 저장한다. 실패하면 예외 — 호출자는 Reviewer 를 띄우지 않는다.

    두 번째 스냅샷을 **완전히 재파싱**해 read_state 때와 동일한지 대조한다(R1-P2-2).
    블록 존재만 확인하면 read_state 이후 문서가 바뀐 경우 남의 슬라이스·등급 위에
    덮어쓰게 된다. 불일치면 **저장하지 않고** 계약 오류로 끝낸다.
    """
    key = counter_key(gate)
    text = doc.read_text(encoding="utf-8")
    record = parse_record(text, gate)
    try:
        seen_consumed = int(record[key])
    except ValueError:
        raise ContractError(f"{key} must be an integer: {record[key]!r}")
    if (record["slice_id"], record["work_grade"], seen_consumed) != (
        expected_slice, expected_grade, expected_consumed
    ):
        raise ContractError(
            "document changed between reserve and persist: "
            f"expected ({expected_slice!r}, {expected_grade!r}, {expected_consumed}) "
            f"but found ({record['slice_id']!r}, {record['work_grade']!r}, {seen_consumed}). "
            "nothing was written and no reviewer was launched"
        )
    match = BLOCK_RE.search(text)
    if match is None:  # parse_record 가 이미 보장하지만 방어적으로 유지
        raise ContractError("review-budget block disappeared before persist")
    # `rounds_consumed` 는 `doc_rounds_consumed` 의 부분문자열이다 — `^[ \t]*` 앵커가
    # 접두사 있는 줄을 배제하므로 두 축이 서로의 카운터를 건드리지 않는다.
    body, replaced = re.subn(
        rf"(?m)^([ \t]*{re.escape(key)}[ \t]*:[ \t]*)\d+[ \t]*$",  # \s* 는 줄바꿈까지 먹어 펜스를 깨뜨린다
        lambda m: f"{m.group(1)}{new_consumed}",
        match.group(1),
    )
    if replaced != 1:
        raise ContractError(
            f"expected exactly one {key} line to update, replaced {replaced}. "
            "nothing was written and no reviewer was launched"
        )
    updated = text[: match.start(1)] + body + text[match.end(1) :]
    tmp = doc.with_suffix(doc.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as handle:
        handle.write(updated)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, doc)


def acquire_lock(doc: Path, timeout_s: float) -> Path:
    lock = doc.with_suffix(doc.suffix + ".lock")
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            return lock
        except FileExistsError:
            if time.monotonic() >= deadline:
                # 자동 회수는 하지 않는다 — 살아 있는 느린 프로세스의 락을 뺏으면 이중 소비가 된다.
                # 소유 프로세스가 죽어 락이 남은 경우의 복구는 사람이 판단한다.
                raise ContractError(
                    f"could not acquire lock within {timeout_s}s: {lock}. "
                    "if no preflight is running, the owning process died — "
                    f"verify with the pid inside the file, then delete {lock.name} manually"
                )
            time.sleep(0.02)


def reserve(doc: Path, expected_slice: str | None, timeout_s: float,
            gate: str = DEFAULT_GATE) -> dict[str, Any]:
    """잠금 → 재검증 → 증가 저장 → 해제. 성공 시에만 호출 승인.

    두 축은 같은 문서·같은 잠금을 공유하므로 축이 달라도 서로 직렬화된다.
    """
    key = counter_key(gate)
    lock = acquire_lock(doc, timeout_s)
    try:
        slice_id, grade, limit, consumed = read_state(doc, expected_slice, gate)  # 잠금 안에서 재읽기
        next_round = consumed + 1
        if next_round > limit:
            raise ContractError(
                f"{gate} review budget exhausted: next_round={next_round} > budget_limit={limit} "
                f"(grade {grade}). call four-way disposition instead ({'/'.join(FOUR_WAY)}). "
                "do not re-classify the same target under the other gate — that is 증축",
                EXIT_EXHAUSTED,
            )
        try:
            persist(doc, slice_id, grade, consumed, next_round, gate)
        except OSError as exc:
            raise ContractError(f"failed to persist {key}: {exc}", EXIT_PERSIST)
        result = {
            "gate": gate,
            "slice_id": slice_id,
            "work_grade": grade,
            "budget_limit": limit,
            key: next_round,
            "reserved_round": next_round,
        }
    except BaseException:
        try:  # 실패 경로 — 해제 실패가 원래 원인을 가리지 않게 best-effort
            os.unlink(lock)
        except OSError:
            pass
        raise

    # 성공 경로의 해제 실패는 삼키지 않는다(R1-P2-1). 다음 호출이 영구 차단되므로
    # 소비는 유지한 채 Reviewer 를 띄우지 않고 Human Gate 로 돌린다.
    try:
        os.unlink(lock)
    except OSError as exc:
        raise ContractError(
            f"round {next_round} was consumed and saved, but releasing the lock failed: {exc}. "
            f"reviewer was NOT launched; resolve {lock.name} and return to the human gate",
            EXIT_UNLOCK,
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="리뷰 예산 preflight (출력 게이트 단일 진입점)")
    parser.add_argument("--doc", required=True, type=Path, help="phase 진행 문서 경로")
    parser.add_argument("--slice", dest="slice_id", help="기대 slice_id (불일치 시 fail-closed)")
    parser.add_argument("--gate", choices=sorted(GATES), default=DEFAULT_GATE,
                        help="예산 축: output=구현 diff 심사(기본), input=문서·계획·프롬프트 심사")
    parser.add_argument("--lock-timeout", type=float, default=10.0)
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="'--' 뒤에 실행할 Reviewer 명령. 생략하면 예약만 수행")
    args = parser.parse_args(argv)

    try:
        state = reserve(args.doc, args.slice_id, args.lock_timeout, args.gate)
    except ContractError as exc:
        payload = {"status": "BLOCKED", "gate": args.gate, "reason": str(exc)}
        if exc.code == EXIT_UNLOCK:  # 저장은 성공했다 — 소비를 되돌리지 않는다
            payload["round_consumed_kept"] = True
        emit(payload)
        return exc.code
    except OSError as exc:
        emit({"status": "BLOCKED", "gate": args.gate, "reason": str(exc)})
        return EXIT_CONTRACT

    command = [arg for arg in args.command if arg != "--"]
    if not command:
        emit({"status": "RESERVED", **state})
        return EXIT_OK

    try:
        completed = subprocess.run(command, check=False)
    except OSError as exc:
        # 저장은 성공했다 — 소비를 되돌리지 않고 Human Gate 로 돌린다(R2-6).
        emit({"status": "LAUNCH_FAILED", "reason": str(exc), "round_consumed_kept": True, **state})
        return EXIT_LAUNCH

    emit({"status": "LAUNCHED", "reviewer_exit_code": completed.returncode, **state})
    return completed.returncode


if __name__ == "__main__":
    sys.exit(main())
