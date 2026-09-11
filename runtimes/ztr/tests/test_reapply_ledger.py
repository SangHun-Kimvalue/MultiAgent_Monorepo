from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.engine.fix_feedback import Finding
from src.engine.reapply_ledger import (
    EMPTY_FINDINGS_DIGEST,
    MAX_ROUNDS_CEILING,
    ReapplyLedger,
    decide_terminal_state,
    findings_digest,
    gate_check,
    validate_max_rounds,
)
from src.envelope import Verdict


def test_findings_digest_is_deterministic_and_preserves_original_bytes() -> None:
    findings = [Finding(leg="검토", status="CHANGES_REQUESTED", text=" 원문 ")]
    assert findings_digest(findings) == findings_digest(findings)
    changed = [Finding(leg="검토", status="CHANGES_REQUESTED", text=" 원문!")]
    assert findings_digest(findings) != findings_digest(changed)


def test_findings_digest_preserves_whitespace_bytes() -> None:
    left = [Finding(leg="reviewer", status="CHANGES_REQUESTED", text="finding")]
    right = [Finding(leg="reviewer", status="CHANGES_REQUESTED", text=" finding ")]
    assert findings_digest(left) != findings_digest(right)


def test_findings_digest_preserves_leg_order() -> None:
    first = Finding(leg="a", status="BLOCKED", text="x")
    second = Finding(leg="b", status="CHANGES_REQUESTED", text="y")
    assert findings_digest([first, second]) != findings_digest([second, first])


def test_empty_findings_digest_is_sha256_of_canonical_empty_list() -> None:
    assert EMPTY_FINDINGS_DIGEST == findings_digest([])
    assert EMPTY_FINDINGS_DIGEST == "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"


@pytest.mark.parametrize("value", [0, -1, MAX_ROUNDS_CEILING + 1, 10**6])
def test_validate_max_rounds_rejects_out_of_range(value: int) -> None:
    with pytest.raises(ValueError):
        validate_max_rounds(value)


@pytest.mark.parametrize("value", [1, 3, MAX_ROUNDS_CEILING])
def test_validate_max_rounds_accepts_closed_range(value: int) -> None:
    validate_max_rounds(value)


@pytest.mark.parametrize(
    ("situation", "verdict", "rounds_used", "max_rounds", "prev", "this", "expected"),
    [
        ("relay PASS", Verdict.PASS, 1, 3, None, EMPTY_FINDINGS_DIGEST, "CONVERGED"),
        ("입력 PASS 조기반환의 상태 판정", Verdict.PASS, 0, 3, None, None, "CONVERGED"),
        ("CR 여유", Verdict.CHANGES_REQUESTED, 1, 3, None, "a", None),
        ("CR 소진", Verdict.CHANGES_REQUESTED, 3, 3, None, "a", "TIMEBOX_EXHAUSTED"),
        ("동일 결과", Verdict.CHANGES_REQUESTED, 2, 3, "same", "same", "NO_PROGRESS"),
        ("게이트 전 BLOCKED는 B-1b 전용", Verdict.BLOCKED, 0, 3, None, None, None),
        ("게이트 위반은 미소비", Verdict.BLOCKED, 0, 3, None, None, None),
        ("timeout 여유", Verdict.BLOCKED, 1, 3, None, None, None),
        ("internal error 여유", Verdict.BLOCKED, 1, 3, None, None, None),
        ("원장 파일 오류는 미소비", Verdict.BLOCKED, 0, 3, None, None, None),
    ],
)
def test_decide_terminal_state_covers_d_b_terminal_state_rows(
    situation: str,
    verdict: Verdict,
    rounds_used: int,
    max_rounds: int,
    prev: str | None,
    this: str | None,
    expected: str | None,
) -> None:
    del situation
    assert decide_terminal_state(
        verdict=verdict,
        rounds_used=rounds_used,
        max_rounds=max_rounds,
        prev_result_digest=prev,
        this_result_digest=this,
    ) == expected


def test_no_progress_requires_two_non_null_digests() -> None:
    assert decide_terminal_state(
        verdict=Verdict.CHANGES_REQUESTED,
        rounds_used=2,
        max_rounds=3,
        prev_result_digest=None,
        this_result_digest="same",
    ) is None


def test_no_progress_ignores_two_empty_findings_digests() -> None:
    assert decide_terminal_state(
        verdict=Verdict.CHANGES_REQUESTED,
        rounds_used=2,
        max_rounds=3,
        prev_result_digest=EMPTY_FINDINGS_DIGEST,
        this_result_digest=EMPTY_FINDINGS_DIGEST,
    ) is None


def test_priority_exhausted_and_same_digest_is_no_progress() -> None:
    assert decide_terminal_state(
        verdict=Verdict.CHANGES_REQUESTED,
        rounds_used=3,
        max_rounds=3,
        prev_result_digest="same",
        this_result_digest="same",
    ) == "NO_PROGRESS"


def test_priority_pass_and_exhausted_is_converged() -> None:
    assert decide_terminal_state(
        verdict=Verdict.PASS,
        rounds_used=3,
        max_rounds=3,
        prev_result_digest=None,
        this_result_digest=None,
    ) == "CONVERGED"


def test_priority_pass_and_same_digest_is_converged() -> None:
    assert decide_terminal_state(
        verdict=Verdict.PASS,
        rounds_used=2,
        max_rounds=3,
        prev_result_digest="same",
        this_result_digest="same",
    ) == "CONVERGED"


def test_priority_blocked_last_round_is_timebox_exhausted() -> None:
    assert decide_terminal_state(
        verdict=Verdict.BLOCKED,
        rounds_used=3,
        max_rounds=3,
        prev_result_digest=None,
        this_result_digest=None,
    ) == "TIMEBOX_EXHAUSTED"


def test_create_does_not_touch_disk_until_save(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    ledger = ReapplyLedger.create(path, phase_id="T11-F2", max_rounds=3)
    assert not path.exists()
    ledger.save()
    assert path.exists()


@pytest.mark.parametrize("max_rounds", [0, 10**6])
def test_create_invalid_max_rounds_leaves_no_file(tmp_path: Path, max_rounds: int) -> None:
    path = tmp_path / "ledger.json"
    with pytest.raises(ValueError):
        ReapplyLedger.create(path, phase_id="T11-F2", max_rounds=max_rounds)
    assert not path.exists()


def test_load_missing_path_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ReapplyLedger.load(tmp_path / "missing.json")


def test_load_or_create_uses_arguments_when_file_is_missing(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    ledger = ReapplyLedger.load_or_create(path, phase_id="T11-F2", max_rounds=3)
    assert (ledger.phase_id, ledger.max_rounds) == ("T11-F2", 3)
    assert not path.exists()


def test_load_or_create_preserves_existing_authoritative_values(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    ReapplyLedger.create(path, phase_id="original", max_rounds=2).save()
    ledger = ReapplyLedger.load_or_create(path, phase_id="other", max_rounds=5)
    assert (ledger.phase_id, ledger.max_rounds) == ("original", 2)


def test_first_create_gate_violation_does_not_persist_ledger(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    ledger = ReapplyLedger.load_or_create(path, phase_id="T11-F2", max_rounds=3)
    reason = gate_check(
        ledger,
        phase_id="T11-F2",
        approve_round=None,
        approve_findings="digest",
        input_digest="digest",
        max_rounds=3,
    )
    assert reason is not None
    assert not path.exists()


def test_first_create_gate_pass_persists_only_after_explicit_save(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    ledger = ReapplyLedger.load_or_create(path, phase_id="T11-F2", max_rounds=3)
    reason = gate_check(
        ledger,
        phase_id="T11-F2",
        approve_round=1,
        approve_findings="digest",
        input_digest="digest",
        max_rounds=3,
    )
    assert reason is None
    assert not path.exists()
    ledger.save()
    assert path.exists()


def test_gate_check_does_not_mutate_ledger_or_create_file(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    ledger = ReapplyLedger.create(path, phase_id="T11-F2", max_rounds=3)
    before = (ledger.phase_id, ledger.max_rounds, list(ledger.rounds), ledger.terminal_state)
    gate_check(
        ledger,
        phase_id="T11-F2",
        approve_round=1,
        approve_findings="digest",
        input_digest="digest",
        max_rounds=3,
    )
    after = (ledger.phase_id, ledger.max_rounds, list(ledger.rounds), ledger.terminal_state)
    assert after == before
    assert not path.exists()


def _valid_gate_ledger(tmp_path: Path) -> ReapplyLedger:
    return ReapplyLedger.create(tmp_path / "ledger.json", phase_id="phase", max_rounds=3)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ({"phase_id": "other"}, "phase_id가 원장과 일치하지 않습니다"),
        ({"max_rounds": 10**6}, "max_rounds는 1..5 정수여야 합니다: 1000000"),
        ({"max_rounds": 2}, "max_rounds가 원장과 일치하지 않습니다"),
        ({"approve_round": None}, "approve_round가 다음 라운드 번호와 일치하지 않습니다"),
        ({"approve_findings": None}, "approve_findings가 입력 findings digest와 일치하지 않습니다"),
    ],
)
def test_gate_check_returns_first_five_ordered_violation_reasons(
    tmp_path: Path,
    mutation: dict[str, Any],
    expected: str,
) -> None:
    ledger = _valid_gate_ledger(tmp_path)
    arguments: dict[str, Any] = {
        "phase_id": "phase",
        "approve_round": 1,
        "approve_findings": "digest",
        "input_digest": "digest",
        "max_rounds": 3,
    }
    arguments.update(mutation)
    assert gate_check(ledger, **arguments) == expected


def test_gate_check_rejects_terminal_ledger_before_exhaustion(tmp_path: Path) -> None:
    ledger = _valid_gate_ledger(tmp_path)
    ledger.terminal_state = "CONVERGED"
    assert gate_check(
        ledger,
        phase_id="phase",
        approve_round=1,
        approve_findings="digest",
        input_digest="digest",
        max_rounds=3,
    ) == "이미 종결된 재적용 원장은 재개할 수 없습니다"


def test_gate_check_rejects_exhausted_ledger(tmp_path: Path) -> None:
    ledger = _valid_gate_ledger(tmp_path)
    ledger.rounds.extend([{}, {}, {}])
    assert gate_check(
        ledger,
        phase_id="phase",
        approve_round=4,
        approve_findings="digest",
        input_digest="digest",
        max_rounds=3,
    ) == "재적용 라운드 상한을 모두 소진했습니다"


def test_gate_check_accepts_matching_bindings(tmp_path: Path) -> None:
    ledger = _valid_gate_ledger(tmp_path)
    assert gate_check(
        ledger,
        phase_id="phase",
        approve_round=1,
        approve_findings="digest",
        input_digest="digest",
        max_rounds=3,
    ) is None


@pytest.mark.parametrize(
    ("terminal", "arguments", "expected"),
    [
        ("NO_PROGRESS", {"phase_id": "other", "max_rounds": 99,
         "approve_round": None, "approve_findings": None}, "phase_id가 원장과 일치하지 않습니다"),
        ("NO_PROGRESS", {"max_rounds": 99, "approve_round": None,
         "approve_findings": None}, "max_rounds는 1..5 정수여야 합니다: 99"),
        ("NO_PROGRESS", {"max_rounds": 2, "approve_round": None,
         "approve_findings": None}, "max_rounds가 원장과 일치하지 않습니다"),
        ("NO_PROGRESS", {"approve_round": None, "approve_findings": None},
         "approve_round가 다음 라운드 번호와 일치하지 않습니다"),
        ("NO_PROGRESS", {"approve_round": 4, "approve_findings": None},
         "approve_findings가 입력 findings digest와 일치하지 않습니다"),
        ("NO_PROGRESS", {"approve_round": 4},
         "이미 종결된 재적용 원장은 재개할 수 없습니다"),
        (None, {"approve_round": 4}, "재적용 라운드 상한을 모두 소진했습니다"),
    ],
)
def test_gate_check_compound_violations_preserve_d_c_priority(
    tmp_path: Path, terminal: str | None, arguments: dict[str, Any], expected: str,
) -> None:
    ledger = _valid_gate_ledger(tmp_path)
    ledger.rounds.extend([{}, {}, {}])
    ledger.terminal_state = terminal
    call: dict[str, Any] = {
        "phase_id": "phase", "approve_round": 4, "approve_findings": "digest",
        "input_digest": "digest", "max_rounds": 3,
    }
    call.update(arguments)
    assert gate_check(ledger, **call) == expected


def test_load_save_round_trip_preserves_unicode_and_removes_temp_file(tmp_path: Path) -> None:
    path = tmp_path / "원장.json"
    ledger = ReapplyLedger.create(path, phase_id="단계-가", max_rounds=3)
    ledger.rounds.append(
        {
            "index": 1,
            "verdict": "CHANGES_REQUESTED",
            "exit_code": 1,
            "input_digest": "입력",
            "result_digest": None,
            "report_path": "보고서/가.json",
            "fix_prompt_path": "",
            "started_at": "2026-07-21T00:00:00+09:00",
            "duration_s": 12.5,
            "note": "한글 메모",
        }
    )
    ledger.save()
    loaded = ReapplyLedger.load(path)
    assert loaded.phase_id == ledger.phase_id
    assert loaded.rounds == ledger.rounds
    assert list(tmp_path.glob("*.tmp")) == []


def test_ledger_replace_failure_preserves_target_and_removes_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.engine.reapply_ledger as ledger_mod

    path = tmp_path / "ledger.json"
    path.write_text("기존 내용", encoding="utf-8")
    ledger = ReapplyLedger.create(path, phase_id="phase", max_rounds=3)

    def fail_replace(*_: object) -> None:
        raise OSError("fail")

    monkeypatch.setattr(ledger_mod.os, "replace", fail_replace)
    with pytest.raises(OSError, match="fail"):
        ledger.save()
    assert path.read_text(encoding="utf-8") == "기존 내용"
    assert not list(tmp_path.glob("*.tmp"))


def test_load_rejects_corrupt_json(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON 파싱 실패"):
        ReapplyLedger.load(path)


def test_load_rejects_future_version(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    path.write_text(
        json.dumps(
            {"version": 2, "phase_id": "p", "max_rounds": 3, "terminal_state": None, "rounds": []}
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="version"):
        ReapplyLedger.load(path)


def test_load_rejects_out_of_range_max_rounds(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    path.write_text(
        json.dumps(
            {"version": 1, "phase_id": "p", "max_rounds": 6, "terminal_state": None, "rounds": []}
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="max_rounds"):
        ReapplyLedger.load(path)


def test_round_index_and_remaining_boundaries(tmp_path: Path) -> None:
    ledger = ReapplyLedger.create(tmp_path / "ledger.json", phase_id="p", max_rounds=2)
    assert (ledger.next_round_index, ledger.rounds_remaining) == (1, 2)
    ledger.rounds.extend([{}, {}])
    assert (ledger.next_round_index, ledger.rounds_remaining) == (3, 0)


def test_command_digest_binds_order_values_gating_and_timeout() -> None:
    from src.engine.reapply_ledger import command_digest

    commands = [
        ("mechanical-review", '["preen", "--changed"]', True, 11.0),
        ("test", '["pytest", "focused"]', True, 12.0),
        ("implementer-reviewer", '["claude", "-p"]', True, None),
    ]
    expected = command_digest(commands)

    for changed in (
        list(reversed(commands)),
        [commands[0], ("test", '["pytest", "full"]', True, 12.0), commands[2]],
        [commands[0], ("test", commands[1][1], False, 12.0), commands[2]],
        [commands[0], ("test", commands[1][1], True, 13.0), commands[2]],
    ):
        assert command_digest(changed) != expected


def test_focused_pass_waits_for_final_verify_instead_of_converging() -> None:
    assert decide_terminal_state(
        verdict=Verdict.PASS,
        rounds_used=1,
        max_rounds=3,
        prev_result_digest=None,
        this_result_digest=EMPTY_FINDINGS_DIGEST,
        focused=True,
    ) == "AWAITING_FINAL_VERIFY"


def test_full_verification_consumer_is_write_once(tmp_path: Path) -> None:
    ledger = ReapplyLedger.create(tmp_path / "ledger.json", phase_id="phase", max_rounds=3)
    verification = {
        "base_sha": "base",
        "candidate_digest": "a" * 64,
        "command_digest": "b" * 64,
        "legs": [
            {"name": "mechanical-review", "status": "PASS", "skipped": False},
            {"name": "test", "status": "PASS", "skipped": False},
            {"name": "implementer-reviewer", "status": "PASS", "skipped": False},
        ],
        "completed_at": "2026-08-27T00:00:00+09:00",
    }

    ledger.append_full_verification(verification)
    with pytest.raises(ValueError, match="이미 소비"):
        ledger.append_full_verification(dict(verification))

    assert ledger.full_verifications == [verification]
