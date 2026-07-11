"""ZTR Dashboard — FastAPI + Jinja2 + HTMX + SSE.

Usage:
    python -m src.web.app
    # → http://localhost:8000
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from src.engine.session_store import SessionStore

logger = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _WEB_DIR / "templates"
_STATIC_DIR = _WEB_DIR / "static"

app = FastAPI(title="ZTR Dashboard", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# 글로벌 SessionStore (앱 시작 시 초기화)
_store: SessionStore | None = None


def get_store() -> SessionStore:
    global _store
    if _store is None:
        _store = SessionStore(".ztr/sessions.db")
    return _store


# ── SSE 이벤트 브로드캐스터 ──

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


broadcaster = EventBroadcaster()


# ════════════════════════════════════════
# HTML Pages (Jinja2 + HTMX)
# ════════════════════════════════════════


@app.get("/", response_class=HTMLResponse)
async def page_sessions(request: Request) -> HTMLResponse:
    """Session List 페이지."""
    store = get_store()
    sessions = store.list_sessions(limit=50)
    stats = store.agent_stats()

    # 요약 통계
    total = len(store.list_sessions(limit=9999))
    completed = sum(1 for s in store.list_sessions(limit=9999) if s["status"] == "completed")
    success_rate = round(completed / total * 100, 1) if total else 0
    avg_rounds = round(
        sum(s["rounds"] or 0 for s in sessions) / len(sessions), 1
    ) if sessions else 0
    total_tokens = sum(s["total_tokens"] for s in stats) if stats else 0

    return templates.TemplateResponse(request, "sessions.html", {
        "sessions": sessions,
        "total_sessions": total,
        "success_rate": success_rate,
        "avg_rounds": avg_rounds,
        "total_tokens": total_tokens,
    })


@app.get("/session/{session_id}", response_class=HTMLResponse)
async def page_session_detail(request: Request, session_id: int) -> HTMLResponse:
    """Session Detail 페이지."""
    store = get_store()
    session = store.get_session(session_id)
    if not session:
        return HTMLResponse("<h1>Session not found</h1>", status_code=404)

    round_logs = store.get_round_logs(session_id)
    metrics = store.get_session_metrics(session_id)

    # 에이전트별 토큰 집계
    token_usage: dict[str, dict[str, Any]] = {}
    for m in metrics:
        aid = m["agent_id"]
        if aid not in token_usage:
            token_usage[aid] = {"tokens": 0, "latency_ms": 0.0}
        token_usage[aid]["tokens"] += m["tokens_used"] or 0
        token_usage[aid]["latency_ms"] += m["latency_ms"] or 0

    # findings 집계
    total_blockers = 0
    total_majors = 0
    total_minors = 0
    for rl in round_logs:
        for f in rl.get("findings", []):
            sev = f.get("severity", "")
            if sev == "blocker":
                total_blockers += 1
            elif sev == "major":
                total_majors += 1
            elif sev == "minor":
                total_minors += 1

    return templates.TemplateResponse(request, "session_detail.html", {
        "session": session,
        "round_logs": round_logs,
        "token_usage": token_usage,
        "total_blockers": total_blockers,
        "total_majors": total_majors,
        "total_minors": total_minors,
    })


@app.get("/agents", response_class=HTMLResponse)
async def page_agent_stats(request: Request) -> HTMLResponse:
    """Agent Stats 페이지."""
    store = get_store()
    stats = store.agent_stats()
    return templates.TemplateResponse(request, "agent_stats.html", {
        "stats": stats,
    })


@app.get("/live", response_class=HTMLResponse)
async def page_live_monitor(request: Request) -> HTMLResponse:
    """Live Monitor 페이지."""
    return templates.TemplateResponse(request, "live_monitor.html", {})


# ════════════════════════════════════════
# API Endpoints (JSON)
# ════════════════════════════════════════


@app.get("/api/sessions")
async def api_sessions(limit: int = 50) -> list[dict[str, Any]]:
    return get_store().list_sessions(limit=limit)


@app.get("/api/sessions/{session_id}")
async def api_session_detail(session_id: int) -> dict[str, Any]:
    store = get_store()
    session = store.get_session(session_id)
    if not session:
        return {"error": "not found"}
    return {
        "session": session,
        "round_logs": store.get_round_logs(session_id),
        "metrics": store.get_session_metrics(session_id),
    }


@app.get("/api/agents/stats")
async def api_agent_stats() -> list[dict[str, Any]]:
    return get_store().agent_stats()


# ════════════════════════════════════════
# SSE Endpoint (Live Monitor)
# ════════════════════════════════════════


@app.get("/api/live/stream")
async def sse_live_stream(request: Request) -> EventSourceResponse:
    """SSE 스트림: 라운드 진행 이벤트를 실시간으로 push."""
    queue = broadcaster.subscribe()

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
                    # keepalive ping
                    yield {"event": "ping", "data": ""}
        finally:
            broadcaster.unsubscribe(queue)

    return EventSourceResponse(event_generator())


# ════════════════════════════════════════
# HTMX Partials (부분 렌더링)
# ════════════════════════════════════════


@app.get("/htmx/sessions-table", response_class=HTMLResponse)
async def htmx_sessions_table(request: Request, limit: int = 50) -> HTMLResponse:
    """세션 테이블만 부분 렌더링 (HTMX polling)."""
    store = get_store()
    sessions = store.list_sessions(limit=limit)
    return templates.TemplateResponse(request, "partials/sessions_table.html", {
        "sessions": sessions,
    })


@app.get("/htmx/recent-sessions", response_class=HTMLResponse)
async def htmx_recent_sessions(request: Request) -> HTMLResponse:
    """Live Monitor 사이드바용 최근 세션 목록."""
    store = get_store()
    sessions = store.list_sessions(limit=5)
    html_parts = []
    for s in sessions:
        verdict = s.get("final_verdict") or "running"
        badge_cls = f"badge-{verdict}"
        task = (s.get("task") or "")[:30]
        html_parts.append(
            f'<div style="display:flex;justify-content:space-between;align-items:center;'
            f'margin-bottom:0.5rem;padding-bottom:0.5rem;border-bottom:1px solid var(--surface-high);">'
            f'<div><a href="/session/{s["id"]}" style="color:var(--text-primary);text-decoration:none;"'
            f' class="text-sm">#{s["id"]:03d} {task}</a></div>'
            f'<span class="badge {badge_cls}" style="font-size:0.625rem;">{verdict.upper()}</span>'
            f'</div>'
        )
    return HTMLResponse("".join(html_parts) if html_parts else '<div class="text-muted text-sm">No sessions.</div>')


# ════════════════════════════════════════
# Entry Point
# ════════════════════════════════════════


def main() -> None:
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    uvicorn.run("src.web.app:app", host="127.0.0.1", port=8000, reload=True)


if __name__ == "__main__":
    main()
