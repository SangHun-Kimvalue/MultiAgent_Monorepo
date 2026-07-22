from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
METHODOLOGY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

import workflow_doctor as doctor  # noqa: E402


SOURCE_VERSION = json.loads(
    (METHODOLOGY_ROOT / "plugins" / "agent-workflow" / ".codex-plugin" / "plugin.json").read_text(
        encoding="utf-8"
    )
)["version"]
SOURCE_SKILLS = {
    "cross-session-plan-review",
    "design-review-leg",
    "nitpicker-review",
    "phase-cycle-orchestrator",
    "phase0-discovery-interview",
    "phased-implementation-handoff",
    "prepare-session-compaction",
    "zrt-phase-commit",
}


def test_plugin_manifest_respects_default_prompt_limit() -> None:
    manifest_path = (
        METHODOLOGY_ROOT
        / "plugins"
        / "agent-workflow"
        / ".codex-plugin"
        / "plugin.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    default_prompts = manifest["interface"]["defaultPrompt"]

    assert isinstance(default_prompts, list)
    assert 1 <= len(default_prompts) <= 3


def test_claude_and_codex_plugin_base_versions_match() -> None:
    plugin_root = METHODOLOGY_ROOT / "plugins" / "agent-workflow"
    claude = json.loads(
        (plugin_root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    codex = json.loads(
        (plugin_root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )

    assert claude["version"] == codex["version"].split("+", 1)[0]


def test_current_workflow_doctor_fixture_tracks_source_version() -> None:
    fixture = json.loads(
        (
            METHODOLOGY_ROOT
            / "tests"
            / "fixtures"
            / "workflow_doctor"
            / "current.json"
        ).read_text(encoding="utf-8")
    )

    assert fixture["version"] == SOURCE_VERSION


STALE_SKILLS = SOURCE_SKILLS - {
    "design-review-leg",
    "phase-cycle-orchestrator",
    "prepare-session-compaction",
}


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _write_skills(plugin_path: Path, skills: set[str]) -> None:
    for skill in skills:
        skill_dir = plugin_path / "skills" / skill
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")


def _make_source(
    root: Path,
    *,
    skills: set[str] | None = None,
    version: str = SOURCE_VERSION,
    marketplace_plugin_path: str = "./plugins/agent-workflow",
    target_count: int = 1,
) -> tuple[Path, Path]:
    marketplace_path = root / ".agents" / "plugins" / "marketplace.json"
    plugin_path = root / "plugins" / doctor.PLUGIN_NAME
    selected_skills = SOURCE_SKILLS if skills is None else skills
    _write_skills(plugin_path, selected_skills)
    _write_json(
        plugin_path / ".codex-plugin" / "plugin.json",
        {
            "name": doctor.PLUGIN_NAME,
            "version": version,
            "skills": "./skills/",
        },
    )
    target = {
        "name": doctor.PLUGIN_NAME,
        "source": {"source": "local", "path": marketplace_plugin_path},
    }
    _write_json(
        marketplace_path,
        {
            "name": doctor.MARKETPLACE_NAME,
            "plugins": [dict(target) for _ in range(target_count)],
        },
    )
    return marketplace_path, plugin_path


def _make_installed_fixture(
    path: Path,
    *,
    marketplace_path: Path,
    plugin_path: Path,
    version: str,
    skills: set[str],
) -> Path:
    _write_json(
        path,
        {
            "marketplace_path": str(marketplace_path),
            "plugin_path": str(plugin_path),
            "version": version,
            "skills": sorted(skills),
        },
    )
    return path


def _execute_fixture(
    marketplace_path: Path, plugin_path: Path, fixture: Path
) -> tuple[dict[str, Any], int]:
    return doctor.execute(
        [
            "--source-marketplace",
            str(marketplace_path),
            "--source-plugin",
            str(plugin_path),
            "--installed-fixture",
            str(fixture),
        ]
    )


def _plugin_list_output(
    marketplace_path: Path,
    plugin_path: Path,
    *,
    version: str = SOURCE_VERSION,
    target: str = doctor.TARGET,
) -> str:
    status = "installed, enabled"
    plugin_width = max(len("PLUGIN"), len(target)) + 2
    status_width = max(len("STATUS"), len(status)) + 2
    version_width = max(len("VERSION"), len(version)) + 2
    header = (
        f"{'PLUGIN':<{plugin_width}}"
        f"{'STATUS':<{status_width}}"
        f"{'VERSION':<{version_width}}PATH"
    )
    row = (
        f"{target:<{plugin_width}}"
        f"{status:<{status_width}}"
        f"{version:<{version_width}}{plugin_path}"
    )
    return (
        f"Marketplace `{doctor.MARKETPLACE_NAME}`\n"
        f"{marketplace_path}\n\n{header}\n{row}\n"
    )


def test_matching_current_fixture_passes(tmp_path: Path) -> None:
    marketplace, plugin = _make_source(tmp_path / "source")
    fixture = _make_installed_fixture(
        tmp_path / "matching.json",
        marketplace_path=marketplace,
        plugin_path=plugin,
        version=SOURCE_VERSION,
        skills=SOURCE_SKILLS,
    )

    payload, exit_code = _execute_fixture(marketplace, plugin, fixture)

    assert exit_code == 0
    assert payload["status"] == "PASS"
    assert payload["observation_mode"] == "fixture"
    assert payload["drift"] == {
        "marketplace_path": False,
        "plugin_path": False,
        "version": False,
        "missing_skills": [],
        "extra_skills": [],
    }
    assert payload["active_session"]["status"] == "NOT_CLAIMED"
    assert payload["mutations_performed"] is False


def test_stale_fixture_reports_exact_drift_and_missing_skills(tmp_path: Path) -> None:
    marketplace, plugin = _make_source(tmp_path / "source")
    stale_marketplace = tmp_path / "legacy" / ".agents" / "plugins" / "marketplace.json"
    stale_plugin = tmp_path / "legacy" / "plugins" / doctor.PLUGIN_NAME
    _write_json(stale_marketplace, {"name": "legacy"})
    stale_plugin.mkdir(parents=True)
    stale_version = "1.0.0+codex.20260613013853"
    fixture = _make_installed_fixture(
        tmp_path / "stale.json",
        marketplace_path=stale_marketplace,
        plugin_path=stale_plugin,
        version=stale_version,
        skills=STALE_SKILLS,
    )

    payload, exit_code = _execute_fixture(marketplace, plugin, fixture)

    assert exit_code == 2
    assert payload["status"] == "BLOCKED"
    assert payload["installed"]["marketplace_path"] == str(stale_marketplace)
    assert payload["installed"]["plugin_path"] == str(stale_plugin)
    assert payload["installed"]["version"] == stale_version
    assert payload["drift"]["marketplace_path"] is True
    assert payload["drift"]["plugin_path"] is True
    assert payload["drift"]["version"] is True
    assert payload["drift"]["missing_skills"] == [
        "design-review-leg",
        "phase-cycle-orchestrator",
        "prepare-session-compaction",
    ]


def test_legacy_global_skill_mismatch_blocks_with_exact_hashes(tmp_path: Path) -> None:
    marketplace, plugin = _make_source(tmp_path / "source")
    fixture = _make_installed_fixture(
        tmp_path / "matching.json",
        marketplace_path=marketplace,
        plugin_path=plugin,
        version=SOURCE_VERSION,
        skills=SOURCE_SKILLS,
    )
    legacy_root = tmp_path / "global-skills"
    legacy_skill = legacy_root / "phased-implementation-handoff" / "SKILL.md"
    legacy_skill.parent.mkdir(parents=True)
    legacy_skill.write_text("# stale\n", encoding="utf-8")

    payload, exit_code = doctor.execute(
        [
            "--source-marketplace",
            str(marketplace),
            "--source-plugin",
            str(plugin),
            "--installed-fixture",
            str(fixture),
            "--legacy-skill-root",
            str(legacy_root),
        ]
    )

    assert exit_code == 2
    assert payload["status"] == "BLOCKED"
    assert len(payload["legacy_skill_surfaces"]) == 1
    observed = payload["legacy_skill_surfaces"][0]
    assert observed["skill"] == "phased-implementation-handoff"
    assert observed["sha_mismatch"] is True
    assert len(observed["source_sha256"]) == 64
    assert len(observed["installed_sha256"]) == 64
    assert "legacy global skill content mismatch" in payload["diagnostics"][0]


def test_matching_legacy_global_skill_is_non_blocking_but_visible(tmp_path: Path) -> None:
    marketplace, plugin = _make_source(tmp_path / "source")
    fixture = _make_installed_fixture(
        tmp_path / "matching.json",
        marketplace_path=marketplace,
        plugin_path=plugin,
        version=SOURCE_VERSION,
        skills=SOURCE_SKILLS,
    )
    legacy_root = tmp_path / "global-skills"
    source_skill = plugin / "skills" / "phased-implementation-handoff" / "SKILL.md"
    legacy_skill = legacy_root / "phased-implementation-handoff" / "SKILL.md"
    legacy_skill.parent.mkdir(parents=True)
    legacy_skill.write_bytes(source_skill.read_bytes())

    payload, exit_code = doctor.execute(
        [
            "--source-marketplace",
            str(marketplace),
            "--source-plugin",
            str(plugin),
            "--installed-fixture",
            str(fixture),
            "--legacy-skill-root",
            str(legacy_root),
        ]
    )

    assert exit_code == 0
    assert payload["status"] == "PASS"
    assert payload["legacy_skill_surfaces"][0]["sha_mismatch"] is False


def test_legacy_global_skill_tree_missing_asset_blocks(tmp_path: Path) -> None:
    marketplace, plugin = _make_source(tmp_path / "source")
    source_skill = plugin / "skills" / "phased-implementation-handoff"
    reference = source_skill / "references" / "prompt-skeleton.md"
    reference.parent.mkdir(parents=True)
    reference.write_text("canonical\n", encoding="utf-8")
    fixture = _make_installed_fixture(
        tmp_path / "matching.json",
        marketplace_path=marketplace,
        plugin_path=plugin,
        version=SOURCE_VERSION,
        skills=SOURCE_SKILLS,
    )
    legacy_root = tmp_path / "global-skills"
    installed_skill = legacy_root / source_skill.name
    shutil.copytree(source_skill, installed_skill)
    (installed_skill / "references" / "prompt-skeleton.md").unlink()

    payload, exit_code = doctor.execute(
        [
            "--source-marketplace",
            str(marketplace),
            "--source-plugin",
            str(plugin),
            "--installed-fixture",
            str(fixture),
            "--legacy-skill-root",
            str(legacy_root),
        ]
    )

    assert exit_code == 2
    observed = payload["legacy_skill_surfaces"][0]
    assert observed["hash_scope"] == "skill-tree"
    assert observed["orphan"] is False
    assert observed["sha_mismatch"] is True


def test_unmatched_legacy_global_skill_is_visible_as_nonblocking_orphan(
    tmp_path: Path,
) -> None:
    marketplace, plugin = _make_source(tmp_path / "source")
    fixture = _make_installed_fixture(
        tmp_path / "matching.json",
        marketplace_path=marketplace,
        plugin_path=plugin,
        version=SOURCE_VERSION,
        skills=SOURCE_SKILLS,
    )
    orphan_skill = tmp_path / "global-skills" / "retired-workflow" / "SKILL.md"
    orphan_skill.parent.mkdir(parents=True)
    orphan_skill.write_text("# retired\n", encoding="utf-8")

    payload, exit_code = doctor.execute(
        [
            "--source-marketplace",
            str(marketplace),
            "--source-plugin",
            str(plugin),
            "--installed-fixture",
            str(fixture),
            "--legacy-skill-root",
            str(orphan_skill.parents[1]),
        ]
    )

    assert exit_code == 0
    assert payload["status"] == "PASS"
    observed = payload["legacy_skill_surfaces"][0]
    assert observed["skill"] == "retired-workflow"
    assert observed["orphan"] is True
    assert observed["source_sha256"] is None
    assert observed["sha_mismatch"] is None


def test_source_marketplace_path_mismatch_is_blocked(tmp_path: Path) -> None:
    marketplace, plugin = _make_source(
        tmp_path / "source", marketplace_plugin_path="./plugins/other"
    )
    (tmp_path / "source" / "plugins" / "other").mkdir(parents=True)
    fixture = _make_installed_fixture(
        tmp_path / "unused.json",
        marketplace_path=marketplace,
        plugin_path=plugin,
        version=SOURCE_VERSION,
        skills=SOURCE_SKILLS,
    )

    payload, exit_code = _execute_fixture(marketplace, plugin, fixture)

    assert exit_code == 2
    assert payload["status"] == "BLOCKED"
    assert "source marketplace plugin path mismatch" in payload["diagnostics"][0]


@pytest.mark.parametrize("count", [0, 2])
def test_source_target_zero_or_duplicate_is_blocked(tmp_path: Path, count: int) -> None:
    marketplace, plugin = _make_source(tmp_path / "source", target_count=count)
    fixture = _make_installed_fixture(
        tmp_path / "unused.json",
        marketplace_path=marketplace,
        plugin_path=plugin,
        version=SOURCE_VERSION,
        skills=SOURCE_SKILLS,
    )

    payload, exit_code = _execute_fixture(marketplace, plugin, fixture)

    assert exit_code == 2
    assert payload["status"] == "BLOCKED"
    assert f"observed {count}" in payload["diagnostics"][0]


@pytest.mark.parametrize(
    "output, expected_count",
    [
        (
            "Marketplace `other`\nC:\\marketplace.json\n\n"
            "PLUGIN  STATUS  VERSION  PATH\n"
            "other@other  installed  1.0  C:\\other\n",
            0,
        ),
        (
            _plugin_list_output(Path("C:/one.json"), Path("C:/one"))
            + "\n"
            + _plugin_list_output(Path("C:/two.json"), Path("C:/two")),
            2,
        ),
    ],
)
def test_installed_target_zero_or_duplicate_is_blocked(
    output: str, expected_count: int
) -> None:
    with pytest.raises(doctor.DoctorError, match=f"observed {expected_count}"):
        doctor.parse_plugin_list(output)


@pytest.mark.parametrize("target_count", [0, 2])
def test_live_execute_target_zero_or_duplicate_is_blocked_json(
    tmp_path: Path, target_count: int
) -> None:
    marketplace, plugin = _make_source(tmp_path / "source")
    if target_count == 0:
        output = _plugin_list_output(
            marketplace,
            plugin,
            target="other@multiagent-methodology",
        )
    else:
        output = _plugin_list_output(marketplace, plugin)
        output += f"\n{_plugin_list_output(marketplace, plugin)}"
    calls = 0

    def list_runner(argv: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(argv, 0, output, "")

    payload, exit_code = doctor.execute(
        ["--source-marketplace", str(marketplace), "--source-plugin", str(plugin)],
        runner=list_runner,
    )

    rendered = json.dumps(payload, ensure_ascii=False)
    assert calls == 1
    assert exit_code == 2
    assert payload["status"] == "BLOCKED"
    assert payload["observation_mode"] == "live"
    assert f"observed {target_count}" in payload["diagnostics"][0]
    assert "Traceback" not in rendered


def test_malformed_manifest_returns_blocked_json_without_traceback(
    tmp_path: Path,
) -> None:
    marketplace, plugin = _make_source(tmp_path / "source")
    (plugin / ".codex-plugin" / "plugin.json").write_text("{bad", encoding="utf-8")

    payload, exit_code = doctor.execute(
        ["--source-marketplace", str(marketplace), "--source-plugin", str(plugin)]
    )

    rendered = json.dumps(payload, ensure_ascii=False)
    assert exit_code == 2
    assert payload["status"] == "BLOCKED"
    assert "Traceback" not in rendered


def test_malformed_list_output_returns_blocked_json_without_traceback(
    tmp_path: Path,
) -> None:
    marketplace, plugin = _make_source(tmp_path / "source")

    def malformed_runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args[0], 0, "Marketplace `broken`\npath\n", "")

    payload, exit_code = doctor.execute(
        ["--source-marketplace", str(marketplace), "--source-plugin", str(plugin)],
        runner=malformed_runner,
    )

    assert exit_code == 2
    assert payload["status"] == "BLOCKED"
    assert "Traceback" not in json.dumps(payload, ensure_ascii=False)


def test_extra_only_skill_is_diagnostic_but_not_blocking(tmp_path: Path) -> None:
    marketplace, plugin = _make_source(tmp_path / "source")
    fixture = _make_installed_fixture(
        tmp_path / "extra.json",
        marketplace_path=marketplace,
        plugin_path=plugin,
        version=SOURCE_VERSION,
        skills=SOURCE_SKILLS | {"future-skill"},
    )

    payload, exit_code = _execute_fixture(marketplace, plugin, fixture)

    assert exit_code == 0
    assert payload["status"] == "PASS"
    assert payload["observation_mode"] == "fixture"
    assert payload["drift"]["extra_skills"] == ["future-skill"]


def test_live_mode_invokes_only_read_only_list_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marketplace, plugin = _make_source(tmp_path / "source")
    calls: list[tuple[Any, dict[str, Any]]] = []
    output = _plugin_list_output(marketplace, plugin)

    def recording_runner(argv: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(
        doctor, "_codex_list_argv", lambda: ["codex", "plugin", "list"]
    )
    payload, exit_code = doctor.execute(
        [
            "--source-marketplace",
            str(marketplace),
            "--source-plugin",
            str(plugin),
            "--legacy-skill-root",
            str(tmp_path / "absent-global-skills"),
        ],
        runner=recording_runner,
    )

    assert exit_code == 0
    assert payload["status"] == "PASS"
    assert payload["observation_mode"] == "live"
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == ["codex", "plugin", "list"]
    assert kwargs == {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": 30.0,
        "check": False,
    }


def test_windows_codex_resolution_prefers_cmd_shim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor.os, "name", "nt")
    monkeypatch.setattr(
        doctor.shutil, "which", lambda name: r"C:\npm\codex.cmd" if name == "codex.cmd" else None
    )

    assert doctor._codex_list_argv() == [r"C:\npm\codex.cmd", "plugin", "list"]


def test_fixed_width_parser_preserves_space_path_and_plus_version() -> None:
    marketplace = Path(r"C:\Workspace With Spaces\.agents\plugins\marketplace.json")
    plugin = Path(r"C:\Workspace With Spaces\plugins\agent workflow")
    output = _plugin_list_output(
        marketplace, plugin, version="1.2.0+codex.20260719214348"
    )

    parsed = doctor.parse_plugin_list(output)

    assert parsed.marketplace_path == str(marketplace)
    assert parsed.plugin_path == str(plugin)
    assert parsed.version == "1.2.0+codex.20260719214348"


@pytest.mark.parametrize(
    "exception",
    [
        UnicodeDecodeError("utf-8", b"x", 0, 1, "bad byte"),
        RuntimeError("unexpected runner failure"),
    ],
)
def test_subprocess_decode_or_unexpected_exception_is_blocked_json(
    tmp_path: Path, exception: Exception
) -> None:
    marketplace, plugin = _make_source(tmp_path / "source")

    def failing_runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise exception

    payload, exit_code = doctor.execute(
        ["--source-marketplace", str(marketplace), "--source-plugin", str(plugin)],
        runner=failing_runner,
    )

    assert exit_code == 2
    assert payload["status"] == "BLOCKED"
    assert "Traceback" not in json.dumps(payload, ensure_ascii=False)


@pytest.mark.skipif(os.name != "nt", reason="Windows path equivalence contract")
def test_case_separator_and_trailing_separator_differences_pass(tmp_path: Path) -> None:
    marketplace, plugin = _make_source(tmp_path / "Source Tree")
    installed_marketplace = str(marketplace).upper().replace("\\", "/") + "/"
    installed_plugin = str(plugin).upper().replace("\\", "/") + "/"
    source = doctor.PluginObservation(
        str(marketplace), str(plugin), SOURCE_VERSION, frozenset(SOURCE_SKILLS)
    )
    installed = doctor.PluginObservation(
        installed_marketplace,
        installed_plugin,
        SOURCE_VERSION,
        frozenset(SOURCE_SKILLS),
    )

    drift = doctor.compare_observations(source, installed)

    assert drift.blocks is False


def test_live_timeout_is_blocked_json_with_exit_2(tmp_path: Path) -> None:
    marketplace, plugin = _make_source(tmp_path / "source")

    def timeout_runner(argv: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    payload, exit_code = doctor.execute(
        [
            "--source-marketplace",
            str(marketplace),
            "--source-plugin",
            str(plugin),
            "--timeout",
            "0.25",
        ],
        runner=timeout_runner,
    )

    assert exit_code == 2
    assert payload["status"] == "BLOCKED"
    assert "timed out after 0.25 seconds" in payload["diagnostics"][0]


def test_cli_emits_single_json_line_and_no_traceback(tmp_path: Path) -> None:
    marketplace, plugin = _make_source(tmp_path / "source")
    (plugin / ".codex-plugin" / "plugin.json").write_text("[]", encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(TOOLS_DIR / "workflow_doctor.py"),
            "--source-marketplace",
            str(marketplace),
            "--source-plugin",
            str(plugin),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        check=False,
    )

    lines = completed.stdout.splitlines()
    assert completed.returncode == 2
    assert len(lines) == 1
    assert json.loads(lines[-1])["status"] == "BLOCKED"
    assert completed.stderr == ""
    assert "Traceback" not in completed.stdout
