"""실행 어댑터 — orchestrator phase 이벤트 emit (계약-conforming JSON 파일).

ACP_EVENT_CONTRACT_DRAFT §2/§4: orchestrator가 events_dir에 phase lifecycle 이벤트를
파일로 내보내면 ACP collector가 폴링한다(transport (b), 파일-폴링).

경계:
- AD-3: ztr은 acp의 모델을 import하지 않는다. **문서화된 6개 필드**를 가진 JSON만 쓰고,
  검증 권위는 소비 측(`acp.orch_events.parse_orch_event`)에 둔다. emit 전에는 type enum만 검증.
- R5: payload는 caller가 준 사실 dict를 불투명하게 기록한다(의미 해석 없음).
- 미지원 type은 실행 전 `EmitError`(BLOCKED) — acp가 거부할 파일을 쓰지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "orch/1.0"
# 계약 §2 phase lifecycle 이벤트 타입. 확장 시 계약 + acp.OrchEventType와 함께 갱신.
EVENT_TYPES = frozenset({"phase.started", "leg.result", "gate.waiting", "phase.verdict"})

# 파일명에 안전하지 않은 문자(콜론·플러스 등)를 치환 — Windows 파일명 호환.
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


class EmitError(ValueError):
    """이벤트 emit 실패 — 실행 전 BLOCKED 사유."""


def build_orch_event(
    *,
    event_type: str,
    project_id: str,
    phase_id: str,
    payload: dict[str, object],
    ts: datetime | None = None,
) -> dict[str, object]:
    """계약-conforming orch 이벤트 dict를 만든다(type 검증, ts UTC ISO 정규화)."""
    if event_type not in EVENT_TYPES:
        raise EmitError(
            f"미지원 이벤트 type: {event_type!r} (허용: {sorted(EVENT_TYPES)})"
        )
    when = ts if ts is not None else datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "phase_id": phase_id,
        "type": event_type,
        "ts": when.astimezone(timezone.utc).isoformat(),
        "payload": payload,
    }


def write_orch_event(out_dir: Path, event: dict[str, object]) -> Path:
    """이벤트를 out_dir에 고유 파일명 JSON으로 쓴다(UTF-8). 파일 경로를 반환.

    파일명 = {ts}_{phase}_{type}_{shorthash}.json. shorthash(내용 해시 앞 8자)로
    같은 ts·phase·type의 서로 다른 payload도 구분하고, 동일 이벤트는 같은 파일로 수렴.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    canonical = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    short = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]
    stamp = _UNSAFE.sub("-", str(event["ts"]))
    phase = _UNSAFE.sub("-", str(event["phase_id"])) or "phase"
    etype = _UNSAFE.sub("-", str(event["type"]))
    path = out_dir / f"{stamp}_{phase}_{etype}_{short}.json"
    path.write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")
    return path
