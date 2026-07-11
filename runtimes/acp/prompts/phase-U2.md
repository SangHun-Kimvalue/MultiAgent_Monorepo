작업: Agent Control Plane — Phase U2 (상황판 필터/정렬 + e2e). 한국어로 응답/주석.

[형상관리] 단일 브랜치 `acp-v1` 유지. 새 브랜치 금지. **태그 미생성**(스킬 §9). 시작 전 `git status`.

[전제] **U1 리뷰 PASS 후 시작.** U1 구현은 커밋됨(`a224bcf`)이나 현재 `review`(Reviewer 서브에이전트 + Nitpicker는 구현세션 몫·미실행) — U2 착수 전 U1 게이트를 닫을 것. U1이 깐 reactive 모델 위에 **필터/정렬만 얹는다(재구현 금지)**:
  - `acp/web/static/dashboard.js` `dashboardStore()`: 단일 진실원천 `sessions`(맵) + 파생 getter `get sessionList()`(updated_at desc 정렬) · `get groupedSessions()`(app×project) · `get kpis()`(전체 기준 카운트). 상수 `ACP_STATES`, `ACP_ACTION_STATES`, 헬퍼 `isActionState(state)`.
  - 하이브리드 실시간 일관성(`refreshSessions()` 주기 스냅샷 + `applyStateChange()`/`applyNotification()` SSE 델타)은 이미 동작 — 건드리지 말 것.
  - `acp/web/templates/dashboard.html`: 테이블은 `x-for="group in groupedSessions"`(L88) → 내부 `x-for="s in group.sessions"`(L97). 필터/정렬은 **이 소스 getter를 필터링된 파생 getter로 교체**하는 방식.

[SoT — 반드시 읽을 것]
- `AgentControlPlane/PHASE.md` §0 결정 로그 + §4 불변 원칙
- U1 산출물: `acp/web/templates/dashboard.html`(Alpine 스토어), `acp/web/static/style.css`, `tests/e2e/`
- `docs/lessons/u1-dashboard-reactive.md`(U1 결정 맥락)

[전략 리마인더] minimal-first. 필터/정렬은 **클라이언트 파생뷰**(U1 모델에서 계산) — 서버 라운드트립·신규 엔드포인트 없이. 무빌드 유지. LLM 0회.

[이번 범위]
할 것:
1. **필터** (KPI 스트립 하단 또는 테이블 헤더 영역):
   - 앱(codex/claude/cursor) · 상태(LIVE/RUNNING/IDLE/HOLDING/STALE/ERROR/DONE/UNKNOWN) · 프로젝트 필터.
   - 다중 선택 가능. 필터는 U1 모델의 파생 계산으로 테이블에 즉시 반영(SSE 갱신 중에도 필터 유지).
   - **KPI는 전체 기준 유지**(필터와 무관하게 전체 카운트) + 필터 적용 시 "표시 N / 전체 M" 보조 표기. (행동필요 카운트를 필터로 숨기지 않기 위함.)
   - "행동 필요만"(HOLDING+STALE+ERROR) 빠른 토글 1개 — 킬러 시나리오 원클릭.
2. **정렬**:
   - 컬럼 헤더 클릭 정렬: 상태(심각도순: ERROR>STALE>HOLDING>IDLE>RUNNING>LIVE>DONE>UNKNOWN) · 최근활동(desc) · 앱 · 프로젝트.
   - 정렬은 그룹 내 또는 평면 모드 — U1 그룹 구조와의 상호작용을 결정 로그에 명시(그룹 유지하며 그룹 내 정렬 권장).
3. **상태 지속**(선택, 가벼우면): 필터/정렬을 URL querystring에 반영해 새로고침/공유 시 복원.
4. **e2e 확장**(`tests/e2e/`): 필터 적용 시 행 수 변화 · "행동 필요만" 토글 · 정렬 순서 검증.

★ out-of-scope ★: 세션 상세 드릴다운(별도 후순위 페이즈), 서버측 필터/페이지네이션(클라이언트로 충분 — 단일PC 규모), 저장된 뷰 프리셋, 인증.

[부착점/대상] (시그니처로 재확인 후 부착)
| 파일 | 작업 |
|---|---|
| `acp/web/static/dashboard.js` | `dashboardStore()`에 필터 상태(filterApp/filterState/filterProject/actionOnly) + 정렬 상태(sortKey/sortDir) + `get filteredSorted()`/`get visibleGroups()` 파생 getter 추가. `sessionList`·`kpis` 원본 getter 불변(KPI 전체 기준 유지) |
| `acp/web/templates/dashboard.html` | 필터 컨트롤(앱·상태·프로젝트·"행동 필요만" 토글) + 정렬 헤더 + 테이블 x-for를 필터링된 getter로 교체 + "표시 N/전체 M" 표기 |
| `acp/web/static/style.css` | 필터 컨트롤·활성 정렬·빈결과("조건에 맞는 세션 없음") 스타일 |
| `tests/e2e/test_dashboard_u2.py` | 필터 행 수·"행동 필요만" 토글·정렬 순서 e2e(U1 `conftest.py` 임시포트 픽스처 재사용) |

[아키텍처 결정]
- 필터/정렬 = **클라이언트 파생**. U1 `sessions` 모델 → `filteredSorted` computed. 원본 모델 불변, 뷰만 변형(SSE 패치가 원본 갱신 → 뷰 자동 재계산).
- **KPI는 필터 비종속**(전체 기준). 행동필요 수를 필터가 가리지 않음. 그룹 정렬은 그룹 보존 + 그룹 내 정렬.
- 무빌드·신규 엔드포인트 0 유지.

[불변 원칙]
- silent fallback 금지(필터로 0건이면 "조건에 맞는 세션 없음" 명시, 빈 화면 금지).
- read-only 표시. LLM 0회.
- Timebox: 필터/정렬·그룹 상호작용이 3회 안 풀리면 멈추고 보고.

[검증·DoD]
1) `pytest -q` 통과 유지.
2) Playwright e2e(`pytest tests/e2e`): 필터 행 수·"행동 필요만" 토글·정렬 순서 3+케이스(명령+결과+artifact).
3) 수동 스모크: `--fake`로 필터/정렬 동작 + SSE 갱신 중 필터 유지 육안 확인.
4) 리뷰(둘 다 **Implementer 세션** 집행, §8): 별도 Reviewer 서브에이전트 + Nitpicker(ollama). 미준비 시 `blocked` 정직 보고.
5) Sync-Out: PHASE.md(U2 done, UI 트랙 완료) + HANDOFF + `docs/lessons/u2-filter-sort.md`. 사용자 "커밋해" 후 커밋(태그 없음). → 상황판 UI 완성.

[완료 보고] 변경 파일 / 필터·정렬 결정 위치 / 검증(명령+출력+artifact) / PASS 어디까지·NOT CLAIMED·가정 / 상황판 UI 종합 상태 + 후순위 후보(상세 드릴다운).
