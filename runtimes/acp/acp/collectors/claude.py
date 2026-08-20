"""acp/collectors/claude.py — Claude Desktop(claude-code-sessions) 세션 수집기 (P2).

소스: %APPDATA%/Claude/claude-code-sessions/**/local_*.json
실측 형상(2026-06-10): top-level 키
  sessionId · cliSessionId · cwd · originCwd · createdAt · lastActivityAt(ms epoch)
  · model · isArchived · title · planPath · completedTurns

불변 원칙:
  - read-only. 앱 파일 수정 금지.
  - 개별 파일 파싱 실패 → 그 레코드만 skip + 경고. 전체 수집 중단 금지(C3).
  - 실행 신호(T14 S3 D2): 프로세스 커맨드라인의 `--resume=<uuid>`와 파일의 `cliSessionId`를
    조인해 running_pid를 채운다. **증거가 유효할 때만** 채우고, 스냅샷이 실패하면
    이월하지 않는다(PID 재사용으로 거짓 RUNNING이 되는 것을 막는다).
  - archived 세션(T14 S4a D2): **제외하지 않는다.** `archived=True`로 표시해 넘기고,
    분류·표시 범위는 저장소와 API가 정한다. S3처럼 여기서 버리면 이미 저장된 과거 행이
    영원히 분류되지 않아 총계에 남으면서 API는 "제외했다"고 고지하게 된다.
  - last_event 없음(Codex 전용 신호) → derive_state는 시간기반 폴백으로 처리.
  - title/completedTurns/planPath 등 부가 필드는 미수집(YAGNI — 보드 가치는 PHASE 조인에서).
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from acp.collectors.base import (
    CAPABILITY_SUPPORTED,
    PROCESS_SIGNAL_OK,
    PROCESS_SIGNAL_UNAVAILABLE,
    STATUS_FAILED,
    STATUS_PARTIAL,
    BaseCollector,
    CollectCycle,
)
from acp.evidence import MARKER_PROCESS_RECHECK
from acp.models import SessionRecord
from acp.proc import CliProcess, ProcessSnapshot, ProcessSnapshotCache, SnapshotStatus
from acp.timeutil import ms_to_dt

logger = logging.getLogger(__name__)



class ClaudeSessionCollector(BaseCollector):
    """Claude claude-code-sessions 수집기.

    sessions_base: %APPDATA%/Claude/claude-code-sessions
    """

    def __init__(
        self,
        sessions_base: Path,
        *,
        include_archived: bool = False,
        process_snapshot: Callable[[], ProcessSnapshot] | None = None,
        signal_freshness_seconds: float = 30.0,
    ) -> None:
        self._sessions_base = sessions_base
        # T14 S4a 이후 수집은 **항상** 보관 세션을 반환한다(플래그로 표시). 이 인자는
        # 더 이상 저장 범위를 제어하지 않으며, 표시 범위는 API `archived` 파라미터가
        # 정한다. 조용한 no-op이 되지 않도록 config 계층이 경고를 낸다(D6).
        self._include_archived = include_archived
        # 관측이 이 한도를 넘으면 증거로 쓰지 않는다(T14 S3 D1b) — 캐시가 오래된 스냅샷을
        # 돌려줄 수 있으므로, 소비 시점에도 freshness를 검사한다.
        self._signal_freshness = signal_freshness_seconds
        # 수집기는 OS를 직접 호출하지 않는다 — 스냅샷 제공자를 주입받는다(T14 S3 D1d).
        self._process_snapshot = process_snapshot or ProcessSnapshotCache().get
        self._last_cycle = CollectCycle(
            app="claude", process_signal_capability=CAPABILITY_SUPPORTED
        )

    @property
    def last_cycle(self) -> "CollectCycle":
        """직전 collect()의 사이클 사실. 0건 수집과 수집 실패를 구분해 담는다."""
        return self._last_cycle

    @property
    def app_name(self) -> str:
        return "claude"

    def collect(self) -> list[SessionRecord]:
        cycle = CollectCycle(
            app="claude",
            observed_at=datetime.now(timezone.utc),
            # 이 앱은 프로세스 조인으로 실행 신호를 **줄 수 있다**. 이번 사이클에 실제로
            # 얻었는지는 아래 `process_signal`이 따로 말한다(두 축을 섞지 않는다).
            process_signal_capability=CAPABILITY_SUPPORTED,
        )
        self._last_cycle = cycle

        if not self._sessions_base.exists():
            logger.warning("Claude sessions 폴더 없음: %s", self._sessions_base)
            cycle.status = STATUS_FAILED
            # 소스가 없으면 아무 축도 세지 못했다 — 0으로 단언하지 않는다(전 축 unknown 유지).
            # `process_signal`도 기본값 `unknown` 그대로다: 스냅샷을 조회조차 하지 않았으므로
            # "정상 관측"도 "관측 실패"도 아니다.
            return []

        # 실행 신호는 사이클마다 **한 번** 조회하고, 실패하면 증거를 만들지 않는다.
        snapshot = self._safe_snapshot()
        fresh = self._is_fresh(snapshot, cycle.observed_at)
        cycle.process_signal = PROCESS_SIGNAL_OK if fresh else PROCESS_SIGNAL_UNAVAILABLE
        cycle.process_observed_at = snapshot.observed_at if fresh else None
        by_uuid, ambiguous = self._index_by_uuid(snapshot) if fresh else ({}, 0)
        cycle.ambiguous = ambiguous
        cycle.malformed_uuid = snapshot.malformed_uuid if fresh else 0

        records: list[SessionRecord] = []
        for jf in self._sessions_base.rglob("local_*.json"):
            outcome = self._parse_one(jf, by_uuid)
            if outcome is None:
                cycle.failed += 1
            else:
                if outcome.archived:
                    cycle.excluded_archived += 1
                records.append(outcome)

        # 이 수집기가 **실제로 센** 축만 선언한다. 나머지는 unknown으로 남는다.
        cycle.declare(
            collected=len(records),
            failed=cycle.failed,
            excluded_archived=cycle.excluded_archived,
            matched=cycle.matched,
            unmatched=cycle.unmatched,
            ambiguous=cycle.ambiguous,
            malformed_uuid=cycle.malformed_uuid,
        )
        if cycle.failed:
            cycle.status = STATUS_PARTIAL
        logger.info(
            "claude 수집: %d건(실패 %d, archived %d, 실행신호 %s, 매칭 %d/모호 %d)",
            cycle.collected,
            cycle.failed,
            cycle.excluded_archived,
            cycle.process_signal,
            cycle.matched,
            cycle.ambiguous,
        )
        return records

    def _safe_snapshot(self) -> ProcessSnapshot:
        """스냅샷 조회 실패가 수집 전체를 죽이지 않는다(C3). 실패는 데이터로 변환."""
        try:
            return self._process_snapshot()
        except Exception as exc:  # 주입된 제공자의 예기치 못한 실패
            logger.warning("프로세스 스냅샷 조회 실패: %s", type(exc).__name__)
            return ProcessSnapshot(
                SnapshotStatus.FAILED, datetime.now(timezone.utc), error=type(exc).__name__
            )

    def _is_fresh(self, snapshot: ProcessSnapshot, now: datetime | None) -> bool:
        """성공 스냅샷이어도 **한도를 넘긴 관측**은 증거로 쓰지 않는다(T14 S3 D1b)."""
        if not snapshot.ok:
            return False
        reference = now or datetime.now(timezone.utc)
        age = (reference - snapshot.observed_at).total_seconds()
        return -1.0 <= age <= self._signal_freshness

    @staticmethod
    def _index_by_uuid(snapshot: ProcessSnapshot) -> tuple[dict[str, CliProcess], int]:
        """uuid → 프로세스. 같은 uuid에 여러 프로세스면 **가장 작은 pid**를 결정론적으로 택한다.

        모호(중복) 건수를 함께 돌려준다 — 조용히 하나를 고르고 끝내면 사실이 사라진다.
        """
        index: dict[str, CliProcess] = {}
        ambiguous = 0
        for proc in snapshot.processes:
            if not proc.session_uuid:
                continue
            current = index.get(proc.session_uuid)
            if current is None:
                index[proc.session_uuid] = proc
                continue
            ambiguous += 1
            if proc.pid < current.pid:
                index[proc.session_uuid] = proc
        return index, ambiguous

    def _parse_one(
        self, path: Path, by_uuid: dict[str, CliProcess]
    ) -> "SessionRecord | None":
        """local_*.json 1개 → SessionRecord.

        반환: 레코드(보관 여부는 `archived` 필드) / `None`(파싱 실패 — 경고, C3).

        **보관 세션도 레코드로 흘린다**(T14 S4a D2). S3는 여기서 건너뛰었는데, 그러면
        이미 저장된 과거 행은 영원히 분류되지 않아 총계에 그대로 남으면서 API는
        "제외했다"고 고지했다 — 일어나지 않은 위생 조치를 단언한 것이다.
        """
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Claude 세션 파싱 실패(skip): %s — %s", path.name, e)
            return None

        if not isinstance(data, dict):
            logger.warning("Claude 세션 형식 불일치(skip): %s", path.name)
            return None

        session_id = data.get("sessionId")
        if not session_id:
            logger.warning("Claude 세션 sessionId 없음(skip): %s", path.name)
            return None

        # 보관 사실은 **행에 기록**한다(건너뛰지 않는다). 분류돼야 총계에서 뺄 수 있다.
        archived = data.get("isArchived") is True

        # 실행 증거: 이번 성공 스냅샷에서 uuid↔pid가 재확인된 경우에만 채운다.
        cli_session_id = data.get("cliSessionId")
        proc = by_uuid.get(str(cli_session_id).lower()) if cli_session_id else None
        if proc is not None:
            self._last_cycle.matched += 1
        elif self._last_cycle.process_signal == PROCESS_SIGNAL_OK:
            # 신호를 **관측한** 사이클에서 매칭되지 않았다는 사실을 센다(대개 종료된 세션).
            # `by_uuid`가 비어 있을 때 세지 않으면 "프로세스 0개" 사이클에서 모든 미매칭이
            # 조용히 사라진다.
            self._last_cycle.unmatched += 1

        try:
            return SessionRecord(
                app="claude",
                session_id=str(session_id),
                project_path=data.get("cwd"),
                # 파일의 model이 권위. 없을 때만 커맨드라인 값으로 보강한다.
                model=data.get("model") or (proc.model if proc else None),
                last_activity=ms_to_dt(data.get("lastActivityAt")),
                running_pid=proc.pid if proc else None,
                # 원문 커맨드라인을 넣지 않는다(토큰·프롬프트 유출 방지). 증거의 **출처**만 밝힌다.
                # 리터럴이 아니라 마커 단일 출처를 쓴다 — 표에 없는 마커가 화면에서
                # 명령어로 승격되지 않도록(T14 S4c-1 D3).
                running_cmd=MARKER_PROCESS_RECHECK if proc else None,
                last_event=None,
                source_file=str(path),
                archived=archived,
            )
        except Exception as e:  # pydantic 검증 등 — 레코드만 skip
            logger.warning("Claude SessionRecord 생성 실패(skip): %s — %s", path.name, e)
            return None
