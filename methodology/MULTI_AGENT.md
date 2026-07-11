# 멀티 에이전트 협업 — 역할·계약·핸드오프

`METHODOLOGY.md`(원칙·루프)의 동반 문서. "어떤 역할을 맡은 세션이 무엇을 하고, 사이에 무엇이 흐르는가"를 정의한다.
역할은 **모델이 아니라 세션에 부여**한다. 역할↔도구 매핑은 예시이며 프로젝트 config에서 바꿀 수 있다
(예: Planner=GPT, Implementer=Claude, Reviewer=Codex도 가능).

## 1. 에이전트 역할·계약

| 역할 세션 | 예시 매핑 | 책임 | 입력 | 산출 | 금지 |
|---|---|---|---|---|---|
| **Discovery** | 지정 인터뷰 세션 | 착수 전 질문·모호성 분류·gate 통과 판단 | 아이디어/요청 + 과거 lessons + config | requirements/design/validation/handoff 초안 | 구현 착수, 미분류 TBD 방치 |
| **Planner** | 지정 계획 세션 | 로드맵·결정·페이즈별 프롬프트 저작 | Discovery 산출물 + SoT 문서 + 코드 실측 | 로드맵(결정로그) + 구현 프롬프트 | 직접 구현/리뷰 떠안기 |
| **Implementer** | 지정 구현 세션 | 결정 범위 내 구현 | 프롬프트 + 어댑터 지침 + config | 코드 + 검증 증거 + HANDOFF/lessons 갱신 | 범위 밖 확장, silent fallback |
| **Reviewer** | 지정 리뷰 세션 | 결정 준수 감사 | diff + 로드맵 + 프롬프트 | finding(정형) | 근거 없는 blocker, 자기 구현 자기 승인 |
| **Mechanical** | Nitpicker/local checker | 스레드/인코딩/계약 nit | diff | PASS/REJECT | 과한 framework 제안 무비판 수용 |
| **Human** | 사용자 | 결정 승인·커밋 게이트·방향 | 요약/finding | 승인/커밋 | — |

핵심: **역할은 세션 단위로 분리**한다. 같은 모델을 써도 Discovery/Planner/Implementer/Reviewer 세션은 서로 다른 책임을 가진다.
Planner 세션은 구현·리뷰 루프에 들어가지 않고, Implementer 세션은 자기 구현을 자기 승인하지 않는다.

### 1.1 역할 판별 우선순위

프로젝트 루트의 `AGENTS.md`/`CLAUDE.md` 같은 surface adapter는 구현 전용 파일이 아니라 **역할 라우터 + 공통 방법론 요약**이어야 한다.
같은 프로젝트 폴더 안에서도 "문서 정리", "리뷰", "기획", "구현" 세션이 나뉠 수 있으므로, 폴더 하나에 Implementer 역할을 고정하지 않는다.

판별 순서:
1. 사용자가 명시한 세션 역할: "이번 세션은 Discovery/Planner/Implementer/Reviewer".
2. 요청 의도:
   - 인터뷰/기획/착수 전/요구사항/문제정의 → Discovery
   - 설계/로드맵/작업지시서/프롬프트/다음 페이즈 → Planner
   - 진행/구현/수정/테스트/커밋 → Implementer
   - 검토/리뷰/크리티컬/수석 관점/문제점 → Reviewer
3. 세션 제목은 보조 신호로만 사용한다. 사용자 지시와 충돌하면 사용자 지시가 우선한다.
4. 역할이 모호하거나 파일 수정 여부가 갈리면 착수 전 질문한다(C1).

### 1.2 역할 독립성 계약 + 스킬 우선 (혼용 방지)

**리뷰는 두 leg로 분리되고, 핵심은 검토 대상의 *저자로부터 독립*이다:**
- **설계 검토(plan/design review)** — 설계 세션(Planner) 산출물(로드맵·핸드오프·결정)을 리뷰. findings만 내고 **저작·코드편집 안 함**. 스킬 = `cross-session-plan-review`.
- **구현 리뷰(code review)** — 구현 diff를 리뷰. **구현 세션과 독립한 에이전트가 집행**(C4: 자기 구현 자기 승인 금지). 기계 nit = `nitpicker-review`(Mechanical).

**한 세션이 둘 이상 역할을 겸하지 않는다.** 특히 Planner(저작)와 설계 검토를 한 세션이 겸하면 자기 스펙 맹점이 생긴다 — 설계 검토 세션은 설계를 저작하지 않고, 저작 세션은 자기 산출을 자기 검토하지 않는다.

**집행(commit gate)**: Planner/orchestrator 세션은 **코드/스크립트(`install.sh` 등 tooling 포함)를 직접 구현하지 않는다.** 부득이 직접 구현했어도, 커밋 범위에 코드·스크립트 변경이 있으면 **author≠reviewer 독립 구현 리뷰(별도 컨텍스트/벤더 에이전트) PASS 증거 없이 커밋 금지.** `pytest`/`ruff`/`mypy`/`bash -n`/`--dry-run` 같은 self-verify는 필요조건일 뿐 **충분조건이 아니다.** "tooling/docs라 사소함"은 면제 사유가 아니다(순수 docs/메모리 변경만 예외). 이 게이트는 `zrt-phase-commit` Hard Gate로도 박혀 있다.

**독립 에이전트 에스컬레이션 (조건부)** — 설계 검토 세션은 설계 저자와 다른 세션이므로 **기본은 직접 검토**한다. 다음일 때만 추가 독립 에이전트를 호출한다: ① 대형·복잡 diff ② R5/보안 등 크리티컬 경계 ③ 심층 코드 검증 필요 ④ 1차 판정 애매. 리뷰 집행은 항상 **현재 동작 중인 모델/CLI와 다른 계열의 독립 CLI**로 수행한다(예: Codex 실행 중이면 Claude CLI, Claude 실행 중이면 Codex CLI). cross-vendor 독립이 같은 벤더 서브에이전트보다 맹점이 덜 겹친다. 원칙: **reviewer CLI 계열 ≠ active executor CLI 계열**, 가능하면 **reviewer 벤더 ≠ author 벤더**. 다른 CLI가 없으면 same-lineage 독립 컨텍스트로 폴백하되 열화(same-lineage)를 보고하고, 리뷰 생략으로 PASS 처리하지 않는다.

**스킬 우선(skills-first)** — 역할 판별(§1.1) 직후, 착수 전 **매칭되는 워크플로 스킬이 있으면 반드시 먼저 호출**한다(수동 진행 금지). 스킬이 세션에 없으면(미설치) 플러그인 설치를 안내하거나 해당 `SKILL.md`를 직접 읽어 규약을 따른다. (스킬을 안 쓰고 진행하는 것이 가장 흔한 규약 이탈이다.)

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
   └── gate 미통과 시 재질문                                └── 로드맵(결정로그) + 페이즈 프롬프트 ──► [Implementer]
                                                                    │  구현 + 검증 증거
                                                  HANDOFF/lessons ◄──┤  (Sync-Out)
                                                                    ▼
                                                   diff ──► [Reviewer] ──► finding(정형)
                                                   diff ──► [Mechanical] ──► PASS/REJECT
                                                                    │ ALL PASS
                                                              [Human] 승인 → 커밋
```
- **프롬프트**는 채팅 전용 가능하지만 **로드맵·HANDOFF·lessons는 파일**(채팅은 세션을 못 넘는다).
- 프롬프트 필수 요소·리뷰 양식은 `artifacts/prompt-skeleton.md`, `artifacts/review-finding.md`.
- 모델명은 예시일 뿐이다. 프로젝트 시작 시 `role assignment`를 명시해 이번 작업에서 어느 세션이 Discovery/Planner/Implementer/Reviewer인지 고정한다.

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
