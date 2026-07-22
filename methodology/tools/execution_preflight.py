#!/usr/bin/env python3
"""Fail-closed preflight for exact Mechanical and provider bindings.

The command validates one selected JSON binding without running the planned
Mechanical or provider model commands.  Live mode is limited to provider
version and authentication-status probes assembled by this module.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Never


PASS = "PASS"
BLOCKED = "BLOCKED"
NOT_CLAIMED = "NOT_CLAIMED"
MAX_ARTIFACT_BYTES = 1024 * 1024
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

TOP_FIELDS = frozenset(
    {"schema_version", "timeout_seconds", "repo_root", "mechanical", "providers"}
)
MECHANICAL_FIELDS = frozenset(
    {
        "interpreter",
        "entrypoint",
        "args",
        "process_mode",
        "daemon",
        "visible_console",
        "start_process",
        "provider",
        "model",
        "working_directory",
        "artifact_root",
        "artifact_probe_file",
    }
)
PROVIDER_FIELDS = frozenset(
    {
        "id",
        "executable",
        "version_expected_substring",
        "model",
        "model_binding_kind",
        "planned_argv",
        "working_directory",
        "artifact_root",
        "artifact_probe_file",
        "approval_policy",
        "sandbox_policy",
        "dangerous_bypass_required_for_dogfood",
        "required_env",
    }
)

SHELL_NAMES = frozenset({"cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe"})
SHELL_SUFFIXES = frozenset({".bat", ".cmd"})
MECHANICAL_FORBIDDEN_TOKENS = frozenset(
    {
        "start-process",
        "daemon",
        "--daemon",
        "watch",
        "--watch",
        "background",
        "--background",
    }
)
PROVIDER_FORBIDDEN_TOKENS = frozenset(
    {
        "login",
        "logout",
        "plugin",
        "install",
        "update",
        "remove",
        "background",
        "--background",
        "agent",
        "--agent",
        "worktree",
        "--worktree",
        "tmux",
        "--tmux",
        "--permission-mode",
        "--allowedtools",
        "--allowed-tools",
        "--add-dir",
        "--add-directory",
        "--writable-directory",
        "--tools",
    }
)


class PreflightError(RuntimeError):
    """A contract violation that must produce BLOCKED/2."""


class JsonArgumentParser(argparse.ArgumentParser):
    """Keep argparse failures inside the one-line JSON contract."""

    def error(self, message: str) -> Never:
        raise PreflightError(f"invalid arguments: {message}")


@dataclass(frozen=True)
class MechanicalBinding:
    interpreter: str
    entrypoint: str
    args: tuple[str, ...]
    process_mode: str
    daemon: bool
    visible_console: bool
    start_process: bool
    provider: str
    model: str
    working_directory: str
    artifact_root: str
    artifact_probe_file: str


@dataclass(frozen=True)
class ProviderBinding:
    provider_id: str
    executable: str
    version_expected_substring: str
    model: str
    model_binding_kind: str
    planned_argv: tuple[str, ...]
    working_directory: str
    artifact_root: str
    artifact_probe_file: str
    approval_policy: str
    sandbox_policy: str
    dangerous_bypass_required_for_dogfood: bool
    required_env: tuple[str, ...]


@dataclass(frozen=True)
class BindingConfig:
    schema_version: int
    timeout_seconds: int
    repo_root: str
    mechanical: MechanicalBinding
    providers: tuple[ProviderBinding, ...]


@dataclass(frozen=True)
class ResolvedMechanical:
    interpreter: Path
    entrypoint: Path
    working_directory: Path
    artifact_root: Path
    artifact_probe_file: Path
    exact_argv: tuple[str, ...]


@dataclass(frozen=True)
class ResolvedProvider:
    binding: ProviderBinding
    executable: Path
    working_directory: Path
    artifact_root: Path
    artifact_probe_file: Path
    planned_argv: tuple[str, ...]


RunCommand = Callable[..., subprocess.CompletedProcess[str]]
WhichCommand = Callable[[str], str | None]


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PreflightError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _exact_fields(value: Any, expected: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PreflightError(f"{label} must be an object")
    actual = set(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing or unknown:
        raise PreflightError(
            f"{label} fields mismatch: missing={missing}, unknown={unknown}"
        )
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PreflightError(f"{label} must be a non-empty string")
    return value


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise PreflightError(f"{label} must be a boolean")
    return value


def _string_array(value: Any, label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise PreflightError(f"{label} must be an array")
    if not allow_empty and not value:
        raise PreflightError(f"{label} must not be empty")
    result: list[str] = []
    for index, item in enumerate(value):
        result.append(_string(item, f"{label}[{index}]"))
    return tuple(result)


def _parse_config(raw: dict[str, Any]) -> BindingConfig:
    top = _exact_fields(raw, TOP_FIELDS, "config")
    schema_version = top["schema_version"]
    if type(schema_version) is not int or schema_version != 1:
        raise PreflightError("config.schema_version must be integer 1")
    timeout = top["timeout_seconds"]
    if type(timeout) is not int or not 1 <= timeout <= 60:
        raise PreflightError("config.timeout_seconds must be an integer from 1 to 60")

    mechanical_raw = _exact_fields(top["mechanical"], MECHANICAL_FIELDS, "mechanical")
    mechanical = MechanicalBinding(
        interpreter=_string(mechanical_raw["interpreter"], "mechanical.interpreter"),
        entrypoint=_string(mechanical_raw["entrypoint"], "mechanical.entrypoint"),
        args=_string_array(mechanical_raw["args"], "mechanical.args"),
        process_mode=_string(mechanical_raw["process_mode"], "mechanical.process_mode"),
        daemon=_boolean(mechanical_raw["daemon"], "mechanical.daemon"),
        visible_console=_boolean(
            mechanical_raw["visible_console"], "mechanical.visible_console"
        ),
        start_process=_boolean(
            mechanical_raw["start_process"], "mechanical.start_process"
        ),
        provider=_string(mechanical_raw["provider"], "mechanical.provider"),
        model=_string(mechanical_raw["model"], "mechanical.model"),
        working_directory=_string(
            mechanical_raw["working_directory"], "mechanical.working_directory"
        ),
        artifact_root=_string(mechanical_raw["artifact_root"], "mechanical.artifact_root"),
        artifact_probe_file=_string(
            mechanical_raw["artifact_probe_file"], "mechanical.artifact_probe_file"
        ),
    )

    providers_raw = top["providers"]
    if not isinstance(providers_raw, list) or not providers_raw:
        raise PreflightError("config.providers must be a non-empty array")
    providers: list[ProviderBinding] = []
    for index, value in enumerate(providers_raw):
        label = f"providers[{index}]"
        item = _exact_fields(value, PROVIDER_FIELDS, label)
        providers.append(
            ProviderBinding(
                provider_id=_string(item["id"], f"{label}.id"),
                executable=_string(item["executable"], f"{label}.executable"),
                version_expected_substring=_string(
                    item["version_expected_substring"],
                    f"{label}.version_expected_substring",
                ),
                model=_string(item["model"], f"{label}.model"),
                model_binding_kind=_string(
                    item["model_binding_kind"], f"{label}.model_binding_kind"
                ),
                planned_argv=_string_array(item["planned_argv"], f"{label}.planned_argv"),
                working_directory=_string(
                    item["working_directory"], f"{label}.working_directory"
                ),
                artifact_root=_string(item["artifact_root"], f"{label}.artifact_root"),
                artifact_probe_file=_string(
                    item["artifact_probe_file"], f"{label}.artifact_probe_file"
                ),
                approval_policy=_string(
                    item["approval_policy"], f"{label}.approval_policy"
                ),
                sandbox_policy=_string(
                    item["sandbox_policy"], f"{label}.sandbox_policy"
                ),
                dangerous_bypass_required_for_dogfood=_boolean(
                    item["dangerous_bypass_required_for_dogfood"],
                    f"{label}.dangerous_bypass_required_for_dogfood",
                ),
                required_env=_string_array(
                    item["required_env"], f"{label}.required_env", allow_empty=True
                ),
            )
        )

    return BindingConfig(
        schema_version=schema_version,
        timeout_seconds=timeout,
        repo_root=_string(top["repo_root"], "config.repo_root"),
        mechanical=mechanical,
        providers=tuple(providers),
    )


def _candidate(path: Path, label: str) -> Path | None:
    if not path.is_absolute():
        raise PreflightError(f"{label} must be an absolute path")
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        return None
    except (OSError, RuntimeError) as exc:
        raise PreflightError(f"{label} cannot be resolved: {exc}") from None
    if not resolved.is_file():
        raise PreflightError(f"{label} must resolve to a regular file: {resolved}")
    return resolved


def select_config(project: Path | None, wrapper: Path | None) -> tuple[Path, str]:
    """Select project first; an existing malformed project never falls back."""

    if project is None and wrapper is None:
        raise PreflightError("at least one config candidate is required")
    if project is not None and not project.is_absolute():
        raise PreflightError("--project-config must be an absolute path")
    if wrapper is not None and not wrapper.is_absolute():
        raise PreflightError("--repo-wrapper-config must be an absolute path")

    resolved_project = _candidate(project, "--project-config") if project is not None else None
    if resolved_project is not None:
        return resolved_project, "project_exact_binding"
    resolved_wrapper = _candidate(wrapper, "--repo-wrapper-config") if wrapper is not None else None
    if resolved_wrapper is not None:
        return resolved_wrapper, "repo_wrapper"
    raise PreflightError("no config candidate exists")


def load_config(path: Path) -> BindingConfig:
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
        value = json.loads(text, object_pairs_hook=_strict_object)
    except PreflightError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PreflightError(f"config is not strict UTF-8 JSON: {path}: {exc}") from None
    if not isinstance(value, dict):
        raise PreflightError("config root must be a JSON object")
    return _parse_config(value)


def _exact_adjacent_pair(argv: tuple[str, ...], option: str, value: str, label: str) -> None:
    indices = [index for index, token in enumerate(argv) if token == option]
    if len(indices) != 1:
        raise PreflightError(f"{label} must contain {option} VALUE exactly once")
    index = indices[0]
    if index + 1 >= len(argv) or argv[index + 1] != value:
        raise PreflightError(f"{label} has a non-adjacent or mismatched {option} value")


def _validate_mechanical_shape(binding: MechanicalBinding) -> None:
    if binding.process_mode != "direct":
        raise PreflightError("mechanical.process_mode must be 'direct'")
    if binding.daemon or binding.visible_console or binding.start_process:
        raise PreflightError(
            "mechanical daemon, visible_console, and start_process must all be false"
        )
    interpreter_name = Path(binding.interpreter).name.casefold()
    entrypoint_name = Path(binding.entrypoint).name.casefold()
    if (
        interpreter_name in SHELL_NAMES
        or entrypoint_name in SHELL_NAMES
        or Path(binding.interpreter).suffix.casefold() in SHELL_SUFFIXES
        or Path(binding.entrypoint).suffix.casefold() in SHELL_SUFFIXES
    ):
        raise PreflightError("mechanical interpreter/entrypoint cannot be a shell or batch launcher")
    if Path(binding.entrypoint).suffix.casefold() != ".py":
        raise PreflightError("mechanical.entrypoint must have .py suffix")
    lowered = tuple(token.casefold() for token in binding.args)
    forbidden = sorted(
        token
        for token in set(lowered)
        if token in MECHANICAL_FORBIDDEN_TOKENS
        or token.partition("=")[0] in MECHANICAL_FORBIDDEN_TOKENS
    )
    if forbidden:
        raise PreflightError(f"mechanical.args contains forbidden launcher tokens: {forbidden}")
    if any(token == "-m" or token.startswith("--model=") for token in binding.args):
        raise PreflightError("mechanical model binding permits only --model VALUE")
    if any(token == "-p" or token.startswith("--provider=") for token in binding.args):
        raise PreflightError("mechanical provider binding permits only --provider VALUE")
    _exact_adjacent_pair(binding.args, "--model", binding.model, "mechanical.args")
    _exact_adjacent_pair(binding.args, "--provider", binding.provider, "mechanical.args")


def _validate_env_names(provider: ProviderBinding) -> None:
    folded: set[str] = set()
    for name in provider.required_env:
        if ENV_NAME.fullmatch(name) is None:
            raise PreflightError(
                f"provider {provider.provider_id} has invalid required_env name: {name!r}"
            )
        normalized = name.casefold()
        if normalized in folded:
            raise PreflightError(
                f"provider {provider.provider_id} has duplicate required_env name: {name!r}"
            )
        folded.add(normalized)


def _reject_forbidden_provider_tokens(provider: ProviderBinding) -> None:
    for token in provider.planned_argv[1:]:
        lowered = token.casefold()
        if lowered in PROVIDER_FORBIDDEN_TOKENS:
            raise PreflightError(
                f"provider {provider.provider_id} planned_argv contains forbidden token: {token!r}"
            )
        if lowered.startswith(("http://", "https://", "file://")):
            raise PreflightError(
                f"provider {provider.provider_id} planned_argv cannot load a URL: {token!r}"
            )


def _validate_codex_shape(provider: ProviderBinding) -> None:
    argv = provider.planned_argv
    if provider.model != "gpt-5.6-sol" or provider.model_binding_kind != "exact_pin":
        raise PreflightError("codex v1 requires gpt-5.6-sol with exact_pin")
    if (
        provider.approval_policy != "dogfood-bypass"
        or provider.sandbox_policy != "danger-full-access"
        or not provider.dangerous_bypass_required_for_dogfood
    ):
        raise PreflightError("codex v1 requires the explicit dogfood bypass policy tuple")
    if len(argv) < 2 or argv[1] != "exec":
        raise PreflightError("codex planned_argv first command must be exec")
    if any(token == "--model" or token.startswith("--model=") for token in argv):
        raise PreflightError("codex model binding permits only -m VALUE")

    no_value = {
        "--json",
        "--ignore-user-config",
        "--dangerously-bypass-approvals-and-sandbox",
    }
    pair_options = {"-m", "-c", "--cd"}
    index = 2
    while index < len(argv):
        token = argv[index]
        if token in no_value:
            index += 1
        elif token in pair_options:
            if index + 1 >= len(argv):
                raise PreflightError(f"codex option {token} is missing its value")
            index += 2
        elif token == "-" and index == len(argv) - 1:
            index += 1
        else:
            raise PreflightError(f"codex planned_argv contains unknown command/option: {token!r}")

    for token in no_value:
        if argv.count(token) != 1:
            raise PreflightError(f"codex planned_argv must contain {token} exactly once")
    if argv.count("-") != 1 or argv[-1] != "-":
        raise PreflightError("codex planned_argv must end in one stdin marker '-'")
    _exact_adjacent_pair(argv, "-m", provider.model, "codex planned_argv")
    _exact_adjacent_pair(
        argv,
        "-c",
        'model_reasoning_effort="high"',
        "codex planned_argv",
    )
    cd_indices = [index for index, token in enumerate(argv) if token == "--cd"]
    if len(cd_indices) != 1 or cd_indices[0] + 1 >= len(argv):
        raise PreflightError("codex planned_argv must contain --cd REPO_ROOT exactly once")


def _validate_claude_shape(provider: ProviderBinding) -> None:
    argv = provider.planned_argv
    if provider.model != "sonnet" or provider.model_binding_kind != "moving_profile":
        raise PreflightError("claude v1 requires sonnet with moving_profile")
    if (
        provider.approval_policy != "not-applicable"
        or provider.sandbox_policy != "external"
        or provider.dangerous_bypass_required_for_dogfood
    ):
        raise PreflightError("claude v1 requires the read-only external policy tuple")
    if any(token == "-m" for token in argv):
        raise PreflightError("claude model binding does not permit -m")
    print_count = argv.count("-p") + argv.count("--print")
    if print_count != 1:
        raise PreflightError("claude planned_argv requires exactly one -p or --print")

    model_count = 0
    model_value: str | None = None
    index = 1
    while index < len(argv):
        token = argv[index]
        if token in {"-p", "--print", "--no-session-persistence"}:
            index += 1
        elif token in {"--model", "--effort"}:
            if index + 1 >= len(argv):
                raise PreflightError(f"claude option {token} is missing its value")
            if token == "--model":
                model_count += 1
                model_value = argv[index + 1]
            elif argv[index + 1] != "medium":
                raise PreflightError("claude --effort value must be medium")
            index += 2
        elif token.startswith("--model="):
            model_count += 1
            model_value = token.partition("=")[2]
            index += 1
        else:
            raise PreflightError(f"claude planned_argv contains unknown command/option: {token!r}")

    if model_count != 1 or model_value != provider.model:
        raise PreflightError("claude planned_argv requires one exact --model binding")
    if argv.count("--effort") != 1:
        raise PreflightError("claude planned_argv requires --effort medium exactly once")
    if argv.count("--no-session-persistence") != 1:
        raise PreflightError(
            "claude planned_argv requires --no-session-persistence exactly once"
        )


def _validate_provider_shapes(config: BindingConfig) -> None:
    seen: set[str] = set()
    for provider in config.providers:
        if provider.provider_id not in {"codex", "claude"}:
            raise PreflightError(f"unsupported provider id: {provider.provider_id!r}")
        if provider.provider_id in seen:
            raise PreflightError(f"duplicate provider id: {provider.provider_id}")
        seen.add(provider.provider_id)
        _validate_env_names(provider)
        if provider.planned_argv[0] != provider.executable:
            raise PreflightError(
                f"provider {provider.provider_id} planned_argv[0] must equal executable"
            )
        _reject_forbidden_provider_tokens(provider)
        if provider.provider_id == "codex":
            _validate_codex_shape(provider)
        else:
            _validate_claude_shape(provider)


def validate_pure(config: BindingConfig) -> None:
    """Validate schema flags and token grammar before any subprocess."""

    if not Path(config.repo_root).is_absolute():
        raise PreflightError("config.repo_root must be an absolute path")
    _validate_mechanical_shape(config.mechanical)
    _validate_provider_shapes(config)


def _inside(path: Path, root: Path, label: str) -> None:
    try:
        path.relative_to(root)
    except ValueError:
        raise PreflightError(f"{label} escapes its required root: {path}") from None


def _resolve_repo_path(root: Path, value: str, label: str) -> Path:
    candidate = Path(value)
    unresolved = candidate if candidate.is_absolute() else root / candidate
    try:
        resolved = unresolved.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PreflightError(f"{label} cannot be resolved: {unresolved}: {exc}") from None
    _inside(resolved, root, label)
    return resolved


def _require_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise PreflightError(f"{label} must be an existing regular file: {path}")


def _require_directory(path: Path, label: str) -> None:
    if not path.is_dir():
        raise PreflightError(f"{label} must be an existing directory: {path}")


def _read_probe(path: Path, artifact_root: Path, label: str) -> None:
    _inside(path, artifact_root, label)
    _require_file(path, label)
    try:
        size = path.stat().st_size
        if size > MAX_ARTIFACT_BYTES:
            raise PreflightError(f"{label} exceeds {MAX_ARTIFACT_BYTES} bytes")
        path.read_text(encoding="utf-8", errors="strict")
    except PreflightError:
        raise
    except (OSError, UnicodeError) as exc:
        raise PreflightError(f"{label} is not bounded readable UTF-8: {path}: {exc}") from None


def _resolve_executable(value: str, label: str, which: WhichCommand) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise PreflightError(f"{label} cannot be resolved: {candidate}: {exc}") from None
    else:
        found = which(value)
        if found is None:
            raise PreflightError(f"{label} is not available on PATH: {value!r}")
        try:
            resolved = Path(found).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise PreflightError(f"{label} PATH result cannot be resolved: {found}: {exc}") from None
    _require_file(resolved, label)
    return resolved


def resolve_filesystem(
    config_path: Path,
    config: BindingConfig,
    which: WhichCommand,
) -> tuple[Path, ResolvedMechanical, tuple[ResolvedProvider, ...]]:
    """Resolve every path and enforce containment before the git probe."""

    try:
        repo_root = Path(config.repo_root).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PreflightError(f"config.repo_root cannot be resolved: {exc}") from None
    _require_directory(repo_root, "config.repo_root")
    _inside(config_path, repo_root, "selected config")

    mechanical = config.mechanical
    interpreter = _resolve_repo_path(repo_root, mechanical.interpreter, "mechanical.interpreter")
    entrypoint = _resolve_repo_path(repo_root, mechanical.entrypoint, "mechanical.entrypoint")
    working_directory = _resolve_repo_path(
        repo_root, mechanical.working_directory, "mechanical.working_directory"
    )
    artifact_root = _resolve_repo_path(
        repo_root, mechanical.artifact_root, "mechanical.artifact_root"
    )
    artifact_probe_file = _resolve_repo_path(
        repo_root, mechanical.artifact_probe_file, "mechanical.artifact_probe_file"
    )
    _require_file(interpreter, "mechanical.interpreter")
    _require_file(entrypoint, "mechanical.entrypoint")
    _require_directory(working_directory, "mechanical.working_directory")
    _require_directory(artifact_root, "mechanical.artifact_root")
    _read_probe(artifact_probe_file, artifact_root, "mechanical.artifact_probe_file")
    resolved_mechanical = ResolvedMechanical(
        interpreter=interpreter,
        entrypoint=entrypoint,
        working_directory=working_directory,
        artifact_root=artifact_root,
        artifact_probe_file=artifact_probe_file,
        exact_argv=(str(interpreter), str(entrypoint), *mechanical.args),
    )

    providers: list[ResolvedProvider] = []
    for binding in config.providers:
        label = f"provider {binding.provider_id}"
        executable = _resolve_executable(binding.executable, f"{label}.executable", which)
        planned_executable = _resolve_executable(
            binding.planned_argv[0], f"{label}.planned_argv[0]", which
        )
        if planned_executable != executable:
            raise PreflightError(
                f"{label} planned argv and probes do not use the same canonical executable"
            )
        provider_cwd = _resolve_repo_path(
            repo_root, binding.working_directory, f"{label}.working_directory"
        )
        provider_artifact_root = _resolve_repo_path(
            repo_root, binding.artifact_root, f"{label}.artifact_root"
        )
        provider_probe = _resolve_repo_path(
            repo_root, binding.artifact_probe_file, f"{label}.artifact_probe_file"
        )
        _require_directory(provider_cwd, f"{label}.working_directory")
        _require_directory(provider_artifact_root, f"{label}.artifact_root")
        _read_probe(provider_probe, provider_artifact_root, f"{label}.artifact_probe_file")
        planned = (str(executable), *binding.planned_argv[1:])
        if binding.provider_id == "codex":
            cd_index = planned.index("--cd")
            planned_root = Path(planned[cd_index + 1])
            if not planned_root.is_absolute():
                raise PreflightError("codex --cd value must be an absolute canonical repo root")
            try:
                resolved_cd = planned_root.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise PreflightError(f"codex --cd value cannot be resolved: {exc}") from None
            if resolved_cd != repo_root:
                raise PreflightError("codex --cd value does not match canonical repo_root")
        providers.append(
            ResolvedProvider(
                binding=binding,
                executable=executable,
                working_directory=provider_cwd,
                artifact_root=provider_artifact_root,
                artifact_probe_file=provider_probe,
                planned_argv=planned,
            )
        )
    return repo_root, resolved_mechanical, tuple(providers)


def _run(
    runner: RunCommand,
    argv: list[str],
    *,
    cwd: Path,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    try:
        return runner(
            argv,
            cwd=cwd,
            shell=False,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise PreflightError(f"allowed probe timed out after {timeout} seconds") from None
    except (OSError, UnicodeError) as exc:
        raise PreflightError(f"allowed probe execution failed: {exc}") from None


def _git_root_probe(
    root: Path,
    timeout: int,
    runner: RunCommand,
    commands: list[dict[str, Any]],
) -> None:
    argv = ["git", "-C", str(root), "rev-parse", "--show-toplevel"]
    commands.append({"purpose": "git_root", "argv": argv})
    completed = _run(runner, argv, cwd=root, timeout=timeout)
    if completed.returncode != 0:
        raise PreflightError(f"git root probe exited with code {completed.returncode}")
    output = completed.stdout.strip()
    if not output:
        raise PreflightError("git root probe returned an empty path")
    try:
        observed = Path(output).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PreflightError(f"git root probe returned an invalid path: {exc}") from None
    if observed != root:
        raise PreflightError(
            f"git top-level mismatch: configured={root}, observed={observed}"
        )


def _environment_value(environ: Mapping[str, str], name: str) -> str | None:
    target = name.casefold()
    for key, value in environ.items():
        if key.casefold() == target:
            return value
    return None


def _validate_environment(
    providers: tuple[ResolvedProvider, ...], environ: Mapping[str, str]
) -> tuple[dict[str, bool], tuple[str, str] | None]:
    results: dict[str, bool] = {}
    first_missing: tuple[str, str] | None = None
    for provider in providers:
        provider_id = provider.binding.provider_id
        results[provider_id] = True
        for name in provider.binding.required_env:
            value = _environment_value(environ, name)
            if value is None or not value.strip():
                results[provider_id] = False
                if first_missing is None:
                    first_missing = (provider_id, name)
    return results, first_missing


def _version_argv(provider: ResolvedProvider) -> list[str]:
    if provider.binding.provider_id not in {"codex", "claude"}:
        raise PreflightError(f"unsupported provider probe id: {provider.binding.provider_id}")
    return [str(provider.executable), "--version"]


def _auth_argv(provider: ResolvedProvider) -> list[str]:
    if provider.binding.provider_id == "codex":
        return [str(provider.executable), "login", "status"]
    if provider.binding.provider_id == "claude":
        return [str(provider.executable), "auth", "status"]
    raise PreflightError(f"unsupported provider probe id: {provider.binding.provider_id}")


def _run_live_probes(
    providers: tuple[ResolvedProvider, ...],
    timeout: int,
    runner: RunCommand,
    commands: list[dict[str, Any]],
    results: dict[str, dict[str, str]],
) -> None:
    for provider in providers:
        provider_id = provider.binding.provider_id
        results[provider_id] = {"version": "NOT_RUN", "auth": "NOT_RUN"}
        version_argv = _version_argv(provider)
        commands.append({"purpose": f"{provider_id}_version", "argv": version_argv})
        try:
            version = _run(
                runner,
                version_argv,
                cwd=provider.working_directory,
                timeout=timeout,
            )
        except PreflightError:
            results[provider_id]["version"] = BLOCKED
            raise
        if version.returncode != 0:
            results[provider_id]["version"] = BLOCKED
            raise PreflightError(
                f"provider {provider_id} version probe exited with code {version.returncode}"
            )
        version_text = f"{version.stdout}\n{version.stderr}"
        if provider.binding.version_expected_substring not in version_text:
            results[provider_id]["version"] = BLOCKED
            raise PreflightError(f"provider {provider_id} version substring mismatch")
        results[provider_id]["version"] = PASS

        auth_argv = _auth_argv(provider)
        commands.append({"purpose": f"{provider_id}_auth", "argv": auth_argv})
        try:
            auth = _run(
                runner,
                auth_argv,
                cwd=provider.working_directory,
                timeout=timeout,
            )
        except PreflightError:
            results[provider_id]["auth"] = BLOCKED
            raise
        if auth.returncode != 0:
            results[provider_id]["auth"] = BLOCKED
            raise PreflightError(
                f"provider {provider_id} auth probe exited with code {auth.returncode}"
            )
        results[provider_id]["auth"] = PASS


def _mechanical_output(
    binding: MechanicalBinding,
    resolved: ResolvedMechanical | None,
) -> dict[str, Any]:
    if resolved is None:
        return {"status": "NOT_EVALUATED"}
    return {
        "status": PASS,
        "provider": binding.provider,
        "model": binding.model,
        "process_mode": binding.process_mode,
        "exact_argv": list(resolved.exact_argv),
        "working_directory": str(resolved.working_directory),
        "artifact_root": str(resolved.artifact_root),
        "artifact_probe_file": str(resolved.artifact_probe_file),
        "host_artifact_readable": True,
        "child_process_artifact_access": NOT_CLAIMED,
        "planned_command_executed": False,
    }


def _provider_output(
    providers: tuple[ResolvedProvider, ...],
    live: bool,
    live_results: dict[str, dict[str, str]],
    required_env_results: Mapping[str, bool],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for provider in providers:
        binding = provider.binding
        probes = live_results.get(binding.provider_id, {})
        if not live:
            provider_status = PASS
        elif not probes:
            provider_status = "NOT_EVALUATED"
        elif probes.get("version") == PASS and probes.get("auth") == PASS:
            provider_status = PASS
        elif BLOCKED in probes.values():
            provider_status = BLOCKED
        else:
            provider_status = "NOT_EVALUATED"
        result.append(
            {
                "id": binding.provider_id,
                "status": provider_status,
                "canonical_executable": str(provider.executable),
                "model": binding.model,
                "model_binding_kind": binding.model_binding_kind,
                "planned_argv": list(provider.planned_argv),
                "working_directory": str(provider.working_directory),
                "artifact_root": str(provider.artifact_root),
                "artifact_probe_file": str(provider.artifact_probe_file),
                "host_artifact_readable": True,
                "provider_process_artifact_access": NOT_CLAIMED,
                "required_env_present": required_env_results.get(
                    binding.provider_id, True
                ),
                "version_probe": probes.get("version", "NOT_RUN"),
                "auth_probe": probes.get("auth", "NOT_RUN"),
                "live_mode": live,
                "planned_command_executed": False,
            }
        )
    return result


def _base_payload() -> dict[str, Any]:
    return {
        "status": BLOCKED,
        "config_path": None,
        "binding_source": None,
        "mechanical": {"status": "NOT_EVALUATED"},
        "providers": [],
        "checks": [],
        "commands_executed": [],
        "mutations_performed": False,
        "provider_probe_side_effects": NOT_CLAIMED,
    }


def build_parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(description="Validate exact execution bindings")
    parser.add_argument("--project-config", type=Path)
    parser.add_argument("--repo-wrapper-config", type=Path)
    parser.add_argument("--live", action="store_true")
    return parser


def execute(
    argv: Sequence[str] | None = None,
    *,
    runner: RunCommand = subprocess.run,
    environ: Mapping[str, str] | None = None,
    which: WhichCommand = shutil.which,
) -> tuple[dict[str, Any], int]:
    """Run preflight and return its JSON-ready payload and exit code."""

    payload = _base_payload()
    config: BindingConfig | None = None
    resolved_mechanical: ResolvedMechanical | None = None
    resolved_providers: tuple[ResolvedProvider, ...] = ()
    live_results: dict[str, dict[str, str]] = {}
    required_env_results: dict[str, bool] = {}
    live_requested = False
    try:
        args = build_parser().parse_args(argv)
        live_requested = bool(args.live)
        config_path, binding_source = select_config(
            args.project_config, args.repo_wrapper_config
        )
        payload["config_path"] = str(config_path)
        payload["binding_source"] = binding_source
        config = load_config(config_path)
        validate_pure(config)
        payload["checks"].append({"name": "selector_schema_argv", "status": PASS})

        repo_root, resolved_mechanical, resolved_providers = resolve_filesystem(
            config_path, config, which
        )
        payload["checks"].append({"name": "filesystem_containment", "status": PASS})

        required_env_results, first_missing_env = _validate_environment(
            resolved_providers, os.environ if environ is None else environ
        )

        commands = payload["commands_executed"]
        assert isinstance(commands, list)
        _git_root_probe(repo_root, config.timeout_seconds, runner, commands)
        payload["checks"].append({"name": "git_root", "status": PASS})

        if first_missing_env is not None:
            provider_id, name = first_missing_env
            raise PreflightError(
                f"provider {provider_id} required_env is missing or empty: {name}"
            )
        payload["checks"].append({"name": "deterministic_bindings", "status": PASS})

        if args.live:
            _run_live_probes(
                resolved_providers,
                config.timeout_seconds,
                runner,
                commands,
                live_results,
            )
            payload["checks"].append({"name": "provider_version_auth", "status": PASS})
        else:
            payload["checks"].append(
                {"name": "provider_version_auth", "status": NOT_CLAIMED}
            )

        payload["status"] = PASS
        payload["mechanical"] = _mechanical_output(config.mechanical, resolved_mechanical)
        payload["providers"] = _provider_output(
            resolved_providers, bool(args.live), live_results, required_env_results
        )
        return payload, 0
    except Exception as exc:  # Fail closed without traceback or provider output leakage.
        message = str(exc).strip() or type(exc).__name__
        checks = payload["checks"]
        assert isinstance(checks, list)
        checks.append({"name": "preflight", "status": BLOCKED, "detail": message})
        if config is not None:
            payload["mechanical"] = _mechanical_output(config.mechanical, resolved_mechanical)
        if resolved_providers:
            payload["providers"] = _provider_output(
                resolved_providers,
                live_requested,
                live_results,
                required_env_results,
            )
        return payload, 2


def _write_payload(payload: dict[str, Any]) -> None:
    data = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8", errors="strict"
    )
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(data)
        buffer.flush()
    else:
        sys.stdout.write(data.decode("utf-8"))
        sys.stdout.flush()


def main(argv: Sequence[str] | None = None) -> int:
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    except Exception as exc:  # Keep encoding setup failures inside BLOCKED/2.
        payload = _base_payload()
        payload["checks"].append(
            {
                "name": "stdout_utf8",
                "status": BLOCKED,
                "detail": str(exc).strip() or type(exc).__name__,
            }
        )
        _write_payload(payload)
        return 2
    payload, exit_code = execute(argv)
    _write_payload(payload)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
