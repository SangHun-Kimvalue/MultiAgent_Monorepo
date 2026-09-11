---
name: phased-implementation-handoff
description: >
  다단계(phased) 구현에서 현재 세션을 Planner로 사용해, 별도 Implementer 세션이 구현하고 별도 Reviewer 세션 +
  Nitpicker가 리뷰하도록 "계획 + 구현 프롬프트"만 저작하는 워크플로. 특정 모델 전용이 아니라 역할 기반이며,
  Planner 세션은 계획/프롬프트에 집중하고 구현·리뷰 루프에는 들어가지 않는다. 다음 상황에서 반드시 사용:
  "다음 페이즈 프롬프트 만들어줘", "구현 세션에 넘길 계획 세워줘", "이 작업 단계별로 쪼개서 프롬프트 만들어",
  "GPT/Codex/다른 세션에 넘길 구현 프롬프트", 진행 중인 phased 프로젝트의 다음 단계 브리핑/프롬프트 요청, plan→implement→review
  핸드오프 설계, 결정 로그·부착점이 든 로드맵 문서 작성/갱신, 현재 작업 브랜치 유지와 문서 기반 페이즈 경계 기록.
  사용자가 "스킬/툴로 만들자"고 한 그 프로세스가 바로 이것이면 트리거한다.
---

# Phased Implementation Handoff — 계획·프롬프트 저작 전용 워크플로

> 이 스킬은 multiagent-methodology **개발 방법론의 Planner-session handoff 어댑터**다. 원칙·루프·역할의 단일 소스(SSoT)는
> multiagent-methodology 레포의 `METHODOLOGY.md`/`MULTI_AGENT.md`/`DOC_TAXONOMY.md`. 아래는 그 캐논을 현재 세션에서 즉시
> 쓰도록 self-contained로 담은 것이며, 충돌 시 캐논이 우선. Codex/GPT 등 구현 세션용 대응 어댑터는
> `adapters/codex/AGENTS.md`처럼 각 surface별로 둔다.

## 0. 이 스킬의 한 줄 정의
**현재 세션은 "Planner + 프롬프트 저자"만 한다.** 구현은 지정된 Implementer 세션이, **출력(diff) 리뷰는 그 Implementer 세션이 자기 독립 에이전트 + Nitpicker로**(§8) 맡는다.
Planner의 리뷰 관여는 **입력 게이트 오케스트레이션(§5.5)**과 **커밋 전 최종 통합 확인(§8.5)** 두 지점뿐 — 구현·출력리뷰 루프 자체엔 들어가지 않는다(역할 오염 방지). 계획 세션의 가치는 탐색·결정·프롬프트에 있고,
구현/리뷰는 diff와 검증 증거 중심으로 별도 세션이 처리하는 편이 재현성과 교차검증에 유리하다.

## 1. 역할 분담 (이 구조를 깨지 말 것)
| 단계 | 누가 | 산출물 |
|---|---|---|
| 로드맵 + 페이즈별 구현 프롬프트 | **Planner 세션(이 스킬을 호출한 세션)** | 리포 로드맵 doc + 프롬프트(채팅 또는 doc) |
| **입력 리뷰**(프롬프트/설계, §5.5) | **Planner**가 독립 cross-lineage 에이전트 오케스트레이션 | PASS/수정 |
| 실제 구현 | 지정 Implementer 세션(예: Codex/GPT/Claude/Cursor) | 코드 |
| **출력 리뷰**(구현 diff, §8) | **Implementer 세션**이 독립 cross-lineage 에이전트 + Nitpicker 오케스트레이션 (자동 릴레이 모드는 외부 릴레이 소유 — §8 예외) | PASS/수정 |
| **최종 통합 확인**(§8.5) | **Planner** | 커밋 승인 |
| 통합 | 현재 작업 브랜치 유지 + 문서/커밋 메시지 기반 페이즈 경계 기록 | — |

## 2. 작업 진입 시 가장 먼저 (선행 점검 — 토큰 쓰기 전)
프롬프트를 만들기 **전에** 아래를 확인한다. 이걸 건너뛰고 잘못된 범위로 프롬프트를 생성하면 토큰 낭비다.
1. **다음 페이즈가 무엇인지 + 그 전제(precondition)가 충족됐는지.** 예: "C1은 B.5 완료 위에 얹힌다" → B.5가
   실제로 끝났는지 모르면 **먼저 물어본다.** 사용자가 "내 프롬프트대로만 진행 중"이라면 직전에 안 준 단계는 아직 안 된 것.
2. **scope가 갈리면(이 페이즈만 vs 묶기, 범용 vs 특정) 생성 전에 질문한다.** 한 번 더 묻는 게 잘못 생성보다 싸다.
3. 프로젝트 설정(§7) 로드: role/config 문서 + (있으면) 리포의 phased-handoff config. 없으면 핵심값을 물어본다.
4. Discovery 산출물(`requirements.md`, `design.md`, `validation_plan.md`, `open_items.md`, `handoff.md`)이 있으면 먼저 읽고,
   gate 판정이 `DISCOVERY_PASS`인지 확인한다. 미통과면 구현 프롬프트를 만들지 않고 부족 항목을 질문한다.

## 3. 계획 수립 방법 (해석·검증 방식)
깊은 절차는 `references/planning-method.md` 참고. 요지:
1. **SoT 문서 먼저.** 설계 문서에서 권위 스펙과 alias/역할명을 구분한다(같은 책임이 다른 이름으로 나오는 함정).
2. **코드 실측 — 추정/환각 금지.** 부착점·시그니처·컨벤션을 `Grep`으로 **file:line** 확인해 프롬프트에 박는다.
   라인은 이동하니 "시그니처로 재확인 후 부착"을 프롬프트에 명시.
3. **컨벤션 체크리스트:** 싱글톤 패턴 / JSON 라이브러리 / 경로는 PathManager류 경유 / pch 같은 빌드 특이점 /
   스레드 안전(콜백 스레드에서 UI·블로킹 금지).
4. **보류 항목을 명시적으로 확정**해 결정 로그에 박는다(저장 루트, 스레드 모델, id 포맷 등). "나중에 결정"으로 흘리지 않는다.

## 4. 페이즈 브리핑 주의점 (반복해서 배운 것들)
- **한 프롬프트 = 한 페이즈.** 범위를 묶으면 오분류·리뷰 부담이 커진다. C 같은 큰 단계는 C1→C2…로 쪼갠다.
- **"수집/구현 가능 ≠ 지금 다 구현."** 능력표를 우선순위표로 오해하지 않도록, 프로젝트 전략(예: collect-max-then-prune,
  또는 반대로 minimal-first)을 **프롬프트에 못박는다.** 전략은 프로젝트마다 다르니 하드코딩하지 말고 §7에서 읽어 반영.
- **전제조건을 프롬프트 [전제]에 명시.** "X가 머지된 상태에서 시작."
- **외부 리뷰는 비판적으로 필터.** 좋은 지적도 프로젝트 전략과 충돌하면 거부하거나 재해석한다. 무비판 수용 금지.
- **이미 있는 산출물 보호.** 진행 중 세션이 만든 파일이 있으면 "있으면 재생성 말고 정렬/완성"으로 지시.
- **브리핑은 짧고 차갑게:** 한 줄 정의 → 왜 지금 필요 → 핵심 작업 N개 → 범위 밖 명시 → 리스크 → DoD.

## 5. 프롬프트 작성 (고정 양식)
프롬프트 스켈레톤과 작성 규칙은 `references/prompt-skeleton.md`에 있다. 그 섹션 구조를 그대로 채운다:
`작업/[형상관리]/[전제]/[SoT]/[전략 리마인더]/[이번 범위(+out-of-scope)]/[부착점 file:line]/[아키텍처 결정]/[불변 원칙]/[검증·DoD]/[완료 보고]`.
`[전제]`에는 **저작 기준(날짜+HEAD)과 착수 시 스테일 자가검증**을 반드시 포함한다 — 사전작성 프롬프트는
저작→실행 사이에 스테일해진다(2026-07-01 실증: item9 프롬프트가 남아 있었으나 작업은 이미 `ae550fe`로
완료 — fresh 구동했으면 중복/회귀. β 프롬프트도 mid-session 스테일 전례).
Implementer 세션이 리포 접근 가능하면 간결하게(경로·시그니처·불변식 중심), 파일 접근이 없으면 코드 골격을 임베드한다.

## 5.5 핸드오프 전 프롬프트/설계 독립 검토 (입력 게이트 — §8과 대칭)
§8이 **산출(diff)**을 하드 게이트하듯, 저작한 IMPL_PROMPT/설계는 **Implementer에 넘기기 전** 독립 검토한다. 나쁜 프롬프트를 §8이 *늦게*(구현 낭비 후) 잡는 것보다, 넘기기 전에 설계·스펙 결함을 싸게 조기 차단한다(실증: 프롬프트 사전검토가 mechanical cwd 오류·session-map 빈캡처·resume-profile 누락·model 라벨 혼선·간접검증 공허 등을 매번 구현 前에 적발).
- **관점**: 계획 리뷰는 사용자 목적·필요성·최소 범위·실현 가능성을 본다. 핵심 기술 위험은 보고하되 구현의 함수·클래스·예외 처리를 선제적으로 모두 설계하지 않는다.
- **오케스트레이터와의 경계**: 이 입력 게이트는 §8 산출 게이트와 **별개**다. `phase-cycle-orchestrator`로 구동할 땐 그 스킬의 **설계 PASS 단계(오케스트레이터 §3 표 설계리뷰(2) = 이 §5.5 입력 게이트)가 §5.5를 충족**하므로 `cross-session-plan-review`를 **중복 호출하지 않는다**. 이 스킬을 단독(수동 Planner)으로 쓸 때만 아래를 직접 집행한다.
- **도구**: `cross-session-plan-review` 스킬(플랜/프롬프트 사전검토 전용 — 사실 검증 후 severity-ranked 비판)로 집행한다. 또는 §7의 프로젝트별 cross-lineage 메커니즘(예: Codex CLI 위임)이 정의돼 있으면 그것을 쓴다.
- **자기검토 금지(독립성)**: Planner는 **검토 번들만** 만들고, **별도 세션/CLI**가 검토를 집행한다. **같은 Planner 세션이 자기 산출물을 자기가 판정하지 않는다**(자기 스펙 자기 승인 = 입력 게이트 무력화, C4).
- **독립성 = §8 상속**: 다른 계열(cross-lineage) 우선 + 폴백 위계(① 다른 계열 CLI → ② 다른 계열 세션 → ③ 같은 계열 독립 컨텍스트=열화, 기록에 `same-lineage` 명시). 같은 계열은 같은 맹점·sycophancy 공유.
- **선언 블록은 `references/prompt-skeleton.md` 최상단 `[등급·예산 선언]` 에 둔다.** 선언 없는 프롬프트로는 게이트를 열지 않는다.
- **등급 선언이 먼저다(주체·시점)**: **Planner 가 착수 전에 작업 등급(L0/L1/L2)과 리뷰 예산을 선언하고 IMPL_PROMPT 상단에 박는다.** 선언 없이 게이트를 열지 않는다. 등급은 **최종 변경 표면**으로 판정한다 — 단일 파일·기계적 변경은 L1 이하다. 리뷰 중 등급을 올리려면 scope 재승인을 받는다(리뷰어가 등급을 올릴 수 없다).
- **등급별 강도**: L2(아키텍처·페이즈 경계)·**신규 트랙/신규 런타임/검증 안 된 통합 경로**·고위험 프롬프트 = **하드 게이트**(finding resolved 전 핸드오프 금지). L1 = 강력 권장. L0·사소·기계적 = 생략 가능(§2 Planner 자기 선행점검으로 충분).
- **종료 장치 (캐논 `METHODOLOGY.md` §3 상속 — 이 절만 보고 일해도 멈추게 하기 위해 명시)**
  - **예산은 두 축**(캐논 §3, 2026-08-14 개정): **구현 diff 심사(출력 게이트) L0 2 / L1 3 / L2 4**, **문서 심사(입력 게이트) L0 1 / L1 2 / L2 3**. 각 축 안에서 슬라이스 전체 semantic review 총량을 합산하며, **두 축은 서로의 잔여를 빌려오지 않는다.** 상한 없는 축은 없다 — 축 분리 자체가 예산 우회 경로가 되기 때문이다(입력 게이트 단독 7라운드 사례).
    - ⛔ **한 축을 소진했다고 다른 축에서 같은 대상을 다시 심사하지 않는다.** 문서 축 3회를 쓴 뒤 같은 산출물을 "구현 diff"로 재분류해 4회를 더 쓰는 것은 축 분리가 아니라 **증축**이다. 축은 대상이 가르지 순서가 가르지 않는다.
  - **증축 금지**: 선언된 예산을 늘리지 않는다. 예외는 보안·인증·데이터 손실 또는 **재현된 false PASS** 뿐이며, 그때도 별도 emergency slice 로 분리한다.
  - **종료 경계**: 예산을 소진했는데 PASS 가 아니면 **PASS 를 만들기 위한 추가 리뷰를 호출하지 않는다.** 현재 verdict·미해결 finding·NOT CLAIMED 를 그대로 Human Gate 에 반환한다.
  - **계약 동결**: Human Gate 1 이후 acceptance contract 는 동결이다. **Reviewer 가 새 요구·새 invariant 를 blocker 로 추가할 수 없다.** 필요하면 슬라이스를 멈추고 scope 재승인을 받는다.
  - **Gate 1 이전에도**: 사용자 요구로 추적되지 않는 새 범위를 제기한 finding 은 `ACCEPT` 전에 사용자 확인 또는 `DEFER_OUT_OF_SCOPE`(캐논 §3, [[LESSON-M068]]). 기존 요구를 충족하기 위한 설계 보정은 새 범위가 아니다.
  - **재현 없는 P2/P3 는 blocker 가 아니다.** 정적 가능성·문서 문구 정합성은 backlog 로 기록하고 verdict 를 막지 않는다.
  - **L0·L1 문서 축 R2 이상은 직전 라운드에 `P1` 이 있을 때만 연다**(캐논 §3, T21-P4). `P2`·`P3` 는 Planner 가 처분(`ACCEPT` 반영, 또는 근거 있는 `REJECT_*`/`DEFER_OUT_OF_SCOPE` = resolved)하고 재심사 없이 닫는다. L2·하드 게이트 문서와 구현 diff 축에는 적용하지 않는다.
  - **four-way disposition 은 매 라운드 필수**다(`ACCEPT` / `REJECT_FALSE_POSITIVE` / `DEFER_OUT_OF_SCOPE` / `REJECT_OVERENGINEERING`, 근거 필수 — 캐논 `MULTI_AGENT.md` 상속). **예산 소진 시**에는 그 처분을 들고 사람에게 올린다.
- finding은 §8 정형(severity/finding/evidence/impact/recommendation). **하드 게이트 finding은 반영하거나, Planner가 거부 사유를 기록해 `resolved` 처리한 뒤 핸드오프한다. unresolved P1/P2는 핸드오프 금지.** 좋은 지적도 프로젝트 전략과 충돌하면 재해석·거부하되 **사유를 남긴다**(§4 "외부 리뷰 비판적 필터" — 무비판 수용 금지).

## 6. 로드맵 문서
페이즈가 2개 이상이거나 다중 세션 협업이면 리포에 로드맵 doc을 둔다(다음 세션의 cold-start 비용을 줄이는 핵심 핸드오프).
구조는 `references/roadmap-template.md` 참고: 결정 로그 / 페이즈 개요(상태) / 부착점표(file:line) / 페이즈별 DoD / 불변 원칙 /
(수집형 작업이면) 수집표 + 우선순위. 프롬프트는 채팅 전용도 가능하지만 로드맵은 파일로 남긴다(채팅은 세션을 못 넘는다).

## 6.5 문서·형상관리 규율 (다중 세션 핸드오프의 핵심)
상세는 `references/doc-management.md`. 요지 — **다음 세션/도구가 맥락을 싸게 재구성하게** 만든다:
- **표준 루프:** Sync-In(로드맵+HANDOFF+lessons+코드 읽기) → Decide/Author → **Input Review(§5.5 입력 게이트, Planner 소유)** → Implement → Verify(실행 증거) → **Output Review(§8 산출 게이트, 구현 세션 소유)** → **Planner 최종 확인(§8.5)** → 사용자 "커밋해" → Sync-Out(HANDOFF·lessons 갱신).
- **페이즈 종료 게이트(§8, 구현 세션 소유):** Implementer 구현 완료 → **구현 세션이 자기 diff에 대해 독립 Reviewer 서브에이전트(cross-lineage) 실행** + Nitpicker(Implementer) **2갈래 PASS 확인** → **결과를 Planner에 보고** → **Planner 최종 전체 확인(§8.5)** → 그 다음에야 커밋. 리뷰 레그를 `NOT CLAIMED`로 닫고 넘어가지 않는다(환경/장비 검증만 `NOT CLAIMED` 허용).
- **HANDOFF.md:** 다음 작업자용 현재상태 스냅샷(최신 위로). 가정/TBD/재현명령 명시 + **증거 정직성**("PASS는 X까지 / NOT CLAIMED: Y").
- **lessons/<module>.md:** append-only, WHAT 말고 **WHY/LESSON**.
- **결정:** 작은 건 로드맵 결정 로그, 큰 아키텍처/페이즈 경계는 ADR.
- **작업 등급 L0/L1/L2 + Timebox:** L2(아키텍처·페이즈 경계)는 한 세션에 바로 구현 금지. 같은 축 N회 실패하면 멈추고 보고(무한 수정 루프 차단). → 프롬프트의 [불변 원칙]에 timebox를 박는다.

## 7. 프로젝트 설정 (per-repo, 갈아끼움)
범용 코어(§0~6)는 그대로 두고, 프로젝트별 값만 분리한다. 우선순위로 읽는다:
1. 리포의 phased-handoff config (있으면)  2. 리포 role/agent 지침  3. 없으면 사용자에게 핵심값 질문.
채울 항목 예시는 `assets/project-config.example.md` 참고:
- **빌드/검증 명령** (예: `cd Src && ./build.sh rc`, 또는 독립 test 컴파일 명령)
- **버전/RC 규칙** (예: `RC_HISTORY.txt` + `VersionInfo.h` 동기, 커밋은 사용자 확인 후)
- **Nitpicker 경로/모델** (리뷰 루프 §의 명령에 주입)
- **독립 Reviewer 실행 계약** (active executor CLI/계열, reviewer CLI/계열, argv shape, artifact/raw-output 경로, 폴백 단계)
- **코드 컨벤션** (싱글톤/JSON/경로/스레드 규칙)
- **형상관리 규칙** (현재 브랜치 유지, 브랜치/태그 생성 금지, 페이즈 경계 기록 방식)

## 8. 리뷰 루프 (프롬프트의 [검증·DoD]에 항상 포함)
두 갈래를 **병행하고 둘 다 PASS해야 페이즈 완료**(advice 아님, **하드 게이트**):
- **별도 Reviewer(구현):** diff + 승인 계획·로드맵·프롬프트 기준 구현 리뷰(목적 달성, 부착 정확성, 누락 경로, 스레드/race, 본문 무수정,
  결정·범위 준수, 결함·회귀, 검증 증거, 과설계 무비판 수용 여부). 구현 중 계획 결함은 보고하되 새 목적·acceptance를 임의 추가하지 않으며, 설계 적합성 확인은 유지한다. ALL PASS까지.
  - **다른 계열(cross-lineage) 독립 에이전트 CLI 우선.** 구현 리뷰는 Implementer와 **다른 모델 계열**의 독립 에이전트 CLI로 받는 것을 기본으로 한다(예: Implementer=Claude → Reviewer=Codex(GPT 계열)/Gemini CLI). 같은 계열은 같은 맹점·sycophancy를 공유하므로 **계열 다양성이 교차검증의 핵심 독립성**이다(C4 "자기 구현 자기 승인 금지"를 모델 계열 수준으로 강화).
    - **폴백 위계(하드 게이트는 불변):** ① 다른 계열 CLI(권장) → ② 다른 계열 세션 → ③ 다른 계열이 전혀 없을 때만 같은 계열의 독립 컨텍스트 서브에이전트. ③은 **열화 모드**이며 review 기록에 `same-lineage(계열 독립성 미확보)`를 명시해 갭이 보이게 한다.
    - **생략·`NOT CLAIMED` 금지.** "다른 계열 CLI 부재"는 스킵 사유가 아니라 폴백 위계를 한 칸 내려가라는 신호다. 어느 단계든 독립 컨텍스트로 **반드시 집행**한다.
  - **소유자 = Implementer 세션(출력 게이트).** 구현 세션이 자기 diff에 대해 **독립 컨텍스트(cross-lineage) 서브에이전트를 띄워 집행**하고 결과를 **Planner에 보고**한다. 구현 세션이 *자기 prose로 자기 코드를 승인*하는 게 아니라(그건 C4 위반), **별개의 독립 에이전트가 판정**하므로 독립성은 유지된다 — §5.5 입력 게이트(Planner가 자기 프롬프트를 독립 에이전트로 검토)와 **대칭**이다. Planner는 이 레그를 인라인으로 대신 떠안지 않고, 커밋 전 **§8.5 통합 확인**만 수행한다. (Implementer는 리뷰를 빈칸으로 두지 말 것 — 자기 소유 게이트다.)
    - **예산 preflight (출력 게이트 단일 진입점, schema 2.1):** 구현 diff Reviewer 호출은
      **반드시 preflight 를 통과**한다. 직접 호출은 비정상 경로다.
      `python <skill-root>/scripts/review_budget_preflight.py --doc <phase 진행 문서> --slice <slice_id> --gate output|input -- <reviewer 명령>`
      preflight 는 진행 문서의 단일 ```review-budget``` 블록을 읽어 `budget_limit` 을 `work_grade` 에서
      파생하고, **잠금 → 재검증 → 증가값 내구 저장 → 해제 → Reviewer 시작** 순서로만 호출을 승인한다.
      `next_round > budget_limit` 이면 **Reviewer 를 호출하지 않고** four-way disposition 을 요구한다
      (exit 3). 저장 실패는 호출 금지(exit 4), 저장 후 launch 실패는 소비 유지 + Human Gate(exit 5).
      ※ 적용 대상은 **구현 diff 출력 게이트**다. 문서·계획·프롬프트 심사는 대상이 아니다.
    - **가짜 PASS 차단(C4 강화) — 보고 필수 필드:** 구현 세션이 리뷰어를 lenient하게 고르거나 finding을 무시할 구멍을 막기 위해, 완료 보고에 다음을 **반드시** 첨부한다: active executor CLI/계열, reviewer tool/CLI version/계열/모델·집행 방식, command shape, review 시각(UTC), review base SHA와 reviewed paths, 폴백 단계(위계 중 어디), review artifact 경로, **verdict(enum)·exit code**(R5: prose 아님), raw output 경로, **모든 P1/P2 finding의 disposition**(반영 커밋 or Planner에 올릴 거부 사유). 배포되는 공용 형식은 이 스킬의 `assets/review-verdict.schema.json`(**2.1** — `slice_id`·`work_grade`·`budget_limit`·`rounds_consumed` 포함)을 따르고 `python <skill-root>/scripts/validate_review_verdict.py <artifact.json>` exit 0을 받아야 한다. 필수 필드 누락, 계열 비교 미기록, unresolved P1/P2가 남은 상태에서는 페이즈 완료·커밋 금지(= 10단계 7步 "구현 세션이 리뷰 검토"의 구체화).
      - Claude가 독립 Reviewer leg이면 `task_grade`와 `model_selection_reason`을 **산문 완료 보고**에만
        기록한다. 이 Claude 전용 규칙은 §5.5 입력 리뷰 PASS와 §8의 독립 출력 Reviewer·Nitpicker
        2-leg 하드 게이트를 보완할 뿐 대체하지 않는다. 구조화 review verdict의 실제 모델은 기존
        `reviewer.model`을 재사용하고 기존 schema validator가 검증한다. `task_grade`와
        `model_selection_reason`은 Planner가 §8.5에서 산문 완료 보고 존재·정합을 확인하며, 코드가
        산문 의미를 파싱해 모델이나 verdict를 자동 판정하지 않는다. `selected_model` 신규 필드나
        `review-verdict.schema.json` 확장을 만들지 않는다.
      - execution-preflight v1은 `opus`/`sonnet` moving profile과 선택적 단일
        `--output-format json`을 허용해 기본 Opus reviewer binding과 정렬됐다. 이는 source
        validation 범위만 갱신한 것이므로 preflight를 포함한 자동 end-to-end Opus 실행은
        별도 live relay 증거 전까지 **NOT CLAIMED**다. preflight 우회나 자동 E2E PASS 주장은
        금지하며, 수동 증거도 preflight 포함 E2E claim과 분리해 보고한다. 설치본 갱신은 별도
        승인·검증 전까지 **NOT CLAIMED**다.
    - **오케스트레이터 모드 예외:** `phase-cycle-orchestrator`/ztr 릴레이로 **자동 구동**할 땐 리뷰어 생성 주체 = **외부 릴레이**이고 Implementer는 리뷰어 존재를 모른다(ROADMAP_V2 D6 — "구현 세션이 자기 리뷰 오케스트레이션"보다 **더 강한 독립성 변형**). 위 "Implementer 세션 소유"는 **수동(단독 Planner) 핸드오프 모드**에 적용된다. 두 모드 다 "Planner 인라인 리뷰"가 아니며 출력 게이트를 충족한다.
- **Nitpicker (기계 체크):** 수정 파일마다(Implementer가 실행). 명령은 §7 설정의 경로/모델을 주입한 형태(예시는 `assets/project-config.example.md`).
  PowerShell에서 `--diff "$(git diff)"`를 직접 넘기면 한글/공백/따옴표/줄바꿈 인코딩이 깨진다 → **repo 래퍼(run_nit.py류)**가 있으면 우선.

**`NOT CLAIMED`의 허용 범위(중요 — 탈출구 오용 차단):** `NOT CLAIMED`는 *지금 실행할 수 없는* **환경/장비 게이트 검증**(실장비 육안, 하드웨어 의존 E2E 등)에만 쓴다. **리뷰 2갈래는 언제든 서브에이전트로 실행 가능 → `NOT CLAIMED`·생략 불가.** 두 레그가 PASS하기 전엔 페이즈 미완료이며 커밋도 보류한다. (재발 방지: 과거 Reviewer 레그를 `NOT CLAIMED`로 흘려 커밋된 사례가 반복됐다.)

**리뷰 finding은 정형으로** (근거 없는 "위험해 보임"으로 blocker 만들지 않기):
```
severity / finding / evidence_or_repro / impact / recommendation
```
**Review bundle은 최소로:** 변경 파일 목록 + 핵심 diff + 관련 문서 발췌 + 검증 출력 요약 + artifact 경로 + 남은 질문.
전체 리포 병합 파일은 만들지 않는다(토큰 낭비 + 초점 흐림). 커밋은 ALL PASS 후 **사용자 확인("커밋해")** 받고 진행.

## 8.5 커밋 전 Planner 최종 확인 (통합 게이트)
출력 게이트(§8) 2갈래가 **PASS로 보고된 뒤**(집행 주체는 §8의 모드 규약을 따른다 — 수동=구현 세션, 릴레이=외부 릴레이), **Planner가 통합 관점에서 한 번 더 확인**하고서야 커밋한다(사용자 "커밋해" 전 마지막 관문 = 의도한 10단계의 9步). **이건 코드 correctness 재판정이 아니다** — diff 의미·정확성 판정은 §8 소유(구현 세션)이며 Planner는 인라인 재리뷰하지 않는다. Planner가 보는 건 **오케스트레이션·정합 3가지**뿐:
- 보고된 2갈래 verdict가 **실제 PASS인가**(보고-실측 정직성 — `NOT CLAIMED`/미검증을 PASS로 위장하지 않았나. R5: verdict enum·exit code로만 판정, prose 재해석 금지).
- **근거 등급 승격 검사(delta 2026-09-02 — [[LESSON-M090]] 재발 형태 3 · [[LESSON-M092]])**: 이번 커밋이 §10·HANDOFF 에 적는 **완료·발견·권고 문장(헤드라인·함의 포함)** 이, 그 문장이 딛는 claim 을 원 문서(census·dogfood 보고서·Discovery 부속 문서·리뷰 결과)가 `NOT CLAIMED`·가설·추론으로 표기했는데도 **그 선언 등급보다 강한 주장을 하는가.** 있으면 **헤드라인 자체를 등급에 맞게 낮추거나**(⭐ 발견 → ⚠ 가설) **실측으로 유보를 해제한 뒤에만** 커밋 승인을 요청한다. ⚠ **"등급 표기를 같은 셀에 함께 옮겼는가"는 판별식이 아니다** — 실측 사례 `641c8fd`: census 가 "가설"이라 적은 수치를 §10 셀이 *"⭐ 더 중요한 발견"* 으로 헤드라인했고, **같은 셀에 "⚠ 가설이다"도 옮겨져 있었다**(등급 존재·동반 이동 검사로는 검출 0/2 — `docs/discovery/claim-grade-promotion-20260902/discovery_brief.md` E-1·E-2). ⚠ **산문 게이트(Planner 성실성 의존) — 집행 부재는 안 닫힌다.** 효과 미관측.
- 이번 페이즈 산출물이 **로드맵/결정 로그/이전 페이즈와 정합**하는가(범위 이탈·회귀·중복·전략 위반 없음).
- **HANDOFF·lessons·결정 로그가 갱신**됐는가(다음 세션 cold-start 대비) — **갱신 집행자는 아래 잔여 목록 게이트의 역할 3분할을 따른다**(수동=Implementer / 릴레이·오케스트레이터=Planner).
- **잔여 목록 stale 차단 게이트(하드 — delta 2026-07-28, 실측. 이 절이 계약 정본)**: 이번 페이즈가 **닫은 항목이 다른 문서에는
  여전히 "잔여/미완/OPEN"으로** 남아 다음 Planner가 끝난 일을 다시 집는 사고가 반복 관측됐다(한 세션 3회).
  **커밋 승인을 요청하기 전에** 반드시 수행한다(Sync-Out까지 미루면 승인·커밋 후가 되어 늦다).
  - **역할 3분할(모드 무관 — 중복·fail-open 방지)**:
    ① **입력 정의 = 항상 Planner**: 문서별 **active-state anchor**(현재 상태를 권위 있게 주장하는 heading·item ID·표 행 —
    ADR closure matrix 행, `## Current` 최상단 상태 블록, PHASES 현재 상태 요약 등)와 **각 anchor의 기대 최종 상태**,
    `canonical residual string`, 닫힌 **item ID와 별칭/표기 변형 검색어**, **검색 root와 exclude**(생성물·vendor·archive 제외),
    **집계 재계산 대상**(`DONE n / PARTIAL n / OPEN n`).
    ② **집행(검색·문서 편집·증적 생성)**: **수동 모드 = Implementer**(프롬프트 [검증·DoD] 항목 6으로 전달) /
    **릴레이·오케스트레이터 모드 = Planner**(Implementer는 SoT를 편집하지 않으며, implementer 프롬프트에는
    **"잔여 목록 Sync-Out은 Planner 소유 · Implementer 편집 금지"** 경계 한 줄만 남긴다).
    ③ **최종 판정 = 항상 Planner**(여기 §8.5에서). 집행자가 누구든 판정 소유자는 하나다.
  - **hit 분류(결정론)**: 각 hit에 **분류 + 근거 + 대상 anchor**를 기록한다.
    `active-state` → **canonical 문자열로 통일**(수정 필수) / `historical` → **원문 보존 + 분류 근거 기록이 기본**(수정 불필요) /
    `무관` → 근거 필수.
    - **historical에 날짜 명시 인접 정정을 "추가로" 붙여야 하는 객관적 조건**(아래 중 하나라도 해당할 때만):
      (a) 그 문장이 **현재형으로 상태를 단정**한다("~는 잔여다/미완이다") · (b) **날짜·시점 라벨이 없어** 현재 판정으로 읽힌다 ·
      (c) **active-state anchor와 같은 문서의 같은 섹션**에 있어 인접 오독 위험이 있다.
      해당 없으면 **원문을 건드리지 않는다**(이력 훼손·churn 방지).
  - **성공 조건**: 열거된 active-state anchor가 **전부** canonical 상태와 일치 · historical hit는 **근거가 기록**(+위 조건 해당 시
    인접 정정 완료) · **미분류 hit 0** · 집계 수치 일치.
    **미열거 active-state 후보 발견 · 설명 불가 hit · 집계 불일치 = `BLOCKED`**(구현 세션 또는 입력 정의로 되돌린다).
  - **Discovery 열거 전건 처분 게이트(하드 — delta 2026-09-01, [[LESSON-M089]] 적용 2)**: 위 잔여 목록 게이트와 **방향이 반대다.**
    잔여 목록 게이트는 *닫은 항목이 다른 문서에 여전히 OPEN 으로 남았나*(**false-open**)를 보고,
    이 게이트는 *열거된 항목이 처분 없이 사라졌나*(**silent-drop**)를 본다 — 서로 대체하지 않는다.
    실측 사례: Discovery 가 순서 의존으로 고정한 `A → B → C` 중 **Slice C 가 착지·비움·이월 어느 것도 아닌 채
    추적에서 빠졌고, 트랙 완결 선언까지 아무도 잡지 못했다**(T10, 2026-09-01).
    - **발동 조건(비례성 — 매 페이즈가 아니다)**: 이번 커밋이 진행 SoT 에 **트랙·슬라이스 체인의 완결/종결/비움을
      표시**할 때만 수행한다. 일반 페이즈 커밋에는 걸지 않는다.
    - **① 입력 정의 = Planner**: 그 트랙의 **Discovery 산출물이 열거한 슬라이스·요구사항 전건 목록**과
      각 항목의 **소재(`파일:섹션`)**. ⛔ 진행 SoT 의 완료 서술을 목록의 출처로 쓰지 않는다 —
      **누적 서술은 누락을 드러내지 못한다**(M089).
    - **② 대조**: 각 항목이 **착지 / 비움 / 이월 중 정확히 하나**로 처분됐고, 그 처분의
      **근거 위치**(커밋 SHA · closeout 문서 · SoT 셀)가 특정되는가.
    - **성공 조건**: 열거 항목 **전건**이 처분 + 근거 위치 특정. **처분 없는 항목이 1건이라도 있으면 `BLOCKED`**
      — 완결 선언을 철회하고 그 항목의 처분을 먼저 만든다.
      ⛔ **"목록에 없다"는 처분이 아니다.** "비움"도 **근거와 재개 조건**을 남겼을 때만 처분이다.
    - ⚠ 이 게이트도 §8.5 의 나머지처럼 **산문이며 Planner 성실성에 의존한다.** 스크립트화는 **T19** 소관이며,
      여기 추가로 집행 부재가 닫혔다고 적지 마라.
finding이 보이면 **결함 종류와 모드에 따라 owner를 가른다**(무주공산·역할 오염 동시 차단):
- **코드·구현 diff 결함 → 항상 구현 세션에 되돌린다.** Planner가 diff를 직접 고치거나 재리뷰로 때우면 역할 오염이다.
- **상태 문서 정합 결함(위 잔여 목록 게이트 포함) → 집행 분담을 따른다**: **수동 모드 = Implementer에게 되돌림** / **릴레이·오케스트레이터 모드 = Planner가 직접 수정**(그 모드에서 SoT 편집은 원래 Planner 소유이므로 implementer-blind 위반이 아니다). 어느 쪽이든 **최종 판정은 Planner**.
ALL 정합일 때만 사용자에게 커밋 승인을 요청한다.

## 9. 형상관리 규약
현재 체크아웃된 작업 브랜치를 유지한다. 새 브랜치 생성, 브랜치 전환, 태그 생성은 사용자가 명시적으로 요청한 경우에만 한다.
페이즈 경계는 `HANDOFF`, 로드맵 결정 로그, 커밋 메시지에 기록한다.

## 10. 안티패턴 (하지 말 것)
- 잘못된 scope/전제로 프롬프트부터 생성(→ §2 선행 점검 먼저).
- 부착점 file:line을 추정으로 적기(→ Grep 실측).
- 한 프롬프트에 여러 페이즈 욱여넣기.
- 외부 리뷰 무비판 수용으로 프로젝트 전략을 뒤집기.
- Planner 세션이 구현/**산출 diff 리뷰(§8)**를 인라인으로 직접 떠안기(역할 오염). ※ 출력 게이트(§8) 리뷰는 **구현 세션이 자기 독립 에이전트로** 집행한다 — Planner의 정상 임무는 **입력 게이트(§5.5) 검토 오케스트레이션**과 **커밋 전 최종 통합 확인(§8.5)**이지, diff 리뷰를 자기가 돌리거나 직접 코드 짜는 게 아니다.
- **리뷰 레그(특히 별도 Reviewer)를 `NOT CLAIMED`/생략으로 닫기**(→ §8: 환경 게이트에만 `NOT CLAIMED` 허용, 리뷰는 서브에이전트로 집행). 가장 자주 반복된 누락.
- 프롬프트만 채팅에 남기고 로드맵을 파일로 안 남겨 다음 세션이 cold-start로 재탐색.
