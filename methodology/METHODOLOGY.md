# 개발 방법론 — 캐논 (Single Source of Truth)

provider/언어 중립. 이 문서가 원칙·루프의 **단일 소스**다. Claude 스킬·Codex `AGENTS.md`·Cursor 규칙 등 모든 어댑터는
이 문서를 가리키거나 요약+출처로 인용한다(중복 정의 금지 → drift 방지). 스택·도구 특화 값은 여기 적지 않고
**프로젝트 config**(`config/project.config.example.md`)에서 정의한다.

본 방법론은 **인간·AI 에이전트에 동일 적용**된다. 역할/핸드오프는 `MULTI_AGENT.md`, 문서 체계는 `DOC_TAXONOMY.md` 참조.

---

## 1. 엔지니어링 원칙 (위반 시 리뷰 blocking)

| # | 원칙 | 뜻 |
|---|---|---|
| **C1** | 질문 우선 (넘겨짚지 말고 질문) | 명세가 모호하면 **착수 전 질문**. 산출물에 "가정(assumption)" 섹션 필수(없으면 "없음" 명시). 침묵한 가정이 최대 리스크. (AI: 토큰 쓰기 전 scope가 갈리면 질문) |
| **C2** | 실측 (넘겨짚지 말고 확인) | 외부 거동·코드 위치를 "아마"로 가정 금지. **로그/코드 실측**(부착점은 file:line). "관측 없는 경로 = 없는 경로." |
| **C3** | Fail-fast (조용한 fallback 금지) | mock/대체 경로로 내려가면 명시적 경고. 실패는 명시적 에러로. 성공한 척 넘어가는 경로 금지. |
| **C4** | 리뷰 검증 (자가→교차) | 머지 전 별도 리뷰어(인간 또는 별도 에이전트) 승인. 리뷰는 스타일이 아니라 **결정 준수 감사**. |
| **C5** | YAGNI (과설계 금지) | 미래의 N개를 위한 추상화 금지. 실수요 발생 시 확장(§4). |
| **C6** | 재현성 (버전 고정·재현) | 모델/도구/패키지/프로파일 버전 고정, seed·재현 명령 기록. 재현 불가 실패는 디버깅 불가. |
| **C7** | 계약 명시 (공개 인터페이스) | 공개 경계(API/팩토리/엔트리)는 전·후조건 + 실패(예외) 목록 명시. C1이 불가능한 환경의 마지막 방어선. |

> 스택 특화 원칙(단위테스트 의무·테스트 피라미드·타입 안전·구조화 로깅+trace_id·예외 계층·프롬프트 분리·코딩 컨벤션)은
> 프로젝트마다 다르므로 **config에서 정의**한다. 캐논은 위 7개 중립 원칙만 강제한다.

## 2. 표준 작업 루프

> **Phase 0 — Discovery Gate (신규 프로젝트/기능, L2급 착수 전):** 루프에 들어가기 전, 모호함과 실패 조건을 먼저 줄인다.
> "질문 → 설계 초안 → 수석 반박 → 수정 → 산출물"로 진행하며 **구현하지 않는다.**
> - 산출(최소): `role_assignment` / `requirements` / `design` / `validation_plan` / `open_items` / `handoff`. (상세: `phase0-discovery-interview` 스킬)
> - gate 판정: `DISCOVERY_PASS`(→ Planner 역할로 핸드오프) / `DISCOVERY_HOLD`(추가 질문·PoC) / `DISCOVERY_REJECT`(문제정의 재작성).
> - 졸업 조건: `open_items`의 TBD 0 · PASS 레벨(unit/deterministic/live/E2E) 명시 · SSOT·소유권·경계 명시 · 수석반박 응답.
> - 소규모(L0/L1)는 Phase 0 생략하고 바로 아래 루프.

```
Sync-In → Decide → Implement → Verify → Review → Sync-Out
```
- **Sync-In(시작 전, MUST):** 로드맵 + `HANDOFF`의 현재 상태 + 대상 모듈 `lessons` + 관련 코드/테스트를 읽는다.
  문서가 stale이면 코드·테스트를 먼저 믿고 문서를 함께 갱신. (AI: 응답에 "재독 완료" 명시 — `MULTI_AGENT.md`)
- **Decide:** 이번 작업의 "할 것 / 안 할 것 / 채택 / 폐기 / 검증 기준"을 고정. 작은 건 HANDOFF, 큰 결정은 로드맵 결정로그 또는 ADR.
- **Implement:** 결정 범위 안에서만. 범위 밖 확장·미허용 인프라 도입 금지. 과설계 제안은 "검토했으나 보류"로 기록.
- **Verify:** 정적 diff가 아니라 **실행 증거**(명령 + 결과 + artifact 경로). 못 돌린 검증은 이유를 남긴다.
- **Review:** C4. 결정 준수·범위·fail-fast·테스트/artifact 충족·과설계 무비판 수용 여부 감사. finding은 정형(§아래).
- **Sync-Out(종료, MUST):** `HANDOFF` 갱신(현재 상태/가정/미완/다음 단계, **PASS는 어디까지·NOT CLAIMED 분리**) + `lessons` append(WHY/LESSON).

**리뷰 finding 정형** (근거 없는 blocker 금지): `severity / finding / evidence_or_repro / impact / recommendation`.
**리뷰 bundle 최소**: 변경 파일 + 핵심 diff + 관련 문서 발췌 + 검증 출력 + artifact 경로 + 남은 질문. 전체 리포 병합 금지.

**형상관리:** 현재 체크아웃된 작업 브랜치를 유지한다. 새 브랜치 생성, 브랜치 전환, 태그 생성은 사용자가 명시적으로 요청한 경우에만 한다.
페이즈 경계는 `HANDOFF`/로드맵/커밋 메시지에 기록한다. 커밋은 리뷰 통과 후 사용자 승인 시.

## 3. 작업 등급 + Timebox
| 등급 | 기준 | 절차 |
|---|---|---|
| L0 | 문서/typo/작은 테스트 | Sync-In / Verify / Sync-Out |
| L1 | 모듈 내부 구현, schema/factory/mock | Decide / Verify / Review 필수 |
| L2 | 아키텍처·페이즈 경계·외부 adapter·orchestrator | **한 세션에 즉시 구현 금지** — 결정·검증기준 먼저 고정(필요 시 구조 검토 request) |

**Timebox:** 같은 축 수정이 L0 2회 / L1 3회 / L2 checkpoint마다 안 풀리면 **멈춘다** → 실패 로그 + 시도/원인 +
다음 접근이 왜 다른지 + 필요 시 방향 질문. (포기가 아니라 무한 수정 루프 차단)

## 4. 확장(YAGNI) 원리
교체 가능성이 있는 경계는 **추상 인터페이스(ABC 등) + 구현 1개 + Mock**으로 출발. 두 번째 구현은 **실수요 발생 시점**에만 추가.
"여러 벤더가 다른 방식으로 같은 동작"일 때만 어댑터(런타임 스왑). 그 외엔 공개 API 계약만 고정(내부 재작성 허용) 또는
설정 스위치로 충분 — 이중 추상 금지.

## 5. 프로젝트 config로 미루는 것 (PARAM)
다음은 캐논이 아니라 `config/project.config.example.md`에서 정의: 빌드/검증 명령, 버전·RC 규칙, 코드 컨벤션(OOP/타입/예외계층/로깅),
테스트 전략·비중, 프롬프트 디렉토리 규칙, Nitpicker(또는 동급) 경로·래퍼·모델, 프로젝트 수집/구현 전략(예: collect-max-then-prune), 무수정 대상.

---
관련: 역할·핸드오프 `MULTI_AGENT.md` · 문서 체계 `DOC_TAXONOMY.md` · 양식 `artifacts/` · 어댑터 `adapters/` · 프로젝트값 `config/`.
