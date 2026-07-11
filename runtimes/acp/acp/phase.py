"""acp/phase.py — PHASE.md frontmatter 파서 (read-only).

규칙(C3):
- 파싱 실패 / 미지원 acp_schema → None 반환 + 경고 로깅.
- 절대 추정값 채움 금지. UNKNOWN 레이블로 상위에서 처리.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

logger = logging.getLogger(__name__)

_SUPPORTED_SCHEMAS = {"phase/1.0"}

# YAML frontmatter 구분자: --- 사이 블록
_FENCE = "---"


def _extract_frontmatter(text: str) -> str | None:
    """마크다운 파일에서 YAML frontmatter 블록을 추출."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FENCE:
        return None
    end = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == _FENCE:
            end = i
            break
    if end is None:
        return None
    return "\n".join(lines[1:end])


class PhaseEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    title: str
    status: str = "planned"


class PhaseDoc(BaseModel):
    """PHASE.md frontmatter의 정규화 표현."""
    model_config = ConfigDict(extra="ignore")

    acp_schema: str
    project_id: str
    roadmap_ref: str = "self"
    current_phase: str
    phase_status: str = "planned"
    pass_level: str | None = None
    updated_at: str | None = None
    phases: list[PhaseEntry] = []
    owner_session: str | None = None
    blocking: str = ""
    source_file: str = ""            # 감사용 (파서가 채움)


def parse_phase_md(path: str | Path) -> PhaseDoc | None:
    """PHASE.md에서 frontmatter만 파싱해 PhaseDoc 반환.

    실패 조건:
      - 파일 없음 → None + warning
      - frontmatter 없음 → None + warning
      - YAML 파싱 오류 → None + warning
      - 미지원 acp_schema → None + warning
      - Pydantic 검증 실패 → None + warning

    Returns:
        PhaseDoc 또는 None (실패 시). 절대 추정값 반환 금지.
    """
    p = Path(path)
    if not p.exists():
        logger.warning("PHASE.md 없음: %s", p)
        return None

    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("PHASE.md 읽기 실패: %s — %s", p, e)
        return None

    raw_fm = _extract_frontmatter(text)
    if raw_fm is None:
        logger.warning("PHASE.md frontmatter 없음: %s", p)
        return None

    try:
        data: Any = yaml.safe_load(raw_fm)
    except yaml.YAMLError as e:
        logger.warning("PHASE.md YAML 파싱 오류: %s — %s", p, e)
        return None

    if not isinstance(data, dict):
        logger.warning("PHASE.md frontmatter가 dict가 아님: %s", p)
        return None

    schema = data.get("acp_schema", "")
    if schema not in _SUPPORTED_SCHEMAS:
        logger.warning(
            "PHASE.md 미지원 acp_schema='%s' (지원: %s): %s",
            schema,
            _SUPPORTED_SCHEMAS,
            p,
        )
        return None

    # phases 리스트: YAML에서 "id: X; title: Y; status: Z" 형식(세미콜론 구분) 처리
    raw_phases = data.get("phases", [])
    normalized_phases: list[dict[str, Any]] = []
    for item in raw_phases:
        if isinstance(item, dict):
            normalized_phases.append(item)
        elif isinstance(item, str):
            # "id: P0; title: ...; status: done" 형식 파싱
            parts = {k.strip(): v.strip() for k, _, v in (seg.partition(":") for seg in item.split(";"))}
            normalized_phases.append(parts)
    data["phases"] = normalized_phases

    try:
        doc = PhaseDoc.model_validate(data)
    except ValidationError as e:
        logger.warning("PHASE.md Pydantic 검증 실패: %s — %s", p, e)
        return None

    # source_file은 파서가 채움 (감사용)
    return doc.model_copy(update={"source_file": str(p.resolve())})
