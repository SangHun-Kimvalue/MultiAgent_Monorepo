작업: Agent Control Plane — Phase V1 (검증 하니스 정비 + 전체 통합 E2E + P3/U1/U2 리뷰 게이트 클로즈). 한국어로 응답/주석.

[형상관리] 단일 브랜치 `acp-v1` 유지. 새 브랜치 금지. **태그 미생성**(스킬 §9 — 경계는 커밋/HANDOFF로 기록). 시작 전 `git status`.

[전제] v1 백엔드(P0~P3) + UI 트랙(U1·U2) 구현·커밋 완료. 단 **P3·U1·U2가 모두 review**(Nitpicker 미통과)이고, U1 Reviewer가 **MAJOR 1건(테스트 게이트 격리 실패)** 을 지적함. 이 페이즈는 신기능이 아니라 **검증 무결성 + 전체 통합 확인 + 리뷰 게이트 클로즈**가 목적.

[SoT — 반드시 읽을 것]
- `AgentControlPlane/PHASE.md` §0 결정 로그 + §4 불변 원칙 + §5 DoD
- `docs/HANDOFF.md` (U1 Reviewer findings 기록)
- 기존 테스트: `tests/`(단위 106) + `tests/e2e/`(conftest.py, test_dashboard_u1.py, test_dashboard_u2.py = 6)

[전략 리마인더] minimal-first. 기능 추가 금지(U2까지로 상황판 기능 동결). 이 페이즈는 **검증·정직성·클로즈**만. LLM 0회.

[이번 범위]
할 것:
1. **테스트 게이트 격리 (U1 Reviewer MAJOR 해결, 부착점 `pyproject.toml:34-40`)**:
   - e2e 테스트에 `@pytest.mark.e2e` 마커 부여(`tests/e2e/test_dashboard_u1.py`, `test_dashboard_u2.py`).
   - `[tool.pytest.ini_options].addopts`에 `-m "not e2e"` 추가(또는 `--ignore=tests/e2e`) → **`pytest -q`는 단위만**(브라우저 미기동), e2e는 `pytest -m e2e tests/e2e`로만.
   - 검증 후 **수치 분리 정직 표기**: "단위 N passed / e2e 6 passed"(기존 '112 passed'가 e2e 포함이었음을 HANDOFF에 정정).
2. **KPI 일관성 (U1 Reviewer MINOR, 부착점 `acp/web/static/dashboard.js` `get kpis()`)**:
   - KPI "프로젝트 수" 집계(`if (session.project_path) projects.add`)와 그룹/필터의 `no-project` 처리 불일치 통일 — KPI도 `no-project`를 1 프로젝트로 셀지, 둘 다 제외할지 **결정 로그에 명시**하고 한쪽으로 통일. e2e로 KPI=그룹 프로젝트 수 일치 케이스 보강.
3. **전체 통합 E2E (사용자 요청)**:
   - **A. 실앱 Live smoke**: `python -m acp web`(실 Codex/Claude/Cursor 폴링)로 띄워 수집→상태판정→PHASE조인→대시보드까지 1회 종단 확인. `/api/sessions` 응답·KPI·SSE 연결 캡처(artifact 경로). ※ 무거운 실행 — **사용자 확인 후** 기동.
   - **B. 풀-피처 e2e**(`--fake` 임시포트): 수집 표시 → 상태전이 주입 → 알림 발사(notification SSE) → 대시보드 KPI 갱신 → 필터/정렬까지 **한 흐름**으로 검증하는 통합 e2e 케이스 1개 추가(`tests/e2e/test_integration_full.py`). 기존 conftest 임시포트 픽스처 재사용.
4. **리뷰 게이트 클로즈**:
   - U1 MAJOR/MINOR 수정 후 **U1 Reviewer 재확인** + **P3·U1·U2 Nitpicker(local LLM/ollama)** 실행. Nitpicker가 `analytics.duckdb` lock 등으로 막히면 `blocked`로 정직 보고(PASS/waive 주장 금지).
   - 2갈래 PASS 된 페이즈만 PHASE.md `done`으로. 미통과는 `review`/`blocked` 유지.

★ out-of-scope ★: 신규 UI 기능(세션 상세 드릴다운 등은 별도 후순위), 백엔드 신규 엔드포인트, v2(양방향 통제·온디맨드 브리핑).

[부착점/대상] (시그니처로 재확인 후 부착)
| 파일 | 작업 |
|---|---|
| `pyproject.toml` | addopts `-m "not e2e"` + `markers`에 e2e(이미 선언됨) 활용 |
| `tests/e2e/test_dashboard_u1.py`, `test_dashboard_u2.py` | `@pytest.mark.e2e` 부여 |
| `tests/e2e/test_integration_full.py` | 신규 — 수집→전이→알림→KPI→필터/정렬 종단 |
| `acp/web/static/dashboard.js` | `get kpis()` 프로젝트 집계 일관성(결정 로그 따라) |
| `docs/HANDOFF.md` | 수치 정직 정정(단위/e2e 분리) + 게이트 클로즈 결과 |

[아키텍처 결정]
- 테스트 2게이트는 **마커로 강제 분리**: 단위(`pytest -q`, 브라우저 0) / e2e(`pytest -m e2e`). 격리를 환경 우연(playwright 미설치 skip)에 의존하지 않는다.
- 통합 E2E는 **기존 자산만 사용**(신규 엔드포인트·기능 0). `--fake`는 통합 흐름 재현용, 실앱 smoke는 실데이터 종단 확인용.
- 리뷰 게이트는 페이즈별 독립 — 한 페이즈 blocked가 다른 페이즈 done을 막지 않음.

[불변 원칙]
- 정직성: 검증 수치를 실측대로(112가 e2e 포함이었음 정정). NOT CLAIMED 오용 금지(리뷰 2갈래는 실행 가능 → 환경 게이트만 blocked 허용).
- read-only 표시. LLM 0회.
- **무거운 검증(실앱 Live smoke·전체 e2e·Nitpicker)은 실행 전 사용자 확인**(Owner 확정 2026-06-11).
- Timebox: 게이트 정비가 3회 안 풀리면 멈추고 보고.

[검증·DoD]
1) `pytest -q` → **단위만** 통과(브라우저 미기동, 수치 명시).
2) `pytest -m e2e tests/e2e` → e2e 전체 통과(통합 케이스 포함, 수치+artifact).
3) (사용자 확인 후) 실앱 Live smoke 1회 — 캡처 artifact.
4) 리뷰: U1 MAJOR/MINOR 해소 후 Reviewer 재PASS + P3/U1/U2 Nitpicker(둘 다 **Implementer 세션** 집행, §8). blocked면 정직 보고.
5) Sync-Out: PHASE.md(통과 페이즈 done, 수치/게이트 갱신) + HANDOFF + `docs/lessons/v1-verification.md`. 사용자 "커밋해" 후 커밋(태그 없음). → v1 + 상황판 정식 클로즈.

[완료 보고] 변경 파일 / 테스트 게이트 격리 결과(단위 vs e2e 수치) / 통합 E2E 흐름 / 각 페이즈 게이트 상태(PASS/blocked, 근거) / NOT CLAIMED·가정 / 최종 v1+UI 종합 상태.
