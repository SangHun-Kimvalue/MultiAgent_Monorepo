"""Envelope 모델 단위 테스트."""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from src.envelope import (
    Envelope,
    INTERNAL_ERROR_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    Verdict,
    exit_code_for_verdict,
    redact_stderr,
)


class TestVerdictExitCode:
    def test_exit_code_mapping(self) -> None:
        assert exit_code_for_verdict(Verdict.PASS) == 0
        assert exit_code_for_verdict(Verdict.CHANGES_REQUESTED) == 1
        assert exit_code_for_verdict(Verdict.BLOCKED) == 2
        assert TIMEOUT_EXIT_CODE == 124
        assert INTERNAL_ERROR_EXIT_CODE == 70


class TestRedaction:
    def test_redacts_32_char_token(self) -> None:
        token = "a" * 32
        assert redact_stderr(f"token={token}") == "token=[REDACTED]"

    def test_keeps_short_values(self) -> None:
        assert redact_stderr("token=short-token") == "token=short-token"

    def test_redacts_hyphenated_token(self) -> None:
        token = "abcd" * 8 + "-SECRET"
        assert "[REDACTED]" in redact_stderr(token)


class TestEnvelope:
    def test_from_verdict_redacts_and_maps_exit_code(self) -> None:
        env = Envelope.from_verdict(
            status=Verdict.CHANGES_REQUESTED,
            backend="ollama",
            model="qwen2.5-coder:7b",
            duration_s=1.25,
            stdout="review output",
            stderr="api_key=" + "x" * 40,
            fallback_used=True,
            not_claimed=["live_e2e"],
        )

        assert env.exit_code == 1
        assert env.stderr_sanitized == "api_key=[REDACTED]"
        assert env.fallback_used is True
        assert env.not_claimed == ["live_e2e"]

    def test_json_round_trip(self) -> None:
        original = Envelope.from_verdict(
            status=Verdict.PASS,
            backend="nitpicker",
            model="local",
            duration_s=0.01,
        )
        payload = original.model_dump_json()
        restored = Envelope.model_validate(json.loads(payload))
        assert restored == original

    def test_rejects_unknown_exit_code(self) -> None:
        with pytest.raises(ValidationError, match="지원하지 않는 exit_code"):
            Envelope(
                status=Verdict.BLOCKED,
                exit_code=99,
                backend="test",
                model="test",
                duration_s=0.0,
            )

    def test_rejects_status_exit_code_mismatch(self) -> None:
        with pytest.raises(ValidationError, match="status PASS"):
            Envelope(
                status=Verdict.PASS,
                exit_code=1,
                backend="test",
                model="test",
                duration_s=0.0,
            )

    def test_timeout_exit_code_requires_blocked_status(self) -> None:
        with pytest.raises(ValidationError, match="BLOCKED status"):
            Envelope(
                status=Verdict.PASS,
                exit_code=TIMEOUT_EXIT_CODE,
                backend="test",
                model="test",
                duration_s=0.0,
            )

    def test_blocked_allows_timeout_and_internal_codes(self) -> None:
        timeout_env = Envelope(
            status=Verdict.BLOCKED,
            exit_code=TIMEOUT_EXIT_CODE,
            backend="test",
            model="test",
            duration_s=0.0,
        )
        internal_env = Envelope(
            status=Verdict.BLOCKED,
            exit_code=INTERNAL_ERROR_EXIT_CODE,
            backend="test",
            model="test",
            duration_s=0.0,
        )
        assert timeout_env.exit_code == TIMEOUT_EXIT_CODE
        assert internal_env.exit_code == INTERNAL_ERROR_EXIT_CODE

    def test_stdout_payload_uses_json_values(self) -> None:
        env = Envelope.from_verdict(
            status=Verdict.BLOCKED,
            backend="test",
            model="test",
            duration_s=0.0,
        )
        assert env.as_stdout_payload()["status"] == "BLOCKED"
