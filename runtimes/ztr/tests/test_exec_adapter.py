"""실행 어댑터(exec_adapter) 단위 테스트 — EXECUTION_ADAPTER_CONTRACT §5."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.engine.exec_adapter import RenderError, render_argv, render_element


def test_render_substitutes_tokens_single_pass() -> None:
    argv = render_argv(
        ["ztr", "run-phase", "--phase-id", "{phase_id}", "--prompt-file", "{prompt_file}"],
        {"phase_id": "p1", "prompt_file": "D:/x/p.md"},
    )

    assert argv == ["ztr", "run-phase", "--phase-id", "p1", "--prompt-file", "D:/x/p.md"]


def test_undefined_token_raises_blocked() -> None:
    with pytest.raises(RenderError, match="미정의 토큰"):
        render_argv(["ztr", "{missing}"], {"phase_id": "p1"})


def test_malformed_brace_raises() -> None:
    with pytest.raises(RenderError, match="중괄호"):
        render_element("a{b", {})


def test_literal_braces_via_double() -> None:
    assert render_element("{{not_a_token}}", {}) == "{not_a_token}"
    # 리터럴 {{ }}는 context에 값이 있어도 토큰으로 치환하지 않는다
    assert render_element("{{name}}", {"name": "v"}) == "{name}"


def test_ambiguous_triple_brace_is_blocked() -> None:
    # {{{name}}} 같은 모호한 중첩 중괄호는 추측 치환하지 않고 BLOCKED
    with pytest.raises(RenderError, match="중괄호"):
        render_element("{{{name}}}", {"name": "v"})


def test_quad_brace_is_two_literal_braces() -> None:
    # {{{{name}}}} = 리터럴 {{ + name + }} → "{{name}}" (회귀 핀)
    assert render_element("{{{{name}}}}", {"name": "v"}) == "{{name}}"


def test_nul_byte_in_element_is_blocked() -> None:
    # 내부 sentinel 충돌/exec 불가 → NUL 입력은 거부(R5 불투명-문자열 보장)
    with pytest.raises(RenderError, match="NUL"):
        render_element("\x00OPEN\x00x\x00CLOSE\x00", {})


def test_nul_byte_in_token_value_is_blocked() -> None:
    with pytest.raises(RenderError, match="NUL"):
        render_element("{x}", {"x": "a\x00b"})


def test_empty_value_element_is_omitted() -> None:
    # 조건부 flag: {record_flag} 값이 ""면 그 원소를 생략한다
    on = render_argv(["ztr", "review", "{record_flag}"], {"record_flag": "--record"})
    off = render_argv(["ztr", "review", "{record_flag}"], {"record_flag": ""})

    assert on == ["ztr", "review", "--record"]
    assert off == ["ztr", "review"]


def test_value_with_braces_is_not_re_substituted() -> None:
    # 값 내부의 {...}는 재귀 치환하지 않는다(단일 pass)
    out = render_element("{x}", {"x": "{phase_id}"})

    assert out == "{phase_id}"


def test_value_is_opaque_no_recursion_into_other_tokens() -> None:
    out = render_argv(["{a}", "{b}"], {"a": "{b}", "b": "literal"})

    assert out == ["{b}", "literal"]


def _render_cli(template: list[str], context: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    repo = str(Path.cwd())
    env["PYTHONPATH"] = repo if not env.get("PYTHONPATH") else f"{repo}{os.pathsep}{env['PYTHONPATH']}"
    return subprocess.run(
        [sys.executable, "-m", "src", "render-argv",
         "--template", json.dumps(template), "--context", json.dumps(context)],
        cwd=Path.cwd(), env=env, text=True, encoding="utf-8",
        capture_output=True, timeout=30, check=False,
    )


def test_render_argv_cli_outputs_json() -> None:
    proc = _render_cli(["ztr", "{phase_id}", "{record_flag}"], {"phase_id": "p1", "record_flag": ""})

    assert proc.returncode == 0
    assert json.loads(proc.stdout) == ["ztr", "p1"]


def test_render_argv_cli_undefined_token_is_blocked_exit2() -> None:
    proc = _render_cli(["{missing}"], {})

    assert proc.returncode == 2
