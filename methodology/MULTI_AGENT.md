# 멀티 에이전트 협업 — 역할·계약·핸드오프

`METHODOLOGY.md`(원칙·루프)의 동반 문서. "어떤 역할을 맡은 세션이 무엇을 하고, 사이에 무엇이 흐르는가"를 정의한다.
역할은 **모델이 아니라 세션에 부여**한다. 역할↔도구 매핑은 예시이며 프로젝트 config에서 바꿀 수 있다
(예: Planner=GPT, Implementer=Claude, Reviewer=Codex도 가능).

## 1. 에이전트 역할·계약

| 역할 세션 | 예시 매핑 | 책임 | 입력 | 산출 | 금지 |
|---|---|---|---|---|---|
| **Discovery** | 지정 인터뷰 세션 | 착수 전 질문·모호성 분류·gate 통과 판단 | 아이디어/요청 + 과거 lessons + config | requirements/design/validation/handoff 초안 | 구현 착수, 미분류 TBD 방치 |
| **Planner** | 지정 계획 세션 | 로드맵·결정·페이즈별 프롬프트 저작 | Discovery 산출물 + SoT 문서 + 코드 실측 | 로드맵(결정로그) + 구현 프롬프트 | 직접 구현/리뷰 떠안기 |
| **Orchestrator** | 지정 바깥 루프 세션 | 페이즈 간 역할 leg·산출물·Human gate 배선, 제어권 회수, 진행 보고 | 로드맵 + 역할별 artifact + gate evidence | handoff 배선 + finding disposition + phase ledger 보고 | 직접 구현, 자기 구현·설계 승인, LLM 산문 자동판정 |
| **Implementer** | 지정 구현 세션 | 결정 범위 내 구현 | 프롬프트 + 어댑터 지침 + config | 코드 + 검증 증거 + HANDOFF/lessons 갱신 | 범위 밖 확장, silent fallback |
| **Reviewer** | 지정 리뷰 세션 | 결정 준수 감사 | diff + 로드맵 + 프롬프트 | finding(정형) | 근거 없는 blocker, 자기 구현 자기 승인 |
| **Mechanical** | Nitpicker/local checker | 스레드/인코딩/계약 nit | diff | PASS/REJECT | 과한 framework 제안 무비판 수용 |
| **Human** | 사용자 | 결정 승인·커밋 게이트·방향 | 요약/finding | 승인/커밋 | — |

핵심: **역할은 세션 단위로 분리**한다. 같은 모델을 써도 Discovery/Planner/Orchestrator/Implementer/Reviewer 세션은 서로 다른 책임을 가진다.
Planner 세션은 구현·리뷰 루프에 들어가지 않고, Orchestrator 세션은 직접 구현하거나 자기 산출을 승인하지 않으며, Implementer 세션은 자기 구현을 자기 승인하지 않는다.

### 1.1 역할 판별 우선순위

프로젝트 루트의 `AGENTS.md`/`CLAUDE.md` 같은 surface adapter는 구현 전용 파일이 아니라 **역할 라우터 + 공통 방법론 요약**이어야 한다.
같은 프로젝트 폴더 안에서도 "문서 정리", "리뷰", "기획", "구현" 세션이 나뉠 수 있으므로, 폴더 하나에 Implementer 역할을 고정하지 않는다.

판별 순서:
1. 사용자가 명시한 세션 역할: "이번 세션은 Discovery/Planner/Orchestrator/Implementer/Reviewer".
2. 요청 의도:
   - 인터뷰/기획/착수 전/요구사항/문제정의 → Discovery
   - 설계/로드맵/작업지시서/프롬프트/다음 페이즈 → Planner
   - N페이즈 진행/리뷰 반영/끝까지 운전/역할 leg 배선 → Orchestrator
   - 진행/구현/수정/테스트/커밋 → Implementer
   - 검토/리뷰/크리티컬/수석 관점/문제점 → Reviewer
3. 세션 제목은 보조 신호로만 사용한다. 사용자 지시와 충돌하면 사용자 지시가 우선한다.
4. 역할이 모호하거나 파일 수정 여부가 갈리면 착수 전 질문한다(C1).

### 1.2 역할 독립성 계약 + 스킬 우선 (혼용 방지)

**리뷰는 두 leg로 분리되고, 핵심은 검토 대상의 *저자로부터 독립*이다:**
- **설계 검토(plan/design review)** — 설계 세션(Planner) 산출물(로드맵·핸드오프·결정)을 사용자 목적·필요성·최소 범위·실현 가능성 관점에서 리뷰. findings만 내고 **저작·코드편집 안 함**. 스킬 = `cross-session-plan-review`.
- **구현 리뷰(code review)** — 구현 diff의 승인 계획·범위 준수, 목적 달성, 결함·회귀, 검증 증거를 리뷰. **구현 세션과 독립한 에이전트가 집행**(C4: 자기 구현 자기 승인 금지). 기계 nit = `nitpicker-review`(Mechanical). 구현 중 발견한 계획 결함은 보고하되 새 목적·acceptance를 임의 추가하지 않는다.

**한 세션이 둘 이상 역할을 겸하지 않는다.** 특히 Planner(저작)와 설계 검토를 한 세션이 겸하면 자기 스펙 맹점이 생긴다 — 설계 검토 세션은 설계를 저작하지 않고, 저작 세션은 자기 산출을 자기 검토하지 않는다.

**컨텍스트 독립성과 전체 이력 상속은 별개다.** Reviewer가 저자와 다른 컨텍스트여야 한다는
계약은 전체 채팅 transcript를 복제하라는 뜻이 아니다. 독립 agent/session은 fresh context를 기본으로 하고,
`canonical anchors + exact targets + unresolved findings + delta + verification summary`만 적은 bounded bundle로
부팅한다. R2 이후에는 해결된 finding과 이전 산문을 재주입하지 않는다. 전체 이력이 꼭 필요하면 누락 사실,
bounded bundle로 표현할 수 없는 이유, 예상 입력 크기를 handoff에 명시한다. 상세 상한은 `METHODOLOGY.md §3.1`이 정본이다.

**집행(commit gate)**: Planner/orchestrator 세션은 **코드/스크립트(`install.sh` 등 tooling 포함)를 직접 구현하지 않는다.** 부득이 직접 구현했어도, 커밋 범위에 코드·스크립트 변경이 있으면 **author≠reviewer 독립 구현 리뷰(별도 컨텍스트/벤더 에이전트) PASS 증거 없이 커밋 금지.** `pytest`/`ruff`/`mypy`/`bash -n`/`--dry-run` 같은 self-verify는 필요조건일 뿐 **충분조건이 아니다.** "tooling/docs라 사소함"은 면제 사유가 아니다(순수 docs/메모리 변경만 예외). 이 게이트는 `zrt-phase-commit` Hard Gate로도 박혀 있다.

**독립 에이전트 에스컬레이션 (조건부)** — 설계 검토 세션은 설계 저자와 다른 세션이므로 **기본은 직접 검토**한다. 다음일 때만 추가 독립 에이전트를 호출한다: ① 대형·복잡 diff ② R5/보안 등 크리티컬 경계 ③ 심층 코드 검증 필요 ④ 1차 판정 애매. 리뷰 집행은 항상 **현재 동작 중인 모델/CLI와 다른 계열의 독립 CLI**로 수행한다(예: Codex 실행 중이면 Claude CLI, Claude 실행 중이면 Codex CLI). cross-vendor 독립이 같은 벤더 서브에이전트보다 맹점이 덜 겹친다. 원칙: **reviewer CLI 계열 ≠ active executor CLI 계열**, 가능하면 **reviewer 벤더 ≠ author 벤더**. 다른 CLI가 없으면 same-lineage 독립 컨텍스트로 폴백하되 열화(same-lineage)를 보고하고, 리뷰 생략으로 PASS 처리하지 않는다.

**스킬 우선(skills-first)** — 역할 판별(§1.1) 직후, 착수 전 **매칭되는 워크플로 스킬이 있으면 반드시 먼저 호출**한다(수동 진행 금지). 스킬이 세션에 없으면(미설치) 플러그인 설치를 안내하거나 해당 `SKILL.md`를 직접 읽어 규약을 따른다. (스킬을 안 쓰고 진행하는 것이 가장 흔한 규약 이탈이다.)

### 1.2.1 위임 결정 후의 provider-중립 profile tier

이 규칙은 agent 호출이 이미 결정된 뒤에만 적용하며, 기존 역할별 binding·작업 등급·리뷰 예산이 우선한다. `LIGHT`는 닫힌 조회·정리·기계 보조, `SESSION_DEFAULT`는 provider adapter binding 또는 CLI/session 기본값, `DEEP`은 명백한 고위험(아키텍처·보안·인증·데이터 무결성·복잡한 파일 간 원인분석)용 해당 provider 상위 profile이다. verdict를 산출하는 Reviewer/설계검토 leg는 기존 binding이 없을 때 `LIGHT`를 선택하지 않는다. 모든 tier에는 provider별 floor가 적용되고, 기본값이 floor 아래면 floor로 올린다; 이미 `DEEP`이면 재호출·추가 판정 없이 유지한다. 애매하면 `SESSION_DEFAULT`이며 non-default 이유는 한 줄이면 충분하다. 줄 수·토큰·키워드·점수표·분류 agent에 의한 자동 판정은 금지한다. 구체 floor·상위 profile은 프로젝트 binding이 소유한다.

### Role Transition Checkpoint

Decide 전과 요청 의도가 바뀔 때마다 현재 의도가 세션 역할의 책임·금지 범위 안인지 재판정한다. 현재 역할의 경계를 넘으면 즉시 작업과 tool call을 **`STOP`**하고, 같은 세션에서 역할을 바꾸지 않는다. 대신 현재 역할·확장된 의도·완료 증거·미통과 gate·next owner를 담은 **`ORCHESTRATOR_HANDOFF` artifact**를 남긴 뒤 Orchestrator가 별도 역할 세션을 배선하게 한다.

Reviewer leg가 종료되면 결과가 PASS이든 finding이 있든 제어권은 Orchestrator가 회수한다. 이는 Reviewer가 Implementer로 변하거나, 현재 세션이 다음 역할을 겸하는 전환이 아니다.

### 1.3 세션 부트 — 콜드스타트·압축 후 컨텍스트 유지

컨텍스트 압축(요약)은 손실적이라 핵심 문맥을 한 번씩 떨어뜨린다. 문서를 갱신해도, **자동 로드되는 채널은 프로젝트 루트 `CLAUDE.md`와 메모리 인덱스 둘뿐**이고 나머지(설계·로드맵·핸드오프·SKILL)는 세션이 직접 Read해야만 보인다 — 그래서 "정확한 문서"가 read-path 밖에 있으면 못 따라간다.

대응 = **보장 채널에 부트 의례를 박아 매 부팅마다 SoT에서 재유도**한다:
- **프로젝트는 루트에 `CLAUDE.md`를 둔다** — `config/CLAUDE.boot.template.md`를 복사해 채운다. 상단에 "새 컨텍스트·압축 직후 필수" 부트 절차(① `git log` + 진행 SoT 실측 ② 역할 확인 ③ 불변 재확인 ④ 동결 경계)를 넣는다.
- **메모리에 휘발성 진행상태를 박지 않는다**(stale 재발 원인). 메모리 = "어디를 보라" 포인터 + 안정 오리엔테이션만. 진행은 진행 SoT(§) + `git log` 실측.
- **요약·보고서를 ground truth로 신뢰하지 않는다** — 보고-실측 불일치는 반복 함정이다.

## 2. 핸드오프 아티팩트 흐름
```
[Human] 요청
   │
[Discovery] ── requirements/design/validation/handoff ──► [Planner]
   │                                                        │
   └── gate 미통과 시 재질문                                └── 로드맵(결정로그) + 페이즈 프롬프트 ──► [Orchestrator]
                                                                                                      │
                                                                                         별도 [Implementer]
                                                                                                      │ 구현 + 검증 증거 + HANDOFF/lessons
                                                                                         [Reviewer] + [Mechanical]
                                                                                                      │ 결과 회수
                                                                                         [Orchestrator] disposition
                                                                                           │             │
                                                                                  ALL PASS │             └── ACCEPT + 유효한 시작 승인 reference
                                                                                           ▼                         │
                                                                                  [Human] 승인 → 커밋     별도 Implementer → 동일 독립 Reviewer/Mechanical 재검증
```
- **프롬프트**는 채팅 전용 가능하지만 **로드맵·HANDOFF·lessons는 파일**(채팅은 세션을 못 넘는다).
- hcom 같은 교차 세션 도구는 선택형 **전송 계층**일 뿐이다. 메시지 도착은 Human Gate·review PASS·Git 파일 동기화를 대신하지 않으며, 교차 PC handoff에는 repo/branch/commit/artifact path를 함께 적는다. 운영·보안 정본은 [`docs/HCOM_CROSS_SESSION_RELAY.md`](docs/HCOM_CROSS_SESSION_RELAY.md).
- 프롬프트 필수 요소·리뷰 요청 양식은 `plugins/agent-workflow/skills/phased-implementation-handoff/references/prompt-skeleton.md`, finding 정형은 같은 스킬 `SKILL.md` §8, 처분 기록은 `artifacts/finding-disposition.md`. (구 경로 `artifacts/prompt-skeleton.md`·`artifacts/review-finding.md` 는 존재하지 않는다.)
- 모델명은 예시일 뿐이다. 프로젝트 시작 시 `role assignment`를 명시해 이번 작업에서 어느 세션이 Discovery/Planner/Orchestrator/Implementer/Reviewer인지 고정한다.

Reviewer finding은 전건 자동 적용하지 않는다. Orchestrator는 각 finding을 `ACCEPT`, `REJECT_FALSE_POSITIVE`, `DEFER_OUT_OF_SCOPE`, `REJECT_OVERENGINEERING` 중 하나로 disposition하고 evidence·rationale·owner를 `artifacts/finding-disposition.md`에 기록한다.

- `ACCEPT`만 유효한 페이즈 시작 승인 reference의 scope/timebox 안에서 기존 `ztr fix-prompt`의 수정 입력이 될 수 있다. 수정은 **separate Implementer**가 수행하고, 원래와 동일한 결정론 검증과 독립 Reviewer/Mechanical gate로 재검증한다. 이 재검증 의무는 **구현 diff 축**이다 — L0·L1 문서 축의 재심사 조건은 [`METHODOLOGY.md`](METHODOLOGY.md) §3(직전 라운드에 `P1` 이 있을 때만).
- `REJECT_FALSE_POSITIVE`와 `REJECT_OVERENGINEERING`은 원 finding을 반박하는 evidence가 필수다.
- `DEFER_OUT_OF_SCOPE`는 결함 부정이 아니며 owner와 후속 위치가 필수다.
- disposition은 수정 대상 선별이지 phase 승인이나 Orchestrator의 자기 구현 승인이 아니다. round별 별도 승인을 요구하지 않으며, 유효한 페이즈 시작 승인 reference의 scope/timebox 안에서는 four-way disposition→separate Implementer→동일 독립 Reviewer/Mechanical 재검증을 자동 진행한다. `BLOCKED`, scope 확대, disposition 모호성, 운영 위험, timebox 소진만 중간 Human escalation이며, 페이즈 종료 Human Gate와 독립 역할/R5 재검증은 대체하지 않는다.

### Phase Ledger Canon

phase ledger의 최소 필드는 `phase`, `status`, `current gate`, `evidence`, `PASS`, `NOT CLAIMED`, `next owner`다. 각 checkpoint에서 전체 ledger를 갱신·보고한다. 이 정의는 방법론의 단일 정본이며, 프로젝트별 실제 상태값과 전이 규칙은 해당 roadmap 또는 `PHASES`가 소유한다. ledger는 상세 프로젝트 관리 시스템을 대체하지 않는다.

## 3. AI 에이전트 특례 (정직성)
인간이든 AI든 §METHODOLOGY 루프는 동일 적용. AI는 추가로:
- **Sync-In 재독 완료를 응답에 명시** — "로드맵/HANDOFF/lessons 확인 완료".
- **가정을 모두 열거** — "가정(assumption)" 섹션(없으면 "없음"). 침묵한 가정 = 최대 리스크(C1).
- **증거 정직성** — 보고에 "PASS는 어디까지 / **NOT CLAIMED**(주장 안 함) / 가정"을 분리.
- **토큰 쓰기 전 질문** — 전제·scope가 갈리면 산출 전에 묻는다(잘못 생성보다 쌈).
- **교차 검증** — Implementer와 Reviewer는 다른 세션/에이전트(자기 구현 자기 승인 금지, C4).
  설계 리뷰는 가능하면 **다른 모델 계열(cross-lineage)의 독립 에이전트 CLI**로 받는다(같은 계열은 같은 맹점·sycophancy 공유). 다른 계열이 없으면 같은 계열 독립 컨텍스트로 폴백하되 열화(same-lineage)를 기록 — 생략 불가.

## 4. 작업 모드 (프롬프트 3종)
L2(아키텍처/페이즈 경계) 결정은 **debate** 먼저, 그다음 **implement**, 끝나면 **review**. 양식은 `artifacts/prompt-skeleton.md`.
- **debate**: 구현 전 구조 검토(파일 수정·범위 밖 인프라 제안 금지) → stance/evidence/risk/recommendation/confidence.
- **implement**: 범위·out-of-scope·검증 기준 명시 → 변경파일/검증/가정/남은 리스크 보고.
- **review**: 결정 준수·안전·회귀·누락 테스트 → finding 정형.
