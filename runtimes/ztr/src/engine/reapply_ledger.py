"""재적용 라운드의 상태와 수렴 판정을 보관하는 순수 원장 모듈.

이 모듈은 LLM이나 자식 프로세스를 실행하지 않으며 원장 JSON만 읽고 쓴다.
승인 값은 round/report binding과 stale-token 차단만 제공한다. 공개 계산 가능한
digest이므로 human-presence나 rubber-stamp 방지를 보장하지 않는다.

``result_digest``는 라운드 결과 report에
``findings_digest(extract_findings(report_payload))``를 적용한 값이다. PASS 결과의 빈
findings는 ``EMPTY_FINDINGS_DIGEST``이고, report를 얻지 못한 경우만 ``None``이다.

단일 프로세스ㆍ단일 스레드 사용을 전제로 한다. ``os.replace``는 파일 교체의 원자성만
보장하며 동시 실행 안전성은 보장하지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from src.engine.fix_feedback import Finding
from src.envelope import Verdict

LEDGER_VERSION = 1
MAX_ROUNDS_FLOOR = 1
MAX_ROUNDS_CEILING = 5
DEFAULT_MAX_ROUNDS = 3

_TERMINAL_STATES = {
    "CONVERGED",
    "AWAITING_FINAL_VERIFY",
    "TIMEBOX_EXHAUSTED",
    "NO_PROGRESS",
    "ESCALATED_BLOCKED",
}
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def command_digest(commands: list[tuple[str, str, bool, float | None]]) -> str:
    """명령 순서를 보존한 ``(name, value, gating, timeout_s)`` digest."""
    canonical = json.dumps(
        [list(command) for command in commands],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def findings_digest(findings: list[Finding]) -> str:
    """findings의 순서와 원문 바이트를 보존한 canonical JSON digest를 반환한다."""
    payload = [
        {"leg": finding.leg, "status": finding.status, "text": finding.text}
        for finding in findings
    ]
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


EMPTY_FINDINGS_DIGEST: str = findings_digest([])


def validate_max_rounds(value: int) -> None:
    """라운드 상한이 코드로 허용한 닫힌 구간인지 검증한다."""
    if type(value) is not int or not MAX_ROUNDS_FLOOR <= value <= MAX_ROUNDS_CEILING:
        raise ValueError(
            f"max_rounds는 {MAX_ROUNDS_FLOOR}..{MAX_ROUNDS_CEILING} 정수여야 합니다: "
            f"{value!r}"
        )


class ReapplyLedger:
    """호출자가 지정한 경로에 저장되는 재적용 라운드 원장."""

    def __init__(
        self,
        *,
        path: Path,
        phase_id: str,
        max_rounds: int,
        terminal_state: str | None,
        rounds: list[dict[str, Any]],
        full_verifications: list[dict[str, Any]],
    ) -> None:
        self.path = path
        self.phase_id = phase_id
        self.max_rounds = max_rounds
        self.terminal_state = terminal_state
        self.rounds = rounds
        self.full_verifications = full_verifications

    @classmethod
    def create(
        cls,
        path: str | os.PathLike[str],
        *,
        phase_id: str,
        max_rounds: int,
    ) -> ReapplyLedger:
        """디스크를 건드리지 않고 새 메모리 원장을 만든다."""
        validate_max_rounds(max_rounds)
        if not phase_id:
            raise ValueError("phase_id는 빈 문자열일 수 없습니다")
        return cls(
            path=Path(path),
            phase_id=phase_id,
            max_rounds=max_rounds,
            terminal_state=None,
            rounds=[],
            full_verifications=[],
        )

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> ReapplyLedger:
        """기존 원장을 읽는다. 파일 부재나 잘못된 스키마는 조용히 복구하지 않는다."""
        ledger_path = Path(path)
        raw = ledger_path.read_text(encoding="utf-8-sig")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"재적용 원장 JSON 파싱 실패: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("재적용 원장 최상위 값은 object여야 합니다")
        if data.get("version") != LEDGER_VERSION:
            raise ValueError(
                f"지원하지 않는 재적용 원장 version: {data.get('version')!r}"
            )

        phase_id = data.get("phase_id")
        max_rounds = data.get("max_rounds")
        terminal_state = data.get("terminal_state")
        rounds = data.get("rounds")
        full_verifications = data.get("full_verifications", [])
        if not isinstance(phase_id, str) or not phase_id:
            raise ValueError("재적용 원장 phase_id는 비어 있지 않은 문자열이어야 합니다")
        if type(max_rounds) is not int:
            raise ValueError("재적용 원장 max_rounds는 정수여야 합니다")
        validate_max_rounds(max_rounds)
        if terminal_state is not None and terminal_state not in _TERMINAL_STATES:
            raise ValueError(f"지원하지 않는 terminal_state: {terminal_state!r}")
        if not isinstance(rounds, list) or not all(isinstance(item, dict) for item in rounds):
            raise ValueError("재적용 원장 rounds는 object 목록이어야 합니다")
        if not isinstance(full_verifications, list) or not all(
            isinstance(item, dict) for item in full_verifications
        ):
            raise ValueError("재적용 원장 full_verifications는 object 목록이어야 합니다")
        if len(full_verifications) > 1:
            raise ValueError("full 검증 소비 기록은 최대 1개여야 합니다")
        for round_entry in rounds:
            _validate_focused_round(round_entry)
        for verification in full_verifications:
            _validate_full_verification(verification)

        return cls(
            path=ledger_path,
            phase_id=phase_id,
            max_rounds=max_rounds,
            terminal_state=terminal_state,
            rounds=rounds,
            full_verifications=full_verifications,
        )

    @classmethod
    def load_or_create(
        cls,
        path: str | os.PathLike[str],
        *,
        phase_id: str,
        max_rounds: int,
    ) -> ReapplyLedger:
        """원장이 있으면 파일 값을 읽고, 없으면 쓰기 없이 새 원장을 만든다."""
        ledger_path = Path(path)
        if ledger_path.exists():
            return cls.load(ledger_path)
        return cls.create(ledger_path, phase_id=phase_id, max_rounds=max_rounds)

    def save(self) -> None:
        """같은 디렉터리의 임시파일을 원자적으로 교체해 원장을 저장한다."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        payload = {
            "version": LEDGER_VERSION,
            "phase_id": self.phase_id,
            "max_rounds": self.max_rounds,
            "terminal_state": self.terminal_state,
            "rounds": self.rounds,
            "full_verifications": self.full_verifications,
        }
        try:
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp_path, self.path)
        finally:
            # write/replace 실패 시 기존 target은 보존하고 이번 호출의 tmp만 정리한다.
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    @property
    def next_round_index(self) -> int:
        return len(self.rounds) + 1

    @property
    def rounds_remaining(self) -> int:
        return self.max_rounds - len(self.rounds)

    def append_full_verification(self, verification: dict[str, Any]) -> None:
        """성공한 full 검증을 정확히 한 번만 소비 기록으로 추가한다."""
        if self.full_verifications:
            raise ValueError("full 검증은 이미 소비되었습니다")
        _validate_full_verification(verification)
        self.full_verifications.append(verification)


def _validate_digest(value: object, field: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field}는 sha256 hex 문자열이어야 합니다")


def _validate_legs(value: object) -> None:
    if not isinstance(value, list):
        raise ValueError("focused round legs는 object 목록이어야 합니다")
    for leg in value:
        if not isinstance(leg, dict):
            raise ValueError("focused round leg는 object여야 합니다")
        if not isinstance(leg.get("name"), str) or not leg["name"]:
            raise ValueError("focused round leg name은 비어 있지 않은 문자열이어야 합니다")
        try:
            Verdict(leg.get("status"))
        except (TypeError, ValueError) as exc:
            raise ValueError("focused round leg status가 없거나 알 수 없는 값입니다") from exc
        if type(leg.get("skipped")) is not bool:
            raise ValueError("focused round leg skipped는 bool이어야 합니다")


def _validate_focused_round(round_entry: dict[str, Any]) -> None:
    focused = round_entry.get("focused")
    if focused is None:
        return
    if type(focused) is not bool:
        raise ValueError("round focused는 bool이어야 합니다")
    if not focused:
        return
    _validate_legs(round_entry.get("legs"))
    _validate_digest(round_entry.get("candidate_digest"), "candidate_digest")
    _validate_digest(round_entry.get("command_digest"), "command_digest")
    base_sha = round_entry.get("base_sha")
    if not isinstance(base_sha, str) or not base_sha.strip():
        raise ValueError("focused round base_sha는 비어 있지 않은 문자열이어야 합니다")


def _validate_full_verification(verification: dict[str, Any]) -> None:
    base_sha = verification.get("base_sha")
    if not isinstance(base_sha, str) or not base_sha.strip():
        raise ValueError("full verification base_sha는 비어 있지 않은 문자열이어야 합니다")
    _validate_digest(verification.get("candidate_digest"), "candidate_digest")
    _validate_digest(verification.get("command_digest"), "command_digest")
    _validate_legs(verification.get("legs"))
    completed_at = verification.get("completed_at")
    if not isinstance(completed_at, str) or not completed_at:
        raise ValueError("full verification completed_at은 비어 있지 않은 문자열이어야 합니다")


def final_verify_gate_check(
    ledger: ReapplyLedger,
    *,
    base_sha: str,
    candidate_digest: str,
    command_digest_value: str,
) -> str | None:
    """구조화된 원장 사실만으로 final-verify 진입 조건을 순서대로 검사한다."""
    if not ledger.rounds or ledger.rounds[-1].get("focused") is not True:
        return "마지막 라운드가 focused 라운드가 아닙니다"
    last_round = ledger.rounds[-1]
    try:
        _validate_focused_round(last_round)
    except ValueError as exc:
        return str(exc)
    legs = last_round["legs"]
    if any(leg["status"] != Verdict.PASS.value for leg in legs):
        return "마지막 focused 라운드의 gating step이 전부 PASS가 아닙니다"
    for required in ("mechanical-review", "implementer-reviewer"):
        matching = [leg for leg in legs if leg["name"] == required]
        if not matching:
            return f"마지막 focused 라운드에 {required} step이 없습니다"
        if matching[-1]["skipped"] is True:
            return f"마지막 focused 라운드의 {required} step이 skipped 상태입니다"
    if last_round["base_sha"] != base_sha:
        return "base_sha가 focused 라운드 기록과 일치하지 않습니다"
    if last_round["candidate_digest"] != candidate_digest:
        return "candidate_digest가 focused 라운드 기록과 일치하지 않습니다"
    if last_round["command_digest"] != command_digest_value:
        return "command_digest가 focused 라운드 기록과 일치하지 않습니다"
    if ledger.full_verifications:
        return "full 검증은 이미 소비되었습니다"
    return None


def decide_terminal_state(
    *,
    verdict: Verdict,
    rounds_used: int,
    max_rounds: int,
    prev_result_digest: str | None,
    this_result_digest: str | None,
    focused: bool = False,
) -> str | None:
    """구조화 verdict, 라운드 수, digest 동일성만으로 종결 상태를 결정한다."""
    if verdict == Verdict.PASS:
        return "AWAITING_FINAL_VERIFY" if focused else "CONVERGED"
    if (
        prev_result_digest is not None
        and this_result_digest is not None
        and prev_result_digest != EMPTY_FINDINGS_DIGEST
        and this_result_digest != EMPTY_FINDINGS_DIGEST
        and prev_result_digest == this_result_digest
    ):
        return "NO_PROGRESS"
    if rounds_used >= max_rounds:
        return "TIMEBOX_EXHAUSTED"
    return None


def gate_check(
    ledger: ReapplyLedger,
    *,
    phase_id: str,
    approve_round: int | None,
    approve_findings: str | None,
    input_digest: str,
    max_rounds: int,
) -> str | None:
    """라운드 진입 binding을 순서대로 검사하며 원장을 변경하지 않는다.

    이 게이트는 round/report binding과 stale-token 차단까지만 제공한다. digest는 공개
    계산 가능하므로 human-presence나 rubber-stamp 방지를 보장하지 않는다.
    """
    if phase_id != ledger.phase_id:
        return "phase_id가 원장과 일치하지 않습니다"
    try:
        validate_max_rounds(max_rounds)
    except ValueError as exc:
        return str(exc)
    if max_rounds != ledger.max_rounds:
        return "max_rounds가 원장과 일치하지 않습니다"
    if approve_round is None or approve_round != ledger.next_round_index:
        return "approve_round가 다음 라운드 번호와 일치하지 않습니다"
    if approve_findings is None or approve_findings != input_digest:
        return "approve_findings가 입력 findings digest와 일치하지 않습니다"
    if ledger.terminal_state is not None:
        return "이미 종결된 재적용 원장은 재개할 수 없습니다"
    if ledger.rounds_remaining <= 0:
        return "재적용 라운드 상한을 모두 소진했습니다"
    return None
