"""acp/notify_view.py — 알림을 화면이 읽는 모양으로 만드는 **단일 출처**(T14 S6 D1).

실측한 결함: 화면의 "최근 알림"이 전부 `UNKNOWN` / `-` / `–`로 떴다. 알림이 없어서가
아니라 **읽는 위치가 달랐다** — 템플릿은 `to`·`title`·`detail`을 최상위에서 읽는데,
DB에서 초기 로드한 행은 그 값들을 `payload` 안에 두고 있었다. `created_at`만 최상위라
**시각만 정상 표시**됐다.

SSE 실시간 경로는 폴러가 평평하게 발행해서 정상이었다. 즉 **두 경로가 다른 모양**이었고
한쪽만 깨졌다. 그래서 여기서 모양을 한 번만 정의하고 두 경로가 같이 쓴다.
"""
from __future__ import annotations

from typing import Any

from acp.notify import StateTransitionEvent

# 행의 사실 — payload가 덮어쓰면 안 되는 키(T14 S6 D1).
# 이 값들은 이벤트 행 자체가 말하는 것이지 알림 내용이 아니다.
# `payload`도 포함한다(구현리뷰 P3): 안에 같은 키가 있으면 **진단용 원본**이
# 통째로 덮여 무엇이 저장돼 있었는지 알 수 없게 된다.
ROW_OWNED_KEYS: frozenset[str] = frozenset(
    {"session_id", "created_at", "event_type", "id", "payload"}
)


def notification_payload(event: StateTransitionEvent) -> dict[str, Any]:
    """알림 이벤트 → 저장·발행에 쓰는 payload. 저장과 발행이 같은 값을 쓴다."""
    return {
        "from": event.from_state,
        "to": event.to_state.value,
        "title": event.title,
        "body": event.body,
        "detail": event.detail,
        "app": event.app,
        "native_session_id": event.native_session_id,
        "project_path": event.project_path,
    }


def notification_event(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """SSE로 내보내는 모양. 화면이 최상위에서 읽으므로 평평하다."""
    return {"type": "notification", "session_id": session_id, **payload}


def notification_view(row: dict[str, Any]) -> dict[str, Any]:
    """저장된 이벤트 행 → 화면이 읽는 모양. **SSE와 같은 계약**이다.

    - `payload`가 dict가 아니면 **표시 필드를 만들지 않는다**(지어내지 않기). 화면에는
      이미 `|| '-'` 폴백이 있으므로 없는 대로 그린다.
    - **행의 사실을 payload가 덮어쓰지 못한다** — `session_id`·`created_at`은 행이 말한다.
    - `payload` 원본은 그대로 남긴다(진단·기존 소비자).
    """
    view = dict(row)
    payload = row.get("payload")
    if not isinstance(payload, dict):
        return view
    for key, value in payload.items():
        if key in ROW_OWNED_KEYS:
            continue
        view[key] = value
    return view
