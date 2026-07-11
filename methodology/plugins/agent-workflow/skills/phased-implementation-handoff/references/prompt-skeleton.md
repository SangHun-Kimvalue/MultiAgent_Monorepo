# 구현 프롬프트 스켈레톤 (Implementer 세션용)

아래 섹션 구조를 그대로 채운다. Implementer 세션이 리포 접근 가능하면 경로·시그니처·불변식 중심으로 간결하게,
파일 접근이 없으면 코드 골격·컨벤션·부착점 원문을 임베드(훨씬 길어짐).

```
작업: <기능명> — Phase <X> (<한 줄 목표>). 한국어로 응답/주석.

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
1) 테스트: <독립 test 확장 + 검증 포인트>
2) 빌드: <프로젝트 빌드 명령> (+ 가능하면 독립 test 컴파일/실행) — 검증은 실행 증거(명령+결과+artifact 경로)로.
3) (가능 시) 실장비/실환경 1회 확인 — 정밀 검증은 후속 페이즈
4) 리뷰 ALL PASS(둘 다, 하드 게이트):
   - B. **Nitpicker(local, repo 래퍼 우선)** — 수정 파일마다 **Implementer가 실행**, REJECT→수정→재실행해 PASS.
   - A. **별도 Reviewer(설계)** — **Planner가 집행**, **다른 계열(cross-lineage) 독립 에이전트 CLI 우선**(예: Implementer=Claude → Reviewer=Codex/Gemini CLI). 폴백 위계: ① 다른 계열 CLI → ② 다른 계열 세션 → ③ 다른 계열 부재 시에만 같은 계열 독립 컨텍스트 서브에이전트(review 기록에 `same-lineage` 명시). Implementer는 **최소 review bundle**(변경파일+핵심diff+검증출력 요약+남은질문)을 완료 보고에 첨부만 하고, **`NOT CLAIMED`로 닫지 말 것**(리뷰는 항상 실행 가능).
   - 두 레그 PASS 전 페이즈 미완료. 커밋도 보류.
5) <버전/RC 갱신 규칙>. 커밋은 사용자 확인 후. 페이즈 경계는 HANDOFF/로드맵/커밋 메시지에 기록.

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
