"""Tests for the local run_nit wrapper."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "run_nit.py"
ENVELOPE = Path(__file__).resolve().parent / "nit_envelope.py"
MAM_ROOT = SCRIPT.parents[2]
SPEC = importlib.util.spec_from_file_location("run_nit_under_test", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
run_nit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(run_nit)


class _FakeUrlopenResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> _FakeUrlopenResponse:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    (repo / "a.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "b.py").write_text("other = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "seed")
    return repo


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), *args],
        cwd=repo,
        check=False,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _last_line(output: str) -> str:
    return [line for line in output.splitlines() if line.strip()][-1]


def _config(**overrides: object) -> dict[str, object]:
    config: dict[str, object] = {
        "provider": "ollama",
        "base_url": "http://localhost:11434",
        "model": "qwen3:8b",
        "timeout_seconds": 12,
        "max_diff_chars": 10000,
        "include_extensions": [".py", ".json", ".md"],
        "exclude_prefixes": [".git/"],
    }
    config.update(overrides)
    return config


def _fixture_finding_marker() -> str:
    return "NITPICKER_" + "FIXTURE_FINDING"


@pytest.fixture(autouse=True)
def _restore_repo() -> None:
    original = run_nit.REPO
    yield
    run_nit.REPO = original


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("**STATUS: ALL PASS**\n- acceptable", "ALL PASS"),
        ("# ALL PASS\n- acceptable", "ALL PASS"),
        ("`ALL PASS`\n- acceptable", "ALL PASS"),
        ("STATUS: CHANGES_REQUESTED\n- fix required", "CHANGES_REQUESTED"),
        ("ALL PASS\n- acceptable", "ALL PASS"),
        ("RESULT: BLOCKED\n- cannot review", "BLOCKED"),
    ],
)
def test_extract_status_accepts_header_tokens(output: str, expected: str) -> None:
    assert run_nit._extract_status(output) == expected


def test_extract_status_ignores_body_token_mentions_after_status() -> None:
    output = "ALL PASS\n- This body mentions BLOCKED as a word, not a header token."

    assert run_nit._extract_status(output) == "ALL PASS"


def test_extract_status_finds_blocked_on_second_header_line() -> None:
    output = "Here is my review:\n**STATUS: BLOCKED**\n- missing context"

    assert run_nit._extract_status(output) == "BLOCKED"


def test_extract_status_prioritizes_blocked_over_all_pass_within_header_window() -> None:
    output = "ALL PASS\nBLOCKED\n- conflicting header tokens"

    assert run_nit._extract_status(output) == "BLOCKED"


def test_extract_status_blocks_when_no_header_token() -> None:
    output = "\n".join(
        [
            "Here is my review:",
            "I looked at the patch.",
            "There are no obvious findings.",
            "The implementation seems fine.",
            "That is all.",
            "ALL PASS",
        ]
    )

    assert run_nit._extract_status(output) == "BLOCKED"


def test_extract_status_prose_only_remains_blocked() -> None:
    output = "This diff looks acceptable overall.\nNo actionable issues found."

    assert run_nit._extract_status(output) == "BLOCKED"


def test_ollama_response_schema_is_strict_literal() -> None:
    assert run_nit.OLLAMA_RESPONSE_SCHEMA == {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["ALL PASS", "CHANGES_REQUESTED", "BLOCKED"],
            },
            "review": {"type": "string"},
        },
        "required": ["status", "review"],
        "additionalProperties": False,
    }


def test_build_prompt_requires_json_object_only() -> None:
    prompt = run_nit.build_prompt("demo.py", "@@ -1 +1 @@\n-print(1)\n+print(2)\n")

    assert 'Return only a JSON object that matches the provided response schema.' in prompt
    assert 'Use "status" with exactly one of: ALL PASS, CHANGES_REQUESTED, BLOCKED.' in prompt
    assert "The first line must contain only one status token exactly" not in prompt
    assert "Return exactly one of these statuses at the top" not in prompt


def test_build_prompt_includes_exact_verdict_rubric_rules() -> None:
    prompt = run_nit.build_prompt("demo.py", "@@ -1 +1 @@\n-old_name()\n+new_name()\n")

    assert "Decision rules:" in prompt
    assert (
        "- BLOCKED: use only when required context is missing, the diff is truncated, "
        "a transport/tool failure occurred, or review is otherwise impossible."
    ) in prompt
    assert "- CHANGES_REQUESTED: use only when the diff contains a concrete defect." in prompt
    assert "- ALL PASS: use when there is no actionable changed-line defect in the diff." in prompt
    assert "Every finding must cite concrete evidence from the current diff." in prompt
    assert "Generic recommendations, optional follow-ups, or future cleanup suggestions are not findings." in prompt
    assert (
        "If you suspect an undefined symbol or missing definition, inspect the full diff first "
        "and only raise it when the symbol is not defined anywhere in this diff."
    ) in prompt


def test_call_ollama_sends_strict_system_role_schema_and_preserves_user_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = "Line 1\nLine 2\n```diff\n+hello\n```"
    config = {
        "base_url": "http://localhost:11434/",
        "model": "qwen2.5-coder:7b",
        "timeout_seconds": 37,
    }
    observed: dict[str, object] = {}

    def fake_urlopen(request, timeout):
        observed["url"] = request.full_url
        observed["timeout"] = timeout
        observed["payload"] = json.loads(request.data.decode("utf-8"))
        return _FakeUrlopenResponse(
            {"message": {"content": json.dumps({"status": "ALL PASS", "review": "- ok"})}}
        )

    monkeypatch.setattr(run_nit.urllib.request, "urlopen", fake_urlopen)

    status, output = run_nit.call_ollama(config, prompt)

    assert status == "ALL PASS"
    assert output == "ALL PASS\n- ok"
    assert observed["url"] == "http://localhost:11434/api/chat"
    assert observed["timeout"] == 37

    payload = observed["payload"]
    assert isinstance(payload, dict)
    assert payload["model"] == "qwen2.5-coder:7b"
    assert payload["stream"] is False
    assert payload["format"] == {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["ALL PASS", "CHANGES_REQUESTED", "BLOCKED"],
            },
            "review": {"type": "string"},
        },
        "required": ["status", "review"],
        "additionalProperties": False,
    }
    assert payload["options"] == {"temperature": 0}
    assert len(payload["messages"]) == 2
    assert payload["messages"][0] == {
        "role": "system",
        "content": run_nit.OLLAMA_SYSTEM_ROLE_MESSAGE,
    }
    assert payload["messages"][1] == {
        "role": "user",
        "content": prompt,
    }


def test_call_ollama_payload_uses_literal_temperature_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_urlopen(request, timeout):
        observed["payload"] = json.loads(request.data.decode("utf-8"))
        return _FakeUrlopenResponse(
            {"message": {"content": json.dumps({"status": "ALL PASS", "review": "- ok"})}}
        )

    monkeypatch.setattr(run_nit.urllib.request, "urlopen", fake_urlopen)

    run_nit.call_ollama(
        {
            "base_url": "http://localhost:11434",
            "model": "qwen2.5-coder:7b",
            "timeout_seconds": 9,
        },
        "prompt",
    )

    payload = observed["payload"]
    assert isinstance(payload, dict)
    assert payload["options"] == {"temperature": 0}


@pytest.mark.parametrize(
    ("status", "review"),
    [
        ("ALL PASS", "- acceptable"),
        ("CHANGES_REQUESTED", "- needs a fix"),
        ("BLOCKED", "- transport failed"),
    ],
)
def test_call_ollama_maps_all_status_enums_to_control_status_and_display_output(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    review: str,
) -> None:
    config = {
        "base_url": "http://localhost:11434",
        "model": "qwen2.5-coder:7b",
        "timeout_seconds": 12,
    }

    def fake_urlopen(request, timeout):
        return _FakeUrlopenResponse(
            {"message": {"content": json.dumps({"status": status, "review": review})}}
        )

    monkeypatch.setattr(run_nit.urllib.request, "urlopen", fake_urlopen)

    control_status, display_output = run_nit.call_ollama(config, "prompt")

    assert control_status == status
    assert display_output == f"{status}\n{review}"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("not-json", "valid JSON"),
        ("[]", "root must be an object"),
        ('{"review":"ok"}', "keys must be exactly"),
        ('{"status":"ALL PASS"}', "keys must be exactly"),
        ('{"status":"ALL PASS","review":"ok","extra":1}', "keys must be exactly"),
        ('{"status":"PASS","review":"ok"}', "supported enum"),
        ('{"status":"ALL PASS","review":5}', "review must be a string"),
        ('{"status":"ALL PASS","review":""}', "must not be empty"),
        ('{"status":"ALL PASS","review":"   "}', "must not be empty"),
    ],
)
def test_parse_ollama_structured_content_rejects_malformed_variants(
    content: str,
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        run_nit._parse_ollama_structured_content(content)


@pytest.mark.parametrize("leading_review_line", ["BLOCKED", "CHANGES_REQUESTED"])
def test_review_file_ollama_uses_control_status_without_extracting_display_prose(
    monkeypatch: pytest.MonkeyPatch,
    leading_review_line: str,
) -> None:
    config = {"max_diff_chars": 100}

    monkeypatch.setattr(run_nit, "diff_for_file", lambda *args, **kwargs: ("diff --git a/x b/x", False))
    observed: dict[str, str] = {}

    def fake_build_prompt(path: str, diff: str, evidence_bundle: str) -> str:
        observed["evidence"] = evidence_bundle
        return "prompt"

    monkeypatch.setattr(run_nit, "build_prompt", fake_build_prompt)
    monkeypatch.setattr(
        run_nit,
        "call_ollama",
        lambda config, prompt: ("ALL PASS", f"ALL PASS\n{leading_review_line}\n- adversarial prose"),
    )

    def fail_extract(_output: str) -> str:
        raise AssertionError("_extract_status must not run for the Ollama control path")

    monkeypatch.setattr(run_nit, "_extract_status", fail_extract)

    result = run_nit.review_file(
        "demo.py",
        config,
        provider="ollama",
        staged=False,
        evidence_bundle='[{"path":"design.md"}]',
    )

    assert result.status == "ALL PASS"
    assert result.output == f"ALL PASS\n{leading_review_line}\n- adversarial prose"
    assert observed["evidence"] == '[{"path":"design.md"}]'


def test_review_file_mock_provider_keeps_legacy_status_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = {"max_diff_chars": 100}
    observed: dict[str, str] = {}

    monkeypatch.setattr(run_nit, "diff_for_file", lambda *args, **kwargs: ("diff --git a/x b/x", False))
    monkeypatch.setattr(run_nit, "mock_review", lambda path, diff: "STATUS: CHANGES_REQUESTED\n- legacy")

    def fake_extract(output: str) -> str:
        observed["output"] = output
        return "CHANGES_REQUESTED"

    monkeypatch.setattr(run_nit, "_extract_status", fake_extract)

    result = run_nit.review_file("demo.py", config, provider="mock", staged=False)

    assert result.status == "CHANGES_REQUESTED"
    assert result.output == "STATUS: CHANGES_REQUESTED\n- legacy"
    assert observed["output"] == "STATUS: CHANGES_REQUESTED\n- legacy"


def test_main_ollama_malformed_structured_response_returns_blocked_exit_3(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(run_nit, "_configure_utf8_streams", lambda: None)
    monkeypatch.setattr(
        run_nit,
        "parse_args",
        lambda _argv=None: argparse.Namespace(
            repo=None,
            model=None,
            provider=None,
            self_test=False,
            staged=False,
            files=["demo.py"],
            include_all=False,
            keep_going=False,
            changed=False,
            evidence_file=["evidence.md"],
            max_evidence_chars=30000,
            file_timeout=[],
        ),
    )
    monkeypatch.setattr(
        run_nit,
        "load_config",
        lambda: {
            "provider": "ollama",
            "base_url": "http://localhost:11434",
            "model": "qwen2.5-coder:7b",
            "timeout_seconds": 5,
            "max_diff_chars": 100,
        },
    )
    monkeypatch.setattr(run_nit, "canonical_explicit_targets", lambda _files: ["demo.py"])
    monkeypatch.setattr(
        run_nit,
        "build_evidence_bundle",
        lambda _files, _max_chars: '[{"path":"evidence.md","content":"context"}]',
    )
    monkeypatch.setattr(run_nit, "diff_for_file", lambda *args, **kwargs: ("diff --git a/x b/x", False))
    observed: dict[str, object] = {}

    def fake_urlopen(request, timeout):
        observed["prompt"] = json.loads(request.data.decode("utf-8"))["messages"][1]["content"]
        return _FakeUrlopenResponse({"message": {"content": "[]"}})

    monkeypatch.setattr(run_nit.urllib.request, "urlopen", fake_urlopen)

    assert run_nit.main() == 3

    captured = capsys.readouterr()
    assert "===== demo.py =====" in captured.out
    assert "BLOCKED" in captured.out
    assert [line for line in captured.out.splitlines() if line.strip()][-1] == "Nitpicker: BLOCKED"
    assert "evidence.md" in str(observed["prompt"])


@pytest.mark.parametrize(
    ("status", "review", "expected_exit", "expected_summary"),
    [
        ("ALL PASS", "- structured ok", 0, "Nitpicker: ALL PASS"),
        ("CHANGES_REQUESTED", "- structured defect", 2, "Nitpicker: CHANGES_REQUESTED"),
    ],
)
def test_main_ollama_happy_path_uses_structured_status_for_exit_and_final_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
    review: str,
    expected_exit: int,
    expected_summary: str,
) -> None:
    monkeypatch.setattr(run_nit, "_configure_utf8_streams", lambda: None)
    monkeypatch.setattr(
        run_nit,
        "parse_args",
        lambda _argv=None: argparse.Namespace(
            repo=None,
            model=None,
            provider=None,
            self_test=False,
            staged=False,
            files=["demo.py"],
            include_all=False,
            keep_going=False,
            changed=False,
            evidence_file=[],
            max_evidence_chars=30000,
            file_timeout=[],
        ),
    )
    monkeypatch.setattr(
        run_nit,
        "load_config",
        lambda: {
            "provider": "ollama",
            "base_url": "http://localhost:11434",
            "model": "qwen2.5-coder:7b",
            "timeout_seconds": 5,
            "max_diff_chars": 100,
        },
    )
    monkeypatch.setattr(run_nit, "canonical_explicit_targets", lambda _files: ["demo.py"])
    monkeypatch.setattr(run_nit, "diff_for_file", lambda *args, **kwargs: ("diff --git a/x b/x", False))

    def fake_urlopen(request, timeout):
        return _FakeUrlopenResponse(
            {"message": {"content": json.dumps({"status": status, "review": review})}}
        )

    monkeypatch.setattr(run_nit.urllib.request, "urlopen", fake_urlopen)

    assert run_nit.main() == expected_exit

    captured = capsys.readouterr()
    assert "===== demo.py =====" in captured.out
    assert f"{status}\n{review}" in captured.out
    assert expected_summary in captured.out


def test_run_nit_repo_option_reviews_explicit_repo(tmp_path: Path) -> None:
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    module_file = repo / "mod.py"
    module_file.write_text("def answer():\n    return 41\n", encoding="utf-8")
    _git(repo, "add", "mod.py")
    _git(repo, "commit", "-m", "seed")
    module_file.write_text("def answer():\n    return 42\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(repo),
            "--provider",
            "mock",
            "--changed",
        ],
        cwd=tmp_path,
        check=False,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode == 0
    assert "===== mod.py =====" in result.stdout
    assert "Mock provider checked mod.py" in result.stdout
    assert "No reviewable files found" not in result.stdout
    assert result.stderr == ""


def test_run_nit_defaults_to_current_working_directory(tmp_path: Path) -> None:
    repo = tmp_path / "default"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    module_file = repo / "mod.py"
    module_file.write_text("def answer():\n    return 41\n", encoding="utf-8")
    _git(repo, "add", "mod.py")
    _git(repo, "commit", "-m", "seed")
    module_file.write_text("def answer():\n    return 42\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--provider", "mock", "--changed"],
        cwd=repo,
        check=False,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode == 0
    assert "===== mod.py =====" in result.stdout
    assert "Mock provider checked mod.py" in result.stdout
    assert result.stderr == ""


def test_model_option_overrides_only_loaded_model(monkeypatch: pytest.MonkeyPatch) -> None:
    original = {
        "provider": "ollama",
        "model": "configured-model",
        "timeout_seconds": 12,
    }
    observed: dict[str, object] = {}

    monkeypatch.setattr(run_nit, "load_config", lambda: dict(original))
    monkeypatch.setattr(
        run_nit,
        "parse_args",
        lambda _argv=None: argparse.Namespace(
            repo=None,
            model="exact-model",
            provider=None,
            self_test=True,
            staged=False,
            files=[],
            include_all=False,
            keep_going=False,
            changed=False,
            evidence_file=[],
            max_evidence_chars=30000,
            file_timeout=[],
        ),
    )

    def fake_self_test(config: dict[str, object]) -> int:
        observed.update(config)
        return 0

    monkeypatch.setattr(run_nit, "self_test", fake_self_test)

    assert run_nit.main() == 0
    assert observed == {
        "provider": "ollama",
        "model": "exact-model",
        "timeout_seconds": 12,
    }
    assert original["model"] == "configured-model"


def test_cp949_subprocess_preserves_non_cp949_path_as_utf8(tmp_path: Path) -> None:
    repo = tmp_path / "cp949-output"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    module_file = repo / "check_✅.py"
    module_file.write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", module_file.name)
    _git(repo, "commit", "-m", "seed")
    module_file.write_text("value = 2\n", encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "cp949"

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--provider", "mock", module_file.name],
        cwd=repo,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    assert result.returncode == 0
    stdout = result.stdout.decode("utf-8")
    assert "check_✅.py" in stdout
    assert b"UnicodeEncodeError" not in result.stderr


# P3 Mechanical adapter hardening focused tests.

INVALID_PATHS = [r"C:foo", r"\foo", "/foo", r"C:\foo", r"\\server\share\foo"]


@pytest.mark.parametrize("value", INVALID_PATHS)
def test_shared_path_validator_rejects_non_pure_relative(value: str) -> None:
    with pytest.raises(run_nit.PreflightError, match="purely repo-relative"):
        run_nit.validate_repo_relative_path(value)


@pytest.mark.parametrize("surface", ["evidence", "target", "timeout"])
@pytest.mark.parametrize("value", INVALID_PATHS)
def test_all_cli_path_surfaces_block_before_provider(
    tmp_path: Path,
    surface: str,
    value: str,
) -> None:
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("value = 2\n", encoding="utf-8")
    args = ["--provider", "mock"]
    if surface == "evidence":
        args.extend(["--evidence-file", value, "a.py"])
    elif surface == "target":
        args.append(value)
    else:
        args.extend(["--file-timeout", f"{value}=3", "a.py"])

    result = _run(repo, *args)

    assert result.returncode == 3
    assert result.stderr == ""
    assert "Mock provider checked" not in result.stdout
    assert _last_line(result.stdout) == "Nitpicker: BLOCKED"


def test_repo_relative_alias_is_canonicalized_and_deduplicated(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    run_nit.REPO = repo

    assert run_nit.canonical_explicit_targets(["b.py", "./a.py", "x/../a.py"]) == [
        "a.py",
        "b.py",
    ]


def test_evidence_preserves_raw_hash_crlf_keys_order_and_single_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    raw = b"line1\r\nline2\r\n"
    (repo / "z.md").write_bytes(raw)
    (repo / "m.md").write_text("middle\n", encoding="utf-8")
    run_nit.REPO = repo
    original = Path.read_bytes
    reads: list[Path] = []

    def counted(path: Path) -> bytes:
        reads.append(path)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", counted)
    bundle = run_nit.build_evidence_bundle(["z.md", "./z.md", "m.md"], 10000)
    entries = json.loads(bundle)

    assert [entry["path"] for entry in entries] == ["m.md", "z.md"]
    assert list(entries[1]) == ["path", "sha256", "char_count", "content"]
    assert entries[1]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert entries[1]["char_count"] == len(raw.decode("utf-8"))
    assert entries[1]["content"] == "line1\r\nline2\r\n"
    assert len(reads) == 2


@pytest.mark.parametrize("kind", ["missing", "directory", "escape", "invalid_utf8", "nul"])
def test_evidence_invalid_inputs_fail_close(tmp_path: Path, kind: str) -> None:
    repo = _repo(tmp_path)
    run_nit.REPO = repo
    value = "bad.md"
    if kind == "directory":
        (repo / value).mkdir()
    elif kind == "escape":
        value = "../outside.md"
        (tmp_path / "outside.md").write_text("outside", encoding="utf-8")
    elif kind == "invalid_utf8":
        (repo / value).write_bytes(b"\xff")
    elif kind == "nul":
        (repo / value).write_bytes(b"a\x00b")

    with pytest.raises(run_nit.PreflightError):
        run_nit.build_evidence_bundle([value], 1000)


def test_evidence_exact_70000_allowed_and_70001_blocked(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    run_nit.REPO = repo
    evidence = repo / "e.md"
    content_size = 69850
    for _ in range(3):
        evidence.write_text("x" * content_size, encoding="utf-8")
        measured = len(run_nit.build_evidence_bundle(["e.md"], 100000))
        content_size += 70000 - measured
    evidence.write_text("x" * content_size, encoding="utf-8")
    assert len(run_nit.build_evidence_bundle(["e.md"], 70000)) == 70000
    evidence.write_text("x" * (content_size + 1), encoding="utf-8")

    with pytest.raises(run_nit.PreflightError) as captured:
        run_nit.build_evidence_bundle(["e.md"], 70000)

    assert captured.value.code == "EVIDENCE_BUDGET_EXCEEDED"


def test_live_p3_evidence_snapshot_is_exact() -> None:
    run_nit.REPO = MAM_ROOT
    paths = [
        "methodology/docs/discovery/ai-research-skills-20260810/p3-design.md",
        "methodology/tests/test_ai_coding_video_benchmark.py",
        "methodology/tests/test_ai_research_contract.py",
    ]
    bundle = run_nit.build_evidence_bundle(paths, 70000)
    entries = {entry["path"]: entry for entry in json.loads(bundle)}

    assert len(bundle) == 66792
    assert (entries[paths[0]]["char_count"], entries[paths[0]]["sha256"]) == (
        11377,
        "aa1c6b8264516106b4b83a4636d6a35de74d6c768aec5e0fa1c91f57dc4dea9a",
    )
    assert (entries[paths[1]]["char_count"], entries[paths[1]]["sha256"]) == (
        44308,
        "9b400b61dd3a0617b5eb50ba5f213fe11916d4680d0501c3cea67316d4a9cf36",
    )
    assert (entries[paths[2]]["char_count"], entries[paths[2]]["sha256"]) == (
        6836,
        "4256a0a333ae365c2c75b5e4aab6b653048582fd6035c526ccc5077e55621bc3",
    )


def test_mock_fixture_marker_keeps_clean_and_finding_branches() -> None:
    marker = _fixture_finding_marker()

    assert run_nit._extract_status(run_nit.mock_review("a.py", "+value = 2")) == "ALL PASS"
    assert run_nit._extract_status(run_nit.mock_review("a.py", f"+{marker} = 1")) == (
        "CHANGES_REQUESTED"
    )


def test_diff_truncation_blocks_without_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    run_nit.REPO = repo
    (repo / "a.py").write_text("value = '" + "x" * 100 + "'\n", encoding="utf-8")
    monkeypatch.setattr(run_nit, "call_ollama", lambda *args: pytest.fail("provider called"))

    result = run_nit.review_file(
        "a.py",
        _config(max_diff_chars=10),
        provider="ollama",
        staged=False,
    )

    assert (result.status, result.transport) == ("BLOCKED", "NOT_CALLED")
    assert "DIFF_TRUNCATED" in result.output


def test_timeout_override_is_exact_deduplicated_and_target_bound(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    run_nit.REPO = repo
    targets = ["a.py", "b.py"]

    assert run_nit.parse_file_timeouts(["a.py=300"], targets) == {"a.py": 300}
    with pytest.raises(run_nit.PreflightError, match="duplicate"):
        run_nit.parse_file_timeouts(["a.py=3", "./a.py=4"], targets)
    with pytest.raises(run_nit.PreflightError, match="not an exact target"):
        run_nit.parse_file_timeouts(["b.py=3"], ["a.py"])


@pytest.mark.parametrize("timeout_exc", [TimeoutError(), urllib.error.URLError(TimeoutError())])
def test_typed_timeout_classification(
    monkeypatch: pytest.MonkeyPatch,
    timeout_exc: Exception,
) -> None:
    def fail(request, timeout):
        raise timeout_exc

    monkeypatch.setattr(run_nit.urllib.request, "urlopen", fail)
    with pytest.raises(run_nit.ProviderTimeout):
        run_nit.call_ollama(_config(), "prompt")


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.HTTPError("http://x", 500, "bad", None, None),
        urllib.error.URLError(ConnectionRefusedError()),
        ConnectionRefusedError(),
    ],
)
def test_non_timeout_transport_classification(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    def fail(request, timeout):
        raise failure

    monkeypatch.setattr(run_nit.urllib.request, "urlopen", fail)
    with pytest.raises(run_nit.ProviderError):
        run_nit.call_ollama(_config(), "prompt")


def test_observation_has_exact_keys() -> None:
    result = run_nit.ReviewResult("BLOCKED", "BLOCKED", "TIMEOUT", 123)
    line = run_nit._observation("a.py", result, 300)
    payload = json.loads(line.removeprefix("Nitpicker-Observation: "))

    assert "\n" not in line
    assert set(payload) == {"path", "status", "timeout_seconds", "elapsed_ms", "transport"}
    assert payload["transport"] == "TIMEOUT"


def test_source_mode_subprocess_contract(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    marker = _fixture_finding_marker()
    (repo / "a.py").write_text(f"{marker} = 1\n", encoding="utf-8")
    _git(repo, "add", "a.py")
    (repo / "a.py").write_text("value = 2\n", encoding="utf-8")
    (repo / "b.py").write_text(f"{marker} = 1\n", encoding="utf-8")

    explicit_changed = _run(repo, "--provider", "mock", "--changed", "a.py")
    explicit_staged = _run(repo, "--provider", "mock", "--staged", "a.py")
    discovered_staged = _run(repo, "--provider", "mock", "--staged")
    discovered_changed = _run(repo, "--provider", "mock")
    conflict = _run(repo, "--provider", "mock", "--changed", "--staged", "a.py")

    assert explicit_changed.returncode == 0
    assert "===== b.py =====" not in explicit_changed.stdout
    assert explicit_staged.returncode == 2
    assert "===== b.py =====" not in explicit_staged.stdout
    assert discovered_staged.returncode == 2
    assert "===== b.py =====" not in discovered_staged.stdout
    assert discovered_changed.returncode == 2
    assert "===== b.py =====" in discovered_changed.stdout
    assert conflict.returncode == 3
    assert "Mock provider checked" not in conflict.stdout


def test_explicit_staged_does_not_synthesize_unstaged_bytes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    marker = _fixture_finding_marker()
    (repo / "a.py").write_text(f"{marker} = 1\n", encoding="utf-8")

    result = _run(repo, "--provider", "mock", "--staged", "a.py")

    assert result.returncode == 0
    assert marker not in result.stdout
    assert '"transport":"NOT_CALLED"' in result.stdout
    assert _last_line(result.stdout) == "Nitpicker: ALL PASS"


@pytest.mark.parametrize(
    "args",
    [
        ["--self-test", "--changed", "--staged"],
        ["--self-test", "--evidence-file", r"C:foo"],
        ["--self-test", r"C:foo"],
        ["--self-test", "--file-timeout", r"C:foo=3", "a.py"],
        ["--self-test", "--file-timeout", "b.py=3", "a.py"],
    ],
)
def test_self_test_cannot_bypass_preflight(tmp_path: Path, args: list[str]) -> None:
    result = _run(_repo(tmp_path), *args)

    assert result.returncode == 3
    assert result.stderr == ""
    assert _last_line(result.stdout) == "Nitpicker: BLOCKED"


PARSER_FAILURES = [
    ["--unknown"],
    ["--repo"],
    ["--model"],
    ["--provider"],
    ["--provider", "bad"],
    ["--max-evidence-chars", "x"],
    ["--max-evidence-chars", "0"],
    ["--max-evidence-chars", "-1"],
    ["--file-timeout", "a.py"],
    ["--file-timeout", "a.py=0", "a.py"],
]


@pytest.mark.parametrize("args", PARSER_FAILURES)
def test_parser_failures_are_structured_blocked(tmp_path: Path, args: list[str]) -> None:
    result = _run(_repo(tmp_path), *args)

    assert result.returncode == 3
    assert result.stderr == ""
    assert "Traceback" not in result.stdout
    assert "usage:" not in result.stdout.lower()
    assert _last_line(result.stdout) == "Nitpicker: BLOCKED"


def test_help_is_summary_free_success(tmp_path: Path) -> None:
    result = _run(_repo(tmp_path), "--help")

    assert result.returncode == 0
    assert "usage:" in result.stdout.lower()
    assert "Nitpicker:" not in result.stdout
    assert result.stderr == ""


CANONICAL_EVIDENCE = [
    "methodology/docs/discovery/ai-research-skills-20260810/p3-design.md",
    "methodology/tests/test_ai_coding_video_benchmark.py",
    "methodology/tests/test_ai_research_contract.py",
]
CANONICAL_TARGETS = [
    "methodology/plugins/ai-research/skills/ai-coding-video-benchmark/SKILL.md",
    "methodology/plugins/ai-research/skills/ai-coding-video-benchmark/scripts/video_gate.py",
    "methodology/plugins/ai-research/schemas/video_result.schema.json",
    "methodology/tests/test_ai_coding_video_benchmark.py",
    "methodology/tests/test_ai_research_contract.py",
]


def _canonical_envelope_command(fake_run_nit: Path) -> list[str]:
    child = [
        sys.executable,
        str(fake_run_nit),
        "--model",
        "qwen3:8b",
        "--keep-going",
        "--max-evidence-chars",
        "70000",
    ]
    for evidence in CANONICAL_EVIDENCE:
        child.extend(["--evidence-file", evidence])
    for target in CANONICAL_TARGETS:
        child.extend(["--file-timeout", f"{target}=300"])
    child.extend(CANONICAL_TARGETS)
    return [
        sys.executable,
        str(ENVELOPE),
        "--backend",
        "nitpicker",
        "--model",
        "qwen3:8b",
        "--style",
        "runnit",
        "--timeout",
        "1800",
        "--",
        *child,
    ]


@pytest.mark.parametrize(
    ("child_exit", "expected_status", "expected_exit"),
    [(0, "PASS", 0), (2, "CHANGES_REQUESTED", 1), (3, "BLOCKED", 2)],
)
def test_canonical_envelope_argv_and_fake_child_runnit_mapping(
    tmp_path: Path,
    child_exit: int,
    expected_status: str,
    expected_exit: int,
) -> None:
    fake_run_nit = tmp_path / "run_nit.py"
    fake_run_nit.write_text(
        "import json, os, sys\n"
        "print(json.dumps(sys.argv[1:]))\n"
        "raise SystemExit(int(os.environ['FAKE_RUN_NIT_EXIT']))\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["FAKE_RUN_NIT_EXIT"] = str(child_exit)

    completed = subprocess.run(
        _canonical_envelope_command(fake_run_nit),
        check=False,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    envelope = json.loads(completed.stdout)
    received_child_argv = json.loads(envelope["stdout"])

    assert (envelope["status"], envelope["exit_code"], completed.returncode) == (
        expected_status,
        expected_exit,
        expected_exit,
    )
    assert received_child_argv == _canonical_envelope_command(fake_run_nit)[13:]
    assert received_child_argv.count("--evidence-file") == 3
    assert received_child_argv.count("--file-timeout") == 5
    assert received_child_argv[-5:] == CANONICAL_TARGETS
