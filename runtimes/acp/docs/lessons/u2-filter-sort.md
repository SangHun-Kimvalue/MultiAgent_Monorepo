# U2 Lesson — dashboard filter/sort

작성: 2026-06-11

## WHY

U2의 목적은 "상황판에서 지금 봐야 할 세션"을 빠르게 좁히는 것이다. 백엔드의 수집/판정 계약은 이미 충분하므로, 필터/정렬은 서버 기능이 아니라 U1 클라이언트 모델 위의 파생뷰로 유지하는 것이 맞다.

## LESSON

1. KPI는 필터에 종속되면 안 된다.

   행동 필요 수가 필터 때문에 숨겨지면 상황판의 경보성이 약해진다. 따라서 KPI는 전체 `sessions` 기준으로 유지하고, 테이블만 `visibleCount / total`을 표시한다.

2. 원본 모델은 불변, 뷰만 변형한다.

   `sessions`와 `sessionList`는 U1의 진실원천으로 남기고, U2는 `filteredSorted`와 `visibleGroups`를 추가했다. SSE patch와 주기 snapshot은 원본만 갱신하고, 필터/정렬은 자동 재계산된다.

3. 그룹 구조는 유지한다.

   사용자에게 앱/프로젝트 묶음은 여전히 중요한 스캔 단위다. 정렬은 그룹 보존 + 그룹 내 정렬로 처리하고, app/project 정렬만 그룹 순서에도 반영한다.

4. 화면 문구와 정렬 키는 같아야 한다.

   "최근 활동" 컬럼은 `last_activity`를 보여주므로 정렬도 `last_activity` 기준이어야 한다. 저장 갱신 시각인 `updated_at`을 fallback 이상으로 쓰면 사용자가 보는 값과 정렬 결과가 어긋난다.

5. DOM 순서 테스트가 필요하다.

   Alpine 모델 내부의 `visibleGroups`가 정렬되어 있어도 실제 테이블 렌더가 같은 순서를 보장하는지 별도로 확인해야 한다. U2 e2e는 row에 `data-state`, `data-last-activity`를 붙여 실제 DOM 순서를 검증한다.

6. 빈 결과는 상태다.

   필터로 0건이 되면 빈 테이블을 보여주지 않고 "조건에 맞는 세션 없음"을 표시한다. silent fallback 금지 원칙을 UI에도 적용한 것이다.

7. querystring 복원은 가볍게 유용하다.

   필터/정렬 상태를 URL querystring에 반영하면 새로고침 후에도 관찰 맥락이 유지된다. 별도 저장소나 서버 상태가 필요 없다.

## 검증

```text
python -m pytest -q
107 passed, 7 deselected in 1.20s

python -m pytest -m e2e tests/e2e -q
7 passed in 13.10s
```

## V1 정정

U2 당시의 `pytest -q 112 passed` 표기는 e2e 6개가 섞인 수치였다. V1에서 단위/e2e 게이트를 분리했으므로 이후 보고는 반드시 두 수치를 따로 적는다. KPI 프로젝트 수는 필터/그룹과 같은 기준으로 `no-project`를 하나의 프로젝트 버킷으로 센다.
