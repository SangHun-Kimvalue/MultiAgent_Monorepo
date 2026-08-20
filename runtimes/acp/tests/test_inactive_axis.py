"""tests/test_inactive_axis.py — "정리 대상" 이름-사실 정합 게이트(T14 S5).

잠그는 결함: 화면이 853건을 "정리 대상"이라 부르는데 **사용자가 할 수 있는 정리가 없다**
(대시보드에 정리 기능이 없고, `purge`로 지운 행은 원본이 남아 있으면 재생성된다).
게다가 `stale_ttl=60분`이라 오늘 아침에 끝낸 세션까지 그렇게 불렸다.

검증은 **핵심 사실 4개**로 한정한다 — 이름 하나 바꾸는 변경에 맞는 무게로.
그중 mutation으로 판별력을 실측한 것은 3개이고, 도달 가능성 검사는 회귀 검사다.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from acp.models import SessionRecord, SessionState
from acp.store import SessionStore
from acp.web.app import _INACTIVE_STATES, _sessions_payload


@pytest.fixture()
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(str(tmp_path / "acp.db"))


def test_axis_is_named_for_the_fact_and_old_name_is_a_projection(store: SessionStore) -> None:
    """새 이름이 있고, 옛 이름은 **같은 값의 투영**으로 남는다.

    옛 필드를 지우면 저장소 밖 소비자가 깨진다 — grep으로 없음을 증명할 수 없다.
    """
    store.upsert_session(
        SessionRecord(app="fake", session_id="s1", source_file="t"), SessionState.STALE
    )
    store.upsert_session(
        SessionRecord(app="fake", session_id="s2", source_file="t"), SessionState.HOLDING
    )
    summary = _sessions_payload(store, limit=50)["summary"]

    assert summary["inactive_total"] == 1
    assert summary["inactive_states"] == list(_INACTIVE_STATES)
    # 투영: 두 이름이 같은 계산에서 나온다.
    assert summary["cleanup_required"] == summary["inactive_total"]
    assert summary["cleanup_states"] == summary["inactive_states"]
    # 조치 축은 그대로다(등급 분리는 S2 계약).
    assert summary["action_required"] == 1


def test_inactive_state_is_still_reachable_by_filter(store: SessionStore) -> None:
    """이름을 바꿔도 **도달 가능성**은 그대로다(LESSON-002 rule 8).

    **이것은 회귀 검사이지 mutation 게이트가 아니다**(구현리뷰 P2): 이 변경을 되돌려도
    통과한다. 게이트인 척 세지 않는다.
    """
    store.upsert_session(
        SessionRecord(app="fake", session_id="s1", source_file="t"), SessionState.STALE
    )
    payload = _sessions_payload(store, limit=50, states=["stale"])
    assert [item["session_id"] for item in payload["items"]] == ["fake:s1"]


def test_purge_tells_that_rows_come_back(store: SessionStore, tmp_path: Path, capsys) -> None:
    """`purge`가 **재생성 사실을 실제로 출력**한다(T14 S5 D3).

    수집기는 시간 컷오프 없이 전량을 재스캔하므로, 원본이 남아 있으면 지운 행이 다음
    주기에 돌아온다. 그 사실을 숨기면 사용자는 하지 않은 정리를 했다고 믿는다.

    상수의 **내용만** 검사하면 출력을 지워도 통과한다 — 실제로 그 mutation을 돌려
    확인하고 이 검사를 출력 기준으로 고쳤다.
    """
    from acp.__main__ import _run_purge

    store.upsert_session(
        SessionRecord(app="fake", session_id="s1", source_file="t"), SessionState.STALE
    )
    store.close()
    db = str(tmp_path / "acp.db")

    # 미리보기(삭제 없음)
    assert _run_purge(["--app", "fake", "--db-path", db]) == 0
    preview_out = capsys.readouterr().out
    assert "다시 생성" in preview_out and "재스캔" in preview_out

    # 실제 삭제 경로에서도 같은 고지를 본다(미리보기가 항상 먼저 출력된다 —
    # 완료 줄에 같은 문구를 한 번 더 찍는 것은 중복이라 두지 않았다).
    assert _run_purge(["--app", "fake", "--db-path", db, "--yes"]) == 0
    deleted_out = capsys.readouterr().out
    assert "삭제 완료" in deleted_out
    assert "다시 생성" in deleted_out


def test_no_action_implying_name_remains_in_the_ui() -> None:
    """화면 어디에도 `정리 대상`이 남지 않는다 — 이름이 함의를 만들던 지점이다."""
    web = Path(__file__).resolve().parent.parent / "acp" / "web"
    for name in ("templates/dashboard.html", "static/dashboard.js"):
        text = (web / name).read_text(encoding="utf-8")
        body = "\n".join(
            line for line in text.splitlines()
            if not line.lstrip().startswith(("//", "<!--", "#"))
        )
        assert "정리 대상" not in body, name
