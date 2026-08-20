#!/usr/bin/env python3
"""Deterministically compose the final daily AI briefing."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any

import jsonschema


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = PLUGIN_ROOT / "schemas"
TITLE = re.compile(r"^# (\d{4}-\d{2}-\d{2}) AI Briefing   STATUS: (OK|DEGRADED)$")
COUNT_TOKEN = re.compile(r"검증 항목: (?:[0-9]+|N)건")
NUMBERED_HEADING_LIKE = re.compile(r"^\s*##\s*[0-9]+\.")
NUMBERED_HEADING = re.compile(r"^## ([0-9]+)\. (.+)$")
MARKDOWN_METACHARACTERS = frozenset(r"`*_{}[]()#+-.!|>")
STATUS_SENTENCE = {
    "READY": "선정 기준을 충족한 영상을 확인했다.",
    "PARTIAL": "후보 검토가 일부 완료되지 않아 확인된 범위만 기록한다.",
    "EMPTY": "최근 검토 창에서 선정 기준을 충족한 영상을 확인하지 못했다.",
}


class BlockedError(ValueError):
    """An expected input, composition, or publication failure."""


class EnvelopeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise BlockedError("ARGUMENT_INVALID")


def compact_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def parse_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise BlockedError("DATE_INVALID") from exc
    if parsed.isoformat() != value:
        raise BlockedError("DATE_INVALID")
    return value


def preflight_input(path: Path, reason: str) -> None:
    try:
        if not path.is_file():
            raise BlockedError(reason)
    except OSError as exc:
        raise BlockedError(reason) from exc


def _is_reparse_or_symlink(path: Path, metadata: os.stat_result) -> bool:
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and attributes & reparse_flag)


def _ensure_real_directory_tree(parent: Path) -> None:
    chain = list(reversed((parent, *parent.parents)))
    for component in chain:
        try:
            metadata = component.lstat()
        except FileNotFoundError as exc:
            raise BlockedError("OUTPUT_PARENT_MISSING") from exc
        except OSError as exc:
            raise BlockedError("OUTPUT_PARENT_UNREADABLE") from exc
        if _is_reparse_or_symlink(component, metadata):
            raise BlockedError("OUTPUT_PARENT_UNSAFE")
        if not stat.S_ISDIR(metadata.st_mode):
            raise BlockedError("OUTPUT_PARENT_INVALID")


def preflight_output(path: Path, requested_date: str) -> Path:
    if path.name != f"{requested_date}-ai-briefing.md":
        raise BlockedError("OUTPUT_BASENAME_INVALID")
    absolute = Path(os.path.abspath(path))
    _ensure_real_directory_tree(absolute.parent)
    try:
        resolved_parent = absolute.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise BlockedError("OUTPUT_PARENT_UNREADABLE") from exc
    destination = resolved_parent / absolute.name
    try:
        destination.lstat()
    except FileNotFoundError:
        return destination
    except OSError as exc:
        raise BlockedError("PUBLISH_UNAVAILABLE") from exc
    raise BlockedError("OUTPUT_EXISTS")


def read_json(path: Path, reason: str) -> Any:
    try:
        with path.open("r", encoding="utf-8-sig") as source:
            return json.load(source)
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise BlockedError(reason) from exc


def read_base(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise BlockedError("BASE_UNREADABLE") from exc


def load_schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))


def validate_schema(payload: Any, schema_name: str, reason: str) -> None:
    validator = jsonschema.Draft202012Validator(
        load_schema(schema_name), format_checker=jsonschema.FormatChecker()
    )
    try:
        validator.validate(payload)
    except jsonschema.ValidationError as exc:
        raise BlockedError(reason) from exc


def validate_news_logic(news: dict[str, Any]) -> None:
    identifiers = [item["id"] for item in news["items"]]
    urls = [item["url"] for item in news["items"]]
    count = news["final_count"]
    status = news["status"]
    if (
        len(identifiers) != len(set(identifiers))
        or len(urls) != len(set(urls))
        or count != len(set(urls))
        or (status == "OK" and count < 6)
        or (status == "DEGRADED" and count >= 6)
    ):
        raise BlockedError("NEWS_LOGIC_INVALID")


def validate_video_logic(video: dict[str, Any]) -> None:
    identifiers = [item["id"] for item in video["items"]]
    urls = [item["url"] for item in video["items"]]
    if len(identifiers) != len(set(identifiers)) or len(urls) != len(set(urls)):
        raise BlockedError("VIDEO_LOGIC_INVALID")


def _normalize_display(value: str, reason: str) -> str:
    normalized: list[str] = []
    for character in value:
        category = unicodedata.category(character)
        if (category == "Cc" and character != "\t") or category in {"Cf", "Cs", "Zl", "Zp"}:
            raise BlockedError(reason)
        normalized.append(" " if character == "\t" or category == "Zs" else character)
    collapsed = re.sub(r" +", " ", "".join(normalized)).strip(" ")
    if not collapsed:
        raise BlockedError(reason)
    return collapsed


def sanitize_markdown(value: Any) -> str:
    if not isinstance(value, str):
        raise BlockedError("VIDEO_RENDER_UNSAFE")
    normalized = _normalize_display(value, "VIDEO_RENDER_UNSAFE")
    normalized = normalized.replace("\\", "\\\\")
    return "".join(f"\\{character}" if character in MARKDOWN_METACHARACTERS else character for character in normalized)


def sanitize_summary(value: str) -> str:
    return _normalize_display(value, "BASE_SUMMARY_UNSAFE")


def safe_url(value: Any) -> str:
    if not isinstance(value, str) or "<" in value or ">" in value:
        raise BlockedError("VIDEO_RENDER_UNSAFE")
    for character in value:
        if unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}:
            raise BlockedError("VIDEO_RENDER_UNSAFE")
    return f"<{value}>"


def _line_content(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith(("\r", "\n")):
        return line[:-1], line[-1]
    return line, ""


def _replace_line_content(line: str, content: str) -> str:
    _, ending = _line_content(line)
    return content + ending


def _split_structural_lines(text: str) -> list[str]:
    lines: list[str] = []
    start = 0
    for match in re.finditer(r"\r\n|\r|\n", text):
        lines.append(text[start : match.end()])
        start = match.end()
    if start < len(text):
        lines.append(text[start:])
    return lines


def _is_structural_blank(value: str) -> bool:
    return all(character == "\t" or unicodedata.category(character) == "Zs" for character in value)


def _parse_base(base: str, requested_date: str) -> tuple[list[str], int, int, int, str]:
    if "\ufeff" in base[1:]:
        raise BlockedError("BASE_STRUCTURE_INVALID")
    lines = _split_structural_lines(base)
    if not lines:
        raise BlockedError("BASE_STRUCTURE_INVALID")
    contents = [_line_content(line)[0] for line in lines]
    comparable = list(contents)
    if comparable[0].startswith("\ufeff"):
        comparable[0] = comparable[0][1:]

    nonempty = [index for index, line in enumerate(comparable) if not _is_structural_blank(line)]
    if not nonempty or not TITLE.fullmatch(comparable[nonempty[0]]):
        raise BlockedError("BASE_STRUCTURE_INVALID")
    title_matches = [index for index, line in enumerate(comparable) if TITLE.fullmatch(line)]
    if len(title_matches) != 1:
        raise BlockedError("BASE_STRUCTURE_INVALID")
    title_index = title_matches[0]
    title_match = TITLE.fullmatch(comparable[title_index])
    if title_match is None or title_match.group(1) != requested_date:
        raise BlockedError("BASE_STRUCTURE_INVALID")

    criteria = [index for index, line in enumerate(comparable) if line.startswith("기준:")]
    if len(criteria) != 1:
        raise BlockedError("BASE_STRUCTURE_INVALID")
    criteria_index = criteria[0]
    if len(COUNT_TOKEN.findall(comparable[criteria_index])) != 1 or comparable[criteria_index].count("검증 항목:") != 1:
        raise BlockedError("BASE_STRUCTURE_INVALID")

    if "포맷: video-benchmark-v1" in base:
        raise BlockedError("BASE_ALREADY_COMPOSED")
    headings: list[tuple[int, int, str]] = []
    for index, content in enumerate(comparable):
        if not NUMBERED_HEADING_LIKE.match(content):
            continue
        match = NUMBERED_HEADING.fullmatch(content)
        if match is None:
            raise BlockedError("BASE_STRUCTURE_INVALID")
        number = int(match.group(1))
        if number == 9:
            raise BlockedError("BASE_ALREADY_COMPOSED")
        if number not in range(9):
            raise BlockedError("BASE_STRUCTURE_INVALID")
        headings.append((number, index, match.group(2)))
    if [number for number, _, _ in headings] != list(range(9)):
        raise BlockedError("BASE_STRUCTURE_INVALID")
    if headings[8][2] != "오늘의 SW 아키텍처와 설계 패턴":
        raise BlockedError("BASE_STRUCTURE_INVALID")

    section_zero_start = headings[0][1] + 1
    section_zero_end = headings[1][1]
    summary: str | None = None
    for content in comparable[section_zero_start:section_zero_end]:
        if _is_structural_blank(content):
            continue
        candidate = sanitize_summary(content)
        if candidate.startswith("#"):
            continue
        summary = candidate
        break
    if summary is None:
        raise BlockedError("BASE_SUMMARY_MISSING")
    return lines, title_index, criteria_index, headings[8][1], summary


def _render_video_section(video: dict[str, Any], newline: str) -> str:
    status = video["video_status"]
    rendered = [
        "## 8. AI 코딩 영상 벤치마킹",
        f"VIDEO: {status}",
        "포맷: video-benchmark-v1",
        STATUS_SENTENCE[status],
        "",
    ]
    for index, item in enumerate(video["items"], 1):
        cross_checks = ", ".join(
            f"{row['kind']} {safe_url(row['url'])}" for row in item["cross_checks"]
        ) or "없음"
        rendered.extend(
            [
                f"### {index}. {sanitize_markdown(item['title'])}",
                f"- 영상: {sanitize_markdown(item['channel'])} · {item['published_date']} · {safe_url(item['url'])}",
                f"- 선정 근거: 증거 등급 {item['evidence_grade']} · 점수 {item['score_total']}/10",
                f"- 환경: {sanitize_markdown(item['environment_ko'])}",
                f"- 제작자 주장: {sanitize_markdown(item['creator_claim_ko'])}",
                f"- 확인된 사실: {sanitize_markdown(item['confirmed_fact_ko'])}",
                f"- 과제: {sanitize_markdown(item['task_ko'])}",
                f"- 교차검증: {cross_checks}",
                f"- 실패 조건과 편향: {sanitize_markdown(item['failure_conditions_ko'])}",
                "- 재현 상태: NOT TESTED",
                f"- 도입 판단: {item['adoption_decision']}",
                f"- 작은 실험: {sanitize_markdown(item['small_experiment_ko'])}",
                "",
            ]
        )
    for row in video["evidence"]:
        identifier = sanitize_markdown(row["id"]) if "id" in row else "없음"
        url = safe_url(row["url"]) if "url" in row else "없음"
        rendered.append(
            f"- 근거: {sanitize_markdown(row['reason_code'])} · id={identifier} · url={url}"
        )
    rendered.extend(["", ""])
    return newline.join(rendered)


def utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def build_kakao_summary(news: dict[str, Any], video: dict[str, Any], summary: str) -> str:
    prefix = (
        f"뉴스 {news['status']}·{news['final_count']}건 | "
        f"영상 {video['video_status']}·{video['selected_count']}편 | "
    )
    remaining = 200 - utf16_length(prefix)
    if remaining < 0:
        raise BlockedError("KAKAO_SUMMARY_UNREPRESENTABLE")
    selected: list[str] = []
    used = 0
    for character in summary:
        width = utf16_length(character)
        if used + width > remaining:
            break
        selected.append(character)
        used += width
    return prefix + "".join(selected)


def compose_markdown(
    base: str, requested_date: str, news: dict[str, Any], video: dict[str, Any]
) -> tuple[str, str]:
    lines, title_index, criteria_index, architecture_index, summary = _parse_base(
        base, requested_date
    )
    title_content, _ = _line_content(lines[title_index])
    bom = "\ufeff" if title_content.startswith("\ufeff") else ""
    comparable_title = title_content[len(bom) :]
    title_match = TITLE.fullmatch(comparable_title)
    if title_match is None:
        raise BlockedError("BASE_STRUCTURE_INVALID")
    lines[title_index] = _replace_line_content(
        lines[title_index],
        f"{bom}# {requested_date} AI Briefing   STATUS: {news['status']}",
    )
    criteria_content, _ = _line_content(lines[criteria_index])
    lines[criteria_index] = _replace_line_content(
        lines[criteria_index], COUNT_TOKEN.sub(f"검증 항목: {news['final_count']}건", criteria_content)
    )

    architecture_content, newline = _line_content(lines[architecture_index])
    if not newline:
        raise BlockedError("BASE_STRUCTURE_INVALID")
    renamed = architecture_content.replace("## 8.", "## 9.", 1) + newline
    video_section = _render_video_section(video, newline)
    final = "".join(lines[:architecture_index]) + video_section + renamed + "".join(lines[architecture_index + 1 :])
    return final, build_kakao_summary(news, video, summary)


def _cleanup_temp(temp_path: Path | None) -> bool:
    if temp_path is None:
        return True
    try:
        temp_path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def publish(
    destination: Path,
    payload: bytes,
    news: dict[str, Any],
    video: dict[str, Any],
    summary: str,
) -> tuple[int, dict[str, Any]]:
    temp_path: Path | None = None
    temp_lexical_path: str | None = None
    try:
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        temp_path = Path(temp_name)
        temp_lexical_path = os.path.abspath(temp_name)
        with os.fdopen(descriptor, "wb") as target:
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        try:
            os.link(temp_path, destination)
        except FileExistsError:
            cleaned = _cleanup_temp(temp_path)
            envelope: dict[str, Any] = {"action": "BLOCKED", "reason_code": "OUTPUT_EXISTS"}
            if not cleaned and temp_lexical_path is not None:
                envelope["cleanup_path"] = temp_lexical_path
            return 2, envelope
        except OSError:
            cleaned = _cleanup_temp(temp_path)
            envelope = {"action": "BLOCKED", "reason_code": "PUBLISH_UNAVAILABLE"}
            if not cleaned and temp_lexical_path is not None:
                envelope["cleanup_path"] = temp_lexical_path
            return 2, envelope
        try:
            temp_path.unlink()
        except OSError:
            return 2, {
                "action": "BLOCKED",
                "reason_code": "PUBLISHED_CLEANUP_FAILED",
                "output_path": str(destination),
                "cleanup_path": temp_lexical_path,
            }
        kakao = {
            "status": news["status"],
            "verified_count": news["final_count"],
            "summary": summary,
            "file_path": str(destination),
        }
        return 0, {"action": "COMPOSED", "output_path": str(destination), "kakao": kakao}
    except OSError:
        cleaned = _cleanup_temp(temp_path)
        envelope = {"action": "BLOCKED", "reason_code": "PUBLISH_UNAVAILABLE"}
        if not cleaned and temp_lexical_path is not None:
            envelope["cleanup_path"] = temp_lexical_path
        return 2, envelope


def run_composer(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    requested_date = parse_date(args.date)
    if args.news.name != f"{requested_date}.news.json":
        raise BlockedError("NEWS_BASENAME_INVALID")
    if args.video.name != f"{requested_date}.video.json":
        raise BlockedError("VIDEO_BASENAME_INVALID")
    preflight_input(args.news, "NEWS_INPUT_INVALID")
    preflight_input(args.video, "VIDEO_INPUT_INVALID")
    preflight_input(args.base_markdown, "BASE_INPUT_INVALID")
    destination = preflight_output(args.output, requested_date)

    news = read_json(args.news, "NEWS_INPUT_INVALID")
    video = read_json(args.video, "VIDEO_INPUT_INVALID")
    validate_schema(news, "news_result.schema.json", "NEWS_SCHEMA_INVALID")
    validate_schema(video, "video_result.schema.json", "VIDEO_SCHEMA_INVALID")
    validate_news_logic(news)
    validate_video_logic(video)
    final, summary = compose_markdown(
        read_base(args.base_markdown), requested_date, news, video
    )
    return publish(destination, final.encode("utf-8"), news, video, summary)


def build_parser() -> argparse.ArgumentParser:
    parser = EnvelopeArgumentParser(add_help=False)
    parser.add_argument("--date", required=True)
    parser.add_argument("--news", required=True, type=Path)
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--base-markdown", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        exit_code, envelope = run_composer(args)
    except BlockedError as exc:
        exit_code, envelope = 2, {"action": "BLOCKED", "reason_code": str(exc)}
    except Exception:
        exit_code, envelope = 3, {"action": "BLOCKED", "reason_code": "INTERNAL_ERROR"}
    print(compact_json(envelope))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
