"""Deterministic tests for the execution binding preflight."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "execution_preflight.py"
SPEC = importlib.util.spec_from_file_location("execution_preflight_under_test", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
preflight = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = preflight
SPEC.loader.exec_module(preflight)


class FakeRunner:
    """Capture every subprocess and provide controlled git/provider results."""

    def __init__(
        self,
        repo_root: Path,
        provider_results: list[subprocess.CompletedProcess[str] | BaseException] | None = None,
        *,
        git_root: Path | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.git_root = git_root or repo_root
        self.provider_results = list(provider_results or [])
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), dict(kwargs)))
        if argv[0] == "git":
            return subprocess.CompletedProcess(argv, 0, f"{self.git_root}\n", "")
        if not self.provider_results:
            raise AssertionError(f"unexpected provider command: {argv}")
        result = self.provider_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    @property
    def provider_calls(self) -> list[list[str]]:
        return [argv for argv, _ in self.calls if argv[0] != "git"]


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _base_binding(repo: Path) -> dict[str, Any]:
    codex = repo / "bin" / "codex.exe"
    claude = repo / "bin" / "claude.exe"
    return {
        "schema_version": 1,
        "timeout_seconds": 12,
        "repo_root": str(repo),
        "mechanical": {
            "interpreter": "bin/python.exe",
            "entrypoint": "tools/direct_nit.py",
            "args": [
                "--repo",
                str(repo),
                "--provider",
                "ollama",
                "--model",
                "qwen2.5-coder:7b",
                "--include-all",
            ],
            "process_mode": "direct",
            "daemon": False,
            "visible_console": False,
            "start_process": False,
            "provider": "ollama",
            "model": "qwen2.5-coder:7b",
            "working_directory": ".",
            "artifact_root": "artifacts",
            "artifact_probe_file": "artifacts/review.md",
        },
        "providers": [
            {
                "id": "codex",
                "executable": str(codex),
                "version_expected_substring": "codex-cli 0.144.1",
                "model": "gpt-5.6-sol",
                "model_binding_kind": "exact_pin",
                "planned_argv": [
                    str(codex),
                    "exec",
                    "--json",
                    "--ignore-user-config",
                    "--dangerously-bypass-approvals-and-sandbox",
                    "-m",
                    "gpt-5.6-sol",
                    "-c",
                    'model_reasoning_effort="high"',
                    "--cd",
                    str(repo),
                    "-",
                ],
                "working_directory": ".",
                "artifact_root": "artifacts",
                "artifact_probe_file": "artifacts/review.md",
                "approval_policy": "dogfood-bypass",
                "sandbox_policy": "danger-full-access",
                "dangerous_bypass_required_for_dogfood": True,
                "required_env": [],
            },
            {
                "id": "claude",
                "executable": str(claude),
                "version_expected_substring": "2.1.206 (Claude Code)",
                "model": "sonnet",
                "model_binding_kind": "moving_profile",
                "planned_argv": [
                    str(claude),
                    "-p",
                    "--model",
                    "sonnet",
                    "--effort",
                    "medium",
                    "--no-session-persistence",
                ],
                "working_directory": ".",
                "artifact_root": "artifacts",
                "artifact_probe_file": "artifacts/review.md",
                "approval_policy": "not-applicable",
                "sandbox_policy": "external",
                "dangerous_bypass_required_for_dogfood": False,
                "required_env": [],
            },
        ],
    }


def _repo(tmp_path: Path, name: str = "repo") -> tuple[Path, Path, dict[str, Any]]:
    repo = tmp_path / name
    (repo / "config").mkdir(parents=True)
    (repo / "bin").mkdir()
    (repo / "tools").mkdir()
    (repo / "artifacts").mkdir()
    for executable in ("python.exe", "codex.exe", "claude.exe"):
        (repo / "bin" / executable).write_text("fixture executable\n", encoding="utf-8")
    (repo / "tools" / "direct_nit.py").write_text("print('not executed')\n", encoding="utf-8")
    (repo / "artifacts" / "review.md").write_text("검토 artifact\n", encoding="utf-8")
    config = _base_binding(repo)
    path = repo / "config" / "project.json"
    _write(path, config)
    return repo, path, config


def _write(path: Path, config: dict[str, Any]) -> None:
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def _execute(
    config_path: Path,
    runner: FakeRunner,
    *,
    live: bool = False,
    environ: dict[str, str] | None = None,
) -> tuple[dict[str, Any], int]:
    argv = ["--project-config", str(config_path)]
    if live:
        argv.append("--live")
    return preflight.execute(argv, runner=runner, environ=environ or {})


def test_direct_python_valid_providers_deterministic_pass(tmp_path: Path) -> None:
    repo, path, _ = _repo(tmp_path)
    runner = FakeRunner(repo)

    payload, exit_code = _execute(path, runner)

    assert exit_code == 0
    assert payload["status"] == "PASS"
    assert payload["binding_source"] == "project_exact_binding"
    assert payload["mechanical"]["process_mode"] == "direct"
    assert payload["mechanical"]["host_artifact_readable"] is True
    assert payload["mechanical"]["child_process_artifact_access"] == "NOT_CLAIMED"
    assert [item["id"] for item in payload["providers"]] == ["codex", "claude"]
    assert payload["providers"][0]["model_binding_kind"] == "exact_pin"
    assert payload["providers"][1]["model_binding_kind"] == "moving_profile"
    assert payload["providers"][1]["provider_process_artifact_access"] == "NOT_CLAIMED"
    assert runner.provider_calls == []
    assert len(runner.calls) == 1


@pytest.mark.parametrize("flag", ["daemon", "visible_console", "start_process"])
def test_process_flags_block_before_any_runner(tmp_path: Path, flag: str) -> None:
    repo, path, config = _repo(tmp_path)
    config["mechanical"][flag] = True
    _write(path, config)
    runner = FakeRunner(repo)

    payload, exit_code = _execute(path, runner)

    assert (payload["status"], exit_code) == ("BLOCKED", 2)
    assert runner.calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("args", ["--provider", "ollama", "--model", "qwen2.5-coder:7b", "Start-Process"]),
        ("args", ["--provider", "ollama", "--model", "qwen2.5-coder:7b", "--watch"]),
        ("args", ["--provider", "ollama", "--model", "qwen2.5-coder:7b", "--daemon=true"]),
        ("args", ["--provider", "ollama", "--model", "qwen2.5-coder:7b", "background"]),
        ("interpreter", "cmd.exe"),
        ("interpreter", "bin/launcher.cmd"),
        ("entrypoint", "tools/review.bat"),
    ],
)
def test_mechanical_launchers_block_before_runner(
    tmp_path: Path, field: str, value: Any
) -> None:
    repo, path, config = _repo(tmp_path)
    config["mechanical"][field] = value
    _write(path, config)
    runner = FakeRunner(repo)

    payload, exit_code = _execute(path, runner)

    assert (payload["status"], exit_code) == ("BLOCKED", 2)
    assert runner.calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("interpreter", "bin/missing-python.exe"),
        ("entrypoint", "tools/missing.py"),
        ("working_directory", "missing-cwd"),
        ("artifact_root", "missing-artifacts"),
    ],
)
def test_missing_mechanical_paths_block_before_runner(
    tmp_path: Path, field: str, value: str
) -> None:
    repo, path, config = _repo(tmp_path)
    config["mechanical"][field] = value
    _write(path, config)
    runner = FakeRunner(repo)

    payload, exit_code = _execute(path, runner)

    assert (payload["status"], exit_code) == ("BLOCKED", 2)
    assert runner.calls == []


@pytest.mark.parametrize(
    "args",
    [
        ["--provider", "ollama"],
        ["--provider", "ollama", "-m", "qwen2.5-coder:7b"],
        ["--provider", "ollama", "--model=qwen2.5-coder:7b"],
        ["--provider", "ollama", "--model", "decoy", "qwen2.5-coder:7b"],
        ["--provider", "ollama", "--model", "qwen2.5-coder:7b", "--model", "other"],
    ],
)
def test_mechanical_model_binding_is_adjacent_unique_exact(
    tmp_path: Path, args: list[str]
) -> None:
    repo, path, config = _repo(tmp_path)
    config["mechanical"]["args"] = args
    _write(path, config)
    runner = FakeRunner(repo)

    payload, exit_code = _execute(path, runner)

    assert (payload["status"], exit_code) == ("BLOCKED", 2)
    assert runner.calls == []


@pytest.mark.parametrize(
    "args",
    [
        ["--model", "qwen2.5-coder:7b"],
        ["-p", "ollama", "--model", "qwen2.5-coder:7b"],
        ["--provider=ollama", "--model", "qwen2.5-coder:7b"],
        ["--provider", "other", "--model", "qwen2.5-coder:7b"],
        [
            "--provider",
            "ollama",
            "--provider",
            "ollama",
            "--model",
            "qwen2.5-coder:7b",
        ],
    ],
)
def test_mechanical_provider_binding_is_adjacent_unique_exact(
    tmp_path: Path, args: list[str]
) -> None:
    repo, path, config = _repo(tmp_path)
    config["mechanical"]["args"] = args
    _write(path, config)
    runner = FakeRunner(repo)

    payload, exit_code = _execute(path, runner)

    assert (payload["status"], exit_code) == ("BLOCKED", 2)
    assert runner.calls == []


def test_exact_mechanical_argv_capture(tmp_path: Path) -> None:
    repo, path, config = _repo(tmp_path)
    runner = FakeRunner(repo)

    payload, exit_code = _execute(path, runner)

    assert exit_code == 0
    assert payload["mechanical"]["exact_argv"] == [
        str((repo / "bin" / "python.exe").resolve()),
        str((repo / "tools" / "direct_nit.py").resolve()),
        *config["mechanical"]["args"],
    ]


def test_server_substring_in_python_filename_is_not_blocked(tmp_path: Path) -> None:
    repo, path, config = _repo(tmp_path)
    entrypoint = repo / "tools" / "observer_server_review.py"
    entrypoint.write_text("print('not executed')\n", encoding="utf-8")
    config["mechanical"]["entrypoint"] = "tools/observer_server_review.py"
    _write(path, config)

    payload, exit_code = _execute(path, FakeRunner(repo))

    assert exit_code == 0
    assert payload["status"] == "PASS"


def _successful_live_results() -> list[subprocess.CompletedProcess[str]]:
    return [
        _completed(stdout="codex-cli 0.144.1\n"),
        _completed(stdout="Logged in as private@example.invalid\n"),
        _completed(stdout="2.1.206 (Claude Code)\n"),
        _completed(stdout='{"email":"another@example.invalid","token":"secret"}\n'),
    ]


def test_live_runs_only_allowlisted_probes_and_redacts_auth_body(tmp_path: Path) -> None:
    repo, path, _ = _repo(tmp_path)
    runner = FakeRunner(repo, _successful_live_results())

    payload, exit_code = _execute(path, runner, live=True)
    serialized = json.dumps(payload)

    assert exit_code == 0
    assert payload["status"] == "PASS"
    assert runner.provider_calls == [
        [str((repo / "bin" / "codex.exe").resolve()), "--version"],
        [str((repo / "bin" / "codex.exe").resolve()), "login", "status"],
        [str((repo / "bin" / "claude.exe").resolve()), "--version"],
        [str((repo / "bin" / "claude.exe").resolve()), "auth", "status"],
    ]
    assert "private@example.invalid" not in serialized
    assert "another@example.invalid" not in serialized
    assert "secret" not in serialized
    for _, kwargs in runner.calls:
        assert kwargs["shell"] is False
        assert kwargs["timeout"] == 12
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        assert kwargs["capture_output"] is True
        assert kwargs["check"] is False


@pytest.mark.parametrize(
    ("first_result", "expected_provider_calls"),
    [
        (_completed(returncode=1, stderr="version failed"), 1),
        (_completed(stdout="wrong version"), 1),
        (subprocess.TimeoutExpired(["codex", "--version"], 12), 1),
    ],
)
def test_version_failure_blocks_and_stops_later_probes(
    tmp_path: Path,
    first_result: subprocess.CompletedProcess[str] | BaseException,
    expected_provider_calls: int,
) -> None:
    repo, path, _ = _repo(tmp_path)
    runner = FakeRunner(repo, [first_result, *_successful_live_results()])

    payload, exit_code = _execute(path, runner, live=True)

    assert (payload["status"], exit_code) == ("BLOCKED", 2)
    assert len(runner.provider_calls) == expected_provider_calls


def test_auth_nonzero_blocks_before_next_provider(tmp_path: Path) -> None:
    repo, path, _ = _repo(tmp_path)
    runner = FakeRunner(
        repo,
        [
            _completed(stdout="codex-cli 0.144.1"),
            _completed(returncode=1, stdout="not logged in"),
            *_successful_live_results(),
        ],
    )

    payload, exit_code = _execute(path, runner, live=True)

    assert (payload["status"], exit_code) == ("BLOCKED", 2)
    assert len(runner.provider_calls) == 2


@pytest.mark.parametrize(
    "mutation",
    [
        lambda item: item["planned_argv"].__setitem__(6, "different-model"),
        lambda item: item["planned_argv"].remove("--ignore-user-config"),
        lambda item: item["planned_argv"].remove('model_reasoning_effort="high"'),
        lambda item: item["planned_argv"].__setitem__(1, "review"),
        lambda item: item["planned_argv"].extend(["-c", 'model_reasoning_effort="high"']),
    ],
)
def test_codex_grammar_failure_blocks_provider_probe_zero(
    tmp_path: Path, mutation: Any
) -> None:
    repo, path, config = _repo(tmp_path)
    mutation(config["providers"][0])
    _write(path, config)
    runner = FakeRunner(repo)

    payload, exit_code = _execute(path, runner, live=True)

    assert (payload["status"], exit_code) == ("BLOCKED", 2)
    assert runner.provider_calls == []
    assert runner.calls == []


@pytest.mark.parametrize(
    "mutation",
    [
        lambda item: item["planned_argv"].remove("-p"),
        lambda item: item["planned_argv"].__setitem__(2, "-m"),
        lambda item: item["planned_argv"].__setitem__(3, "opus"),
        lambda item: item["planned_argv"].remove("--no-session-persistence"),
        lambda item: item["planned_argv"].__setitem__(5, "high"),
    ],
)
def test_claude_grammar_failure_blocks_provider_probe_zero(
    tmp_path: Path, mutation: Any
) -> None:
    repo, path, config = _repo(tmp_path)
    mutation(config["providers"][1])
    _write(path, config)
    runner = FakeRunner(repo)

    payload, exit_code = _execute(path, runner, live=True)

    assert (payload["status"], exit_code) == ("BLOCKED", 2)
    assert runner.provider_calls == []
    assert runner.calls == []


def test_claude_model_equal_form_is_allowed(tmp_path: Path) -> None:
    repo, path, config = _repo(tmp_path)
    claude_argv = config["providers"][1]["planned_argv"]
    model_index = claude_argv.index("--model")
    claude_argv[model_index : model_index + 2] = ["--model=sonnet"]
    _write(path, config)

    payload, exit_code = _execute(path, FakeRunner(repo))

    assert exit_code == 0
    assert payload["providers"][1]["model_binding_kind"] == "moving_profile"


@pytest.mark.parametrize(
    "token",
    [
        "logout",
        "install",
        "update",
        "plugin",
        "https://example.invalid/plugin",
        "--background",
        "worktree",
        "tmux",
        "--permission-mode",
        "--tools",
        "--add-dir",
    ],
)
def test_forbidden_provider_command_blocks_before_runner(tmp_path: Path, token: str) -> None:
    repo, path, config = _repo(tmp_path)
    config["providers"][1]["planned_argv"].append(token)
    _write(path, config)
    runner = FakeRunner(repo)

    payload, exit_code = _execute(path, runner, live=True)

    assert (payload["status"], exit_code) == ("BLOCKED", 2)
    assert runner.calls == []


@pytest.mark.parametrize(
    "field",
    ["version_probe_argv", "auth_probe_argv", "command", "binding_source"],
)
def test_unknown_command_or_selector_claim_field_is_rejected(
    tmp_path: Path, field: str
) -> None:
    repo, path, config = _repo(tmp_path)
    if field == "binding_source":
        config[field] = "project_exact_binding"
    else:
        config["providers"][0][field] = ["codex", "logout"]
    _write(path, config)
    runner = FakeRunner(repo)

    payload, exit_code = _execute(path, runner, live=True)

    assert (payload["status"], exit_code) == ("BLOCKED", 2)
    assert runner.calls == []


def test_selector_project_wins_over_wrapper(tmp_path: Path) -> None:
    repo, project, config = _repo(tmp_path)
    wrapper = repo / "config" / "wrapper.json"
    _write(wrapper, config)
    runner = FakeRunner(repo)

    payload, exit_code = preflight.execute(
        [
            "--project-config",
            str(project),
            "--repo-wrapper-config",
            str(wrapper),
        ],
        runner=runner,
        environ={},
    )

    assert exit_code == 0
    assert payload["config_path"] == str(project.resolve())
    assert payload["binding_source"] == "project_exact_binding"


def test_selector_uses_wrapper_only_when_project_is_absent(tmp_path: Path) -> None:
    repo, project, config = _repo(tmp_path)
    wrapper = repo / "config" / "wrapper.json"
    _write(wrapper, config)
    project.unlink()
    runner = FakeRunner(repo)

    payload, exit_code = preflight.execute(
        [
            "--project-config",
            str(project),
            "--repo-wrapper-config",
            str(wrapper),
        ],
        runner=runner,
        environ={},
    )

    assert exit_code == 0
    assert payload["config_path"] == str(wrapper.resolve())
    assert payload["binding_source"] == "repo_wrapper"


def test_malformed_project_does_not_fall_back_to_wrapper(tmp_path: Path) -> None:
    repo, project, config = _repo(tmp_path)
    wrapper = repo / "config" / "wrapper.json"
    _write(wrapper, config)
    project.write_text("{malformed", encoding="utf-8")
    runner = FakeRunner(repo)

    payload, exit_code = preflight.execute(
        [
            "--project-config",
            str(project),
            "--repo-wrapper-config",
            str(wrapper),
        ],
        runner=runner,
        environ={},
    )

    assert (payload["status"], exit_code) == ("BLOCKED", 2)
    assert payload["config_path"] == str(project.resolve())
    assert runner.calls == []


def test_selector_blocks_when_both_candidates_are_absent(tmp_path: Path) -> None:
    runner = FakeRunner(tmp_path)

    payload, exit_code = preflight.execute(
        [
            "--project-config",
            str(tmp_path / "missing-project.json"),
            "--repo-wrapper-config",
            str(tmp_path / "missing-wrapper.json"),
        ],
        runner=runner,
        environ={},
    )

    assert (payload["status"], exit_code) == ("BLOCKED", 2)
    assert runner.calls == []


@pytest.mark.parametrize("path_arg", ["project.json", ".\\project.json"])
def test_relative_config_argument_is_blocked(tmp_path: Path, path_arg: str) -> None:
    runner = FakeRunner(tmp_path)

    payload, exit_code = preflight.execute(
        ["--project-config", path_arg], runner=runner, environ={}
    )

    assert (payload["status"], exit_code) == ("BLOCKED", 2)
    assert runner.calls == []


def test_config_outside_declared_repo_is_blocked(tmp_path: Path) -> None:
    repo, _, config = _repo(tmp_path)
    outside = tmp_path / "outside.json"
    _write(outside, config)
    runner = FakeRunner(repo)

    payload, exit_code = _execute(outside, runner)

    assert (payload["status"], exit_code) == ("BLOCKED", 2)
    assert runner.calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("entrypoint", "../outside.py"),
        ("working_directory", "../repo-sibling"),
        ("artifact_root", "../repo-other"),
    ],
)
def test_dotdot_and_sibling_prefix_escape_are_blocked(
    tmp_path: Path, field: str, value: str
) -> None:
    repo, path, config = _repo(tmp_path, "repo")
    (tmp_path / "outside.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "repo-sibling").mkdir()
    (tmp_path / "repo-other").mkdir()
    config["mechanical"][field] = value
    _write(path, config)
    runner = FakeRunner(repo)

    payload, exit_code = _execute(path, runner)

    assert (payload["status"], exit_code) == ("BLOCKED", 2)
    assert runner.calls == []


def test_symlink_escape_is_blocked_when_supported(tmp_path: Path) -> None:
    repo, path, config = _repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "escape.py").write_text("pass\n", encoding="utf-8")
    link = repo / "linked"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError as exc:
        if os.name != "nt":
            pytest.skip(f"symlink creation is unavailable: {exc}")
        junction = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside)],
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
        if junction.returncode != 0:
            pytest.fail(f"cannot create symlink or junction fixture: {junction.stderr}")
    config["mechanical"]["entrypoint"] = "linked/escape.py"
    _write(path, config)
    runner = FakeRunner(repo)

    payload, exit_code = _execute(path, runner)

    assert (payload["status"], exit_code) == ("BLOCKED", 2)
    assert runner.calls == []


def test_git_top_level_mismatch_blocks_after_safe_git_probe(tmp_path: Path) -> None:
    repo, path, config = _repo(tmp_path)
    config["providers"][0]["required_env"] = ["PREFLIGHT_SECRET"]
    _write(path, config)
    other = tmp_path / "other-git-root"
    other.mkdir()
    runner = FakeRunner(repo, git_root=other)

    payload, exit_code = _execute(path, runner)

    assert (payload["status"], exit_code) == ("BLOCKED", 2)
    assert len(runner.calls) == 1
    assert runner.calls[0][0][0] == "git"
    assert payload["commands_executed"][0]["purpose"] == "git_root"
    assert payload["providers"][0]["required_env_present"] is False
    assert payload["providers"][1]["required_env_present"] is True


@pytest.mark.parametrize(
    "mutation",
    [
        lambda config: config.__setitem__("unknown", True),
        lambda config: config.__setitem__("schema_version", "1"),
        lambda config: config.__setitem__("timeout_seconds", 0),
        lambda config: config.__setitem__("timeout_seconds", 61),
        lambda config: config["mechanical"].__setitem__("args", []),
        lambda config: config["mechanical"].__setitem__("daemon", 0),
        lambda config: config["providers"].append(copy.deepcopy(config["providers"][0])),
        lambda config: config["providers"][0].__setitem__("required_env", ["TOKEN", "token"]),
        lambda config: config["providers"][0].__setitem__("required_env", ["BAD-NAME"]),
    ],
)
def test_strict_schema_type_uniqueness_and_timeout_block_before_runner(
    tmp_path: Path, mutation: Any
) -> None:
    repo, path, config = _repo(tmp_path)
    mutation(config)
    _write(path, config)
    runner = FakeRunner(repo)

    payload, exit_code = _execute(path, runner)

    assert (payload["status"], exit_code) == ("BLOCKED", 2)
    assert runner.calls == []


def test_duplicate_json_key_is_rejected_at_decode_time(tmp_path: Path) -> None:
    repo, path, config = _repo(tmp_path)
    text = json.dumps(config)
    path.write_text(text.replace('{"schema_version": 1', '{"schema_version": 1, "schema_version": 1', 1), encoding="utf-8")
    runner = FakeRunner(repo)

    payload, exit_code = _execute(path, runner)

    assert (payload["status"], exit_code) == ("BLOCKED", 2)
    assert "duplicate JSON key" in payload["checks"][-1]["detail"]
    assert runner.calls == []


@pytest.mark.parametrize(
    ("provider_index", "updates"),
    [
        (0, {"approval_policy": "never"}),
        (0, {"sandbox_policy": "workspace-write"}),
        (0, {"dangerous_bypass_required_for_dogfood": False}),
        (1, {"approval_policy": "acceptEdits"}),
        (1, {"sandbox_policy": "danger-full-access"}),
        (1, {"dangerous_bypass_required_for_dogfood": True}),
    ],
)
def test_only_supported_provider_policy_combinations_are_allowed(
    tmp_path: Path, provider_index: int, updates: dict[str, Any]
) -> None:
    repo, path, config = _repo(tmp_path)
    config["providers"][provider_index].update(updates)
    _write(path, config)
    runner = FakeRunner(repo)

    payload, exit_code = _execute(path, runner, live=True)

    assert (payload["status"], exit_code) == ("BLOCKED", 2)
    assert runner.calls == []


def test_missing_required_env_is_false_and_blocks_before_live_probes(
    tmp_path: Path,
) -> None:
    repo, path, config = _repo(tmp_path)
    config["providers"][0]["required_env"] = ["PREFLIGHT_SECRET"]
    _write(path, config)
    runner = FakeRunner(repo)

    payload, exit_code = _execute(path, runner, live=True, environ={})

    assert (payload["status"], exit_code) == ("BLOCKED", 2)
    assert payload["providers"][0]["required_env_present"] is False
    assert payload["providers"][1]["required_env_present"] is True
    assert runner.provider_calls == []
    assert len(runner.calls) == 1


def test_present_required_env_is_true_without_outputting_value(tmp_path: Path) -> None:
    repo, path, config = _repo(tmp_path)
    config["providers"][0]["required_env"] = ["PREFLIGHT_SECRET"]
    _write(path, config)
    runner = FakeRunner(repo)

    payload, exit_code = _execute(
        path, runner, environ={"preflight_secret": "do-not-print-this"}
    )

    serialized = json.dumps(payload)
    assert (payload["status"], exit_code) == ("PASS", 0)
    assert payload["providers"][0]["required_env_present"] is True
    assert "do-not-print-this" not in serialized


@pytest.mark.parametrize("probe_value", ["artifacts/missing.md", "tools/direct_nit.py"])
def test_artifact_probe_must_exist_inside_artifact_root(
    tmp_path: Path, probe_value: str
) -> None:
    repo, path, config = _repo(tmp_path)
    config["providers"][0]["artifact_probe_file"] = probe_value
    _write(path, config)
    runner = FakeRunner(repo)

    payload, exit_code = _execute(path, runner)

    assert (payload["status"], exit_code) == ("BLOCKED", 2)
    assert runner.calls == []


def test_artifact_probe_is_bounded_and_strict_utf8(tmp_path: Path) -> None:
    repo, path, _ = _repo(tmp_path)
    probe = repo / "artifacts" / "review.md"
    probe.write_bytes(b"\xff\xfe")
    runner = FakeRunner(repo)

    payload, exit_code = _execute(path, runner)

    assert (payload["status"], exit_code) == ("BLOCKED", 2)
    assert runner.calls == []


def test_artifact_probe_larger_than_one_mib_is_blocked(tmp_path: Path) -> None:
    repo, path, _ = _repo(tmp_path)
    (repo / "artifacts" / "review.md").write_text(
        "x" * (1024 * 1024 + 1), encoding="utf-8"
    )
    runner = FakeRunner(repo)

    payload, exit_code = _execute(path, runner)

    assert (payload["status"], exit_code) == ("BLOCKED", 2)
    assert runner.calls == []


def test_missing_provider_executable_cwd_and_artifact_block_before_git(
    tmp_path: Path,
) -> None:
    for field, value in (
        ("executable", str(tmp_path / "missing-provider.exe")),
        ("working_directory", "missing-provider-cwd"),
        ("artifact_root", "missing-provider-artifacts"),
    ):
        repo, path, config = _repo(tmp_path, f"repo-{field}")
        config["providers"][0][field] = value
        if field == "executable":
            config["providers"][0]["planned_argv"][0] = value
        _write(path, config)
        runner = FakeRunner(repo)

        payload, exit_code = _execute(path, runner)

        assert (payload["status"], exit_code) == ("BLOCKED", 2)
        assert runner.calls == []


def test_warlords_style_direct_external_entrypoint_fixture_passes(tmp_path: Path) -> None:
    repo, path, config = _repo(tmp_path, "temporary-warlords-style-repo")
    external_style = repo / "external-review" / "run_external.py"
    external_style.parent.mkdir()
    external_style.write_text("print('direct fixture only')\n", encoding="utf-8")
    config["mechanical"]["entrypoint"] = "external-review/run_external.py"
    _write(path, config)
    runner = FakeRunner(repo)

    payload, exit_code = _execute(path, runner)

    assert exit_code == 0
    assert payload["mechanical"]["exact_argv"][1] == str(external_style.resolve())
    assert runner.provider_calls == []


def test_unexpected_runner_exception_is_json_blocked_without_traceback(tmp_path: Path) -> None:
    repo, path, _ = _repo(tmp_path)

    def broken_runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise ValueError("unexpected runner failure")

    payload, exit_code = preflight.execute(
        ["--project-config", str(path)], runner=broken_runner, environ={}
    )
    serialized = json.dumps(payload)

    assert (payload["status"], exit_code) == ("BLOCKED", 2)
    assert "unexpected runner failure" in serialized
    assert "Traceback" not in serialized


def test_cli_stdout_is_exactly_one_utf8_json_line(tmp_path: Path) -> None:
    repo, path, _ = _repo(tmp_path)
    subprocess.run(
        ["git", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )

    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--project-config", str(path)],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="strict",
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert len(completed.stdout.splitlines()) == 1
    assert json.loads(completed.stdout)["status"] == "PASS"


def test_malformed_cli_config_still_emits_one_json_line(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("not-json", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--project-config", str(path)],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="strict",
    )

    assert completed.returncode == 2
    assert completed.stderr == ""
    assert len(completed.stdout.splitlines()) == 1
    payload = json.loads(completed.stdout)
    assert payload["status"] == "BLOCKED"
    assert "Traceback" not in completed.stdout
