"""실행 어댑터 — project.config 템플릿의 `{name}` 치환 + argv 조립 (결정론적).

EXECUTION_ADAPTER_CONTRACT §5 규칙을 코드로 구현한다. run-phase는 치환하지 않고
(§1) 완성된 argv를 받으므로, 이 모듈이 오케스트레이터를 대신해 LLM 수작업 없이
argv 배열을 만든다(§10 step 14 — 무인 자율 enabler).

규칙:
- `{name}` 토큰을 **단일 pass**로 context 값으로 치환한다(값 내부는 재귀 치환 안 함).
- 리터럴 중괄호는 `{{`, `}}`로 쓴다.
- 미정의 토큰이 남으면 `RenderError`(실행 전 BLOCKED).
- 잘못된/짝 안 맞는 중괄호도 `RenderError`.
- 치환 결과가 빈 문자열인 argv 원소는 **생략**한다(조건부 flag, 예: `{record_flag}`).
- R5: 토큰 값은 불투명 문자열 사실일 뿐, 의미를 해석하지 않는다.
"""
from __future__ import annotations

import re

_OPEN = "\x00OPEN\x00"
_CLOSE = "\x00CLOSE\x00"
_TOKEN_RE = re.compile(r"\{([A-Za-z0-9_]+)\}")


class RenderError(ValueError):
    """템플릿 치환 실패 — 실행 전 BLOCKED 사유."""


def render_element(element: str, context: dict[str, str]) -> str:
    """단일 argv 원소를 단일 pass로 치환한다."""
    # NUL 바이트는 내부 sentinel과 충돌해 치환을 오염시킬 수 있고, argv로 exec될 수도
    # 없다. 입력 단계에서 거부한다(R5 불투명-문자열 보장 + 실행 전 BLOCKED).
    if "\x00" in element:
        raise RenderError(f"NUL 바이트는 허용되지 않는다: {element!r}")
    protected = element.replace("{{", _OPEN).replace("}}", _CLOSE)

    # 유효 토큰을 제거한 뒤에도 중괄호가 남으면 잘못된 토큰/짝 불일치
    stray = _TOKEN_RE.sub("", protected)
    if "{" in stray or "}" in stray:
        raise RenderError(
            f"잘못된 토큰 또는 짝 안 맞는 중괄호: {element!r} (리터럴은 {{ }}로 쓴다)"
        )

    missing = sorted(
        {m.group(1) for m in _TOKEN_RE.finditer(protected) if m.group(1) not in context}
    )
    if missing:
        raise RenderError(f"미정의 토큰: {missing}")

    # 단일 pass 치환(값 내부 재귀 치환 없음) → 리터럴 중괄호 복원
    def _sub(match: re.Match[str]) -> str:
        value = context[match.group(1)]
        if "\x00" in value:
            raise RenderError(f"토큰 값에 NUL 바이트: {match.group(1)!r}")
        return value

    result = _TOKEN_RE.sub(_sub, protected)
    return result.replace(_OPEN, "{").replace(_CLOSE, "}")


def render_argv(template: list[str], context: dict[str, str]) -> list[str]:
    """argv 템플릿을 치환해 완성 argv 배열을 만든다.

    빈 문자열로 치환된 원소는 생략한다(조건부 flag — 빈 원소를 넣지 않는다, §5).
    """
    rendered: list[str] = []
    for element in template:
        value = render_element(element, context)
        if value == "":
            continue
        rendered.append(value)
    return rendered
