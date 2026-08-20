from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
METHODOLOGY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

import p5_fresh_discovery as fresh  # noqa: E402


def _write_plugin(root: Path, *, marker: str = "same") -> Path:
    plugin = root / "plugins" / fresh.PLUGIN_NAME
    for manifest in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
        path = plugin / manifest
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"name": fresh.PLUGIN_NAME, "version": fresh.PLUGIN_VERSION}),
            encoding="utf-8",
        )
    for skill in fresh.EXPECTED_SKILLS:
        skill_path = plugin / "skills" / skill / "SKILL.md"
        agent_path = plugin / "skills" / skill / "agents" / "openai.yaml"
        agent_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(f"# {skill}\n{marker}\n", encoding="utf-8")
        agent_path.write_text("interface:\n  display_name: test\n", encoding="utf-8")
    return plugin


def _claude_payload(install_path: Path, **overrides: Any) -> str:
    row: dict[str, Any] = {
        "id": fresh.PLUGIN_ID,
        "version": fresh.PLUGIN_VERSION,
        "enabled": True,
        "installPath": str(install_path),
    }
    row.update(overrides)
    return json.dumps([row])


def _codex_payload(source_path: Path, **overrides: Any) -> str:
    row: dict[str, Any] = {
        "pluginId": fresh.PLUGIN_ID,
        "name": fresh.PLUGIN_NAME,
        "marketplaceName": fresh.MARKETPLACE_NAME,
        "version": fresh.PLUGIN_VERSION,
        "installed": True,
        "enabled": True,
        "source": {"source": "local", "path": str(source_path)},
    }
    row.update(overrides)
    return json.dumps({"installed": [row], "available": []})


def test_path_guard_accepts_only_strict_temp_descendants(tmp_path: Path) -> None:
    child = tmp_path / "inside" / "config"
    assert fresh.require_descendant(child, tmp_path, "config") == child.resolve()
    with pytest.raises(fresh.EvidenceError, match="outside isolated temp root"):
        fresh.require_descendant(tmp_path, tmp_path, "config")
    with pytest.raises(fresh.EvidenceError, match="outside isolated temp root"):
        fresh.require_descendant(tmp_path.parent / "outside", tmp_path, "config")


def test_inventory_excludes_python_cache_and_reports_all_parity_axes(tmp_path: Path) -> None:
    expected_root = tmp_path / "expected"
    actual_root = tmp_path / "actual"
    expected_root.mkdir()
    actual_root.mkdir()
    (expected_root / "missing.txt").write_text("missing", encoding="utf-8")
    (expected_root / "changed.txt").write_text("old", encoding="utf-8")
    (actual_root / "extra.txt").write_text("extra", encoding="utf-8")
    (actual_root / "changed.txt").write_text("new", encoding="utf-8")
    cache = actual_root / "__pycache__" / "ignored.pyc"
    cache.parent.mkdir()
    cache.write_bytes(b"ignored")

    diff = fresh.compare_inventories(
        fresh.inventory_tree(expected_root), fresh.inventory_tree(actual_root)
    )

    assert diff == {
        "missing": ["missing.txt"],
        "extra": ["extra.txt"],
        "changed": ["changed.txt"],
        "match": False,
    }


@pytest.mark.parametrize(
    ("before", "after", "axis"),
    [
        ({"a": "1"}, {}, "missing"),
        ({}, {"a": "1"}, "extra"),
        ({"a": "1"}, {"a": "2"}, "changed"),
    ],
)
def test_protected_state_each_single_axis_blocks(
    before: dict[str, str], after: dict[str, str], axis: str
) -> None:
    old = {"protected": {"state": "DIRECTORY", "inventory": before}}
    new = {"protected": {"state": "DIRECTORY", "inventory": after}}

    diff = fresh.compare_protected_state(old, new)

    assert diff["match"] is False
    assert diff[axis]
    assert not any(diff[name] for name in {"missing", "extra", "changed"} - {axis})


@pytest.mark.parametrize(
    ("before_bytes", "after_bytes", "axis"),
    [
        (None, b"added", "extra"),
        (b"removed", None, "missing"),
        (b"old", b"new", "changed"),
    ],
)
def test_protected_state_detects_python_cache_byte_mutations(
    tmp_path: Path, before_bytes: bytes | None, after_bytes: bytes | None, axis: str
) -> None:
    protected = tmp_path / "protected"
    cache = protected / "__pycache__" / "state.pyc"
    cache.parent.mkdir(parents=True)
    if before_bytes is not None:
        cache.write_bytes(before_bytes)
    before = {"protected": fresh.inventory_protected_path(protected)}

    if after_bytes is None:
        cache.unlink()
    else:
        cache.write_bytes(after_bytes)
    after = {"protected": fresh.inventory_protected_path(protected)}

    diff = fresh.compare_protected_state(before, after)

    assert diff["match"] is False
    assert diff[axis] == ["protected/__pycache__/state.pyc"]


def test_claude_list_requires_exact_target_and_temp_install_path(tmp_path: Path) -> None:
    config_root = tmp_path / "claude"
    install_path = config_root / "plugins" / fresh.PLUGIN_NAME
    install_path.mkdir(parents=True)

    assert fresh.parse_claude_list(_claude_payload(install_path), config_root) == install_path.resolve()

    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(fresh.EvidenceError, match="outside isolated temp root"):
        fresh.parse_claude_list(_claude_payload(outside), config_root)
    duplicate = json.loads(_claude_payload(install_path)) * 2
    with pytest.raises(fresh.EvidenceError, match="observed 2"):
        fresh.parse_claude_list(json.dumps(duplicate), config_root)


@pytest.mark.parametrize(
    "payload",
    [
        "{}",
        "not-json",
        json.dumps([]),
        json.dumps([{"id": fresh.PLUGIN_ID, "version": "0.0.0", "enabled": True}]),
        json.dumps(
            [
                {
                    "id": fresh.PLUGIN_ID,
                    "version": fresh.PLUGIN_VERSION,
                    "enabled": "true",
                    "installPath": "x",
                }
            ]
        ),
    ],
)
def test_claude_malformed_or_mismatched_list_is_blocked(
    tmp_path: Path, payload: str
) -> None:
    with pytest.raises(fresh.EvidenceError):
        fresh.parse_claude_list(payload, tmp_path / "claude")


def test_codex_list_requires_registered_extraction_root_and_state(tmp_path: Path) -> None:
    extract_root = tmp_path / "extract"
    plugin = _write_plugin(extract_root)

    assert fresh.parse_codex_list(_codex_payload(plugin), extract_root) == plugin.resolve()

    with pytest.raises(fresh.EvidenceError, match="enabled mismatch"):
        fresh.parse_codex_list(_codex_payload(plugin, enabled=False), extract_root)
    duplicate = json.loads(_codex_payload(plugin))
    duplicate["installed"] *= 2
    with pytest.raises(fresh.EvidenceError, match="observed 2"):
        fresh.parse_codex_list(json.dumps(duplicate), extract_root)


@pytest.mark.parametrize(
    "payload",
    [
        "[]",
        "not-json",
        json.dumps({"installed": "bad"}),
        json.dumps({"installed": []}),
    ],
)
def test_codex_unknown_or_malformed_list_is_blocked(tmp_path: Path, payload: str) -> None:
    with pytest.raises(fresh.EvidenceError):
        fresh.parse_codex_list(payload, tmp_path / "extract")


def test_codex_cache_no_cache_is_not_applicable(tmp_path: Path) -> None:
    extraction = _write_plugin(tmp_path / "extract-root")

    result = fresh.evaluate_codex_cache(extraction, None)

    assert result["status"] == "NOT_APPLICABLE"
    assert result["parity"] is None


def test_codex_distinct_cache_requires_byte_parity(tmp_path: Path) -> None:
    extraction = _write_plugin(tmp_path / "extract-root")
    cache = tmp_path / "codex-home" / "cache" / fresh.PLUGIN_NAME
    shutil.copytree(extraction, cache)

    result = fresh.evaluate_codex_cache(extraction, cache)

    assert result["status"] == "PASS"
    assert result["parity"]["match"] is True
    (cache / "skills" / "daily-ai-news" / "SKILL.md").write_text(
        "changed", encoding="utf-8"
    )
    with pytest.raises(fresh.EvidenceError, match="differs"):
        fresh.evaluate_codex_cache(extraction, cache)


def test_codex_cache_self_comparison_is_rejected(tmp_path: Path) -> None:
    extraction = _write_plugin(tmp_path / "extract-root")
    with pytest.raises(fresh.EvidenceError, match="self-comparison"):
        fresh.evaluate_codex_cache(extraction, extraction)


def test_codex_versioned_canonical_cache_is_discovered(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    cache = (
        codex_home
        / "plugins"
        / "cache"
        / fresh.MARKETPLACE_NAME
        / fresh.PLUGIN_NAME
        / fresh.PLUGIN_VERSION
    )
    plugin = _write_plugin(tmp_path / "cache-fixture")
    shutil.copytree(plugin, cache)

    result = fresh.find_distinct_codex_cache(
        codex_home, tmp_path / "extraction" / fresh.PLUGIN_NAME
    )

    assert result == cache.resolve()


def test_codex_canonical_cache_missing_manifest_is_blocked(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    cache = (
        codex_home
        / "plugins"
        / "cache"
        / fresh.MARKETPLACE_NAME
        / fresh.PLUGIN_NAME
        / fresh.PLUGIN_VERSION
    )
    cache.mkdir(parents=True)

    with pytest.raises(fresh.EvidenceError, match="cache manifest is missing"):
        fresh.find_distinct_codex_cache(
            codex_home, tmp_path / "extraction" / fresh.PLUGIN_NAME
        )


@pytest.mark.parametrize("contents", [b"{bad", b"\xff"])
def test_codex_canonical_cache_manifest_errors_are_blocked(
    tmp_path: Path, contents: bytes
) -> None:
    codex_home = tmp_path / "codex-home"
    manifest = (
        codex_home
        / "plugins"
        / "cache"
        / fresh.MARKETPLACE_NAME
        / fresh.PLUGIN_NAME
        / fresh.PLUGIN_VERSION
        / ".codex-plugin"
        / "plugin.json"
    )
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(contents)

    with pytest.raises(fresh.EvidenceError, match="cache manifest is unreadable"):
        fresh.find_distinct_codex_cache(codex_home, tmp_path / "extraction" / fresh.PLUGIN_NAME)


def test_codex_canonical_cache_manifest_name_mismatch_is_blocked(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    manifest = (
        codex_home
        / "plugins"
        / "cache"
        / fresh.MARKETPLACE_NAME
        / fresh.PLUGIN_NAME
        / fresh.PLUGIN_VERSION
        / ".codex-plugin"
        / "plugin.json"
    )
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"name": "other-plugin"}), encoding="utf-8")

    with pytest.raises(fresh.EvidenceError, match="cache manifest name mismatch"):
        fresh.find_distinct_codex_cache(codex_home, tmp_path / "extraction" / fresh.PLUGIN_NAME)


def test_codex_unrelated_cache_manifest_remains_out_of_scope(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    manifest = (
        codex_home
        / "plugins"
        / "cache"
        / fresh.MARKETPLACE_NAME
        / "other-plugin"
        / fresh.PLUGIN_VERSION
        / ".codex-plugin"
        / "plugin.json"
    )
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(b"{bad")

    assert (
        fresh.find_distinct_codex_cache(codex_home, tmp_path / "extraction" / fresh.PLUGIN_NAME)
        is None
    )


def test_plugin_inventory_requires_exact_two_skills_and_agents(tmp_path: Path) -> None:
    plugin = _write_plugin(tmp_path / "root")
    inventory = fresh.assert_plugin_inventory(plugin, ".codex-plugin/plugin.json")
    assert {path.split("/")[1] for path in inventory if path.endswith("/SKILL.md")} == set(
        fresh.EXPECTED_SKILLS
    )

    extra = plugin / "skills" / "extra" / "SKILL.md"
    extra.parent.mkdir()
    extra.write_text("extra", encoding="utf-8")
    with pytest.raises(fresh.EvidenceError, match="skill inventory mismatch"):
        fresh.assert_plugin_inventory(plugin, ".codex-plugin/plugin.json")


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash unavailable")
def test_forced_archive_records_stdlib_zip_provenance(tmp_path: Path) -> None:
    _artifact, extract_root, provenance = fresh.build_forced_archive(
        METHODOLOGY_ROOT, tmp_path, 120.0, subprocess.run
    )

    assert provenance["backend"] == "python-stdlib-zipfile"
    assert Path(provenance["python_executable"]).resolve() == Path(sys.executable).resolve()
    zipfile_module = Path(provenance["zipfile_module"])
    assert zipfile_module.is_file()
    assert "zipfile" in zipfile_module.as_posix()
    assert provenance["python_version_argv"] == [sys.executable, "--version"]
    assert provenance["package_exit_code"] == 0
    assert provenance["package_argv"][-1].endswith("package.sh")
    assert len(provenance["artifact_sha256"]) == 64
    assert (extract_root / ".claude-plugin" / "marketplace.json").is_file()
    assert (extract_root / ".agents" / "plugins" / "marketplace.json").is_file()
    assert (extract_root / "tools" / "workflow_doctor.py").is_file()


def test_isolated_run_uses_exact_cli_argv_and_no_cache_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    methodology = tmp_path / "methodology"
    methodology.mkdir()
    (methodology / "package.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    source_plugin = _write_plugin(methodology)
    protected_home = tmp_path / "protected-home"
    protected_home.mkdir()
    evidence_dir = tmp_path / "evidence"
    calls: list[tuple[list[str], dict[str, str]]] = []
    roots: dict[str, Path] = {}

    def fake_build(
        _methodology_root: Path,
        temp_root: Path,
        _timeout: float,
        _runner: fresh.RunCommand,
    ) -> tuple[Path, Path, dict[str, Any]]:
        extract_root = temp_root / "forced-extraction"
        shutil.copytree(source_plugin, extract_root / "plugins" / fresh.PLUGIN_NAME)
        archive = temp_root / "forced.zip"
        archive.write_bytes(b"fixture")
        roots["extract"] = extract_root
        return archive, extract_root, {
            "backend": "python-stdlib-zipfile",
            "artifact_sha256": fresh.sha256_file(archive),
        }

    monkeypatch.setattr(fresh, "build_forced_archive", fake_build)
    monkeypatch.setattr(fresh, "_resolved_command", lambda command: command)

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        env = kwargs["env"]
        calls.append((argv, env))
        stdout = "{}"
        if argv[:3] == ["claude", "plugin", "list"]:
            claude_cache = Path(env["CLAUDE_CONFIG_DIR"]) / "plugins" / fresh.PLUGIN_NAME
            shutil.copytree(source_plugin, claude_cache)
            stdout = _claude_payload(claude_cache)
        elif argv[:3] == ["codex", "plugin", "list"]:
            stdout = _codex_payload(roots["extract"] / "plugins" / fresh.PLUGIN_NAME)
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    config = fresh.FreshConfig(
        methodology_root=methodology,
        evidence_dir=evidence_dir,
        protected_home=protected_home,
        temp_parent=tmp_path,
        claude_command="claude",
        codex_command="codex",
    )
    payload, exit_code = fresh.run_fresh_discovery(config, runner)

    assert exit_code == 0
    assert payload["status"] == "PASS"
    assert payload["protected_state"]["diff"]["match"] is True
    assert payload["codex"]["distinct_cache"]["status"] == "NOT_APPLICABLE"
    argv_list = [argv for argv, _env in calls]
    extract = roots["extract"]
    assert ["codex", "plugin", "marketplace", "add", str(extract), "--json"] in argv_list
    assert ["codex", "plugin", "add", fresh.PLUGIN_ID, "--json"] in argv_list
    assert [
        "codex",
        "plugin",
        "list",
        "--json",
        "--marketplace",
        fresh.MARKETPLACE_NAME,
    ] in argv_list
    assert all("model" not in argv for argv in argv_list)
    for _argv, env in calls:
        if "CLAUDE_CONFIG_DIR" in env:
            assert Path(env["CLAUDE_CONFIG_DIR"]).is_relative_to(tmp_path)
        if "CODEX_HOME" in env:
            assert Path(env["CODEX_HOME"]).is_relative_to(tmp_path)
