#!/usr/bin/env python3
"""Deterministic prepare/reconcile gate for daily AI news candidates."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


CONFIDENCE_VALUES = {"official", "trusted_secondary"}
AUDIT_VERDICTS = {"PASS", "REVISE"}
FAILURE_KINDS = {"timeout", "auth", "transport"}
HUB_PATHS = {"/news", "/blog", "/research", "/updates", "/announcements"}


class BlockedError(ValueError):
    """The caller supplied a candidate or invocation that cannot be processed."""


@dataclass(frozen=True)
class CandidateItem:
    identifier: str
    claim: str
    url: str
    published_date: str
    event_date: str | None
    confidence: str

    @property
    def effective_date(self) -> str:
        return self.event_date or self.published_date


def emit(action: str, reason_code: str, *, result_path: Path | None = None, rejected_ids: list[str] | None = None) -> None:
    envelope = {
        "action": action,
        "reason_code": reason_code,
        "result_path": str(result_path) if result_path else None,
        "rejected_ids": rejected_ids or [],
    }
    print(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))


def read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8-sig") as source:
            return json.load(source)
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BlockedError(f"INPUT_UNREADABLE:{type(exc).__name__}") from exc


def read_audit(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        with path.open("r", encoding="utf-8-sig") as source:
            payload = json.load(source)
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, "AUDIT_INPUT_UNREADABLE"
    if not isinstance(payload, dict):
        return None, "AUDIT_ROOT_INVALID"
    return payload, None


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_date(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field}_TYPE")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field}_FORMAT") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field}_FORMAT")
    return value


def normalize_url(value: str) -> str:
    return value.strip().rstrip("/")


def url_reason(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return "URL_INVALID"
    normalized = normalize_url(value)
    try:
        parsed = urlsplit(normalized)
        host = parsed.hostname
    except ValueError:
        return "URL_INVALID"
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        return "URL_INVALID"
    if not host:
        return "URL_INVALID"
    lowered = host.lower().rstrip(".")
    if lowered == "localhost" or lowered.endswith(".localhost") or lowered.endswith(".local"):
        return "URL_PRIVATE_HOST"
    path = parsed.path.rstrip("/").lower() or "/"
    if path in HUB_PATHS:
        return "HUB_URL"
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        return "HUB_URL" if parsed.path in {"", "/"} else None
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
        or address.is_multicast
    ):
        return "URL_PRIVATE_HOST"
    return "HUB_URL" if parsed.path in {"", "/"} else None


def freshness_reason(value: str, briefing_date: str) -> str | None:
    delta = (date.fromisoformat(briefing_date) - date.fromisoformat(value)).days
    if delta < 0 or delta > 3:
        return "DATE_OUT_OF_RANGE"
    return None


def parse_candidate(payload: Any) -> tuple[str, list[CandidateItem]]:
    if not isinstance(payload, dict) or set(payload) != {"date", "items"}:
        raise BlockedError("CANDIDATE_ROOT_INVALID")
    try:
        briefing_date = parse_date(payload["date"], field="CANDIDATE_DATE")
    except ValueError as exc:
        raise BlockedError(str(exc)) from exc
    raw_items = payload["items"]
    if not isinstance(raw_items, list):
        raise BlockedError("CANDIDATE_ITEMS_INVALID")

    seen_ids: set[str] = set()
    parsed_items: list[CandidateItem] = []
    for raw in raw_items:
        if not isinstance(raw, dict) or set(raw) != {
            "id", "claim", "url", "published_date", "event_date", "confidence"
        }:
            raise BlockedError("CANDIDATE_ITEM_INVALID")
        identifier = raw["id"]
        claim = raw["claim"]
        if not isinstance(identifier, str) or not identifier:
            raise BlockedError("CANDIDATE_ID_INVALID")
        if identifier in seen_ids:
            raise BlockedError("CANDIDATE_ID_DUPLICATE")
        if not isinstance(claim, str) or not claim:
            raise BlockedError("CANDIDATE_CLAIM_INVALID")
        try:
            published_date = parse_date(raw["published_date"], field="PUBLISHED_DATE")
            event_date = raw["event_date"]
            if event_date is not None:
                event_date = parse_date(event_date, field="EVENT_DATE")
        except ValueError as exc:
            raise BlockedError(str(exc)) from exc
        if not isinstance(raw["url"], str):
            raise BlockedError("URL_INVALID")
        if not isinstance(raw["confidence"], str):
            raise BlockedError("CONFIDENCE_INVALID")
        seen_ids.add(identifier)
        parsed_items.append(
            CandidateItem(identifier, claim, raw["url"], published_date, event_date, raw["confidence"])
        )
    return briefing_date, parsed_items


def classify_items(items: list[CandidateItem], briefing_date: str) -> tuple[list[CandidateItem], list[dict[str, Any]]]:
    countable: list[CandidateItem] = []
    evidence: list[dict[str, Any]] = []
    for item in items:
        reason = url_reason(item.url)
        if reason is None and item.confidence not in CONFIDENCE_VALUES:
            reason = "CONFIDENCE_INVALID"
        if reason is None:
            reason = freshness_reason(item.effective_date, briefing_date)
        if reason is None:
            countable.append(
                CandidateItem(
                    item.identifier,
                    item.claim,
                    normalize_url(item.url),
                    item.published_date,
                    item.event_date,
                    item.confidence,
                )
            )
            continue
        evidence.append(evidence_item("machine_exclusion", reason, 0, item.identifier, item.url))
    return countable, evidence


def digest_for(briefing_date: str, items: list[CandidateItem]) -> str:
    projection = {
        "date": briefing_date,
        "items": [
            {
                "id": item.identifier,
                "claim": item.claim,
                "url": item.url,
                "published_date": item.published_date,
                "event_date": item.event_date,
                "confidence": item.confidence,
            }
            for item in sorted(items, key=lambda item: item.identifier)
        ],
    }
    canonical = json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def evidence_item(kind: str, reason_code: str, attempt: int, identifier: str | None = None, url: str | None = None, detail: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"kind": kind, "reason_code": reason_code, "attempt": attempt}
    if identifier is not None:
        item["id"] = identifier
    if url is not None:
        item["url"] = url
    if detail is not None:
        item["detail_ko"] = detail
    return item


def prepare(args: argparse.Namespace) -> int:
    try:
        briefing_date, items = parse_candidate(read_json(args.candidate))
        requested_date = parse_date(args.date, field="REQUEST_DATE")
    except (BlockedError, ValueError) as exc:
        emit("BLOCKED", str(exc))
        return 2
    if briefing_date != requested_date:
        emit("BLOCKED", "BRIEFING_DATE_MISMATCH")
        return 2

    countable, exclusions = classify_items(items, briefing_date)
    request = {
        "date": briefing_date,
        "request_digest": digest_for(briefing_date, countable),
        "candidates": [
            {
                "id": item.identifier,
                "claim": item.claim,
                "url": item.url,
                "published_date": item.published_date,
                "event_date": item.event_date,
                "confidence": item.confidence,
            }
            for item in countable
        ],
        "excluded_evidence": exclusions,
    }
    write_json(args.output, request)
    emit("AUDIT_READY", "AUDIT_REQUEST_WRITTEN", result_path=args.output)
    return 0


def degraded_result(evidence: list[dict[str, Any]], reason_code: str, attempt: int) -> dict[str, Any]:
    return {
        "status": "DEGRADED",
        "final_count": 0,
        "items": [],
        "evidence": evidence
        + [
            evidence_item("audit_failure", reason_code, attempt),
            evidence_item("finalization", "FINALIZED_DEGRADED", attempt),
        ],
    }


def parse_audit_rows(
    audit: dict[str, Any], countable: list[CandidateItem], digest: str, briefing_date: str
) -> tuple[tuple[list[tuple[CandidateItem, dict[str, Any]]], list[str]] | None, str | None]:
    if set(audit) != {"verdict", "request_digest", "supported_count", "rows", "issues_ko"}:
        return None, "AUDIT_STRUCTURE_INVALID"
    if not isinstance(audit["verdict"], str) or audit["verdict"] not in AUDIT_VERDICTS:
        return None, "AUDIT_STRUCTURE_INVALID"
    if not isinstance(audit["request_digest"], str) or audit["request_digest"] != digest:
        return None, "AUDIT_REQUEST_MISMATCH"
    if not isinstance(audit["supported_count"], int) or isinstance(audit["supported_count"], bool):
        return None, "AUDIT_STRUCTURE_INVALID"
    if not isinstance(audit["rows"], list) or not isinstance(audit["issues_ko"], list):
        return None, "AUDIT_STRUCTURE_INVALID"
    if not all(isinstance(issue, str) for issue in audit["issues_ko"]):
        return None, "AUDIT_STRUCTURE_INVALID"

    by_id = {item.identifier: item for item in countable}
    if len(audit["rows"]) != len(by_id):
        return None, "AUDIT_STRUCTURE_INVALID"
    rows_by_id: dict[str, dict[str, Any]] = {}
    for row in audit["rows"]:
        if not isinstance(row, dict) or set(row) != {
            "id", "supported", "published_date", "event_date", "source_url", "reason_ko"
        }:
            return None, "AUDIT_STRUCTURE_INVALID"
        identifier = row["id"]
        if not isinstance(identifier, str) or identifier not in by_id or identifier in rows_by_id:
            return None, "AUDIT_STRUCTURE_INVALID"
        if not isinstance(row["supported"], bool) or not isinstance(row["source_url"], str):
            return None, "AUDIT_STRUCTURE_INVALID"
        if url_reason(row["source_url"]) is not None:
            return None, "AUDIT_STRUCTURE_INVALID"
        if row["event_date"] is not None and not isinstance(row["event_date"], str):
            return None, "AUDIT_STRUCTURE_INVALID"
        if not isinstance(row["published_date"], str) or not isinstance(row["reason_ko"], str):
            return None, "AUDIT_STRUCTURE_INVALID"
        try:
            parse_date(row["published_date"], field="AUDIT_PUBLISHED_DATE")
            if row["event_date"] is not None:
                parse_date(row["event_date"], field="AUDIT_EVENT_DATE")
        except ValueError:
            return None, "AUDIT_STRUCTURE_INVALID"
        rows_by_id[identifier] = row
    if set(rows_by_id) != set(by_id):
        return None, "AUDIT_STRUCTURE_INVALID"

    paired = [(item, rows_by_id[item.identifier]) for item in countable]
    rejected = [item.identifier for item, row in paired if not row["supported"] or row_mismatch(item, row, briefing_date)]
    supported_count = len(paired) - len(rejected)
    if audit["supported_count"] != supported_count:
        return None, "AUDIT_STRUCTURE_INVALID"
    expected_verdict = "PASS" if not rejected else "REVISE"
    if audit["verdict"] != expected_verdict:
        return None, "AUDIT_STRUCTURE_INVALID"
    return (paired, rejected), None


def row_mismatch(item: CandidateItem, row: dict[str, Any], briefing_date: str) -> bool:
    if normalize_url(row["source_url"]) != item.url:
        return True
    if row["published_date"] != item.published_date or row["event_date"] != item.event_date:
        return True
    return freshness_reason(item.effective_date, briefing_date) is not None


def reconcile(args: argparse.Namespace) -> int:
    try:
        briefing_date, items = parse_candidate(read_json(args.candidate))
    except BlockedError as exc:
        emit("BLOCKED", str(exc))
        return 2

    countable, exclusions = classify_items(items, briefing_date)
    audit, audit_error = read_audit(args.audit)
    if audit_error is None and audit is not None and "failure_kind" in audit:
        if (
            set(audit) != {"failure_kind", "reason_code"}
            or not isinstance(audit["failure_kind"], str)
            or audit["failure_kind"] not in FAILURE_KINDS
            or not isinstance(audit["reason_code"], str)
        ):
            audit_error = "AUDIT_FAILURE_INVALID"
        else:
            audit_error = f"AUDIT_{audit['failure_kind'].upper()}"
    if audit_error is not None or audit is None:
        result = degraded_result(exclusions, audit_error or "AUDIT_INVALID", args.attempt)
        write_json(args.output, result)
        emit("FINALIZE_DEGRADED", audit_error or "AUDIT_INVALID", result_path=args.output)
        return 0

    parsed, parse_error = parse_audit_rows(audit, countable, digest_for(briefing_date, countable), briefing_date)
    if parse_error is not None:
        result = degraded_result(exclusions, parse_error, args.attempt)
        write_json(args.output, result)
        emit("FINALIZE_DEGRADED", parse_error, result_path=args.output)
        return 0
    assert parsed is not None
    paired, rejected = parsed
    if rejected and args.attempt < 3:
        emit("REVISE", "CLAIM_MISMATCH", rejected_ids=rejected)
        return 1

    accepted_items = [
        {
            "id": item.identifier,
            "claim": item.claim,
            "url": item.url,
            "published_date": item.published_date,
            "event_date": item.event_date,
            "confidence": item.confidence,
            "audit_reason_ko": row["reason_ko"],
        }
        for item, row in paired
        if item.identifier not in rejected
    ]
    evidence = list(exclusions)
    for item, row in paired:
        if item.identifier in rejected:
            evidence.append(evidence_item("unverified", "CLAIM_MISMATCH", args.attempt, item.identifier, item.url, row["reason_ko"]))
    final_count = len({item["url"] for item in accepted_items})
    status = "OK" if final_count >= 6 else "DEGRADED"
    evidence.append(evidence_item("finalization", f"FINALIZED_{status}", args.attempt))
    result = {"status": status, "final_count": final_count, "items": accepted_items, "evidence": evidence}
    write_json(args.output, result)
    emit("FINALIZE_OK" if status == "OK" else "FINALIZE_DEGRADED", "FINALIZED", result_path=args.output, rejected_ids=rejected)
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Prepare and reconcile daily AI news audits.")
    commands = root.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--candidate", type=Path, required=True)
    prepare_parser.add_argument("--date", required=True)
    prepare_parser.add_argument("--output", type=Path, required=True)
    prepare_parser.set_defaults(handler=prepare)
    reconcile_parser = commands.add_parser("reconcile")
    reconcile_parser.add_argument("--candidate", type=Path, required=True)
    reconcile_parser.add_argument("--audit", type=Path, required=True)
    reconcile_parser.add_argument("--attempt", type=int, choices=range(1, 4), required=True)
    reconcile_parser.add_argument("--output", type=Path, required=True)
    reconcile_parser.set_defaults(handler=reconcile)
    return root


def main() -> int:
    args = parser().parse_args()
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
