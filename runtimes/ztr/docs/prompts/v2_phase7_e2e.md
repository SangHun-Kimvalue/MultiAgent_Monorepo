# ZRT v2 — Phase 7 구현 프롬프트: 통합 E2E + UI 관측 (Implementer 세션용)

작업: ZRT v2 — Phase 7 통합 E2E 검증 + 대시보드 관측 배선 (Phase 6에서 NOT CLAIMED로 남긴 full E2E를 비로소 claim 시도하고, v2 검증 이력을 실제 UI에 표시). 한국어로 응답/주석.

[형상관리] 시작 전 `git status` — 클린이어야 함(아니면 STOP 후 보고). **현재 브랜치(`main`) 유지** — v2는 이미 머지됨(3eaa92a). 새 브랜치/태그 생성 금지. 커밋은 한국어, feat/fix/docs 프리픽스, **사용자 확인 후**.

[전제] v2가 main에 머지 완료. 기준선 실측: ruff PASS, `mypy src`(strict, 3.12) PASS, `pytest -q` **189 passed**, `ztr invariants` PASS. spike S1 완전 통과(claude/codex/gemini 헤드리스 실측 — ROADMAP §0). 헤드리스 호출 시 stdin 파이프는 EOF 대기 → `< NUL` 또는 명시 close 필수.

[SoT — 반드시 읽을 것]
- `docs/ROADMAP_V2.md` §0 결정 로그 전부 + 끝 절 불변 원칙 1~7 + §3 Phase 5/6 종결 기록
- `docs/discovery/ztr-v2/validation_plan.md` — PASS = unit+deterministic+live 1회, **full E2E는 원래 NOT CLAIMED**(이번에 별도 claim 시도)
- `docs/discovery/ztr-v2/design.md` — Boundaries 표 (관측 배선이 이 선을 넘지 않아야 함)
- `docs/ZRT_V2_DIAGNOSIS.md` §3 KEEP 표 — "SQLite SessionStore + 대시보드: 검증 이력 관측용으로 축소 유지". 이 프롬프트의 관측 배선이 바로 그 역할의 구현이다.

[전략 리마인더] **이건 검증·관측 페이즈다 — 새 판단 기능을 만들지 않는다.** full E2E를 실모델로 관통시켜 *무엇이 진짜 통과인지*를 정직하게 가르는 게 목적. 대시보드는 v2 결과를 **표시(관측)만** 한다 — 편집·판단·재실행 트리거 금지(동결 원칙 유지, "관측으로 축소"만 해제). R5는 절대 불변: 어떤 LLM 자연어도 verdict로 파싱하지 않는다.

[이번 범위]
1. **E2E 시나리오 스크립트** (`scripts/e2e_smoke.py` 또는 `tests/test_e2e_integration.py` 신설 — repo 컨벤션 따라):
   - 체인 1 (CLI 관통): 의도적으로 결함 있는 임시 파일 생성 → `ztr review <path>` (CHANGES_REQUESTED 기대) → 결함 수정 → 재실행 (PASS 기대) → `ztr verify --post-merge <path>` → `ztr invariants`. 각 단계에서 **stdout이 단일 Envelope JSON으로 파싱되고 exit code가 verdict 매핑(0/1/2)과 일치**하는지 단언. 모델 호출 없는 결정론 경로.
   - 체인 2 (gate): critic result JSON 픽스처(빈 finding / M2 누락 등) → `ztr gate <file>` → CHANGES_REQUESTED/PASS 분기 단언.
2. **Full relay E2E (live — claim 시도)**: `ztr run-phase`로 실제 헤드리스 leg 관통.
   - implementer leg = `codex exec --cd <repo> --sandbox read-only --ephemeral --color never -` (read-only, 편집 금지 — Phase 6 검증된 안전 구성).
   - (가능하면) reviewer leg = `claude -p --model sonnet` 추가해 2-leg 순차 관통. **Claude 세션 한도로 child exit 1 가능 → 그 경우 relay CHANGES_REQUESTED는 정상 동작이고, Sonnet leg 성공은 NOT CLAIMED로 정직 분리**(Phase 6 선례).
   - outer envelope PASS/exit 0 + step capture(stdin/stdout/stderr/envelope) 파일 존재를 단언. model 필드는 `external-cli`(중립화 유지).
3. **관측 배선 (대시보드용 — Boundaries 준수)**: `ztr review`/`verify`/`run-phase` 완료 시 결과를 `SessionStore`에 **기록만** 한다.
   - 부착: `SessionStore.create_session(task, target_file)` → 실행 → `finish_session(id, verdict=<envelope.status 값>, rounds=<step수 또는 1>)`. `final_verdict` 컬럼에 `PASS|CHANGES_REQUESTED|BLOCKED` 문자열 그대로 기록.
   - **기록은 옵트인**: 기존 stdout=단일 Envelope JSON 계약을 깨지 말 것. `--record` 플래그(기본 off) 또는 config 토글로 SessionStore 기록을 켠다. 기록 실패가 명령 verdict를 오염시키면 안 됨(try/except 격리, 실패 시 stderr 경고만).
   - **Boundaries**: SessionStore에 쓰는 것은 verdict enum·exit code·대상 경로·소요시간 같은 **사실**뿐. 리뷰 자연어 의미를 해석해 저장하지 않는다(R5).
4. **UI 검증**: `ztr web`(uvicorn, `127.0.0.1:8000`) 기동 → 위 관측 배선으로 기록된 v2 run이 세션 목록(`/`)·상세(`/sessions/{id}`)에 **렌더되는지 실측**. 페이지가 200 + v2 verdict가 표시됨을 확인(스크린샷 또는 HTML 단언). 대시보드 코드 변경은 최소화 — 기존 템플릿이 `final_verdict`를 이미 표시하면 배선만으로 충분.

★ 명시적 out-of-scope ★
- 대시보드 신규 기능/재디자인/실시간 푸시 추가 (동결 — 관측 표시만)
- 자동 소스 편집·머지·rollback (영구 NOT CLAIMED — Boundaries)
- run-phase에 자동 수정 루프/타임박스 재도입 (§0 영구 descope)
- Claude 세션 한도 우회 (불가 — 한도 시 NOT CLAIMED 정직 기록)
- ollama 리뷰 의미 품질 정량 평가

[부착점/대상] (라인 이동 가능 → 시그니처로 재확인)
| 위치 | 내용 |
|---|---|
| `src/engine/session_store.py:126` `create_session(task, target_file, config_snapshot)` / `:148` `finish_session(id, *, verdict, rounds, error)` | 관측 기록 부착점 — verdict 문자열에 v2 enum 그대로 |
| `src/runner.py` `cmd_review`/`cmd_verify`/`cmd_run_phase` | `--record` 분기 추가 지점. stdout 단일 Envelope 계약 유지 |
| `src/web/app.py:75` `page_sessions` / `:100` `page_session_detail` / `:166` `list_sessions` | 대시보드 읽기 경로 — `final_verdict` 표시 확인 |
| `src/web/templates/sessions.html`, `session_detail.html` | v2 verdict 표시 여부 확인(필요 시 최소 라벨 보강) |
| `src/runner.py` `cmd_web`(`web` 서브커맨드) | 대시보드 기동 |

[아키텍처 결정] (틀리기 쉬운 지점)
- **관측 기록은 verdict/exit/경로/시간 같은 사실만.** LLM 자연어(ollama review_text, relay child stdout)를 파싱해 DB에 의미로 저장하면 R5 위반 = 설계 자동 BLOCKED.
- **stdout 계약 불변**: `--record`가 켜져도 stdout은 여전히 단일 Envelope JSON. DB 기록은 부수효과일 뿐.
- 관측 기록 실패는 verdict를 바꾸지 않는다(격리). DB 잠금/경로 부재 시 stderr 경고 후 정상 envelope 반환.
- `finish_session`의 verdict→status 매핑(현재 fail/timeout/error만 failed)은 v2 enum에서 전부 completed로 떨어짐 — 표시에는 `final_verdict` 컬럼을 쓰므로 무해. status 매핑을 v2용으로 바꾸려면 별도 결정 기록.

[불변 원칙] `docs/ROADMAP_V2.md` 끝 절 1~7 전부. 특히 R5 Boundaries, encoding='utf-8', subprocess 3단 방어, Timebox L1=3회 STOP.

[검증/DoD]
1) unit/deterministic: E2E 체인 1·2 스크립트가 모델 호출 없이 envelope/exit code/verdict 분기를 단언하며 GREEN. 관측 배선 기록/격리(실패 시 verdict 불변) 단위 테스트.
2) 회귀: `pytest` 전체 GREEN(189+신규), `mypy src` strict(3.12), `ruff check src tests` 통과. **stdout 단일 Envelope 계약 회귀 없음**(기존 test_*_cli 통과).
3) **live full E2E (claim 경계 정밀)**: `ztr run-phase` 실 codex leg 관통 1회 — outer envelope PASS·exit 0·step capture 파일 존재를 단언, 재현 커맨드 전문 기록(C6). claude leg 추가 시도 결과를 정직 보고(성공=claim, 세션 한도=NOT CLAIMED).
4) **UI 실측**: `ztr web` 기동 → v2 run이 기록된 DB로 세션 목록/상세가 200 렌더 + v2 verdict 표시. 스크린샷 또는 HTML 본문 단언을 완료 보고에 첨부.
5) 리뷰 ALL PASS (둘 다, 하드 게이트):
   - B. 기계: `ztr review --changed` + `ztr invariants` + ruff/mypy 자기 적용(dogfood).
   - A. 별도 Reviewer(설계): Planner가 서브에이전트로 집행. review bundle(변경 파일+핵심 diff+E2E 출력+UI 스크린샷+남은 질문)만 첨부. **NOT CLAIMED로 닫지 말 것.** finding은 M2 5필드 정형으로: `severity / finding / evidence_or_repro / impact / recommendation`.
6) 커밋은 사용자 확인 후. 페이즈 경계를 ROADMAP_V2.md §1·§3·커밋 메시지에 기록.

[완료 보고] 변경 파일 / 핵심 결정·부착 위치 / E2E 재현 커맨드+출력 / UI 스크린샷 경로 / **PASS는 어디까지·NOT CLAIMED·가정**(특히: full E2E 자동 관통을 어디까지 claim하는지, Sonnet leg 결과, 관측 배선의 Boundaries 준수) / 새 LESSON append 여부.
