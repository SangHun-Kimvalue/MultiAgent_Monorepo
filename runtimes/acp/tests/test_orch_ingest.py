"""orchestrator 이벤트 소비 파이프라인 테스트 — OrchEventCollector + store.record/list.

계약: ACP_EVENT_CONTRACT_DRAFT §3(파싱 실패 silent drop 금지) / §4(파일-폴링).
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from acp.collectors.orch_collector import OrchEventCollector
from acp.orch_events import OrchEventType, OrchPhaseEvent


def _event(phase_id: str, event_type: OrchEventType, **payload: object) -> dict[str, object]:
    return {
        "schema_version": "orch/1.0",
        "project_id": "TestProject",
        "phase_id": phase_id,
        "type": event_type.value,
        "ts": "2026-06-14T12:00:00+00:00",
        "payload": payload or {"status": "PASS"},
    }


def _write(events_dir: Path, name: str, obj: object) -> Path:
    events_dir.mkdir(parents=True, exist_ok=True)
    path = events_dir / name
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return path


# ── collector ───────────────────────────────────────────────────────────

def test_collect_valid_events(tmp_path: Path) -> None:
    events_dir = tmp_path / "acp-events"
    _write(events_dir, "01.json", _event("P1", OrchEventType.PHASE_STARTED))
    _write(events_dir, "02.json", _event("P1", OrchEventType.PHASE_VERDICT, status="PASS"))

    result = OrchEventCollector(events_dir).collect()

    assert len(result.events) == 2
    assert result.failures == []
    assert {e.type for e in result.events} == {
        OrchEventType.PHASE_STARTED,
        OrchEventType.PHASE_VERDICT,
    }


def test_collect_missing_dir_is_empty(tmp_path: Path) -> None:
    result = OrchEventCollector(tmp_path / "nope").collect()

    assert result.events == []
    assert result.failures == []


def test_collect_invalid_schema_is_reported_not_dropped(tmp_path: Path) -> None:
    # 스키마 불일치 이벤트는 조용히 버리지 않고 failures로 보고(C3 / 계약 §3)
    events_dir = tmp_path / "acp-events"
    bad = _event("P1", OrchEventType.PHASE_STARTED)
    bad["schema_version"] = "orch/99.0"
    _write(events_dir, "bad.json", bad)
    _write(events_dir, "good.json", _event("P2", OrchEventType.LEG_RESULT))

    result = OrchEventCollector(events_dir).collect()

    assert len(result.events) == 1
    assert result.events[0].phase_id == "P2"
    assert len(result.failures) == 1
    assert result.failures[0].path.endswith("bad.json")


def test_collect_malformed_json_is_reported(tmp_path: Path) -> None:
    events_dir = tmp_path / "acp-events"
    events_dir.mkdir(parents=True)
    (events_dir / "broken.json").write_text("{ not json", encoding="utf-8")

    result = OrchEventCollector(events_dir).collect()

    assert result.events == []
    assert len(result.failures) == 1
    assert "read/json" in result.failures[0].reason


def test_collect_reports_json_and_schema_failures_without_dropping_valid_event(
    tmp_path: Path,
) -> None:
    events_dir = tmp_path / "acp-events"
    events_dir.mkdir(parents=True)
    broken_path = events_dir / "broken.json"
    schema_path = _write(events_dir, "bad_schema.json", {"type": "bogus"})
    valid_path = _write(events_dir, "valid.json", _event("P1", OrchEventType.PHASE_STARTED))
    broken_path.write_text("{", encoding="utf-8")

    result = OrchEventCollector(events_dir).collect()

    assert len(result.events) == 1
    assert result.events[0].phase_id == "P1"
    assert len(result.failures) == 2
    failures_by_path = {failure.path: failure.reason for failure in result.failures}
    assert failures_by_path[str(broken_path)].startswith("read/json:")
    assert failures_by_path[str(schema_path)].startswith("validate:")
    assert str(valid_path) not in failures_by_path


def test_collect_is_read_only(tmp_path: Path) -> None:
    events_dir = tmp_path / "acp-events"
    p1 = _write(events_dir, "01.json", _event("P1", OrchEventType.PHASE_STARTED))
    before = p1.read_text(encoding="utf-8")

    OrchEventCollector(events_dir).collect()

    # collect()는 파일을 수정/삭제하지 않는다(C7)
    assert p1.exists()
    assert p1.read_text(encoding="utf-8") == before


# ── store ingest ─────────────────────────────────────────────────────────

def test_record_orch_event_is_idempotent(tmp_store, tmp_path: Path) -> None:
    events_dir = tmp_path / "acp-events"
    _write(events_dir, "01.json", _event("P1", OrchEventType.PHASE_VERDICT, status="PASS"))
    event = OrchEventCollector(events_dir).collect().events[0]

    first = tmp_store.record_orch_event(event)
    second = tmp_store.record_orch_event(event)

    assert first is True   # 신규 삽입
    assert second is False  # 동일 이벤트 재수집 → 무시
    assert len(tmp_store.list_orch_events()) == 1


def test_record_orch_event_dedup_is_normalized(tmp_store) -> None:
    # payload 키 순서·ts tz 표기가 달라도 같은 논리적 이벤트면 1행으로 수렴(정규화 멱등).
    # byte-identical 재수집만 막던 갭(리뷰 HIGH)을 잡는다.
    from datetime import datetime, timedelta, timezone

    from acp.orch_events import OrchEventType, OrchPhaseEvent

    base = OrchPhaseEvent(
        project_id="P",
        phase_id="P1",
        type=OrchEventType.LEG_RESULT,
        ts=datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc),
        payload={"a": 1, "b": 2},
    )
    # 같은 순간을 +09:00으로 표기 + payload 키 순서 반대
    variant = OrchPhaseEvent(
        project_id="P",
        phase_id="P1",
        type=OrchEventType.LEG_RESULT,
        ts=datetime(2026, 6, 14, 21, 0, tzinfo=timezone(timedelta(hours=9))),
        payload={"b": 2, "a": 1},
    )

    assert tmp_store.record_orch_event(base) is True
    assert tmp_store.record_orch_event(variant) is False  # 정규화 후 동일 → 무시
    assert len(tmp_store.list_orch_events()) == 1


def test_list_orch_events_deserializes_payload_and_filters(tmp_store, tmp_path: Path) -> None:
    events_dir = tmp_path / "acp-events"
    _write(events_dir, "01.json", _event("P1", OrchEventType.PHASE_STARTED, target="x"))
    _write(events_dir, "02.json", _event("P2", OrchEventType.PHASE_VERDICT, status="PASS"))
    for event in OrchEventCollector(events_dir).collect().events:
        tmp_store.record_orch_event(event)

    all_rows = tmp_store.list_orch_events()
    p2_rows = tmp_store.list_orch_events(phase_id="P2")

    assert len(all_rows) == 2
    assert isinstance(all_rows[0]["payload"], dict)
    assert len(p2_rows) == 1
    assert p2_rows[0]["phase_id"] == "P2"
    assert p2_rows[0]["payload"] == {"status": "PASS"}
    assert p2_rows[0]["type"] == "phase.verdict"


def test_list_orch_events_applies_limit_cap(tmp_store) -> None:
    for phase_id in ("P1", "P2", "P3"):
        event = OrchPhaseEvent(
            project_id="P",
            phase_id=phase_id,
            type=OrchEventType.LEG_RESULT,
            ts=datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc),
            payload={"phase_id": phase_id},
        )
        assert tmp_store.record_orch_event(event) is True

    rows = tmp_store.list_orch_events(limit=2)

    assert len(rows) == 2


def test_list_orch_events_orders_by_ts_desc(tmp_store) -> None:
    earlier = OrchPhaseEvent(
        project_id="P",
        phase_id="earlier",
        type=OrchEventType.LEG_RESULT,
        ts=datetime(2026, 6, 14, 11, 0, tzinfo=timezone.utc),
        payload={"status": "EARLIER"},
    )
    later = OrchPhaseEvent(
        project_id="P",
        phase_id="later",
        type=OrchEventType.LEG_RESULT,
        ts=datetime(2026, 6, 14, 13, 0, tzinfo=timezone.utc),
        payload={"status": "LATER"},
    )
    assert tmp_store.record_orch_event(earlier) is True
    assert tmp_store.record_orch_event(later) is True

    rows = tmp_store.list_orch_events()

    assert [row["phase_id"] for row in rows] == ["later", "earlier"]
