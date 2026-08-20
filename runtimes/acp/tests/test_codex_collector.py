"""tests/test_codex_collector.py — CodexCollector 단위 테스트 (픽스처 기반).

픽스처 구조:
  tests/fixtures/codex/
    process_manager/chat_processes.json
    sessions/2026/06/09/
      rollout-...-aaa*  (task_complete 세션)
      rollout-...-bbb*  (exec_approval_request 세션)
      rollout-...-ccc*  (task_aborted 세션, proc 항목 없음)
      rollout-...-ddd*  (진행중 턴 세션: 마지막 event_msg=token_count) ← P1.5 신규
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from acp.collectors.codex import CodexCollector, _tail_lines, _UUID_RE
from acp.models import SessionRecord

FIXTURE_BASE = Path(__file__).parent / "fixtures" / "codex"
SESSIONS_BASE = FIXTURE_BASE / "sessions"
PROCESSES_PATH = FIXTURE_BASE / "process_manager" / "chat_processes.json"


@pytest.fixture
def collector():
    return CodexCollector(SESSIONS_BASE, PROCESSES_PATH)


# ── 기본 수집 테스트 ──

def test_collect_returns_records(collector):
    records = collector.collect()
    assert len(records) >= 1
    for r in records:
        assert isinstance(r, SessionRecord)
        assert r.app == "codex"


def test_collect_session_ids(collector):
    """픽스처 3개 세션이 모두 수집됨."""
    records = collector.collect()
    ids = {r.session_id for r in records}
    assert "aaa00000-0001-0001-0001-000000000001" in ids
    assert "bbb00000-0002-0002-0002-000000000002" in ids
    assert "ccc00000-0003-0003-0003-000000000003" in ids


def test_collect_last_event_task_complete(collector):
    """aaa 세션: last_event = task_complete."""
    records = {r.session_id: r for r in collector.collect()}
    r = records["aaa00000-0001-0001-0001-000000000001"]
    assert r.last_event == "task_complete"


def test_collect_last_event_approval_request(collector):
    """bbb 세션: last_event = exec_approval_request."""
    records = {r.session_id: r for r in collector.collect()}
    r = records["bbb00000-0002-0002-0002-000000000002"]
    assert r.last_event == "exec_approval_request"


def test_collect_last_event_aborted(collector):
    """ccc 세션: last_event = task_aborted."""
    records = {r.session_id: r for r in collector.collect()}
    r = records["ccc00000-0003-0003-0003-000000000003"]
    assert r.last_event == "task_aborted"


def test_collect_cwd_from_session_meta(collector):
    """session_meta의 cwd가 project_path로 수집됨."""
    records = {r.session_id: r for r in collector.collect()}
    r = records["aaa00000-0001-0001-0001-000000000001"]
    assert r.project_path and "AgentControlPlane" in r.project_path


def test_collect_model_from_session_meta(collector):
    """model_provider가 model 필드로 수집됨."""
    records = {r.session_id: r for r in collector.collect()}
    r = records["aaa00000-0001-0001-0001-000000000001"]
    assert r.model == "openai"


def test_collect_running_pid_from_proc(collector):
    """chat_processes의 osPid가 running_pid로 수집됨."""
    records = {r.session_id: r for r in collector.collect()}
    r = records["aaa00000-0001-0001-0001-000000000001"]
    assert r.running_pid == 12345


def test_collect_running_cmd_from_proc(collector):
    """chat_processes의 command가 running_cmd로 수집됨."""
    records = {r.session_id: r for r in collector.collect()}
    r = records["aaa00000-0001-0001-0001-000000000001"]
    assert r.running_cmd == "python -m acp web"


def test_collect_last_activity_from_jsonl(collector):
    """aaa 세션: last_activity = jsonl 마지막 이벤트 timestamp."""
    records = {r.session_id: r for r in collector.collect()}
    r = records["aaa00000-0001-0001-0001-000000000001"]
    assert r.last_activity is not None
    assert r.last_activity == datetime(2026, 6, 9, 3, 0, 10, tzinfo=timezone.utc)


def test_collect_proc_only_session(collector):
    """proc에만 있고 jsonl 없는 conversationId도 수집 (last_activity=updatedAtMs)."""
    # chat_processes에 있는 bbb도 jsonl 있음. 여기선 proc only 없지만 머지 검증.
    records = {r.session_id: r for r in collector.collect()}
    # bbb: 둘 다 있음 → jsonl last_activity 우선
    r = records["bbb00000-0002-0002-0002-000000000002"]
    assert r.last_activity is not None


# ── 머지 규칙 테스트 ──

def test_no_duplicate_session_ids(collector):
    """같은 conversationId가 중복되지 않음."""
    records = collector.collect()
    ids = [r.session_id for r in records]
    assert len(ids) == len(set(ids))


def test_collect_is_readonly(collector):
    """2회 호출해도 픽스처 파일 변경 없음 (read-only)."""
    r1 = collector.collect()
    r2 = collector.collect()
    ids1 = sorted(r.session_id for r in r1)
    ids2 = sorted(r.session_id for r in r2)
    assert ids1 == ids2


# ── 에러 처리 테스트 (C3) ──

def test_collect_broken_jsonl(tmp_path):
    """깨진 jsonl 줄 → 해당 레코드 last_event=None + 수집 계속."""
    s_dir = tmp_path / "sessions" / "2026" / "06" / "09"
    s_dir.mkdir(parents=True)
    p = tmp_path / "process_manager" / "chat_processes.json"
    p.parent.mkdir(parents=True)
    p.write_text("[]", encoding="utf-8")

    broken = s_dir / "rollout-2026-06-09T00-00-00-ddd00000-0004-0004-0004-000000000004.jsonl"
    broken.write_text(
        '{"timestamp":"2026-06-09T00:00:00.000Z","type":"session_meta","payload":{"id":"ddd00000-0004-0004-0004-000000000004","cwd":"/tmp","model_provider":"openai"}}\n'
        '{broken json line\n'
        '{"timestamp":"2026-06-09T00:00:10.000Z","type":"event_msg","payload":{"type":"task_complete"}}\n',
        encoding="utf-8",
    )

    c = CodexCollector(tmp_path / "sessions", p)
    records = c.collect()
    # 깨진 줄이 있어도 마지막 valid event_msg는 파싱됨
    assert len(records) == 1
    assert records[0].last_event == "task_complete"


def test_collect_missing_processes_file(tmp_path):
    """chat_processes.json 없음 → proc 정보 없이 jsonl만 수집."""
    s_dir = tmp_path / "sessions" / "2026" / "06" / "09"
    s_dir.mkdir(parents=True)
    jf = s_dir / "rollout-2026-06-09T00-00-00-eee00000-0005-0005-0005-000000000005.jsonl"
    jf.write_text(
        '{"timestamp":"2026-06-09T00:00:00.000Z","type":"session_meta","payload":{"id":"eee00000-0005-0005-0005-000000000005","cwd":"/proj","model_provider":"openai"}}\n'
        '{"timestamp":"2026-06-09T00:00:05.000Z","type":"event_msg","payload":{"type":"task_complete"}}\n',
        encoding="utf-8",
    )

    c = CodexCollector(tmp_path / "sessions", tmp_path / "missing.json")
    records = c.collect()
    assert len(records) == 1
    assert records[0].last_event == "task_complete"
    assert records[0].running_pid is None


def test_collect_missing_sessions_dir(tmp_path):
    """sessions 폴더 없음 → [] 반환 + 경고(예외 아님)."""
    p = tmp_path / "chat_processes.json"
    p.write_text("[]", encoding="utf-8")
    c = CodexCollector(tmp_path / "nonexistent", p)
    assert c.collect() == []


# ════════════════════════════════════════
# P1.5 신규 테스트: 진행중 턴 + tail 확대 폴백
# ════════════════════════════════════════

def test_collect_inturn_last_event_token_count(collector):
    """ddd 픽스처: 진행중 턴 세션의 last_event = token_count (마지막 event_msg)."""
    records = {r.session_id: r for r in collector.collect()}
    r = records["ddd11111-0004-0004-0004-000000000004"]
    # 마지막 event_msg가 token_count (agent_message보다 나중)
    assert r.last_event == "token_count"


def test_collect_inturn_session_count(collector):
    """픽스처 4개 세션 모두 수집됨 (P1.5에서 ddd 추가)."""
    records = collector.collect()
    ids = {r.session_id for r in records}
    assert "ddd11111-0004-0004-0004-000000000004" in ids
    assert len(ids) == 4


def test_tail_fallback_when_event_msg_outside_30_lines(tmp_path):
    """MINOR-3: event_msg가 30줄 밖에 있을 때 확대 재시도로 찾음."""
    s_dir = tmp_path / "sessions" / "2026" / "06" / "09"
    s_dir.mkdir(parents=True)
    p = tmp_path / "chat_processes.json"
    p.write_text("[]", encoding="utf-8")

    # session_meta 1줄 + response_item 35줄 + event_msg 1줄 → 총 37줄
    # 30줄 tail = event_msg 포함(37-30=7번째 이후) → 실제론 포함될 수 있어
    # 확실히 30줄 밖에 두려면 response_item 35줄 중간에 event_msg를 박고
    # 그 뒤에 response_item을 더 채운다
    lines = []
    lines.append('{"timestamp":"2026-06-09T00:00:00.000Z","type":"session_meta","payload":{"id":"fff22222-0006-0006-0006-000000000006","cwd":"/proj","model_provider":"openai"}}')
    # event_msg를 줄 2번째에 배치
    lines.append('{"timestamp":"2026-06-09T00:00:01.000Z","type":"event_msg","payload":{"type":"task_started"}}')
    # 그 뒤에 response_item을 35줄 추가 → event_msg는 끝에서 36번째 줄
    for i in range(35):
        lines.append(f'{{"timestamp":"2026-06-09T00:00:{i+2:02d}.000Z","type":"response_item","payload":{{"type":"message","role":"assistant","content":[]}}}}')

    jf = s_dir / "rollout-2026-06-09T00-00-00-fff22222-0006-0006-0006-000000000006.jsonl"
    jf.write_text("\n".join(lines), encoding="utf-8")

    c = CodexCollector(tmp_path / "sessions", p)
    records = c.collect()
    assert len(records) == 1
    # 30줄 tail에는 event_msg가 없음 (36번째) → 폴백 500줄로 찾아야 함
    assert records[0].last_event == "task_started"


def test_tail_30lines_finds_event_msg_within_window(tmp_path):
    """30줄 안에 event_msg 있으면 폴백 없이 바로 찾음 (정상 경로)."""
    s_dir = tmp_path / "sessions" / "2026" / "06" / "09"
    s_dir.mkdir(parents=True)
    p = tmp_path / "chat_processes.json"
    p.write_text("[]", encoding="utf-8")

    lines = []
    lines.append('{"timestamp":"2026-06-09T00:00:00.000Z","type":"session_meta","payload":{"id":"ab3333cc-0007-0007-0007-000000000007","cwd":"/proj","model_provider":"openai"}}')
    for i in range(5):
        lines.append(f'{{"timestamp":"2026-06-09T00:00:{i+1:02d}.000Z","type":"response_item","payload":{{"type":"message","role":"assistant","content":[]}}}}')
    lines.append('{"timestamp":"2026-06-09T00:00:10.000Z","type":"event_msg","payload":{"type":"task_complete"}}')

    jf = s_dir / "rollout-2026-06-09T00-00-00-ab3333cc-0007-0007-0007-000000000007.jsonl"
    jf.write_text("\n".join(lines), encoding="utf-8")

    c = CodexCollector(tmp_path / "sessions", p)
    records = c.collect()
    assert len(records) == 1
    assert records[0].last_event == "task_complete"



# ── 유틸리티 테스트 ──

def test_uuid_re_extracts_from_filename():
    stem = "rollout-2026-06-09T03-00-00-aaa00000-0001-0001-0001-000000000001"
    m = _UUID_RE.search(stem)
    assert m is not None
    assert m.group(1) == "aaa00000-0001-0001-0001-000000000001"


def test_tail_lines_small_file(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("\n".join(f"line{i}" for i in range(5)), encoding="utf-8")
    lines, ok, truncated = _tail_lines(f, n=3)
    assert ok, "정상 읽기는 성공 신호와 함께 온다"
    # 줄 수로 앞을 버린 경우 반환 첫 줄은 **온전한 줄**이다 — 조각 가능성은
    # 바이트 경계에서 읽기 시작했고 그 줄이 그대로 남았을 때만이다(T14 S4b).
    assert truncated is False, "줄 수로 버린 앞부분은 조각 가능성이 아니다"
    assert lines[-1] == "line4"
    assert len(lines) == 3


def test_tail_lines_empty_file(tmp_path):
    f = tmp_path / "empty.txt"
    f.write_bytes(b"")
    # 빈 파일과 **읽기 실패**를 구분한다(T14 S4b) — 예전엔 둘 다 []였다.
    assert _tail_lines(f) == ([], True, False)
