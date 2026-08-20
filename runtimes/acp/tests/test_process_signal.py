"""tests/test_process_signal.py — 실행 신호 수집 계약 (T14 S3 D1·D2).

잠그는 결함:
  - Claude `running_pid`가 항상 None이라 실행 중 세션이 시간만으로 IDLE/STALE로 내려간 것
  - 관측 실패를 "빈 결과"로 축약해 "세션 없음"과 구분되지 않던 것
  - 실패 시 PID를 이월하면 PID 재사용으로 **거짓 RUNNING**이 되는 것
  - 커맨드라인 원문(토큰·프롬프트·경로)이 레코드/DB로 새어 나가는 것
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from acp.collectors.claude import ClaudeSessionCollector
from acp.config import LivenessConfig
from acp.liveness import derive_state
from acp.models import SessionState
from acp.proc import (
    CliProcess,
    ProcessSnapshot,
    ProcessSnapshotCache,
    SnapshotStatus,
    parse_cli_process,
    split_command_line,
)

NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
SESSION_UUID = "33c7836b-d39b-4b0e-b153-92b5f2103c58"


def _write_session(base, name, *, cli_session_id=None, archived=False, model=None, ms=None):
    folder = base / "acct" / "ws"
    folder.mkdir(parents=True, exist_ok=True)
    payload = {
        "sessionId": name,
        "cwd": "C:/proj",
        "lastActivityAt": ms if ms is not None else int(NOW.timestamp() * 1000),
    }
    if cli_session_id:
        payload["cliSessionId"] = cli_session_id
    if archived:
        payload["isArchived"] = True
    if model:
        payload["model"] = model
    (folder / f"local_{name}.json").write_text(json.dumps(payload), encoding="utf-8")


def _snapshot(*processes, status=SnapshotStatus.OK, observed_at=None):
    # 관측 시각은 **실제 now**여야 freshness 계약을 통과한다(수집기가 소비 시점에 검사).
    return ProcessSnapshot(
        status, observed_at or datetime.now(timezone.utc), tuple(processes)
    )


# ── CLI 문법 (D2) ──

@pytest.mark.parametrize(
    "command_line",
    [
        f"claude --resume={SESSION_UUID}",
        f"claude --resume {SESSION_UUID}",
        f"claude -r {SESSION_UUID}",
        f'"C:\\Program Files\\claude.exe" --resume {SESSION_UUID}',
        f"claude --resume={SESSION_UUID.upper()}",
    ],
)
def test_resume_flag_forms(command_line):
    """정규식 하나로 긁으면 따옴표·형식 변형에서 깨진다 — argv 분해 후 exact token."""
    assert parse_cli_process(1, command_line).session_uuid == SESSION_UUID


def test_malformed_uuid_is_rejected():
    assert parse_cli_process(1, "claude --resume=not-a-uuid").session_uuid is None
    assert parse_cli_process(1, "claude --resume").session_uuid is None


def test_model_flag_forms():
    assert parse_cli_process(1, "claude --model=claude-opus-5").model == "claude-opus-5"
    assert parse_cli_process(1, "claude --model claude-opus-5").model == "claude-opus-5"


def test_quoted_paths_with_spaces_survive_split():
    argv = split_command_line('"C:\\Program Files\\a b\\claude.exe" --resume x')
    assert argv[0] == "C:\\Program Files\\a b\\claude.exe"


def test_extracted_process_carries_no_raw_command_line():
    """추출 결과에 원문 필드가 존재하지 않는다(민감정보 유출 차단, D1c)."""
    secret = f"claude --resume={SESSION_UUID} --token=SECRET-abc --prompt 'private text'"
    proc = parse_cli_process(7, secret)
    assert "SECRET" not in repr(proc)
    assert "private" not in repr(proc)
    assert not hasattr(proc, "command_line")


# ── 조인 (D2) ──

def test_join_fills_running_pid_and_signal(tmp_path):
    _write_session(tmp_path, "s1", cli_session_id=SESSION_UUID)
    collector = ClaudeSessionCollector(
        tmp_path, process_snapshot=lambda: _snapshot(CliProcess(4242, SESSION_UUID, "claude-opus-5"))
    )

    [record] = collector.collect()

    assert record.running_pid == 4242
    assert record.running_cmd == "process-resume"
    assert collector.last_cycle.process_signal == "ok"


def test_file_model_wins_over_command_line(tmp_path):
    """세션 파일 기록이 권위 — 커맨드라인은 없을 때만 보강한다."""
    _write_session(tmp_path, "s1", cli_session_id=SESSION_UUID, model="from-file")
    collector = ClaudeSessionCollector(
        tmp_path, process_snapshot=lambda: _snapshot(CliProcess(1, SESSION_UUID, "from-cli"))
    )
    [record] = collector.collect()
    assert record.model == "from-file"

    _write_session(tmp_path, "s2", cli_session_id="0b193c7d-0bd8-424d-9e4f-5d7966880d91")
    collector2 = ClaudeSessionCollector(
        tmp_path,
        process_snapshot=lambda: _snapshot(
            CliProcess(2, "0b193c7d-0bd8-424d-9e4f-5d7966880d91", "from-cli")
        ),
    )
    enriched = {r.session_id: r for r in collector2.collect()}
    assert enriched["s2"].model == "from-cli"


def test_duplicate_uuid_picks_lowest_pid_deterministically(tmp_path):
    """같은 세션에 프로세스가 여럿이면 틱마다 결과가 흔들리면 안 된다."""
    _write_session(tmp_path, "s1", cli_session_id=SESSION_UUID)
    collector = ClaudeSessionCollector(
        tmp_path,
        process_snapshot=lambda: _snapshot(
            CliProcess(900, SESSION_UUID, None),
            CliProcess(100, SESSION_UUID, None),
            CliProcess(500, SESSION_UUID, None),
        ),
    )
    assert collector.collect()[0].running_pid == 100


def test_record_never_contains_raw_argv(tmp_path):
    _write_session(tmp_path, "s1", cli_session_id=SESSION_UUID)
    collector = ClaudeSessionCollector(
        tmp_path, process_snapshot=lambda: _snapshot(CliProcess(1, SESSION_UUID, None))
    )
    [record] = collector.collect()
    assert record.running_cmd == "process-resume"
    assert "--resume" not in (record.running_cmd or "")


# ── 실패 ≠ 빈 결과, 이월 금지 (D1a) ──

@pytest.mark.parametrize(
    "status", [SnapshotStatus.FAILED, SnapshotStatus.TIMEOUT, SnapshotStatus.UNSUPPORTED]
)
def test_failed_snapshot_makes_no_running_evidence(tmp_path, status):
    """실패 시 PID를 이월하면 PID 재사용으로 거짓 RUNNING이 된다 — 증거를 만들지 않는다."""
    _write_session(tmp_path, "s1", cli_session_id=SESSION_UUID)
    collector = ClaudeSessionCollector(
        tmp_path,
        process_snapshot=lambda: ProcessSnapshot(status, datetime.now(timezone.utc), ()),
    )

    [record] = collector.collect()

    assert record.running_pid is None
    assert record.running_cmd is None
    assert collector.last_cycle.process_signal == "unavailable"
    assert collector.last_cycle.process_observed_at is None


def test_empty_success_differs_from_failure(tmp_path):
    """0개 성공과 관측 실패는 절대 같은 값이 아니다."""
    _write_session(tmp_path, "s1", cli_session_id=SESSION_UUID)

    ok = ClaudeSessionCollector(tmp_path, process_snapshot=lambda: _snapshot())
    ok.collect()
    failed = ClaudeSessionCollector(
        tmp_path,
        process_snapshot=lambda: ProcessSnapshot(
            SnapshotStatus.FAILED, datetime.now(timezone.utc), ()
        ),
    )
    failed.collect()

    assert ok.last_cycle.process_signal == "ok"
    assert failed.last_cycle.process_signal == "unavailable"


def test_snapshot_provider_exception_does_not_break_collection(tmp_path):
    """주입된 제공자가 터져도 수집은 계속된다(C3). 실패는 데이터로 변환."""
    _write_session(tmp_path, "s1", cli_session_id=SESSION_UUID)

    def boom():
        raise RuntimeError("enumeration exploded")

    collector = ClaudeSessionCollector(tmp_path, process_snapshot=boom)
    records = collector.collect()

    assert len(records) == 1
    assert records[0].running_pid is None
    assert collector.last_cycle.process_signal == "unavailable"


# ── 신호 없는 동안 "종료" 단언 금지 (D1a-2) ──

def test_stale_is_suppressed_while_process_signal_unavailable():
    from acp.models import SessionRecord

    cfg = LivenessConfig(300.0, 900.0, 3600.0)
    old = SessionRecord(
        app="claude", session_id="s", source_file="", last_activity=NOW - timedelta(days=30)
    )

    assert derive_state(old, NOW, cfg) == SessionState.STALE
    assert (
        derive_state(old, NOW, cfg, process_signal_available=False) == SessionState.UNKNOWN
    ), "종료를 관측하지 못한 채 '사실상 종료'라 단언하면 cleanup 집계로 거짓이 흘러간다"

    # 활동 기반 서술(HOLDING)은 그대로 허용된다.
    stalled = SessionRecord(
        app="claude", session_id="s2", source_file="", last_activity=NOW - timedelta(seconds=1000)
    )
    assert derive_state(stalled, NOW, cfg, process_signal_available=False) == SessionState.HOLDING


# ── archived 제외와 고지 (D3) ──

def test_archived_is_flagged_not_dropped(tmp_path):
    """T14 S4a D2: 보관 세션을 **건너뛰지 않고** 플래그로 표시해 넘긴다.

    S3는 여기서 건너뛰었고, 그 결과 이미 저장된 과거 행은 영원히 분류되지 않은 채
    총계에 남으면서 API는 "제외했다"고 고지했다. 버리면 분류할 수 없다.
    """
    _write_session(tmp_path, "keep", cli_session_id=SESSION_UUID)
    _write_session(tmp_path, "old1", archived=True)
    _write_session(tmp_path, "old2", archived=True)

    collector = ClaudeSessionCollector(tmp_path, process_snapshot=lambda: _snapshot())
    records = collector.collect()

    assert sorted(r.session_id for r in records) == ["keep", "old1", "old2"]
    archived_ids = sorted(r.session_id for r in records if r.archived)
    assert archived_ids == ["old1", "old2"]
    assert not next(r for r in records if r.session_id == "keep").archived
    # 스캔이 본 보관 파일 수는 계속 사실로 남는다(이름은 API에서 정직화된다).
    assert collector.last_cycle.excluded_archived == 2
    assert collector.last_cycle.status == "success_complete"


def test_include_archived_setting_no_longer_changes_collection(tmp_path):
    """T14 S4a D6: 이 설정은 더 이상 저장 범위를 제어하지 않는다.

    두 값이 **같은 결과**를 내야 한다 — 그래야 config 계층의 deprecation 경고가
    거짓이 아니다(경고는 test_config_deprecation에서 별도 검증).
    """
    _write_session(tmp_path, "keep")
    _write_session(tmp_path, "old1", archived=True)

    default = ClaudeSessionCollector(tmp_path, process_snapshot=lambda: _snapshot())
    legacy = ClaudeSessionCollector(
        tmp_path, include_archived=True, process_snapshot=lambda: _snapshot()
    )
    assert len(default.collect()) == len(legacy.collect()) == 2
    assert default.last_cycle.excluded_archived == legacy.last_cycle.excluded_archived == 1


# ── 수집 사이클 사실 (D4) ──

def test_cycle_distinguishes_zero_and_failure(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    ok = ClaudeSessionCollector(empty, process_snapshot=lambda: _snapshot())
    assert ok.collect() == []
    assert ok.last_cycle.status == "success_complete"
    assert ok.last_cycle.collected == 0

    missing = ClaudeSessionCollector(tmp_path / "nope", process_snapshot=lambda: _snapshot())
    assert missing.collect() == []
    assert missing.last_cycle.status == "failed"


def test_cycle_marks_partial_on_item_failure(tmp_path):
    _write_session(tmp_path, "good")
    (tmp_path / "acct" / "ws" / "local_broken.json").write_text("{ not json", encoding="utf-8")

    collector = ClaudeSessionCollector(tmp_path, process_snapshot=lambda: _snapshot())
    records = collector.collect()

    assert len(records) == 1
    assert collector.last_cycle.failed == 1
    assert collector.last_cycle.status == "partial"


# ── 캐시·단일 비행 (D1b) ──

def test_cache_collapses_calls_within_ttl():
    calls = {"n": 0}
    clock = {"now": NOW}

    def enumerate_once(_name, _timeout):
        calls["n"] += 1
        return ProcessSnapshot(SnapshotStatus.OK, clock["now"], ())

    cache = ProcessSnapshotCache(ttl_seconds=30.0, enumerator=enumerate_once, clock=lambda: clock["now"])
    for _ in range(5):
        cache.get()
    assert calls["n"] == 1, "TTL 내 반복 호출은 외부 프로세스를 한 번만 띄운다"

    clock["now"] = NOW + timedelta(seconds=31)
    cache.get()
    assert calls["n"] == 2, "TTL 경과 후에는 갱신된다"


def test_cache_reports_timeout_as_data_not_exception():
    def timing_out(_name, _timeout):
        return ProcessSnapshot(SnapshotStatus.TIMEOUT, NOW, (), error="timeout>5s")

    snapshot = ProcessSnapshotCache(enumerator=timing_out, clock=lambda: NOW).get()
    assert snapshot.status is SnapshotStatus.TIMEOUT
    assert snapshot.ok is False


def test_live_process_makes_session_running_without_events():
    """실행 중 세션이 조용하다는 이유로 IDLE/STALE로 내려가면 안 된다(T14 S3 D2+G2).

    claude는 `last_event`가 없어 시간 폴백만 탄다. 세션 uuid에 묶인 PID가 살아 있다는 것은
    시간보다 강한 직접 증거다. 이 분기를 제거하면 실패한다.
    """
    from acp.models import SessionRecord

    cfg = LivenessConfig(300.0, 900.0, 3600.0)
    quiet = SessionRecord(
        app="claude",
        session_id="s",
        source_file="",
        last_activity=NOW - timedelta(hours=5),
        running_pid=4242,
        running_cmd="process-resume",
    )

    assert derive_state(quiet, NOW, cfg, is_alive=lambda _: False) == SessionState.STALE
    assert derive_state(quiet, NOW, cfg, is_alive=lambda pid: pid == 4242) == SessionState.RUNNING


def test_unconfirmed_pid_is_not_running_evidence():
    """이번 스냅샷에서 재확인되지 않은 PID는 직접 증거가 아니다(PID 재사용 방어).

    codex의 오래된 툴콜 osPid가 다른 프로세스에 재할당되면 `is_alive`만으로는
    거짓 RUNNING이 된다.
    """
    from acp.models import SessionRecord

    cfg = LivenessConfig(300.0, 900.0, 3600.0)
    stale_pid = SessionRecord(
        app="codex",
        session_id="s",
        source_file="",
        last_activity=NOW - timedelta(hours=5),
        running_pid=4242,  # 재확인 마커 없음
    )
    assert derive_state(stale_pid, NOW, cfg, is_alive=lambda _: True) == SessionState.STALE


def test_stale_snapshot_is_not_used_as_evidence(tmp_path):
    """성공 스냅샷이어도 **한도를 넘긴 관측**은 증거가 아니다(T14 S3 D1b).

    캐시가 오래된 스냅샷을 돌려줄 수 있으므로 소비 시점에도 freshness를 검사한다.
    낡은 PID가 증거·API로 새어 나가면 안 된다.
    """
    _write_session(tmp_path, "s1", cli_session_id=SESSION_UUID)
    old_observation = datetime.now(timezone.utc) - timedelta(seconds=120)
    collector = ClaudeSessionCollector(
        tmp_path,
        signal_freshness_seconds=30.0,
        process_snapshot=lambda: ProcessSnapshot(
            SnapshotStatus.OK, old_observation, (CliProcess(4242, SESSION_UUID, None),)
        ),
    )

    [record] = collector.collect()

    assert record.running_pid is None, "한도 초과 관측이 증거로 쓰이면 안 된다"
    assert collector.last_cycle.process_signal == "unavailable"


def test_join_counts_are_surfaced(tmp_path):
    """매칭·모호 건수를 조용히 버리지 않는다(T14 S3 D2)."""
    _write_session(tmp_path, "s1", cli_session_id=SESSION_UUID)
    other = "0b193c7d-0bd8-424d-9e4f-5d7966880d91"
    _write_session(tmp_path, "s2", cli_session_id=other)

    collector = ClaudeSessionCollector(
        tmp_path,
        process_snapshot=lambda: _snapshot(
            CliProcess(300, SESSION_UUID, None),
            CliProcess(100, SESSION_UUID, None),  # 같은 세션에 두 프로세스 = 모호
        ),
    )
    collector.collect()

    assert collector.last_cycle.matched == 1
    assert collector.last_cycle.unmatched == 1, "신호는 있었는데 매칭 안 된 세션도 센다"
    assert collector.last_cycle.ambiguous == 1


def test_enumerator_counts_malformed_resume_values(monkeypatch):
    """열거기가 **실제로** malformed를 센다(수동 생성 객체 검사는 동어반복).

    값이 없는 bare `--resume`도 "의도는 있었으나 못 읽었다"이므로 집계 대상이다.
    """
    import subprocess

    from acp import proc as proc_module

    stdout = chr(10).join(
        [
            "111" + chr(9) + "claude --resume=" + SESSION_UUID,   # 정상
            "222" + chr(9) + "claude --resume=not-a-uuid",         # malformed
            "333" + chr(9) + "claude --resume",                    # bare (값 없음)
            "444" + chr(9) + "claude --help",                      # resume 플래그 없음
        ]
    )

    class _Completed:
        returncode = 0

    completed = _Completed()
    completed.stdout = stdout  # type: ignore[attr-defined]
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: completed)

    snapshot = proc_module._enumerate_windows("claude.exe", 5.0)

    assert snapshot.status is SnapshotStatus.OK
    assert len(snapshot.processes) == 4
    assert snapshot.malformed_uuid == 2, "malformed와 bare 모두 세야 한다"
    assert [p.session_uuid for p in snapshot.processes].count(SESSION_UUID) == 1


def test_malformed_count_reaches_cycle(tmp_path):
    """열거기 → 사이클까지 전달된다(중간에서 사라지지 않는다)."""
    _write_session(tmp_path, "s1", cli_session_id=SESSION_UUID)
    collector = ClaudeSessionCollector(
        tmp_path,
        process_snapshot=lambda: ProcessSnapshot(
            SnapshotStatus.OK,
            datetime.now(timezone.utc),
            (CliProcess(1, SESSION_UUID, None),),
            malformed_uuid=3,
        ),
    )
    collector.collect()
    assert collector.last_cycle.malformed_uuid == 3
    assert collector.last_cycle.as_row()["malformed_uuid"] == 3


def test_unmatched_counted_even_when_no_processes(tmp_path):
    """프로세스 0개인 **성공** 사이클에서도 미매칭 세션을 센다.

    `by_uuid`가 비어 있을 때 세지 않으면 모든 미매칭이 조용히 사라진다.
    """
    _write_session(tmp_path, "s1", cli_session_id=SESSION_UUID)
    _write_session(tmp_path, "s2", cli_session_id="0b193c7d-0bd8-424d-9e4f-5d7966880d91")

    collector = ClaudeSessionCollector(tmp_path, process_snapshot=lambda: _snapshot())
    collector.collect()

    assert collector.last_cycle.process_signal == "ok"
    assert collector.last_cycle.matched == 0
    assert collector.last_cycle.unmatched == 2
