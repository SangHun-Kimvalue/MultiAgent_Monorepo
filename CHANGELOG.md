# CHANGELOG — MultiAgent 스위트 버전·페이즈 히스토리

> 성격: **버전 태그 ↔ 완료 페이즈 ↔ 대표 커밋** 매핑(릴리스 지향 히스토리).
> 트랙별 딥 SoT는 `methodology/docs/PHASE_CYCLE_ORCHESTRATOR_DESIGN.md §10`(진행 상태), 의도·방향 서사는 `methodology/docs/SUITE_NARRATIVE.md`. 이 문서는 그 둘을 **중복하지 않고** 버전 경계만 짚는다.
> 정직성 규약: `完` = 게이트 PASS·머지 실증. `진행`/`NOT CLAIMED` = 미실증 명시. 상태 충돌 시 §10 요약판이 SoT.
> 별 repo(Nitpicker Daemon·CubiForge) 커밋은 `(별 repo)`로 표기 — 이 모노레포 이력이 아니다.

---

## v0.3.0 — 하드닝 + 외부 트라이얼 + 방법론 자기적용 (2026-07-16)

v0.2.0(빌드+self-dogfood 15단계 完) 이후 **90 커밋**. 스위트를 "돌아가는 골격"에서 "검증·하드닝된 도구 + 실프로젝트 적용 검증"으로 끌어올린 레이어. 세부는 §10 트랙표(T1~T9).

### 하드닝 (step 11 적발 기술부채 해소)
- **T4 mechanical cross-runtime lint 完**: `methodology/lint/cross_runtime_lint.py` 결정론 게이트(runtime별 venv 매핑, exit 0/1/2). 도구 `f7dadf0` → mypy `--strict` 승격 `50cc7bf` → relay 바인딩.
- **T5 ACP `[dev]` 정적게이트 完**: ruff/mypy 도입 + 설정 + 클린업 `61f2f6e`.
- **T8 멀티세션 worktree 조율 강제 메커니즘 完**: `methodology/t8/t8_guard.py`(preflight/commit 파일-한정 강제/isolate) + 재발 시뮬 테스트 25 + Codex 적대 리뷰 7R `d3d04d7`.

### T2 통합 구동 GUI (자연어 "N페이즈" 입력+관제 한 화면) — Phase1~6 完
- Phase1 driver PoC(ACP `POST /api/orch/run`+approve, `MockGateDriver`) `524a115`
- Phase2 UI PoC(dashboard 입력/승인 버튼) `567cb76`
- Phase3 Claude CLI probe driver(R5 status·AD-7 gate) · Phase4 OrchRunManager fail-closed `f8cebbc`
- Phase5 매니저 계층 BLOCKED 관측성(합성 verdict emit) `c0d69f6`
- Phase6 **ZtrRelayDriver**(실 run-phase 릴레이, run_in_background 관측성) + 라이브 스모크 `df21333`
- **NOT CLAIMED**: provider 선택 UI, ztr real relay 품질·결정성, N>1, 비용/속도, long-running 안정성.

### 방법론 캐논 자기적용 (스위트를 스위트로 개발)
- **phased-handoff §5.5 입력 게이트 추가**(핸드오프 전 프롬프트 독립 검토, Codex ACCEPT) `67412b8`
- **phased-handoff 10단계 정합**(출력리뷰 소유권 = Implementer 세션, §8.5 Planner 통합확인 신설, Codex 4R ACCEPT) `ca67312`
- 실행마찰 룰북화: `LESSON-025~028`(codex Windows 셸·비-git cwd·pytest cwd·run-phase cwd) + `EXECUTION_ADAPTER_CONTRACT §3.1`.

### 외부 실프로젝트 트라이얼 (다형성·실적용 실증)
- **T3 닛피커 provider 플러그인화 完(핵심)**: Gemini API 만료 → Ollama 기본 플러그인화, 기존 로직 유지, 560 tests `4091760 (별 repo)`. Mechanical 레그 기준 = `config/system_prompt.md` 4계명(CORE RULES).
- **T9 CubiForge 어댑션 진행**: β mechanical 계약정합 完(nit_envelope↔ztr Envelope 3분기 라이브) · γ 첫 실페이즈 부분 完 · 풀 4-leg 무인 E2E capstone `(별 repo)`. 2번째 실프로젝트 적용 실증(단 같은 ztr 백엔드 — §9 비-ztr 다형성은 여전히 NOT CLAIMED).
- **T1 Cubicon(C++ FLTK) trial 진행**: 외부 오케스트레이터(ZTR 숨김·무오염·push 금지). 환경·바인딩 準備完, Slice A(UI 파서 제네릭화) 프롬프트 `ad3bc83`.

### 버전관리 종합 (이 릴리스)
- CHANGELOG 신설 · SUITE_NARRATIVE/README 현행화 · `.gitignore` 정리 · stale 브랜치 `codex/phase8-resume-chain` 정리.

### T10 재적용 수렴 루프 — 개설·Discovery_PASS (진행)
- **동기**(xCeler-Plus 실측): `run-phase` 한 사이클 후 리뷰 finding을 fix→재검증하는 루프 부재(`fix_feedback`는 수동 `ztr fix-prompt`만 배선).
- **방향**: 완전 자동 재적용(v1 함정)은 영구 금지, **매 라운드 사람 승인**하는 휴먼-게이트 수렴 루프(PASS/N=3). 계층 분리(결정론 1라운드=ztr / 루프·게이트=SKILL·GUI).
- Discovery_PASS(7 산출물+risk_register, `docs/discovery/reapply-convergence-loop/`). 슬라이스 A(ztr fix-round)→B(SKILL 규약)→C(GUI 버튼). 다음=Slice A phased-handoff. 상세=§10 T10.

---

## v0.2.0-suite-dogfood — 빌드 + full dogfood 15단계 完 (2026-06-14, `2eb6c4e`)

§10 1~15단계 완주: 오케스트레이터 골격 + ztr Phase 8(run-phase resume 체인) + ACP 흡수·이벤트 계약 5 slice(모델·collector/store·emit·poller·대시보드) + 실행 어댑터 코드화 + N=2 full dogfood(무인 관제경로·§0.1 절감 3종). **외부 실프로젝트 검증·하드닝은 당시 NOT CLAIMED** → v0.3.0에서 착수.

## v0.1.0-orchestrator-mvp — 바깥 루프 오케스트레이터 골격 (2026-06-14, `8b0ea7b`)

`phase-cycle-orchestrator` 스킬 + 2-leg 리뷰 게이트 확정. 1차 목표(채팅→N페이즈를 코드가 아니라 LLM 세션이 운전) 골격.
