"""Read-only diagnosis of the agent-workflow plugin installation.

The doctor observes one canonical plugin target and reports drift as a single
JSON object.  It never installs, removes, repairs, or rewrites plugin state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Never


PLUGIN_NAME = "agent-workflow"
MARKETPLACE_NAME = "multiagent-methodology"
TARGET = f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"
PASS = "PASS"
BLOCKED = "BLOCKED"
ACTIVE_SESSION_REASON = (
    "P1은 source/installed/global skill 상태만 관측하며 현재 열린 Codex 세션의 "
    "active skill exposure는 증명하지 않는다."
)


class DoctorError(RuntimeError):
    """An observation or contract error that must fail closed."""


class JsonArgumentParser(argparse.ArgumentParser):
    """Keep invalid CLI input inside the JSON failure contract."""

    def error(self, message: str) -> Never:
        raise DoctorError(f"invalid arguments: {message}")


@dataclass(frozen=True)
class PluginObservation:
    """A source or installed plugin observation."""

    marketplace_path: str
    plugin_path: str
    version: str
    skills: frozenset[str]


@dataclass(frozen=True)
class Drift:
    """Pure comparison result between source and installed observations."""

    marketplace_path: bool
    plugin_path: bool
    version: bool
    missing_skills: tuple[str, ...]
    extra_skills: tuple[str, ...]

    @property
    def blocks(self) -> bool:
        return (
            self.marketplace_path
            or self.plugin_path
            or self.version
            or bool(self.missing_skills)
        )


@dataclass(frozen=True)
class ParsedInstalledRow:
    """The unique target row parsed from ``codex plugin list``."""

    marketplace_path: str
    plugin_path: str
    version: str


RunCommand = Callable[..., subprocess.CompletedProcess[str]]


def canonicalize_path(value: str) -> str:
    """Resolve and normalize a real path at the shared comparison boundary."""

    if not value or not value.strip():
        raise DoctorError("path is empty")
    try:
        resolved = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DoctorError(f"path cannot be resolved: {value!r}: {exc}") from None
    normalized = os.path.normpath(str(resolved))
    return normalized.casefold() if os.name == "nt" else normalized


def compare_observations(
    source: PluginObservation, installed: PluginObservation
) -> Drift:
    """Compare two observations without mutating either or external state."""

    required = source.skills
    present = installed.skills
    return Drift(
        marketplace_path=(
            canonicalize_path(source.marketplace_path)
            != canonicalize_path(installed.marketplace_path)
        ),
        plugin_path=(
            canonicalize_path(source.plugin_path)
            != canonicalize_path(installed.plugin_path)
        ),
        version=source.version != installed.version,
        missing_skills=tuple(sorted(required - present)),
        extra_skills=tuple(sorted(present - required)),
    )


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DoctorError(f"{label} cannot be read as UTF-8 JSON: {path}: {exc}") from None
    if not isinstance(value, dict):
        raise DoctorError(f"{label} root must be a JSON object: {path}")
    return value


def _required_string(mapping: dict[str, Any], key: str, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DoctorError(f"{label}.{key} must be a non-empty string")
    return value


def _scan_skills(skills_dir: Path) -> frozenset[str]:
    try:
        if not skills_dir.is_dir():
            raise DoctorError(f"skills directory does not exist: {skills_dir}")
        return frozenset(
            child.name
            for child in skills_dir.iterdir()
            if child.is_dir() and (child / "SKILL.md").is_file()
        )
    except OSError as exc:
        raise DoctorError(f"skills directory cannot be scanned: {skills_dir}: {exc}") from None


def _sha256_skill_tree(path: Path) -> str:
    """Hash relative paths and contents for one complete skill directory."""

    try:
        digest = hashlib.sha256()
        files = sorted(
            child
            for child in path.rglob("*")
            if child.is_file() and "__pycache__" not in child.relative_to(path).parts
        )
        for child in files:
            relative = child.relative_to(path).as_posix().encode("utf-8")
            contents = child.read_bytes()
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            digest.update(len(contents).to_bytes(8, "big"))
            digest.update(contents)
        return digest.hexdigest()
    except OSError as exc:
        raise DoctorError(f"skill tree cannot be hashed: {path}: {exc}") from None


def observe_legacy_skill_surfaces(
    source_plugin_path: Path, roots: Sequence[Path]
) -> list[dict[str, Any]]:
    """Compare direct global skill copies with the canonical plugin skills."""

    source_skills = source_plugin_path / "skills"
    observations: list[dict[str, Any]] = []
    for root in roots:
        expanded_root = root.expanduser().absolute()
        if not expanded_root.exists():
            continue
        if not expanded_root.is_dir():
            raise DoctorError(f"legacy skill root is not a directory: {expanded_root}")
        canonical_skills = {
            child.name: child
            for child in source_skills.iterdir()
            if child.is_dir() and (child / "SKILL.md").is_file()
        }
        installed_skills = {
            child.name: child
            for child in expanded_root.iterdir()
            if child.is_dir() and (child / "SKILL.md").is_file()
        }
        for skill_name in sorted(canonical_skills.keys() & installed_skills.keys()):
            source_skill = canonical_skills[skill_name]
            installed_skill = installed_skills[skill_name]
            source_sha = _sha256_skill_tree(source_skill)
            installed_sha = _sha256_skill_tree(installed_skill)
            observations.append(
                {
                    "surface_root": str(expanded_root),
                    "skill": skill_name,
                    "orphan": False,
                    "hash_scope": "skill-tree",
                    "source_sha256": source_sha,
                    "installed_sha256": installed_sha,
                    "sha_mismatch": source_sha != installed_sha,
                }
            )
        for skill_name in sorted(installed_skills.keys() - canonical_skills.keys()):
            installed_skill = installed_skills[skill_name]
            if not installed_skill.is_dir():
                continue
            observations.append(
                {
                    "surface_root": str(expanded_root),
                    "skill": skill_name,
                    "orphan": True,
                    "hash_scope": "skill-tree",
                    "source_sha256": None,
                    "installed_sha256": _sha256_skill_tree(installed_skill),
                    "sha_mismatch": None,
                }
            )
    return observations


def _marketplace_root(marketplace_path: Path) -> Path:
    parent = marketplace_path.parent
    if parent.name.casefold() != "plugins" or parent.parent.name.casefold() != ".agents":
        raise DoctorError(
            "marketplace manifest must be located at .agents/plugins/marketplace.json"
        )
    return parent.parent.parent


def observe_source(
    marketplace_path: Path, expected_plugin_path: Path
) -> PluginObservation:
    """Read and validate the canonical marketplace and plugin source."""

    marketplace_path = marketplace_path.absolute()
    expected_plugin_path = expected_plugin_path.absolute()
    marketplace = _read_json_object(marketplace_path, "marketplace manifest")
    if _required_string(marketplace, "name", "marketplace manifest") != MARKETPLACE_NAME:
        raise DoctorError(f"marketplace manifest.name must be {MARKETPLACE_NAME!r}")
    plugins = marketplace.get("plugins")
    if not isinstance(plugins, list):
        raise DoctorError("marketplace manifest.plugins must be an array")
    targets = [
        item
        for item in plugins
        if isinstance(item, dict) and item.get("name") == PLUGIN_NAME
    ]
    if len(targets) != 1:
        raise DoctorError(
            f"source marketplace target count must be 1, observed {len(targets)}"
        )

    source_spec = targets[0].get("source")
    if not isinstance(source_spec, dict):
        raise DoctorError("source marketplace target.source must be an object")
    if source_spec.get("source") != "local":
        raise DoctorError("source marketplace target must use local source")
    relative_plugin = _required_string(source_spec, "path", "target.source")
    relative_path = Path(relative_plugin)
    if relative_path.is_absolute():
        marketplace_plugin_path = relative_path
    else:
        marketplace_plugin_path = _marketplace_root(marketplace_path) / relative_path

    if canonicalize_path(str(marketplace_plugin_path)) != canonicalize_path(
        str(expected_plugin_path)
    ):
        raise DoctorError(
            "source marketplace plugin path mismatch: "
            f"resolved={marketplace_plugin_path}, expected={expected_plugin_path}"
        )

    plugin_manifest_path = expected_plugin_path / ".codex-plugin" / "plugin.json"
    manifest = _read_json_object(plugin_manifest_path, "plugin manifest")
    if _required_string(manifest, "name", "plugin manifest") != PLUGIN_NAME:
        raise DoctorError(f"plugin manifest.name must be {PLUGIN_NAME!r}")
    version = _required_string(manifest, "version", "plugin manifest")
    skills_value = _required_string(manifest, "skills", "plugin manifest")
    skills_path = Path(skills_value)
    skills_dir = skills_path if skills_path.is_absolute() else expected_plugin_path / skills_path
    skills = _scan_skills(skills_dir)

    return PluginObservation(
        marketplace_path=str(marketplace_path),
        plugin_path=str(expected_plugin_path),
        version=version,
        skills=skills,
    )


def _marketplace_header(line: str) -> str | None:
    prefix = "Marketplace `"
    if not line.startswith(prefix) or not line.endswith("`"):
        return None
    name = line[len(prefix) : -1]
    if not name:
        raise DoctorError("marketplace header has an empty name")
    return name


def _column_offsets(header: str) -> tuple[int, int, int, int]:
    labels = ("PLUGIN", "STATUS", "VERSION", "PATH")
    offsets = (
        header.find(labels[0]),
        header.find(labels[1]),
        header.find(labels[2]),
        header.find(labels[3]),
    )
    if any(offset < 0 for offset in offsets):
        raise DoctorError("plugin list table is missing a required header")
    if offsets != tuple(sorted(offsets)) or len(set(offsets)) != len(offsets):
        raise DoctorError("plugin list table headers are out of order")
    if any(header.count(label) != 1 for label in labels):
        raise DoctorError("plugin list table contains ambiguous headers")
    return offsets


def _slice_row(line: str, offsets: tuple[int, int, int, int]) -> tuple[str, ...]:
    plugin_at, status_at, version_at, path_at = offsets
    if len(line) <= path_at:
        return (line[plugin_at:status_at].strip(), "", "", "")
    return (
        line[plugin_at:status_at].strip(),
        line[status_at:version_at].strip(),
        line[version_at:path_at].strip(),
        line[path_at:].strip(),
    )


def parse_plugin_list(stdout: str) -> ParsedInstalledRow:
    """Parse the unique target using validated fixed-width table offsets."""

    if not isinstance(stdout, str):
        raise DoctorError("plugin list stdout must be decoded text")
    lines = stdout.splitlines()
    matches: list[ParsedInstalledRow] = []
    index = 0
    saw_marketplace = False

    while index < len(lines):
        marketplace = _marketplace_header(lines[index])
        if marketplace is None:
            if lines[index].strip():
                raise DoctorError(
                    f"unexpected content outside marketplace block at line {index + 1}"
                )
            index += 1
            continue

        saw_marketplace = True
        index += 1
        if index >= len(lines) or not lines[index].strip():
            raise DoctorError(f"marketplace {marketplace!r} is missing its manifest path")
        marketplace_path = lines[index].strip()
        index += 1
        while index < len(lines) and not lines[index].strip():
            index += 1
        if index >= len(lines) or _marketplace_header(lines[index]) is not None:
            raise DoctorError(f"marketplace {marketplace!r} is missing its plugin table")

        offsets = _column_offsets(lines[index])
        index += 1
        while index < len(lines) and lines[index].strip():
            if _marketplace_header(lines[index]) is not None:
                break
            plugin, status, version, plugin_path = _slice_row(lines[index], offsets)
            if plugin == TARGET:
                if marketplace != MARKETPLACE_NAME:
                    raise DoctorError(
                        f"target plugin row is under unexpected marketplace {marketplace!r}"
                    )
                if not all((status, version, plugin_path)):
                    raise DoctorError("target plugin row has an empty required cell")
                if not status.casefold().startswith("installed"):
                    raise DoctorError(f"target plugin is not installed: {status}")
                matches.append(
                    ParsedInstalledRow(
                        marketplace_path=marketplace_path,
                        plugin_path=plugin_path,
                        version=version,
                    )
                )
            index += 1

    if not saw_marketplace:
        raise DoctorError("plugin list contains no marketplace block")
    if len(matches) != 1:
        raise DoctorError(f"installed target count must be 1, observed {len(matches)}")
    return matches[0]


def observe_installed_from_list(stdout: str) -> PluginObservation:
    row = parse_plugin_list(stdout)
    skills = _scan_skills(Path(row.plugin_path) / "skills")
    return PluginObservation(
        marketplace_path=row.marketplace_path,
        plugin_path=row.plugin_path,
        version=row.version,
        skills=skills,
    )


def _codex_list_argv() -> list[str]:
    """Resolve the npm Windows shim without invoking PowerShell or a shell string."""

    if os.name == "nt":
        cmd_shim = shutil.which("codex.cmd")
        if cmd_shim is not None:
            return [cmd_shim, "plugin", "list"]
    return ["codex", "plugin", "list"]


def observe_installed_live(
    timeout_seconds: float, runner: RunCommand = subprocess.run
) -> PluginObservation:
    """Invoke the single allowed read-only command exactly once."""

    try:
        completed = runner(
            _codex_list_argv(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise DoctorError(
            f"codex plugin list timed out after {timeout_seconds:g} seconds"
        ) from None
    except (OSError, UnicodeError) as exc:
        raise DoctorError(f"codex plugin list execution failed: {exc}") from None
    if completed.returncode != 0:
        raise DoctorError(
            f"codex plugin list exited with code {completed.returncode}"
        )
    return observe_installed_from_list(completed.stdout)


def observe_installed_fixture(path: Path) -> PluginObservation:
    """Read an explicit deterministic observation; never substitute it for live mode."""

    path = path.absolute()
    fixture = _read_json_object(path, "installed fixture")
    marketplace_path = _required_string(fixture, "marketplace_path", "installed fixture")
    plugin_path = _required_string(fixture, "plugin_path", "installed fixture")
    version = _required_string(fixture, "version", "installed fixture")
    skills_value = fixture.get("skills")
    if (
        not isinstance(skills_value, list)
        or any(not isinstance(item, str) or not item for item in skills_value)
        or len(set(skills_value)) != len(skills_value)
    ):
        raise DoctorError("installed fixture.skills must be an array of unique strings")

    def fixture_path(value: str) -> str:
        candidate = Path(value)
        return str(candidate if candidate.is_absolute() else path.parent / candidate)

    return PluginObservation(
        marketplace_path=fixture_path(marketplace_path),
        plugin_path=fixture_path(plugin_path),
        version=version,
        skills=frozenset(skills_value),
    )


def _observation_json(observation: PluginObservation | None) -> dict[str, Any]:
    if observation is None:
        return {
            "marketplace_path": None,
            "plugin_path": None,
            "version": None,
            "skills": [],
        }
    return {
        "marketplace_path": observation.marketplace_path,
        "plugin_path": observation.plugin_path,
        "version": observation.version,
        "skills": sorted(observation.skills),
    }


def _payload(
    source: PluginObservation | None,
    installed: PluginObservation | None,
    drift: Drift | None,
    diagnostics: list[str],
    observation_mode: str,
    legacy_skill_surfaces: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    status = PASS if drift is not None and not drift.blocks and not diagnostics else BLOCKED
    return {
        "status": status,
        "plugin": PLUGIN_NAME,
        "marketplace": MARKETPLACE_NAME,
        "observation_mode": observation_mode,
        "source": _observation_json(source),
        "installed": _observation_json(installed),
        "drift": {
            "marketplace_path": None if drift is None else drift.marketplace_path,
            "plugin_path": None if drift is None else drift.plugin_path,
            "version": None if drift is None else drift.version,
            "missing_skills": [] if drift is None else list(drift.missing_skills),
            "extra_skills": [] if drift is None else list(drift.extra_skills),
        },
        "active_session": {
            "status": "NOT_CLAIMED",
            "reason": ACTIVE_SESSION_REASON,
        },
        "legacy_skill_surfaces": legacy_skill_surfaces or [],
        "diagnostics": diagnostics,
        "mutations_performed": False,
    }


def build_parser() -> JsonArgumentParser:
    methodology_root = Path(__file__).resolve().parent.parent
    parser = JsonArgumentParser(description="Read-only agent-workflow installation doctor")
    parser.add_argument(
        "--source-marketplace",
        type=Path,
        default=methodology_root / ".agents" / "plugins" / "marketplace.json",
    )
    parser.add_argument(
        "--legacy-skill-root",
        type=Path,
        action="append",
        help=(
            "direct global skill root to compare with source; repeatable. "
            "Live mode defaults to ~/.codex/skills and ~/.claude/skills"
        ),
    )
    parser.add_argument(
        "--source-plugin",
        type=Path,
        default=methodology_root / "plugins" / PLUGIN_NAME,
    )
    parser.add_argument(
        "--installed-fixture",
        type=Path,
        help="explicit installed observation JSON for deterministic tests; omitted means live mode",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser


def execute(
    argv: Sequence[str] | None = None, runner: RunCommand = subprocess.run
) -> tuple[dict[str, Any], int]:
    source: PluginObservation | None = None
    installed: PluginObservation | None = None
    legacy_skill_surfaces: list[dict[str, Any]] = []
    observation_mode = "live"
    try:
        args = build_parser().parse_args(argv)
        if not 0 < args.timeout <= 300:
            raise DoctorError("timeout must be greater than 0 and at most 300 seconds")
        observation_mode = "fixture" if args.installed_fixture is not None else "live"
        source = observe_source(args.source_marketplace, args.source_plugin)
        legacy_roots = args.legacy_skill_root
        if legacy_roots is None and args.installed_fixture is None:
            legacy_roots = [Path.home() / ".codex" / "skills", Path.home() / ".claude" / "skills"]
        legacy_skill_surfaces = observe_legacy_skill_surfaces(
            args.source_plugin, legacy_roots or []
        )
        installed = (
            observe_installed_fixture(args.installed_fixture)
            if args.installed_fixture is not None
            else observe_installed_live(args.timeout, runner)
        )
        drift = compare_observations(source, installed)
        diagnostics = [
            "legacy global skill content mismatch: "
            f"{item['surface_root']}/{item['skill']}"
            for item in legacy_skill_surfaces
            if item["sha_mismatch"]
        ]
        payload = _payload(
            source,
            installed,
            drift,
            diagnostics,
            observation_mode,
            legacy_skill_surfaces,
        )
        return payload, 0 if payload["status"] == PASS else 2
    except Exception as exc:  # Fail closed without exposing a traceback.
        message = str(exc).strip() or type(exc).__name__
        return (
            _payload(
                source,
                installed,
                None,
                [message],
                observation_mode,
                legacy_skill_surfaces,
            ),
            2,
        )


def main(argv: Sequence[str] | None = None) -> int:
    payload, exit_code = execute(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
