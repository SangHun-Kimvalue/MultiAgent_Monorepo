"""tests/test_cursor_collector.py — CursorWorkspaceCollector 결정 테스트."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from acp.collectors.cursor import CursorWorkspaceCollector
from acp.join import join_phase
from acp.models import SessionState


def _write_workspace(base, ws_hash: str, folder: str, *, with_db: bool = True, ts: float = 1234567890.0):
    wsdir = base / ws_hash
    wsdir.mkdir(parents=True)
    wsjson = wsdir / "workspace.json"
    wsjson.write_text(json.dumps({"folder": folder}), encoding="utf-8")
    os.utime(wsjson, (ts - 10, ts - 10))
    if with_db:
        db = wsdir / "state.vscdb"
        db.write_bytes(b"sqlite is not opened by collector")
        os.utime(db, (ts, ts))
    return wsjson


def test_cursor_collector_decodes_file_uri_and_uses_state_db_mtime(tmp_path):
    base = tmp_path / "workspaceStorage"
    _write_workspace(base, "hash-a", "file:///c:/Users/shkim/Desktop/Proj", ts=1700000000.0)

    records = CursorWorkspaceCollector(base).collect()

    assert len(records) == 1
    rec = records[0]
    assert rec.app == "cursor"
    assert rec.session_id == "hash-a"
    assert rec.project_path == r"c:\Users\shkim\Desktop\Proj"
    assert rec.last_activity == datetime.fromtimestamp(1700000000.0, tz=timezone.utc)
    assert rec.source_file.endswith("state.vscdb")


def test_cursor_collector_preserves_remote_uri_and_join_reports_no_phase_file(tmp_path):
    base = tmp_path / "workspaceStorage"
    remote = "vscode-remote://ssh-remote%2Brpi_5/home/pi/Cubi_xCeler_rpi"
    _write_workspace(base, "hash-remote", remote)

    rec = CursorWorkspaceCollector(base).collect()[0]
    joined = join_phase(rec, SessionState.IDLE)

    assert rec.project_path == remote
    assert joined.flag == "no-phase-file"


def test_cursor_collector_falls_back_to_workspace_mtime(tmp_path):
    base = tmp_path / "workspaceStorage"
    _write_workspace(base, "hash-b", "file:///c:/Proj", with_db=False, ts=1710000000.0)

    rec = CursorWorkspaceCollector(base).collect()[0]

    assert rec.last_activity == datetime.fromtimestamp(1710000000.0 - 10, tz=timezone.utc)
    assert rec.source_file.endswith("workspace.json")


def test_cursor_collector_missing_folder_returns_empty(tmp_path):
    assert CursorWorkspaceCollector(tmp_path / "missing").collect() == []
