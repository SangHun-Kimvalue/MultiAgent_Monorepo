# ZRT v2 Phase 8 — run-phase resume 체인

상위 handoff: `D:\MultiAgent_Monorepo\docs\handoff\PHASE8_RESUME_HANDOFF.md`

## 범위

- `run-phase`에 `--session-map`, `--implementer-resume`, `--reviewer-resume`를 추가한다.
- provider별 session id는 구조화 stdout JSON에서만 캡처한다.
- resume argv 변형은 명시 profile 설정으로 결정한다.
- `auto` resume 실패는 새 세션으로 폴백하고 맥락 손실 경고를 남긴다.
- 명시 session id 실패는 폴백하지 않고 기존 leg verdict를 유지한다.

## NOT CLAIMED

- full 다중페이즈 오케스트레이션 자동화.
- 외부 CLI session id 필드명의 장기 안정성.
- live quota/만료 실패 경로의 실제 서버 재현.
