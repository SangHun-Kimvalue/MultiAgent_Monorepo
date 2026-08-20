from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from acp.orch_events import OrchEventType, OrchPhaseEvent, parse_orch_event


def _event_dict(event_type: OrchEventType) -> dict[str, object]:
    return {
        "schema_version": "orch/1.0",
        "project_id": "TestProject",
        "phase_id": "P1",
        "type": event_type.value,
        "ts": "2026-06-14T12:00:00+00:00",
        "payload": {"status": "PASS"},
    }


@pytest.mark.parametrize("event_type", list(OrchEventType))
def test_orch_phase_event_round_trip(event_type: OrchEventType):
    event = OrchPhaseEvent(
        project_id="TestProject",
        phase_id="P1",
        type=event_type,
        ts=datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc),
        payload={"leg": "implementer"},
    )

    dumped = event.model_dump()
    round_tripped = OrchPhaseEvent.model_validate(dumped)

    assert dumped["schema_version"] == "orch/1.0"
    assert dumped["type"] == event_type
    assert round_tripped == event


def test_orch_phase_event_json_round_trip_through_parse_orch_event():
    event = OrchPhaseEvent(
        project_id="TestProject",
        phase_id="P1",
        type=OrchEventType.LEG_RESULT,
        ts=datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc),
        payload={"leg": "implementer"},
    )

    dumped = json.loads(event.model_dump_json())
    round_tripped = parse_orch_event(dumped)

    assert dumped["type"] == OrchEventType.LEG_RESULT.value
    assert dumped["ts"] == "2026-06-14T12:00:00Z"
    assert round_tripped == event


def test_parse_orch_event_valid_dict():
    event = parse_orch_event(_event_dict(OrchEventType.PHASE_VERDICT))

    assert event.schema_version == "orch/1.0"
    assert event.project_id == "TestProject"
    assert event.phase_id == "P1"
    assert event.type == OrchEventType.PHASE_VERDICT
    assert event.payload == {"status": "PASS"}


def test_parse_orch_event_invalid_type_raises_value_error():
    raw = _event_dict(OrchEventType.PHASE_STARTED)
    raw["type"] = "phase.unknown"

    with pytest.raises(ValueError):
        parse_orch_event(raw)


def test_parse_orch_event_missing_required_field_raises_value_error():
    raw = _event_dict(OrchEventType.PHASE_STARTED)
    del raw["project_id"]

    with pytest.raises(ValueError):
        parse_orch_event(raw)


def test_parse_orch_event_schema_mismatch_raises_value_error():
    raw = _event_dict(OrchEventType.PHASE_STARTED)
    raw["schema_version"] = "orch/99.0"

    with pytest.raises(ValueError):
        parse_orch_event(raw)


def test_parse_orch_event_unknown_top_level_key_raises_value_error():
    raw = _event_dict(OrchEventType.PHASE_STARTED)
    raw["acp_event_schema"] = "orch/1.0"

    with pytest.raises(ValueError):
        parse_orch_event(raw)
