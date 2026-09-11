"""Isolated structural discovery evidence for MAM T15-P5.

The helper packages a temporary source copy, installs only into temporary
Claude/Codex roots, and judges machine-readable plugin state. It never invokes
a model or interprets prose as a verdict.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Never


PLUGIN_NAME = "ai-research"
MARKETPLACE_NAME = "multiagent-methodology"
PLUGIN_ID = f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"
PLUGIN_VERSION = "0.1.0"
EXPECTED_SKILLS = frozenset({"ai-coding-video-benchmark", "daily-ai-news"})
SCHEMA = "mam.t15-p5.fresh-discovery.v1"


class Verdict(str, Enum):
    PASS = "PASS"
    BLOCKED = "BLOCKED"


class Applicability(str, Enum):
    PASS = "PASS"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class EvidenceError(RuntimeError):
    """A structural evidence contract failed and must block."""


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise EvidenceError(f"invalid arguments: {message}")


RunCommand = Callable[..., subprocess.CompletedProcess[str]]
Inventory = dict[str, str]


@dataclass(frozen=True)
class FreshConfig:
    methodology_root: Path
    evidence_dir: Path
    protected_home: Path
    temp_parent: Path | None = None
    claude_command: str = "claude"
    codex_command: str = "codex"
    timeout_seconds: float = 120.0


def _canonical(path: Path) -> Path:
    try:
        return path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise EvidenceError(f"path cannot be resolved: {path}: {exc}") from None


def require_descendant(path: Path, root: Path, label: str) -> Path:
    """Require path to be strictly inside root, including on Windows."""

    resolved_path = _canonical(path)
    resolved_root = _canonical(root)
    try:
        common = Path(os.path.commonpath([resolved_path, resolved_root]))
    except ValueError:
        raise EvidenceError(f"{label} is outside isolated temp root: {path}") from None
    if common != resolved_root or resolved_path == resolved_root:
        raise EvidenceError(f"{label} is outside isolated temp root: {path}")
    return resolved_path


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    try:
        return _hash_bytes(path.read_bytes())
    except OSError as exc:
        raise EvidenceError(f"file cannot be hashed: {path}: {exc}") from None


def _inventory_tree(root: Path, *, exclude_python_cache: bool) -> Inventory:
    """Inventory relative file paths with an explicit generated-cache policy."""

    root = _canonical(root)
    if not root.is_dir():
        raise EvidenceError(f"inventory root is not a directory: {root}")
    inventory: Inventory = {}
    try:
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if exclude_python_cache and (
                "__pycache__" in relative.parts or path.suffix in {".pyc", ".pyo"}
            ):
                continue
            key = relative.as_posix()
            if path.is_symlink():
                inventory[key] = _hash_bytes(
                    f"SYMLINK:{os.readlink(path)}".encode("utf-8")
                )
            elif path.is_file():
                inventory[key] = sha256_file(path)
    except OSError as exc:
        raise EvidenceError(f"inventory cannot be read: {root}: {exc}") from None
    return inventory


def inventory_tree(root: Path) -> Inventory:
    """Inventory distribution files while excluding generated Python cache."""

    return _inventory_tree(root, exclude_python_cache=True)


def inventory_protected_tree(root: Path) -> Inventory:
    """Inventory every protected-state byte, including generated Python cache."""

    return _inventory_tree(root, exclude_python_cache=False)


def inventory_protected_path(path: Path) -> dict[str, Any]:
    path = _canonical(path)
    if not path.exists() and not path.is_symlink():
        return {"state": "ABSENT", "inventory": {}}
    if path.is_dir():
        return {"state": "DIRECTORY", "inventory": inventory_protected_tree(path)}
    if path.is_symlink():
        digest = _hash_bytes(f"SYMLINK:{os.readlink(path)}".encode("utf-8"))
    else:
        digest = sha256_file(path)
    return {"state": "FILE", "inventory": {".": digest}}


def compare_inventories(expected: Mapping[str, str], actual: Mapping[str, str]) -> dict[str, Any]:
    expected_keys = set(expected)
    actual_keys = set(actual)
    changed = sorted(
        key for key in expected_keys & actual_keys if expected[key] != actual[key]
    )
    return {
        "missing": sorted(expected_keys - actual_keys),
        "extra": sorted(actual_keys - expected_keys),
        "changed": changed,
        "match": not (expected_keys - actual_keys or actual_keys - expected_keys or changed),
    }


def compare_protected_state(
    before: Mapping[str, dict[str, Any]], after: Mapping[str, dict[str, Any]]
) -> dict[str, Any]:
    missing: list[str] = []
    extra: list[str] = []
    changed: list[str] = []
    for label in sorted(set(before) | set(after)):
        old = before.get(label, {"state": "ABSENT", "inventory": {}})
        new = after.get(label, {"state": "ABSENT", "inventory": {}})
        if old["state"] != new["state"]:
            changed.append(label)
        diff = compare_inventories(old["inventory"], new["inventory"])
        missing.extend(f"{label}/{item}" for item in diff["missing"])
        extra.extend(f"{label}/{item}" for item in diff["extra"])
        changed.extend(f"{label}/{item}" for item in diff["changed"])
    return {
        "missing": sorted(set(missing)),
        "extra": sorted(set(extra)),
        "changed": sorted(set(changed)),
        "match": not (missing or extra or changed),
    }


def capture_protected_state(home: Path) -> dict[str, dict[str, Any]]:
    return {
        "claude_plugins": inventory_protected_path(home / ".claude" / "plugins"),
        "codex_plugins": inventory_protected_path(home / ".codex" / "plugins"),
        "codex_config": inventory_protected_path(home / ".codex" / "config.toml"),
    }


def _json_object(value: str, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise EvidenceError(f"{label} is not valid JSON: {exc}") from None
    if not isinstance(parsed, dict):
        raise EvidenceError(f"{label} root must be an object")
    return parsed


def parse_claude_list(stdout: str, config_root: Path) -> Path:
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise EvidenceError(f"Claude plugin list is not valid JSON: {exc}") from None
    if not isinstance(parsed, list):
        raise EvidenceError("Claude plugin list root must be an array")
    matches = [item for item in parsed if isinstance(item, dict) and item.get("id") == PLUGIN_ID]
    if len(matches) != 1:
        raise EvidenceError(f"Claude installed target count must be 1, observed {len(matches)}")
    row = matches[0]
    if row.get("version") != PLUGIN_VERSION:
        raise EvidenceError("Claude plugin version mismatch")
    if row.get("enabled") is not True:
        raise EvidenceError("Claude plugin enabled must be true")
    install_path = row.get("installPath")
    if not isinstance(install_path, str) or not install_path:
        raise EvidenceError("Claude plugin installPath must be a non-empty string")
    return require_descendant(Path(install_path), config_root, "Claude installPath")


def parse_codex_list(stdout: str, extract_root: Path) -> Path:
    parsed = _json_object(stdout, "Codex plugin list")
    installed = parsed.get("installed")
    if not isinstance(installed, list):
        raise EvidenceError("Codex plugin list installed must be an array")
    matches = [
        item
        for item in installed
        if isinstance(item, dict) and item.get("pluginId") == PLUGIN_ID
    ]
    if len(matches) != 1:
        raise EvidenceError(f"Codex installed target count must be 1, observed {len(matches)}")
    row = matches[0]
    expected = {
        "name": PLUGIN_NAME,
        "marketplaceName": MARKETPLACE_NAME,
        "version": PLUGIN_VERSION,
        "installed": True,
        "enabled": True,
    }
    for key, value in expected.items():
        if row.get(key) != value:
            raise EvidenceError(f"Codex plugin {key} mismatch")
    source = row.get("source")
    if not isinstance(source, dict) or source.get("source") != "local":
        raise EvidenceError("Codex plugin source.source must be local")
    source_path = source.get("path")
    if not isinstance(source_path, str) or not source_path:
        raise EvidenceError("Codex plugin source.path must be a non-empty string")
    return require_descendant(Path(source_path), extract_root, "Codex source.path")


def assert_plugin_inventory(plugin_root: Path, manifest_name: str) -> Inventory:
    inventory = inventory_tree(plugin_root)
    required = {manifest_name}
    for skill in EXPECTED_SKILLS:
        required.add(f"skills/{skill}/SKILL.md")
        required.add(f"skills/{skill}/agents/openai.yaml")
    missing = sorted(required - set(inventory))
    if missing:
        raise EvidenceError(f"plugin inventory is missing required files: {missing}")
    observed_skills = {
        path.split("/")[1]
        for path in inventory
        if path.startswith("skills/") and path.endswith("/SKILL.md")
    }
    if observed_skills != EXPECTED_SKILLS:
        raise EvidenceError(
            "plugin skill inventory mismatch: "
            f"expected={sorted(EXPECTED_SKILLS)} observed={sorted(observed_skills)}"
        )
    return inventory


def evaluate_codex_cache(
    extraction_plugin: Path, cache_plugin: Path | None
) -> dict[str, Any]:
    extraction_plugin = _canonical(extraction_plugin)
    if cache_plugin is None:
        return {
            "status": Applicability.NOT_APPLICABLE.value,
            "reason": "no distinct Codex cache copy observed",
            "path": None,
            "parity": None,
        }
    cache_plugin = _canonical(cache_plugin)
    if cache_plugin == extraction_plugin:
        raise EvidenceError("Codex cache self-comparison is forbidden")
    parity = compare_inventories(
        inventory_tree(extraction_plugin), inventory_tree(cache_plugin)
    )
    if not parity["match"]:
        raise EvidenceError("Codex distinct cache differs from extraction tree")
    return {
        "status": Applicability.PASS.value,
        "reason": "distinct Codex cache copy matches extraction tree",
        "path": str(cache_plugin),
        "parity": parity,
    }


def find_distinct_codex_cache(codex_home: Path, extraction_plugin: Path) -> Path | None:
    candidates: list[Path] = []
    canonical_home = _canonical(codex_home)
    canonical_extraction = _canonical(extraction_plugin)
    cache_root = (
        canonical_home
        / "plugins"
        / "cache"
        / MARKETPLACE_NAME
        / PLUGIN_NAME
    )
    if cache_root.is_dir():
        try:
            version_directories = sorted(
                path for path in cache_root.iterdir() if path.is_dir()
            )
        except OSError as exc:
            raise EvidenceError(
                f"Codex {PLUGIN_NAME} cache versions cannot be read: {cache_root}: {exc}"
            ) from None
        for version_directory in version_directories:
            candidate = _canonical(version_directory)
            manifest = candidate / ".codex-plugin" / "plugin.json"
            if not manifest.is_file():
                raise EvidenceError(
                    f"Codex {PLUGIN_NAME} cache manifest is missing: {manifest}"
                )
            try:
                value = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise EvidenceError(
                    f"Codex {PLUGIN_NAME} cache manifest is unreadable: {manifest}: {exc}"
                ) from None
            if not isinstance(value, dict) or value.get("name") != PLUGIN_NAME:
                raise EvidenceError(
                    f"Codex {PLUGIN_NAME} cache manifest name mismatch: {manifest}"
                )
            if candidate != canonical_extraction:
                candidates.append(candidate)
    unique = sorted(set(candidates))
    if len(unique) > 1:
        raise EvidenceError(f"multiple distinct Codex cache copies observed: {unique}")
    return unique[0] if unique else None


def _run_capture(
    argv: Sequence[str],
    env: Mapping[str, str],
    timeout_seconds: float,
    runner: RunCommand,
    cwd: Path | None = None,
) -> dict[str, Any]:
    try:
        completed = runner(
            list(argv),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            env=dict(env),
            cwd=None if cwd is None else str(cwd),
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        raise EvidenceError(f"command failed to execute: {list(argv)}: {exc}") from None
    return {
        "argv": list(argv),
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _require_command_ok(command: dict[str, Any], label: str) -> None:
    if command["exit_code"] != 0:
        raise EvidenceError(f"{label} exited with code {command['exit_code']}")


def _copy_source(methodology_root: Path, destination: Path) -> None:
    def ignore(_directory: str, names: list[str]) -> set[str]:
        blocked = {"dist", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
        return {name for name in names if name in blocked or name.endswith((".pyc", ".pyo"))}

    shutil.copytree(methodology_root, destination, ignore=ignore)


def _safe_extract(archive: Path, extract_root: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            require_descendant(extract_root / member.filename, extract_root, "archive member")
        bundle.extractall(extract_root)


def build_forced_archive(
    methodology_root: Path,
    temp_root: Path,
    timeout_seconds: float,
    runner: RunCommand,
) -> tuple[Path, Path, dict[str, Any]]:
    source_copy = temp_root / "source-copy"
    _copy_source(methodology_root, source_copy)
    bash = shutil.which("bash")
    if bash is None:
        raise EvidenceError("bash is required for forced package evidence")
    package_argv = [bash, "package.sh"]
    env = dict(os.environ)
    env.update({"PACKAGE_FORMAT": "zip", "PACKAGE_ZTR": "0", "PYTHON": sys.executable})
    package_command = _run_capture(
        package_argv, env, timeout_seconds, runner, cwd=source_copy
    )
    _require_command_ok(package_command, "forced ZIP package")
    output_lines = [line for line in package_command["stdout"].splitlines() if line.strip()]
    if len(output_lines) != 1:
        raise EvidenceError("forced ZIP package must emit exactly one artifact path")
    candidates = sorted((source_copy / "dist").glob("multiagent-methodology-beta-*.zip"))
    if len(candidates) != 1:
        raise EvidenceError(
            f"forced ZIP package artifact count must be 1, observed {len(candidates)}"
        )
    artifact = _canonical(candidates[0])
    if not output_lines[0].replace("\\", "/").endswith(f"/{artifact.name}"):
        raise EvidenceError("forced ZIP package stdout does not identify the artifact")
    require_descendant(artifact, source_copy / "dist", "forced ZIP artifact")
    if artifact.suffix != ".zip" or not artifact.is_file():
        raise EvidenceError("forced package artifact must be an existing .zip")
    version_command = _run_capture(
        [sys.executable, "--version"], env, timeout_seconds, runner
    )
    _require_command_ok(version_command, "Python version provenance")
    provenance = {
        "backend": "python-stdlib-zipfile",
        "python_executable": str(_canonical(Path(sys.executable))),
        "python_version_argv": version_command["argv"],
        "python_version_exit_code": version_command["exit_code"],
        "python_version_stdout": version_command["stdout"],
        "python_version_stderr": version_command["stderr"],
        "zipfile_module": str(_canonical(Path(zipfile.__file__))),
        "package_argv": package_command["argv"],
        "package_exit_code": package_command["exit_code"],
        "package_stdout": package_command["stdout"],
        "package_stderr": package_command["stderr"],
        "artifact": str(artifact),
        "artifact_sha256": sha256_file(artifact),
    }
    extract_root = temp_root / "forced-extraction"
    extract_root.mkdir()
    _safe_extract(artifact, extract_root)
    return artifact, extract_root, provenance


def _resolved_command(command: str) -> str:
    resolved = shutil.which(command)
    if resolved is None:
        raise EvidenceError(f"required CLI is unavailable: {command}")
    return resolved


def _write_command_evidence(evidence_dir: Path, label: str, command: Mapping[str, Any]) -> None:
    (evidence_dir / f"{label}.stdout.txt").write_text(
        str(command["stdout"]), encoding="utf-8"
    )
    (evidence_dir / f"{label}.stderr.txt").write_text(
        str(command["stderr"]), encoding="utf-8"
    )


def _base_payload() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "status": Verdict.BLOCKED.value,
        "distribution": {},
        "claude": {},
        "codex": {},
        "protected_state": {},
        "diagnostics": [],
        "not_claimed": [
            "model_invocation",
            "machine_readable_skill_selection_event",
            "desktop_heartbeat",
            "shadow_activation",
            "production_activation",
        ],
        "mutations": "isolated-temp-roots-only",
    }


def run_fresh_discovery(
    config: FreshConfig, runner: RunCommand = subprocess.run
) -> tuple[dict[str, Any], int]:
    payload = _base_payload()
    before: dict[str, dict[str, Any]] = {}
    temp_root: Path | None = None
    try:
        if not 0 < config.timeout_seconds <= 600:
            raise EvidenceError("timeout must be greater than 0 and at most 600 seconds")
        methodology_root = _canonical(config.methodology_root)
        if not (methodology_root / "package.sh").is_file():
            raise EvidenceError(f"methodology root lacks package.sh: {methodology_root}")
        config.evidence_dir.mkdir(parents=True, exist_ok=True)
        before = capture_protected_state(config.protected_home)
        temp_root = Path(
            tempfile.mkdtemp(prefix="mam-p5-fresh-", dir=config.temp_parent)
        ).resolve()
        claude_config = temp_root / "claude-config"
        codex_home = temp_root / "codex-home"
        claude_config.mkdir()
        codex_home.mkdir()
        require_descendant(claude_config, temp_root, "CLAUDE_CONFIG_DIR")
        require_descendant(codex_home, temp_root, "CODEX_HOME")

        _artifact, extract_root, provenance = build_forced_archive(
            methodology_root, temp_root, config.timeout_seconds, runner
        )
        extraction_plugin = extract_root / "plugins" / PLUGIN_NAME
        source_inventory = inventory_tree(methodology_root / "plugins" / PLUGIN_NAME)
        extraction_inventory = assert_plugin_inventory(
            extraction_plugin, ".codex-plugin/plugin.json"
        )
        source_extraction = compare_inventories(source_inventory, extraction_inventory)
        if not source_extraction["match"]:
            raise EvidenceError("source plugin differs from forced archive extraction")
        payload["distribution"] = {
            "extract_root": str(extract_root),
            "provenance": provenance,
            "source_inventory": source_inventory,
            "extraction_inventory": extraction_inventory,
            "source_extraction_parity": source_extraction,
        }

        claude = _resolved_command(config.claude_command)
        claude_env = dict(os.environ)
        claude_env["CLAUDE_CONFIG_DIR"] = str(claude_config)
        claude_commands = {
            "marketplace_add": _run_capture(
                [claude, "plugin", "marketplace", "add", str(extract_root)],
                claude_env,
                config.timeout_seconds,
                runner,
            ),
            "install": _run_capture(
                [claude, "plugin", "install", PLUGIN_ID],
                claude_env,
                config.timeout_seconds,
                runner,
            ),
            "list": _run_capture(
                [claude, "plugin", "list", "--json"],
                claude_env,
                config.timeout_seconds,
                runner,
            ),
            "details": _run_capture(
                [claude, "plugin", "details", PLUGIN_ID],
                claude_env,
                config.timeout_seconds,
                runner,
            ),
        }
        for label in ("marketplace_add", "install", "list"):
            _require_command_ok(claude_commands[label], f"Claude {label}")
        claude_cache = parse_claude_list(claude_commands["list"]["stdout"], claude_config)
        claude_inventory = assert_plugin_inventory(
            claude_cache, ".claude-plugin/plugin.json"
        )
        claude_parity = compare_inventories(extraction_inventory, claude_inventory)
        if not claude_parity["match"]:
            raise EvidenceError("Claude cache differs from forced extraction")
        payload["claude"] = {
            "config_root": str(claude_config),
            "commands": claude_commands,
            "cache_root": str(claude_cache),
            "cache_inventory": claude_inventory,
            "extraction_cache_parity": claude_parity,
            "details_role": "diagnostic-only",
        }
        for label, command in claude_commands.items():
            _write_command_evidence(config.evidence_dir, f"claude-{label}", command)

        codex = _resolved_command(config.codex_command)
        codex_env = dict(os.environ)
        codex_env["CODEX_HOME"] = str(codex_home)
        codex_commands = {
            "marketplace_add": _run_capture(
                [codex, "plugin", "marketplace", "add", str(extract_root), "--json"],
                codex_env,
                config.timeout_seconds,
                runner,
            ),
            "install": _run_capture(
                [codex, "plugin", "add", PLUGIN_ID, "--json"],
                codex_env,
                config.timeout_seconds,
                runner,
            ),
            "list": _run_capture(
                [codex, "plugin", "list", "--json", "--marketplace", MARKETPLACE_NAME],
                codex_env,
                config.timeout_seconds,
                runner,
            ),
        }
        for label, command in codex_commands.items():
            _require_command_ok(command, f"Codex {label}")
        registered_root = parse_codex_list(codex_commands["list"]["stdout"], extract_root)
        registered_inventory = assert_plugin_inventory(
            registered_root, ".codex-plugin/plugin.json"
        )
        registration_parity = compare_inventories(extraction_inventory, registered_inventory)
        if not registration_parity["match"]:
            raise EvidenceError("Codex registered extraction root differs from extraction")
        cache_result = evaluate_codex_cache(
            extraction_plugin,
            find_distinct_codex_cache(codex_home, extraction_plugin),
        )
        payload["codex"] = {
            "home": str(codex_home),
            "commands": codex_commands,
            "registered_root": str(registered_root),
            "registered_inventory": registered_inventory,
            "registration_parity": registration_parity,
            "distinct_cache": cache_result,
        }
        for label, command in codex_commands.items():
            _write_command_evidence(config.evidence_dir, f"codex-{label}", command)
    except Exception as exc:  # Fail closed without a traceback in the JSON contract.
        payload["diagnostics"].append(str(exc).strip() or type(exc).__name__)
    finally:
        if before:
            try:
                after = capture_protected_state(config.protected_home)
                protected_diff = compare_protected_state(before, after)
                payload["protected_state"] = {
                    "before": before,
                    "after": after,
                    "diff": protected_diff,
                }
                if not protected_diff["match"]:
                    payload["diagnostics"].append("actual user protected plugin state changed")
            except Exception as exc:
                payload["diagnostics"].append(
                    f"protected state post-check failed: {str(exc).strip() or type(exc).__name__}"
                )
        if temp_root is not None:
            payload["temp_root"] = str(temp_root)
            shutil.rmtree(temp_root, ignore_errors=True)

    if not payload["diagnostics"] and payload["protected_state"].get("diff", {}).get("match"):
        payload["status"] = Verdict.PASS.value
    try:
        config.evidence_dir.mkdir(parents=True, exist_ok=True)
        (config.evidence_dir / "result.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        payload["status"] = Verdict.BLOCKED.value
        payload["diagnostics"].append(f"evidence output failed: {exc}")
    return payload, 0 if payload["status"] == Verdict.PASS.value else 2


def build_parser() -> JsonArgumentParser:
    methodology_root = Path(__file__).resolve().parent.parent
    parser = JsonArgumentParser(description="T15-P5 isolated fresh discovery evidence")
    parser.add_argument("--methodology-root", type=Path, default=methodology_root)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--temp-parent", type=Path)
    parser.add_argument("--claude-command", default="claude")
    parser.add_argument("--codex-command", default="codex")
    parser.add_argument("--timeout", type=float, default=120.0)
    return parser


def execute(argv: Sequence[str] | None = None) -> tuple[dict[str, Any], int]:
    try:
        args = build_parser().parse_args(argv)
        config = FreshConfig(
            methodology_root=args.methodology_root,
            evidence_dir=args.evidence_dir,
            protected_home=Path.home(),
            temp_parent=args.temp_parent,
            claude_command=args.claude_command,
            codex_command=args.codex_command,
            timeout_seconds=args.timeout,
        )
        return run_fresh_discovery(config)
    except Exception as exc:
        payload = _base_payload()
        payload["diagnostics"].append(str(exc).strip() or type(exc).__name__)
        return payload, 2


def main(argv: Sequence[str] | None = None) -> int:
    payload, exit_code = execute(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
