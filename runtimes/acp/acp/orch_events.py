"""Consumer-side orchestrator phase lifecycle event contract."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class OrchEventType(StrEnum):
    PHASE_STARTED = "phase.started"
    LEG_RESULT = "leg.result"
    GATE_WAITING = "gate.waiting"
    PHASE_VERDICT = "phase.verdict"


class OrchPhaseEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "orch/1.0"
    project_id: str
    phase_id: str
    type: OrchEventType
    ts: datetime
    payload: dict[str, object] = Field(default_factory=dict)

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: str) -> str:
        if value != "orch/1.0":
            raise ValueError("unsupported orchestrator event schema_version")
        return value


def parse_orch_event(raw: dict[str, Any]) -> OrchPhaseEvent:
    """Validate and parse an orchestrator event without silent fallback."""
    if not isinstance(raw, dict):
        raise ValueError("orchestrator event must be a dict")

    try:
        return OrchPhaseEvent.model_validate(raw)
    except ValidationError as exc:
        raise ValueError("invalid orchestrator event") from exc
