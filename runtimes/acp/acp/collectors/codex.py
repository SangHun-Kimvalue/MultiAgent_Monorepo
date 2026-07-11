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
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from acp.collectors.base import BaseCollector
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


def _tail_lines(path: Path, n: int = _TAIL_LINES) -> list[str]:
    """파일 끝에서 최대 n줄을 효율적으로 읽는다(seek 방식, 전체 로드 금지)."""
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return []
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
            return [line.decode("utf-8", errors="replace") for line in lines[-n:]]
    except OSError as e:
        logger.warning("tail 읽기 실패: %s — %s", path.name, e)
        return []


_ms_to_dt = ms_to_dt


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

    @property
    def app_name(self) -> str:
        return "codex"

    def collect(self) -> list[SessionRecord]:
        """두 소스를 읽고 SessionRecord 목록 반환."""
        proc_map = self._load_chat_processes()   # conv_id → latest proc entry
        jsonl_map = self._scan_sessions()         # conv_id → session data

        all_ids = set(proc_map) | set(jsonl_map)
        records: list[SessionRecord] = []

        for conv_id in all_ids:
            try:
                records.append(self._merge(conv_id, proc_map.get(conv_id), jsonl_map.get(conv_id)))
            except Exception as e:
                logger.warning("레코드 머지 실패 [%s]: %s", conv_id, e)

        logger.debug("CodexCollector: %d 세션 수집", len(records))
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
        """chat_processes.json (배열) 파싱 → {conv_id: 최신 항목}."""
        if not self._processes_path.exists():
            logger.warning("chat_processes.json 없음: %s", self._processes_path)
            return {}

        try:
            items: list[dict[str, Any]] = json.loads(
                self._processes_path.read_text(encoding="utf-8", errors="replace")
            )
        except Exception as e:
            logger.warning("chat_processes.json 파싱 실패: %s", e)
            return {}

        result: dict[str, dict[str, Any]] = {}
        for item in items:
            conv_id = item.get("conversationId")
            if not conv_id:
                continue
            updated = item.get("updatedAtMs", 0)
            # 같은 conversationId는 최신 updatedAtMs 우선
            if conv_id not in result or updated > result[conv_id].get("updatedAtMs", 0):
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

            self._mtime_cache[cache_key] = mtime

            meta = self._read_session_meta(jf)
            last_ev, last_ts = self._read_last_event(jf)

            data: dict[str, Any] = {
                "cwd": meta.get("cwd"),
                "model_provider": meta.get("model_provider"),
                "last_event": last_ev,
                "last_activity_ts": last_ts,
                "source_file": str(jf),
                "_path": str(jf),
                "_mtime": mtime,
            }
            self._session_cache[cache_key] = data

            # 같은 conv_id 중 더 최신 파일로 덮어쓰기
            existing = result.get(conv_id)
            if existing is None or mtime > existing.get("_mtime", 0):
                result[conv_id] = data

        logger.debug("jsonl scan: %d 세션 파일에서 %d 대화 추출", len(self._session_cache), len(result))
        return result

    def _read_session_meta(self, path: Path) -> dict[str, Any]:
        """첫 줄(session_meta)을 읽어 payload 반환."""
        try:
            with path.open(encoding="utf-8", errors="replace") as f:
                first = f.readline()
            j = json.loads(first)
            if j.get("type") == "session_meta":
                payload = j.get("payload", {})
                return payload if isinstance(payload, dict) else {}
        except Exception as e:
            logger.warning("session_meta 파싱 실패: %s — %s", path.name, e)
        return {}

    def _read_last_event(self, path: Path) -> tuple[str | None, datetime | None]:
        """jsonl 꼬리에서 마지막 event_msg의 payload.type과 timestamp 추출.

        신호는 **마지막 이벤트 타입 1개**로 판정 — started/complete 카운트 짝맞춤 금지.
        1차: 끝 30줄. event_msg 없으면 500줄로 확대 재시도(상한 1회). 전체 로드 금지.
        """
        result = self._scan_for_last_event(_tail_lines(path, n=_TAIL_LINES))
        if result[0] is not None:
            return result
        # 1차 30줄에서 event_msg 미발견 → 윈도우 확대 1회 재시도
        logger.debug("tail 확대 재시도: %s", path.name)
        return self._scan_for_last_event(_tail_lines(path, n=_TAIL_LINES_FALLBACK))

    @staticmethod
    def _scan_for_last_event(lines: list[str]) -> tuple[str | None, datetime | None]:
        """줄 목록에서 역순으로 마지막 event_msg를 찾아 (payload.type, timestamp) 반환."""
        for line in reversed(lines):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                j = json.loads(stripped)
                if j.get("type") == "event_msg":
                    ev_type: str | None = j.get("payload", {}).get("type")
                    ts_str: str | None = j.get("timestamp")
                    ts: datetime | None = None
                    if ts_str:
                        try:
                            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        except ValueError:
                            pass
                    return ev_type, ts
            except json.JSONDecodeError:
                continue
        return None, None
