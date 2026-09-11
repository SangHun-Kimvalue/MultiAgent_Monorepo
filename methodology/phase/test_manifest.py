from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from methodology.phase import manifest
from methodology.phase.manifest import append_entry, init_manifest


BASE_SHA = "a" * 40
SHA256_EMPTY = hashlib.sha256(b"").hexdigest()
UTC_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T.*Z$")


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _init(tmp_path: Path) -> Path:
    path = tmp_path / "phase-manifest.json"
    assert init_manifest(path, "T10-O1b", BASE_SHA) == 0
    return path


def _artifact_entry(reference: str, digest: str = SHA256_EMPTY) -> dict[str, Any]:
    return {"kind": "artifact", "artifact_ref": reference, "content_sha256": digest}


def _verdict_entry(
    reference: str | None,
    digest: str | None,
    verdict: str = "PASS",
    exit_code: int = 0,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "kind": "verdict",
        "verdict": verdict,
        "exit_code": exit_code,
        "artifact_ref": reference,
        "content_sha256": digest,
        "attempt_id": "attempt-1",
        "stage": "implementation_review",
        "subject": "diff",
        **extra,
    }


class TestGroup01InitAndVersion:
    def test_init_success_existing_and_invalid_sha(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.json"
        assert init_manifest(path, "phase-1", BASE_SHA) == 0
        assert _read(path)["schema_version"] == "t10-o1.phase-manifest.v1"
        original = path.read_bytes()
        assert init_manifest(path, "phase-1", BASE_SHA) == 1
        assert path.read_bytes() == original
        assert init_manifest(tmp_path / "bad.json", "phase-1", "ABC") == 1

    @pytest.mark.parametrize("version", ["future.v2", "garbage"])
    def test_append_rejects_other_schema_versions(self, tmp_path: Path, version: str) -> None:
        path = _init(tmp_path)
        value = _read(path)
        value["schema_version"] = version
        path.write_text(json.dumps(value), encoding="utf-8")
        assert append_entry(path, {"kind": "changed_paths", "changed_paths": ["a"]}) == 1


class TestGroup02SevenValidKinds:
    @pytest.mark.parametrize(
        "entry",
        [
            {"kind": "role_session", "role": "implementer", "session_id": "s1"},
            _artifact_entry("artifact.txt"),
            {"kind": "changed_paths", "changed_paths": ["a.cpp"]},
            {"kind": "command", "argv": ["pytest"], "artifact_ref": "missing.log"},
            _verdict_entry("artifact.txt", SHA256_EMPTY),
            {
                "kind": "finding_disposition",
                "finding_id": "F1",
                "decision": "ACCEPT",
                "rationale_ref": "R1",
            },
            {"kind": "validation_fact", "name": "pytest", "result": "PASS"},
        ],
    )
    def test_valid_kind(self, tmp_path: Path, entry: dict[str, Any]) -> None:
        path = _init(tmp_path)
        (tmp_path / "artifact.txt").write_bytes(b"")
        assert append_entry(path, entry) == 0


class TestGroup03SevenClosedSchemas:
    CASES = [
        ({"kind": "role_session", "session_id": "s"}, {"role": "implementer", "session_id": "s", "name": "x"}, {"role": [], "session_id": "s"}),
        ({"kind": "artifact", "artifact_ref": "a"}, {"artifact_ref": "a", "content_sha256": SHA256_EMPTY, "role": "test"}, {"artifact_ref": [], "content_sha256": SHA256_EMPTY}),
        ({"kind": "changed_paths"}, {"changed_paths": ["a"], "argv": ["x"]}, {"changed_paths": [1]}),
        ({"kind": "command"}, {"argv": ["x"], "role": "test"}, {"argv": []}),
        ({"kind": "verdict", "verdict": "PASS"}, {**_verdict_entry(None, None, "BLOCKED", 2, failure_reason="timeout"), "role": "reviewer"}, {**_verdict_entry(None, None, "BLOCKED", 2, failure_reason="timeout"), "exit_code": True}),
        ({"kind": "finding_disposition", "finding_id": "F", "decision": "ACCEPT"}, {"finding_id": "F", "decision": "ACCEPT", "rationale_ref": "R", "role": "reviewer"}, {"finding_id": "F", "decision": [], "rationale_ref": "R"}),
        ({"kind": "validation_fact", "name": "n"}, {"name": "n", "result": "PASS", "argv": ["x"]}, {"name": "n", "result": []}),
    ]

    @pytest.mark.parametrize("base", CASES)
    @pytest.mark.parametrize("variant", [0, 1, 2], ids=["missing", "mixed", "type"])
    def test_invalid_kind_schema(
        self, tmp_path: Path, base: tuple[dict[str, Any], ...], variant: int
    ) -> None:
        path = _init(tmp_path)
        entry = {"kind": base[variant].get("kind", base[0]["kind"]), **base[variant]}
        assert append_entry(path, entry) == 1


class TestGroup04VerdictCrossFields:
    @pytest.mark.parametrize(
        ("verdict", "exit_code"),
        [("PASS", 1), ("CHANGES_REQUESTED", 2), ("BLOCKED", 0)],
    )
    def test_mismatched_verdict_exit(self, tmp_path: Path, verdict: str, exit_code: int) -> None:
        path = _init(tmp_path)
        assert append_entry(path, _verdict_entry(None, None, verdict, exit_code, failure_reason="timeout")) == 1

    def test_blocked_null_pair_and_failure_reason(self, tmp_path: Path) -> None:
        path = _init(tmp_path)
        assert append_entry(path, _verdict_entry(None, None, "BLOCKED", 2, failure_reason="timeout")) == 0

    @pytest.mark.parametrize(
        "entry",
        [
            _verdict_entry(None, None, "BLOCKED", 2),
            _verdict_entry(None, SHA256_EMPTY, "BLOCKED", 2, failure_reason="timeout"),
            _verdict_entry("artifact.txt", None, "BLOCKED", 2, failure_reason="timeout"),
            _verdict_entry(None, None, "PASS", 0),
        ],
    )
    def test_invalid_null_evidence(self, tmp_path: Path, entry: dict[str, Any]) -> None:
        assert append_entry(_init(tmp_path), entry) == 1


class TestGroup05FindingDisposition:
    def test_decision_contract(self, tmp_path: Path) -> None:
        path = _init(tmp_path)
        base = {"kind": "finding_disposition", "finding_id": "F1", "rationale_ref": "R1"}
        assert append_entry(path, {**base, "decision": "UNKNOWN"}) == 1
        assert append_entry(path, {**base, "decision": "DEFER_OUT_OF_SCOPE", "deferred_to": "T10-O2"}) == 0
        assert append_entry(path, {**base, "decision": "ACCEPT", "deferred_to": "T10-O2"}) == 1

    @pytest.mark.parametrize("deferred_to", [None, "", [], "T10 O2"])
    def test_invalid_deferred_to(self, tmp_path: Path, deferred_to: Any) -> None:
        entry = {
            "kind": "finding_disposition",
            "finding_id": "F1",
            "decision": "DEFER_OUT_OF_SCOPE",
            "rationale_ref": "R1",
            "deferred_to": deferred_to,
        }
        assert append_entry(_init(tmp_path), entry) == 1


class TestGroup06ArtifactHashes:
    def test_artifact_hash_outcomes(self, tmp_path: Path) -> None:
        path = _init(tmp_path)
        artifact = tmp_path / "artifact.txt"
        artifact.write_bytes(b"")
        assert append_entry(path, _artifact_entry("artifact.txt")) == 0
        count = len(_read(path)["entries"])
        assert append_entry(path, _artifact_entry("artifact.txt", "b" * 64)) == 1
        assert len(_read(path)["entries"]) == count
        assert append_entry(path, _artifact_entry("missing.txt")) == 2

    def test_command_does_not_hash_or_require_artifact(self, tmp_path: Path) -> None:
        path = _init(tmp_path)
        assert append_entry(path, {"kind": "command", "argv": ["run"], "artifact_ref": "missing.txt"}) == 0
        assert append_entry(path, {"kind": "command", "argv": ["run"], "content_sha256": SHA256_EMPTY}) == 1


class TestGroup07PathContainment:
    def test_normalized_storage_preserves_case(self, tmp_path: Path) -> None:
        path = _init(tmp_path)
        entry = {"kind": "command", "argv": ["run"], "artifact_ref": r".\Evidence\File.json"}
        assert append_entry(path, entry) == 0
        assert _read(path)["entries"][0]["artifact_ref"] == "Evidence/File.json"

    def test_absolute_and_parent_escape(self, tmp_path: Path) -> None:
        path = _init(tmp_path)
        base = {"kind": "command", "argv": ["run"]}
        assert append_entry(path, {**base, "artifact_ref": str((tmp_path / "x").resolve())}) == 1
        assert append_entry(path, {**base, "artifact_ref": "../outside.txt"}) == 1

    def test_symlink_containment(self, tmp_path: Path) -> None:
        path = _init(tmp_path)
        inside = tmp_path / "inside.txt"
        outside = tmp_path.parent / "outside.txt"
        inside.write_text("inside", encoding="utf-8")
        outside.write_text("outside", encoding="utf-8")
        inside_link = tmp_path / "inside-link.txt"
        outside_link = tmp_path / "outside-link.txt"
        try:
            os.symlink(inside, inside_link)
            os.symlink(outside, outside_link)
        except OSError as exc:
            pytest.skip(f"symlink creation unavailable: {exc}")
        base = {"kind": "command", "argv": ["run"]}
        assert append_entry(path, {**base, "artifact_ref": inside_link.name}) == 0
        assert append_entry(path, {**base, "artifact_ref": outside_link.name}) == 1


class TestGroup08WriterFields:
    @pytest.mark.parametrize("field", ["seq", "recorded_at_utc"])
    def test_writer_fields_are_rejected(self, tmp_path: Path, field: str) -> None:
        entry = {"kind": "changed_paths", "changed_paths": ["a"], field: 1}
        assert append_entry(_init(tmp_path), entry) == 1

    def test_writer_fills_sequence_and_time(self, tmp_path: Path) -> None:
        path = _init(tmp_path)
        entry = {"kind": "changed_paths", "changed_paths": ["a"]}
        assert append_entry(path, entry) == 0
        assert append_entry(path, entry) == 0
        written = _read(path)["entries"]
        assert [item["seq"] for item in written] == [1, 2]
        assert all(UTC_PATTERN.fullmatch(item["recorded_at_utc"]) for item in written)


class TestGroup09ExistingManifestAndContraryVerdicts:
    def test_broken_json_and_discontinuous_seq(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.json"
        path.write_text("{broken", encoding="utf-8")
        assert append_entry(path, {"kind": "changed_paths", "changed_paths": ["a"]}) == 2
        path.unlink()
        path = _init(tmp_path)
        value = _read(path)
        value["entries"] = [{"seq": 2}]
        path.write_text(json.dumps(value), encoding="utf-8")
        assert append_entry(path, {"kind": "changed_paths", "changed_paths": ["a"]}) == 1

    def test_contrary_verdicts_are_both_appended(self, tmp_path: Path) -> None:
        path = _init(tmp_path)
        artifact = tmp_path / "artifact.txt"
        artifact.write_bytes(b"")
        first = _verdict_entry("artifact.txt", SHA256_EMPTY, "CHANGES_REQUESTED", 1)
        second = _verdict_entry("artifact.txt", SHA256_EMPTY, "PASS", 0)
        assert append_entry(path, first) == 0
        assert append_entry(path, second) == 0
        assert len(_read(path)["entries"]) == 2


class TestGroup10AtomicityAndDryRun:
    def test_replace_uses_unique_sibling_temporary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _init(tmp_path)
        replace_calls: list[tuple[Path, Path]] = []
        real_replace = manifest._os.replace

        def record_replace(source: Path, destination: Path) -> None:
            replace_calls.append((Path(source), Path(destination)))
            real_replace(source, destination)

        monkeypatch.setattr(manifest._os, "replace", record_replace)
        entry = {"kind": "changed_paths", "changed_paths": ["a"]}
        assert append_entry(path, entry) == 0
        assert append_entry(path, entry) == 0
        assert [destination for _, destination in replace_calls] == [path, path]
        sources = [source for source, _ in replace_calls]
        assert all(source.parent == path.parent and source != path for source in sources)
        assert len(set(sources)) == 2

    def test_replace_failure_preserves_existing_bytes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _init(tmp_path)
        original = path.read_bytes()

        def fail_replace(_source: Path, _destination: Path) -> None:
            raise OSError("injected replace failure")

        monkeypatch.setattr(manifest._os, "replace", fail_replace)
        assert append_entry(path, {"kind": "changed_paths", "changed_paths": ["a"]}) == 2
        assert path.read_bytes() == original

    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        path = _init(tmp_path)
        original = path.read_bytes()
        assert append_entry(path, {"kind": "changed_paths", "changed_paths": ["a"]}, dry_run=True) == 0
        assert path.read_bytes() == original
        assert list(tmp_path.glob("*.tmp")) == []


class TestGroup11PublicAndCliSurface:
    def test_stdin_and_module_execution(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.json"
        init_run = subprocess.run(
            [sys.executable, "-m", "methodology.phase.manifest", "init", "--manifest", str(path), "--phase-id", "p", "--base-sha", BASE_SHA],
            capture_output=True,
            text=True,
            check=False,
        )
        assert init_run.returncode == 0
        json.loads(init_run.stdout.splitlines()[-1])
        append_run = subprocess.run(
            [sys.executable, "-m", "methodology.phase.manifest", "append", "--manifest", str(path), "--entry", "-"],
            input=json.dumps({"kind": "changed_paths", "changed_paths": ["a"]}),
            capture_output=True,
            text=True,
            check=False,
        )
        assert append_run.returncode == 0
        json.loads(append_run.stdout.splitlines()[-1])
        assert len(_read(path)["entries"]) == 1
        missing_entry_run = subprocess.run(
            [
                sys.executable,
                "-m",
                "methodology.phase.manifest",
                "append",
                "--manifest",
                str(path),
                "--entry",
                str(tmp_path / "missing-entry.json"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert missing_entry_run.returncode == 2
        json.loads(missing_entry_run.stdout.splitlines()[-1])

    def test_non_argparse_outputs_and_api_return_codes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "manifest.json"
        assert init_manifest(path, "p", BASE_SHA) == 0
        json.loads(capsys.readouterr().out.splitlines()[-1])
        assert append_entry(path, {"kind": "unknown"}) == 1
        json.loads(capsys.readouterr().out.splitlines()[-1])

        def explode(_path: Path, _value: dict[str, Any]) -> None:
            raise RuntimeError("unexpected")

        monkeypatch.setattr(manifest, "_write_atomic", explode)
        assert append_entry(path, {"kind": "changed_paths", "changed_paths": ["a"]}) == 2
        json.loads(capsys.readouterr().out.splitlines()[-1])
        assert manifest.__all__ == ["append_entry", "init_manifest"]


class TestGroup12VerdictEnums:
    @pytest.mark.parametrize("stage", ["review", "input gate", ""])
    def test_rejects_unsupported_stage(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], stage: str
    ) -> None:
        path = _init(tmp_path)
        capsys.readouterr()
        entry = _verdict_entry(None, None, "BLOCKED", 2, failure_reason="timeout", stage=stage)
        assert append_entry(path, entry) == 1
        message = json.loads(capsys.readouterr().out.splitlines()[-1])["message"]
        allowed = ["input_gate", "implementation_review", "final_verification"]
        assert message == f"stage has unsupported value {stage!r}; allowed values: {allowed!r}"

    @pytest.mark.parametrize("subject", ["implementation", "unknown"])
    def test_rejects_unsupported_subject(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], subject: str
    ) -> None:
        path = _init(tmp_path)
        capsys.readouterr()
        entry = _verdict_entry(
            None, None, "BLOCKED", 2, failure_reason="timeout", subject=subject
        )
        assert append_entry(path, entry) == 1
        message = json.loads(capsys.readouterr().out.splitlines()[-1])["message"]
        allowed = ["design", "prompt", "diff", "mechanical", "test", "integration"]
        assert message == f"subject has unsupported value {subject!r}; allowed values: {allowed!r}"

    def test_append_does_not_revalidate_existing_stage_enum(self, tmp_path: Path) -> None:
        path = _init(tmp_path)
        value = _read(path)
        existing = _verdict_entry(
            None, None, "BLOCKED", 2, failure_reason="timeout", stage="review"
        )
        existing.update({"seq": 1, "recorded_at_utc": "2026-08-20T00:00:00Z"})
        value["entries"] = [existing]
        path.write_text(json.dumps(value), encoding="utf-8")

        assert append_entry(
            path, _verdict_entry(None, None, "BLOCKED", 2, failure_reason="timeout")
        ) == 0
        assert len(_read(path)["entries"]) == 2
