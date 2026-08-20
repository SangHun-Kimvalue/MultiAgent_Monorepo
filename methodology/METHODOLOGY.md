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
- **역할 재판정 checkpoint(Decide 전 + 요청 의도 변경 시, MUST):** 현재 의도가 세션에 부여된 역할의 허용 범위 안인지 다시 확인한다.
  범위를 넘으면 작업과 tool call을 `STOP`하고 같은 세션에서 역할을 바꾸지 않는다. [`MULTI_AGENT.md`의 Role Transition Checkpoint](MULTI_AGENT.md#role-transition-checkpoint)에 따라 `ORCHESTRATOR_HANDOFF`로 제어권을 넘긴다.
- **Decide:** 이번 작업의 "할 것 / 안 할 것 / 채택 / 폐기 / 검증 기준"을 고정. 작은 건 HANDOFF, 큰 결정은 로드맵 결정로그 또는 ADR.
- **Implement:** 결정 범위 안에서만. 범위 밖 확장·미허용 인프라 도입 금지. 과설계 제안은 "검토했으나 보류"로 기록.
- **Verify:** 정적 diff가 아니라 **실행 증거**(명령 + 결과 + artifact 경로). 못 돌린 검증은 이유를 남긴다.
- **Review:** C4. 결정 준수·범위·fail-fast·테스트/artifact 충족·과설계 무비판 수용 여부 감사. finding은 정형(§아래).
- **Sync-Out(종료, MUST):** `HANDOFF` 갱신(현재 상태/가정/미완/다음 단계, **PASS는 어디까지·NOT CLAIMED 분리**) + `lessons` append(WHY/LESSON).
  phase 작업이면 각 checkpoint에서 phase ledger를 갱신·보고하되, 스키마를 다시 정의하지 않고 [`MULTI_AGENT.md#phase-ledger-canon`](MULTI_AGENT.md#phase-ledger-canon)을 따른다.

**리뷰 finding 정형** (근거 없는 blocker 금지): `severity / finding / evidence_or_repro / impact / recommendation`.
**리뷰 bundle 최소**: 변경 파일 + 핵심 diff + 관련 문서 발췌 + 검증 출력 + artifact 경로 + 남은 질문. 전체 리포 병합 금지.

### 2-1. 대상 선정 게이트 (Target Evidence) — 다음에 무엇을 할지 정할 때

**대상은 설계 전에 근거를 요구한다.** 설계·구현은 교차 검토를 받는데 "다음에 뭘 할지"는
검토 없이 사람에게 가므로, 여기가 가장 값싸게 틀릴 수 있는 지점이다.

**적용 범위 — 이 게이트는 의례가 아니다.** 사용자가 직접 지정하지 않은 **L1/L2 후속 후보를
추천할 때만** 아래 근거를 **작업 규모에 비례해** 기록한다.
**L0 · 명백한 재현 결함 · 사용자가 명시적으로 지시한 작업은 면제**한다 —
"작은 수정 → 검증" 흐름을 측정 절차로 막지 않는다.

| 항목 | 무엇을 적나 |
|---|---|
| **현상 인스턴스** | 이 대상이 고치려는 현상이 **지금 몇 건** 있는가 + **측정 범위·시점·데이터 출처** |
| 사용자 영향 | 화면·결과에 **어떻게 보이는가**(안 보이면 그렇게 적는다) |
| 직접 원인 증거 | 원인을 **어떻게 확인했나**(추정이면 추정이라 적는다) |
| 변경 비용 | 대략 어느 계층 몇 곳인가 |
| 안 했을 때 손실 | 방치하면 무엇이 계속 틀리는가 |

**인스턴스 0건은 자동 탈락이 아니라 `보류(NOT CLAIMED)`가 기본값**이다.
`0 observed`와 `mechanism absent`는 다르다 — 예방적 정합성·희귀 P1·신규 기능·아직 데이터가
쌓이지 않은 L2 작업을 관측 0건이라는 이유로 잘라내면 그것 자체가 왜곡이다. 다음 중
**하나라도 있으면 0건이어도 진행**한다(무엇인지 적는다):

- 합성·주입으로 **재현 가능한 위험 증거**(그 재현이 검증 게이트가 될 수 있다)
- 보안·데이터 손실·감사처럼 **발생하면 되돌릴 수 없는** 축
- **사람의 명시적 override**(근거와 함께 기록)

그 밖의 0건 후보는 대상 목록에 남기고 **지금 착수하지 않는다** — 검증할 데이터가 없으면
"쓰이는지 모르는 기능"이 된다.

- **측정하지 않은 채 "가치가 크다/작다"를 말하지 않는다.** 특히 "사용자에게 안 보인다"는
  판단은 성능·정합성 항목을 과소평가하는 흔한 경로다 — 안 보인다고 적으려면 그것도 재고 적는다.
- 이 게이트의 출력은 사람에게 **그대로** 보인다. 추천 근거가 곧 게이트 기록이다.

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

**Human Gate의 단위는 페이즈 경계다.** 페이즈 시작 승인은 승인된 scope와 timebox 안의
`review → four-way disposition → 별도 Implementer 수정 → 독립 재검증`을 포함한다. 정상적인
corrective round마다 새 승인을 받지 않는다.

**중간 에스컬레이션 집합(이 목록이 정본 — 하위 문서가 줄이거나 늘릴 수 없다)**:
① `BLOCKED` ② scope 확대 ③ disposition 모호성 ④ 운영상 위험
⑤ **리뷰 예산 소진** ⑥ corrective timebox(`fix_rounds_max`) 소진.

**⑤와 ⑥은 다른 축이다.** 리뷰 예산(semantic review terminal 총량)과 자동 수정 라운드는
서로를 대체하지 않는다 — `fix_rounds_max`가 남았어도 **리뷰 예산이 소진되면 멈춘다.**
그때는 PASS를 만들기 위한 추가 리뷰를 호출하지 않고 현재 verdict·미해결 finding·
NOT CLAIMED를 그대로 Human Gate에 반환한다(종료 경계).

페이즈 종료 시에는 결과와 NOT CLAIMED를 제시하고 **커밋 여부와 다음 페이즈 진행 여부를
각각 독립적으로** 승인받는다 — 둘을 하나의 yes/no로 묶으면 사람의 개입 선택지가 줄어든다.

**리뷰 예산은 슬라이스 전체가 공유한다.** 독립 설계검토·구현리뷰·LLM Mechanical·adjudicator를
합친 semantic review terminal 기준으로 센다. deterministic validator와 로컬 test 실행은 세지 않는다.
checkpoint 전환은 예산을 리셋하지 않는다. 자동 수정 라운드(`fix_rounds_max`)와는 다른 축이다.

**예산은 심사 대상에 따라 두 축으로 나뉜다** (2026-08-14 개정):

| 축 | 대상 | 예산 |
|---|---|---|
| **구현 diff 심사** (출력 게이트) | 실제 코드 변경 | **L0 2 / L1 3 / L2 4** |
| **문서 심사** (입력 게이트) | 계획서·구현 프롬프트·설계 문서 | **L0 1 / L1 2 / L2 3** |

축을 나눈 이유는 계획 심사가 값싸고 설계 결함을 **구현 전에** 차단하기 때문이다. 예산의
보호 대상 본체는 구현 diff 심사다.

**수치의 출처(초기값이다)**: 문서 축 상한은 위 7라운드 사례에서 "3회 이후 실질 결함 0건"이었다는
**단일 관측**에서 잡았다. L0·L1 값은 구현 diff 축의 비율을 따른 것이며 독립 근거가 없다.
관측이 쌓이면 조정하되, **조정은 슬라이스 진행 중이 아니라 별도 결정으로 한다** — 진행 중
상향은 증축이다. 상한이 낮아 3회차에도 실질 결함이 남으면, 라운드를 늘리지 말고 미해결
finding 을 disposition 과 함께 Human Gate 로 올린다(종료 경계).

**그러나 상한 없는 축을 만들지 않는다.** 축 분리 자체가 예산 우회 경로가 되기 때문이다 —
2026-08-13 외부 프로젝트 NFC 핫픽스에서 **입력 게이트 단독 7라운드**가 났고 후반 4라운드는 코드가
아니라 문서 문구 정합성이었으며 실질 결함 0건이었다. 두 축은 서로의 잔여 예산을 빌려오지
않으며, 한 축을 소진했다고 다른 축에서 같은 대상을 다시 심사하지 않는다.

**집행**: `review_budget_preflight.py` 가 **두 축 모두** Reviewer 호출 전에 예약·차단한다
(`--gate output` = 구현 diff, 기본 / `--gate input` = 문서 심사). 카운터는 phase 진행 문서의
`review-budget` 블록에 축별로 분리돼 있고(`rounds_consumed` / `doc_rounds_consumed`), 해당 축의
카운터 줄이 없으면 fail-closed 다 — 줄을 지워 예산을 리셋할 수 없다.

⚠ **집행되지 않는 부분**: **"한 축을 소진하고 같은 대상을 다른 축으로 재분류해 다시 심사"하는
우회는 도구가 막지 못한다.** 심사 대상의 동일성은 의미 판단이라 숫자·enum 비교로 환원되지
않는다(R5). 이 금지는 산문으로만 존재하며 집행은 Planner 와 Human Gate 에 있다.

- **증축 금지**: 선언된 리뷰 예산은 늘리지 않는다. 예외는 보안·인증·데이터 손실 위험 또는
  실제 false PASS가 재현된 경우뿐이며, 이때도 기존 슬라이스를 연장하지 않고 해당 위험만 다루는
  별도 emergency corrective slice로 분리한다.
- **종료 경계**: 같은 슬라이스가 예산을 소진했는데 PASS가 아니면 PASS를 만들기 위한 추가 리뷰를
  호출하지 않는다. 현재 verdict, 미해결 finding, NOT CLAIMED를 그대로 Human Gate에 반환한다.
- **기존 finding의 반복에는 예산을 리셋하지 않는다.** 라운드마다 무언가는 나오게 돼 있고,
  같은 지적이 형태만 바꿔 돌아오는 것으로 예산을 늘리지 않는다.
- 예산을 소진하면 `APPROVED`를 계속 좇지 말고 남은 finding을 **four-way disposition**으로
  처분한다(`ACCEPT` / `REJECT_FALSE_POSITIVE` / `DEFER_OUT_OF_SCOPE` /
  `REJECT_OVERENGINEERING`, 근거 필수) → 그 처분을 들고 **사람에게 올린다**.
- **disposition은 승인을 대체하지 않는다**([`MULTI_AGENT.md`](MULTI_AGENT.md) 상속 —
  수정 대상 선별일 뿐 phase 승인도 독립 재검증도 아니다). 정상 경로에서 사람이 고르는 것은
  `STOP` / `scope 재설계` / `현재 결과 승인 여부`다.
  **미해결 P1을 안고 넘어가는 "위험 수용"은 정상 경로가 아니다** — 보안 사고 대응 같은
  명시적 비상 절차에서만, 그 사실과 근거를 기록으로 남기고 쓴다. 제도화하면 게이트가
  형식이 된다.
- **finding이 그 슬라이스의 목적에 필요한가**로 관리한다. 슬라이스 주제를 벗어난 일반
  강건성 지적은 원 acceptance contract를 확대하지 않으며 `REJECT_OVERENGINEERING` 또는
  `DEFER_OUT_OF_SCOPE`로 처분한다.
- **Human Gate 1 이후 acceptance contract는 동결**한다. Reviewer가 새 요구·새 invariant·새
  failure-injection matrix를 blocker로 추가하지 않는다. 보안·인증·데이터 손실·재현된 false PASS로
  계약 변경이 필요하면 현재 slice를 멈추고 사람에게 scope 재승인을 받는다.
- **실제 재현 없는 P2/P3는 blocker가 아니다.** risk/backlog로 기록하고 phase verdict를 막지 않는다.
  정적 가능성만으로 수정 트리거를 만들지 않는다.
- **공정 무게는 등급에 맞춘다.** 이름 하나 바꾸는 변경에 설계·구현 리뷰를 각각 5라운드
  도는 것은 등급표를 무시한 것이다.

### 3.1 Context / Token Budget

리뷰 **횟수 예산**과 한 번에 전달하는 **컨텍스트 예산**은 별도 축이다. 리뷰 라운드를
재승인해도 전체 대화·전체 SoT·누적 리뷰 역사를 매번 다시 보내는 권한이 생기지 않는다.

- 첫 리뷰는 canonical anchor, frozen acceptance contract, exact changed paths, 검증 요약만 담은
  bounded context bundle을 받는다. 원문은 복제하지 않고 repo path와 SHA-256으로 참조한다.
- R2 이후 재리뷰 입력은 `미해결 finding + disposition + 이번 변경 hunk + 새 검증 결과`만 담는다.
  해결된 finding, 이전 Reviewer 산문, raw test log는 artifact로 보존하되 재전송하지 않는다.
- 독립 컨텍스트는 전체 대화 상속을 뜻하지 않는다. subagent/별도 세션은 fresh context를 기본으로
  하며, Codex Agent 도구에서는 `fork_turns="none"` 또는 필요한 최소 recent turns를 사용한다.
- dispatch 전 control bundle은 **12,000 UTF-8 문자**, 한 leg의 diff/hunk payload는 **40,000 문자**를
  기본 상한으로 삼는다. 넘으면 모듈·파일별로 분할하거나 risk-relevant hunk만 선택한다. 수치는
  요구사항 PASS 기준이 아니라 **호출 전 라우팅 기준**이며, 초과한 채 비싼 모델로 밀어 넣지 않는다.
- tool 결과는 `명령 / exit / pass-fail count / artifact path / digest`만 전달하고 raw stdout·stderr는
  파일로 보존한다. 동일 full test log와 full diff를 Reviewer·Mechanical·Adjudicator에 중복 주입하지 않는다.
- Mechanical raw verdict와 독립 Reviewer가 충돌할 때도 구체적인 재현 finding이 없으면 새 전체 리뷰를
  열지 않는다. raw 결과를 보존하고 기존 증거로 disposition하거나, 판정이 실제로 뒤집힐 때만 adjudicator를 호출한다.
- Mechanical finding은 최소 `severity / evidence_or_repro / impact / recommendation` 네 필드가 모두
  있어야 수정 트리거다. 하나라도 없으면 raw verdict는 보존하되 adapter degradation으로만 기록한다.
  verdict token과 prose가 충돌해도 prose를 의미 판정해 재라벨하지 않고 adapter degradation으로 분류한다.
- phase 진행 문서는 `현재 status / next owner / NOT CLAIMED / evidence pointer`만 유지한다. 라운드별
  finding·disposition·raw review 역사는 phase-local artifact에 두고 SoT 본문에 누적하지 않는다.
- 상한 안으로 줄일 수 없으면 호출을 강행하지 않고 `현재 크기 / 분할 불가 이유 / 필요한 결정`을
  보고한다. 토큰 한도 소진은 품질 저하 모델·누락 리뷰로 조용히 fallback할 근거가 아니다.

## 4. 확장(YAGNI) 원리
교체 가능성이 있는 경계는 **추상 인터페이스(ABC 등) + 구현 1개 + Mock**으로 출발. 두 번째 구현은 **실수요 발생 시점**에만 추가.
"여러 벤더가 다른 방식으로 같은 동작"일 때만 어댑터(런타임 스왑). 그 외엔 공개 API 계약만 고정(내부 재작성 허용) 또는
설정 스위치로 충분 — 이중 추상 금지.

## 5. 프로젝트 config로 미루는 것 (PARAM)
다음은 캐논이 아니라 `config/project.config.example.md`에서 정의: 빌드/검증 명령, 버전·RC 규칙, 코드 컨벤션(OOP/타입/예외계층/로깅),
테스트 전략·비중, 프롬프트 디렉토리 규칙, Nitpicker(또는 동급) 경로·래퍼·모델, 프로젝트 수집/구현 전략(예: collect-max-then-prune), 무수정 대상.

---
관련: 역할·핸드오프 `MULTI_AGENT.md` · 문서 체계 `DOC_TAXONOMY.md` · 양식 `artifacts/` · 어댑터 `adapters/` · 프로젝트값 `config/`.
