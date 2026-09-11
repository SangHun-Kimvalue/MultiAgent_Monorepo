"""nit_envelope 어댑터 계약 테스트.

fake 닛피커(exit code만 다른 작은 파이썬)로 매핑을 검증하고,
emit된 envelope를 ztr 실제 Envelope로 재검증해 계약 준수를 증명한다.
ztr import 불가 환경에서는 Envelope 재검증만 skip한다(매핑 검증은 항상 수행).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ADAPTER = str(Path(__file__).resolve().parent / "nit_envelope.py")


def _fake_nitpicker(exit_code: int, out: str = "review-body") -> list[str]:
    # 실제 mini_nitpicker처럼 utf-8 stdout 강제(LESSON-016) → 어댑터 utf-8 패스스루 검증
    code = (
        "import sys, io; "
        "sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8'); "
        f"print('{out} 한글'); sys.exit({exit_code})"
    )
    return ["--", sys.executable, "-c", code]


def _fake_mini(exit_code: int, *, status: str | None = "REVIEWED",
               tally: str | None = '{"requested": 1, "reviewed": 1, "unreviewed": 0}',
               extra: str = "") -> list[str]:
    """토큰을 내보내는 fake mini_nitpicker. status=None이면 토큰 없는 구버전."""
    lines = ["review-body 한글"]
    if status is not None:
        lines.append(f"MINI_NITPICKER_STATUS={status}")
    if tally is not None:
        lines.append(f"MINI_NITPICKER_TALLY={tally}")
    if extra:
        lines.append(extra)
    # 자식 소스에 본문을 문자열로 끼워 넣지 않는다 — 개행·따옴표 이스케이프가 깨진다.
    # 리스트 repr로 넘기면 파이썬이 알아서 안전하게 직렬화한다.
    code = (
        "import sys, io; "
        "sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8'); "
        f"[print(x) for x in {lines!r}]; sys.exit({exit_code})"
    )
    return ["--", sys.executable, "-c", code]


def _run_cmd(*args: str) -> tuple[dict, int]:
    proc = subprocess.run(
        [sys.executable, ADAPTER, *args],
        text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    return json.loads(lines[-1]), proc.returncode


def _run_mini(exit_code: int, **kw) -> tuple[dict, int]:
    flags = list(kw.pop("flags", []))
    return _run_cmd("--style", "mini", *flags, *_fake_mini(exit_code, **kw))


def _run(style: str, exit_code: int) -> tuple[dict, int]:
    proc = subprocess.run(
        [sys.executable, ADAPTER, "--backend", "nitpicker", "--model", "qwen2.5-coder:7b",
         "--style", style, *_fake_nitpicker(exit_code)],
        text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    return json.loads(lines[-1]), proc.returncode


def _maybe_validate_against_ztr(env: dict) -> None:
    """ztr import 가능하면 실제 Envelope로 재검증(계약 준수 증명)."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "runtimes" / "ztr"))
        from src.envelope import Envelope  # noqa: PLC0415
    except Exception:  # pragma: no cover - ztr 미가용 환경
        return
    Envelope.model_validate(env)  # extra=forbid·status↔exit 짝 검증


def test_mini_style_pass() -> None:
    # exit 0 **+ STATUS=REVIEWED + 미검토 0건** 이라야 PASS다.
    env, rc = _run_mini(0)
    assert (env["status"], env["exit_code"], rc) == ("PASS", 0, 0)
    assert env["fallback_used"] is False and env["not_claimed"] == []
    assert "한글" in env["stdout"]
    _maybe_validate_against_ztr(env)


# ── (exit, STATUS) 튜플 판정 — exit 0 단독으로는 PASS가 되지 않는다 ────────────

def test_exit0_without_status_token_is_blocked_not_pass() -> None:
    """토큰 없는 구버전 닛피커의 exit 0을 PASS로 승격하지 않는다(fail-closed)."""
    env, rc = _run_mini(0, status=None, tally=None)
    assert (env["status"], env["exit_code"], rc) == ("BLOCKED", 2, 2)
    assert "nitpicker-status-token-missing" in env["not_claimed"]
    _maybe_validate_against_ztr(env)


def test_exit0_with_missing_token_allowed_records_degradation() -> None:
    """명시 허용 시에만 PASS. 조용히 통과하지 않고 열화를 envelope에 남긴다."""
    env, rc = _run_mini(0, status=None, tally=None,
                        flags=["--allow-missing-status-token"])
    assert (env["status"], rc) == ("PASS", 0)
    assert "nitpicker-status-token-missing-allowed" in env["not_claimed"]
    _maybe_validate_against_ztr(env)


def test_exit0_with_non_reviewed_status_is_blocked() -> None:
    """계약 위반(무검토인데 exit 0)을 PASS로 세탁하지 않는다."""
    for bad in ("NO_TARGETS", "NO_DIFF", "SKIPPED", "UNRESOLVED_REPO", "ERROR"):
        env, rc = _run_mini(0, status=bad, tally=None)
        assert (env["status"], rc) == ("BLOCKED", 2), bad
        assert f"nitpicker-status-not-reviewed-{bad}" in env["not_claimed"], bad


def test_exit0_with_partial_review_is_blocked() -> None:
    """일부만 리뷰됐는데 exit 0이면 나머지는 한 번도 안 본 것이다."""
    env, rc = _run_mini(
        0, tally='{"requested": 5, "reviewed": 1, "unreviewed": 4}')
    assert (env["status"], rc) == ("BLOCKED", 2)
    assert "nitpicker-partial-review-unreviewed-4" in env["not_claimed"]


def test_exit0_with_zero_requested_is_blocked() -> None:
    """대상 0건은 통과가 아니라 무검토다(LESSON-M046)."""
    env, rc = _run_mini(
        0, tally='{"requested": 0, "reviewed": 0, "unreviewed": 0}')
    assert (env["status"], rc) == ("BLOCKED", 2)
    assert "nitpicker-tally-requested-0" in env["not_claimed"]


def test_exit0_with_conflicting_status_tokens_is_blocked() -> None:
    """어느 토큰이 진짜인지 모르면 추측하지 않고 닫는다."""
    env, rc = _run_mini(0, extra="MINI_NITPICKER_STATUS=NO_TARGETS")
    assert (env["status"], rc) == ("BLOCKED", 2)
    assert "nitpicker-status-token-ambiguous" in env["not_claimed"]


def test_exit0_without_tally_is_blocked() -> None:
    """R3-P1: TALLY가 없으면 '미검토 0건'을 증명할 수 없다 — STATUS만으로 PASS 금지."""
    env, rc = _run_mini(0, tally=None)
    assert (env["status"], rc) == ("BLOCKED", 2)
    assert "nitpicker-tally-missing" in env["not_claimed"]


def test_exit0_with_multiple_tallies_is_blocked() -> None:
    """R3-P2: 상충하는 TALLY 두 줄은 각각은 통과한다 — 개수로 닫는다."""
    env, rc = _run_mini(
        0, extra='MINI_NITPICKER_TALLY={"requested": 99, "reviewed": 99, "unreviewed": 0}')
    assert (env["status"], rc) == ("BLOCKED", 2)
    assert "nitpicker-tally-ambiguous" in env["not_claimed"]


def test_exit0_with_duplicate_status_tokens_is_blocked() -> None:
    """STATUS도 정확히 한 줄이어야 한다(값이 같아도 계약 위반)."""
    env, rc = _run_mini(0, extra="MINI_NITPICKER_STATUS=REVIEWED")
    assert (env["status"], rc) == ("BLOCKED", 2)
    assert "nitpicker-status-token-ambiguous" in env["not_claimed"]


def test_exit0_with_missing_tally_field_is_blocked() -> None:
    """R3-P1: `reviewed` 누락처럼 필드가 빠진 집계는 전량 리뷰를 증명하지 못한다."""
    env, rc = _run_mini(0, tally='{"requested": 5, "unreviewed": 0}')
    assert (env["status"], rc) == ("BLOCKED", 2)
    assert "nitpicker-tally-field-invalid-reviewed" in env["not_claimed"]


def test_exit0_with_negative_tally_field_is_blocked() -> None:
    """음수 집계는 `unreviewed > 0` 검사를 우회해 부분 리뷰를 세탁한다."""
    env, rc = _run_mini(0, tally='{"requested": 5, "reviewed": 1, "unreviewed": -4}')
    assert (env["status"], rc) == ("BLOCKED", 2)
    assert "nitpicker-tally-field-invalid-unreviewed" in env["not_claimed"]


def test_exit0_with_boolean_tally_field_is_blocked() -> None:
    """bool은 int의 서브클래스라 isinstance만으로는 걸러지지 않는다(`True == 1`)."""
    env, rc = _run_mini(0, tally='{"requested": true, "reviewed": true, "unreviewed": 0}')
    assert (env["status"], rc) == ("BLOCKED", 2)
    assert "nitpicker-tally-field-invalid-requested" in env["not_claimed"]


def test_exit0_with_inconsistent_tally_sum_is_blocked() -> None:
    """R3-P1: `requested != reviewed + unreviewed` 면 집계 자체를 믿을 수 없다."""
    for bad in ('{"requested": 5, "reviewed": 0, "unreviewed": 0}',
                '{"requested": 5, "reviewed": 6, "unreviewed": 0}'):
        env, rc = _run_mini(0, tally=bad)
        assert (env["status"], rc) == ("BLOCKED", 2), bad
        assert "nitpicker-tally-inconsistent" in env["not_claimed"], bad


def test_exit0_with_unparsable_tally_is_blocked() -> None:
    env, rc = _run_mini(0, tally="not-json")
    assert (env["status"], rc) == ("BLOCKED", 2)
    assert "nitpicker-tally-unparsable" in env["not_claimed"]


def test_mini_exit3_is_named_no_review_not_unexpected() -> None:
    """무검토(exit 3)를 '미지의 코드'가 아니라 무검토로 이름 붙여 보고한다."""
    env, rc = _run_mini(3, status="NO_TARGETS", tally=None)
    assert (env["status"], env["exit_code"], rc) == ("BLOCKED", 2, 2)
    assert "nitpicker-no-review-exit-3" in env["not_claimed"]
    assert not any("unexpected" in nc for nc in env["not_claimed"])
    _maybe_validate_against_ztr(env)


def test_runnit_pass_records_unverified() -> None:
    """run_nit은 STATUS 토큰이 없어 튜플 판정이 불가능하다 — 막지 않되 기록한다."""
    env, rc = _run("runnit", 0)
    assert (env["status"], rc) == ("PASS", 0)
    assert "nitpicker-review-unverified-no-status-token" in env["not_claimed"]
    _maybe_validate_against_ztr(env)


def test_status_token_is_not_read_from_prose() -> None:
    """산문 안에 토큰 문자열이 들어 있어도 줄 접두사가 아니면 무시한다(R5)."""
    env, rc = _run_mini(
        0, status=None, tally=None,
        extra="see MINI_NITPICKER_STATUS=REVIEWED in the docs")
    assert (env["status"], rc) == ("BLOCKED", 2)
    assert "nitpicker-status-token-missing" in env["not_claimed"]


def test_mini_style_changes() -> None:
    env, rc = _run("mini", 1)
    assert (env["status"], env["exit_code"], rc) == ("CHANGES_REQUESTED", 1, 1)
    _maybe_validate_against_ztr(env)


def test_mini_style_blocked() -> None:
    env, rc = _run("mini", 2)
    assert (env["status"], env["exit_code"], rc) == ("BLOCKED", 2, 2)
    _maybe_validate_against_ztr(env)


def test_runnit_style_mapping() -> None:
    assert _run("runnit", 0)[0]["status"] == "PASS"
    env_cr, rc_cr = _run("runnit", 2)
    assert (env_cr["status"], rc_cr) == ("CHANGES_REQUESTED", 1)
    env_bl, rc_bl = _run("runnit", 3)
    assert (env_bl["status"], rc_bl) == ("BLOCKED", 2)
    _maybe_validate_against_ztr(env_cr)


def test_unexpected_exit_is_blocked_not_pass() -> None:
    # silent-PASS 금지: 미지의 exit code는 BLOCKED로
    env, rc = _run("mini", 5)
    assert (env["status"], env["exit_code"], rc) == ("BLOCKED", 2, 2)
    assert any("unexpected-exit-5" in nc for nc in env["not_claimed"])
    _maybe_validate_against_ztr(env)


def test_timeout_is_blocked_124() -> None:
    # --timeout 짧게 + 오래 자는 자식 → BLOCKED/124 (silent-PASS 아님)
    proc = subprocess.run(
        [sys.executable, ADAPTER, "--timeout", "0.5",
         "--", sys.executable, "-c", "import time; time.sleep(10)"],
        text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    env = json.loads(proc.stdout.splitlines()[-1])
    assert (env["status"], env["exit_code"], proc.returncode) == ("BLOCKED", 124, 124)
    assert "nitpicker-timeout" in env["not_claimed"]
    _maybe_validate_against_ztr(env)


def test_spawn_failure_is_blocked_70() -> None:
    proc = subprocess.run(
        [sys.executable, ADAPTER, "--", "no_such_executable_xyz_123"],
        text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    env = json.loads(proc.stdout.splitlines()[-1])
    assert (env["status"], env["exit_code"], proc.returncode) == ("BLOCKED", 70, 70)
    assert "nitpicker-spawn-failed" in env["not_claimed"]
    _maybe_validate_against_ztr(env)


def test_stderr_secret_is_redacted() -> None:
    # stderr에 32자+ 토큰을 흘리는 자식 → stderr_sanitized에 [REDACTED]
    secret = "A" * 40
    code = (
        "import sys, io; "
        "sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8'); "
        f"sys.stderr.write('token={secret} end'); sys.exit(0)"
    )
    proc = subprocess.run(
        [sys.executable, ADAPTER, "--", sys.executable, "-c", code],
        text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    env = json.loads(proc.stdout.splitlines()[-1])
    assert secret not in env["stderr_sanitized"]
    assert "[REDACTED]" in env["stderr_sanitized"]
    _maybe_validate_against_ztr(env)


def test_empty_command_blocked() -> None:
    proc = subprocess.run(
        [sys.executable, ADAPTER, "--style", "mini"],
        text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    env = json.loads(proc.stdout.splitlines()[-1])
    assert (env["status"], proc.returncode) == ("BLOCKED", 2)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL PASS")
