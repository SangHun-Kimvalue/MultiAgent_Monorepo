# runtimes/ — 런타임 컴포넌트

기계 사실(ztr)과 관제(ACP)를 제공하는 런타임. 캐논·스킬과 **계약(envelope/exit code/이벤트)으로만** 결합(ADR AD-3).

## 흡수 시점 (ADR AD-4 — 단계화, 빅뱅 금지)
| 컴포넌트 | prefix | 흡수 시점 | 상태 |
|---|---|---|---|
| ztr (mechanical runtime + 안쪽 루프 relay) | `runtimes/ztr/` | **ztr Phase 8(resume 체인) 착수 시** | ⬜ 대기 (`D:\ZRT`) |
| ACP (control plane / 관제) | `runtimes/acp/` | **오케스트레이터 골격 동작 후** | ⬜ 대기 (`D:\Dashboard`) |

흡수 명령(예):
```
git remote add ztr-src <ztr repo/local path>
git subtree add --prefix=runtimes/ztr ztr-src main
```
> 흡수 전 각 컴포넌트 `git bundle` 백업 필수(외부 공개 없이 전체 이력 복구).
