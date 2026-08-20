"""tests/test_collector_health.py — 수집 건강도·신호 출처 회귀 (T14 S4b).

이 파일이 잠그는 실제 결함(2026-07-28 실측, `.acp/acp.db`):
  - `collector_health`가 **사이클 행이 있는 앱만** 키로 가져, 세션 723건인 codex와
    cursor·fake가 응답에서 사라졌다. 화면에서 "없음"은 "건강함"과 구분되지 않는다.
  - 사이클을 만드는 수집기가 claude 하나뿐이었고 폴러는 `getattr`로 읽었다 —
    **계약이 요구하지 않아** 침묵으로 통과했다.
  - `cursor.py:71`·`codex.py:143,172`가 소스 부재를 경고 후 `[]`로 축약했다.
    폴러가 이를 "정상"으로 번역하면 **전면 수집 실패가 건강 단언으로 세탁**된다.
  - 예외 경로가 `failed=0`을 기록해 "실패 0건"과 "셀 수 없었음"을 뭉갰다.
  - `app_signals`가 증거 없는 앱을 하드코딩 폴백으로 채워 추정을 권위처럼 내보냈다.

각 테스트는 설계 §4의 V1~V14에 대응하며 **되돌리면 실패**하도록 썼다(mutation gate).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import acp.web.app as webapp
from acp.collectors.base import (
    COUNT_AXES,
    SCOPE_DECLARED,
    SCOPE_NOT_APPLICABLE,
    SCOPE_UNKNOWN,
    STATUS_COMPLETED_UNKNOWN,
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_SUCCESS,
    BaseCollector,
    CollectCycle,
    CountScopeError,
    all_unknown_scopes,
    parse_count_scopes,
    validate_count_scopes,
)
from acp.models import SessionRecord, SessionState
from acp.store import SessionStore
from acp.web.app import EventBroadcaster, app, init_app

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _restore_app_globals():
    prev = (webapp._store, webapp._broadcaster, webapp._run_manager)
    yield
    webapp._store, webapp._broadcaster, webapp._run_manager = prev


@pytest.fixture
def store(tmp_path):
    s = SessionStore(str(tmp_path / "acp.db"), str(tmp_path / "events.jsonl"))
    yield s
    s.close()


def _record(sid: str, app_name: str) -> SessionRecord:
    return SessionRecord(app=app_name, session_id=sid, source_file="test")


def _client(store: SessionStore) -> TestClient:
    init_app(store, EventBroadcaster())
    return TestClient(app)


def _health(store: SessionStore) -> dict:
    return webapp._sessions_payload(store, limit=50)["summary"]["collector_health"]


# ══════════════════════════════════════
# V1·V2·V3 — 합집합 키와 미기록 명시
# ══════════════════════════════════════

def test_v1_health_covers_union_of_sessions_and_cycles(store):
    """세션만 있는 앱도, 사이클만 있는 앱도 **전부** 등장해야 한다.

    사이클 키만 순회하면 관측된 적 없는 앱이 응답에서 사라지고, 소비자에게 그 침묵은
    "건강함"으로 읽힌다.
    """
    store.upsert_session(_record("s1", "codex"), SessionState.LIVE)
    store.upsert_session(_record("s2", "cursor"), SessionState.IDLE)
    cycle = CollectCycle(app="claude", observed_at=NOW)
    cycle.declare(collected=3, failed=0)
    store.record_collector_cycle(cycle.as_row())

    health = _health(store)
    assert set(health) == {"codex", "cursor", "claude"}


def test_v2_app_without_cycle_says_so(store):
    """키를 지우지 않고 **이름으로** 말한다. 세션 수도 함께 실어 대비가 보이게."""
    for i in range(5):
        store.upsert_session(_record(f"s{i}", "codex"), SessionState.LIVE)

    entry = _health(store)["codex"]
    assert entry["status"] == "no_cycle_record"
    assert entry["observation_scope"] == "collector_cycle", "무엇을 근거로 한 판정인지 밝힌다"
    assert entry["session_count"] == 5, "세션 5건인데 관측 0회임이 한 눈에 보여야 한다"
    assert entry["collected"] is None, "관측이 없으면 카운트도 0이 아니라 미상"
    assert set(entry["count_scopes"].values()) == {SCOPE_UNKNOWN}


def test_v3_zero_session_cycle_is_preserved(store):
    """세션 0개인 앱의 사이클도 **남긴다** — 정상 0건과 실패를 가르는 증거다."""
    cycle = CollectCycle(app="probe", observed_at=NOW)
    cycle.declare(collected=0, failed=0)
    store.record_collector_cycle(cycle.as_row())

    entry = _health(store)["probe"]
    assert entry["session_count"] == 0
    assert entry["status"] == STATUS_SUCCESS
    assert entry["collected"] == 0, "정상적으로 0건을 **센** 것이다(미상 아님)"


def test_v4_zero_success_differs_from_failure(store):
    """`success_complete, collected=0`과 `failed`는 절대 같은 값이 아니다."""
    ok = CollectCycle(app="a", observed_at=NOW)
    ok.declare(collected=0, failed=0)
    store.record_collector_cycle(ok.as_row())
    bad = CollectCycle(app="b", observed_at=NOW, status=STATUS_FAILED)
    store.record_collector_cycle(bad.as_row())

    health = _health(store)
    assert health["a"]["status"] == STATUS_SUCCESS and health["a"]["collected"] == 0
    assert health["b"]["status"] == STATUS_FAILED and health["b"]["collected"] is None


# ══════════════════════════════════════
# V4(실제 경로) — failure-as-empty 교정
# ══════════════════════════════════════

def test_v4b_cursor_declares_failure_instead_of_empty(tmp_path):
    """소스 부재를 **빈 결과로 축약하지 않는다**(실제 경로로 검증).

    설계 초안의 "두 인공 status가 다름"만 보는 검사는 이 회귀를 그대로 통과시켰다.
    """
    from acp.collectors.cursor import CursorWorkspaceCollector

    missing = CursorWorkspaceCollector(tmp_path / "does-not-exist")
    assert missing.collect() == []
    assert missing.last_cycle.status == STATUS_FAILED, "부재를 정상 0건으로 부르지 않는다"
    assert missing.last_cycle.count_scopes["collected"] == SCOPE_UNKNOWN

    empty = tmp_path / "empty"
    empty.mkdir()
    normal = CursorWorkspaceCollector(empty)
    assert normal.collect() == []
    assert normal.last_cycle.status == STATUS_SUCCESS, "정말 0건인 것은 성공이다"
    assert normal.last_cycle.collected == 0
    assert normal.last_cycle.count_scopes["collected"] == SCOPE_DECLARED


def test_v4c_codex_required_vs_auxiliary_source(tmp_path):
    """필수 소스 부재는 `failed`, 보조 소스만 부재는 `partial`(V11b)."""
    from acp.collectors.codex import CodexCollector

    sessions = tmp_path / "sessions"
    procs = tmp_path / "chat_processes.json"

    no_required = CodexCollector(tmp_path / "missing", procs)
    assert no_required.collect() == []
    assert no_required.last_cycle.status == STATUS_FAILED

    sessions.mkdir()
    no_aux = CodexCollector(sessions, procs)  # 보조 파일 없음
    no_aux.collect()
    assert no_aux.last_cycle.status == STATUS_PARTIAL, "세션은 모을 수 있으니 실패가 아니다"

    procs.write_text("[]", encoding="utf-8")
    both = CodexCollector(sessions, procs)
    both.collect()
    assert both.last_cycle.status == STATUS_SUCCESS


def test_v11c_record_level_failure_is_counted(tmp_path):
    """소스는 정상인데 레코드 1건이 깨지면 `partial` + 실패 건수를 **센다**."""
    from acp.collectors.cursor import CursorWorkspaceCollector

    base = tmp_path / "ws"
    (base / "good").mkdir(parents=True)
    (base / "good" / "workspace.json").write_text(
        json.dumps({"folder": "file:///c:/repo"}), encoding="utf-8"
    )
    (base / "bad").mkdir()
    (base / "bad" / "workspace.json").write_text("{ not json", encoding="utf-8")

    collector = CursorWorkspaceCollector(base)
    records = collector.collect()
    assert len(records) == 1
    assert collector.last_cycle.status == STATUS_PARTIAL
    assert collector.last_cycle.failed == 1
    assert collector.last_cycle.count_scopes["failed"] == SCOPE_DECLARED


# ══════════════════════════════════════
# V5·V6·V7 — 폴러 방어선
# ══════════════════════════════════════

class _ContractViolator(BaseCollector):
    """계약 위반 더블 — `last_cycle`이 `None`을 돌려준다.

    ABC 승급(V11) 때문에 "프로퍼티가 아예 없는" 수집기는 만들 수 없다. 방어 경로를
    타려면 **계약을 어긴 구현**이어야 한다(설계 R3에서 정정한 모순).
    """

    @property
    def app_name(self) -> str:
        return "violator"

    @property
    def last_cycle(self):  # type: ignore[override]
        return None

    def collect(self) -> list[SessionRecord]:
        return [_record("v1", "violator")]


class _Exploder(BaseCollector):
    @property
    def app_name(self) -> str:
        return "boom"

    @property
    def last_cycle(self) -> CollectCycle:
        return CollectCycle(app="boom")

    def collect(self) -> list[SessionRecord]:
        raise RuntimeError("소스 폭발")


class _Declaring(BaseCollector):
    @property
    def app_name(self) -> str:
        return "declaring"

    def __init__(self) -> None:
        self._cycle = CollectCycle(app="declaring")

    @property
    def last_cycle(self) -> CollectCycle:
        return self._cycle

    def collect(self) -> list[SessionRecord]:
        self._cycle = CollectCycle(app="declaring", observed_at=NOW)
        self._cycle.declare(collected=1, failed=0)
        return [_record("d1", "declaring")]


class _Broadcaster:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def publish(self, event: dict) -> None:
        self.events.append(event)


def _poller(store):
    from acp.config import AppConfig
    from acp.poller import Poller

    return Poller(store, AppConfig(), _Broadcaster())


@pytest.mark.asyncio
async def test_v5_synthesis_never_claims_completeness(store):
    """폴러 합성은 `completed_unknown`까지만. **`success_complete`를 만들지 않는다.**

    "예외 없이 반환했다"는 완결성이 아니다 — 이 계약은 전면 실패를 `[]`로 표현하는 것을
    허용하고, cursor·codex가 실제로 그렇게 한다.
    """
    poller = _poller(store)
    poller.register(_ContractViolator())
    await poller._tick()

    entry = _health(store)["violator"]
    assert entry["status"] == STATUS_COMPLETED_UNKNOWN
    assert entry["source"] == "synthesized", "누가 말했는지 밝힌다"
    assert set(entry["count_scopes"].values()) == {SCOPE_UNKNOWN}
    assert entry["collected"] is None
    observed = datetime.fromisoformat(entry["observed_at"])
    age = (datetime.now(timezone.utc) - observed).total_seconds()
    assert age < 60, "합성 사이클은 **이번 틱**의 것이어야 한다(과거 행 재노출 아님)"


@pytest.mark.asyncio
async def test_v6_declared_cycle_wins(store):
    poller = _poller(store)
    poller.register(_Declaring())
    await poller._tick()

    entry = _health(store)["declaring"]
    assert entry["status"] == STATUS_SUCCESS
    assert entry["source"] == "declared"
    assert entry["collected"] == 1


@pytest.mark.asyncio
async def test_v7_exception_is_failed_with_unknown_counts(store):
    """예외로 중단됐으면 **아무 축도 세지 못했다** — 0으로 단언하지 않는다."""
    poller = _poller(store)
    poller.register(_Exploder())
    await poller._tick()

    entry = _health(store)["boom"]
    assert entry["status"] == STATUS_FAILED
    assert entry["failed"] is None, "'실패 0건'과 '셀 수 없었음'은 다른 사실이다"
    assert entry["collected"] is None
    assert entry["source"] == "synthesized"


# ══════════════════════════════════════
# V8 계열 — count_scopes 계약
# ══════════════════════════════════════

def test_v8_partial_knowledge_survives(store):
    """한 사이클 안에서 아는 축과 모르는 축이 **동시에** 보존된다."""
    cycle = CollectCycle(app="mix", observed_at=NOW)
    cycle.declare(collected=7)
    cycle.mark_not_applicable("matched")
    store.record_collector_cycle(cycle.as_row())

    entry = _health(store)["mix"]
    assert entry["collected"] == 7, "센 축은 숫자 그대로"
    assert entry["failed"] is None, "안 센 축은 0이 아니라 미상"
    assert entry["matched"] is None
    assert entry["count_scopes"]["collected"] == SCOPE_DECLARED
    assert entry["count_scopes"]["failed"] == SCOPE_UNKNOWN
    assert entry["count_scopes"]["matched"] == SCOPE_NOT_APPLICABLE, (
        "'못 셌다'와 '셀 것이 없다'는 다른 사실이다"
    )


def test_v8b_write_contract_is_enforced():
    counts = {axis: 0 for axis in COUNT_AXES}
    # ① 축 누락
    with pytest.raises(CountScopeError):
        validate_count_scopes({"collected": SCOPE_DECLARED}, counts)
    # ② 여분 키
    bad_extra = all_unknown_scopes() | {"bogus": SCOPE_UNKNOWN}
    with pytest.raises(CountScopeError):
        validate_count_scopes(bad_extra, counts)
    # ③ 계약 밖 enum
    bad_enum = all_unknown_scopes() | {"collected": "probably"}
    with pytest.raises(CountScopeError):
        validate_count_scopes(bad_enum, counts)


def test_v8c_declared_requires_a_real_value():
    """`declared`인데 값이 없으면 거부 — 재지 않은 0이 확인된 0으로 승격되던 경로."""
    scopes = all_unknown_scopes() | {"collected": SCOPE_DECLARED}
    with pytest.raises(CountScopeError):
        validate_count_scopes(scopes, {})               # 값 없음
    with pytest.raises(CountScopeError):
        validate_count_scopes(scopes, {"collected": None})
    with pytest.raises(CountScopeError):
        validate_count_scopes(scopes, {"collected": -1})
    with pytest.raises(CountScopeError):
        # bool은 int의 하위 타입 — isinstance만 쓰면 True가 1로 통과한다.
        validate_count_scopes(scopes, {"collected": True})
    assert validate_count_scopes(scopes, {"collected": 3})["collected"] == SCOPE_DECLARED


def test_v8_read_is_fail_closed():
    """NULL·malformed·계약 위반은 전부 **전 축 unknown**. 절대 declared로 보정하지 않는다."""
    assert parse_count_scopes(None) == all_unknown_scopes()
    assert parse_count_scopes("{ not json") == all_unknown_scopes()
    assert parse_count_scopes(json.dumps({"collected": SCOPE_DECLARED})) == all_unknown_scopes()
    good = json.dumps({axis: SCOPE_DECLARED for axis in COUNT_AXES})
    assert parse_count_scopes(good)["collected"] == SCOPE_DECLARED


def test_v8_axes_match_storage_columns(store):
    """`COUNT_AXES`가 저장 카운트 컬럼과 **1:1**. 축을 늘리고 여기를 안 고치면 실패."""
    cols = {
        row[1] for row in store._conn.execute("PRAGMA table_info(collector_cycles)")
    }
    assert set(COUNT_AXES) <= cols
    assert set(COUNT_AXES) == {
        "collected", "failed", "excluded_archived",
        "matched", "unmatched", "ambiguous", "malformed_uuid",
    }


def test_v8d_web_does_not_reparse_raw_json(store):
    """정규화는 **읽기 경계 한 곳**에서. 웹이 다시 해석하면 두 진실이 생긴다."""
    cycle = CollectCycle(app="one", observed_at=NOW)
    cycle.declare(collected=2)
    store.record_collector_cycle(cycle.as_row())

    normalized = store.latest_collector_cycles()["one"]
    assert isinstance(normalized["count_scopes"], dict), "store가 이미 파싱해 돌려준다"
    assert normalized["failed"] is None, "store가 이미 null로 투영한다"

    entry = _health(store)["one"]
    assert isinstance(entry["count_scopes"], dict)
    assert all(not isinstance(v, str) or v in {
        SCOPE_DECLARED, SCOPE_UNKNOWN, SCOPE_NOT_APPLICABLE
    } for v in entry["count_scopes"].values())


def test_v12_migration_reads_legacy_rows_as_unknown(tmp_path):
    """마이그레이션 이전 행의 숫자를 새 계약의 `declared`로 **소급 인증하지 않는다**."""
    db = str(tmp_path / "legacy.db")
    log = str(tmp_path / "events.jsonl")
    raw = sqlite3.connect(db)
    raw.executescript(
        """
        CREATE TABLE collector_cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT, app TEXT NOT NULL,
            observed_at TEXT NOT NULL, status TEXT NOT NULL,
            collected INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
            excluded_archived INTEGER NOT NULL DEFAULT 0, process_signal TEXT
        );
        INSERT INTO collector_cycles (app, observed_at, status, collected, failed)
        VALUES ('claude', '2026-07-01T00:00:00+00:00', 'success_complete', 112, 3);
        """
    )
    raw.commit()
    raw.close()

    first = SessionStore(db, log)
    first.close()
    SessionStore(db, log).close()  # 재적용 무해

    check = sqlite3.connect(db)
    cols = [r[1] for r in check.execute("PRAGMA table_info(collector_cycles)")]
    assert cols.count("count_scopes") == 1 and cols.count("source") == 1
    kept = check.execute("SELECT collected, failed FROM collector_cycles").fetchone()
    assert kept == (112, 3), "원시 값은 보존된다"
    check.close()

    store = SessionStore(db, log)
    try:
        entry = store.latest_collector_cycles()["claude"]
        assert entry["collected"] is None, "증거 없는 과거 숫자를 확인된 값으로 내보내지 않는다"
        assert set(entry["count_scopes"].values()) == {SCOPE_UNKNOWN}
    finally:
        store.close()


# ══════════════════════════════════════
# V9 — 신호 출처
# ══════════════════════════════════════

def test_v9_signal_source_is_declared_or_fallback_or_unknown(store):
    cycle = CollectCycle(app="claude", observed_at=NOW, signal_quality="session-file")
    cycle.declare(collected=1)
    store.record_collector_cycle(cycle.as_row())
    store.upsert_session(_record("x", "newapp"), SessionState.LIVE)

    details = webapp._sessions_payload(store, limit=50)["summary"]["app_signal_details"]
    assert details["claude"] == {"value": "session-file", "source": "declared"}
    assert details["codex"]["source"] == "fallback", "증거 없는 폴백을 권위로 두지 않는다"
    assert details["newapp"] == {"value": None, "source": "unknown"}, "지어내지 않는다"


def test_v9b_old_field_is_a_projection_not_a_second_truth(store):
    """두 필드가 각각 생산되면 드리프트한다 — 옛 필드는 상세의 **투영**이다."""
    store.upsert_session(_record("x", "newapp"), SessionState.LIVE)
    summary = webapp._sessions_payload(store, limit=50)["summary"]
    signals, details = summary["app_signals"], summary["app_signal_details"]

    for app_name, entry in details.items():
        if entry["value"] is None:
            assert app_name not in signals, "모르는 값을 옛 필드에 지어내지 않는다"
        else:
            assert signals[app_name] == entry["value"]


def test_v10_app_signals_keeps_its_shape(store):
    """공개 API 타입을 바꾸지 않는다 — 저장소 grep은 외부 소비자 부재를 증명 못 한다."""
    summary = webapp._sessions_payload(store, limit=50)["summary"]
    assert all(isinstance(v, str) for v in summary["app_signals"].values())
    assert summary["app_signals"]["codex"] == "session-events"
    assert "app_signal_details" in summary, "상세는 **별도 키**로 추가된다"


def test_retired_name_absent_at_every_depth(store):
    """S4a가 폐기한 이름이 `count_scopes` 키로 되살아나지 않는다(구현 중 실측한 회귀)."""
    cycle = CollectCycle(app="claude", observed_at=NOW)
    cycle.declare(collected=1, excluded_archived=2)
    store.record_collector_cycle(cycle.as_row())

    body = _client(store).get("/api/sessions").json()

    def keys(node):
        if isinstance(node, dict):
            for k, v in node.items():
                yield k
                yield from keys(v)
        elif isinstance(node, list):
            for item in node:
                yield from keys(item)

    assert "excluded_archived" not in set(keys(body))
    entry = body["summary"]["collector_health"]["claude"]
    assert entry["archived_seen"] == 2
    assert entry["count_scopes"]["archived_seen"] == SCOPE_DECLARED


def test_v11_collector_must_declare_a_cycle():
    """`last_cycle` 미구현 수집기는 **인스턴스화 불가**(ABC 승급).

    선택 계약이던 시절에는 선언하지 않은 수집기가 침묵으로 통과했고, 침묵은
    소비자에게 "건강함"으로 읽혔다.
    """

    class Silent(BaseCollector):
        @property
        def app_name(self) -> str:
            return "silent"

        def collect(self) -> list[SessionRecord]:
            return []

    with pytest.raises(TypeError, match="last_cycle"):
        Silent()  # type: ignore[abstract]


def test_v4d_codex_auxiliary_parse_failure_is_partial(tmp_path):
    """보조 소스가 **있는데 깨진** 경우도 `partial`(구현리뷰 P1).

    부재만 검사하던 초판은 "파일은 있는데 읽지 못했다"를 정상으로 통과시켰다 —
    실패를 빈 결과로 축약하는 바로 그 경로다.
    """
    from acp.collectors.codex import CodexCollector

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    procs = tmp_path / "chat_processes.json"
    procs.write_text("{ not json", encoding="utf-8")

    collector = CodexCollector(sessions, procs)
    collector.collect()
    assert collector.last_cycle.status == STATUS_PARTIAL


def test_v11c_codex_unreadable_session_file_is_counted(tmp_path):
    """codex 개별 세션 파일 실패도 **센다**(구현리뷰 P1).

    초판은 cursor만 검증했고, codex의 meta/tail/stat 실패는 레코드를 만들면서
    `failed=0, success_complete`로 고지될 수 있었다.
    """
    from acp.collectors.codex import CodexCollector

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    uuid = "0b193c7d-0bd8-424d-9a1e-1c2d3e4f5a6b"
    broken = sessions / f"rollout-{uuid}.jsonl"
    broken.write_text("{ not json\n", encoding="utf-8")
    procs = tmp_path / "chat_processes.json"
    procs.write_text("[]", encoding="utf-8")

    collector = CodexCollector(sessions, procs)
    collector.collect()
    assert collector.last_cycle.failed >= 1, "읽지 못한 파일이 조용히 사라지면 안 된다"
    assert collector.last_cycle.status == STATUS_PARTIAL
    assert collector.last_cycle.count_scopes["failed"] == SCOPE_DECLARED


def test_write_requires_declared_scope_contract(store):
    """`count_scopes` 없이 기록하면 **쓰기가 거부**된다(구현리뷰 P2).

    자동 생성하면 계약을 빠뜨린 호출자가 조용히 통과해 ABC 승급의 강제력이 샌다.
    """
    from acp.collectors.base import CountScopeError

    with pytest.raises(CountScopeError):
        store.record_collector_cycle({
            "app": "x", "observed_at": NOW.isoformat(), "status": STATUS_SUCCESS,
            "collected": 1,
        })


def test_null_scope_path_is_logged(caplog):
    """NULL 경로도 조용하지 않다 — 진단 없이 전 축을 내리면 원인을 못 짚는다."""
    with caplog.at_level("WARNING", logger="acp.collectors.base"):
        parse_count_scopes(None, context="app=x id=1")
    assert any("count_scopes" in r.message for r in caplog.records)
    assert any("app=x id=1" in str(r.args) or "app=x id=1" in r.getMessage()
               for r in caplog.records), "어느 앱·사이클인지 남겨야 추적된다"


def test_v11d_codex_failure_is_not_laundered_by_cache(tmp_path):
    """실패한 파일을 캐시하면 **다음 사이클이 정상으로 세탁**한다(구현리뷰 P1).

    첫 사이클은 `partial, failed=1`인데 파일이 그대로여도 두 번째 사이클이
    `success_complete, failed=0`이 되던 경로. 조건이 그대로면 실패도 그대로여야 한다.
    """
    from acp.collectors.codex import CodexCollector

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    uuid = "0b193c7d-0bd8-424d-9a1e-1c2d3e4f5a6b"
    (sessions / f"rollout-{uuid}.jsonl").write_text("{ not json\n", encoding="utf-8")
    procs = tmp_path / "chat_processes.json"
    procs.write_text("[]", encoding="utf-8")

    collector = CodexCollector(sessions, procs)
    collector.collect()
    first = (collector.last_cycle.status, collector.last_cycle.failed)
    collector.collect()  # 같은 깨진 파일, 변경 없음
    second = (collector.last_cycle.status, collector.last_cycle.failed)

    assert first == second, f"두 번째 사이클이 실패를 세탁했다: {first} → {second}"
    assert second[0] == STATUS_PARTIAL and second[1] >= 1


def _codex_file(sessions, name: str, body: str) -> None:
    (sessions / f"rollout-{name}.jsonl").write_text(body, encoding="utf-8")


def test_v11e_malformed_tail_is_counted_but_seek_boundary_is_not(tmp_path):
    """꼬리 JSONL 파싱 실패는 **세되**, tail 창 첫 줄의 절단은 세지 않는다.

    tail은 바이트 오프셋에서 읽으므로 창 맨 앞 줄이 잘려 있을 수 있다 — 정상 파일에서도
    일어나는 일이라 실패로 세면 건강한 세션마다 거짓 실패가 난다. 반대로 창 안쪽의
    malformed를 조용히 건너뛰면 `(None, None)`이라는 빈 결과로 실패가 축약된다.
    """
    from acp.collectors.codex import CodexCollector

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    procs = tmp_path / "chat_processes.json"
    procs.write_text("[]", encoding="utf-8")
    meta = json.dumps({"type": "session_meta", "payload": {"cwd": "C:/repo"}})
    event = json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}})

    # ① 창 안쪽이 깨진 파일 → 실패로 센다
    _codex_file(sessions, "0b193c7d-0bd8-424d-9a1e-1c2d3e4f5a6b",
                f"{meta}\n{{ broken\n{event}\n".replace(event, "{ also broken"))
    collector = CodexCollector(sessions, procs)
    collector.collect()
    assert collector.last_cycle.failed >= 1
    assert collector.last_cycle.status == STATUS_PARTIAL

    # ② 정상 파일만 있으면 실패 0 — 경계 절단을 실패로 세지 않는다
    clean = tmp_path / "clean"
    clean.mkdir()
    _codex_file(clean, "1c2d3e4f-5a6b-7c8d-9e0f-1a2b3c4d5e6f", f"{meta}\n{event}\n")
    ok = CodexCollector(clean, procs)
    ok.collect()
    assert ok.last_cycle.failed == 0
    assert ok.last_cycle.status == STATUS_SUCCESS


def test_failed_counts_conversations_not_operations(tmp_path):
    """한 대화가 메타·tail 양쪽에서 실패해도 **1건**이다(구현리뷰 P2).

    단위는 **대화 id**다(파일이 아니다 — 같은 대화의 rollout 파일이 여러 개면 1건).
    작업 수로 세면 같은 대화가 2건으로 부풀어 "실패 건수"의 의미가 흐려진다.
    """
    from acp.collectors.codex import CodexCollector

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    procs = tmp_path / "chat_processes.json"
    procs.write_text("[]", encoding="utf-8")
    # 첫 줄부터 깨졌고 꼬리도 깨졌다 — 두 경로 모두 실패하는 파일 하나.
    _codex_file(sessions, "0b193c7d-0bd8-424d-9a1e-1c2d3e4f5a6b",
                "{ broken meta\n{ broken tail\n{ broken tail2\n")

    collector = CodexCollector(sessions, procs)
    collector.collect()
    assert collector.last_cycle.failed == 1, "단위는 실패 대화 수다(작업 수 아님)"


def test_non_dict_json_does_not_kill_the_cycle(tmp_path):
    """JSON 문법은 유효하지만 **객체가 아닌** 레코드가 수집 전체를 죽이면 안 된다.

    `json.loads`가 list/null/string을 돌려주면 `j.get(...)`이 AttributeError를 냈고,
    그것이 `collect()` 밖으로 나가 사이클 전체가 `failed`가 됐다 — 개별 레코드 실패가
    사이클 전체 실패로 번지는 구조다(구현리뷰 P1).
    """
    from acp.collectors.codex import CodexCollector

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    procs = tmp_path / "chat_processes.json"
    procs.write_text("[]", encoding="utf-8")
    meta = json.dumps({"type": "session_meta", "payload": {"cwd": "C:/repo"}})
    _codex_file(sessions, "0b193c7d-0bd8-424d-9a1e-1c2d3e4f5a6b",
                f'{meta}\n[1, 2, 3]\n"just a string"\nnull\n')

    collector = CodexCollector(sessions, procs)
    collector.collect()  # 예외가 나가면 이 줄에서 실패한다
    assert collector.last_cycle.status in {STATUS_PARTIAL, STATUS_SUCCESS}
    assert collector.last_cycle.count_scopes["failed"] == SCOPE_DECLARED


def test_non_dict_session_meta_payload_is_not_success(tmp_path):
    """확인하지 못한 메타를 "정상적인 빈 메타"로 축약하지 않는다(구현리뷰 P1)."""
    from acp.collectors.codex import CodexCollector

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    procs = tmp_path / "chat_processes.json"
    procs.write_text("[]", encoding="utf-8")
    bad_meta = json.dumps({"type": "session_meta", "payload": "not-a-dict"})
    _codex_file(sessions, "0b193c7d-0bd8-424d-9a1e-1c2d3e4f5a6b", f"{bad_meta}\n")

    collector = CodexCollector(sessions, procs)
    collector.collect()
    assert collector.last_cycle.failed == 1
    assert collector.last_cycle.status == STATUS_PARTIAL


def test_seek_boundary_truncation_is_not_a_failure(tmp_path):
    """**실제 seek 절단**을 만들어 경계 오탐이 없음을 확인한다(구현리뷰 P2).

    2줄짜리 파일은 tail 창이 파일 전체라 절단이 일어나지 않는다 — 그런 픽스처로는
    "창 첫 줄 제외" 규칙을 검증할 수 없다. 청크 경계를 넘도록 크게 만든다.
    """
    from acp.collectors.codex import CodexCollector

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    procs = tmp_path / "chat_processes.json"
    procs.write_text("[]", encoding="utf-8")

    meta = json.dumps({"type": "session_meta", "payload": {"cwd": "C:/repo"}})
    filler = json.dumps({"type": "event_msg", "payload": {"type": "agent_message"},
                         "pad": "x" * 400})
    tail = json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}})
    body = "\n".join([meta] + [filler] * 200 + [tail]) + "\n"
    assert len(body.encode("utf-8")) > 8192, "청크 경계를 넘겨야 절단이 생긴다"
    _codex_file(sessions, "0b193c7d-0bd8-424d-9a1e-1c2d3e4f5a6b", body)

    collector = CodexCollector(sessions, procs)
    records = collector.collect()
    assert len(records) == 1
    assert collector.last_cycle.failed == 0, "seek 경계 절단을 실패로 세지 않는다"
    assert collector.last_cycle.status == STATUS_SUCCESS
    assert records[0].last_event == "task_complete"


def test_stat_failure_path_counts_and_does_not_crash(tmp_path, monkeypatch):
    """`stat()` 실패 경로도 살아 있어야 한다.

    이 경로는 어떤 테스트도 타지 않아, 리팩터링 중 존재하지 않는 속성을 참조해도
    전 스위트가 초록으로 통과했다(구현 중 실측). 경로 자체에 게이트를 세운다.
    """
    from pathlib import Path

    from acp.collectors.codex import CodexCollector

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    procs = tmp_path / "chat_processes.json"
    procs.write_text("[]", encoding="utf-8")
    _codex_file(sessions, "0b193c7d-0bd8-424d-9a1e-1c2d3e4f5a6b", "{}\n")

    real_stat = Path.stat

    def boom(self, *args, **kwargs):
        if self.suffix == ".jsonl":
            raise OSError("stat 실패")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", boom)
    collector = CodexCollector(sessions, procs)
    collector.collect()  # AttributeError가 나면 여기서 실패한다
    assert collector.last_cycle.failed == 1
    assert collector.last_cycle.status == STATUS_PARTIAL


def test_untruncated_window_counts_its_first_line():
    """창이 **잘리지 않았다면** 첫 줄의 파싱 실패도 사실이다(구현리뷰 P1).

    "창 첫 줄은 조각일 수 있다"는 면제를 무조건 적용하면, 파일 전체가 창에 들어온
    경우의 진짜 malformed를 놓친다. 면제는 절단이 실제로 일어났을 때만 유효하다.

    **단위 경계에서 검증하는 이유(실측)**: 통합 경로로는 이 차이를 만들 수 없다.
    창이 잘리지 않았다면 창의 첫 줄 = 파일의 첫 줄 = 메타 줄이고, 그게 깨졌다면
    `_read_session_meta`가 이미 실패로 센다. 그래서 collect() 수준의 픽스처로는
    이 규칙을 판별하지 못한다 — 그 사실을 숨기고 통합 테스트를 흉내내는 대신,
    실제로 판별 가능한 경계에서 고정한다.
    """
    from acp.collectors.codex import CodexCollector

    # 스캔은 **최신 event_msg에서 즉시 멈춘다** — 그보다 오래된 줄은 아예 보지 않는다.
    # 그래서 판별하려면 창 안에 event_msg가 없어 끝까지 순회하는 구성이어야 한다.
    lines = ["{ broken", json.dumps({"type": "response_item"})]

    _, _, malformed_when_cut = CodexCollector._scan_for_last_event(lines, truncated=True)
    assert malformed_when_cut is False, "잘린 창의 첫 줄은 조각일 수 있어 면제한다"

    _, _, malformed_when_whole = CodexCollector._scan_for_last_event(lines, truncated=False)
    assert malformed_when_whole is True, "잘리지 않았다면 그 줄도 온전한 레코드다"


def test_malformed_first_returned_line_is_counted_via_integration(tmp_path):
    """통합 경로로도 malformed가 계측된다.

    **한계(실측)**: 이 픽스처는 창 안에 `event_msg`가 없어 500줄 fallback 경로를 탄다.
    따라서 "첫 창의 조각 면제 규칙" 자체를 판별하지는 못한다 — 그 규칙은
    `test_tail_lines_reports_fragment_risk_precisely`가 직접 잠근다. 여기서 고정하는 것은
    "온전한 줄의 malformed가 실패로 계측된다"는 사실이다.
    """
    from acp.collectors.codex import CodexCollector

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    procs = tmp_path / "chat_processes.json"
    procs.write_text("[]", encoding="utf-8")
    meta = json.dumps({"type": "session_meta", "payload": {"cwd": "C:/repo"}})
    other = json.dumps({"type": "response_item"})
    # 40줄: 창(30줄)이 줄 수로 앞을 버린다. 창의 첫 줄을 malformed로 만든다.
    body_lines = [meta] + [other] * 9 + ["{ broken"] + [other] * 29
    _codex_file(sessions, "0b193c7d-0bd8-424d-9a1e-1c2d3e4f5a6b",
                "\n".join(body_lines) + "\n")

    collector = CodexCollector(sessions, procs)
    collector.collect()
    assert collector.last_cycle.failed == 1, "온전한 줄의 malformed는 면제 대상이 아니다"


def test_non_list_chat_processes_does_not_kill_the_cycle(tmp_path):
    """보조 소스가 유효 JSON이지만 배열이 아니어도 수집이 죽지 않는다(구현리뷰 P1)."""
    from acp.collectors.codex import CodexCollector

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    procs = tmp_path / "chat_processes.json"
    procs.write_text("{}", encoding="utf-8")

    collector = CodexCollector(sessions, procs)
    collector.collect()  # 예외가 나가면 여기서 실패
    assert collector.last_cycle.status == STATUS_PARTIAL

    procs.write_text("[null]", encoding="utf-8")
    collector2 = CodexCollector(sessions, procs)
    collector2.collect()
    assert collector2.last_cycle.status == STATUS_PARTIAL


def test_malformed_event_payload_does_not_kill_the_cycle(tmp_path):
    """`payload=null`·숫자 timestamp가 수집 전체를 죽이지 않는다(구현리뷰 P1)."""
    from acp.collectors.codex import CodexCollector

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    procs = tmp_path / "chat_processes.json"
    procs.write_text("[]", encoding="utf-8")
    meta = json.dumps({"type": "session_meta", "payload": {"cwd": "C:/repo"}})
    bad_payload = json.dumps({"type": "event_msg", "payload": None})
    bad_ts = json.dumps({"type": "event_msg", "payload": {"type": "x"}, "timestamp": 12345})
    _codex_file(sessions, "0b193c7d-0bd8-424d-9a1e-1c2d3e4f5a6b",
                f"{meta}\n{bad_payload}\n")
    collector = CodexCollector(sessions, procs)
    collector.collect()
    assert collector.last_cycle.failed == 1

    _codex_file(sessions, "1c2d3e4f-5a6b-7c8d-9e0f-1a2b3c4d5e6f", f"{meta}\n{bad_ts}\n")
    collector2 = CodexCollector(sessions, procs)
    collector2.collect()  # 예외가 나가면 여기서 실패
    assert collector2.last_cycle.failed >= 1


def test_failed_unit_is_conversation_not_file(tmp_path):
    """같은 대화의 rollout 파일이 **여럿 깨져도 1건**이다 — 단위가 대화 id임을 고정."""
    from acp.collectors.codex import CodexCollector

    sessions = tmp_path / "sessions"
    (sessions / "a").mkdir(parents=True)
    (sessions / "b").mkdir()
    procs = tmp_path / "chat_processes.json"
    procs.write_text("[]", encoding="utf-8")
    uuid = "0b193c7d-0bd8-424d-9a1e-1c2d3e4f5a6b"
    for sub in ("a", "b"):
        (sessions / sub / f"rollout-{uuid}.jsonl").write_text("{ broken\n", encoding="utf-8")

    collector = CodexCollector(sessions, procs)
    collector.collect()
    assert collector.last_cycle.failed == 1, "같은 대화의 여러 파일은 1건으로 센다"


def test_tail_lines_reports_fragment_risk_precisely(tmp_path):
    """`truncated`는 "**반환 첫 줄이 조각일 수 있는가**"만 뜻해야 한다(구현리뷰 P1).

    줄 수로 앞을 버린 경우 반환 첫 줄은 온전하므로 조각 위험이 아니다. 이 판정을 넓게
    잡으면(`pos > 0 or len(lines) > n`) 온전한 줄의 malformed까지 면제해 진짜 결함을 놓친다.

    **범위 실측**: 루프는 `len(lines) >= n`에서 멈추므로, 조각 위험이 실제로 성립하는
    구간은 `pos > 0 이고 len(lines) == n`인 드문 경우뿐이다. 그 희소성을 근거로 판정을
    넓히면 흔한 경우(줄 수로 버림)를 통째로 면제하게 되므로 좁게 유지한다.
    """
    from acp.collectors.codex import _tail_lines

    f = tmp_path / "small.jsonl"
    f.write_text(chr(10).join(f"line{i}" for i in range(40)) + chr(10), encoding="utf-8")
    lines, ok, fragment_risk = _tail_lines(f, n=30)
    assert ok and len(lines) == 30
    assert fragment_risk is False, "줄 수로 버린 앞부분은 조각 위험이 아니다"

    whole, ok, fragment_risk = _tail_lines(f, n=1000)
    assert ok and len(whole) == 40
    assert fragment_risk is False, "파일 처음까지 읽었으면 조각이 아니다"


def test_malformed_chat_process_fields_do_not_kill_the_cycle(tmp_path):
    """보조 소스 **원소 내부 필드**가 깨져도 수집 전체가 죽지 않는다(구현리뷰 P1).

    `conversationId=[]`는 dict 키 조회에서 `TypeError: unhashable type`을,
    비교 불가한 `updatedAtMs`는 `>` 비교에서 TypeError를 냈다. 둘 다 레코드별
    예외 격리보다 **앞에서** 실행돼 사이클 전체로 번졌다.
    """
    from acp.collectors.codex import CodexCollector

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    procs = tmp_path / "chat_processes.json"

    procs.write_text(json.dumps([{"conversationId": []}]), encoding="utf-8")
    c1 = CodexCollector(sessions, procs)
    c1.collect()  # 예외가 나가면 여기서 실패
    assert c1.last_cycle.status == STATUS_PARTIAL

    procs.write_text(json.dumps([
        {"conversationId": "abc", "updatedAtMs": "bad"},
        {"conversationId": "abc", "updatedAtMs": 0},
    ]), encoding="utf-8")
    c2 = CodexCollector(sessions, procs)
    c2.collect()
    assert c2.last_cycle.status == STATUS_PARTIAL


def test_first_record_not_session_meta_is_not_clean(tmp_path):
    """첫 레코드가 유효 객체지만 `session_meta`가 아니면 **미확인**이다(구현리뷰 P1).

    빈 dict를 "정상적인 빈 메타"로 돌려주면 cwd·model 미확인이 정상 수집으로 세탁된다.
    """
    from acp.collectors.codex import CodexCollector

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    procs = tmp_path / "chat_processes.json"
    procs.write_text("[]", encoding="utf-8")
    _codex_file(sessions, "0b193c7d-0bd8-424d-9a1e-1c2d3e4f5a6b",
                json.dumps({"type": "response_item"}) + chr(10))

    collector = CodexCollector(sessions, procs)
    collector.collect()
    assert collector.last_cycle.failed == 1
    assert collector.last_cycle.status == STATUS_PARTIAL


def test_malformed_updated_at_keeps_the_record(tmp_path):
    """깨진 `updatedAtMs`가 있어도 상태는 partial이고 **레코드는 보존**된다.

    **이 테스트는 mutation gate가 아니다(실측)**: 리뷰가 예측한 "동률일 때 깨진 값이
    살아남아 하류에서 대화가 탈락한다"는 데이터 손실은 실제로 재현되지 않았다 —
    저장 항목 정규화를 제거해도 레코드는 그대로 보존된다. 정규화는 방어로 남기되,
    판별력이 없는 검사를 게이트인 척하지 않는다. 여기서 고정하는 것은 관찰된 계약
    (partial + 레코드 보존)이다.
    """
    from acp.collectors.codex import CodexCollector

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    procs = tmp_path / "chat_processes.json"
    procs.write_text(json.dumps([
        {"conversationId": "abc", "updatedAtMs": "bad", "cwd": "C:/repo"},
        {"conversationId": "abc", "updatedAtMs": 0, "cwd": "C:/repo"},
    ]), encoding="utf-8")

    collector = CodexCollector(sessions, procs)
    records = collector.collect()
    assert collector.last_cycle.status == STATUS_PARTIAL
    assert [r.session_id for r in records] == ["abc"], "정직한 상태와 별개로 레코드는 보존된다"


def test_fragment_risk_true_branch_is_covered(tmp_path):
    """조각 위험 **True** 경계도 고정한다(구현리뷰 P3).

    False만 검증하면 "항상 False"로 바꾸는 mutation을 판별하지 못한다. 청크 경계에
    정확히 맞춰 `pos > 0 이고 len(lines) == n`을 만든다.
    """
    from acp.collectors.codex import _TAIL_CHUNK, _tail_lines

    f = tmp_path / "aligned.jsonl"
    line = "y" * 1023  # +개행 = 1024바이트 → 청크(8192)에 정확히 8줄
    f.write_text((line + chr(10)) * 40, encoding="utf-8")
    assert f.stat().st_size > _TAIL_CHUNK

    lines, ok, fragment_risk = _tail_lines(f, n=_TAIL_CHUNK // 1024)
    assert ok and len(lines) == 8
    assert fragment_risk is True, "바이트 경계에서 시작했고 앞을 더 버리지 않았다"
