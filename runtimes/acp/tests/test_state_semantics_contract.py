"""tests/test_state_semantics_contract.py — 상태 의미 문구 계약 (T14 S2 D5).

HOLDING은 실측 어휘에서 "승인/입력 대기"가 아니라 **무응답(stalled)**이다.
문서 검토에 의존하면 문구가 조용히 되돌아가므로 기계적으로 고정한다.
"""
from __future__ import annotations

from pathlib import Path

_ACP = Path(__file__).resolve().parent.parent / "acp"

# 상태 의미를 소유하는 지점 — 한 곳만 고치면 "코드는 stalled, 모델 계약은 입력대기"라는
# 새 불일치가 남는다.
_MEANING_OWNERS = (
    _ACP / "models.py",
    _ACP / "liveness.py",
)

# HOLDING을 승인/입력 대기로 규정하는 표현.
_FORBIDDEN = ("입력대기", "입력 대기", "승인대기 상태", "승인 대기 상태")


def test_holding_is_not_described_as_approval_wait():
    for path in _MEANING_OWNERS:
        text = path.read_text(encoding="utf-8")
        for phrase in _FORBIDDEN:
            assert phrase not in text, f"{path.name}에 '{phrase}' 재등장 — HOLDING 의미는 무응답(stalled)"


def test_holding_meaning_is_stated_explicitly():
    """정의가 사라지지도 않아야 한다(빈칸으로 통과하는 것을 막는다)."""
    models = (_ACP / "models.py").read_text(encoding="utf-8")
    assert "무응답" in models
    liveness = (_ACP / "liveness.py").read_text(encoding="utf-8")
    assert "무응답" in liveness
