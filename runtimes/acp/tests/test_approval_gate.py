from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from acp.approval_gate import (
    ApprovalBinding,
    ApprovalBlocked,
    ApprovalClaimLevel,
    ApprovalGateService,
    ApprovalResult,
    HmacSha256Verifier,
)
from acp.store import SessionStore

NOW = datetime(2026, 7, 22, 3, 4, 5, tzinfo=timezone.utc)
SECRET = b"test-secret-that-is-injected-not-loaded"


def _binding(**changes: Any) -> ApprovalBinding:
    values: dict[str, Any] = {
        "phase_id": "T10-B3",
        "round": 1,
        "findings_digest": "a" * 64,
        "session_id": "implementer-session-id",
        "issuer_id": "external-issuer",
        "key_id": "key-2026-01",
    }
    values.update(changes)
    return ApprovalBinding.model_validate(values, strict=True)


def _service(store: SessionStore) -> ApprovalGateService:
    verifier = HmacSha256Verifier({("external-issuer", "key-2026-01"): SECRET})
    return ApprovalGateService(store, verifier)


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _artifact(
    service: ApprovalGateService,
    *,
    binding: ApprovalBinding | None = None,
    now: datetime = NOW,
    external_id: str = "provider-event-id",
    ttl: int = 60,
    changes: dict[str, Any] | None = None,
    sign: bool = True,
) -> tuple[ApprovalBinding, dict[str, Any]]:
    selected = binding or _binding()
    challenge = service.issue_challenge(selected, ttl_seconds=ttl, now=now)
    value: dict[str, Any] = {
        "schema_version": 1,
        "decision": "APPROVE_REAPPLY",
        "challenge_id": challenge.challenge_id,
        "nonce": challenge.nonce,
        **selected.model_dump(),
        "external_approval_id": external_id,
        "issued_at": challenge.issued_at,
        "expires_at": challenge.expires_at,
        "approved_at": now.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "alg": "HMAC-SHA256",
    }
    if changes:
        value.update(changes)
    signature = hmac.new(SECRET, _canonical(value), hashlib.sha256).digest() if sign else b"x" * 32
    value["signature"] = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    return selected, value


def _raw(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def _resign(value: dict[str, Any]) -> None:
    unsigned = {key: item for key, item in value.items() if key != "signature"}
    value["signature"] = base64.urlsafe_b64encode(
        hmac.new(SECRET, _canonical(unsigned), hashlib.sha256).digest()
    ).rstrip(b"=").decode()


def test_positive_issue_then_single_consume_and_redacted_row_shape(tmp_store: SessionStore) -> None:
    service = _service(tmp_store)
    binding, value = _artifact(service)

    receipt = service.consume(_raw(value), expected_binding=binding, now=NOW)

    assert receipt.result is ApprovalResult.APPROVAL_VALID
    assert receipt.claim_level is ApprovalClaimLevel.DETERMINISTIC_VERIFIER
    with pytest.raises(ApprovalBlocked):
        service.consume(_raw(value), expected_binding=binding, now=NOW)
    rows = tmp_store._conn.execute(
        "SELECT event_type, external_approval_id, result FROM approval_audit ORDER BY id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("ISSUE", None, None),
        ("CONSUME", "provider-event-id", "APPROVAL_VALID"),
    ]
    dump = " ".join(str(tuple(row)) for row in tmp_store._conn.execute(
        "SELECT * FROM approval_challenges"
    ).fetchall())
    assert value["nonce"] not in dump
    assert value["signature"] not in dump
    assert SECRET.decode() not in dump


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("phase_id", "T10-X"),
        ("round", 2),
        ("findings_digest", "b" * 64),
        ("session_id", "wrong-session"),
        ("issuer_id", "wrong-issuer"),
        ("key_id", "wrong-key"),
    ],
)
def test_signed_wrong_binding_is_blocked(
    tmp_store: SessionStore, field: str, replacement: Any
) -> None:
    service = _service(tmp_store)
    binding, value = _artifact(service, changes={field: replacement})
    unsigned = {key: item for key, item in value.items() if key != "signature"}
    value["signature"] = base64.urlsafe_b64encode(
        hmac.new(SECRET, _canonical(unsigned), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    with pytest.raises(ApprovalBlocked):
        service.consume(_raw(value), expected_binding=binding, now=NOW)


def test_forged_and_tampered_mac_are_blocked(tmp_store: SessionStore) -> None:
    service = _service(tmp_store)
    binding, forged = _artifact(service, sign=False)
    with pytest.raises(ApprovalBlocked):
        service.consume(_raw(forged), expected_binding=binding, now=NOW)
    binding2, tampered = _artifact(service)
    tampered["external_approval_id"] = "tampered-after-signing"
    with pytest.raises(ApprovalBlocked):
        service.consume(_raw(tampered), expected_binding=binding2, now=NOW)


def test_expired_and_future_clock_skew_are_blocked(tmp_store: SessionStore) -> None:
    service = _service(tmp_store)
    binding, expired = _artifact(service, ttl=30)
    with pytest.raises(ApprovalBlocked):
        service.consume(_raw(expired), expected_binding=binding, now=NOW + timedelta(seconds=31))
    binding2, future = _artifact(
        service,
        changes={"approved_at": (NOW + timedelta(seconds=31)).isoformat().replace("+00:00", "Z")},
    )
    unsigned = {key: item for key, item in future.items() if key != "signature"}
    future["signature"] = base64.urlsafe_b64encode(
        hmac.new(SECRET, _canonical(unsigned), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    with pytest.raises(ApprovalBlocked):
        service.consume(_raw(future), expected_binding=binding2, now=NOW)


@pytest.mark.parametrize("ttl", [True, 29, 901, 30.0])
def test_ttl_requires_exact_bounded_int(tmp_store: SessionStore, ttl: Any) -> None:
    with pytest.raises(ApprovalBlocked):
        _service(tmp_store).issue_challenge(_binding(), ttl_seconds=ttl, now=NOW)


@pytest.mark.parametrize("invalid_round", [True, 1.0])
def test_issue_revalidates_constructed_binding_before_side_effects(
    tmp_store: SessionStore, invalid_round: Any
) -> None:
    invalid = ApprovalBinding.model_construct(
        **{**_binding().model_dump(), "round": invalid_round}
    )
    with pytest.raises(ApprovalBlocked):
        _service(tmp_store).issue_challenge(invalid, ttl_seconds=60, now=NOW)
    assert tmp_store._conn.execute("SELECT count(*) FROM approval_challenges").fetchone()[0] == 0
    assert tmp_store._conn.execute("SELECT count(*) FROM approval_audit").fetchone()[0] == 0


def test_issue_rejects_wrong_object_without_side_effects(tmp_store: SessionStore) -> None:
    with pytest.raises(ApprovalBlocked):
        _service(tmp_store).issue_challenge(object(), ttl_seconds=60, now=NOW)  # type: ignore[arg-type]
    assert tmp_store._conn.execute("SELECT count(*) FROM approval_challenges").fetchone()[0] == 0
    assert tmp_store._conn.execute("SELECT count(*) FROM approval_audit").fetchone()[0] == 0


@pytest.mark.parametrize("field", ["external_approval_id", "phase_id"])
def test_missing_fields_are_blocked(tmp_store: SessionStore, field: str) -> None:
    service = _service(tmp_store)
    binding, value = _artifact(service)
    del value[field]
    with pytest.raises(ApprovalBlocked):
        service.consume(_raw(value), expected_binding=binding, now=NOW)


def test_duplicate_or_empty_external_id_is_blocked(tmp_store: SessionStore) -> None:
    service = _service(tmp_store)
    binding, value = _artifact(service, external_id="")
    with pytest.raises(ApprovalBlocked):
        service.consume(_raw(value), expected_binding=binding, now=NOW)
    binding2, valid = _artifact(service)
    text = _raw(valid).decode().replace(
        '"external_approval_id":"provider-event-id"',
        '"external_approval_id":"provider-event-id","external_approval_id":"duplicate"',
    )
    with pytest.raises(ApprovalBlocked):
        service.consume(text.encode(), expected_binding=binding2, now=NOW)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: value.update(extra="unknown"),
        lambda value: value.update(round=True),
        lambda value: value.update(schema_version=True),
        lambda value: value.update(issued_at="2026-07-22T03:04:05+00:00"),
        lambda value: value.update(nonce="bad="),
        lambda value: value.update(signature="%%%"),
    ],
)
def test_strict_schema_and_encodings_blocked(
    tmp_store: SessionStore, mutator: Any
) -> None:
    service = _service(tmp_store)
    binding, value = _artifact(service)
    mutator(value)
    with pytest.raises(ApprovalBlocked):
        service.consume(_raw(value), expected_binding=binding, now=NOW)


@pytest.mark.parametrize("raw", [b"\xff", b"{", b"[]", b'{"schema_version":1,"schema_version":1}'])
def test_malformed_utf8_json_and_duplicate_key_blocked(
    tmp_store: SessionStore, raw: bytes
) -> None:
    with pytest.raises(ApprovalBlocked):
        _service(tmp_store).consume(raw, expected_binding=_binding(), now=NOW)


def test_agent_writable_unsigned_json_has_no_authority(tmp_store: SessionStore) -> None:
    service = _service(tmp_store)
    binding, value = _artifact(service, sign=False)
    with pytest.raises(ApprovalBlocked):
        service.consume(_raw(value), expected_binding=binding, now=NOW)


@pytest.mark.parametrize("sensitive_field", ["nonce", "signature"])
def test_external_parse_marker_is_absent_from_exception_and_storage(
    tmp_store: SessionStore, sensitive_field: str
) -> None:
    marker = "RAW-MARKER-MUST-NOT-LEAK"
    service = _service(tmp_store)
    binding, value = _artifact(service)
    value[sensitive_field] = [marker]
    try:
        service.consume(_raw(value), expected_binding=binding, now=NOW)
    except ApprovalBlocked as exc:
        rendered = "".join(traceback.format_exception(exc))
        assert marker not in str(exc)
        assert marker not in repr(exc)
        assert marker not in rendered
        assert exc.__cause__ is None
        assert exc.__context__ is None
    else:
        pytest.fail("malformed external field gained approval authority")
    database_dump = " ".join(
        str(tuple(row))
        for table in ("approval_challenges", "approval_audit")
        for row in tmp_store._conn.execute(f"SELECT * FROM {table}").fetchall()
    )
    assert marker not in database_dump


def test_verifier_requires_mapping_and_copies_allowlist() -> None:
    with pytest.raises(TypeError):
        HmacSha256Verifier(lambda _issuer, _key: SECRET)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        HmacSha256Verifier("secret-file-path")  # type: ignore[arg-type]
    source = {("external-issuer", "key-2026-01"): SECRET}
    verifier = HmacSha256Verifier(source)
    source[("unknown", "unknown")] = b"default-secret"
    artifact = {"issuer_id": "unknown", "key_id": "unknown", "value": 1}
    signature = hmac.new(b"default-secret", _canonical(artifact), hashlib.sha256).digest()
    assert verifier.verify(artifact, signature) is False


@pytest.mark.parametrize("offset", [-31, 31])
def test_approved_at_outside_skew_is_blocked(tmp_store: SessionStore, offset: int) -> None:
    service = _service(tmp_store)
    binding, value = _artifact(service, now=NOW - timedelta(seconds=60), ttl=120)
    value["approved_at"] = (NOW + timedelta(seconds=offset)).isoformat().replace("+00:00", "Z")
    _resign(value)
    with pytest.raises(ApprovalBlocked):
        service.consume(_raw(value), expected_binding=binding, now=NOW)


@pytest.mark.parametrize("offset", [-30, 30])
def test_approved_at_exact_skew_boundary_is_allowed(tmp_store: SessionStore, offset: int) -> None:
    service = _service(tmp_store)
    binding, value = _artifact(
        service, now=NOW - timedelta(seconds=60), ttl=120,
        external_id=f"boundary-{offset}",
    )
    value["approved_at"] = (NOW + timedelta(seconds=offset)).isoformat().replace("+00:00", "Z")
    _resign(value)
    assert service.consume(_raw(value), expected_binding=binding, now=NOW).result is (
        ApprovalResult.APPROVAL_VALID
    )


def test_external_id_cannot_be_reused_for_another_challenge(tmp_store: SessionStore) -> None:
    service = _service(tmp_store)
    binding, first = _artifact(service)
    service.consume(_raw(first), expected_binding=binding, now=NOW)
    binding2, second = _artifact(service)
    with pytest.raises(ApprovalBlocked):
        service.consume(_raw(second), expected_binding=binding2, now=NOW)
    second_row = tmp_store._conn.execute(
        "SELECT state, consumed_at, external_approval_id FROM approval_challenges "
        "WHERE challenge_id=?",
        (second["challenge_id"],),
    ).fetchone()
    assert tuple(second_row) == ("pending", None, None)
    assert tmp_store._conn.execute(
        "SELECT count(*) FROM approval_audit WHERE event_type='CONSUME'"
    ).fetchone()[0] == 1


@pytest.mark.parametrize("invalid_round", [True, 1.0])
def test_consume_revalidates_expected_binding_before_database_write(
    tmp_store: SessionStore, invalid_round: Any
) -> None:
    service = _service(tmp_store)
    binding, value = _artifact(service)
    invalid_expected = ApprovalBinding.model_construct(
        **{**binding.model_dump(), "round": invalid_round}
    )
    with pytest.raises(ApprovalBlocked) as caught:
        service.consume(_raw(value), expected_binding=invalid_expected, now=NOW)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    row = tmp_store._conn.execute(
        "SELECT state, consumed_at, external_approval_id FROM approval_challenges"
    ).fetchone()
    assert tuple(row) == ("pending", None, None)
    assert tmp_store._conn.execute(
        "SELECT count(*) FROM approval_audit WHERE event_type='CONSUME'"
    ).fetchone()[0] == 0


def test_concurrent_consume_across_store_instances_has_one_winner(tmp_path: Any) -> None:
    db = str(tmp_path / "shared.db")
    first_store = SessionStore(db, str(tmp_path / "first.jsonl"))
    second_store = SessionStore(db, str(tmp_path / "second.jsonl"))
    first = _service(first_store)
    second = _service(second_store)
    binding, value = _artifact(first)

    def consume(service: ApprovalGateService) -> ApprovalResult:
        try:
            return service.consume(_raw(value), expected_binding=binding, now=NOW).result
        except ApprovalBlocked as exc:
            return exc.result

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(consume, (first, second)))
        assert results.count(ApprovalResult.APPROVAL_VALID) == 1
        assert results.count(ApprovalResult.BLOCKED) == 1
        assert first_store._conn.execute(
            "SELECT count(*) FROM approval_audit WHERE event_type='CONSUME'"
        ).fetchone()[0] == 1
        row = first_store._conn.execute(
            "SELECT state, consumed_at, external_approval_id FROM approval_challenges"
        ).fetchone()
        assert row["state"] == "consumed"
        assert row["consumed_at"] is not None
        assert row["external_approval_id"] == "provider-event-id"
        counts = dict(first_store._conn.execute(
            "SELECT event_type, count(*) FROM approval_audit GROUP BY event_type"
        ).fetchall())
        assert counts == {"CONSUME": 1, "ISSUE": 1}
    finally:
        first_store.close()
        second_store.close()


def test_busy_database_fails_closed(tmp_path: Any) -> None:
    db = str(tmp_path / "busy.db")
    store = SessionStore(db, str(tmp_path / "events.jsonl"))
    service = _service(store)
    binding, value = _artifact(service)
    lock = sqlite3.connect(db, isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    original = store._approval_connection

    def short_timeout() -> sqlite3.Connection:
        conn = sqlite3.connect(db, isolation_level=None, timeout=0.01)
        conn.execute("PRAGMA busy_timeout=10")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    store._approval_connection = short_timeout  # type: ignore[method-assign]
    try:
        with pytest.raises(ApprovalBlocked):
            service.consume(_raw(value), expected_binding=binding, now=NOW)
    finally:
        store._approval_connection = original  # type: ignore[method-assign]
        lock.execute("ROLLBACK")
        lock.close()
        store.close()


def test_rollback_failure_never_returns_approval_valid(tmp_path: Any) -> None:
    db = str(tmp_path / "rollback-failure.db")
    store = SessionStore(db, str(tmp_path / "events.jsonl"))
    service = _service(store)
    binding, value = _artifact(service)
    original = store._approval_connection

    class FaultConnection:
        def __init__(self, wrapped: sqlite3.Connection) -> None:
            self._wrapped = wrapped

        def execute(self, sql: str, parameters: Any = ()) -> Any:
            normalized = " ".join(sql.split())
            if normalized.startswith("INSERT INTO approval_audit"):
                raise sqlite3.OperationalError("injected audit failure")
            if normalized == "ROLLBACK":
                raise sqlite3.OperationalError("injected rollback failure")
            return self._wrapped.execute(sql, parameters)

        def close(self) -> None:
            self._wrapped.close()

    def faulty_connection() -> sqlite3.Connection:
        return FaultConnection(original())  # type: ignore[return-value]

    store._approval_connection = faulty_connection  # type: ignore[method-assign]
    try:
        with pytest.raises(ApprovalBlocked):
            service.consume(_raw(value), expected_binding=binding, now=NOW)
        row = store._conn.execute(
            "SELECT state, consumed_at, external_approval_id FROM approval_challenges"
        ).fetchone()
        assert tuple(row) == ("pending", None, None)
        assert store._conn.execute(
            "SELECT count(*) FROM approval_audit WHERE event_type='CONSUME'"
        ).fetchone()[0] == 0
    finally:
        store._approval_connection = original  # type: ignore[method-assign]
        store.close()


def test_bool_round_rejected_before_issue() -> None:
    with pytest.raises(ValidationError):
        _binding(round=True)
