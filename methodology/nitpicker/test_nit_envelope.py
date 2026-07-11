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
    env, rc = _run("mini", 0)
    assert (env["status"], env["exit_code"], rc) == ("PASS", 0, 0)
    assert env["fallback_used"] is False and env["not_claimed"] == []
    assert "한글" in env["stdout"]
    _maybe_validate_against_ztr(env)


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
