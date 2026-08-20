# Finding Disposition

Reviewer finding을 수정 대상으로 채택할지 근거와 함께 기록하는 최소 양식이다. finding마다 아래 블록을 복사해 사용한다.

## Finding `<finding-id>`

- Finding ID: `<고유 ID>`
- Reviewer: `<독립 Reviewer/session 또는 review artifact>`

### 원 Finding (Reviewer 기록)

- severity: `<blocker | high | medium | low 등 원문 값>`
- finding: `<발견 내용>`
- evidence_or_repro: `<근거 또는 재현 절차>`
- impact: `<영향>`
- recommendation: `<권고>`

### Disposition

- disposition: `<ACCEPT | REJECT_FALSE_POSITIVE | DEFER_OUT_OF_SCOPE | REJECT_OVERENGINEERING>`
- evidence: `<결정을 지지하는 실측 근거>`
- rationale: `<결정 이유>`
- owner: `<책임 역할/세션>`
- follow-up location: `<DEFER_OUT_OF_SCOPE이면 필수인 roadmap/issue/HANDOFF 위치, 아니면 N/A>`
- corrective round: `<라운드 ID 또는 N/A>`
- re-review status: `<독립 Reviewer/Mechanical 재검증 상태와 evidence 또는 N/A>`

## 적용 규칙

- `ACCEPT`만 사람의 명시적 trigger 뒤 기존 `ztr fix-prompt`의 입력이 될 수 있다. 수정은 separate Implementer가 수행하고 동일한 결정론 검증과 독립 Reviewer/Mechanical gate를 다시 통과해야 한다.
- `REJECT_FALSE_POSITIVE`와 `REJECT_OVERENGINEERING`에는 원 finding을 반박하는 evidence가 반드시 있어야 한다.
- `DEFER_OUT_OF_SCOPE`는 결함 부정이 아니다. owner와 구체적인 follow-up location을 반드시 기록한다.
- disposition은 수정 대상 선별이며 phase 승인이나 Orchestrator의 자기 구현 승인이 아니다. Human trigger와 독립 재리뷰를 대체하지 않는다.
