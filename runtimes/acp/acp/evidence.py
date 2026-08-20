"""acp/evidence.py — 실행 증거 어휘(T14 S4c-1 D3).

`running_cmd`에는 **두 종류**의 값이 섞여 있다. 앱이 실제로 실행 중인 명령어와,
ACP가 스스로 써 넣은 내부 마커다. 화면은 그 둘을 구분하지 못해 내부 사정
(`process-resume`)을 사용자의 명령어인 양 그렸다 — 표시된 문자열이 실행된 적 없는
명령이므로, 이름이 사실과 다른 결함이다.

**구분은 서버가 한다.** UI가 sentinel을 직접 비교하면 필드에 두 의미가 계속 남고,
마커가 하나 늘 때마다 같은 결함이 반복된다. 여기서 `execution_evidence_kind` enum으로
투영하고 소비자는 enum만 본다(R5 — 코드는 enum으로 분기한다).
"""
from __future__ import annotations

# 증거 종류. 소비자(UI·API)는 이 값으로만 분기한다.
EVIDENCE_COMMAND = "command"                  # 앱이 준 실제 명령어
EVIDENCE_PROCESS_RECHECK = "process_recheck"  # 이번 사이클에 프로세스를 재확인했다는 내부 마커
EVIDENCE_NONE = "none"                        # 값이 없다
EVIDENCE_UNKNOWN = "unknown"                  # 무엇인지 모른다 — **명령으로 승격하지 않는다**

# 이번 사이클 스냅샷에서 uuid↔pid가 재확인된 증거 마커(T14 S3 D2).
# 값 자체는 유지한다 — 이미 저장된 `sessions.running_cmd` 행에 이 문자열이 들어 있고,
# 값을 바꾸면 과거 행의 뜻이 조용히 달라진다.
MARKER_PROCESS_RECHECK = "process-resume"

# 앞으로 추가될 내부 마커가 쓸 **예약 네임스페이스**. 실행 명령어가 이 접두사로
# 시작하는 일은 없다. 네임스페이스 안인데 아래 표에 없는 값은 `unknown`이다 —
# 마커를 추가하면서 투영을 빼먹어도 명령어로 승격되지 않는다(fail-closed).
#
# **이 접두사만으로는 부족하다**(구현리뷰 R1 P1). 읽기 시점에는 네임스페이스 밖의 새 마커와
# 실제 명령어를 구분할 방법이 없다 — 명령어는 임의의 문자열이기 때문이다. 그래서 강제는
# **쓰기 쪽**에 둔다: ACP가 `running_cmd`에 써 넣는 리터럴은 전부 아래 표에 있어야 하고,
# `tests/test_execution_evidence.py`의 AST 스캔 게이트가 그것을 강제한다. 접두사 규칙은
# 그 위의 2차 방어일 뿐, 유일한 경계가 아니다.
INTERNAL_MARKER_NAMESPACE = "acp:"

# 알려진 내부 마커 → 증거 종류. **마커의 단일 출처**다. 마커를 쓰는 쪽(수집기)과
# 읽는 쪽(liveness)이 각자 리터럴을 들고 있으면 이 표가 비어도 아무도 모른다.
INTERNAL_MARKERS: dict[str, str] = {
    MARKER_PROCESS_RECHECK: EVIDENCE_PROCESS_RECHECK,
}


def evidence_kind(running_cmd: object) -> str:
    """`running_cmd` 원문 → 증거 종류 enum.

    저장 스키마는 건드리지 않는다(API projection 단계의 순수 함수).
    """
    if running_cmd is None:
        return EVIDENCE_NONE
    text = str(running_cmd).strip()
    if not text:
        return EVIDENCE_NONE
    marker = INTERNAL_MARKERS.get(text)
    if marker is not None:
        return marker
    if text.startswith(INTERNAL_MARKER_NAMESPACE):
        # 우리 네임스페이스인데 투영이 없다. 모르는 것을 명령어라 부르지 않는다.
        return EVIDENCE_UNKNOWN
    return EVIDENCE_COMMAND
