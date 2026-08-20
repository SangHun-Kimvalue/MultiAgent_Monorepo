"""Deterministic selection gate for provider-neutral AI coding video candidates."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit


DETAIL_FIELDS = (
    "creator_claim_ko",
    "confirmed_fact_ko",
    "task_ko",
    "environment_ko",
    "failure_conditions_ko",
    "small_experiment_ko",
)
SCORE_FIELDS = (
    "task_specificity",
    "environment_transparency",
    "reproducibility",
    "project_relevance",
)
CANDIDATE_KEYS = {
    "id",
    "url",
    "title",
    "channel",
    "published_date",
    "evidence_grade",
    "content_source",
    *DETAIL_FIELDS,
    "cross_checks",
    "adoption_decision",
    "scores",
    "disqualifiers",
}
RESULT_ITEM_KEYS = {
    "id",
    "url",
    "title",
    "channel",
    "published_date",
    "evidence_grade",
    *DETAIL_FIELDS,
    "cross_checks",
    "adoption_decision",
    "score_total",
}
EVIDENCE_REQUIRED_KEYS = {"kind", "reason_code"}
EVIDENCE_OPTIONAL_KEYS = {"id", "url", "detail_ko"}
COLLECTION_KEYS = {"pages_opened", "elapsed_seconds", "complete", "stop_reason"}
DISQUALIFIERS = {
    "UNDISCLOSED_SPONSORSHIP_SUSPECTED",
    "TITLE_CONTENT_MISMATCH",
    "BODY_ACCESS_FAILED",
}
CROSS_CHECK_KINDS = {"official_doc", "release_note", "public_repository"}
CONTENT_SOURCES = {"description", "transcript", "video_body"}
ADOPTION_DECISIONS = {"ADOPT", "PILOT", "WATCH", "REJECT"}
EVIDENCE_KINDS = {"input_fallback", "machine_exclusion", "history_exclusion", "collection", "finalization"}
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com"}
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
YOUTU_BE_PATH = re.compile(r"^/[A-Za-z0-9_-]{11}$")
YOUTUBE_SHORTS_PATH = re.compile(r"^/shorts/[A-Za-z0-9_-]{11}$")
HISTORY_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})\.video\.json$")
DNS_HOST = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*$")
URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


class BlockedError(ValueError):
    """Expected input, history, or publication problem."""


@dataclass(frozen=True)
class Candidate:
    raw: dict[str, Any]
    canonical_url: str

    @property
    def identifier(self) -> str:
        return self.raw["id"]

    @property
    def published_date(self) -> str:
        return self.raw["published_date"]

    @property
    def total_score(self) -> int:
        freshness = (date.fromisoformat(self.raw["_date"]) - date.fromisoformat(self.published_date)).days
        return (2 if freshness <= 7 else 1) + sum(self.raw["scores"].values())


def compact_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def emit(action: str, reason_code: str, *, result_path: Path | None = None, selected_count: int | None = None, cleanup_path: Path | None = None) -> None:
    print(
        compact_json(
            {
                "action": action,
                "reason_code": reason_code,
                "result_path": str(result_path) if result_path is not None else None,
                "selected_count": selected_count,
                "cleanup_path": str(cleanup_path) if cleanup_path is not None else None,
            }
        )
    )


def read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8-sig") as source:
            return json.load(source)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BlockedError(f"INPUT_UNREADABLE:{type(exc).__name__}") from exc


def parse_date(value: Any, reason: str) -> str:
    if not isinstance(value, str):
        raise BlockedError(reason)
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise BlockedError(reason) from exc
    if parsed.isoformat() != value:
        raise BlockedError(reason)
    return value


def nonempty_string(value: Any, reason: str) -> str:
    if not isinstance(value, str) or not value:
        raise BlockedError(reason)
    return value


def canonicalize_video_url(value: Any) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or any(character.isspace() or ord(character) == 127 for character in value)
        or "\\" in value
        or INVALID_PERCENT_ESCAPE.search(value)
    ):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password or port not in {None, 443}:
        return None
    if host not in YOUTUBE_HOSTS | {"youtu.be"}:
        return None
    try:
        query_pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    query: dict[str, str] = {}
    for key, query_value in query_pairs:
        if key not in {"v", "t", "si", "feature"} or key in query:
            return None
        query[key] = query_value

    video_id: str | None = None
    if host == "youtu.be":
        if YOUTU_BE_PATH.fullmatch(parsed.path) is None or "v" in query:
            return None
        video_id = parsed.path[1:]
    elif parsed.path == "/watch":
        if "v" not in query:
            return None
        video_id = query["v"]
    else:
        if YOUTUBE_SHORTS_PATH.fullmatch(parsed.path) is None or "v" in query:
            return None
        video_id = parsed.path.removeprefix("/shorts/")
    if video_id is None or VIDEO_ID.fullmatch(video_id) is None:
        return None
    return f"https://www.youtube.com/watch?v={video_id}"


def validate_cross_checks(value: Any, grade: str) -> None:
    if not isinstance(value, list) or (grade == "X" and not value):
        raise BlockedError("CROSS_CHECKS_INVALID")
    for row in value:
        if not isinstance(row, dict) or set(row) != {"kind", "url"}:
            raise BlockedError("CROSS_CHECKS_INVALID")
        if not isinstance(row["kind"], str) or row["kind"] not in CROSS_CHECK_KINDS or not isinstance(row["url"], str):
            raise BlockedError("CROSS_CHECKS_INVALID")
        if not is_valid_cross_check_url(row["url"]):
            raise BlockedError("CROSS_CHECKS_INVALID")


def is_valid_cross_check_url(value: str) -> bool:
    if not value.startswith("https://"):
        return False
    if any(ord(character) < 32 or ord(character) == 127 for character in value) or " " in value or "\\" in value:
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and DNS_HOST.fullmatch(parsed.hostname) is not None
        and "[" not in parsed.netloc
        and "]" not in parsed.netloc
        and parsed.username is None
        and parsed.password is None
        and port is None
        and parsed.path not in {"", "/"}
    )


def is_valid_uri(value: Any) -> bool:
    if not isinstance(value, str) or not value or URI_SCHEME.match(value) is None:
        return False
    if any(ord(character) <= 32 or ord(character) >= 127 for character in value):
        return False
    if any(character in '<>"{}|\\^`' for character in value) or INVALID_PERCENT_ESCAPE.search(value):
        return False
    try:
        return bool(urlsplit(value).scheme)
    except ValueError:
        return False


def validate_collection(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != COLLECTION_KEYS:
        raise BlockedError("COLLECTION_INVALID")
    pages = value["pages_opened"]
    elapsed = value["elapsed_seconds"]
    complete = value["complete"]
    stop_reason = value["stop_reason"]
    if type(pages) is not int or not 0 <= pages <= 12 or type(elapsed) is not int or not 0 <= elapsed <= 480:
        raise BlockedError("COLLECTION_BUDGET_INCONSISTENT")
    if (
        type(complete) is not bool
        or not isinstance(stop_reason, str)
        or stop_reason not in {"COMPLETE", "PAGE_BUDGET", "TIME_BUDGET", "ACCESS_FAILURE", "TRANSPORT_FAILURE"}
    ):
        raise BlockedError("COLLECTION_INVALID")
    if complete != (stop_reason == "COMPLETE"):
        raise BlockedError("COLLECTION_BUDGET_INCONSISTENT")
    if stop_reason == "PAGE_BUDGET" and (complete or pages != 12):
        raise BlockedError("COLLECTION_BUDGET_INCONSISTENT")
    if stop_reason == "TIME_BUDGET" and (complete or elapsed != 480):
        raise BlockedError("COLLECTION_BUDGET_INCONSISTENT")
    if not complete:
        if pages == 12 and elapsed < 480 and stop_reason != "PAGE_BUDGET":
            raise BlockedError("COLLECTION_BUDGET_INCONSISTENT")
        if elapsed == 480 and pages < 12 and stop_reason != "TIME_BUDGET":
            raise BlockedError("COLLECTION_BUDGET_INCONSISTENT")
        if pages == 12 and elapsed == 480 and stop_reason not in {"PAGE_BUDGET", "TIME_BUDGET"}:
            raise BlockedError("COLLECTION_BUDGET_INCONSISTENT")
    return value


def parse_candidate(payload: Any, requested_date: str) -> tuple[dict[str, Any], list[Candidate]]:
    if not isinstance(payload, dict) or set(payload) != {"date", "collection", "items"}:
        raise BlockedError("CANDIDATE_ROOT_INVALID")
    candidate_date = parse_date(payload["date"], "CANDIDATE_DATE_INVALID")
    if candidate_date != requested_date:
        raise BlockedError("BRIEFING_DATE_MISMATCH")
    collection = validate_collection(payload["collection"])
    if not isinstance(payload["items"], list):
        raise BlockedError("CANDIDATE_ITEMS_INVALID")

    seen_ids: set[str] = set()
    candidates: list[Candidate] = []
    for raw_value in payload["items"]:
        if not isinstance(raw_value, dict) or set(raw_value) != CANDIDATE_KEYS:
            raise BlockedError("CANDIDATE_ITEM_INVALID")
        raw = dict(raw_value)
        identifier = nonempty_string(raw["id"], "CANDIDATE_ID_INVALID")
        if identifier in seen_ids:
            raise BlockedError("CANDIDATE_ID_DUPLICATE")
        seen_ids.add(identifier)
        for field in ("url", "title", "channel", "content_source"):
            nonempty_string(raw[field], "CANDIDATE_ITEM_INVALID")
        parse_date(raw["published_date"], "PUBLISHED_DATE_INVALID")
        grade = raw["evidence_grade"]
        if not isinstance(grade, str) or grade not in {"M1", "M2", "X"}:
            raise BlockedError("EVIDENCE_GRADE_INVALID")
        if not isinstance(raw["adoption_decision"], str) or raw["adoption_decision"] not in ADOPTION_DECISIONS:
            raise BlockedError("ADOPTION_DECISION_INVALID")
        if (
            not isinstance(raw["disqualifiers"], list)
            or any(not isinstance(value, str) or value not in DISQUALIFIERS for value in raw["disqualifiers"])
            or len(set(raw["disqualifiers"])) != len(raw["disqualifiers"])
        ):
            raise BlockedError("DISQUALIFIERS_INVALID")
        body_access_failed = "BODY_ACCESS_FAILED" in raw["disqualifiers"]
        if grade == "M1":
            if raw["content_source"] != "metadata" or raw["cross_checks"] != [] or any(raw[field] is not None for field in DETAIL_FIELDS):
                raise BlockedError("M1_EVIDENCE_INVALID")
        else:
            if raw["content_source"] not in CONTENT_SOURCES:
                raise BlockedError("CONTENT_SOURCE_INVALID")
            validate_cross_checks(raw["cross_checks"], grade)
            for field in DETAIL_FIELDS:
                if raw[field] is None and body_access_failed:
                    continue
                nonempty_string(raw[field], "DETAIL_FIELDS_INVALID")
        scores = raw["scores"]
        if not isinstance(scores, dict) or set(scores) != set(SCORE_FIELDS):
            raise BlockedError("SCORES_INVALID")
        if any(type(score) is not int or not 0 <= score <= 2 for score in scores.values()):
            raise BlockedError("SCORES_INVALID")
        raw["_date"] = candidate_date
        canonical_url = canonicalize_video_url(raw["url"])
        candidates.append(Candidate(raw, canonical_url or ""))
    return collection, candidates


def candidate_projection(candidate: Candidate) -> str:
    projection = {key: value for key, value in candidate.raw.items() if key not in {"id", "url", "_date"}}
    projection["url"] = candidate.canonical_url
    return compact_json(projection)


def deduplicate(candidates: list[Candidate]) -> tuple[list[Candidate], list[dict[str, Any]]]:
    valid: list[Candidate] = []
    evidence: list[dict[str, Any]] = []
    grouped: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        if not candidate.canonical_url:
            continue
        else:
            grouped.setdefault(candidate.canonical_url, []).append(candidate)
    for candidate in sorted(
        (item for item in candidates if not item.canonical_url),
        key=lambda item: (item.identifier, candidate_projection(item)),
    ):
        evidence.append(evidence_item("machine_exclusion", "URL_INVALID", candidate))
    for canonical_url in sorted(grouped):
        group = sorted(grouped[canonical_url], key=lambda item: item.identifier)
        if len({candidate_projection(item) for item in group}) != 1:
            raise BlockedError("CANDIDATE_VIDEO_CONFLICT")
        valid.append(group[0])
        for duplicate in group[1:]:
            evidence.append(evidence_item("machine_exclusion", "DUPLICATE_CANONICAL_URL", duplicate))
    return valid, evidence


def evidence_item(kind: str, reason_code: str, candidate: Candidate | None = None, *, url: str | None = None, detail: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"kind": kind, "reason_code": reason_code}
    if candidate is not None:
        result["id"] = candidate.identifier
        if candidate.canonical_url:
            result["url"] = candidate.canonical_url
    elif url is not None:
        result["url"] = url
    if detail is not None:
        result["detail_ko"] = detail
    return result


def load_history(history_root: Path, requested_date: str) -> set[str]:
    try:
        history_root.lstat()
    except FileNotFoundError:
        return set()
    except OSError as exc:
        raise BlockedError("HISTORY_UNREADABLE") from exc
    try:
        history_mode = history_root.stat().st_mode
    except OSError as exc:
        raise BlockedError("HISTORY_UNREADABLE") from exc
    if not stat.S_ISDIR(history_mode):
        raise BlockedError("HISTORY_UNREADABLE")
    try:
        paths = list(history_root.glob("*.video.json"))
    except OSError as exc:
        raise BlockedError("HISTORY_UNREADABLE") from exc
    today = date.fromisoformat(requested_date)
    allowed_dates = {(today - timedelta(days=offset)).isoformat() for offset in range(1, 8)}
    history_urls: set[str] = set()
    for path in sorted(paths, key=lambda item: item.name):
        match = HISTORY_NAME.fullmatch(path.name)
        if match is None or match.group(1) not in allowed_dates:
            continue
        try:
            with path.open("r", encoding="utf-8-sig") as source:
                payload = json.load(source)
        except OSError as exc:
            raise BlockedError("HISTORY_UNREADABLE") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BlockedError("HISTORY_MALFORMED") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise BlockedError("HISTORY_MALFORMED")
        for item in payload["items"]:
            if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                raise BlockedError("HISTORY_MALFORMED")
            canonical_url = canonicalize_video_url(item["url"])
            if canonical_url is None:
                raise BlockedError("HISTORY_MALFORMED")
            history_urls.add(canonical_url)
    return history_urls


def classify_candidates(candidates: list[Candidate], history_urls: set[str], requested_date: str) -> tuple[list[Candidate], list[dict[str, Any]], bool]:
    selected_pool: list[Candidate] = []
    evidence: list[dict[str, Any]] = []
    partial_evidence = False
    current_date = date.fromisoformat(requested_date)
    for candidate in sorted(candidates, key=lambda item: item.identifier):
        age_days = (current_date - date.fromisoformat(candidate.published_date)).days
        if age_days < 0 or age_days > 14:
            evidence.append(evidence_item("machine_exclusion", "DATE_OUT_OF_RANGE", candidate))
            continue
        if candidate.canonical_url in history_urls:
            evidence.append(evidence_item("history_exclusion", "HISTORY_DUPLICATE", candidate))
            continue
        if candidate.raw["evidence_grade"] == "M1":
            evidence.append(evidence_item("machine_exclusion", "EVIDENCE_M1", candidate))
            partial_evidence = True
            continue
        if "BODY_ACCESS_FAILED" in candidate.raw["disqualifiers"]:
            evidence.append(evidence_item("machine_exclusion", "BODY_ACCESS_FAILED", candidate))
            partial_evidence = True
            continue
        if candidate.raw["disqualifiers"]:
            evidence.append(evidence_item("machine_exclusion", candidate.raw["disqualifiers"][0], candidate))
            continue
        if candidate.total_score < 7:
            evidence.append(evidence_item("machine_exclusion", "SCORE_BELOW_THRESHOLD", candidate))
            continue
        selected_pool.append(candidate)
    return selected_pool, evidence, partial_evidence


def parse_news_count(value: str | None) -> tuple[int, bool]:
    if not isinstance(value, str) or not value or not value.isascii() or not value.isdigit():
        return 1, True
    try:
        parsed = int(value)
    except ValueError:
        return 1, True
    return (3 if parsed < 6 else 1), False


def output_item(candidate: Candidate) -> dict[str, Any]:
    raw = candidate.raw
    return {
        "id": raw["id"],
        "url": candidate.canonical_url,
        "title": raw["title"],
        "channel": raw["channel"],
        "published_date": raw["published_date"],
        "evidence_grade": raw["evidence_grade"],
        "creator_claim_ko": raw["creator_claim_ko"],
        "confirmed_fact_ko": raw["confirmed_fact_ko"],
        "task_ko": raw["task_ko"],
        "environment_ko": raw["environment_ko"],
        "cross_checks": raw["cross_checks"],
        "failure_conditions_ko": raw["failure_conditions_ko"],
        "adoption_decision": raw["adoption_decision"],
        "small_experiment_ko": raw["small_experiment_ko"],
        "score_total": candidate.total_score,
    }


def validate_result(result: dict[str, Any]) -> None:
    if not isinstance(result, dict) or set(result) != {"video_status", "selected_count", "items", "evidence"}:
        raise BlockedError("RESULT_INVALID")
    count = result["selected_count"]
    items = result["items"]
    evidence = result["evidence"]
    if (
        type(count) is not int
        or not isinstance(items, list)
        or not isinstance(evidence, list)
        or not 0 <= count <= 3
        or count != len(items)
    ):
        raise BlockedError("RESULT_INVALID")
    status = result["video_status"]
    if (
        not isinstance(status, str)
        or status not in {"READY", "PARTIAL", "EMPTY"}
        or (status == "READY" and count < 1)
        or (status == "EMPTY" and count != 0)
    ):
        raise BlockedError("RESULT_INVALID")
    for item in items:
        if (
            not isinstance(item, dict)
            or set(item) != RESULT_ITEM_KEYS
            or any(not isinstance(item[field], str) or not item[field] for field in ("id", "title", "channel", *DETAIL_FIELDS))
            or not isinstance(item["evidence_grade"], str)
            or item["evidence_grade"] not in {"M2", "X"}
            or not isinstance(item["url"], str)
            or not item["url"]
            or canonicalize_video_url(item["url"]) != item["url"]
            or not isinstance(item["adoption_decision"], str)
            or item["adoption_decision"] not in ADOPTION_DECISIONS
            or type(item["score_total"]) is not int
            or not 0 <= item["score_total"] <= 10
        ):
            raise BlockedError("RESULT_INVALID")
        try:
            parse_date(item["published_date"], "RESULT_INVALID")
            validate_cross_checks(item["cross_checks"], item["evidence_grade"])
        except BlockedError as exc:
            raise BlockedError("RESULT_INVALID") from exc
    for row in evidence:
        if (
            not isinstance(row, dict)
            or not EVIDENCE_REQUIRED_KEYS <= set(row) <= EVIDENCE_REQUIRED_KEYS | EVIDENCE_OPTIONAL_KEYS
            or not isinstance(row["kind"], str)
            or row["kind"] not in EVIDENCE_KINDS
            or not isinstance(row["reason_code"], str)
            or not row["reason_code"]
            or any(key in row and (not isinstance(row[key], str) or not row[key]) for key in ("id", "detail_ko"))
            or (
                "url" in row
                and (
                    not isinstance(row["url"], str)
                    or not row["url"]
                    or canonicalize_video_url(row["url"]) != row["url"]
                )
            )
        ):
            raise BlockedError("RESULT_INVALID")


def publish(destination: Path, result: dict[str, Any], action: str, reason_code: str) -> int:
    temp_path: Path | None = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            emit("BLOCKED", "OUTPUT_ALREADY_EXISTS")
            return 2
        descriptor, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
        temp_path = Path(temp_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
            target.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            target.flush()
            os.fsync(target.fileno())
        try:
            os.link(temp_path, destination)
        except FileExistsError:
            temp_path.unlink(missing_ok=True)
            emit("BLOCKED", "OUTPUT_ALREADY_EXISTS")
            return 2
        except OSError:
            temp_path.unlink(missing_ok=True)
            emit("BLOCKED", "OUTPUT_PUBLISH_FAILED")
            return 2
        try:
            temp_path.unlink()
        except OSError:
            emit(action, "FINALIZED_TEMP_CLEANUP_PENDING", result_path=destination, selected_count=result["selected_count"], cleanup_path=temp_path)
            return 0
        emit(action, reason_code, result_path=destination, selected_count=result["selected_count"])
        return 0
    except OSError:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        emit("BLOCKED", "OUTPUT_PUBLISH_FAILED")
        return 2


def ensure_output_available(destination: Path) -> None:
    try:
        destination.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise BlockedError("OUTPUT_PUBLISH_FAILED") from exc
    raise BlockedError("OUTPUT_ALREADY_EXISTS")


def select(args: argparse.Namespace) -> int:
    try:
        requested_date = parse_date(args.date, "REQUEST_DATE_INVALID")
        if args.output.name != f"{requested_date}.video.json":
            raise BlockedError("OUTPUT_BASENAME_INVALID")
        collection, parsed_candidates = parse_candidate(read_json(args.candidate), requested_date)
        canonical_candidates, duplicate_evidence = deduplicate(parsed_candidates)
        ensure_output_available(args.output)
        history_urls = load_history(args.history_root, requested_date)
        pool, exclusions, partial_evidence = classify_candidates(canonical_candidates, history_urls, requested_date)
    except BlockedError as exc:
        emit("BLOCKED", str(exc))
        return 2

    budget, fallback = parse_news_count(args.news_final_count)
    selected = sorted(
        pool,
        key=lambda item: (-item.total_score, -date.fromisoformat(item.published_date).toordinal(), item.canonical_url),
    )[:budget]
    evidence: list[dict[str, Any]] = []
    if fallback:
        evidence.append({"kind": "input_fallback", "reason_code": "NEWS_FINAL_COUNT_INVALID"})
    evidence.extend(duplicate_evidence)
    evidence.extend(exclusions)
    if not collection["complete"]:
        evidence.append({"kind": "collection", "reason_code": collection["stop_reason"]})
    partial = not collection["complete"] or partial_evidence
    status = "PARTIAL" if partial else "READY" if selected else "EMPTY"
    result = {
        "video_status": status,
        "selected_count": len(selected),
        "items": [output_item(candidate) for candidate in selected],
        "evidence": evidence + [{"kind": "finalization", "reason_code": f"FINALIZED_{status}"}],
    }
    try:
        validate_result(result)
    except BlockedError as exc:
        emit("BLOCKED", str(exc))
        return 2
    return publish(args.output, result, f"FINALIZE_{status}", f"FINALIZED_{status}")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Select AI coding videos deterministically.")
    commands = root.add_subparsers(dest="command", required=True)
    select_parser = commands.add_parser("select")
    select_parser.add_argument("--candidate", type=Path, required=True)
    select_parser.add_argument("--date", required=True)
    select_parser.add_argument("--news-final-count")
    select_parser.add_argument("--history-root", type=Path, required=True)
    select_parser.add_argument("--output", type=Path, required=True)
    select_parser.set_defaults(handler=select)
    return root


def main() -> int:
    args = parser().parse_args()
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
