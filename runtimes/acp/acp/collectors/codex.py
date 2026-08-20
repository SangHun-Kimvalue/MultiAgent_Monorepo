"""acp/collectors/codex.py — Codex Desktop 세션 수집기.

두 소스를 conversationId로 머지:
  1. ~/.codex/process_manager/chat_processes.json
     - 툴콜 서브프로세스 레지스트리 (배열)
     - 필드: conversationId · cwd · osPid · command · startedAtMs · updatedAtMs
     - ★ osPid = 개별 명령 서브프로세스 PID. RUNNING 확인 양성 신호로만 사용.
  2. ~/.codex/sessions/**/rollout-*.jsonl
     - 첫 줄: session_meta → cwd · model_provider · id
     - 꼬리: 마지막 event_msg → last_event (payload.type) · timestamp

불변 원칙:
  - read-only. 앱 파일 수정 금지.
  - 파싱 실패 → 해당 레코드 UNKNOWN + 경고 로깅. 전체 수집 중단 금지(C3).
  - jsonl 전체 로드 금지 — 끝부분만 tail (mtime 변동 시만 re-tail).
  - conversationId가 머지 키. 동일 cwd 다중 세션은 conversationId로 분리.
"""
from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from acp.collectors.base import (
    CAPABILITY_SUPPORTED,
    PROCESS_SIGNAL_OK,
    PROCESS_SIGNAL_UNAVAILABLE,
    STATUS_FAILED,
    STATUS_PARTIAL,
    BaseCollector,
    CollectCycle,
)
from acp.models import SessionRecord
from acp.timeutil import ms_to_dt

logger = logging.getLogger(__name__)

# 파일명에서 conversationId(UUID) 추출
_UUID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)

# jsonl tail 읽기 청크 크기 (bytes)
_TAIL_CHUNK = 8192
# 역방향으로 읽을 최대 줄 수 (1차 시도)
_TAIL_LINES = 30
# 1차 시도에서 event_msg 못 찾을 때 확대 재시도 줄 수 (상한 1회)
_TAIL_LINES_FALLBACK = 500


def _tail_lines(path: Path, n: int = _TAIL_LINES) -> tuple[list[str], bool, bool]:
    """파일 끝에서 최대 n줄을 읽는다(seek 방식, 전체 로드 금지).

    반환 `(lines, ok, truncated)`:
      - `ok`: **읽기 실패와 빈 파일을 구분**한다. 예전에는 둘 다 `[]`였고, 그래서 읽지
        못한 파일이 "이벤트 없는 정상 파일"과 같은 값이 됐다.
      - `truncated`: 창이 파일 **앞부분을 잘랐는지**. 잘렸다면 맨 앞 줄은 seek 경계에
        걸린 조각일 수 있어 파싱 실패를 결함으로 셀 수 없다. 잘리지 않았다면 그 줄은
        온전한 레코드이므로 실패가 곧 사실이다(구현리뷰 P1 — 무조건 면제는 틀렸다).
    """
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return [], True, False
            buf = b""
            pos = size
            while pos > 0:
                chunk_size = min(_TAIL_CHUNK, pos)
                pos -= chunk_size
                f.seek(pos)
                buf = f.read(chunk_size) + buf
                lines = buf.split(b"\n")
                # 마지막 빈 줄 제거
                if lines and lines[-1] == b"":
                    lines = lines[:-1]
                if len(lines) >= n:
                    break
            decoded = [line.decode("utf-8", errors="replace") for line in lines[-n:]]
            # **조각 가능성**은 `pos > 0`(바이트 경계에서 읽기 시작)일 때만이고,
            # 그때도 줄 수로 앞을 더 버렸다면(`len(lines) > n`) 반환 첫 줄은 온전하다.
            # `truncated`를 넓게 잡으면 온전한 줄의 malformed까지 면제해 진짜 결함을
            # 놓친다(구현리뷰 P1 — 이전 판정이 너무 넓었다).
            first_line_may_be_fragment = pos > 0 and len(lines) <= n
            return decoded, True, first_line_may_be_fragment
    except OSError as e:
        logger.warning("tail 읽기 실패: %s — %s", path.name, e)
        return [], False, False


_ms_to_dt = ms_to_dt


def _is_finite_number(value: Any) -> bool:
    """유한한 수인가. `bool`은 `int` 하위 타입이라 `type(...)`으로 배제한다.

    `NaN`·`Infinity`는 **타입은 float이지만 시각이 아니다**(구현리뷰 R5 P1). JSON은
    이 비표준 값을 기본 허용하고, `NaN`은 모든 비교가 False라 최신 선택이 조용히
    어긋나며 `Infinity`는 시각 변환에서 예외를 냈다. 타입만 보면 둘 다 통과한다.

    검사 자체도 실패할 수 있다(구현리뷰 R6 P1): 파이썬 int는 임의 정밀도라
    `math.isfinite(10**400)`이 `OverflowError`를 낸다. 그 예외가 새어 나가면 값 하나가
    **수집 사이클 전체를 죽여**, 멀쩡한 다른 대화의 실행 증거까지 미관측으로 왜곡한다.
    판별자는 판별에 실패해도 예외를 던지지 않는다 — 모르면 "유한하지 않음"이다.
    """
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


class CodexCollector(BaseCollector):
    """Codex Desktop 세션 수집기.

    sessions_base : ~/.codex/sessions
    processes_path: ~/.codex/process_manager/chat_processes.json
    """

    def __init__(self, sessions_base: Path, processes_path: Path) -> None:
        self._sessions_base = sessions_base
        self._processes_path = processes_path
        # mtime 캐시: {str(path): float(mtime)}
        self._mtime_cache: dict[str, float] = {}
        # 파싱 캐시: {str(path): {conv_id, cwd, model, last_event, last_activity_ts, source_file}}
        self._session_cache: dict[str, dict[str, Any]] = {}
        self._last_cycle = CollectCycle(
            app="codex",
            signal_quality="session-events",
            process_signal_capability=CAPABILITY_SUPPORTED,
        )
        # 스캔 중 읽지 못한 **대화 id 집합**. 단위는 파일이 아니라 대화 id다
        # (같은 대화의 rollout 파일이 여러 개면 1건으로 센다 — 구현리뷰 P2).
        self._scan_failed_ids: set[str] = set()

    @property
    def app_name(self) -> str:
        return "codex"

    @property
    def last_cycle(self) -> CollectCycle:
        """직전 collect()의 완결성 사실(T14 S4b D2a)."""
        return self._last_cycle

    def collect(self) -> list[SessionRecord]:
        """두 소스를 읽고 SessionRecord 목록 반환.

        **완결성을 선언한다**(T14 S4b D2e). codex는 두 소스를 읽는다:
        필수(sessions 디렉터리)와 보조(`chat_processes.json`). 등급이 다르다 —
        필수가 없으면 수집 자체가 성립하지 않고(`failed`), 보조만 없으면 세션은
        모았지만 실행 신호 축이 빠진다(`partial`). 예전에는 둘 다 조용한 빈 결과였다.
        """
        cycle = CollectCycle(
            app="codex",
            observed_at=datetime.now(timezone.utc),
            signal_quality="session-events",
            # `chat_processes.json`의 `osPid`로 `running_pid`를 채운다 — 실행 신호를
            # **줄 수 있는** 앱이다. 조인 카운트를 세지 않는 것(아래 not_applicable)과
            # 신호를 줄 수 있는가는 다른 축이다(T14 S4c-1 D1).
            process_signal_capability=CAPABILITY_SUPPORTED,
        )
        self._last_cycle = cycle
        self._scan_failed_ids = set()
        # 이 수집기는 보관·실행신호 조인 축을 세지 않는다 — "0건"이 아니라 "해당 없음".
        cycle.mark_not_applicable(
            "excluded_archived", "matched", "unmatched", "ambiguous", "malformed_uuid"
        )

        if not self._sessions_base.exists():
            logger.warning("Codex sessions 폴더 없음: %s", self._sessions_base)
            cycle.status = STATUS_FAILED
            return []
        if not self._processes_path.exists():
            # 보조 소스만 빠졌다 — 세션은 모을 수 있으므로 실패가 아니라 부분이다.
            cycle.status = STATUS_PARTIAL

        proc_map = self._load_chat_processes()   # conv_id → latest proc entry
        jsonl_map = self._scan_sessions()         # conv_id → session data

        all_ids = set(proc_map) | set(jsonl_map)
        records: list[SessionRecord] = []

        merge_failed_ids: set[str] = set()
        for conv_id in all_ids:
            try:
                records.append(self._merge(conv_id, proc_map.get(conv_id), jsonl_map.get(conv_id)))
            except Exception as e:
                merge_failed_ids.add(conv_id)
                logger.warning("레코드 머지 실패 [%s]: %s", conv_id, e)

        # 센 축만 선언한다. 건너뛴 레코드를 세지 않으면 그 실패가 조용히 사라진다.
        # 같은 대화가 스캔·머지 양쪽에서 실패해도 **1건**이다. 단위 = 대화 id.
        failed = len(merge_failed_ids | self._scan_failed_ids)
        cycle.declare(collected=len(records), failed=failed)
        if failed:
            cycle.status = STATUS_PARTIAL
        logger.debug("CodexCollector: %d 세션 수집(실패 %d)", len(records), failed)
        return records

    # ── 내부 메서드 ─────────────────────────────────────────────────────

    def _merge(
        self,
        conv_id: str,
        proc: dict[str, Any] | None,
        sess: dict[str, Any] | None,
    ) -> SessionRecord:
        proc = proc or {}
        sess = sess or {}

        # last_activity: jsonl 이벤트 ts 우선, 없으면 updatedAtMs
        last_act: datetime | None = sess.get("last_activity_ts")
        if last_act is None:
            last_act = _ms_to_dt(proc.get("updatedAtMs"))

        return SessionRecord(
            app="codex",
            session_id=conv_id,
            project_path=sess.get("cwd") or proc.get("cwd"),
            model=sess.get("model_provider"),
            last_activity=last_act,
            running_pid=proc.get("osPid"),
            running_cmd=proc.get("command"),
            last_event=sess.get("last_event"),
            source_file=sess.get("source_file") or str(self._processes_path),
        )

    def _load_chat_processes(self) -> dict[str, dict[str, Any]]:
        """chat_processes.json (배열) 파싱 → {conv_id: 최신 항목}.

        이 파일이 **실행 신호의 소스**다. 그래서 관측 결과를 `process_signal`로
        명시 선언한다(T14 S4c-1 D1b) — 예전에는 소스를 못 읽어도 사이클의
        `process_signal`이 기본값 `"ok"`로 남아, 화면이 "안 돌고 있음"이라고
        **관측하지 않은 사실을 단언**했다.
        """
        if not self._processes_path.exists():
            logger.warning("chat_processes.json 없음: %s", self._processes_path)
            self._last_cycle.process_signal = PROCESS_SIGNAL_UNAVAILABLE
            return {}

        try:
            items: list[dict[str, Any]] = json.loads(
                self._processes_path.read_text(encoding="utf-8", errors="replace")
            )
        except Exception as e:
            # 파일은 있는데 읽거나 파싱하지 못했다. 부재와 마찬가지로 **보조 축이 빠진**
            # 것이므로 빈 결과로 축약하지 않고 부분 수집으로 선언한다(구현리뷰 P1).
            logger.warning("chat_processes.json 파싱 실패: %s", e)
            self._last_cycle.status = STATUS_PARTIAL
            self._last_cycle.process_signal = PROCESS_SIGNAL_UNAVAILABLE
            return {}

        if not isinstance(items, list):
            # 유효 JSON이지만 배열이 아니다. 예전에는 아래 `.get()`에서 예외가 나
            # **수집 전체가 죽었다**(구현리뷰 P1).
            logger.warning("chat_processes.json 형식 불일치(배열 아님): %s", self._processes_path)
            self._last_cycle.status = STATUS_PARTIAL
            self._last_cycle.process_signal = PROCESS_SIGNAL_UNAVAILABLE
            return {}

        # 여기까지 왔으면 소스를 **읽었다**. 다만 개별 원소가 깨지면 아래에서 다시
        # `unavailable`로 내린다 — `process_signal`은 **앱 단위 플래그**라 "일부 대화의
        # 신호만 못 봤다"를 표현할 수 없기 때문이다(구현리뷰 R4 P1). 앱 전체에 대해
        # `ok`라고 말하면, 증거를 못 본 대화가 화면에서 "안 돌고 있음"으로 그려지고
        # STALE 확정까지 허용된다 — 관측 실패를 관측 결과로 바꿔 말하는 것이다.
        # 설계 D1b의 계약도 "형식 오류 → unavailable"이다.
        self._last_cycle.process_signal = PROCESS_SIGNAL_OK
        result: dict[str, dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict):
                logger.warning("chat_processes 항목 형식 불일치(객체 아님)")
                self._last_cycle.status = STATUS_PARTIAL
                self._last_cycle.process_signal = PROCESS_SIGNAL_UNAVAILABLE
                continue
            # 원소 **내부 필드 형식도** 검사한다. 예전에는 `conversationId=[]`가
            # dict 키 조회에서 `TypeError: unhashable type`을, 비교 불가한
            # `updatedAtMs`가 `>` 비교에서 TypeError를 내 **수집 전체가 죽었다**.
            # 개별 malformed 레코드는 건너뛰고 사이클을 partial로 선언한다(구현리뷰 P1).
            conv_id = item.get("conversationId")
            if conv_id is None or conv_id == "":
                continue
            if not isinstance(conv_id, str):
                logger.warning("chat_processes conversationId 형식 불일치: %r", type(conv_id))
                self._last_cycle.status = STATUS_PARTIAL
                # 이 항목의 실행 증거는 통째로 버려진다 — 앱 단위 신호를 내린다.
                self._last_cycle.process_signal = PROCESS_SIGNAL_UNAVAILABLE
                continue
            updated = item.get("updatedAtMs", 0)
            if not _is_finite_number(updated):
                logger.warning("chat_processes updatedAtMs 형식 불일치 [%s]", conv_id)
                self._last_cycle.status = STATUS_PARTIAL
                # pid/command는 살아남지만 소스가 형식 계약을 어겼다. 살린 값만 보고
                # "정상 관측"이라 말하지 않는다(fail-closed).
                self._last_cycle.process_signal = PROCESS_SIGNAL_UNAVAILABLE
                # 정렬 비교용으로만 0을 쓴다(동률일 때 깨진 값이 살아남지 않게).
                updated = 0
                # 저장 항목에서는 깨진 키를 **지운다**. 예전처럼 0을 심으면 하류가
                # 그것을 시각으로 읽어 "1970-01-01에 마지막 활동"이라는, 우리가 모르는
                # 사실을 단언한다. 모르는 시각은 `None`이어야 한다(S4c-1 R5 실측).
                item = {key: value for key, value in item.items() if key != "updatedAtMs"}
            # 같은 conversationId는 최신 updatedAtMs 우선
            previous = result.get(conv_id)
            prev_updated = previous.get("updatedAtMs", 0) if previous else 0
            if not _is_finite_number(prev_updated):
                prev_updated = 0
            if previous is None or updated > prev_updated:
                result[conv_id] = item

        logger.debug("chat_processes: %d 대화 항목 로드", len(result))
        return result

    def _scan_sessions(self) -> dict[str, dict[str, Any]]:
        """sessions 폴더를 재귀 순회, mtime 캐시로 변동 파일만 re-tail."""
        result: dict[str, dict[str, Any]] = {}

        if not self._sessions_base.exists():
            logger.warning("Codex sessions 폴더 없음: %s", self._sessions_base)
            return result

        for jf in self._sessions_base.rglob("rollout-*.jsonl"):
            m = _UUID_RE.search(jf.stem)
            if not m:
                continue
            conv_id = m.group(1)

            try:
                mtime = jf.stat().st_mtime
            except OSError:
                # 읽지 못한 파일은 **세고 넘어간다**. 조용히 건너뛰면 그 세션이 없었던
                # 것처럼 되고, 사이클은 완전하다고 고지한다(구현리뷰 P1).
                self._scan_failed_ids.add(conv_id)
                continue

            cache_key = str(jf)
            if (
                cache_key in self._mtime_cache
                and self._mtime_cache[cache_key] == mtime
                and cache_key in self._session_cache
            ):
                # 변경 없음 — 캐시 재사용
                cached = self._session_cache[cache_key]
                # 같은 conv_id의 최신 파일 우선 (mtime 기준)
                existing = result.get(conv_id)
                if existing is None or mtime > self._mtime_cache.get(str(existing.get("_path", "")), 0):
                    result[conv_id] = cached
                continue

            meta, meta_ok = self._read_session_meta(jf)
            last_ev, last_ts, tail_ok = self._read_last_event(jf)
            # `failed`의 단위는 **실패 대화 수**다(구현리뷰 P2). 작업 수로 세면 한
            # 파일이 메타·tail 양쪽에서 실패할 때 2건으로 부풀고, 파일 수로 세면 같은
            # 대화의 rollout이 여러 개일 때 대화 단위 집계와 어긋난다.
            parsed_clean = meta_ok and tail_ok
            if not parsed_clean:
                self._scan_failed_ids.add(conv_id)

            data: dict[str, Any] = {
                "cwd": meta.get("cwd"),
                "model_provider": meta.get("model_provider"),
                "last_event": last_ev,
                "last_activity_ts": last_ts,
                "source_file": str(jf),
                "_path": str(jf),
                "_mtime": mtime,
            }
            if parsed_clean:
                # **깨진 파일은 캐시하지 않는다**(구현리뷰 P1). 캐시하면 다음 사이클이
                # 재파싱 없이 그 데이터를 쓰면서 `success_complete, failed=0`으로
                # 고지한다 — 첫 사이클의 실패가 조용히 세탁된다.
                self._mtime_cache[cache_key] = mtime
                self._session_cache[cache_key] = data

            # 같은 conv_id 중 더 최신 파일로 덮어쓰기
            existing = result.get(conv_id)
            if existing is None or mtime > existing.get("_mtime", 0):
                result[conv_id] = data

        logger.debug("jsonl scan: %d 세션 파일에서 %d 대화 추출", len(self._session_cache), len(result))
        return result

    def _read_session_meta(self, path: Path) -> tuple[dict[str, Any], bool]:
        """첫 줄(session_meta)을 읽어 `(payload, ok)` 반환.

        실패를 빈 dict로 축약하지 않는다 — 읽지 못한 메타를 "값이 없던 정상 레코드"로
        흘리면 그 파일이 완전한 것처럼 집계된다(구현리뷰 P1).
        """
        try:
            with path.open(encoding="utf-8", errors="replace") as f:
                first = f.readline()
            j = json.loads(first)
            if not isinstance(j, dict):
                logger.warning("session_meta 형식 불일치(객체 아님): %s", path.name)
                return {}, False
            if j.get("type") == "session_meta":
                payload = j.get("payload", {})
                if not isinstance(payload, dict):
                    # 확인하지 못한 메타를 "정상적인 빈 메타"로 축약하지 않는다.
                    logger.warning("session_meta payload 형식 불일치: %s", path.name)
                    return {}, False
                return payload, True
            # 유효한 객체지만 session_meta가 아니다 → 메타를 **얻지 못했다**.
            # 빈 dict를 "정상적인 빈 메타"로 돌려주면 cwd·model 미확인이 정상 수집으로
            # 세탁된다(구현리뷰 P1). 미확인을 정상이라 부르지 않는다.
            logger.warning("첫 레코드가 session_meta가 아님: %s", path.name)
            return {}, False
        except Exception as e:
            logger.warning("session_meta 파싱 실패: %s — %s", path.name, e)
            return {}, False
        return {}, False

    def _read_last_event(self, path: Path) -> tuple[str | None, datetime | None, bool]:
        """jsonl 꼬리에서 마지막 event_msg의 payload.type과 timestamp 추출.

        신호는 **마지막 이벤트 타입 1개**로 판정 — started/complete 카운트 짝맞춤 금지.
        1차: 끝 30줄. event_msg 없으면 500줄로 확대 재시도(상한 1회). 전체 로드 금지.
        """
        lines, ok, truncated = _tail_lines(path, n=_TAIL_LINES)
        if not ok:
            return None, None, False
        ev, ts, malformed = self._scan_for_last_event(lines, truncated=truncated)
        if ev is not None:
            return ev, ts, not malformed
        # 1차 30줄에서 event_msg 미발견 → 윈도우 확대 1회 재시도
        logger.debug("tail 확대 재시도: %s", path.name)
        lines, ok, truncated = _tail_lines(path, n=_TAIL_LINES_FALLBACK)
        if not ok:
            return None, None, False
        ev, ts, malformed = self._scan_for_last_event(lines, truncated=truncated)
        return ev, ts, not malformed

    @staticmethod
    def _scan_for_last_event(
        lines: list[str], *, truncated: bool = True
    ) -> tuple[str | None, datetime | None, bool]:
        """역순으로 마지막 event_msg를 찾아 `(payload.type, timestamp, malformed)` 반환.

        `truncated=True`(창이 파일 앞부분을 잘랐다)일 때만 **창의 첫 줄을 면제**한다 —
        그 줄은 seek 경계에 걸린 조각일 수 있고, 정상 파일에서도 일어나는 일이라
        실패로 세면 건강한 세션마다 거짓 실패가 난다. 잘리지 않았다면 그 줄도 온전한
        레코드이므로 파싱 실패는 사실이다(구현리뷰 P1 — 무조건 면제는 틀렸다).

        **범위 한정**: 최신 `event_msg`를 만나면 즉시 멈추므로, 그보다 오래된 줄의
        malformed는 보지 않는다. 이 값은 "파일 전체의 malformed 수"가 아니라
        **"이번 스캔에서 만난 malformed 여부"**다. 그 이상을 주장하지 않는다.
        """
        malformed = False
        for index, line in enumerate(reversed(lines)):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                j = json.loads(stripped)
                if not isinstance(j, dict):
                    # JSON 문법은 유효하지만 객체가 아니다. 예전에는 `j.get(...)`이
                    # AttributeError를 내 **수집 전체가 죽었다** — 개별 레코드 실패가
                    # 사이클 전체 실패로 번지면 안 된다(구현리뷰 P1).
                    if not (truncated and index == len(lines) - 1):
                        malformed = True
                    continue
                if j.get("type") == "event_msg":
                    # payload·timestamp의 **형식도 검사**한다. 예전에는 `payload=null`이나
                    # 숫자 timestamp가 `.get()`/`.replace()`에서 AttributeError를 내
                    # 수집 전체를 죽였다 — 개별 레코드 실패가 사이클 전체 실패로 번지면
                    # 안 된다(구현리뷰 P1).
                    payload = j.get("payload")
                    if not isinstance(payload, dict):
                        malformed = True
                        continue
                    ev_type: str | None = payload.get("type")
                    raw_ts = j.get("timestamp")
                    ts: datetime | None = None
                    if isinstance(raw_ts, str) and raw_ts:
                        try:
                            ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                        except ValueError:
                            pass
                    elif raw_ts is not None:
                        malformed = True
                    return ev_type, ts, malformed
            except json.JSONDecodeError:
                # 역순 순회이므로 창의 첫 줄 = 마지막 인덱스. 잘린 창에서만 면제한다.
                if not (truncated and index == len(lines) - 1):
                    malformed = True
                continue
        return None, None, malformed
