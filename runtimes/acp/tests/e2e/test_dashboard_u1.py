"""U1 dashboard e2e tests."""
from __future__ import annotations

from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")
expect = playwright.expect

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_DIR = ROOT / ".acp"


def _open_dashboard(page, acp_base_url: str):
    page.goto(acp_base_url)
    page.wait_for_function("window.__acpDashboardReady === true")
    expect(page.get_by_test_id("kpi-strip")).to_be_visible()


def _kpi_state_count(page, state: str) -> int:
    return int(page.locator(f"[data-testid='kpi-state-{state}'] strong").inner_text())


def test_dashboard_renders_kpi_and_session_table(page, acp_base_url):
    _open_dashboard(page, acp_base_url)

    expect(page.get_by_test_id("sessions-table")).to_be_visible()
    expect(page.get_by_test_id("kpi-total")).not_to_have_text("0")
    expect(page.locator("[data-testid='session-row']")).to_have_count(6)

    ARTIFACT_DIR.mkdir(exist_ok=True)
    page.screenshot(path=str(ARTIFACT_DIR / "u1-e2e-dashboard.png"), full_page=True)


def test_kpi_counts_match_server_summary(page, acp_base_url):
    """KPI는 표시 창이 아니라 **서버 전량 집계**와 일치해야 한다.

    과거 이 테스트는 KPI가 잘린 창 카운트와 같은지를 확인해, 거짓 총계를
    정상 동작으로 고정하고 있었다(전체 1005건이 "전체 100"으로 보고됨).
    """
    _open_dashboard(page, acp_base_url)
    payload = page.request.get(f"{acp_base_url}/api/sessions").json()
    summary = payload["summary"]

    assert int(page.get_by_test_id("kpi-total").inner_text()) == summary["total"]
    assert int(page.get_by_test_id("kpi-apps").inner_text()) == len(summary["by_app"])
    assert int(page.get_by_test_id("kpi-projects").inner_text()) == summary["projects"]
    assert int(page.get_by_test_id("kpi-action-required").inner_text()) == summary["action_required"]
    for state in ("holding", "stale", "error", "live", "running", "idle", "done", "unknown"):
        assert _kpi_state_count(page, state) == summary["by_state"].get(state, 0)


def test_truncation_is_disclosed_when_window_cuts(page, acp_base_url):
    """창이 잘리면 그 사실이 화면에 표시돼야 한다(조용한 절단 금지)."""
    _open_dashboard(page, acp_base_url)
    payload = page.request.get(f"{acp_base_url}/api/sessions").json()

    note = page.get_by_test_id("truncation-note")
    if payload["truncated"]:
        expect(note).to_be_visible()
        assert str(payload["total"]) in note.inner_text()
    else:
        expect(note).not_to_be_visible()


def test_state_delta_updates_row_badge_and_schedules_authoritative_refresh(page, acp_base_url):
    """SSE 전이는 행 배지를 즉시 바꾸고, KPI는 **서버 재조회**로 갱신한다.

    과거 이 테스트는 클라가 KPI 카운트를 직접 가감하길 기대했다. 그 산술은
    중복·누락 이벤트에서 총계를 드리프트시켜 거짓 집계를 만들 수 있으므로
    제거했고(리뷰 P2), KPI 권위는 서버 스냅샷 하나로 일원화했다.
    """
    _open_dashboard(page, acp_base_url)
    before = page.evaluate(
        """() => {
            const app = window.__acpDashboard;
            const target = app.sessionList.find((session) => session.state !== 'error');
            return { id: target.session_id, previous: target.state };
        }"""
    )

    page.evaluate(
        """(payload) => {
            const app = window.__acpDashboard;
            const current = app.sessions[payload.id];
            app.applyStateChange({
                session_id: payload.id,
                native_session_id: current.native_session_id,
                app: current.app,
                project_path: current.project_path,
                state: 'error',
            });
        }""",
        before,
    )

    expect(page.locator(f"[data-session-id=\"{before['id']}\"] [data-testid='state-badge']")).to_have_text("error")

    # KPI가 서버 집계와 다시 일치할 때까지 기다린다.
    # (`summaryRefreshTimer === null`은 전이 이전에도 참이고 콜백 진입 즉시 참이 되므로
    #  "재조회가 끝났다"의 증거가 못 된다 — 아래 수렴 대기가 실질 검증이다.)
    payload = page.request.get(f"{acp_base_url}/api/sessions").json()
    page.wait_for_function(
        """(expected) => {
            const app = window.__acpDashboard;
            return app.kpis.total === expected.total
                && app.kpis.actionRequired === expected.action_required;
        }""",
        arg={"total": payload["total"], "action_required": payload["summary"]["action_required"]},
    )
