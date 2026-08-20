"""orchestrator 이벤트 emit(event_emit) 테스트 — ACP_EVENT_CONTRACT_DRAFT §2/§4."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.engine.event_emit import (
    EVENT_TYPES,
    SCHEMA_VERSION,
    EmitError,
    build_orch_event,
    write_orch_event,
)

_CONTRACT_KEYS = {"schema_version", "project_id", "phase_id", "type", "ts", "payload"}


# ── build_orch_event ──────────────────────────────────────────────────────

@pytest.mark.parametrize("event_type", sorted(EVENT_TYPES))
def test_build_event_has_exactly_contract_keys(event_type: str) -> None:
    event = build_orch_event(
        event_type=event_type, project_id="P", phase_id="P1", payload={"k": "v"},
        ts=datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc),
    )

    assert set(event) == _CONTRACT_KEYS  # acp extra=forbid와 정합(6필드 정확히)
    assert event["schema_version"] == SCHEMA_VERSION == "orch/1.0"
    assert event["type"] == event_type
    assert event["payload"] == {"k": "v"}


def test_build_event_unsupported_type_blocked() -> None:
    with pytest.raises(EmitError, match="미지원 이벤트 type"):
        build_orch_event(event_type="phase.unknown", project_id="P", phase_id="P1", payload={})


def test_build_event_ts_default_is_utc_aware() -> None:
    event = build_orch_event(event_type="phase.started", project_id="P", phase_id="P1", payload={})

    parsed = datetime.fromisoformat(str(event["ts"]))
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)  # UTC


def test_build_event_naive_ts_assumed_utc() -> None:
    event = build_orch_event(
        event_type="phase.started", project_id="P", phase_id="P1", payload={},
        ts=datetime(2026, 6, 14, 12, 0),  # naive
    )

    assert str(event["ts"]) == "2026-06-14T12:00:00+00:00"


def test_build_event_payload_is_opaque() -> None:
    # R5: payload는 불투명하게 그대로 기록(중첩/타입 보존)
    payload = {"nested": {"a": 1}, "list": [1, 2], "flag": True}
    event = build_orch_event(event_type="leg.result", project_id="P", phase_id="P1", payload=payload)

    assert event["payload"] == payload


# ── write_orch_event ──────────────────────────────────────────────────────

def test_write_event_creates_filename_safe_json(tmp_path: Path) -> None:
    event = build_orch_event(
        event_type="phase.verdict", project_id="P", phase_id="P1", payload={"status": "PASS"},
        ts=datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc),
    )

    path = write_orch_event(tmp_path / "ev", event)

    assert path.exists()
    assert ":" not in path.name  # Windows 파일명 안전
    assert json.loads(path.read_text(encoding="utf-8")) == event


def test_write_event_idempotent_same_event_one_file(tmp_path: Path) -> None:
    event = build_orch_event(
        event_type="phase.verdict", project_id="P", phase_id="P1", payload={"status": "PASS"},
        ts=datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc),
    )

    p1 = write_orch_event(tmp_path / "ev", event)
    p2 = write_orch_event(tmp_path / "ev", event)

    assert p1 == p2  # 같은 이벤트 → 같은 파일명(내용 해시)
    assert len(list((tmp_path / "ev").glob("*.json"))) == 1


def test_write_event_different_payload_different_file(tmp_path: Path) -> None:
    common = dict(event_type="leg.result", project_id="P", phase_id="P1",
                  ts=datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc))
    p1 = write_orch_event(tmp_path / "ev", build_orch_event(payload={"leg": "implementer"}, **common))
    p2 = write_orch_event(tmp_path / "ev", build_orch_event(payload={"leg": "reviewer"}, **common))

    assert p1 != p2
    assert len(list((tmp_path / "ev").glob("*.json"))) == 2


# ── CLI smoke ─────────────────────────────────────────────────────────────

def _emit_cli(*extra: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    repo = str(Path.cwd())
    env["PYTHONPATH"] = repo if not env.get("PYTHONPATH") else f"{repo}{os.pathsep}{env['PYTHONPATH']}"
    return subprocess.run(
        [sys.executable, "-m", "src", "emit-event", *extra],
        cwd=Path.cwd(), env=env, text=True, encoding="utf-8",
        capture_output=True, timeout=30, check=False,
    )


def test_emit_event_cli_writes_contract_file(tmp_path: Path) -> None:
    out = tmp_path / "acp-events"
    proc = _emit_cli(
        "--type", "phase.verdict", "--project-id", "P", "--phase-id", "P1",
        "--out-dir", str(out), "--payload", json.dumps({"status": "PASS"}),
    )

    assert proc.returncode == 0
    result = json.loads(proc.stdout)
    written = Path(result["event_path"])
    assert written.exists()
    event = json.loads(written.read_text(encoding="utf-8"))
    assert set(event) == _CONTRACT_KEYS
    assert event["type"] == "phase.verdict"
    assert event["payload"] == {"status": "PASS"}


def test_emit_event_cli_unsupported_type_is_blocked_exit2(tmp_path: Path) -> None:
    proc = _emit_cli(
        "--type", "phase.bogus", "--project-id", "P", "--phase-id", "P1",
        "--out-dir", str(tmp_path / "ev"),
    )

    assert proc.returncode == 2
