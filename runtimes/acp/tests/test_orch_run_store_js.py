"""Phase 2 구동 패널 JS store 계약을 node로 실측하는 pytest 래퍼.

브라우저 JS 단위 테스트 인프라가 없어 node 스크립트로 dashboardStore()의 run/approve
메서드와 상태 전이를 검증한다. node 미설치 환경에서는 skip(열화로 정직 보고).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_NODE = shutil.which("node")
_JS_TEST = Path(__file__).resolve().parent / "js" / "orch_run_store.test.mjs"


@pytest.mark.skipif(_NODE is None, reason="node 미설치 — JS store 단위 테스트 건너뜀")
def test_orch_run_store_js_contract():
    result = subprocess.run(
        [_NODE, str(_JS_TEST)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"node JS store 테스트 실패:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK orch_run_store" in result.stdout
