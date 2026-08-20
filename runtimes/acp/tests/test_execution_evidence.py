"""tests/test_execution_evidence.py — 실행 증거 정직성 게이트(T14 S4c-1).

잠그는 결함:
  - `process_signal` 기본값 `"ok"` — 아무도 선언하지 않아도 "정상 관측"으로 보였다.
  - capability 부재 — "실행 신호를 줄 수 없는 앱"과 "이번엔 못 얻은 앱"이 같아 보였다.
  - 가용성 판정 `!= "unavailable"` — 넓어진 어휘와 `None`이 전부 가용으로 통과해
    stale 구간이 STALE로 **확정**됐다(화면은 `확인 불가`, 상태는 `STALE`).
  - 내부 마커 `process-resume`가 명령어인 양 노출됐다.

각 검사는 설계 §4의 V번호와 대응한다. 되돌리면 실패하는지를 mutation으로 실측했다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from acp.collectors.base import (
    CAPABILITY_SUPPORTED,
    CAPABILITY_UNKNOWN,
    CAPABILITY_UNSUPPORTED,
    PROCESS_SIGNAL_NOT_APPLICABLE,
    PROCESS_SIGNAL_OK,
    PROCESS_SIGNAL_UNAVAILABLE,
    PROCESS_SIGNAL_UNKNOWN,
    BaseCollector,
    CollectCycle,
    CountScopeError,
)
from acp.collectors.claude import ClaudeSessionCollector
from acp.collectors.codex import CodexCollector
from acp.collectors.cursor import CursorWorkspaceCollector
from acp.collectors.fake import FakeCollector
from acp.config import AppConfig, LivenessConfig, NotifyConfig
from acp import evidence as evidence_module
from acp.evidence import (
    EVIDENCE_COMMAND,
    EVIDENCE_NONE,
    EVIDENCE_PROCESS_RECHECK,
    EVIDENCE_UNKNOWN,
    INTERNAL_MARKERS,
    MARKER_PROCESS_RECHECK,
    evidence_kind,
)
from acp.models import SessionRecord, SessionState
from acp.poller import Poller
from acp.store import SessionStore


# ── 헬퍼 ─────────────────────────────────────────────────────────────


class _Broadcaster:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def publish(self, event: dict) -> None:
        self.events.append(event)


class _SignalCollector(BaseCollector):
    """실행 신호 두 축을 테스트가 지정하는 수집기."""

    def __init__(
        self,
        records: list[SessionRecord],
        *,
        capability: str = CAPABILITY_SUPPORTED,
        signal: str = PROCESS_SIGNAL_OK,
        declare_cycle: bool = True,
    ) -> None:
        self.records = records
        self._capability = capability
        self._signal = signal
        self._declare_cycle = declare_cycle
        self._cycle = self._make()

    def _make(self) -> CollectCycle:
        cycle = CollectCycle(
            app="fake",
            process_signal_capability=self._capability,
            process_signal=self._signal,
        )
        cycle.declare(collected=len(self.records), failed=0)
        return cycle

    @property
    def app_name(self) -> str:
        return "fake"

    @property
    def last_cycle(self) -> CollectCycle:
        if not self._declare_cycle:
            raise AttributeError("이 수집기는 사이클을 선언하지 않는다")
        return self._cycle

    def collect(self) -> list[SessionRecord]:
        self._cycle = self._make()
        return self.records


def _cfg() -> AppConfig:
    return AppConfig(
        poll_interval=1,
        liveness=LivenessConfig(idle_threshold=10, hold_threshold=20, stale_ttl=40),
        notify=NotifyConfig(toast_enabled=False, webhook_url="", notify_cooldown=3600),
    )


def _stale_record() -> SessionRecord:
    """stale_ttl을 한참 넘긴 레코드 — 신호가 있으면 STALE로 확정될 대상."""
    return SessionRecord(
        app="fake",
        session_id="s1",
        project_path="C:/repo",
        last_activity=datetime.now(timezone.utc) - timedelta(days=7),
        last_event="task_complete",
        source_file="test",
    )


@pytest.fixture()
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(str(tmp_path / "acp.db"))


# ── V1 · V2 — capability는 선언 대상이고, 기본값은 unknown ────────────


def test_v1_capability_defaults_to_unknown() -> None:
    """선언하지 않은 수집기는 `unknown`이다.

    기본을 `supported`로 두면 "줄 수 있는데 안 왔다"(=안 돌고 있음)로 읽혀
    **미확인이 사실로 승격**된다. mutation: 기본값을 supported로 → 실패.
    """
    assert CollectCycle(app="new-app").process_signal_capability == CAPABILITY_UNKNOWN


def test_v1b_process_signal_defaults_to_unknown() -> None:
    """`process_signal`의 기본값도 `unknown`이다(옛 기본값은 `"ok"`였다).

    기본값이 `ok`면 실행 신호 소스를 **읽지도 않은** 사이클이 정상 관측으로 보고된다.
    mutation: 기본값을 "ok"로 → 실패.
    """
    assert CollectCycle(app="new-app").process_signal == PROCESS_SIGNAL_UNKNOWN


def test_v2_each_collector_declares_its_capability(tmp_path: Path) -> None:
    """앱별 선언값을 **값까지** 고정한다(필드 존재만 보면 값 정확성을 못 잡는다).

    codex는 조인 카운트를 `not_applicable`로 선언하면서도 `osPid`로 `running_pid`를
    채운다 — 그 둘은 다른 축이므로 capability는 `supported`다.
    """
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    procs = tmp_path / "chat_processes.json"
    procs.write_text("[]", encoding="utf-8")
    workspaces = tmp_path / "ws"
    workspaces.mkdir()

    claude = ClaudeSessionCollector(sessions)
    claude.collect()
    codex = CodexCollector(sessions, procs)
    codex.collect()
    cursor = CursorWorkspaceCollector(workspaces)
    cursor.collect()
    fake = FakeCollector()
    fake.collect()

    assert claude.last_cycle.process_signal_capability == CAPABILITY_SUPPORTED
    assert codex.last_cycle.process_signal_capability == CAPABILITY_SUPPORTED
    assert cursor.last_cycle.process_signal_capability == CAPABILITY_UNSUPPORTED
    # 합성이어도 pid/cmd를 실제로 채운다 — capability는 "축을 제공하는가"이다.
    assert fake.last_cycle.process_signal_capability == CAPABILITY_SUPPORTED
    # 구조적으로 줄 수 없는 앱의 관측 축은 `not_applicable`(≠ unavailable).
    assert cursor.last_cycle.process_signal == PROCESS_SIGNAL_NOT_APPLICABLE


# ── V3 — 두 축은 분리돼 있다 ─────────────────────────────────────────


def test_v3_capability_and_observation_are_independent_axes(tmp_path: Path) -> None:
    """`supported`인데 이번 사이클은 `unavailable`인 조합이 표현 가능해야 한다.

    한 필드로 합치면 "구조적으로 못 준다"와 "이번엔 못 얻었다"가 같아진다.
    codex의 보조 소스(chat_processes.json) 부재로 그 조합을 **실제로** 만든다.
    """
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    collector = CodexCollector(sessions, tmp_path / "missing.json")
    collector.collect()
    assert collector.last_cycle.process_signal_capability == CAPABILITY_SUPPORTED
    assert collector.last_cycle.process_signal == PROCESS_SIGNAL_UNAVAILABLE


def test_v3b_codex_declares_ok_when_source_is_readable(tmp_path: Path) -> None:
    """소스를 읽었으면 0건이어도 `ok`다 — 관측했다는 사실과 결과 수는 다르다."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    procs = tmp_path / "chat_processes.json"
    procs.write_text("[]", encoding="utf-8")
    collector = CodexCollector(sessions, procs)
    collector.collect()
    assert collector.last_cycle.process_signal == PROCESS_SIGNAL_OK


def test_v3c_codex_malformed_source_is_unavailable(tmp_path: Path) -> None:
    """파싱 실패·비배열은 `unavailable` — 파일이 있다고 관측한 것이 아니다."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    procs = tmp_path / "chat_processes.json"
    procs.write_text("{not json", encoding="utf-8")
    collector = CodexCollector(sessions, procs)
    collector.collect()
    assert collector.last_cycle.process_signal == PROCESS_SIGNAL_UNAVAILABLE

    procs.write_text('{"conversationId": "x"}', encoding="utf-8")
    collector2 = CodexCollector(sessions, procs)
    collector2.collect()
    assert collector2.last_cycle.process_signal == PROCESS_SIGNAL_UNAVAILABLE


@pytest.mark.parametrize(
    "payload",
    [
        '[{"conversationId": "a", "osPid": 1}, "not-an-object"]',
        '[{"conversationId": [], "osPid": 1}]',
        '[{"conversationId": "a", "updatedAtMs": "yesterday", "osPid": 1}]',
        # 타입은 float이지만 시각이 아니다. JSON이 기본 허용하는 비표준 값이라
        # 실제로 들어올 수 있다(구현리뷰 R5 P1).
        '[{"conversationId": "a", "updatedAtMs": NaN, "osPid": 1}]',
        '[{"conversationId": "a", "updatedAtMs": Infinity, "osPid": 1}]',
        # 파이썬 int는 임의 정밀도다 — 판별자 자신이 OverflowError를 낼 수 있고,
        # 그 예외가 새면 값 하나가 수집 전체를 죽인다(구현리뷰 R6 P1).
        '[{"conversationId": "a", "updatedAtMs": ' + "9" * 400 + ', "osPid": 1}]',
    ],
)
def test_v3c2_codex_malformed_entry_downgrades_the_app_signal(
    tmp_path: Path, payload: str
) -> None:
    """항목이 깨진 사이클을 "정상 관측"이라 부르지 않는다(구현리뷰 R4 P1).

    `process_signal`은 **앱 단위 플래그**라 "일부 대화의 신호만 못 봤다"를 표현할 수
    없다. 그런데도 `ok`라 말하면 증거를 못 본 대화가 화면에서 `-`(안 돌고 있음)로
    그려지고 D1c의 STALE 확정까지 통과한다 — 관측 실패가 관측 결과로 바뀐다.
    """
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    procs = tmp_path / "chat_processes.json"
    procs.write_text(payload, encoding="utf-8")
    collector = CodexCollector(sessions, procs)
    collector.collect()
    assert collector.last_cycle.process_signal == PROCESS_SIGNAL_UNAVAILABLE


def test_v3c3_non_finite_timestamp_does_not_drop_the_conversation(tmp_path: Path) -> None:
    """`Infinity`가 대화를 **통째로 탈락**시키지 않는다(구현리뷰 R5 P1).

    `int(inf)`의 `OverflowError`가 `ms_to_dt`에서 잡히지 않아 `_merge`가 터졌고,
    그 대화는 목록에서 사라졌다 — 시각 하나를 몰라서 세션 자체를 잃는 것은
    버린 사실이다(LESSON-004). 시각은 `None`으로 남기고 레코드는 살린다.
    """
    from acp.timeutil import ms_to_dt

    assert ms_to_dt(float("inf")) is None
    assert ms_to_dt(float("nan")) is None

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    procs = tmp_path / "chat_processes.json"
    procs.write_text(
        '[{"conversationId": "conv-inf", "updatedAtMs": Infinity, "osPid": 77}]',
        encoding="utf-8",
    )
    collector = CodexCollector(sessions, procs)
    records = collector.collect()
    assert [r.session_id for r in records] == ["conv-inf"]
    assert records[0].last_activity is None
    assert records[0].running_pid == 77
    # 레코드는 살아남되, 소스가 형식 계약을 어겼다는 사실은 신호가 말한다.
    assert collector.last_cycle.process_signal == PROCESS_SIGNAL_UNAVAILABLE


def test_v3c4_huge_int_does_not_kill_the_whole_cycle(tmp_path: Path) -> None:
    """값 하나의 형식 오류가 **다른 대화의 관측까지** 없애지 않는다(구현리뷰 R6 P1).

    판별자가 예외를 던지면 `collect()`가 통째로 죽고, 멀쩡히 실행 중인 다른 대화의
    증거까지 "관측하지 못함"으로 바뀐다 — 국소 오류가 전면 실패로 번지는 형태다.
    """
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    procs = tmp_path / "chat_processes.json"
    procs.write_text(
        "["
        '{"conversationId": "broken", "updatedAtMs": ' + "9" * 400 + ', "osPid": 1},'
        '{"conversationId": "healthy", "updatedAtMs": 1700000000000, "osPid": 2}'
        "]",
        encoding="utf-8",
    )
    collector = CodexCollector(sessions, procs)
    records = {r.session_id: r for r in collector.collect()}
    assert set(records) == {"broken", "healthy"}
    assert records["healthy"].running_pid == 2
    assert records["healthy"].last_activity is not None
    assert records["broken"].last_activity is None


def test_v3d_claude_never_queried_the_snapshot_stays_unknown(tmp_path: Path) -> None:
    """소스 폴더가 없어 스냅샷을 조회조차 못 했으면 `unknown`이다.

    `unavailable`이라 적으면 "조회했는데 실패"라는 하지 않은 관측을 주장하게 된다.
    """
    collector = ClaudeSessionCollector(tmp_path / "does-not-exist")
    collector.collect()
    assert collector.last_cycle.process_signal == PROCESS_SIGNAL_UNKNOWN
    assert collector.last_cycle.process_signal_capability == CAPABILITY_SUPPORTED


# ── V4 — 저장·API 왕복에서 두 축이 보존된다 ──────────────────────────


def test_v4_axes_survive_store_roundtrip(store: SessionStore) -> None:
    cycle = CollectCycle(
        app="claude",
        process_signal_capability=CAPABILITY_SUPPORTED,
        process_signal=PROCESS_SIGNAL_UNAVAILABLE,
    )
    cycle.declare(collected=3, failed=0)
    store.record_collector_cycle(cycle.as_row())
    row = store.latest_collector_cycles()["claude"]
    assert row["process_signal_capability"] == CAPABILITY_SUPPORTED
    assert row["process_signal"] == PROCESS_SIGNAL_UNAVAILABLE


def test_v4b_legacy_rows_read_as_unknown(store: SessionStore) -> None:
    """컬럼이 NULL인 옛 행을 새 계약의 `supported`/`ok`로 소급 인증하지 않는다."""
    with store._conn:  # 마이그레이션 이전 행을 그대로 재현한다
        store._conn.execute(
            "INSERT INTO collector_cycles (app, observed_at, status, collected, failed, "
            "excluded_archived) VALUES ('legacy', '2026-01-01T00:00:00+00:00', "
            "'success_complete', 5, 0, 0)"
        )
    row = store.latest_collector_cycles()["legacy"]
    assert row["process_signal_capability"] == CAPABILITY_UNKNOWN
    assert row["process_signal"] == PROCESS_SIGNAL_UNKNOWN


def test_v4c_unknown_capability_is_rejected_at_write(store: SessionStore) -> None:
    """계약 밖 값은 조용히 통과시키지 않는다 — 읽기가 unknown으로 덮으면 영영 안 드러난다."""
    cycle = CollectCycle(app="claude", process_signal_capability="probably")
    cycle.declare(collected=0, failed=0)
    with pytest.raises(CountScopeError):
        store.record_collector_cycle(cycle.as_row())


def test_v4c2_unknown_process_signal_is_rejected_at_write(store: SessionStore) -> None:
    """관측 축도 같은 강도로 거절한다(구현리뷰 R1 P1).

    한 축만 검사하면 계약 밖 값이 저장된 뒤 읽기의 fail-closed가 `unknown`으로 덮어써서,
    잘못된 선언이 영원히 드러나지 않는다 — 읽기 방어가 쓰기 게이트를 무력화한다.
    """
    cycle = CollectCycle(
        app="claude",
        process_signal_capability=CAPABILITY_SUPPORTED,
        process_signal="fine",
    )
    cycle.declare(collected=0, failed=0)
    with pytest.raises(CountScopeError):
        store.record_collector_cycle(cycle.as_row())

    # 폴러의 방어 합성은 `None`을 쓴다 — 그 자체가 "모름"이므로 정당한 값이다.
    row = CollectCycle(app="claude").as_row()
    row["process_signal"] = None
    store.record_collector_cycle(row)
    assert store.latest_collector_cycles()["claude"]["process_signal"] == PROCESS_SIGNAL_UNKNOWN


def test_v4d_api_exposes_both_axes_and_unknown_for_appless(store: SessionStore) -> None:
    """`collector_health`에 두 축이 **둘 다** 있고, 사이클 없는 앱은 `unknown`이다."""
    from acp.web.app import _sessions_payload

    cycle = CollectCycle(
        app="cursor",
        process_signal_capability=CAPABILITY_UNSUPPORTED,
        process_signal=PROCESS_SIGNAL_NOT_APPLICABLE,
    )
    cycle.declare(collected=1, failed=0)
    store.record_collector_cycle(cycle.as_row())
    store.upsert_session(
        SessionRecord(app="codex", session_id="c1", source_file="test"), SessionState.UNKNOWN
    )

    health = _sessions_payload(store, limit=50)["summary"]["collector_health"]
    assert health["cursor"]["process_signal_capability"] == CAPABILITY_UNSUPPORTED
    assert health["cursor"]["process_signal"] == PROCESS_SIGNAL_NOT_APPLICABLE
    # 사이클이 없는 앱: 침묵도 `null`도 아니고 **모른다**고 말한다.
    assert health["codex"]["status"] == "no_cycle_record"
    assert health["codex"]["process_signal_capability"] == CAPABILITY_UNKNOWN
    assert health["codex"]["process_signal"] == PROCESS_SIGNAL_UNKNOWN


# ── V5 — 증거 종류 enum(서버) ────────────────────────────────────────


@pytest.mark.parametrize(
    ("running_cmd", "expected"),
    [
        (MARKER_PROCESS_RECHECK, EVIDENCE_PROCESS_RECHECK),
        ("python -m acp web", EVIDENCE_COMMAND),
        (None, EVIDENCE_NONE),
        ("", EVIDENCE_NONE),
        ("   ", EVIDENCE_NONE),
        # 우리 네임스페이스인데 표에 없다 → 명령으로 **승격하지 않는다**.
        ("acp:future-marker", EVIDENCE_UNKNOWN),
    ],
)
def test_v5_evidence_kind_projection(running_cmd: object, expected: str) -> None:
    assert evidence_kind(running_cmd) == expected


# `running_cmd`에 리터럴을 쓰지만 **앱 역할**이라 명령어가 맞는 곳.
# 이 목록은 사람의 판단이며 게이트가 그 판단을 대신하지 못한다(구현리뷰 R2 P2) —
# 짧게 유지하고, 항목을 늘리는 변경은 리뷰에서 그 이유를 물어야 한다.
_APP_ROLE_COMMAND_LITERALS = {
    # FakeCollector는 앱을 흉내내는 픽스처다. 이 값은 ACP의 내부 마커가 아니라
    # "앱이 준 명령어"를 연기한다(그래서 화면에도 명령어로 보여야 옳다).
    "python -m acp web",
}


def test_v5b2_no_unregistered_marker_literal_is_written() -> None:
    """ACP가 `running_cmd`에 써 넣는 값은 **등록표에서** 와야 한다.

    네임스페이스 접두사는 읽기 쪽 2차 방어일 뿐이다. 새 마커를 `acp:` 밖 이름으로
    써 넣으면 `evidence_kind`는 그것을 **명령어로 승격**하고 원문이 화면·`title`에
    새어 나간다 — 읽기만으로는 임의 문자열인 명령어와 구분할 수 없기 때문이다
    (구현리뷰 R1 P1). 그래서 강제를 **쓰기 쪽**에 둔다.

    상수 경유(`NEW_MARKER = "x"; running_cmd=NEW_MARKER`)는 마커를 추가하는 가장
    자연스러운 방식이므로 **이름 바인딩까지 따라간다**(구현리뷰 R2 P1).

    **이 게이트가 증명하지 않는 것**(정직한 한계):
      - 호출·속성 접근으로 만들어진 값(`proc.get("command")`)은 앱이 준 통로라
        추적하지 않는다. f-string·문자열 결합으로 마커를 조립하는 경우도 못 잡는다.
      - `_APP_ROLE_COMMAND_LITERALS`에 무엇을 넣을지는 **사람의 판단**이다. 내부 마커를
        거기 넣으면 게이트는 통과한다 — 목록이 짧고 리뷰 대상이라는 것이 유일한 방어다.
    """
    import ast

    acp_root = Path(__file__).resolve().parent.parent / "acp"
    found: dict[str, str] = {}
    resolved_via_name: set[str] = set()

    for path in acp_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        # 이름 바인딩은 **모듈 상수와 evidence import로 한정**한다(구현리뷰 R3 P1).
        # 파일 전체의 대입을 한 사전에 합치면 스코프를 무시하게 되고, 다른 함수의
        # 지역 변수가 같은 이름이라는 이유로 **앱이 준 값을 리터럴로 오인**해 정당한
        # 코드를 막는다(오탐). 함수·클래스 안에서 다시 대입되는 이름은 모호하므로
        # 아예 해석하지 않는다 — 여기서는 놓치는 쪽이 막는 쪽보다 안전하다.
        local_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Store):
                        local_names.add(inner.id)
                    elif isinstance(inner, ast.arg):
                        local_names.add(inner.arg)

        bindings: dict[str, str] = {}
        for node in tree.body:  # 모듈 최상위만
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                if isinstance(node.value.value, str):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            bindings[target.id] = node.value.value
            elif isinstance(node, ast.ImportFrom) and node.module == "acp.evidence":
                for alias in node.names:
                    value = getattr(evidence_module, alias.name, None)
                    if isinstance(value, str):
                        bindings[alias.asname or alias.name] = value
        for name in local_names:
            bindings.pop(name, None)

        def resolve(node: ast.AST):
            """**값 위치**만 본다. 중첩 호출까지 훑으면 `proc.get("command")`의 dict
            키가 마커로 오인된다 — 그건 앱이 준 값을 읽는 통로다."""
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                yield node.value, False
            elif isinstance(node, ast.Name) and node.id in bindings:
                yield bindings[node.id], True
            elif isinstance(node, ast.IfExp):
                yield from resolve(node.body)
                yield from resolve(node.orelse)
            elif isinstance(node, ast.BoolOp):
                for value in node.values:
                    yield from resolve(value)

        targets: list[ast.AST] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "running_cmd":
                targets.append(node.value)
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value == "running_cmd":
                        targets.append(value)
        for target in targets:
            for literal, via_name in resolve(target):
                found.setdefault(literal, str(path.relative_to(acp_root)))
                if via_name:
                    resolved_via_name.add(literal)

    unregistered = {
        literal: where
        for literal, where in found.items()
        if literal not in INTERNAL_MARKERS and literal not in _APP_ROLE_COMMAND_LITERALS
    }
    assert not unregistered, (
        f"등록되지 않은 running_cmd 값(마커면 INTERNAL_MARKERS에, 앱 명령이면 "
        f"_APP_ROLE_COMMAND_LITERALS에 등록하라): {unregistered}"
    )
    # 탐지기 생존 검사: **알려진 마커를 이름 경유로 실제 찾아냈는가**. 대입 지점 개수를
    # 세는 것으로는 부족했다 — 지점이 남아 있어도 추출 능력이 죽으면 통과했다(R2 P2).
    assert MARKER_PROCESS_RECHECK in resolved_via_name, (
        "알려진 마커를 이름 경유로 찾지 못했다 — 스캔의 추출 능력이 죽었다"
    )
    # 앱 역할 리터럴은 **명령어로** 투영돼야 한다(마커로 오분류되면 그것도 결함).
    for literal in _APP_ROLE_COMMAND_LITERALS:
        assert evidence_kind(literal) == EVIDENCE_COMMAND


def test_v5b_marker_registry_is_the_single_source() -> None:
    """마커 상수는 등록표 안에 있어야 한다.

    쓰는 쪽(claude 수집기)·읽는 쪽(liveness)이 각자 리터럴을 들면 투영표에서 빠진
    마커가 화면에서 명령어로 승격되는데, 아무도 알아채지 못한다.
    """
    from acp.liveness import RUNNING_SIGNAL_PROCESS_RESUME

    assert RUNNING_SIGNAL_PROCESS_RESUME in INTERNAL_MARKERS
    assert MARKER_PROCESS_RECHECK in INTERNAL_MARKERS


def test_v5c_api_items_carry_evidence_kind(store: SessionStore) -> None:
    from acp.web.app import _sessions_payload

    store.upsert_session(
        SessionRecord(
            app="claude",
            session_id="marker",
            running_pid=4242,
            running_cmd=MARKER_PROCESS_RECHECK,
            source_file="test",
        ),
        SessionState.RUNNING,
    )
    store.upsert_session(
        SessionRecord(
            app="codex",
            session_id="cmd",
            running_pid=99,
            running_cmd="codex --resume",
            source_file="test",
        ),
        SessionState.RUNNING,
    )
    items = {item["session_id"]: item for item in _sessions_payload(store, limit=50)["items"]}
    assert items["claude:marker"]["execution_evidence_kind"] == EVIDENCE_PROCESS_RECHECK
    assert items["codex:cmd"]["execution_evidence_kind"] == EVIDENCE_COMMAND


# ── V9 — 값별 stale 판정(D1c) ────────────────────────────────────────


@pytest.mark.parametrize(
    ("signal", "expected"),
    [
        (PROCESS_SIGNAL_OK, SessionState.STALE),
        (PROCESS_SIGNAL_UNAVAILABLE, SessionState.UNKNOWN),
        (PROCESS_SIGNAL_UNKNOWN, SessionState.UNKNOWN),
        (PROCESS_SIGNAL_NOT_APPLICABLE, SessionState.UNKNOWN),
        # 계약 밖 값도 가용이 아니다. 이 값은 쓰기에서 거절되지만(사이클 기록 실패),
        # 폴러의 가용성 계산은 **저장 전 in-memory 행**에서 나오므로 경로가 실재한다.
        ("brand-new-signal", SessionState.UNKNOWN),
    ],
)
@pytest.mark.asyncio
async def test_v9_stale_is_confirmed_only_when_signal_was_observed(
    store: SessionStore, signal: str, expected: SessionState
) -> None:
    """관측했다고 말한 경우에만 STALE로 **확정**한다.

    옛 `!= "unavailable"` 분기는 `unknown`·`not_applicable`을 가용으로 읽어,
    화면이 `확인 불가`라 그리는 세션을 상태에서는 "사실상 종료"로 확정했다.
    mutation: 판정을 `!= "unavailable"`로 되돌리면 아래 두 케이스가 실패한다.
    """
    poller = Poller(store, _cfg(), _Broadcaster())
    poller.register(_SignalCollector([_stale_record()], signal=signal))
    await poller._tick()
    assert store.get_session("fake:s1")["state"] == expected


@pytest.mark.asyncio
async def test_v9b_synthesized_cycle_does_not_confirm_stale(store: SessionStore) -> None:
    """폴러가 방어 합성한 사이클(`process_signal=None`)도 가용이 아니다.

    합성이 아는 것은 "호출이 예외 없이 반환됐다"뿐이다.
    """
    poller = Poller(store, _cfg(), _Broadcaster())
    poller.register(_SignalCollector([_stale_record()], declare_cycle=False))
    await poller._tick()
    assert store.get_session("fake:s1")["state"] == SessionState.UNKNOWN
