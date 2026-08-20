"""acp/web/app.py — FastAPI 대시보드.

ZTR src/web/app.py의 SSE broadcaster 패턴(lines 44-66, 192-214)을 이식.
엔드포인트:
  GET /                   → Jinja2 대시보드 (세션 테이블)
  GET /api/sessions       → {items, returned, total, limit, truncated, summary}
                            (리스트가 아니라 봉투 — 받은 건수를 전체로 오해하지
                             않도록 총계·절단 사실을 함께 싣는다)
  GET /api/live/stream    → SSE 실시간 스트림 (30s keepalive ping)
"""
from __future__ import annotations

import asyncio
import hashlib
from collections import defaultdict
import json
import logging
from pathlib import Path
from collections.abc import AsyncIterator, Sequence
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
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
from acp.models import state_vocabulary
from acp.collectors.base import (
    CAPABILITY_UNKNOWN,
    PROCESS_SIGNAL_UNKNOWN,
    all_unknown_scopes,
)
from acp.evidence import evidence_kind
from acp.notify_view import notification_view
from acp.store import (
    ARCHIVED_SCOPE_DEFAULT,
    ARCHIVED_SCOPES,
    STATE_DISTRIBUTION_SCOPE,
    SessionStore,
    UnknownStateFilter,
)

logger = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _WEB_DIR / "templates"
_STATIC_DIR = _WEB_DIR / "static"

app = FastAPI(title="Agent Control Plane", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _asset_version() -> str:
    """정적 자원 캐시 무효화 토큰(T14 S4a).

    실측한 결함: 자원 URL이 고정이라 기존 사용자의 브라우저가 **새 HTML + 낡은 JS**를
    받는다. 그 조합은 어느 쪽 버전보다도 나쁘다 — 템플릿이 새 필드를 참조하는데 옛
    스토어에는 없어 화면에 `undefined`가 찍히고, KPI가 창 파생 값으로 퇴행했다
    (라이브에서 `전체 300`·`보관 undefined건` 재현).

    의미가 바뀌는 수와 그 설명을 같은 배포로 묶겠다는 계약(D8)은, 브라우저가 옛 자원을
    계속 쓰면 지켜지지 않는다. 파일 내용이 바뀌면 URL도 바뀌게 한다.
    """
    digest = hashlib.sha256()
    for name in sorted(("dashboard.js", "style.css")):
        path = _STATIC_DIR / name
        try:
            digest.update(path.read_bytes())
        except OSError:
            # 자원을 못 읽어도 페이지는 떠야 한다. 이 경우 토큰은 파일명 기반의
            # **고정값**이 되므로 그 자원에 대해서는 캐시가 계속 적중한다 — 읽기
            # 실패를 무효화로 승격시키지 않는다(매 요청 토큰이 바뀌면 정상 자원까지
            # 매번 다시 받게 된다). 대신 사실을 로그로 남긴다.
            logger.warning("정적 자원 버전 계산 실패(캐시 토큰 고정): %s", name)
            digest.update(name.encode("utf-8"))
    return digest.hexdigest()[:12]

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


# ── 세션 조회 창 계약 ──
#
# 표시 창(page size)과 방어 상한을 상수로 고정한다. 과거에는 서버 기본값 100과
# 대시보드 하드코딩 300이 서로 달라 "무엇이 전체인가"가 계층마다 어긋났다.
_SESSION_PAGE_SIZE = 300
_SESSION_LIMIT_MAX = 5000

# 상태를 두 축으로 나눈다(T14 S2 D4). 한 축으로 뭉치면 "확인 필요 656건"의 95%가
# 30일 전 죽은 세션이 되어 배지 자체가 무의미해진다.
#   action  = 지금 사람이 손대야 함
#   cleanup = 사실상 종료 — 정리 대상(알림 축 아님)
# 클라이언트는 두 목록을 **응답에서 받아** 쓴다. 양쪽에 각자 정의하면 드리프트 시
# "KPI는 N인데 필터 결과는 다른 집합"이 된다.
_ACTION_STATES = ("holding", "error")
# 비활성 축(T14 S5에서 `_INACTIVE_STATES`에서 개명). "치워야 함"이 아니라
# "임계 시간 동안 활동이 없음"이라는 **사실**을 뜻한다.
_INACTIVE_STATES = ("stale",)

# 앱별 **신호 품질**(T14 S3 D5). 같은 표에 다른 종류의 신호가 섞여 있다는 사실을
# 데이터로 노출한다 — cursor의 last_activity는 세션 활동이 아니라 워크스페이스를
# 마지막으로 연 시각(state.vscdb mtime)이다.
# **소유권은 수집기**에 있다(사이클에 선언값이 실린다). 아래는 아직 선언을 싣지 않은
# 수집기를 위한 폴백일 뿐, 사이클 값이 있으면 그것이 이긴다.
# 사이클 기록이 없는 앱의 상태. "앱을 관측하지 않았다"가 아니라 "이 API가 보는
# collector_cycles에 기록이 없다"는 뜻이므로 이름이 그 범위를 말한다(T14 S4b D1).
_NO_CYCLE_RECORD = "no_cycle_record"
_OBSERVATION_SCOPE = "collector_cycle"
# 저장 축 → **응답 어휘**. API는 한 어휘만 쓴다 — 저장 축 이름을 그대로 내보내면
# S4a에서 폐기한 `excluded_archived`가 `count_scopes` 키로 되살아난다(구현 중 실측).
_AXIS_TO_API = {
    "collected": "collected",
    "failed": "failed",
    "excluded_archived": "archived_seen",
    "matched": "matched",
    "unmatched": "unmatched",
    "ambiguous": "ambiguous",
    "malformed_uuid": "malformed_uuid",
}


def _api_scopes(scopes: dict[str, str]) -> dict[str, str]:
    """저장 축 키를 응답 어휘로 투영한다."""
    return {_AXIS_TO_API[axis]: scope for axis, scope in scopes.items() if axis in _AXIS_TO_API}

_APP_SIGNAL_FALLBACK = {
    "codex": "session-events",
    "claude": "session-file",
    "cursor": "workspace-history",
    "fake": "synthetic",
}

# 화면 표시 순서 — **제품 정책**이며 계약 어휘와 다른 축이다(T14 S4c-2 D1).
# 어휘는 `SessionState`가, 수용 여부는 store가 정한다. 여기서는 순서만 말한다.
# 예전에는 클라의 `ACP_STATES` 배열 하나가 어휘·순서·필터 후보를 겸했고, 그래서
# 순서를 바꾸면 필터 후보가 바뀌고 어휘 밖 상태는 화면에서 아예 사라졌다.
STATE_DISPLAY_ORDER: tuple[str, ...] = (
    "holding", "stale", "error", "live", "running", "idle", "done", "unknown",
)

# 순서는 어휘의 **순열**이어야 한다. 어긋난 채 기동하면 상태 하나가 조용히 화면에서
# 빠지거나 없는 상태가 표시된다 — 드리프트를 런타임까지 끌고 가지 않고 여기서 죽는다.
_VOCABULARY = state_vocabulary()
if sorted(STATE_DISPLAY_ORDER) != sorted(_VOCABULARY):
    raise RuntimeError(
        "STATE_DISPLAY_ORDER가 상태 어휘의 순열이 아니다: "
        f"순서={sorted(STATE_DISPLAY_ORDER)} 어휘={sorted(_VOCABULARY)}"
    )

# 상태 필터 거절을 **구조화**해 알린다(T14 S4c-2 D5). 산문 `detail`만 주면 소비자가
# 어떤 값이 거절됐는지 알기 위해 문장을 파싱해야 한다 — R5 위반을 소비자에게 강요하는 꼴이다.
_ERROR_UNKNOWN_STATE_FILTER = "unknown_state_filter"


def _sessions_payload(
    store: SessionStore,
    *,
    limit: int,
    states: Sequence[str] | None = None,
    apps: Sequence[str] | None = None,
    archived: str = ARCHIVED_SCOPE_DEFAULT,
) -> dict[str, Any]:
    """세션 창 + 전량 집계 + 절단 사실을 한 봉투로 묶는다.

    창·집계는 store가 **단일 read 스냅샷**으로 계산한다(리뷰 P2: 따로 조회하면
    그 사이 폴러 upsert가 끼어 returned/total/by_state가 서로 다른 시점을 가리킴).
    여기서는 그 스냅샷에 대한 순수 산술만 덧붙이므로 새 레이스를 만들지 않는다.
    """
    payload = store.sessions_view(limit=limit, states=states, apps=apps, archived=archived)
    # 실행 증거의 **종류**를 서버가 판별해 싣는다(T14 S4c-1 D3). UI가 `running_cmd`의
    # sentinel을 직접 해석하면 그 필드에 두 의미가 계속 남고, 내부 마커가 하나 늘 때마다
    # 같은 결함이 반복된다. 저장 스키마는 그대로 두고 여기서 투영만 한다.
    for item in payload["items"]:
        item["execution_evidence_kind"] = evidence_kind(item.get("running_cmd"))
    by_state: dict[str, int] = payload["summary"]["by_state"]
    payload["summary"]["action_required"] = sum(
        by_state.get(state, 0) for state in _ACTION_STATES
    )
    payload["summary"]["action_states"] = list(_ACTION_STATES)
    # 축 이름을 **판정이 증명하는 만큼만** 말한다(T14 S5). 옛 이름 `cleanup_*`은 그 축이
    # 무엇인지 말하지 않고 **무엇을 하라고** 말했는데, 정작 사용자가 할 수 있는 정리가 없다
    # (대시보드에 정리 기능이 없고, `purge`로 지운 행은 원본이 남아 있으면 재생성된다).
    # 이 판정이 증명하는 것은 "임계 시간 동안 활동이 없었고 이번 사이클에 실행 신호를
    # 관측했다"까지다 — 종료를 본 것이 아니며(`--resume`으로 이어갈 수 있다) 치우라는
    # 뜻도 아니다. 그래서 **비활성**이다.
    inactive_total = sum(by_state.get(state, 0) for state in _INACTIVE_STATES)
    payload["summary"]["inactive_total"] = inactive_total
    payload["summary"]["inactive_states"] = list(_INACTIVE_STATES)
    # 옛 이름은 **같은 계산의 투영**으로 남긴다(S4b `app_signals` 패턴). `/api/sessions`는
    # 공개 응답이고, 저장소 grep은 저장소 밖 소비자가 없음을 증명하지 못한다.
    payload["summary"]["cleanup_required"] = inactive_total
    payload["summary"]["cleanup_states"] = list(_INACTIVE_STATES)
    # action/cleanup은 `by_state`에서 파생되고, `by_state`는 보관 미관측 행의 분포다
    # (T14 S4a D7). 따라서 두 수는 **보관 세션을 세지 않는다** — 이미 정리한 세션을
    # "조치 필요"라 부르지 않기 위해서다. 좁힌 범위를 이름으로 말한다(D5).
    payload["summary"]["action_scope"] = STATE_DISTRIBUTION_SCOPE
    # 표시 순서는 제품 정책이므로 여기서 싣는다. 어휘(`state_vocabulary`)와 실제 존재
    # (`observed_states`)는 store가 **같은 스냅샷에서** 실었다(T14 S4c-2 D1·D7).
    payload["summary"]["state_display_order"] = list(STATE_DISPLAY_ORDER)
    # 수집 사이클 사실(T14 S3): 앱별 신호 품질·수집 건강도.
    # 사이클은 세션과 **같은 read 스냅샷**에서 왔다(S4a D4) — 별도 조회하지 않는다.
    cycles: dict[str, dict[str, Any]] = payload.pop("cycles", {})
    # 이름을 사실에 맞춘다(T14 S4a D1): 이 수는 "총계에서 빠진 행 수"가 아니라
    # **앱별 최신 스캔이 본 archived 파일 수의 합**이다. 옛 이름 `excluded_archived`는
    # 빠지지도 않은 행을 빠졌다고 읽히게 만들었다. 총계에서 실제로 갈라진 수는
    # `archived_total`이며, 두 축을 절대 더하지 않는다.
    payload["summary"]["archived_seen_in_latest_cycle"] = sum(
        int(row.get("excluded_archived") or 0) for row in cycles.values()
    )
    # 건강도 키는 **세션의 앱 ∪ 사이클의 앱**이다(T14 S4b D1). 사이클 있는 앱만 담으면
    # 관측된 적 없는 앱이 응답에서 사라지고, 화면에서 "없음"은 "건강함"과 구분되지 않는다.
    by_app: dict[str, int] = payload["summary"]["by_app"]
    health: dict[str, dict[str, Any]] = {}
    for app in sorted(set(by_app) | set(cycles)):
        row = cycles.get(app)
        session_count = int(by_app.get(app, 0))
        if row is None:
            # 침묵 대신 이름. 세션 723건인데 관측 0회인 상태가 한 눈에 보여야 한다.
            health[app] = {
                "status": _NO_CYCLE_RECORD,
                "observation_scope": _OBSERVATION_SCOPE,
                "session_count": session_count,
                "source": None,
                "observed_at": None,
                "count_scopes": _api_scopes(all_unknown_scopes()),
                **{name: None for name in _AXIS_TO_API.values()},
                # 사이클이 없으면 두 축 모두 **모른다**. `null`로 두면 소비자가 각자
                # 기본값을 지어내므로, 모른다는 사실을 이름으로 말한다(T14 S4c-1 D1·D1b).
                "process_signal": PROCESS_SIGNAL_UNKNOWN,
                "process_signal_capability": CAPABILITY_UNKNOWN,
            }
            continue
        scopes = row.get("count_scopes") or all_unknown_scopes()
        health[app] = {
            "status": row.get("status"),
            "observation_scope": _OBSERVATION_SCOPE,
            "session_count": session_count,
            "source": row.get("source"),
            "observed_at": row.get("observed_at"),
            # 값은 store가 정규화했다(scope != declared → None). 여기서 다시 해석하지 않는다.
            **{name: row.get(axis) for axis, name in _AXIS_TO_API.items()},
            "count_scopes": _api_scopes(scopes),
            # 두 축을 **함께** 싣는다(T14 S4c-1 D1b). 하나만 내보내면 "줄 수 없는 앱"과
            # "이번엔 못 얻은 앱"이 화면에서 같아진다. 값은 store가 정규화했다.
            "process_signal": row.get("process_signal"),
            "process_signal_capability": row.get("process_signal_capability"),
        }
    payload["summary"]["collector_health"] = health

    # 신호 품질: **값과 출처를 함께** 싣는다(T14 S4b D3). 하드코딩 폴백을 권위에서
    # 강등하되 옛 필드의 형태는 유지한다 — `/api/sessions`는 공개 응답이고, 저장소 grep은
    # 저장소 밖 소비자가 없음을 증명하지 못한다.
    details: dict[str, dict[str, Any]] = {}
    for app in sorted(set(by_app) | set(cycles) | set(_APP_SIGNAL_FALLBACK)):
        declared = (cycles.get(app) or {}).get("signal_quality")
        if declared:
            details[app] = {"value": str(declared), "source": "declared"}
        elif app in _APP_SIGNAL_FALLBACK:
            details[app] = {"value": _APP_SIGNAL_FALLBACK[app], "source": "fallback"}
        else:
            # 폴백조차 없다. 지어내지 않는다.
            details[app] = {"value": None, "source": "unknown"}
    payload["summary"]["app_signal_details"] = details
    # 옛 필드는 상세의 **투영**이다. 따로 만들면 두 진실이 생겨 드리프트한다.
    payload["summary"]["app_signals"] = {
        app: entry["value"] for app, entry in details.items() if entry["value"] is not None
    }
    return payload


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
def page_dashboard(request: Request) -> HTMLResponse:
    store = get_store()
    payload = _sessions_payload(store, limit=_SESSION_PAGE_SIZE)
    sessions = payload["items"]
    grouped_sessions = _group_sessions(sessions)
    # 화면은 `to`·`title`·`detail`을 **최상위**에서 읽는다. 저장 행은 그것을
    # `payload` 안에 두므로 같은 모양으로 펴서 내려준다(T14 S6 D1) — SSE 경로와
    # 같은 계약이다. 예전에는 이 경로만 중첩이라 "최근 알림"이 전부 `-`로 떴다.
    notifications = [
        notification_view(row)
        for row in store.list_events(limit=10, event_type="notification_sent")
    ]
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "sessions": sessions,
            "grouped_sessions": grouped_sessions,
            "notifications": notifications,
            "poll_interval": _poll_interval,
            "session_meta": {k: v for k, v in payload.items() if k != "items"},
            "asset_version": _asset_version(),
            "title": "Agent Control Plane",
        },
    )


# ════════════════════════════════════════
# JSON API
# ════════════════════════════════════════

@app.get("/api/sessions")
def api_sessions(
    limit: int = Query(default=_SESSION_PAGE_SIZE, ge=1, le=_SESSION_LIMIT_MAX),
    states: list[str] | None = Query(default=None),
    apps: list[str] | None = Query(default=None),
    archived: str = Query(default=ARCHIVED_SCOPE_DEFAULT),
) -> dict[str, Any]:
    """세션 창 + 전량 기준 집계.

    리스트를 그냥 돌려주면 호출자가 "받은 건수 = 전체"로 오해한다(과거 KPI가
    정확히 그렇게 거짓 총계를 만들었다). 창·총계·절단 여부를 한 응답에 함께
    담아 절단 사실이 경계를 넘어가도록 한다.

    `states`/`apps` 필터는 **서버에서** 전량에 적용한다. 클라이언트가 창 안에서만
    거르면 `last_activity` 정렬과 반상관인 `stale`/`holding`이 창 밖에 남아
    "KPI는 652건이라는데 표에는 없다"가 된다(리뷰 P1).

    `archived`(`include`|`exclude`|`only`)는 `items`/`matched`/`truncated`의 집합을
    정한다. 기본은 `include` — 목록에서 무엇을 뺄지는 표시 정책이므로 API 기본값을
    조용히 바꾸지 않는다(T14 S4a D3). 응답의 `archived_scope`가 적용된 범위를 밝힌다.

    `def`(비동기 아님)로 두어 FastAPI가 threadpool에서 실행한다 — 동기 SQLite
    조회가 이벤트 루프를 막아 SSE·폴러까지 함께 멈추는 것을 피한다.
    """
    if archived not in ARCHIVED_SCOPES:
        # 모르는 값을 조용히 기본값으로 삼키면 요청한 범위와 응답이 달라진다.
        raise HTTPException(
            status_code=422,
            detail=f"알 수 없는 archived 범위: {archived!r} (허용: {list(ARCHIVED_SCOPES)})",
        )
    try:
        # 상태 수용 여부는 **저장소 스냅샷에 의존하는 도메인 판정**이라 store가 한다
        # (T14 S4c-2 D7). 여기서는 그 결과를 HTTP 어휘로 옮기기만 한다.
        return _sessions_payload(
            get_store(), limit=limit, states=states, apps=apps, archived=archived
        )
    except UnknownStateFilter as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": _ERROR_UNKNOWN_STATE_FILTER,
                "invalid_values": exc.invalid_values,
                "accepted": exc.accepted,
            },
        ) from exc


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
