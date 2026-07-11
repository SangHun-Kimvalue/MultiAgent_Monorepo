# 문서·형상관리 규율 (다중 세션 핸드오프)

성숙한 사내 운영 프로토콜에서 추출해 경량으로 다듬은 문서/형상관리. 목적은 하나 —
**다음 작업자(다음 세션/다른 도구)가 같은 맥락을 싸게 재구성**하게 한다.

## 1. 표준 작업 루프
```
Sync-In → Decide → Implement → Verify → Review → Sync-Out
```
- **Sync-In(시작 전):** 로드맵 + HANDOFF의 `## Current` + 대상 모듈 `lessons/<module>.md` + 관련 코드/테스트를 읽는다.
  문서가 stale이면 코드·테스트를 먼저 믿고 문서를 함께 갱신.
- **Decide:** 이번 작업의 "할 것 / 안 할 것 / 채택 / 폐기 / 검증 기준"을 짧게 고정(로드맵 결정 로그 또는 HANDOFF 항목).
- **Implement / Verify / Review:** 결정 범위 안에서만. 검증은 정적 diff가 아니라 **실행 증거**(명령+결과+artifact 경로).
- **Sync-Out(종료 시):** HANDOFF 갱신 + lessons append + (설계 바뀌면) 로드맵/ADR 갱신.
  → Planner 세션도 동일: 프롬프트를 넘긴 뒤 HANDOFF에 "지금 상태/다음 단계"를 남긴다.

## 2. HANDOFF.md (다음 작업자용 현재상태 스냅샷)
리포에 `docs/HANDOFF.md` 하나. **최신 항목 최상단.** 작성 규칙:
1. 형식 헤더: `### YYYY-MM-DD — 작성자/도구 — 대상 모듈/페이즈`
2. 모호하면 **"미완(TBD)" / "unknown"** 으로 명시 — 넘겨짚기 금지.
3. **가정(assumption)을 반드시 열거** — 없으면 "없음".
4. **재현 방법**(명령/시드/env)을 빠뜨리지 말 것.
5. **증거 정직성:** "PASS는 어디까지"와 **"NOT CLAIMED(주장 안 함)"**을 분리해 적는다.
6. 본문엔 최근 3~5개만, 오래된 항목은 `docs/archive/handoff/`로 이동(삭제 금지).

항목 블록:
```markdown
### YYYY-MM-DD — <작성자/도구> — <대상>
- 무엇: <한 일>
- 산출물: <파일/경로>
- 검증: <실제로 통과한 명령들>
- 중요한 해석: PASS=<…> / NOT CLAIMED=<…>
- 가정: <…>
- 다음 권장: <다음 단계>
```

## 3. lessons/<module>.md (append-only 교훈)
- **WHAT은 git log로 충분 → WHY/LESSON만** 남긴다. 비-자명한 실패·결정·workaround·벤더 이슈.
- Append-only, 최신 위로, 추정은 "presumed" 표시.
- 블록: `맥락 / 시도·결정 / 결과·영향 / 교훈(action 한 문장) / 관련(PR·문서·링크)`.

## 4. 결정 기록 — 로드맵 결정 로그 vs ADR
- 작은 결정: 로드맵 **결정 로그 표**에 한 줄(저장 위치/스레드 모델/id 포맷 등).
- 아키텍처/페이즈 경계 같은 큰 결정: `docs/decisions/ADR-NNNN-<slug>.md` (제목/상태/맥락/결정/대안/결과).

## 5. 작업 등급 + Timebox (언제 천천히/멈출지)
| 등급 | 기준 | 절차 |
|---|---|---|
| L0 | 문서/typo/작은 테스트 | Sync-In / Verify / Sync-Out |
| L1 | 모듈 내부 구현, schema/factory/mock | Decide / Verify / Review 필수 |
| L2 | 아키텍처·페이즈 경계·외부 adapter·orchestrator | **한 세션에 바로 구현 금지** — 결정·검증기준 먼저 고정(필요 시 request-debate) |

**Timebox:** 같은 축 수정이 L0 2회 / L1 3회 / L2 checkpoint마다 안 풀리면 **멈춘다** → 실패 로그 + 시도/원인 + 다음 접근이 왜 다른지 기록 + 필요 시 사용자/리뷰어에 방향 질문. (포기가 아니라 무한 수정 루프 차단)

## 6. Phase Exit Checklist (페이즈 닫기 전)
- [ ] 구현 범위가 로드맵/PHASES와 일치
- [ ] unit/contract test 존재, mock은 명시적 test adapter(silent fallback 아님)
- [ ] factory 선택·실패 경로가 structured log로 관측됨
- [ ] HANDOFF 최신화 + 관련 lessons append
- [ ] (PHASE.md 사용 프로젝트) PHASE.md frontmatter 갱신: `current_phase` / `phase_status` / `updated_at`
      — 설계 무변경이어도 페이즈가 진행되면 갱신(관제소가 의존하는 진행 신호. 누락 시 plan-stale 경고 유발)
- [ ] Nitpicker/리뷰 수행, 과설계 제안은 채택/보류 이유 기록
- [ ] RC/버전 갱신(프로젝트 규칙)

## 7. 외부 공개/IP 분류 (선택 — IP 민감 프로젝트만)
외부 공개·라이선스·provenance 리스크가 있으면 `docs/DISCLOSURE_CLASSIFICATION.md` 류로 "공개 가능 / 내부 / 기밀"을
분류하고, 외부 asset은 license/provenance 확정 전 product 경로에 넣지 않는다. 일반 프로젝트는 생략.
