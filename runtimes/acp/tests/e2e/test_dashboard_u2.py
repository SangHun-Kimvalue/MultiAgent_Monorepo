"""U2 dashboard filter/sort e2e tests."""
from __future__ import annotations

import pytest

playwright = pytest.importorskip("playwright.sync_api")
expect = playwright.expect

pytestmark = pytest.mark.e2e


def _open_dashboard(page, acp_base_url: str):
    page.goto(acp_base_url)
    page.wait_for_function("window.__acpDashboardReady === true")
    expect(page.get_by_test_id("filter-band")).to_be_visible()


def _visible_count(page) -> int:
    return int(page.get_by_test_id("visible-count").inner_text())


def test_action_only_toggle_filters_to_action_states(page, acp_base_url):
    _open_dashboard(page, acp_base_url)

    page.get_by_test_id("filter-action-only").click()
    page.wait_for_function("window.__acpDashboard.visibleCount === 3")

    states = page.evaluate("() => window.__acpDashboard.filteredSorted.map((session) => session.state)")
    assert states == ["error", "stale", "holding"] or set(states) == {"holding", "stale", "error"}
    assert _visible_count(page) == 3
    assert int(page.get_by_test_id("kpi-total").inner_text()) == 6


def test_state_and_project_filters_change_visible_rows(page, acp_base_url):
    _open_dashboard(page, acp_base_url)

    page.locator("details").filter(has_text="상태").locator("summary").click()
    page.get_by_test_id("filter-state-holding").click()
    page.wait_for_function("window.__acpDashboard.visibleCount === 1")
    assert _visible_count(page) == 1

    page.get_by_test_id("filter-reset").click()
    page.locator("details").filter(has_text="프로젝트").locator("summary").click()
    first_project = page.evaluate("() => window.__acpDashboard.availableProjects[0]")
    page.locator(f"[data-testid='filter-project-option'][data-filter-project=\"{first_project}\"]").click()
    page.wait_for_function(
        """(project) => window.__acpDashboard.filteredSorted.every((session) => (session.project_path || 'no-project') === project)""",
        arg=first_project,
    )
    assert _visible_count(page) >= 1
    assert int(page.get_by_test_id("kpi-total").inner_text()) == 6


def test_sort_headers_order_visible_groups_without_changing_kpi(page, acp_base_url):
    _open_dashboard(page, acp_base_url)
    total = int(page.get_by_test_id("kpi-total").inner_text())

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
    assert int(page.get_by_test_id("kpi-total").inner_text()) == total

    page.get_by_test_id("sort-updated").click()
    page.wait_for_function("window.__acpDashboard.sortKey === 'last_activity'")
    activity_order_ok = page.evaluate(
        """() => {
            return Array.from(document.querySelectorAll('#sessions-table tbody')).every((group) => {
                const values = Array.from(group.querySelectorAll('[data-testid="session-row"]'))
                    .map((row) => row.dataset.lastActivity || '');
                return values.every((value, index) => index === 0 || values[index - 1] >= value);
            });
        }"""
    )
    assert activity_order_ok is True

    page.get_by_test_id("sort-app").click()
    page.wait_for_function("window.__acpDashboard.sortKey === 'app' && window.__acpDashboard.sortDir === 'asc'")

    page.get_by_test_id("sort-project").click()
    page.wait_for_function("window.__acpDashboard.sortKey === 'project_path' && window.__acpDashboard.sortDir === 'asc'")
