"""Deterministic token substitution for phase-plan Markdown templates."""

from __future__ import annotations

import re
from typing import Any

__all__: list[str] = []

_REQUIRED_TOKENS = (
    "phase_id",
    "base_sha",
    "allowed_paths",
    "forbidden_paths",
    "dod",
    "not_claimed",
)
_LIST_TOKENS = {"allowed_paths", "forbidden_paths", "not_claimed"}
_NONEMPTY_LIST_TOKENS = {"allowed_paths", "not_claimed"}
_TOKEN_PATTERN = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")


class _TemplateError(Exception):
    def __init__(self, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _format_value(value: Any) -> str:
    if isinstance(value, list):
        return "\n".join(f"- {item}" for item in value) or "(none)"
    return str(value)


def _validate_values(values: Any) -> dict[str, Any]:
    if not isinstance(values, dict):
        raise _TemplateError(1, "values must be a JSON object")
    missing = [name for name in _REQUIRED_TOKENS if name not in values]
    if missing:
        raise _TemplateError(1, f"required tokens are missing: {missing}")
    for name in _REQUIRED_TOKENS:
        value = values[name]
        if isinstance(value, str) and not value:
            raise _TemplateError(1, f"required token {name} must not be an empty string")
    for name in _LIST_TOKENS:
        value = values[name]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise _TemplateError(1, f"required token {name} must be an array of strings")
        if name in _NONEMPTY_LIST_TOKENS and not value:
            raise _TemplateError(1, f"required token {name} must not be an empty array")
    if not isinstance(values["dod"], str):
        raise _TemplateError(1, "required token dod must be a string")
    for name in ("phase_id", "base_sha"):
        if not isinstance(values[name], str):
            raise _TemplateError(1, f"required token {name} must be a string")
    return values


def _render_template_text(source: str, template_name: str, values: Any) -> str:
    validated = _validate_values(values)

    def replace_token(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in validated:
            return match.group(0)
        return _format_value(validated[name])

    rendered = _TOKEN_PATTERN.sub(replace_token, source)
    unresolved = sorted(set(_TOKEN_PATTERN.findall(rendered)))
    if unresolved:
        raise _TemplateError(2, f"template tokens are unresolved: {unresolved}")
    return f"template: {template_name}\n{rendered.rstrip()}\n"
