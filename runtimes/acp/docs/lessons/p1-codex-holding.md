# P1 Codex 홀딩 탐지 교훈

날짜: 2026-06-09  
모듈: acp/collectors/codex.py, acp/liveness.py

---

## L1 — osPid는 홀딩 판별자가 아니다 (가장 중요)

**문제(잠재적 실수):** chat_processes.json의 osPid를 "세션 생존" 판별에 쓰고 싶은 유혹.

**진실:** osPid = 개별 툴콜 서브프로세스 PID. 세션이 idle할 때는 항상 부재. 심지어 hung orphan일 수도 있음.

**올바른 사용법:** osPid는 **RUNNING 확인용 양성 신호**로만. 부재 = 실행 중 아님이지 홀딩이 아님.

**홀딩 판별 신호:** `last_event(jsonl) + age`. started/complete 카운트 짝맞춤 절대 금지.

---

## L2 — 마지막 이벤트 1개로 판정 (카운트 짝맞춤 금지)

**문제:** `task_started` 횟수 vs `task_complete` 횟수를 세서 OPEN 여부 판단하려는 시도.

**실측 결과:** 휴지인데 OPEN count > 0인 사례 실측됨 (롤백, 리트라이 등으로 불일치 발생).

**올바른 방법:** jsonl 꼬리(tail)에서 **마지막 event_msg 타입 1개**만 읽어 판정.

---

## L3 — chat_processes.json은 배열 (Object 아님)

**문제:** 처음에 Object(key=conversationId)로 예상했으나 배열 구조.

**실측:** `[{"conversationId": "...", "osPid": ..., ...}, ...]`  
동일 conversationId에 여러 항목 가능 → `updatedAtMs` 기준으로 최신 1개 선택.

---

## L4 — jsonl tail 역방향 읽기 + mtime 캐시

**최적화:** 대형 jsonl(~3MB, ~9MB)을 전체 로드하면 수십ms 낭비.  
**구현:** `seek(0, 2)` → 역방향 chunk 읽기 → `mtime` 변동 시에만 re-tail.  
실측: 3MB 파일도 tail 30줄 = 즉시 완료.

---

## L5 — HOLDING 결정테이블 ZTR 망각 케이스

**핵심 인사이트:** `task_complete` 이후 세션을 열린 채로 방치하는 경우(ZTR 망각),  
종료 신호를 못 받은 외부 감시자는 그 세션이 여전히 살아있다고 착각.

**판정 규칙:** `task_complete` + age > hold_threshold → HOLDING (되살릴 수 있는 멈춤).  
이것이 이 프로젝트의 존재 이유.

**실증:** stale_ttl 확장 시 6개 세션 HOLDING 판정 확인.

---

## L6 — in-turn 판정을 `task_started` 리터럴로 좁히면 실데이터 무력화 (P1.5 버그)

**문제:** `derive_state`의 in-turn 분기가 `if last_event == "task_started":` 리터럴 한정으로 구현.

**실측:** Codex 실세션에서 진행 중 턴의 마지막 `event_msg`는 보통 `agent_message`(어시스턴트 응답
청크) 또는 `token_count`(토큰 소모 통계). `task_started`는 턴 시작점에만 등장하며,
롤아웃 `.jsonl` 꼬리(tail)에서는 거의 잡히지 않는다.

**결과:** `task_started` 리터럴 한정 시 → 진행 중 세션 RUNNING/HOLDING 판정 **완전 무력화**.
in-turn인 `agent_message`/`token_count`를 raw_status 폴백 경로나 시간 폴백이 받아
LIVE/IDLE/UNKNOWN으로 오판.

**올바른 구현:** in-turn = `last_event is not None AND last_event ∉ COMPLETE_EVENTS(frozenset)`.
`task_started`를 특별취급할 이유 없음. 분기 순서도 `task_complete` 먼저, 그 다음 in-turn.

**재발 방지:** 테스트 픽스처에 `task_complete`만 넣으면 이 버그를 잡을 수 없음.
`agent_message`/`token_count`를 마지막 이벤트로 갖는 진행중 픽스처를 반드시 포함할 것.

---

## L7 — tail 30줄 고정은 장문 응답에서 event_msg 누락 위험

**문제:** `_read_last_event`가 끝 30줄만 보면 대량 `response_item`이 뒤에 쌓이는
장문 응답 세션에서 event_msg가 30줄 밖에 위치.

**구현:** 1차 30줄 시도 → event_msg 미발견 시 500줄로 1회 확대 재시도(전체 로드 금지).
2차 재시도도 실패하면 `None` 반환(UNKNOWN 판정). 무한 확대 금지.
