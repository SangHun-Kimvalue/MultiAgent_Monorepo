# Validation Plan — ZRT v2

> PASS 레벨: **unit + deterministic + live 1회** (인터뷰 확정 2026-06-13)

## PASS에 포함 (이것이 통과해야 페이즈 완료)

| 레벨 | 내용 | 도구/방법 |
|---|---|---|
| **unit** | KEEP 자산 이식 테스트 + 신규 명령 단위 테스트 | pytest. v1의 test_harness*.py 중 H4/H6 계열 이식, DROP 대상 테스트는 명시적 삭제 커밋 |
| **deterministic** | mock 백엔드로 전체 흐름 결정론 검증 — envelope 스키마, verdict enum 라우팅, 타임박스 STOP, NOT CLAIMED 표기, 인바리언트 검사 로직 | 가짜 CLI 스텁(고정 출력) + 파일시스템 픽스처. 모델 호출 0회, CI 가능 |
| **live 1회** | 페이즈별 실모델 검증 1회 + **재현 커맨드 기록(C6)** | Phase 2: ollama(`qwen2.5-coder:7b`) 실호출 nitpicker 1건 / Phase 5: `claude -p --model sonnet` 실리뷰 1건 (spike S1 후) |

## NOT CLAIMED (PASS에 포함하지 않음 — 미검증을 통과로 위장하지 않는다)
- full E2E: `ztr run-phase` 전체 루프의 실모델 자동 관통 → **Phase 6 dogfood에서 별도 검증**, v2 구현 PASS와 분리.
- Sonnet 리뷰의 *탐지 품질* (정확도) → 정량 평가 없음. dogfood 중 LESSON으로 축적.
- ollama 소형 모델의 린트 품질 → ruff+mypy가 1차 방어라는 전제로 보조 역할만 claim.

## 재현성 (C6)
- 모델 식별: ollama 모델 태그(`qwen2.5-coder:7b`), claude `--model sonnet`(별칭 — 버전 핀 안 함, MS 모델 정책 차용), 실행 커맨드 전문을 live 검증 기록에 포함.
- LLM 비결정성 대응: live 검증의 단언 대상은 **envelope 구조 + verdict enum 존재**이지 응답 내용이 아님.
- 환경 기준선 (2026-06-13 실측): Windows 11, Python 3.12.10, ollama 0.18.0, ruff 0.3.4. mypy·venv는 Phase 1에서 구성.
