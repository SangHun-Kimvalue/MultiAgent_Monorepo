# ZRT v2 — Phase 6 구현 프롬프트 (Dogfood 세션용)

작업: ZRT v2 — Phase 6 `ztr run-phase` dogfood. 한국어로 응답/주석.

[형상관리] 시작 전 `git status`. **`v2` 브랜치 유지** — 새 브랜치/태그 생성 금지. 커밋은 한국어, feat/fix/refactor/docs 프리픽스, 사용자 확인 후.

[전제] Phase 5가 커밋된 상태에서 시작. `ztr review --changed`, `ztr gate`, `ztr verify --post-merge`, `ztr invariants`, `ztr run-phase` deterministic fake CLI 테스트가 PASS여야 한다.

[SoT — 반드시 읽을 것]
- `docs/ROADMAP_V2.md` — §3 Phase 6, 끝 절 불변 원칙
- `docs/DESIGN.md` — Boundaries: 판단·편집·머지 금지
- `docs/ZRT_V2_DIAGNOSIS.md` §6 파이프라인 릴레이
- `docs/discovery/ztr-v2/validation_plan.md` — PASS vs NOT CLAIMED
- `docs/LESSONS_LEARNED.md` — 새 LESSON 발견 시 append-only

[이번 범위]
1. 실제 프로젝트 1페이즈를 작은 범위로 선택해 `ztr run-phase`로 dogfood한다.
2. full E2E를 처음으로 claim할 수 있는지 검증하되, 실패 시 PASS로 위장하지 않고 `NOT CLAIMED` 또는 `BLOCKED`로 남긴다.
3. 실행 커맨드, 모델 식별자, 캡처 디렉터리, envelope stdout을 재현 가능하게 기록한다.
4. 새로 발견한 운영 실패는 `docs/LESSONS_LEARNED.md`에 `LESSON-NNN` 형식으로 append한다.
5. finding을 출력해야 하는 경우 M2 5필드 `severity / finding / evidence_or_repro / impact / recommendation`을 유지한다.

★ 명시적 out-of-scope ★
- ZRT 코드의 새 기능 확장
- 리뷰 자연어 의미 파싱 또는 설계 수락 판단 자동화
- 자동 소스 편집·머지·rollback
- 대시보드/web 변경
- `ztr roundtable --once`

[부착점/대상]
| 위치 | 내용 |
|---|---|
| `.ztr/run-phase/` | dogfood 실행 캡처 |
| `docs/ROADMAP_V2.md` | Phase 6 결과 기록 |
| `docs/LESSONS_LEARNED.md` | 새 운영 교훈 append |
| `docs/prompts/v2_phase7.md` | 후속 페이즈가 필요할 때만 생성 |

[검증/DoD]
1. dogfood run: `ztr run-phase`가 단일 Envelope JSON을 출력하고 exit code 계약을 지킨다.
2. 캡처: 각 leg의 stdin/stdout/stderr/envelope 파일이 재현 가능한 경로에 저장된다.
3. 회귀: `python -m ruff check src tests` PASS, `.venv\Scripts\python.exe -m mypy src` PASS, `python -m pytest -q` GREEN.
4. dogfood 후: `ztr review --changed`, `ztr verify --post-merge --changed`, `ztr invariants` PASS.
5. 리뷰 ALL PASS: 별도 Reviewer + Nitpicker 7b.

[완료 보고]
변경 파일 / dogfood 대상·명령 / 캡처 경로 / 검증 결과 / PASS는 어디까지·NOT CLAIMED·가정 / 새 LESSON append 여부.
