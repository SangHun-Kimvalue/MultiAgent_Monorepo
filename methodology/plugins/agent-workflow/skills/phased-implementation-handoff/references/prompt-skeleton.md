# 구현 프롬프트 스켈레톤 (Implementer 세션용)

아래 섹션 구조를 그대로 채운다. Implementer 세션이 리포 접근 가능하면 경로·시그니처·불변식 중심으로 간결하게,
파일 접근이 없으면 코드 골격·컨벤션·부착점 원문을 임베드(훨씬 길어짐).

```
작업: <기능명> — Phase <X> (<한 줄 목표>). 한국어로 응답/주석.

[등급·예산 선언] 작업 등급: <L0|L1|L2>  리뷰 예산: <2|3|4>  현재 라운드: <N>
  ※ Planner 가 착수 전에 선언한다. **선언 없이 게이트를 열지 않는다.**
  ※ 등급은 최종 변경 표면으로 판정한다(단일 파일·기계적 = L1 이하). 예산은 등급에서 파생하며
     입력·출력 게이트가 **공유**한다(L0 2 / L1 3 / L2 4). **증축 금지.**
  ※ 라운드가 예산을 넘으면 추가 리뷰 대신 **four-way disposition 으로 Human Gate 반환**.

[형상관리] 시작 전 git status. 현재 체크아웃된 브랜치를 유지하고 그대로 작업/커밋.
새 브랜치 생성, 브랜치 전환, 태그 생성은 사용자가 명시적으로 요청한 경우에만 수행.

[전제] <선행 페이즈/머지 조건. 예: B+B.5가 머지·리뷰 완료된 상태에서 시작. 아직이면 그것부터.>
  + 스테일 가드: 이 프롬프트는 <YYYY-MM-DD, HEAD abc1234> 기준. **착수 전 git log·테스트로
  이 작업이 이미 완료됐는지 / 전제·부착점이 변했는지 실측**. 이미 완료면 구현하지 말고
  "이미 완료(커밋 X)"로 보고, 전제가 변했으면 멈추고 보고. (사전작성 프롬프트는 저작→실행
  사이에 스테일해진다 — 다른 세션이 먼저 닫았는데 fresh 구동하면 중복/회귀.)

[SoT — 반드시 읽을 것] <로드맵 doc 경로 + 관련 설계 문서 §절>

[전략 리마인더] <프로젝트 전략을 못박음. 예: collect-max-then-prune + 안전 가드 / 또는 minimal-first.
  "수집/구현 가능 ≠ 지금 다 구현">

[이번 범위] <할 것 + ★명시적 out-of-scope★(다음 페이즈로 미루는 것 명시)>

[부착점/대상] <file:line 실측 표. "라인 이동 가능 → 시그니처로 재확인 후 부착">

[아키텍처 결정] <틀리기 쉬운 핵심을 못박음. 예: SessionManager는 X를 직접 호출 말고 주입된 provider로 /
  콜백 스레드 비차단 / 공개 시그니처 유지>

[불변 원칙] <무수정 대상(본문 로직/특정 매크로), 스레드 안전(FLTK·블로킹 금지), schema 유지, silent fallback 금지 등>
  + Timebox: 같은 축 수정이 N회(예: 3회) 안 풀리면 멈추고 실패 로그+원인+다음 접근을 보고. 무한 수정 루프 금지.

[검증/DoD]
0) 구현 diff Reviewer 호출은 **예산 preflight 를 통과**한다(직접 호출 금지):
   `python <skill-root>/scripts/review_budget_preflight.py --doc <phase 진행 문서> --slice <slice_id> --gate output|input -- <reviewer 명령>`
1) 테스트: <독립 test 확장 + 검증 포인트>
2) 빌드: <프로젝트 빌드 명령> (+ 가능하면 독립 test 컴파일/실행) — 검증은 실행 증거(명령+결과+artifact 경로)로.
3) (가능 시) 실장비/실환경 1회 확인 — 정밀 검증은 후속 페이즈
4) 리뷰 ALL PASS(둘 다, 하드 게이트):
   ★ **모드 분기(필수 — LESSON-039)**: **릴레이/오케스트레이터(`ztr run-phase`)로 구동될 프롬프트면** 출력 게이트는 **릴레이 leg가 소유** → implementer는 **구현만** 하고 자기 게이트 실행·SoT 문서 편집 **금지**(implementer blind, D6). 아래 2갈래는 **수동 핸드오프 모드**(implementer 세션이 자기 diff 게이트 자체집행, §8)에만 적는다. 모드 안 맞으면 implementer가 릴레이 leg와 중복 실행하다 타임아웃·가짜 SoT-PASS 편집을 한다.
   — 아래는 **수동 모드** [검증·DoD] (**이 출력 게이트는 Implementer 세션이 소유·집행**, §8):
   - B. **Nitpicker(local, repo 래퍼 우선)** — 수정 파일마다 **Implementer가 실행**, REJECT→수정→재실행해 PASS.
   - A. **별도 Reviewer(설계)** — **Implementer 세션이 자기 diff에 대해 다른 계열(cross-lineage) 독립 에이전트 CLI로 집행**(예: Implementer=Claude → Reviewer=Codex/Gemini CLI). 폴백 위계: ① 다른 계열 CLI → ② 다른 계열 세션 → ③ 다른 계열 부재 시에만 같은 계열 독립 컨텍스트 서브에이전트(review 기록에 `same-lineage` 명시). **`NOT CLAIMED`로 닫지 말 것**(리뷰는 항상 실행 가능).
   - 두 레그 PASS 전 페이즈 미완료. 커밋도 보류.
   - **가짜 PASS 차단 — 완료 보고에 리뷰 증적 첨부:** active executor CLI/계열, reviewer tool/CLI version/계열/모델·집행 방식, command shape, review 시각(UTC), review base SHA와 reviewed paths, 폴백 단계, review artifact 경로, **verdict(enum)·exit code**, raw output 경로, **모든 P1/P2 finding의 disposition**(반영 커밋 or Planner에 올릴 거부 사유). 스킬 `assets/review-verdict.schema.json` 필수 필드를 채우고 `scripts/validate_review_verdict.py` exit 0을 받아야 한다. unresolved P1/P2가 남으면 페이즈 완료·커밋 금지.
5) <버전/RC 갱신 규칙>. 커밋은 사용자 확인 후. 페이즈 경계는 HANDOFF/로드맵/커밋 메시지에 기록.
6) **잔여 목록 stale 차단 (Sync-Out 게이트 — delta 2026-07-28, 실측)**: 이번 페이즈가 닫은 항목이 **다른 문서에는 여전히
   "잔여/미완/OPEN"으로 남아** 다음 Planner가 **끝난 일을 다시 집는** 사고가 반복 관측됐다(한 세션에서 3회).
   - **소유권(§8.5 역할 3분할을 따른다 — 계약 정본은 `SKILL.md` §8.5)**: **입력 정의는 항상 Planner**(active-state anchor·
     canonical 문자열·검색어·root/exclude·집계 대상), **최종 판정도 항상 Planner**. 이 항목 6은 **집행(검색·문서 편집·증적 생성)**
     지시이며 **수동 모드에서만** Implementer 프롬프트에 넣는다. **릴레이/오케스트레이터 모드에서는 이 항목을 넣지 않고**
     implementer 프롬프트에 **"잔여 목록 Sync-Out은 Planner 소유 · Implementer 편집 금지"** 경계 한 줄만 남긴다
     (그 모드의 집행자는 Planner). 어느 모드든 **커밋 승인 요청 전** §8.5에서 최종 판정한다.
   - **적용 집합 = active-state에 한정**: 정규화 대상은 **현재 상태를 권위 있게 주장하는 필드·표·요약**
     (예: ADR closure matrix 행, `## Current` 최상단 상태 블록, PHASES의 현재 상태 요약)뿐이다.
     **historical/log/archive/과거 시점 항목은 비교 대상에서 제외** — **원문 보존 + 분류 근거 기록이 기본이고 수정하지 않는다**.
     **날짜 명시 인접 정정을 추가로 붙이는 객관적 조건**(§8.5 정본과 동일 — **아래 중 하나라도 해당할 때만**, OR):
     (a) 현재형으로 상태를 단정 · (b) 날짜·시점 라벨이 없어 현재 판정으로 읽힘 · (c) active-state anchor와 같은 문서의 같은 섹션. 해당 없으면 **건드리지 않는다**(churn 방지).
     이 구분이 "모든 위치 동일"과 "과거 보존"의 충돌을 없앤다.
   - **프롬프트 저자(Planner)가 미리 고정할 것** — 아래가 없으면 "전수"는 재현 불가능한 주장이 된다:
     **문서별 active-state anchor와 각 anchor의 기대 최종 상태**(어디가 현재 상태를 주장하는 자리인지) ·
     `canonical residual string`(잔여 목록의 정본 문자열) · 닫힌 **item ID와 별칭/표기 변형 검색어 목록** ·
     **검색 root와 exclude**(생성물·vendor·archive 제외) · **집계 재계산 대상**(`DONE n / PARTIAL n / OPEN n`).
   - **완료 보고 필수 증적**: 실행한 **검색 명령**, 검색 범위(root·exclude), **hit 목록 또는 artifact 경로**,
     **hit별 분류(`active-state`/`historical`/`무관`) + 분류 근거 + 대상 anchor**.
   - **성공 조건(fail-open 차단 — §8.5와 동일)**: 열거된 active-state anchor가 **전부** canonical 상태와 일치 ·
     historical hit는 **근거 기록 + 위 (a)~(c) 중 하나라도 해당하면 인접 정정까지 완료** · **미분류 hit 0** · 집계 수치 일치. **미열거 active-state 후보 발견·설명 불가 hit·집계 불일치는 `BLOCKED`**로 반환한다.
   - 완료를 과장하지 않는다: 부분만 닫혔으면 **PARTIAL**로 쓰고 무엇이 남았는지 `canonical residual string` 그대로 적는다.

[완료 보고] 변경 파일 / 핵심 결정·부착 위치 / 검증 결과(통과 명령) / **PASS는 어디까지·NOT CLAIMED·가정** / 버전 번호.
  ※ `NOT CLAIMED`는 *지금 실행 불가한* **환경/장비 게이트 검증**(실장비 육안 등)에만. **리뷰 2갈래엔 쓰지 말 것**(서브에이전트로 실행 가능).
```

## 3가지 핸드오프 모드 (정형 request 템플릿)
위 스켈레톤은 **implement** 모드다. L2(아키텍처/페이즈 경계) 결정엔 먼저 **debate**, 끝나면 **review**를 쓴다.

- **request-debate** (구현 전 구조 검토): `Issue / Decision candidates / Constraints / Relevant files` 제시 →
  답변은 `stance / evidence / risk / recommendation / confidence`. **파일 수정 금지, 범위 밖 인프라 제안 금지.**
- **request-implement** (위 스켈레톤): `Accepted decision / Allowed scope / Out of scope / Relevant files / Verification` →
  보고는 `changed files / tests run / runtime artifacts / assumptions / remaining risks`.
- **request-review** (교차 리뷰): `Accepted decision / Changed files / Verification artifacts / Phase boundary` →
  finding은 `severity / finding / evidence_or_repro / impact / recommendation`. 근거 없는 blocker 금지.

## 작성 팁
- **[아키텍처 결정]이 가장 중요.** 구현자가 가장 자주 틀리는 한두 지점을 미리 못박는다(예: 비차단 enqueue, 주입 패턴).
- **out-of-scope를 반드시 적는다.** "이건 다음 페이즈"라고 안 적으면 범위가 번진다.
- **부착점은 표로, file:line 포함.** 단 "이동 가능 → 시그니처 재확인"을 함께.
- 리포 접근 가능한 Implementer 세션에는 원문 코드 복붙 대신 "어디를 보라"로 충분. 분량을 아낀다.
- 전략/불변식은 프로젝트 설정(§7)에서 읽어와 채운다 — 스킬에 하드코딩 금지.
