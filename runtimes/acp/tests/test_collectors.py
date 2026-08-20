"""tests/test_collectors.py — FakeCollector 스키마 검증."""
from __future__ import annotations

from acp.collectors.fake import FakeCollector
from acp.models import SessionRecord


def test_fake_collector_returns_records():
    records = FakeCollector().collect()
    assert len(records) >= 1
    for r in records:
        assert isinstance(r, SessionRecord)


def test_fake_collector_schema_valid():
    """모든 레코드가 Pydantic v2 검증 통과 + app 필드 일치."""
    records = FakeCollector().collect()
    for r in records:
        assert r.app == "fake"
        assert r.session_id  # 비어있으면 안 됨


def test_fake_collector_has_mixed_states():
    """LIVE(활동최근)/IDLE(활동중간)/HOLDING후보(PID없음)/UNKNOWN(활동없음) 픽스처 포함 확인."""
    records = FakeCollector().collect()
    session_ids = [r.session_id for r in records]
    assert "fake-live-001" in session_ids
    assert "fake-idle-002" in session_ids
    assert "fake-holding-003" in session_ids
    assert "fake-unknown-004" in session_ids


def test_fake_collector_is_readonly():
    """collect() 가 외부 상태를 변경하지 않음 (2회 호출 결과 동일)."""
    c = FakeCollector()
    r1 = c.collect()
    r2 = c.collect()
    assert [x.session_id for x in r1] == [x.session_id for x in r2]


def test_fake_collector_app_name():
    assert FakeCollector().app_name == "fake"
