"""Deterministic signed approval capability verifier.

This slice does not claim human presence or trusted-clock authority. A production
adapter must supply time from an ACP-process-owned trusted clock, never caller input.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr, field_validator

from acp.store import SessionStore

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_UTC_Z_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})T(?P<time>\d{2}:\d{2}:\d{2})"
    r"(?P<fraction>\.\d{1,6})?Z$"
)


class ApprovalDecision(str, Enum):
    APPROVE_REAPPLY = "APPROVE_REAPPLY"


class ApprovalClaimLevel(str, Enum):
    DETERMINISTIC_VERIFIER = "DETERMINISTIC_VERIFIER"


class ApprovalResult(str, Enum):
    APPROVAL_VALID = "APPROVAL_VALID"
    BLOCKED = "BLOCKED"


class ApprovalBlocked(Exception):
    """Fail-closed approval rejection with a stable enum result."""

    result = ApprovalResult.BLOCKED


class ApprovalBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    phase_id: StrictStr
    round: StrictInt
    findings_digest: StrictStr
    session_id: StrictStr
    issuer_id: StrictStr
    key_id: StrictStr

    @field_validator("phase_id", "session_id", "issuer_id", "key_id")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value or value.strip() != value:
            raise ValueError("must be non-empty and trimmed")
        return value

    @field_validator("round")
    @classmethod
    def _positive_round(cls, value: int) -> int:
        if value < 1:
            raise ValueError("round must be positive")
        return value

    @field_validator("findings_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if _DIGEST_RE.fullmatch(value) is None:
            raise ValueError("findings_digest must be 64 lowercase hex characters")
        return value


class ApprovalChallenge(ApprovalBinding):
    challenge_id: StrictStr
    nonce: StrictStr
    issued_at: StrictStr
    expires_at: StrictStr


class ApprovalArtifact(ApprovalBinding):
    schema_version: StrictInt
    decision: ApprovalDecision
    challenge_id: StrictStr
    nonce: StrictStr
    external_approval_id: StrictStr
    issued_at: StrictStr
    expires_at: StrictStr
    approved_at: StrictStr
    alg: StrictStr
    signature: StrictStr

    @field_validator("schema_version")
    @classmethod
    def _v1(cls, value: int) -> int:
        if value != 1:
            raise ValueError("unsupported schema version")
        return value

    @field_validator("external_approval_id", "challenge_id")
    @classmethod
    def _artifact_non_empty(cls, value: str) -> str:
        if not value or value.strip() != value:
            raise ValueError("must be non-empty and trimmed")
        return value

    @field_validator("alg")
    @classmethod
    def _algorithm(cls, value: str) -> str:
        if value != "HMAC-SHA256":
            raise ValueError("unsupported algorithm")
        return value


class ApprovalReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    result: ApprovalResult
    claim_level: ApprovalClaimLevel
    challenge_id: StrictStr
    external_approval_id: StrictStr


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _strict_json(raw: bytes) -> dict[str, Any]:
    def decode() -> str | None:
        try:
            return raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return None

    text = decode()
    if text is None:
        raise ApprovalBlocked("invalid UTF-8")

    duplicate = False

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        nonlocal duplicate
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                duplicate = True
            result[key] = value
        return result

    def load() -> Any | None:
        try:
            return json.loads(text, object_pairs_hook=reject_duplicates)
        except json.JSONDecodeError:
            return None

    value = load()
    if value is None or duplicate:
        raise ApprovalBlocked("invalid JSON")
    if not isinstance(value, dict):
        raise ApprovalBlocked("artifact must be an object")
    return value


def _validated_binding(value: object) -> ApprovalBinding | None:
    try:
        plain = {field: getattr(value, field) for field in ApprovalBinding.model_fields}
        return ApprovalBinding.model_validate(plain, strict=True)
    except Exception:
        return None


def _validated_artifact(value: dict[str, Any]) -> ApprovalArtifact | None:
    try:
        return ApprovalArtifact.model_validate(value, strict=True)
    except Exception:
        return None


def _parse_utc_z(value: str) -> datetime:
    if _UTC_Z_RE.fullmatch(value) is None:
        raise ApprovalBlocked("timestamp must be RFC3339 UTC Z")
    def parse() -> datetime | None:
        try:
            return datetime.fromisoformat(value[:-1] + "+00:00")
        except ValueError:
            return None

    parsed = parse()
    if parsed is None:
        raise ApprovalBlocked("invalid timestamp")
    return parsed


def _format_utc_z(value: datetime) -> str:
    if value.tzinfo is None:
        raise ApprovalBlocked("now must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _decode_base64url(value: str) -> bytes:
    if not value or "=" in value or _B64URL_RE.fullmatch(value) is None:
        raise ApprovalBlocked("invalid base64url")
    def decode() -> bytes | None:
        try:
            return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        except (binascii.Error, ValueError):
            return None

    decoded = decode()
    if decoded is None:
        raise ApprovalBlocked("invalid base64url")
    if base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != value:
        raise ApprovalBlocked("non-canonical base64url")
    return decoded


class HmacSha256Verifier:
    """Verifier backed only by an explicit immutable issuer/key allowlist."""

    def __init__(
        self,
        secrets_by_key: Mapping[tuple[str, str], bytes],
    ) -> None:
        if not isinstance(secrets_by_key, Mapping):
            raise TypeError("secrets_by_key must be an explicit mapping")
        copied: dict[tuple[str, str], bytes] = {}
        for pair, secret in secrets_by_key.items():
            if (
                not isinstance(pair, tuple)
                or len(pair) != 2
                or not all(isinstance(item, str) and item for item in pair)
                or not isinstance(secret, bytes)
                or not secret
            ):
                raise TypeError("invalid issuer/key allowlist entry")
            copied[pair] = secret
        self._secrets_by_key = MappingProxyType(copied)

    def _resolve(self, issuer_id: str, key_id: str) -> bytes | None:
        return self._secrets_by_key.get((issuer_id, key_id))

    def verify(self, artifact: Mapping[str, Any], signature: bytes) -> bool:
        secret = self._resolve(str(artifact["issuer_id"]), str(artifact["key_id"]))
        if not isinstance(secret, bytes) or not secret:
            return False
        payload = {key: value for key, value in artifact.items() if key != "signature"}
        expected = hmac.new(secret, _canonical_json(payload), hashlib.sha256).digest()
        return hmac.compare_digest(expected, signature)


class ApprovalGateService:
    """Capability issuer/verifier; human presence and trusted time are NOT CLAIMED."""

    def __init__(self, store: SessionStore, verifier: HmacSha256Verifier) -> None:
        self._store = store
        self._verifier = verifier

    def issue_challenge(
        self, binding: ApprovalBinding, *, ttl_seconds: int, now: datetime
    ) -> ApprovalChallenge:
        validated_binding = _validated_binding(binding)
        if validated_binding is None:
            raise ApprovalBlocked("binding schema rejected")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
            raise ApprovalBlocked("TTL must be an exact integer")
        if not 30 <= ttl_seconds <= 900:
            raise ApprovalBlocked("TTL outside allowed range")
        issued_at = _format_utc_z(now)
        expires_at = _format_utc_z(now + timedelta(seconds=ttl_seconds))
        nonce_bytes = secrets.token_bytes(32)
        nonce = base64.urlsafe_b64encode(nonce_bytes).rstrip(b"=").decode("ascii")
        challenge_id = str(uuid.uuid4())
        values: dict[str, Any] = {
            **validated_binding.model_dump(mode="json"),
            "challenge_id": challenge_id,
            "nonce_hash": hashlib.sha256(nonce_bytes).hexdigest(),
            "issued_at": issued_at,
            "expires_at": expires_at,
        }
        try:
            self._store.insert_approval_challenge(values)
        except Exception:
            raise ApprovalBlocked("challenge persistence failed") from None
        return ApprovalChallenge(
            **validated_binding.model_dump(), challenge_id=challenge_id, nonce=nonce,
            issued_at=issued_at, expires_at=expires_at
        )

    def consume(
        self, raw_artifact: bytes, *, expected_binding: ApprovalBinding, now: datetime
    ) -> ApprovalReceipt:
        validated_expected_binding = _validated_binding(expected_binding)
        if validated_expected_binding is None:
            raise ApprovalBlocked("expected binding schema rejected")
        raw = _strict_json(raw_artifact)
        validated_raw = dict(raw)
        if validated_raw.get("decision") == ApprovalDecision.APPROVE_REAPPLY.value:
            validated_raw["decision"] = ApprovalDecision.APPROVE_REAPPLY
        artifact = _validated_artifact(validated_raw)
        if artifact is None:
            raise ApprovalBlocked("artifact schema rejected")
        signature = _decode_base64url(artifact.signature)
        nonce_bytes = _decode_base64url(artifact.nonce)
        if len(nonce_bytes) != 32 or len(signature) != hashlib.sha256().digest_size:
            raise ApprovalBlocked("invalid nonce or signature length")
        if not self._verifier.verify(raw, signature):
            raise ApprovalBlocked("signature rejected")
        actual_binding = ApprovalBinding.model_validate(
            artifact.model_dump(include=set(ApprovalBinding.model_fields)), strict=True
        )
        if actual_binding != validated_expected_binding:
            raise ApprovalBlocked("binding mismatch")
        issued_at = _parse_utc_z(artifact.issued_at)
        expires_at = _parse_utc_z(artifact.expires_at)
        approved_at = _parse_utc_z(artifact.approved_at)
        if now.tzinfo is None:
            raise ApprovalBlocked("now must be timezone-aware")
        trusted_now = now.astimezone(timezone.utc)
        if expires_at < issued_at or trusted_now > expires_at:
            raise ApprovalBlocked("challenge expired")
        if approved_at < issued_at or approved_at > expires_at:
            raise ApprovalBlocked("approval outside challenge lifetime")
        if abs((approved_at - trusted_now).total_seconds()) > 30:
            raise ApprovalBlocked("approved_at clock skew")
        values: dict[str, Any] = {
            **actual_binding.model_dump(mode="json"),
            "challenge_id": artifact.challenge_id,
            "nonce_hash": hashlib.sha256(nonce_bytes).hexdigest(),
            "issued_at": artifact.issued_at,
            "expires_at": artifact.expires_at,
            "consumed_at": _format_utc_z(trusted_now),
            "external_approval_id": artifact.external_approval_id,
        }
        if not self._store.consume_approval_challenge(values):
            raise ApprovalBlocked("challenge consume rejected")
        return ApprovalReceipt(
            result=ApprovalResult.APPROVAL_VALID,
            claim_level=ApprovalClaimLevel.DETERMINISTIC_VERIFIER,
            challenge_id=artifact.challenge_id,
            external_approval_id=artifact.external_approval_id,
        )
