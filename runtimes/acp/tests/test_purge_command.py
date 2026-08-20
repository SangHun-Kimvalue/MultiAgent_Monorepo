"""tests/test_purge_command.py — 일회성 정리 명령 안전 계약 (T14 S3 D4).

삭제는 비가역이다. 미리보기가 기본이고, 명시적 승인 없이는 아무것도 지우지 않으며,
지운 사실은 감사 이벤트로 남는다.
"""
from __future__ import annotations

from acp.models import SessionRecord, SessionState


def _seed(store, app, count):
    for i in range(count):
        store.upsert_session(
            SessionRecord(app=app, session_id=f"{app}-{i}", source_file="t"),
            SessionState.LIVE,
        )


def test_preview_does_not_delete(tmp_store):
    _seed(tmp_store, "fake", 3)
    _seed(tmp_store, "claude", 2)

    preview = tmp_store.preview_app_rows("fake")

    assert preview["count"] == 3
    assert tmp_store.count_sessions() == 5, "미리보기는 아무것도 지우지 않는다"


def test_purge_requires_explicit_confirmation(tmp_store):
    _seed(tmp_store, "fake", 3)

    assert tmp_store.purge_app_rows("fake", confirmed=False) == 0
    assert tmp_store.count_sessions() == 3, "승인 없이는 삭제 금지"


def test_purge_is_scoped_to_exact_app_and_audited(tmp_store):
    _seed(tmp_store, "fake", 3)
    _seed(tmp_store, "claude", 2)

    deleted = tmp_store.purge_app_rows("fake", confirmed=True)

    assert deleted == 3
    assert tmp_store.count_sessions() == 2, "다른 앱 행은 건드리지 않는다"
    assert [row["app"] for row in tmp_store.list_sessions()] == ["claude", "claude"]

    audit = tmp_store.list_events(event_type="rows_purged")
    assert len(audit) == 1
    assert audit[0]["payload"]["deleted"] == 3


def test_cli_preview_is_default_and_yes_gates_deletion(tmp_path, capsys):
    """CLI 경로로 잠근다 — 저장소 메서드만 테스트하면 승인 경계가 검증되지 않는다."""
    from acp.__main__ import _run_purge
    from acp.store import SessionStore

    db = str(tmp_path / "t.db")
    log = str(tmp_path / "t.jsonl")
    store = SessionStore(db, log)
    _seed(store, "fake", 4)
    store.close()

    # 기본은 미리보기 — 삭제하지 않는다.
    assert _run_purge(["--app", "fake", "--db-path", db]) == 0
    out = capsys.readouterr().out
    assert "4행" in out and "--yes" in out

    check = SessionStore(db, log)
    assert check.count_sessions() == 4, "미리보기가 지우면 안 된다"
    check.close()

    # --yes가 있어야 실제 삭제.
    assert _run_purge(["--app", "fake", "--db-path", db, "--yes"]) == 0
    after = SessionStore(db, log)
    assert after.count_sessions() == 0
    audit = after.list_events(event_type="rows_purged")
    assert audit[0]["payload"] == {"app": "fake", "deleted": 4, "previewed": 4}
    after.close()


def test_cli_requires_app_argument(capsys):
    from acp.__main__ import _run_purge

    assert _run_purge([]) == 2
    assert "--app" in capsys.readouterr().err
