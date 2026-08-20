"""tests/test_join.py — 세션×PHASE.md 조인 결정 테스트."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from acp.join import join_phase
from acp.models import SessionRecord, SessionState


def _record(path: Path | str | None, *, last_activity: datetime | None = None) -> SessionRecord:
    return SessionRecord(
        app="fake",
        session_id="join-test",
        project_path=str(path) if path is not None else None,
        last_activity=last_activity,
        source_file="test",
    )


def _write_phase(path: Path, *, updated_at: str = "2026-06-10T00:00:00+00:00", status: str = "in_progress") -> None:
    path.write_text(
        "\n".join([
            "---",
            'acp_schema: "phase/1.0"',
            'project_id: "join-fixture"',
            'current_phase: "P2"',
            f'phase_status: "{status}"',
            f'updated_at: "{updated_at}"',
            'owner_session: "Implementer"',
            "phases:",
            '  - id: "P1"',
            '    title: "Codex"',
            '    status: "done"',
            '  - id: "P2"',
            '    title: "Join"',
            f'    status: "{status}"',
            "---",
            "",
        ]),
        encoding="utf-8",
    )


def test_join_phase_ok_with_progress_and_owner(tmp_path):
    project = tmp_path / "repo"
    child = project / "src" / "pkg"
    child.mkdir(parents=True)
    _write_phase(project / "PHASE.md")

    joined = join_phase(_record(str(child).replace("\\", "/")), SessionState.IDLE)

    assert joined.flag == "ok"
    assert joined.current_phase == "P2"
    assert joined.phase_status == "in_progress"
    assert joined.owner_session == "Implementer"
    assert joined.phases_done == 1
    assert joined.phases_total == 2
    assert joined.phase_source == str((project / "PHASE.md").resolve())


def test_join_phase_no_phase_file_for_missing_or_remote(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()

    assert join_phase(_record(project), SessionState.IDLE).flag == "no-phase-file"
    assert join_phase(_record("vscode-remote://ssh-remote%2Bhost/repo"), SessionState.IDLE).flag == "no-phase-file"
    assert join_phase(_record(None), SessionState.IDLE).flag == "no-phase-file"


def test_join_phase_no_phase_file_for_nonexistent_local_path_under_project(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    _write_phase(project / "PHASE.md")

    joined = join_phase(_record(project / "deleted-workspace"), SessionState.IDLE)

    assert joined.flag == "no-phase-file"


def test_join_phase_unknown_when_phase_file_is_unparseable(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    (project / "PHASE.md").write_text("---\nacp_schema: nope\n---\n", encoding="utf-8")

    joined = join_phase(_record(project), SessionState.IDLE)

    assert joined.flag == "unknown"
    assert joined.phase_source == str((project / "PHASE.md").resolve())


def test_join_phase_plan_stale_when_phase_doc_is_older_than_activity(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    _write_phase(project / "PHASE.md", updated_at="2026-06-09T00:00:00+00:00")
    record = _record(project, last_activity=datetime(2026, 6, 10, 1, 0, tzinfo=timezone.utc))

    joined = join_phase(record, SessionState.LIVE)

    assert joined.flag == "ok"
    assert joined.plan_stale is True


def test_join_phase_plan_stale_when_current_phase_is_done(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    _write_phase(project / "PHASE.md", updated_at=datetime.now(timezone.utc).isoformat(), status="done")

    joined = join_phase(_record(project, last_activity=datetime.now(timezone.utc)), SessionState.IDLE)

    assert joined.flag == "ok"
    assert joined.plan_stale is True


def test_join_phase_does_not_mark_inactive_or_bad_date_as_stale(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    _write_phase(project / "PHASE.md", updated_at="not-a-date")
    record = _record(project, last_activity=datetime.now(timezone.utc) + timedelta(days=5))

    assert join_phase(record, SessionState.STALE).plan_stale is False
    assert join_phase(record, SessionState.LIVE).plan_stale is False


def test_join_phase_stops_at_git_boundary(tmp_path):
    parent = tmp_path / "parent"
    repo = parent / "repo"
    child = repo / "src"
    child.mkdir(parents=True)
    (repo / ".git").mkdir()
    _write_phase(parent / "PHASE.md")

    joined = join_phase(_record(child), SessionState.IDLE)

    assert joined.flag == "no-phase-file"


def test_join_phase_respects_parent_search_limit(tmp_path):
    cur = tmp_path / "root"
    cur.mkdir()
    _write_phase(cur / "PHASE.md")
    too_deep = cur
    for i in range(6):
        too_deep = too_deep / f"d{i}"
    too_deep.mkdir(parents=True)

    joined = join_phase(_record(too_deep), SessionState.IDLE)

    assert joined.flag == "no-phase-file"
