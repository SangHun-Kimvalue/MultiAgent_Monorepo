"""acp/web/app.py — FastAPI 대시보드.

ZTR src/web/app.py의 SSE broadcaster 패턴(lines 44-66, 192-214)을 이식.
엔드포인트:
  GET /                   → Jinja2 대시보드 (세션 테이블)
  GET /api/sessions       → JSON 세션 목록
  GET /api/live/stream    → SSE 실시간 스트림 (30s keepalive ping)
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
import json
import logging
from pathlib import Path
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from acp.orch_runs import (
    ActiveRunConflictError,
    InvalidRunStateError,
    OrchRunManager,
    OrchRunStartRequest,
    UnknownRunError,
)
from acp.store import SessionStore

logger = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _WEB_DIR / "templates"
_STATIC_DIR = _WEB_DIR / "static"

app = FastAPI(title="Agent Control Plane", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# 글로벌 상태 (앱 lifespan에서 주입)
_store: SessionStore | None = None
_broadcaster: "EventBroadcaster | None" = None
_run_manager: OrchRunManager | None = None
_poll_interval: float = 15.0


def init_app(
    store: SessionStore,
    broadcaster: "EventBroadcaster",
    poll_interval: float = 15.0,
    run_manager: OrchRunManager | None = None,
) -> None:
    """main에서 의존성 주입."""
    global _store, _broadcaster, _run_manager, _poll_interval
    _store = store
    _broadcaster = broadcaster
    _run_manager = run_manager or OrchRunManager(store=store, broadcaster=broadcaster)
    _poll_interval = poll_interval


def get_store() -> SessionStore:
    assert _store is not None, "store 미초기화 — init_app() 먼저 호출"
    return _store


def get_broadcaster() -> "EventBroadcaster":
    assert _broadcaster is not None, "broadcaster 미초기화 — init_app() 먼저 호출"
    return _broadcaster


def get_run_manager() -> OrchRunManager:
    assert _run_manager is not None, "run_manager 미초기화 — init_app() 먼저 호출"
    return _run_manager


# ── SSE 이벤트 브로드캐스터 (ZTR 패턴 이식) ──

class EventBroadcaster:
    """SSE 이벤트를 여러 클라이언트에 브로드캐스트."""

    def __init__(self) -> None:
        self._queues: list[asyncio.Queue[dict[str, Any]]] = []

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        if q in self._queues:
            self._queues.remove(q)

    async def publish(self, event: dict[str, Any]) -> None:
        for q in self._queues:
            await q.put(event)


# 싱글턴 브로드캐스터 (main에서 init_app에 전달)
broadcaster = EventBroadcaster()


# ── 상태 배지 색상 헬퍼 ──

_STATE_BADGE = {
    "live":     "badge-live",
    "running":  "badge-running",
    "idle":     "badge-idle",
    "holding":  "badge-holding",
    "stale":    "badge-stale",
    "error":    "badge-error",
    "done":     "badge-done",
    "unknown":  "badge-unknown",
}


def _badge_class(state: str) -> str:
    return _STATE_BADGE.get(state.lower(), "badge-unknown")


_PHASE_BADGE = {
    "ok": "badge-phase-ok",
    "no-phase-file": "badge-phase-missing",
    "unknown": "badge-phase-unknown",
}


def _phase_badge_class(session: dict[str, Any]) -> str:
    if session.get("plan_stale"):
        return "badge-phase-stale"
    flag = str(session.get("phase_flag") or "no-phase-file")
    return _PHASE_BADGE.get(flag, "badge-phase-unknown")


def _phase_label(session: dict[str, Any]) -> str:
    if session.get("plan_stale"):
        return "plan-stale"
    flag = session.get("phase_flag")
    if flag and flag != "ok":
        return str(flag)
    return session.get("current_phase") or "no-phase-file"


def _progress_text(session: dict[str, Any]) -> str:
    total = int(session.get("phases_total") or 0)
    done = int(session.get("phases_done") or 0)
    if total <= 0:
        return "-"
    return f"{done}/{total}"


def _group_sessions(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for session in sessions:
        grouped[(session.get("app") or "unknown", session.get("project_path") or "no-project")].append(session)

    result: list[dict[str, Any]] = []
    for (app_name, project_path), rows in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1])):
        rows.sort(key=lambda row: str(row.get("updated_at") or ""), reverse=True)
        result.append({"app": app_name, "project_path": project_path, "sessions": rows})
    return result


templates.env.globals["badge_class"] = _badge_class
templates.env.globals["phase_badge_class"] = _phase_badge_class
templates.env.globals["phase_label"] = _phase_label
templates.env.globals["progress_text"] = _progress_text


# ════════════════════════════════════════
# HTML 대시보드
# ════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def page_dashboard(request: Request) -> HTMLResponse:
    store = get_store()
    sessions = store.list_sessions(limit=100)
    grouped_sessions = _group_sessions(sessions)
    notifications = store.list_events(limit=10, event_type="notification_sent")
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "sessions": sessions,
            "grouped_sessions": grouped_sessions,
            "notifications": notifications,
            "poll_interval": _poll_interval,
            "title": "Agent Control Plane",
        },
    )


# ════════════════════════════════════════
# JSON API
# ════════════════════════════════════════

@app.get("/api/sessions")
async def api_sessions(limit: int = 100) -> list[dict[str, Any]]:
    return get_store().list_sessions(limit=limit)


@app.get("/api/orch-events")
async def api_orch_events(limit: int = 50, phase_id: str | None = None) -> list[dict[str, Any]]:
    return get_store().list_orch_events(limit=limit, phase_id=phase_id)


@app.post("/api/orch/run")
async def api_orch_run(request: OrchRunStartRequest) -> dict[str, Any]:
    try:
        state = await get_run_manager().start_run(request)
    except ActiveRunConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return state.model_dump(mode="json")


@app.post("/api/orch/runs/{run_id}/approve")
async def api_orch_run_approve(run_id: str) -> dict[str, Any]:
    try:
        state = await get_run_manager().approve_run(run_id)
    except UnknownRunError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidRunStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return state.model_dump(mode="json")


# ════════════════════════════════════════
# SSE 실시간 스트림 (ZTR 192-214 패턴 이식)
# ════════════════════════════════════════

@app.get("/api/live/stream")
async def sse_live_stream(request: Request) -> EventSourceResponse:
    """SSE 스트림: 세션 상태 변경 이벤트를 실시간으로 push. 30s keepalive ping."""
    queue = get_broadcaster().subscribe()

    async def event_generator() -> AsyncIterator[dict[str, str]]:
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield {
                        "event": event.get("type", "update"),
                        "data": json.dumps(event, ensure_ascii=False),
                    }
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": ""}
        finally:
            get_broadcaster().unsubscribe(queue)

    return EventSourceResponse(event_generator())
