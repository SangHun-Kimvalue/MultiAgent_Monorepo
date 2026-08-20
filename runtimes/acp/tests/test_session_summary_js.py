"""세션 집계(KPI) 정직성 JS 계약을 node로 실측하는 pytest 래퍼.

`test_orch_run_store_js.py`와 같은 패턴. node 미설치 환경에서는 skip(열화로 정직 보고).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_NODE = shutil.which("node")
_JS_TEST = Path(__file__).resolve().parent / "js" / "session_summary_store.test.mjs"


@pytest.mark.skipif(_NODE is None, reason="node 미설치 — JS 집계 계약 테스트 건너뜀")
def test_session_summary_store_js_contract():
    result = subprocess.run(
        [_NODE, str(_JS_TEST)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"node JS 집계 계약 테스트 실패:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK session_summary_store" in result.stdout
