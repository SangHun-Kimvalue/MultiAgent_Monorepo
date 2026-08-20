"""tests/test_phase.py — parse_phase_md 단위 테스트."""
from __future__ import annotations

from pathlib import Path

import pytest

from acp.phase import parse_phase_md, PhaseDoc


# ── 정상 케이스 ──

def test_parse_valid_phase_md(tmp_path: Path):
    """유효한 PHASE.md → PhaseDoc 반환."""
    f = tmp_path / "PHASE.md"
    f.write_text(
        '---\n'
        'acp_schema: "phase/1.0"\n'
        'project_id: "TestProject"\n'
        'roadmap_ref: "self"\n'
        'current_phase: "P0"\n'
        'phase_status: "in_progress"\n'
        'updated_at: "2026-06-09T12:00:00+09:00"\n'
        'phases:\n'
        '  - id: "P0"\n'
        '    title: "스캐폴드"\n'
        '    status: "in_progress"\n'
        '  - id: "P1"\n'
        '    title: "수집"\n'
        '    status: "planned"\n'
        '---\n'
        '# TestProject\n',
        encoding="utf-8",
    )
    doc = parse_phase_md(f)
    assert doc is not None
    assert isinstance(doc, PhaseDoc)
    assert doc.project_id == "TestProject"
    assert doc.current_phase == "P0"
    assert doc.phase_status == "in_progress"
    assert len(doc.phases) == 2
    assert doc.phases[0].id == "P0"
    assert doc.source_file != ""


def test_parse_real_phase_md():
    """AgentControlPlane/PHASE.md 자체를 파싱 (실측)."""
    real = Path(__file__).parent.parent / "PHASE.md"
    if not real.exists():
        pytest.skip("PHASE.md 없음")
    doc = parse_phase_md(real)
    assert doc is not None
    assert doc.project_id == "AgentControlPlane"
    assert doc.current_phase is not None


# ── 실패 케이스 (C3: 모두 None 반환, silent 금지) ──

def test_parse_unsupported_schema(tmp_path: Path):
    """미지원 acp_schema → None."""
    f = tmp_path / "PHASE.md"
    f.write_text(
        '---\nacp_schema: "phase/99.0"\nproject_id: "X"\ncurrent_phase: "P0"\n---\n',
        encoding="utf-8",
    )
    assert parse_phase_md(f) is None


def test_parse_broken_yaml(tmp_path: Path):
    """깨진 YAML → None."""
    f = tmp_path / "PHASE.md"
    f.write_text('---\n: : :\n---\n', encoding="utf-8")
    assert parse_phase_md(f) is None


def test_parse_missing_file(tmp_path: Path):
    """파일 없음 → None."""
    assert parse_phase_md(tmp_path / "nonexistent.md") is None


def test_parse_no_frontmatter(tmp_path: Path):
    """frontmatter 없는 마크다운 → None."""
    f = tmp_path / "PHASE.md"
    f.write_text("# 그냥 마크다운\n내용\n", encoding="utf-8")
    assert parse_phase_md(f) is None


def test_parse_missing_required_field(tmp_path: Path):
    """필수 필드(project_id) 누락 → None."""
    f = tmp_path / "PHASE.md"
    f.write_text(
        '---\nacp_schema: "phase/1.0"\ncurrent_phase: "P0"\n---\n',
        encoding="utf-8",
    )
    assert parse_phase_md(f) is None
