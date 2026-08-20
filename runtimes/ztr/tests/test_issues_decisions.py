"""Phase 8-1 단위 테스트 — Issues + Decisions DB CRUD."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.engine.session_store import SessionStore


@pytest.fixture()
def store(tmp_path: Path) -> SessionStore:
    s = SessionStore(str(tmp_path / "test.db"))
    yield s  # type: ignore[misc]
    s.close()


class TestIssues:
    def test_create_and_get(self, store: SessionStore) -> None:
        store.create_issue("ISSUE-001", "SRP 위반", description="CameraManager", target_file="camera.cpp")
        issue = store.get_issue("ISSUE-001")
        assert issue is not None
        assert issue["title"] == "SRP 위반"
        assert issue["status"] == "open"
        assert issue["target_file"] == "camera.cpp"

    def test_resolve_issue(self, store: SessionStore) -> None:
        store.create_issue("ISSUE-002", "에러 처리 누락")
        store.resolve_issue("ISSUE-002")
        issue = store.get_issue("ISSUE-002")
        assert issue is not None
        assert issue["status"] == "resolved"
        assert issue["resolved_at"] is not None

    def test_list_issues_all(self, store: SessionStore) -> None:
        store.create_issue("I-1", "첫번째")
        store.create_issue("I-2", "두번째")
        store.create_issue("I-3", "세번째")
        store.resolve_issue("I-2")
        assert len(store.list_issues()) == 3

    def test_list_issues_by_status(self, store: SessionStore) -> None:
        store.create_issue("I-1", "열린 이슈")
        store.create_issue("I-2", "해결된 이슈")
        store.resolve_issue("I-2")
        open_issues = store.list_issues(status="open")
        assert len(open_issues) == 1
        assert open_issues[0]["id"] == "I-1"

    def test_get_open_issues_for_file(self, store: SessionStore) -> None:
        store.create_issue("I-1", "A파일 이슈", target_file="src/a.py")
        store.create_issue("I-2", "B파일 이슈", target_file="src/b.py")
        store.create_issue("I-3", "A파일 다른 이슈", target_file="src/a.py")
        store.resolve_issue("I-3")
        a_issues = store.get_open_issues_for_file("src/a.py")
        assert len(a_issues) == 1
        assert a_issues[0]["id"] == "I-1"

    def test_duplicate_issue_ignored(self, store: SessionStore) -> None:
        store.create_issue("DUP-1", "첫번째")
        store.create_issue("DUP-1", "두번째 시도")  # 무시됨
        issue = store.get_issue("DUP-1")
        assert issue is not None
        assert issue["title"] == "첫번째"  # 원본 유지

    def test_get_nonexistent(self, store: SessionStore) -> None:
        assert store.get_issue("NOPE") is None


class TestDecisions:
    def test_add_and_retrieve(self, store: SessionStore) -> None:
        store.create_issue("I-1", "SRP", target_file="camera.cpp")
        sid = store.create_session(task="리뷰")
        did = store.add_decision(
            decision="MonitorWorker를 CameraHealthMonitor로 분리",
            issue_id="I-1",
            session_id=sid,
            reasoning="SRP 위반 해결",
            decided_by="writer=claude, critic=gemini",
        )
        assert did >= 1

        decisions = store.get_decisions_for_file("camera.cpp")
        assert len(decisions) == 1
        assert "MonitorWorker" in decisions[0]["decision"]
        assert decisions[0]["issue_title"] == "SRP"

    def test_recent_decisions(self, store: SessionStore) -> None:
        for i in range(5):
            store.add_decision(decision=f"결정 {i}")
        recent = store.get_recent_decisions(limit=3)
        assert len(recent) == 3

    def test_decision_without_issue(self, store: SessionStore) -> None:
        """이슈 없이도 결정 기록 가능."""
        did = store.add_decision(decision="전역 결정")
        assert did >= 1
        recent = store.get_recent_decisions()
        assert any("전역" in d["decision"] for d in recent)

    def test_decisions_empty_file(self, store: SessionStore) -> None:
        assert store.get_decisions_for_file("nonexistent.py") == []
