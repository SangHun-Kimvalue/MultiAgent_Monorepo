# artifacts/ — 산출물 양식 인덱스

방법론이 쓰는 문서/프롬프트 양식 모음. 중복(drift)을 피하려고 **마스터 위치를 가리키는 인덱스**로 둔다.
대부분은 Planner-session handoff 스킬의 `references/`에 마스터가 있고, 일부는 캐논 문서 안에 정의되어 있다.

| 양식 | 마스터 위치 | 용도 |
|---|---|---|
| Discovery role assignment / requirements / design / validation / open items / handoff 템플릿 | `plugins/agent-workflow/skills/phase0-discovery-interview/assets/templates/` | Phase 0 gate 산출물 |
| Discovery 질문 가이드 / profile 질문 | `plugins/agent-workflow/skills/phase0-discovery-interview/references/` | 착수 전 interview + critique |
| 구현 프롬프트 스켈레톤 (debate/implement/review 3모드) | `plugins/agent-workflow/skills/phased-implementation-handoff/references/prompt-skeleton.md` | Planner→Implementer 핸드오프 |
| 로드맵 템플릿 (결정로그/페이즈/DoD) | `…/references/roadmap-template.md` | 진행/결정 SSoT |
| HANDOFF 템플릿 + lessons 블록 + ADR 포인터 | `…/references/doc-management.md` | Sync-Out 인수인계·교훈 |
| 계획·실측 방법 | `…/references/planning-method.md` | Planner 계획 절차 |
| 리뷰 finding 정형 | `METHODOLOGY.md` §2 + `MULTI_AGENT.md` §4 | Reviewer 출력 |
| finding disposition 양식 | `artifacts/finding-disposition.md` | Reviewer finding 선별·근거·corrective round·재리뷰 기록 |
| 구현 리뷰 verdict 스키마/예시 | `plugins/agent-workflow/skills/phased-implementation-handoff/assets/review-verdict.schema.json`, `review-verdict.example.json` | 설치 surface에도 동봉되는 cross-lineage provenance·enum verdict 계약 |
| 문서 체계(최소셋/풀셋) | `DOC_TAXONOMY.md` | 어떤 문서를 둘지 |
| 프로젝트 config 템플릿 | `config/project.config.example.md` | 프로젝트 특화값(PARAM) |

> 향후 이 양식들을 `artifacts/`로 물리 이관할 수도 있으나, 지금은 surface adapter가 **self-contained**여야 하므로
> 마스터를 스킬 `references/`에 두고 여기서 인덱싱한다(불필요한 복사 = drift, C5 위반). 실수요 시 단일 위치로 통합.
