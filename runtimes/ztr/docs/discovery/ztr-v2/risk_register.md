# Risk Register — ZRT v2

| ID | 등급 | 리스크 | 영향 | 완화 |
|---|---|---|---|---|
| R1 | P1 | 헤드리스 CLI(spike S1) 미가용 — 설치 불가 또는 플래그 부재 | Phase 5 릴레이 무산 | spike를 Phase 5 진입 게이트로 고정. **폴백: 수동 트리거 + 기계 게이트(Phase 1~4)만으로도 가치 1 성립** — 릴레이 없는 v2도 출시 가능 |
| R2 | P2 | v1 점진 리팩토링 중 테스트 대량 파손 | 일정 지연, 회귀 누락 | KEEP/DROP 분류 선행(ZRT_V2_DIAGNOSIS §3). DROP 테스트는 별도 커밋으로 명시 삭제 — 침묵 삭제 금지(C3) |
| R3 | P2 | Sonnet 구현 리뷰의 탐지력 부족 | 결함 통과 | L2 페이즈 Opus 강제(D-1) + 닛피커 선행으로 기계 결함 분리 + 휴먼 커밋 게이트 유지 |
| R4 | P3 | ollama 소형 모델 린트 품질 저하 | 노이즈 finding | ruff+mypy 1차 방어, ollama는 보조로만 claim(NOT CLAIMED 명시). 바인딩으로 모델 교체 용이 |
| R5 | P3 | 릴레이의 verdict enum 파싱이 의미 파싱으로 슬금슬금 확대 (v1 회귀) | 하네스 군비경쟁 재발 | design.md Boundaries 표를 리뷰 기준으로 — "enum+exit code 외 파싱 추가" = 설계 리뷰 자동 BLOCKED 사유 |
| R6 | P3 | dogfood 순환 의존 — v2를 만들면서 v2(미완)로 검증 | 검증 공백 | Phase 1~2는 v1 nitpicker+ruff 수동 사용, Phase 3부터 자기 검증 전환 |
