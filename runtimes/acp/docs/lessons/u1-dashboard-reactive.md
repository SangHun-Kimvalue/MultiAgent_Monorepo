# Lessons — U1 대시보드 reactive 토대

> append-only. WHAT이 아니라 WHY/LESSON.

## 2026-06-11 — 단일 모델 파생뷰가 실시간 일관성의 핵심

**LESSON:** 실시간 대시보드에서 "KPI 카운트"를 별도 상태로 들고 SSE마다 손으로 ±1 동기화하면 반드시 드리프트한다(누락·중복·재연결 시 어긋남). U1은 `dashboard.js`의 `sessions` 맵 **하나**만 진실원천으로 두고 KPI·그룹테이블·알림을 전부 **파생 getter**(`get kpis()`, `get groupedSessions()`, `get sessionList()`)로 계산했다. SSE는 원본 맵만 패치하고 뷰는 자동 재계산 → 카운트가 구조적으로 어긋날 수 없다.

## 2026-06-11 — 하이브리드: 스냅샷 + 델타 (둘 중 하나만으론 부족)

**WHY:** SSE `state_change`만 쓰면 **신규 세션 등장**과 phase/activity 같은 **비-상태 필드 변경**을 못 따라간다(SSE 페이로드에 그 필드가 없음). `/api/sessions` 주기 폴링만 쓰면 상태 전이 반영이 poll_interval만큼 지연된다. → 스냅샷(`/api/sessions?limit=300`을 poll_interval마다) = 신규/필드변경 진실원천, SSE 델타 = 즉시성 보강. 둘을 합쳐야 한다.

## 2026-06-11 — 무빌드 reactive: Alpine 벤더링

**WHY:** 풀 SPA(React/Vue)는 KPI+필터/정렬 수준에 과설계이고 node 빌드 의존이 v1 무빌드·토큰0 철학과 충돌한다. Alpine.js를 `static/vendor/alpine.min.js`로 **벤더링**(런타임 CDN 의존 0 — 단일PC·오프라인)해 빌드 없이 반응성만 얻었다. `dashboard.js`(전역 `dashboardStore()` 정의)를 alpine보다 **먼저** defer 로드해야 `x-data="dashboardStore()"`가 초기화 시 정의돼 있다.

## 2026-06-11 — Playwright를 기본 pytest에서 격리

**LESSON:** `-p no:pytest_playwright`만으로는 충분하지 않다. `testpaths=["tests"]`가 `tests/e2e`까지 포함하면 기본 `pytest -q`에 브라우저 e2e가 섞인다. V1에서 e2e 테스트에 `@pytest.mark.e2e`를 부여하고 `addopts = "-p no:pytest_playwright -m 'not e2e'"`로 강제 분리했다. 기본 게이트는 `107 passed, 7 deselected`, 브라우저 게이트는 `pytest -m e2e tests/e2e -q`의 `7 passed`로 별도 표기한다.
