"""V1 full dashboard integration e2e."""
from __future__ import annotations

from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")
expect = playwright.expect

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_DIR = ROOT / ".acp"


def test_full_dashboard_flow_state_notification_filter_sort(page, acp_transition_base_url):
    page.goto(acp_transition_base_url)
    page.wait_for_function("window.__acpDashboardReady === true")
    page.wait_for_function("window.__acpDashboard.streamConnected === true")
    expect(page.get_by_test_id("kpi-total")).to_have_text("6")

    before = page.evaluate(
        """() => {
            const app = window.__acpDashboard;
            const target = app.sessionList.find((session) => session.state === 'live');
            return {
                id: target.session_id,
                native: target.native_session_id,
                appName: target.app,
                project: target.project_path,
                actionBefore: app.kpis.actionRequired,
                holdingBefore: app.kpis.counts.holding,
            };
        }"""
    )

    page.wait_for_function(
        """(payload) => {
            const app = window.__acpDashboard;
            const target = app.sessions[payload.id];
            const notified = app.notifications.some((item) => item.session_id === payload.id && item.to === 'holding');
            // KPI는 클라 가감이 아니라 서버 재조회로 갱신되므로(debounce),
            // 최종 수렴값만 요구한다.
            return target && target.state === 'holding'
                && notified
                && app.kpis.actionRequired === payload.actionBefore + 1
                && app.kpis.counts.holding === payload.holdingBefore + 1;
        }""",
        arg=before,
        timeout=12000,
    )
    expect(page.get_by_test_id("notifications-list")).to_contain_text("세션 홀딩 감지")

    page.get_by_test_id("filter-action-only").click()
    # "행동 필요만"은 이제 **서버 필터**다(창 안에서만 거르면 stale/holding이
    # 활동순 창 밖에 남아 도달 불가). 창이 조건 일치 전량을 덮을 때에만
    # 표시 건수와 KPI가 같으므로, 절단 여부로 갈라 검증한다.
    page.wait_for_function(
        """() => {
            const app = window.__acpDashboard;
            return app.sessionMeta.filtered === true
                && (app.sessionMeta.truncated
                    ? app.visibleCount <= app.kpis.actionRequired
                    : app.visibleCount === app.kpis.actionRequired);
        }"""
    )
    if not page.evaluate("() => window.__acpDashboard.sessionMeta.truncated"):
        assert int(page.get_by_test_id("visible-count").inner_text()) == int(
            page.get_by_test_id("kpi-action-required").inner_text()
        )

    page.get_by_test_id("sort-state").click()
    page.wait_for_function("window.__acpDashboard.sortKey === 'state'")
    severities_ok = page.evaluate(
        """() => {
            const order = { error: 0, stale: 1, holding: 2, idle: 3, running: 4, live: 5, done: 6, unknown: 7 };
            return Array.from(document.querySelectorAll('#sessions-table tbody')).every((group) => {
                const values = Array.from(group.querySelectorAll('[data-testid="session-row"]'))
                    .map((row) => order[row.dataset.state] ?? 99);
                return values.every((value, index) => index === 0 || values[index - 1] <= value);
            });
        }"""
    )
    assert severities_ok is True

    ARTIFACT_DIR.mkdir(exist_ok=True)
    page.screenshot(path=str(ARTIFACT_DIR / "v1-e2e-full-flow.png"), full_page=True)
