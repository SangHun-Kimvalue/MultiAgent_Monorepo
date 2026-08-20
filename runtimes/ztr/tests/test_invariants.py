"""ztr invariants 엔진 테스트."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.engine.invariants import ChangedPath, InvariantEngine
from src.engine.static_review import ToolRun
from src.envelope import Verdict


async def test_invariants_pass_on_clean_repo(tmp_path: Path) -> None:
    _init_fixture_repo(tmp_path)

    report = await InvariantEngine(root=tmp_path).run()

    assert report.verdict == Verdict.PASS
    assert {check.name for check in report.checks} == {
        "git_changed_paths",
        "lessons_append",
        "roadmap_phase_record",
        "phase_prompt_exists",
        "m2_finding_terms",
        "not_claimed_text",
        "runner_envelope_contract",
    }


async def test_changed_paths_are_normalized_when_ztr_is_subtree(tmp_path: Path) -> None:
    mono_root = tmp_path / "mono"
    ztr_root = mono_root / "runtimes" / "ztr"
    _write_minimal_tree(ztr_root)
    subprocess.run(["git", "init"], cwd=mono_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=mono_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=mono_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", "."], cwd=mono_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=mono_root,
        check=True,
        capture_output=True,
    )
    (ztr_root / "src" / "engine" / "invariants.py").write_text(
        "# changed in subtree\n",
        encoding="utf-8",
    )

    report = await InvariantEngine(root=ztr_root).run()

    assert any(
        item.path == "src/engine/invariants.py"
        for item in report.changed_paths
    )
    assert all(
        not item.path.startswith("runtimes/ztr/")
        for item in report.changed_paths
    )


async def test_git_changed_paths_failure_is_blocked_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.engine import invariants as invariants_mod

    async def fake_run(*_: object, **__: object) -> ToolRun:
        return ToolRun(
            command=("git",),
            exit_code=2,
            stdout="",
            stderr_sanitized="fatal: not a git repository",
            duration_s=0.01,
        )

    _write_minimal_tree(tmp_path)
    monkeypatch.setattr(invariants_mod, "run_subprocess_tool", fake_run)

    report = await InvariantEngine(root=tmp_path).run()

    assert report.verdict == Verdict.BLOCKED
    assert report.checks[0].name == "git_changed_paths"
    assert report.checks[0].issues[0].severity == "blocker"


def test_lessons_duplicate_is_changes_requested(tmp_path: Path) -> None:
    _write_minimal_tree(tmp_path)
    lessons = tmp_path / "docs" / "LESSONS_LEARNED.md"
    lessons.write_text(
        "## LESSON-001: A\n\n## LESSON-001: B\n",
        encoding="utf-8",
    )

    check = InvariantEngine(root=tmp_path)._check_lessons([
        ChangedPath("docs/LESSONS_LEARNED.md", "M")
    ])

    assert check.status == Verdict.CHANGES_REQUESTED
    assert any("중복" in issue.finding for issue in check.issues)


def test_lessons_git_show_exception_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_run(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        raise OSError("git unavailable")

    _write_minimal_tree(tmp_path)
    monkeypatch.setattr(subprocess, "run", broken_run)

    check = InvariantEngine(root=tmp_path)._check_lessons([
        ChangedPath("docs/LESSONS_LEARNED.md", "M")
    ])

    assert check.status == Verdict.BLOCKED
    assert check.issues[0].severity == "blocker"


def test_roadmap_required_for_phase_work(tmp_path: Path) -> None:
    _write_minimal_tree(tmp_path)

    check = InvariantEngine(root=tmp_path)._check_roadmap_updated([
        ChangedPath("src/engine/invariants.py", "A")
    ])

    assert check.status == Verdict.CHANGES_REQUESTED
    assert "ROADMAP" in check.issues[0].finding


def test_phase_prompt_missing_is_changes_requested(tmp_path: Path) -> None:
    _write_minimal_tree(tmp_path)
    (tmp_path / "docs" / "prompts" / "v2_phase4.md").unlink()

    check = InvariantEngine(root=tmp_path)._check_phase_prompt_exists()

    assert check.status == Verdict.CHANGES_REQUESTED
    assert "프롬프트" in check.issues[0].finding


def test_phase_prompt_suffix_file_is_allowed(tmp_path: Path) -> None:
    _write_minimal_tree(tmp_path)
    (tmp_path / "docs" / "ROADMAP_V2.md").write_text(
        "## 3. 다음 Phase 상세 - Phase 7\n\nNOT CLAIMED\n",
        encoding="utf-8",
    )
    (tmp_path / "docs" / "prompts" / "v2_phase4.md").unlink()
    (tmp_path / "docs" / "prompts" / "v2_phase7_e2e.md").write_text(
        "finding fields: severity finding evidence_or_repro impact recommendation\n",
        encoding="utf-8",
    )

    check = InvariantEngine(root=tmp_path)._check_phase_prompt_exists()

    assert check.status == Verdict.PASS
    assert check.evidence["paths"] == ["docs/prompts/v2_phase7_e2e.md"]


def test_phase_prompt_does_not_match_other_phase_prefix(tmp_path: Path) -> None:
    _write_minimal_tree(tmp_path)
    (tmp_path / "docs" / "ROADMAP_V2.md").write_text(
        "## 3. 다음 Phase 상세 - Phase 7\n\nNOT CLAIMED\n",
        encoding="utf-8",
    )
    (tmp_path / "docs" / "prompts" / "v2_phase4.md").unlink()
    (tmp_path / "docs" / "prompts" / "v2_phase70.md").write_text(
        "finding fields: severity finding evidence_or_repro impact recommendation\n",
        encoding="utf-8",
    )

    check = InvariantEngine(root=tmp_path)._check_phase_prompt_exists()

    assert check.status == Verdict.CHANGES_REQUESTED


def test_m2_terms_missing_is_changes_requested(tmp_path: Path) -> None:
    _write_minimal_tree(tmp_path)
    prompt = tmp_path / "docs" / "prompts" / "v2_phase4.md"
    prompt.write_text("finding 포맷은 severity만 둔다.\n", encoding="utf-8")

    check = InvariantEngine(root=tmp_path)._check_m2_terms([
        ChangedPath("docs/prompts/v2_phase4.md", "M")
    ])

    assert check.status == Verdict.CHANGES_REQUESTED
    assert "evidence_or_repro" in check.issues[0].finding


def test_m2_terms_ignore_test_code_fixtures(tmp_path: Path) -> None:
    _write_minimal_tree(tmp_path)
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_recording_cli.py").write_text(
        "report = {'findings': []}\n",
        encoding="utf-8",
    )

    check = InvariantEngine(root=tmp_path)._check_m2_terms([
        ChangedPath("tests/test_recording_cli.py", "A")
    ])

    assert check.status == Verdict.PASS


def test_not_claimed_missing_is_changes_requested(tmp_path: Path) -> None:
    _write_minimal_tree(tmp_path)
    (tmp_path / "docs" / "ROADMAP_V2.md").write_text(
        "## 3. 다음 Phase 상세 - Phase 4\n",
        encoding="utf-8",
    )
    (tmp_path / "docs" / "discovery" / "ztr-v2" / "validation_plan.md").write_text(
        "PASS only\n",
        encoding="utf-8",
    )

    check = InvariantEngine(root=tmp_path)._check_not_claimed_text()

    assert check.status == Verdict.CHANGES_REQUESTED
    assert "NOT CLAIMED" in check.issues[0].finding


def test_runner_contract_missing_is_changes_requested(tmp_path: Path) -> None:
    _write_minimal_tree(tmp_path)
    (tmp_path / "src" / "runner.py").write_text("print('no envelope')\n", encoding="utf-8")

    check = InvariantEngine(root=tmp_path)._check_runner_envelope_contract()

    assert check.status == Verdict.CHANGES_REQUESTED
    assert "Envelope" in check.issues[0].finding


def test_runner_contract_checks_cmd_invariants_body(tmp_path: Path) -> None:
    _write_minimal_tree(tmp_path)
    (tmp_path / "src" / "runner.py").write_text(
        "def helper():\n"
        "    Envelope.from_verdict()\n"
        "    envelope.as_stdout_payload()\n"
        "    sys.exit(0)\n"
        "async def cmd_invariants(args):\n"
        "    return None\n"
        "sub.add_parser(\"invariants\")\n",
        encoding="utf-8",
    )

    check = InvariantEngine(root=tmp_path)._check_runner_envelope_contract()

    assert check.status == Verdict.CHANGES_REQUESTED
    assert "Envelope.from_verdict" in check.issues[0].finding


def _init_fixture_repo(root: Path) -> None:
    _write_minimal_tree(root)
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _write_minimal_tree(root: Path) -> None:
    (root / "docs" / "prompts").mkdir(parents=True)
    (root / "docs" / "discovery" / "ztr-v2").mkdir(parents=True)
    (root / "src" / "engine").mkdir(parents=True)
    (root / "docs" / "LESSONS_LEARNED.md").write_text(
        "## LESSON-001: A\n\n## LESSON-002: B\n",
        encoding="utf-8",
    )
    (root / "docs" / "ROADMAP_V2.md").write_text(
        "## 3. 다음 Phase 상세 - Phase 4\n\nNOT CLAIMED\n",
        encoding="utf-8",
    )
    (root / "docs" / "prompts" / "v2_phase4.md").write_text(
        "finding fields: severity finding evidence_or_repro impact recommendation\n",
        encoding="utf-8",
    )
    (root / "docs" / "discovery" / "ztr-v2" / "validation_plan.md").write_text(
        "NOT CLAIMED\n",
        encoding="utf-8",
    )
    (root / "src" / "runner.py").write_text(
        "async def cmd_invariants():\n"
        "    envelope = Envelope.from_verdict()\n"
        "    print(envelope.as_stdout_payload())\n"
        "    sys.exit(envelope.exit_code)\n"
        "sub.add_parser(\"invariants\")\n",
        encoding="utf-8",
    )
