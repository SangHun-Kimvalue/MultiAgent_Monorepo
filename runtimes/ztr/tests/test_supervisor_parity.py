"""ztr 쪽 supervisor 동등성 게이트 (P1)."""
from __future__ import annotations

from tests._supervisor_parity import assert_parity


def test_process_supervisor_copies_are_in_sync() -> None:
    assert_parity(__file__)
