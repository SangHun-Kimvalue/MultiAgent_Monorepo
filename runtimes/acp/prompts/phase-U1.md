작업: Agent Control Plane — Phase U1 (상황판 뷰 토대 + KPI 요약 스트립 + Playwright e2e 토대). 한국어로 응답/주석.

[형상관리] 단일 브랜치 `acp-v1` 유지. 새 브랜치 금지. **태그 미생성**(스킬 §9 — 경계는 커밋/HANDOFF로 기록). 시작 전 `git status`.

[전제] v1 백엔드(P0~P3) 동작·머지 상태. P3는 review(Nitpicker 게이트 미종료)지만 **UI 트랙은 P3의 Nitpicker 종료에 의존하지 않는다** — 안정적인 API 계약(`/api/sessions` + `/api/live/stream` SSE) 위에 얹히는 additive 작업. 백엔드 공개 시그니처는 변경 금지(추가만).
**시작 전 P2/P3 산출물 재구현 금지 — 검증 후 재사용**:
  - `acp/web/app.py:155` `GET /` page_dashboard → sessions/grouped_sessions/notifications 전달
  - `acp/web/app.py:177` `GET /api/sessions?limit=` → `store.list_sessions(limit)` = 전체 컬럼 dict 리스트(state·app·project_path·current_phase·phases_done/total·last_activity·owner_session 등)
  - `acp/web/app.py:186` `GET /api/live/stream` SSE → 이벤트명은 payload `type`: 현재 `state_change`(session_id=app:native, native_session_id, app, state, project_path), `notification`(P3), `ping`(30s)
  - `acp/web/templates/dashboard.html` / `acp/web/static/style.css` — 기존 다크테마·그룹테이블·배지·최근알림 패널(SSE 연동). **재작성 말고 확장**.

[SoT — 반드시 읽을 것]
- `AgentControlPlane/PHASE.md` §0 결정 로그(UI 기술스택·실시간 일관성 모델·브라우저 테스트 행) + §4 불변 원칙
- 현 UI: `acp/web/templates/dashboard.html`, `acp/web/static/style.css`, `acp/web/app.py`

[전략 리마인더] minimal-first. **무빌드 유지** — Alpine.js를 벤더링(런타임 npm/빌드 0). 풀 SPA 금지. 실시간 파생뷰(KPI·이후 필터/정렬)는 클라이언트 모델에서 계산하되, 서버 라운드트립 최소화. LLM 0회.

[이번 범위]
할 것:
1. **클라이언트 reactive 토대** (`acp/web/static/` + `dashboard.html`):
   - Alpine.js를 **벤더링**해 `acp/web/static/vendor/alpine.min.js`로 두고 `dashboard.html`에서 로드(CDN 런타임 의존 금지 — 단일PC·오프라인).
   - `x-data` 세션 스토어: `sessions` 맵(키 = `session_id` = `app:native`). 초기값은 페이지에 임베드된 JSON(SSR된 `sessions`) 또는 첫 `/api/sessions` fetch.
   - **실시간 일관성(핵심 결정, §아키텍처):** ① `/api/sessions`를 poll_interval(기본 15s)마다 재fetch해 모델 재빌드(신규 세션 등장·phase/activity 필드 변경 반영) ② SSE `state_change` 수신 시 해당 세션 `state` 즉시 패치(+KPI 재계산) ③ SSE `notification` 수신 시 알림 피드 prepend. → KPI·테이블은 모델에서 **파생**되어 항상 일관.
   - 테이블 렌더를 Alpine `x-for`로 전환(기존 Jinja 그룹테이블의 컬럼/배지/그룹 구조 보존). 신규 세션이 폴링 새로고침에 자연히 나타나야 함.
2. **KPI 요약 스트립** (상단 신규 섹션):
   - 상태별 카운트 배지: HOLDING·STALE·ERROR(행동 필요, 강조) + LIVE·RUNNING·IDLE·DONE·UNKNOWN. + 총 세션 수 / 앱 수 / 프로젝트 수.
   - 모델에서 파생 계산 → SSE 상태전이/폴링 갱신 시 **실시간으로 숫자가 바뀌어야** 함(하드코딩·서버 round-trip 금지).
   - HOLDING/STALE/ERROR 합이 0이면 시각적으로 "정상", >0이면 강조(색/카운트).
3. **Playwright e2e 토대** (`tests/e2e/`):
   - dev 의존성으로 Playwright 추가(`pyproject.toml` `[project.optional-dependencies]`에 `e2e` 그룹: `playwright`, `pytest-playwright`). 런타임 의존성 아님.
   - 테스트 픽스처: `python -m acp web --fake`를 **임시 포트**로 띄우고(서브프로세스), 페이지 로드 → 종료. 포트 충돌 회피(고정 8900 금지, ephemeral).
   - e2e 케이스: ① 대시보드 로드 시 세션 테이블·KPI 스트립 렌더 ② KPI 카운트가 `/api/sessions` 데이터와 일치 ③ SSE 상태전이 1건 주입 시 해당 배지+KPI 카운트 갱신 확인.
   - README/HANDOFF에 실행법 명시(`playwright install chromium` 1회 + `pytest tests/e2e`).

★ out-of-scope ★: 필터/정렬(U2), 세션 상세 드릴다운(후순위), 풀 SPA 프레임워크, 인증, 다중 사용자, 백엔드 신규 엔드포인트(가능하면 기존 `/api/sessions`+SSE만 사용).

[부착점/대상] (시그니처로 재확인 후 부착)
| 파일 | 작업 |
|---|---|
| `acp/web/static/vendor/alpine.min.js` | 신규(벤더링) |
| `acp/web/templates/dashboard.html` | KPI 스트립 추가 + 테이블을 Alpine x-for로 + SSE→모델 패치 스크립트로 교체 |
| `acp/web/static/style.css` | KPI 스트립 스타일(기존 배지 변수 재사용) |
| `acp/web/app.py` | (최소) 초기 세션 JSON 임베드 또는 기존 그대로. 신규 엔드포인트는 지양 |
| `pyproject.toml` | `[e2e]` optional-deps 추가 |
| `tests/e2e/` | 신규 Playwright 픽스처 + 케이스 |

[아키텍처 결정]
- **실시간 일관성 = 하이브리드**: 주기 스냅샷 재fetch(`/api/sessions`, poll_interval) + SSE 델타 패치. 스냅샷이 신규/필드변경의 진실원천, SSE는 즉시성 보강. 둘 중 하나만 쓰면 신규세션 누락 또는 지연 발생.
- **KPI·테이블은 파생뷰**: 모델 1개에서 계산. 별도 카운트 상태를 손으로 동기화하지 말 것(드리프트 원인).
- **무빌드**: Alpine 벤더링, `<script src="/static/vendor/alpine.min.js">`. 빌드/번들러/npm 금지.
- 세션 키는 `app:session_id`(=`session_id` 컬럼). SSE `state_change.session_id`와 `/api/sessions[].session_id` 동일 키 — 패치 시 그대로 매칭.

[불변 원칙]
- silent fallback 금지: fetch 실패·SSE 끊김은 UI에 표시(기존 stream-indicator 패턴) + 콘솔 로그. 오래된 데이터를 현재처럼 보이지 않게.
- 앱 아티팩트·PHASE.md read-only(UI는 표시만).
- 수집·판정·표시 경로 LLM 0회.
- Timebox: Alpine 모델/SSE 패치가 3회 안 풀리면 멈추고 보고.

[검증·DoD]
1) `pytest -q` (기존 server-side 단위 테스트 전부 통과 유지 — UI 변경이 백엔드 깨지지 않음).
2) **Playwright e2e** `pytest tests/e2e` 통과: 렌더·KPI 일치·SSE 갱신 3케이스(명령+결과+artifact 캡처 경로).
3) 수동 브라우저 스모크: `python -m acp web --fake` → KPI 숫자가 실시간 변하는지(임계값 낮춰 전이 유발) 육안 + 캡처.
4) 리뷰(둘 다 **Implementer 세션**이 집행, 스킬 §8): 별도 Reviewer 서브에이전트(독립 컨텍스트) + Nitpicker(local LLM/ollama). 미준비 시 `blocked`로 정직 보고, PASS 주장 금지.
5) Sync-Out: PHASE.md(U1 done/updated_at) + `docs/HANDOFF.md` + `docs/lessons/u1-dashboard-reactive.md`(WHY/LESSON: 하이브리드 실시간 일관성, Alpine 벤더링 결정). 사용자 "커밋해" 후 커밋(태그 없음).

[완료 보고] 변경 파일 / 실시간 일관성 구현 위치(스냅샷 주기+SSE 패치) / 검증(명령+출력+Playwright artifact) / PASS 어디까지·NOT CLAIMED·가정 / U2(필터·정렬) 준비 상태.
