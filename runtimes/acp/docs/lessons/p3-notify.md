# P3 Lesson — transition notification + dedupe

작성: 2026-06-11

## WHY

P3의 가치는 "현재 상태가 위험하다"를 폴링마다 반복해서 외치는 것이 아니라, 세션 상태가 위험 상태로 **전이되는 순간**을 한 번만 사용자에게 알려주는 데 있다. 알림은 상태 판정의 부산물이 아니라 상태 전이 이벤트의 소비자여야 한다.

## LESSON

1. 알림은 snapshot 기반이 아니라 transition 기반이어야 한다.

   P2 poller에는 이미 `prev_state != state` 전이 감지 블록이 있다. P3가 새 비교 루프를 만들면 state_change 이벤트와 notification 이벤트가 서로 다른 진실을 가질 수 있다. 따라서 Notifier는 기존 전이 블록 안에만 부착한다.

2. dedupe 키는 `(app:session_id, to_state)`가 맞다.

   앱별 native session id는 충돌할 수 있으므로 저장소 canonical key인 `app:session_id`를 쓴다. `HOLDING -> STALE`은 `to_state`가 다르므로 별도 알림으로 인정한다.

3. 복귀 상태는 알림 마커를 지워야 한다.

   `HOLDING -> LIVE -> HOLDING`은 같은 상태라도 새 이벤트다. 쿨다운만 보면 재진입 알림이 막힐 수 있으므로 `LIVE/RUNNING/IDLE` 진입 시 `last_notified_state/at`를 reset한다.

4. webhook 미설정은 정상 skip이지만 silent는 아니다.

   기본 설정은 토스트만 켜져 있다. webhook URL이 비어 있으면 조용히 무시하지 않고 로그에 남긴다. 실패한 webhook은 Notifier에서 예외를 올리고 poller가 레코드 단위로 잡아 루프를 유지한다.

5. Nitpicker CLI는 대상 프로젝트 루트를 검증해야 한다.

   `jemmin_cli.py` direct path는 provider는 local LLM으로 돌릴 수 있지만, 내부 `VerificationService(project_root=ROOT)`가 Nitpicker Daemon 루트로 고정되어 ACP 검증과 섞일 수 있다. ACP에서 Nitpicker PASS를 주장하려면 repo-local wrapper 또는 project_root 주입 보강이 필요하다.

## 검증

```text
python -m pytest -q
106 passed in 1.54s

Live smoke artifact:
.acp/p3-live-smoke-notification.json
```
