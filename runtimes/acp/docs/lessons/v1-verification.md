# Lessons — V1 verification

작성: 2026-06-11

## WHY

V1은 기능 추가가 아니라 검증 정직성 회복 페이즈다. U1/U2 구현은 동작했지만 `pytest -q` 수치에 e2e가 섞여 있었고, KPI 프로젝트 수가 그룹/필터의 `no-project` 모델과 어긋날 수 있었다.

## LESSON

1. 단위와 e2e 수치는 반드시 분리한다.

   `pytest -q`는 `-m "not e2e"`로 브라우저 테스트를 제외하고, Playwright는 `pytest -m e2e tests/e2e -q`로 별도 실행한다. 환경 우연이나 optional dependency skip에 검증 경계를 맡기지 않는다.

2. 통합 e2e는 실제 이벤트 경로를 타야 한다.

   클라이언트 reducer를 직접 호출하면 UI 파생뷰 검증일 뿐 SSE 검증이 아니다. V1 통합 e2e는 `EventSource` 연결 후 fake collector의 명시적 전이 옵션으로 poller가 `state_change`와 `notification` SSE를 실제 발행하게 만든다.

3. 초기 baseline은 알림 전이가 아니다.

   fresh DB에서 이미 HOLDING/STALE/ERROR인 기존 세션을 처음 수집할 때 알림을 보내면 실사용 데이터가 많은 환경에서 알림 폭주가 난다. 알림은 `prev_state is not None`인 실제 전이에만 발행한다.

4. `no-project`는 하나의 버킷으로 다룬다.

   그룹/필터가 `no-project`를 보여주면 KPI 프로젝트 수도 같은 기준이어야 한다. 숨겨진 예외 버킷은 카운트 불일치를 만든다.

5. local LLM 리뷰도 운영 리스크다.

   DuckDB lock이 풀려도 긴 phase-level diff는 stale `analyzing`/timeout으로 멈출 수 있다. 이 경우 PASS로 올리지 말고 blocked로 남기고, 생성된 고CPU `python -` 프로세스와 spool 잔재를 정리한다.

## 검증

```text
python -m pytest -q
107 passed, 7 deselected in 1.20s

python -m pytest -m e2e tests/e2e -q
7 passed in 13.10s

python -m compileall -q acp
PASS

git diff --check
PASS (CRLF normalization warnings only)
```

실앱 API smoke:

```text
python -m acp web --host 127.0.0.1 --port 8913 --db-path .acp/v1-live-smoke-afterfix.db --events-log .acp/v1-live-smoke-afterfix-events.jsonl --poll-interval 60
/api/sessions total: 100
artifact: .acp/v1-live-smoke-afterfix-sessions.json
initial webhook/toast notification logs: 0
```
