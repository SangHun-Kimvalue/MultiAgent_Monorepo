"""Phase 8 resume 체인 단위 테스트."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.engine.resume_chain import (
    ResumeAttempt,
    ResumeCoordinator,
    ResumeSpec,
    SessionMap,
    build_resume_argv,
    build_resume_argv_plan,
    extract_session_id,
)


def test_extract_claude_session_id_from_stdout_json() -> None:
    stdout = json.dumps({"session_id": "claude-session-123", "result": "ok"})

    assert extract_session_id(stdout, profile="claude") == "claude-session-123"


def test_extract_codex_thread_id_from_jsonl() -> None:
    stdout = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "codex-thread-123"}),
        json.dumps({"type": "item.completed"}),
    ])

    assert extract_session_id(stdout, profile="codex") == "codex-thread-123"


def test_extract_missing_field_stops_instead_of_guessing() -> None:
    with pytest.raises(ValueError, match="thread.started.thread_id"):
        extract_session_id('{"type":"item.completed"}', profile="codex")


def test_extract_claude_missing_session_id_stops_instead_of_guessing() -> None:
    with pytest.raises(ValueError, match="session_id 문자열"):
        extract_session_id(json.dumps({"result": "ok"}), profile="claude")


def test_session_map_loads_utf8_sig_and_saves_atomically(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    path.write_bytes(
        b"\xef\xbb\xbf"
        + json.dumps({"implementer": "old-id"}, ensure_ascii=False).encode("utf-8")
    )

    session_map = SessionMap.load(path)
    session_map.set("reviewer", "review-id")

    reloaded = SessionMap.load(path)
    assert reloaded.get("implementer") == "old-id"
    assert reloaded.get("reviewer") == "review-id"
    assert not list(tmp_path.glob("*.tmp"))


def test_session_map_absent_file_starts_empty(tmp_path: Path) -> None:
    session_map = SessionMap.load(tmp_path / "missing.json")

    assert session_map.get("implementer") is None


def test_session_map_replace_failure_preserves_target_and_removes_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.engine.resume_chain as resume_mod

    path = tmp_path / "sessions.json"
    path.write_text('{"implementer":"old"}', encoding="utf-8")
    session_map = SessionMap.load(path)
    session_map.values["implementer"] = "new"

    def fail_replace(*_: object) -> None:
        raise OSError("fail")

    monkeypatch.setattr(resume_mod.os, "replace", fail_replace)
    with pytest.raises(OSError, match="fail"):
        session_map.save()
    assert json.loads(path.read_text(encoding="utf-8")) == {"implementer": "old"}
    assert not list(tmp_path.glob("*.tmp"))


def test_session_map_invalid_json_raises_value_error(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    path.write_text("{", encoding="utf-8")

    with pytest.raises(ValueError, match="session-map JSON 파싱 실패"):
        SessionMap.load(path)


def test_session_map_rejects_non_object_top_level(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="최상위 값은 object"):
        SessionMap.load(path)


def test_session_map_rejects_non_string_key_or_value(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({"implementer": 123}), encoding="utf-8")

    with pytest.raises(ValueError, match="문자열 역할명과 문자열 id"):
        SessionMap.load(path)


def test_session_map_ignores_blank_session_ids(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({"implementer": "   "}), encoding="utf-8")

    session_map = SessionMap.load(path)

    assert session_map.get("implementer") is None


def test_claude_resume_argv_injects_resume_and_json_output() -> None:
    argv = build_resume_argv(
        ["claude", "-p", "--model", "sonnet"],
        profile="claude",
        session_id="session-123",
    )

    assert argv == [
        "claude",
        "-p",
        "--resume",
        "session-123",
        "--model",
        "sonnet",
        "--output-format",
        "json",
    ]


def test_claude_new_argv_only_forces_json_output() -> None:
    argv = build_resume_argv(
        ["claude", "-p", "--output-format", "text"],
        profile="claude",
        session_id=None,
    )

    assert argv == ["claude", "-p", "--output-format", "json"]


def test_codex_resume_argv_uses_exec_resume_shape() -> None:
    plan = build_resume_argv_plan(
        ["codex", "exec", "--cd", "D:\\ZRT"],
        profile="codex",
        session_id="thread-123",
    )

    assert plan.argv == [
        "codex",
        "exec",
        "resume",
        "thread-123",
        "--json",
    ]
    assert plan.working_dir == "D:\\ZRT"


@pytest.mark.parametrize(
    ("tokens", "flag", "value"),
    [
        (["--cd", "D:\\ZRT"], "--cd", "D:\\ZRT"),
        (["-C", "D:\\ZRT"], "-C", "D:\\ZRT"),
        (["--sandbox", "workspace-write"], "--sandbox", "workspace-write"),
        (["-s", "read-only"], "-s", "read-only"),
        (["--add-dir", "D:\\Extra"], "--add-dir", "D:\\Extra"),
        (["--profile", "work"], "--profile", "work"),
        (["-p", "work"], "-p", "work"),
        (["--oss"], "--oss", None),
        (["-V"], "-V", None),
        (["--version"], "--version", None),
        (["--local-provider", "ollama"], "--local-provider", "ollama"),
        (["--color", "never"], "--color", "never"),
    ],
)
def test_codex_resume_strips_each_exec_only_flag(
    tokens: list[str], flag: str, value: str | None
) -> None:
    argv = build_resume_argv(
        ["codex", "exec", *tokens, "-"], profile="codex", session_id="thread-123"
    )

    assert flag not in argv
    if value is not None:
        assert value not in argv


@pytest.mark.parametrize(
    "token",
    [
        "--sandbox=workspace-write",
        "--cd=D:\\ZRT",
        "-Cfoo",
        "-sfoo",
        "-pfoo",
        "-C=foo",
        "-s=foo",
        "-p=foo",
    ],
)
def test_codex_resume_strips_joined_exec_only_values(token: str) -> None:
    argv = build_resume_argv(
        ["codex", "exec", token, "-"], profile="codex", session_id="thread-123"
    )

    assert token not in argv


@pytest.mark.parametrize("token", ["-CD:\\ZRT", "-C=D:\\ZRT", "--cd=D:\\ZRT"])
def test_codex_resume_extracts_joined_working_dir_without_prefix(token: str) -> None:
    plan = build_resume_argv_plan(
        ["codex", "exec", token, "-"], profile="codex", session_id="thread-123"
    )

    assert plan.working_dir == "D:\\ZRT"


def test_codex_new_argv_preserves_exec_only_flags() -> None:
    argv = build_resume_argv(
        ["codex", "exec", "--cd", "D:\\ZRT", "-s", "read-only", "-"],
        profile="codex",
        session_id=None,
    )

    assert argv == [
        "codex", "exec", "--json", "--cd", "D:\\ZRT", "-s", "read-only", "-"
    ]


@pytest.mark.parametrize(
    "flag",
    [
        "-C",
        "--cd",
        "-s",
        "--sandbox",
        "--add-dir",
        "-p",
        "--profile",
        "--local-provider",
        "--color",
    ],
)
@pytest.mark.parametrize("trailing", [[], ["--json", "-"], ["-"]])
def test_codex_resume_missing_exec_only_value_is_blocked(
    flag: str, trailing: list[str]
) -> None:
    plan = build_resume_argv_plan(
        ["codex", "exec", flag, *trailing],
        profile="codex",
        session_id="thread-123",
    )

    assert plan.block_reason == f"codex resume argv: {flag} 값 누락"
    assert plan.working_dir not in {"--json", "-"}


def test_codex_resume_preserves_repeated_image_options() -> None:
    argv = build_resume_argv(
        ["codex", "exec", "-i", "a", "-i", "b", "-"],
        profile="codex",
        session_id="thread-123",
    )

    assert argv[-5:] == ["-i", "a", "-i", "b", "-"]


def test_codex_resume_leaves_contiguous_image_values_unchanged() -> None:
    """resume single-value arity라 실행 시 clap이 시끄럽게 실패함 = 지원 NOT CLAIMED."""
    argv = build_resume_argv(
        ["codex", "exec", "-i", "a", "b", "-"],
        profile="codex",
        session_id="thread-123",
    )

    assert argv[-4:] == ["-i", "a", "b", "-"]


def test_resume_attempt_surfaces_strip_without_changing_verdict_facts() -> None:
    coordinator = ResumeCoordinator(
        session_map=None,
        specs={
            "implementer": ResumeSpec(
                role="implementer", policy="thread-123", profile="codex"
            )
        },
    )

    _, attempt = coordinator.prepare(
        ["codex", "exec", "--sandbox", "read-only", "--cd", "D:\\ZRT"],
        role="implementer",
    )
    payload = attempt.as_payload()

    assert payload["working_dir"] == "D:\\ZRT"
    assert payload["stripped_flags"] == ["--sandbox", "--cd"]
    assert payload["resumed"] is True
    assert payload["requested_id"] == "thread-123"
    assert coordinator.fallback_used is False
    assert len(coordinator.warnings) == 1
    assert "NOT CLAIMED" in coordinator.warnings[0]


def test_codex_new_argv_only_forces_json_output() -> None:
    argv = build_resume_argv(
        ["codex", "exec", "--json", "prompt"],
        profile="codex",
        session_id=None,
    )

    assert argv == ["codex", "exec", "--json", "prompt"]


def test_none_resume_profile_returns_unmodified_copy() -> None:
    original = ["custom", "--flag"]

    argv = build_resume_argv(original, profile="none", session_id="ignored")

    assert argv == original
    assert argv is not original


def test_codex_resume_requires_exec_argv_shape() -> None:
    with pytest.raises(ValueError, match="codex exec"):
        build_resume_argv(["codex"], profile="codex", session_id="thread-123")


def test_auto_resume_failure_can_fallback_but_explicit_id_cannot() -> None:
    coordinator = ResumeCoordinator(
        session_map=None,
        specs={"implementer": ResumeSpec(role="implementer")},
    )
    auto_attempt = ResumeAttempt(
        role="implementer",
        profile="codex",
        policy="auto",
        requested_id="thread-123",
        resumed=True,
    )
    explicit_attempt = ResumeAttempt(
        role="implementer",
        profile="codex",
        policy="thread-123",
        requested_id="thread-123",
        resumed=True,
    )

    assert coordinator.should_fallback(auto_attempt) is True
    assert coordinator.should_fallback(explicit_attempt) is False
