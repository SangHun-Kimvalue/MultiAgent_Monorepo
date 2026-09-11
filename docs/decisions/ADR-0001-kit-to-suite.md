# ADR-0001 — 방법론 키트 → AI 개발 스위트 승격

- 상태: ACCEPTED
- 일자: 2026-06-13
- 맥락(why·여정·기각안): `methodology/docs/SUITE_NARRATIVE.md` 참조 (이 ADR은 **결정사항만** 담는다)
- 관련: `methodology/docs/PHASE_CYCLE_ORCHESTRATOR_DESIGN.md`, `methodology/docs/METHODOLOGY_KIT_ROADMAP.md`, `D:\ZRT\docs\ROADMAP_V2.md`(ztr 흡수 후 `runtimes/ztr/docs/`)

> 주: 본 ADR은 스위트 루트 `docs/decisions/`로 **hoist 완료**(MultiAgent_Monorepo, 2026-06-13) — 컴포넌트 내부가 아니라 스위트 레벨 결정이므로. 내러티브/설계 문서는 링크 무결성을 위해 우선 `methodology/docs/`에 둔다(물리 hoist는 후속 정리).

## 결정

**AD-1 — 정체성 승격.** `multiagent-methodology`를 "방법론 키트(캐논+스킬, markdown 마켓플레이스)"에서 **"AI 개발 스위트"**로 승격한다. 누적 흡수(ztr → preen → ACP)의 귀결을 표류가 아니라 명시 결정으로 확정.

**AD-2 — 새 엄브렐러 레포.** 스위트는 **MM 개명이 아니라 새 엄브렐러 레포**로 구성하고, 기존 3프로젝트를 컴포넌트로 흡수한다:
- methodology 컴포넌트 = 현 MM (캐논 + 스킬 + 어댑터 + config)
- mechanical runtime 컴포넌트 = ztr (`D:\ZRT`)
- control plane 컴포넌트 = ACP (`D:\Dashboard`)

**AD-3 — 흡수 방식 = git subtree + 계약 seam.** 흡수는 `git subtree`(이력 보존, 단일 clone). 컴포넌트 간 결합은 **계약(envelope JSON / exit code 0·1·2·124·70 / 이벤트)으로만** 한다. 캐논(중립 markdown)과 런타임 코드는 **import/디렉터리 비융합**. seam은 구현을 갈아끼우게 하되 계약은 공유한다.

**AD-4 — 결정/이주 분리, 단계화(빅뱅 금지).** 본 승격 결정은 지금 확정하되 물리 이주는 의존 트리에 맞춘다: ztr subtree 흡수 = ztr Phase 8(resume 체인) 착수 시 / ACP subtree 흡수 = 오케스트레이터 골격 동작 후. 세 레포 동시 머지 금지(미완 컴포넌트 조기 융합 차단). (→ 개정 A1)

**AD-5 — Mechanical 역할 디스패처 = `preen`.** Mechanical 역할의 단일 진입점은 `preen`이며 `project.config`의 백엔드(`ztr review` / `run_nit` / 외부)로 위임한다. **"nitpicker"는 비 load-bearing 구어 별칭**으로만 잔존 — 코드·config·캐논의 식별자에서 은퇴.

**AD-6 — Observability(관제) 역할 단일 진입점 = ACP.** 관제는 ACP 하나로 SSoT화한다. ztr v1 대시보드(SessionStore 검증 이력)는 ACP의 **데이터 소스로 흡수**되고 독립 UI로 잔존하지 않는다. ("대시보드가 둘"인 혼동 제거 — AD-5와 같은 역할+진입점+백엔드 패턴.)

**AD-7 — 자율성 = 페이즈 경계마다 휴먼 확인.** 안쪽 루프(구현→리뷰→preen)는 자동, 페이즈 경계 3곳(설계 PASS / 커밋 / 다음 페이즈)은 사람 go/no-go. 완전 무인은 범위 밖. (ztr D5 · MS A5 승인게이트와 매핑.)

**AD-8 — 스위트 이름 = 후속 결정(TBD).** 엄브렐러 레포/마켓플레이스 이름은 본 ADR 시점 미정. 새 레포 생성 시 별도 ADR 또는 본 문서 개정으로 확정. (rename 마켓플레이스 비용 때문에 서두르지 않음.)

**AD-9 — 타깃 구조 3층.** 엄브렐러 레포는 `docs/`(스위트 캐논·결정·내러티브) / `methodology/`(MM subtree) / `runtimes/{ztr,acp}/`(런타임 subtree) 3층으로 격리. 격리는 코드 결합만 차단 — 계약 결합(AD-3)은 의도적 유지.

## 불변 (전 컴포넌트 공통)
- v1 함정 회피: 코드가 파싱하는 LLM 출력은 verdict enum + exit code뿐. 판단은 LLM 세션·사람.
- 캐논 = SSoT 1벌. 런타임/스킬/어댑터는 캐논을 가리킴(drift 금지).
- 스택 특화는 전부 `project.config`로(다형성).

## 개정 이력 (Amendments)

**A1 (2026-06-13)** — AD-4 문구 정밀화(결정 불변, 오독 차단).
- "ztr subtree 흡수 = Phase 8 **착수 시**"의 정확한 의미 = **spike S2 통과 후**.
- 운영 결정: **spike S2는 standalone `D:\ZRT`에서 진행**(de-risk-first). S2 통과를 흡수 게이트로 하고, 통과 후 `runtimes/ztr/`로 흡수 → Phase 8 resume 구현.
- 사유: "Phase 8 착수"가 "S2 포함 착수"로 읽혀 조기 흡수((b)안) 제안이 나옴. 결정/이주 분리·빅뱅 금지(AD-4 본문) 원칙과 일치하도록 정밀화.
- 출처: 설계 검토 leg finding (CHANGES_REQUESTED, major).
