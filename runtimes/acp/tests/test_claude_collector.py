"""tests/test_claude_collector.py — ClaudeSessionCollector 결정 테스트."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from acp.collectors.claude import ClaudeSessionCollector


def test_claude_collector_maps_local_json(tmp_path):
    base = tmp_path / "claude-code-sessions" / "session" / "turn"
    base.mkdir(parents=True)
    source = base / "local_abc.json"
    source.write_text(
        json.dumps({
            "sessionId": "local_abc",
            "cwd": r"C:\proj\alpha",
            "lastActivityAt": 1776756031313,
            "model": "claude-opus-4-7[1m]",
        }),
        encoding="utf-8",
    )

    records = ClaudeSessionCollector(tmp_path / "claude-code-sessions").collect()

    assert len(records) == 1
    rec = records[0]
    assert rec.app == "claude"
    assert rec.session_id == "local_abc"
    assert rec.project_path == r"C:\proj\alpha"
    assert rec.model == "claude-opus-4-7[1m]"
    assert rec.last_activity == datetime.fromtimestamp(1776756031313 / 1000, tz=timezone.utc)
    assert rec.running_pid is None
    assert rec.running_cmd is None
    assert rec.last_event is None
    assert rec.source_file == str(source)


def test_claude_collector_skips_bad_json_and_continues(tmp_path, caplog):
    base = tmp_path / "claude-code-sessions" / "session"
    base.mkdir(parents=True)
    (base / "local_bad.json").write_text("{not-json", encoding="utf-8")
    (base / "local_ok.json").write_text(
        json.dumps({"sessionId": "local_ok", "cwd": str(tmp_path), "lastActivityAt": 1000}),
        encoding="utf-8",
    )

    records = ClaudeSessionCollector(tmp_path / "claude-code-sessions").collect()

    assert [r.session_id for r in records] == ["local_ok"]
    assert "파싱 실패" in caplog.text


def test_claude_collector_missing_folder_returns_empty(tmp_path):
    assert ClaudeSessionCollector(tmp_path / "missing").collect() == []
