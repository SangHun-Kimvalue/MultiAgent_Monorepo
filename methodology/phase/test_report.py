from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from methodology.phase import report
from methodology.phase.report import project_report, promote, render_template

BASE_SHA = "a" * 40
TEMPLATE_NAME = "phase-plan.v1"


def _values(**updates: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "phase_id": "T10-O1c",
        "base_sha": BASE_SHA,
        "allowed_paths": ["methodology/phase/report.py"],
        "forbidden_paths": [],
        "dod": "All deterministic gates pass.",
        "not_claimed": ["No runtime wiring."],
    }
    values.update(updates)
    return values


def _verdict(
    seq: int,
    stage: str,
    subject: str,
    verdict: str = "PASS",
    marker: str | None = None,
) -> dict[str, Any]:
    return {
        "seq": seq,
        "kind": "verdict",
        "stage": stage,
        "subject": subject,
        "verdict": verdict,
        "attempt_id": marker or f"attempt-{seq}",
    }


def _write_manifest(
    directory: Path,
    entries: list[dict[str, Any]],
    phase_id: str = "phase-safe",
    name: str = "manifest.json",
) -> Path:
    path = directory / name
    value = {
        "schema_version": "t10-o1.phase-manifest.v1",
        "phase_id": phase_id,
        "base_sha": BASE_SHA,
        "created_at_utc": "2026-08-20T00:00:00Z",
        "entries": entries,
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _passing_entries() -> list[dict[str, Any]]:
    return [
        _verdict(1, "final_verification", "mechanical"),
        _verdict(2, "final_verification", "test"),
        _verdict(3, "final_verification", "integration"),
    ]


def _project(tmp_path: Path, entries: list[dict[str, Any]]) -> tuple[int, str]:
    manifest = _write_manifest(tmp_path, entries)
    output = tmp_path / "PHASE_REPORT.md"
    code = project_report(manifest, output)
    return code, output.read_text(encoding="utf-8") if output.exists() else ""


def _section(markdown: str, title: str) -> str:
    start = markdown.index(f"## {title}")
    next_header = markdown.find("\n## ", start + 3)
    return markdown[start : next_header if next_header >= 0 else len(markdown)]


def _last_stdout_json(stdout: str) -> dict[str, Any]:
    return json.loads(stdout.strip().splitlines()[-1])


class TestGroup01RequiredTokens:
    @pytest.mark.parametrize(
        "updates",
        [
            {"remove": "phase_id"},
            {"allowed_paths": []},
            {"not_claimed": []},
            {"dod": ""},
        ],
    )
    def test_missing_and_empty_required_values(self, tmp_path: Path, updates: dict[str, Any]) -> None:
        values = _values()
        if "remove" in updates:
            values.pop(updates["remove"])
        else:
            values.update(updates)
        assert render_template(TEMPLATE_NAME, values, tmp_path / "out.md") == 1


class TestGroup02TemplateResolution:
    def test_unresolved_token_is_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: "value={{unknown}}")
        assert render_template(TEMPLATE_NAME, _values(), tmp_path / "out.md") == 2

    def test_first_line_identifies_template(self, tmp_path: Path) -> None:
        output = tmp_path / "out.md"
        assert render_template(TEMPLATE_NAME, _values(), output) == 0
        assert output.read_text(encoding="utf-8").splitlines()[0] == "template: phase-plan.v1"


class TestGroup03SectionMapping:
    def test_records_are_selected_only_by_kind_stage_and_subject(self, tmp_path: Path) -> None:
        entries = [
            _verdict(1, "final_verification", "mechanical", marker="FINAL-MARKER"),
            _verdict(2, "input_gate", "design", marker="INPUT-MARKER"),
            {"seq": 3, "kind": "role_session", "role": "implementer", "session_id": "ROLE-MARKER"},
            _verdict(4, "implementation_review", "diff", marker="IMPL-MARKER"),
            {"seq": 5, "kind": "changed_paths", "changed_paths": ["PATH-MARKER"]},
            {"seq": 6, "kind": "command", "argv": ["COMMAND-MARKER"]},
            {"seq": 7, "kind": "validation_fact", "name": "FACT-MARKER", "result": "PASS"},
            {
                "seq": 8,
                "kind": "finding_disposition",
                "finding_id": "FINDING-MARKER",
                "decision": "DEFER_OUT_OF_SCOPE",
                "rationale_ref": "R1",
                "deferred_to": "NEXT-MARKER",
            },
        ]
        code, markdown = _project(tmp_path, entries)
        assert code == 0
        assert "INPUT-MARKER" in _section(markdown, "Input Gate")
        assert "FINAL-MARKER" not in _section(markdown, "Input Gate")
        implementation = _section(markdown, "Implementation And Review")
        assert all(marker in implementation for marker in ("ROLE-MARKER", "IMPL-MARKER", "PATH-MARKER", "COMMAND-MARKER"))
        assert "INPUT-MARKER" not in implementation and "FINAL-MARKER" not in implementation
        final = _section(markdown, "Final Verification")
        assert "FINAL-MARKER" in final and "FACT-MARKER" in final
        assert "ROLE-MARKER" not in final
        claim = _section(markdown, "Claim Boundary")
        assert "FACT-MARKER" in claim and "FINDING-MARKER" in claim
        next_section = _section(markdown, "Next")
        assert "NEXT-MARKER" in next_section and "FACT-MARKER" not in next_section


class TestGroup04StatusRules:
    def test_blocked_precedes_changes_requested(self, tmp_path: Path) -> None:
        entries = [
            _verdict(1, "final_verification", "mechanical", "BLOCKED"),
            _verdict(2, "final_verification", "test", "CHANGES_REQUESTED"),
            _verdict(3, "final_verification", "integration", "PASS"),
        ]
        code, markdown = _project(tmp_path, entries)
        assert code == 0
        status = _section(markdown, "Status")
        assert "status: BLOCKED" in status
        assert '"seq":1' in status and '"seq":2' not in status

    @pytest.mark.parametrize(
        ("entries", "expected"),
        [
            (
                [
                    _verdict(1, "final_verification", "mechanical", "PASS"),
                    _verdict(2, "final_verification", "test", "CHANGES_REQUESTED"),
                    _verdict(3, "final_verification", "integration", "PASS"),
                ],
                "CHANGES_REQUESTED",
            ),
            (_passing_entries()[:2], "IN_PROGRESS"),
            (_passing_entries(), "PASS"),
        ],
    )
    def test_remaining_status_rules(
        self, tmp_path: Path, entries: list[dict[str, Any]], expected: str
    ) -> None:
        code, markdown = _project(tmp_path, entries)
        assert code == 0
        assert f"status: {expected}" in _section(markdown, "Status")


class TestGroup05EmptySections:
    def test_unrecorded_sections_are_explicit(self, tmp_path: Path) -> None:
        code, markdown = _project(tmp_path, [])
        assert code == 0
        for title in (
            "Input Gate",
            "Implementation And Review",
            "Final Verification",
            "Claim Boundary",
            "Next",
        ):
            assert "(none recorded)" in _section(markdown, title)


class TestGroup06PhaseFormat:
    @pytest.mark.parametrize("phase_id", ["../../t8", "C:/absolute", "with/path", r"with\path"])
    def test_invalid_phase_ids_create_no_files(self, tmp_path: Path, phase_id: str) -> None:
        manifest = _write_manifest(tmp_path, _passing_entries(), phase_id=phase_id)
        root = tmp_path / "tracked"
        assert promote(manifest, phase_id, root) == 1
        assert not root.exists()


class TestGroup06bContainment:
    def test_valid_phase_id_that_resolves_outside_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        phase_id = "safe-phase"
        manifest = _write_manifest(tmp_path, _passing_entries(), phase_id=phase_id)
        root = tmp_path / "tracked"
        target = root / phase_id
        outside = tmp_path / "outside" / phase_id
        original_resolve = Path.resolve

        def escaped_resolve(path: Path, *args: Any, **kwargs: Any) -> Path:
            if path == target:
                return outside
            return original_resolve(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", escaped_resolve)
        assert promote(manifest, phase_id, root) == 1
        assert not target.exists()


class TestGroup07Idempotency:
    def test_identical_replay_and_changed_source(self, tmp_path: Path) -> None:
        manifest = _write_manifest(tmp_path, _passing_entries())
        root = tmp_path / "tracked"
        assert promote(manifest, "phase-safe", root) == 0
        target = root / "phase-safe"
        before = {path.name: path.read_bytes() for path in target.iterdir()}
        assert promote(manifest, "phase-safe", root) == 0
        assert {path.name: path.read_bytes() for path in target.iterdir()} == before
        value = json.loads(manifest.read_text(encoding="utf-8"))
        value["base_sha"] = "b" * 40
        manifest.write_text(json.dumps(value), encoding="utf-8")
        assert promote(manifest, "phase-safe", root) == 1
        assert {path.name: path.read_bytes() for path in target.iterdir()} == before


class TestGroup08ReplaceMechanics:
    def test_replace_sources_are_unique_temps_in_target_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manifest = _write_manifest(tmp_path, _passing_entries())
        root = tmp_path / "tracked"
        calls: list[tuple[Path, Path]] = []
        original_replace = os.replace

        def recording_replace(source: str | Path, target: str | Path) -> None:
            calls.append((Path(source), Path(target)))
            original_replace(source, target)

        monkeypatch.setattr(report.os, "replace", recording_replace)
        assert promote(manifest, "phase-safe", root) == 0
        target_directory = root / "phase-safe"
        assert [target.name for _, target in calls] == ["manifest.json", "PHASE_REPORT.md"]
        assert all(source.parent == target_directory and source.name.endswith(".tmp") for source, _ in calls)
        assert calls[0][0].name != calls[1][0].name

    def test_replace_failure_preserves_an_existing_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manifest = _write_manifest(tmp_path, _passing_entries())
        projected = tmp_path / "expected.md"
        assert project_report(manifest, projected) == 0
        target_directory = tmp_path / "tracked" / "phase-safe"
        target_directory.mkdir(parents=True)
        report_target = target_directory / "PHASE_REPORT.md"
        expected = projected.read_bytes()
        report_target.write_bytes(expected)

        def fail_replace(source: str | Path, target: str | Path) -> None:
            raise OSError("injected replace failure")

        monkeypatch.setattr(report.os, "replace", fail_replace)
        assert promote(manifest, "phase-safe", tmp_path / "tracked") == 2
        assert report_target.read_bytes() == expected


class TestGroup09PublicSurface:
    def test_cli_success_and_failure_end_with_json(self, tmp_path: Path) -> None:
        output = tmp_path / "plan.md"
        success = subprocess.run(
            [
                sys.executable,
                "-m",
                "methodology.phase.report",
                "render-plan",
                "--template",
                TEMPLATE_NAME,
                "--values",
                json.dumps(_values()),
                "--out",
                str(output),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        assert success.returncode == 0
        assert _last_stdout_json(success.stdout)["exit_code"] == 0
        failure = subprocess.run(
            [
                sys.executable,
                "-m",
                "methodology.phase.report",
                "project",
                "--manifest",
                str(tmp_path / "missing.json"),
                "--out",
                str(tmp_path / "missing.md"),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        assert failure.returncode == 2
        assert _last_stdout_json(failure.stdout)["exit_code"] == 2
        manifest = _write_manifest(tmp_path, _passing_entries())
        promotion = subprocess.run(
            [
                sys.executable,
                "-m",
                "methodology.phase.report",
                "promote",
                "--manifest",
                str(manifest),
                "--phase-id",
                "phase-safe",
                "--root",
                str(tmp_path / "tracked"),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        assert promotion.returncode == 0
        assert _last_stdout_json(promotion.stdout)["exit_code"] == 0

    def test_api_returns_exit_codes_and_exports_only_three_functions(self, tmp_path: Path) -> None:
        assert report.__all__ == ["render_template", "project_report", "promote"]
        assert isinstance(render_template(TEMPLATE_NAME, _values(), tmp_path / "plan.md"), int)
        manifest = _write_manifest(tmp_path, [])
        assert isinstance(project_report(manifest, tmp_path / "report.md"), int)
        assert isinstance(promote(manifest, "phase-safe", tmp_path / "tracked"), int)


class TestGroup10EnumTolerance:
    def test_legacy_unknown_stage_is_visible_and_forces_in_progress(self, tmp_path: Path) -> None:
        entries = _passing_entries()
        entries.append(_verdict(4, "input gate", "design"))
        code, markdown = _project(tmp_path, entries)
        assert code == 0
        unclassified = _section(markdown, "Unclassified Records")
        assert "seq=4" in unclassified
        assert "kind=verdict" in unclassified
        assert "field=stage" in unclassified
        assert 'value="input gate"' in unclassified
        assert "status: IN_PROGRESS" in _section(markdown, "Status")


class TestGroup11LatestSubjectState:
    def test_maximum_seq_wins_when_entry_order_differs_from_sequence_order(
        self, tmp_path: Path
    ) -> None:
        entries = [
            _verdict(4, "final_verification", "mechanical", "PASS"),
            _verdict(1, "final_verification", "mechanical", "CHANGES_REQUESTED"),
            _verdict(2, "final_verification", "test", "PASS"),
            _verdict(3, "final_verification", "integration", "PASS"),
        ]
        code, markdown = _project(tmp_path, entries)
        assert code == 0
        status = _section(markdown, "Status")
        assert "status: PASS" in status
        assert '"seq":4' in status
        assert '"seq":1' not in status

    @pytest.mark.parametrize(
        ("first", "second", "expected", "winning_seq"),
        [
            ("CHANGES_REQUESTED", "PASS", "PASS", 4),
            ("PASS", "CHANGES_REQUESTED", "CHANGES_REQUESTED", 4),
        ],
    )
    def test_latest_seq_wins_and_is_named_in_blocking_records(
        self,
        tmp_path: Path,
        first: str,
        second: str,
        expected: str,
        winning_seq: int,
    ) -> None:
        entries = [
            _verdict(1, "final_verification", "mechanical", first),
            _verdict(2, "final_verification", "test", "PASS"),
            _verdict(3, "final_verification", "integration", "PASS"),
            _verdict(4, "final_verification", "mechanical", second),
        ]
        code, markdown = _project(tmp_path, entries)
        assert code == 0
        status = _section(markdown, "Status")
        assert f"status: {expected}" in status
        assert f'"seq":{winning_seq}' in status
        assert '"seq":1' not in status


class TestGroup12PartialPromotion:
    def test_second_replace_failure_and_idempotent_recovery(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        manifest = _write_manifest(tmp_path, _passing_entries())
        root = tmp_path / "tracked"
        original_replace = os.replace
        calls = 0

        def fail_second(source: str | Path, target: str | Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected second replace failure")
            original_replace(source, target)

        monkeypatch.setattr(report.os, "replace", fail_second)
        assert promote(manifest, "phase-safe", root) == 2
        payload = _last_stdout_json(capsys.readouterr().out)
        assert payload["promoted"] == ["manifest.json"]
        assert payload["partial"] is True
        target = root / "phase-safe"
        assert (target / "manifest.json").exists()
        assert not (target / "PHASE_REPORT.md").exists()
        monkeypatch.setattr(report.os, "replace", original_replace)
        assert promote(manifest, "phase-safe", root) == 0
        assert (target / "PHASE_REPORT.md").exists()


class TestGroup13ExitBoundaries:
    def test_extra_values_are_harmless(self, tmp_path: Path) -> None:
        assert render_template(TEMPLATE_NAME, _values(extra="unused"), tmp_path / "out.md") == 0

    def test_missing_template_value_is_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: "{{phase_id}} {{unknown}}")
        assert render_template(TEMPLATE_NAME, _values(), tmp_path / "out.md") == 2

    def test_missing_required_value_is_input_failure(self, tmp_path: Path) -> None:
        values = _values()
        values.pop("base_sha")
        assert render_template(TEMPLATE_NAME, values, tmp_path / "out.md") == 1

    def test_missing_output_directory_is_blocked(self, tmp_path: Path) -> None:
        assert render_template(TEMPLATE_NAME, _values(), tmp_path / "missing" / "out.md") == 2


class TestGroup14SequenceValidation:
    @pytest.mark.parametrize(
        ("entries", "detail", "value"),
        [
            ([_verdict(1, "final_verification", "test"), _verdict(1, "final_verification", "test")], "duplicate_seq", 1),
            ([_verdict(1, "final_verification", "test"), _verdict(3, "final_verification", "test")], "missing_seq", 2),
        ],
    )
    def test_duplicate_and_missing_seq_are_source_damage(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        entries: list[dict[str, Any]],
        detail: str,
        value: int,
    ) -> None:
        manifest = _write_manifest(tmp_path, entries)
        assert project_report(manifest, tmp_path / "out.md") == 2
        payload = _last_stdout_json(capsys.readouterr().out)
        assert value in payload[detail]

    @pytest.mark.parametrize("seq", [True, "1"])
    def test_non_plain_integer_seq_is_source_damage(self, tmp_path: Path, seq: Any) -> None:
        entry = _verdict(1, "final_verification", "test")
        entry["seq"] = seq
        manifest = _write_manifest(tmp_path, [entry])
        assert project_report(manifest, tmp_path / "out.md") == 2


class TestGroup15PhaseIdentity:
    def test_manifest_and_destination_phase_must_match(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        manifest = _write_manifest(tmp_path, _passing_entries(), phase_id="SOURCE")
        root = tmp_path / "tracked"
        assert promote(manifest, "DESTINATION", root) == 1
        payload = _last_stdout_json(capsys.readouterr().out)
        assert payload["manifest_phase_id"] == "SOURCE"
        assert payload["destination_phase_id"] == "DESTINATION"
        assert not root.exists()


class TestGroup16DirectoryCreation:
    def test_first_promotion_creates_phase_directory(self, tmp_path: Path) -> None:
        manifest = _write_manifest(tmp_path, _passing_entries())
        root = tmp_path / "new-root"
        assert promote(manifest, "phase-safe", root) == 0
        assert (root / "phase-safe" / "manifest.json").exists()
        assert (root / "phase-safe" / "PHASE_REPORT.md").exists()

    def test_mkdir_failure_is_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manifest = _write_manifest(tmp_path, _passing_entries())
        root = tmp_path / "tracked"
        target = root / "phase-safe"
        original_mkdir = Path.mkdir

        def fail_target_mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
            if path == target:
                raise OSError("injected mkdir failure")
            original_mkdir(path, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", fail_target_mkdir)
        assert promote(manifest, "phase-safe", root) == 2
        assert not target.exists()
