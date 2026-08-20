"""acp/liveness.py — 세션 상태 판정 (순수함수, 결정적).

핵심 불변식:
- derive_state(record, now, cfg, is_alive) 는 **순수함수**.
  - 내부에서 datetime.now() / OS 프로세스 조회 호출 금지 → 주입(테스트 mock 가능).
  - 부작용 없음, DB 접근 없음.
- 결정테이블 (last_event + is_alive + age) — 구간 규칙은 **모든 경로에서 동일**:
  `[idle, hold)` = IDLE 계열, `[hold, stale)` = HOLDING, `[stale, ∞)` = STALE.

  last_event                        | is_alive | age            | → state
  ─────────────────────────────────────────────────────────────────────────
  error                             |    —     |     —          | ERROR
  *_approval_request                |    —     |     —          | HOLDING (아래 ※)
  턴 종료(task_complete/turn_aborted/  |   —     | < idle         | LIVE
   task_aborted)                    |    —     | [idle, hold)   | IDLE
                                    |    —     | [hold, stale)  | HOLDING (ZTR 망각)
                                    |    —     | ≥ stale        | STALE
  in-turn (task_started/agent_message/ |  True   |     —          | RUNNING
   token_count/patch_apply_end/     |  False   | ≥ stale        | STALE
   context_compacted/user_message/  |  False   | [hold, stale)  | HOLDING
   그 외 non-None event_msg)        |  False   | < hold         | RUNNING(잠정)
  last_event=None, 재확인 PID 생존   |   True   |     —          | RUNNING (S3: 스냅샷 증거)
  last_event=None, raw_status 있음  |    —     |     —          | DONE/ERROR (폴백)
  last_event=None, 시간 기반        |    —     | < idle         | LIVE
                                    |    —     | [idle, hold)   | IDLE
                                    |    —     | [hold, stale)  | HOLDING
                                    |    —     | ≥ stale        | STALE

  UNKNOWN이 남는 경로는 **판정 불가 셋**: last_activity 없음 / elapsed < 0(시계 역전) /
  `process_signal_available=False`에서 stale 구간(= 종료를 관측하지 못함, T14 S3 D1a-2).

  ★ "in-turn"은 task_started 리터럴이 아니라 "턴 종료·에러·승인 이외의 모든 non-None
    event_msg"로 일반화. 실제 진행중 세션의 마지막 event_msg는 대개 agent_message/token_count.

  ★ HOLDING의 실제 의미는 **"무응답(stalled)"**이지 "승인 대기"가 아니다(T14 S2 D5).
    ※ 실측(codex rollout 로그 60파일 전수)에서 approval 계열 event_msg는 **0건**이라
    승인 분기는 현재 어휘에서 발화하지 않는다. 화면에 보이는 HOLDING은 전부
    "턴이 진행 중이거나 종료됐는데 hold 임계를 넘도록 소식이 없음"이다.

  ★ abort 어휘는 ERROR가 아니라 **턴 종료**로 라우팅한다(T14 S2 D1). `turn_aborted`는
    턴이 완료 없이 끝났다는 사실만 말하고 원인을 말하지 않으므로 장애로 단언하지 않는다.
    같은 뜻의 두 철자(`turn_aborted`/`task_aborted`)가 서로 다른 축으로 가면 그 자체가
    새 불일치이므로 함께 묶는다. 실측 어휘: `turn_aborted`만 관측(`task_aborted` 0건).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from acp.config import LivenessConfig
from acp.evidence import MARKER_PROCESS_RECHECK
from acp.models import SessionRecord, SessionState

# 턴이 끝난 이벤트 타입 — 정상 완료와 terminal abort를 함께 본다.
# abort는 "완료 없이 끝났다"는 사실만 말하므로 장애 축이 아니라 종료 축이다(T14 S2 D1).
_TURN_END_EVENTS = frozenset({"task_complete", "turn_aborted", "task_aborted"})
# 명시적 오류 이벤트 타입. 실측 어휘에서는 관측되지 않았으나 명시적 오류 축으로 유지한다.
_ERROR_EVENTS = frozenset({"error"})

# 하위호환 별칭(기존 import 보호). 의미는 _TURN_END_EVENTS로 일반화됐다.
_COMPLETE_EVENTS = _TURN_END_EVENTS

# 공개 계약 — 다른 모듈이 분기 축을 참조할 때 이 이름을 쓴다.
# 어휘 계약 테스트(tests/test_event_vocabulary.py)가 검사하는 대상이기도 하다.
TURN_END_EVENTS = _TURN_END_EVENTS
ERROR_EVENTS = _ERROR_EVENTS

# 이번 사이클 스냅샷에서 uuid↔pid가 **재확인된** 증거를 뜻하는 마커(T14 S3 D2).
# 이 마커가 없는 running_pid(예: codex의 오래된 osPid)는 PID 재사용으로 거짓 RUNNING을
# 만들 수 있으므로 직접 증거로 쓰지 않는다.
# 값은 `acp.evidence`가 소유한다(T14 S4c-1 D3) — 쓰는 쪽·읽는 쪽·화면에 투영하는 쪽이
# 각자 리터럴을 들고 있으면 표에서 빠진 마커를 아무도 알아채지 못한다.
RUNNING_SIGNAL_PROCESS_RESUME = MARKER_PROCESS_RECHECK


def _stale_or_unknown(process_signal_available: bool) -> SessionState:
    """실행 신호를 관측하지 못했으면 "종료"를 단언하지 않는다(T14 S3 D1a-2).

    STALE의 확정 의미는 "사실상 종료 — 정리 대상"이고 cleanup 집계·필터로 공급된다.
    신호가 없을 때는 판정 불가(UNKNOWN)가 정확하다.
    """
    return SessionState.STALE if process_signal_available else SessionState.UNKNOWN


def _utc_now_if_naive(dt: datetime) -> datetime:
    """naive datetime을 UTC로 간주."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def derive_state(
    record: SessionRecord,
    now: datetime,
    cfg: LivenessConfig,
    is_alive: Callable[[int | None], bool] = lambda _: False,
    # ^ 기본값 False = 주입 누락 시 PID 신호 없음(보수적 판정). 테스트에서 명시 주입 권장.
    process_signal_available: bool = True,
) -> SessionState:
    """SessionRecord + 현재 시각 + 설정 + PID 생존 조회로 SessionState를 결정적으로 반환.

    Args:
        record:   수집된 세션 레코드.
        now:      현재 UTC 시각 (주입 — 내부 datetime.now() 호출 금지).
        cfg:      임계값 설정.
        is_alive: PID 생존 여부 확인 콜러블 (테스트에서 fake 주입 가능).
                  내부 OS 호출 금지 — 콜러블로 위임.
                  기본값 False = 주입 누락 시 PID 신호 없음(보수적, RUNNING 잠정).
        process_signal_available:
                  이 앱의 실행 신호를 이번 사이클에 **관측했는지**. False면 STALE 확정을
                  억제한다(T14 S3 D1a-2) — 종료를 관측하지 못한 채 "사실상 종료"라고
                  단언하면 "확인 불가"와 모순되고, 그 판정이 cleanup 집계·필터로 흘러간다.
                  LIVE/IDLE/HOLDING은 활동 기반 서술이라 그대로 허용한다.
    """
    last_ev = record.last_event

    # ── 1. 에러/중단 (즉시 반환) ─────────────────────────────────────────
    if last_ev in _ERROR_EVENTS:
        return SessionState.ERROR

    # ── 2. 승인대기 → HOLDING (즉시 반환) ───────────────────────────────
    if last_ev and "approval_request" in last_ev:
        return SessionState.HOLDING

    # ── 3. last_activity 없음 → UNKNOWN ──────────────────────────────────
    if record.last_activity is None:
        return SessionState.UNKNOWN

    last = _utc_now_if_naive(record.last_activity)
    current = _utc_now_if_naive(now)
    elapsed = (current - last).total_seconds()

    # ── 4. 시계 불일치 방어 (C3) ─────────────────────────────────────────
    if elapsed < 0:
        return SessionState.UNKNOWN

    # ── 5. 턴 종료(완료/abort) → 나이 기반 (ZTR 망각 케이스 포함) ────────
    if last_ev in _TURN_END_EVENTS:
        if elapsed < cfg.idle_threshold:
            return SessionState.LIVE
        elif elapsed < cfg.hold_threshold:
            return SessionState.IDLE
        elif elapsed < cfg.stale_ttl:
            return SessionState.HOLDING  # ZTR 망각 케이스
        else:
            return _stale_or_unknown(process_signal_available)

    # ── 6. in-turn: 완료·에러·승인 이외의 모든 non-None event_msg ────────
    # task_started / agent_message / token_count / patch_apply_end /
    # context_compacted / user_message 등 진행중 턴의 마지막 신호.
    # ★ task_started 리터럴 한정이 아님 — 실제 진행중 세션은 대개
    #   agent_message / token_count 등이 마지막 event_msg.
    # 구간 규칙은 턴 종료 경로와 **동일**하다. 임계값의 열림/닫힘이 이벤트 종류에 따라
    # 달라질 근거가 없다(T14 S2 D2). stale 단계가 없던 탓에 완료 이벤트 없이 닫힌 세션이
    # 나이와 무관하게 영구 HOLDING으로 남았다.
    if last_ev is not None:
        if is_alive(record.running_pid):
            return SessionState.RUNNING
        elif elapsed >= cfg.stale_ttl:
            return _stale_or_unknown(process_signal_available)
        elif elapsed >= cfg.hold_threshold:
            return SessionState.HOLDING
        else:
            return SessionState.RUNNING  # 잠정 (PID 없지만 아직 hold 미만)

    # ── 6.5 살아있는 프로세스는 그 자체로 실행 증거다 (T14 S3 D2) ─────────
    # last_event가 없는 앱(claude 등)은 시간 폴백만 타서, 실제로 돌고 있는 세션이
    # 조용해지면 IDLE/STALE로 내려갔다. 세션 uuid에 묶인 PID가 살아 있다는 것은
    # 시간보다 강한 직접 증거이므로 여기서 먼저 판정한다.
    # 단 **이번 스냅샷에서 재확인된 증거**로 한정한다. 마커 없이 남아 있는 PID
    # (codex의 오래된 툴콜 osPid 등)는 재사용된 PID를 짚어 거짓 RUNNING을 만든다.
    if record.running_cmd == RUNNING_SIGNAL_PROCESS_RESUME and is_alive(record.running_pid):
        return SessionState.RUNNING

    # ── 7. last_event=None: raw_status 폴백 (FakeCollector/non-Codex 하위호환) ─
    # last_event가 있으면 6번에서 이미 반환됨. 여기 도달 = last_ev is None.
    if record.raw_status:
        status_lower = record.raw_status.lower()
        if any(k in status_lower for k in ("done", "complete", "finished")):
            return SessionState.DONE
        if any(k in status_lower for k in ("error", "fail", "exception")):
            return SessionState.ERROR

    # ── 8. 시간 기반 폴백 (last_event=None + raw_status 없음) ────────────
    # FakeCollector/P0 하위 호환 + last_event 미지원 앱(P2 Claude/Cursor 초기).
    # 전 구간을 명시한다. `hold` 초과를 전부 UNKNOWN으로 두면 "정보 없음"과 "오래 멈춤"이
    # 한 값으로 뭉개진다(T14 S2 D3). UNKNOWN은 판정 불가에만 남긴다.
    if elapsed < cfg.idle_threshold:
        return SessionState.LIVE
    elif elapsed < cfg.hold_threshold:
        return SessionState.IDLE
    elif elapsed < cfg.stale_ttl:
        return SessionState.HOLDING
    else:
        return _stale_or_unknown(process_signal_available)
