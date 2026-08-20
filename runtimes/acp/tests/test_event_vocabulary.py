"""tests/test_event_vocabulary.py — 분기 상수 ↔ 실측 어휘 계약 (T14 S2 D1).

이 결함의 근인은 픽스처가 **손으로 쓴 가짜 어휘**(`task_aborted`)를 써서, 테스트가
초록인 채로 실데이터의 `turn_aborted`가 어느 분기에도 닿지 않은 것이었다
(LESSON-002 rule 7과 동형: 테스트가 갭을 가림).

그래서 계약은 synthetic 픽스처가 아니라 **실측 어휘 artifact**에 건다.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from acp.liveness import ERROR_EVENTS, TURN_END_EVENTS
from tests.vocab_extract import VOCAB_PATH, extract, load_artifact

# 분기 상수 중 실측 어휘에 없는 항목은 **여기 명시적으로 등재**돼야 통과한다.
# 조용한 dead branch를 금지하기 위한 목록이다.
_LEGACY_OR_DEFENSIVE = {
    "task_aborted": "실측 0건. `turn_aborted`와 같은 뜻의 다른 철자 — 축이 갈리지 않도록 함께 묶음",
    "error": "실측 0건. 명시적 오류 축의 자리(다른 앱/버전이 emit할 수 있음)",
}

_ABORT_OR_COMPLETE_SUFFIXES = ("_aborted", "_complete")


def _artifact_types() -> set[str]:
    return set(load_artifact()["event_types"])


def test_vocabulary_artifact_exists_with_provenance():
    """artifact는 추출기 산출물이어야 한다 — 손으로 적은 목록은 '실측'이 아니다."""
    data = json.loads(VOCAB_PATH.read_text(encoding="utf-8"))
    provenance = data["provenance"]
    assert provenance["extractor"] == "tests/vocab_extract.py"
    assert provenance["sample_files"] > 0
    assert data["event_types"], "빈 목록이면 계약이 아무것도 검사하지 않는다"


def test_abort_and_complete_vocabulary_is_covered_by_turn_end():
    """실측 어휘의 abort/complete 계열이 분기 상수에서 빠지면 실패.

    이번 결함의 직접 회귀 — `turn_aborted`가 어느 축에도 없던 상태를 잡는다.
    """
    observed = _artifact_types()
    relevant = {
        event
        for event in observed
        if event.endswith(_ABORT_OR_COMPLETE_SUFFIXES) and not event.startswith(("patch_", "mcp_", "web_", "image_"))
    }
    assert relevant, "표본에 abort/complete 계열이 하나도 없다면 표본이 잘못됐다"
    missing = relevant - set(TURN_END_EVENTS)
    assert not missing, f"실측 어휘가 턴 종료 축에서 누락됨: {sorted(missing)}"


def test_error_and_turn_end_axes_are_disjoint_partition():
    """의미 파티션까지 단언한다 — 단순 포함 관계만 보면 축이 뒤바뀌어도 통과한다."""
    assert set(TURN_END_EVENTS) & set(ERROR_EVENTS) == set()
    assert set(ERROR_EVENTS) == {"error"}


def test_branch_constants_absent_from_vocabulary_are_declared():
    """실측에 없는 분기 상수는 '레거시/방어'로 명시 등재돼야 한다(조용한 dead branch 금지)."""
    observed = _artifact_types()
    unobserved = (set(TURN_END_EVENTS) | set(ERROR_EVENTS)) - observed
    undeclared = unobserved - set(_LEGACY_OR_DEFENSIVE)
    assert not undeclared, f"실측 0건인데 사유 미등재: {sorted(undeclared)}"


def test_live_logs_have_no_new_event_types():
    """로그가 있는 환경에서는 **새 어휘가 나타나면 실패**한다.

    정적 artifact 단독으로는 미래 변경을 감지하지 못한다. 실제 로그가 있을 때만
    추출 결과와 비교하고, 없으면 명시 사유로 skip한다(열화를 정직하게 표면화).
    이 감지는 **로그 보유 환경 한정**이며 CI 일반에 대해서는 NOT CLAIMED다.
    """
    try:
        from acp.config import AppConfig

        base: Path = AppConfig.load("config/paths.yaml").get_path("codex_sessions")
    except Exception as exc:  # pragma: no cover - 환경 의존
        pytest.skip(f"codex 설정을 읽을 수 없음: {exc}")

    if not base.exists():
        pytest.skip(f"codex rollout 로그 없음: {base}")

    observed = set(extract(base))
    if not observed:
        pytest.skip(f"rollout 표본에서 event_msg를 찾지 못함: {base}")

    new_types = observed - _artifact_types()
    assert not new_types, (
        f"실측 로그에 artifact에 없는 event type: {sorted(new_types)} — "
        "`python -m tests.vocab_extract --write`로 갱신하고 분기 축을 재검토하라"
    )
