"""C1 corpus and C2 canonical projection tests."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from methodology.tools import phase_equivalence

from methodology.tools.phase_equivalence import (
    CORPUS,
    EquivalenceBlocked,
    LegObservation,
    RouteObservation,
    canonical_projection,
    compare_corpus,
    compare_observations,
    main,
    run_corpus,
)


BASE_SHA = "c3bd90185dcc36f04fc6888cf2153a3dd825910d"
PYTHON = Path("C:/Users/shkim/tools/py313/python.exe")


@pytest.fixture(scope="module")
def corpus_result(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, object]]:
    root = tmp_path_factory.mktemp("phase-equivalence") / "corpus"
    result = run_corpus(root, python=PYTHON, base_sha=BASE_SHA)
    return root, result


def test_seven_shapes_run_twice_on_both_routes_and_reproduce(
    corpus_result: tuple[Path, dict[str, object]],
) -> None:
    _root, result = corpus_result
    assert result["status"] == "PASS"
    cases = result["cases"]
    assert isinstance(cases, list)
    assert [case["name"] for case in cases] == [case.name for case in CORPUS]
    assert all(case["manifest"] == "OK" for case in cases)
    assert {case["name"]: case["report"] for case in cases} == {
        "pass": "OK",
        "changes_requested": "OK",
        "blocked": "OK",
        "timeout": "OK",
        "partial_malformed": "OK",
        "multiple_findings": "OK",
        "no_findings": "OK",
    }
    for case in cases:
        assert len(case["base_returncodes"]) == 2
        assert len(case["opt_in_returncodes"]) == 2


def test_fixture_is_stdin_insensitive_and_contains_required_findings(
    corpus_result: tuple[Path, dict[str, object]],
) -> None:
    root, _result = corpus_result
    stub = (root / "fixture" / "fixed_leg.py").read_bytes()
    assert b"sys.stdin.read()" in stub
    assert b"len(_stdin)" not in stub
    assert b"print(_stdin)" not in stub

    phase_dir = root / "multiple_findings" / "repeat-1" / "opt_in" / "phase"
    manifest = json.loads((phase_dir / "phase-manifest.json").read_bytes())
    reviewer = manifest["entries"][-1]
    artifact = phase_dir / reviewer["artifact_ref"]
    envelope = json.loads(artifact.read_bytes())
    assert envelope["stdout_preview"].splitlines()[0] == (
        '{"findings":["finding-A","안전 경계 finding-B"]}'
    )


def test_canonical_hash_is_separate_from_stored_hash(
    corpus_result: tuple[Path, dict[str, object]],
) -> None:
    root, _result = corpus_result
    phase_dir = root / "pass" / "repeat-1" / "opt_in" / "phase"
    source = json.loads((phase_dir / "phase-manifest.json").read_bytes())
    canonical = canonical_projection(phase_dir)
    projection = json.loads(canonical.manifest)
    source_verdict = next(entry for entry in source["entries"] if "content_sha256" in entry)
    projected_verdict = next(
        entry for entry in projection["entries"] if "canonical_content_sha256" in entry
    )
    assert "content_sha256" in source_verdict
    assert "canonical_content_sha256" not in source_verdict
    assert "content_sha256" not in projected_verdict
    assert len(projected_verdict["canonical_content_sha256"]) == 64
    assert projected_verdict["canonical_content_sha256"] in canonical.report
    assert source_verdict["content_sha256"] not in canonical.report
    assert (
        f"manifest_sha256: {hashlib.sha256(canonical.manifest.encode('utf-8')).hexdigest()}"
        in canonical.report
    )
    assert "<SHA>" not in canonical.report


def test_artifact_finding_timestamp_difference_breaks_reproducibility(
    tmp_path: Path,
) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    _write_phase_with_finding(first_dir, "finding at 2026-08-31T01:02:03.001Z")
    _write_phase_with_finding(second_dir, "finding at 2026-08-31T01:02:03.002Z")

    first = canonical_projection(first_dir)
    second = canonical_projection(second_dir)
    assert first.manifest != second.manifest
    assert first.report != second.report


def test_artifact_byte_tamper_blocks_before_projection(
    corpus_result: tuple[Path, dict[str, object]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, _result = corpus_result
    source = root / "pass" / "repeat-1" / "opt_in" / "phase"
    target = tmp_path / "artifact-tamper"
    _copy_phase(source, target)
    manifest = json.loads((target / "phase-manifest.json").read_bytes())
    entry = next(item for item in manifest["entries"] if item.get("artifact_ref"))
    artifact = target / entry["artifact_ref"]
    artifact.write_bytes(artifact.read_bytes().replace(b'"duration_s":', b'"duration_s":9', 1))

    assert main(["project", "--phase-dir", str(target)]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "BLOCKED"
    assert payload["exit_code"] == 2
    assert "content_sha256" in payload["message"]


def test_manifest_byte_tamper_blocks_against_original_report_hash(
    corpus_result: tuple[Path, dict[str, object]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, _result = corpus_result
    source = root / "pass" / "repeat-1" / "opt_in" / "phase"
    target = tmp_path / "manifest-tamper"
    _copy_phase(source, target)
    manifest_path = target / "phase-manifest.json"
    raw = manifest_path.read_bytes()
    assert b'"phase_id":"equivalence-pass"' in raw
    manifest_path.write_bytes(
        raw.replace(b'"phase_id":"equivalence-pass"', b'"phase_id":"tampered-pass"', 1)
    )

    assert main(["project", "--phase-dir", str(target)]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "BLOCKED"
    assert payload["exit_code"] == 2
    assert "manifest_sha256" in payload["message"]


def test_report_missing_blocks_before_projection(
    corpus_result: tuple[Path, dict[str, object]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, _result = corpus_result
    source = root / "pass" / "repeat-1" / "opt_in" / "phase"
    target = tmp_path / "report-missing"
    _copy_phase(source, target)
    (target / "PHASE_REPORT.md").unlink()

    assert main(["project", "--phase-dir", str(target)]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "BLOCKED"
    assert payload["exit_code"] == 2
    assert "report is unavailable" in payload["message"]


@pytest.fixture(scope="module")
def comparison_result(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, object]:
    root = tmp_path_factory.mktemp("phase-comparison") / "compare"
    return compare_corpus(root, python=PYTHON, base_sha=BASE_SHA)


def test_compare_matches_all_shapes_and_counts_actual_spawned_legs(
    comparison_result: dict[str, object],
) -> None:
    assert comparison_result["status"] == "PASS"
    cases = comparison_result["cases"]
    assert isinstance(cases, list)
    assert [case["name"] for case in cases] == [case.name for case in CORPUS]
    assert all(case["gating_legs"] == "MATCH" for case in cases)
    assert all(case["finding_text"] == "MATCH" for case in cases)
    assert comparison_result["counters"] == {
        "external_cli_calls": {"base": 24, "opt_in": 24},
        "full_suite_runs": {"base": 6, "opt_in": 6},
    }
    assert "wall" not in json.dumps(comparison_result, ensure_ascii=False).lower()


def test_prefix_finding_is_exact_mismatch_and_cli_blocks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = _finding_observation("base", "finding-A")
    opt_in = _finding_observation("opt_in", "finding-A-extra")

    with pytest.raises(EquivalenceBlocked) as raised:
        compare_observations("prefix_negative", base, opt_in)
    assert raised.value.details == {
        "case": "prefix_negative",
        "axis": "finding_text",
        "failure": "mismatch",
        "index": 0,
        "leg": "implementer-reviewer",
        "base": "finding-A",
        "opt_in": "finding-A-extra",
    }

    def blocked_compare(*args: object, **kwargs: object) -> dict[str, object]:
        compare_observations("prefix_negative", base, opt_in)
        raise AssertionError("exact mismatch must block")

    monkeypatch.setattr(phase_equivalence, "compare_corpus", blocked_compare)
    exit_code = main(
        [
            "compare",
            "--output-root",
            str(tmp_path / "unused"),
            "--python",
            str(PYTHON),
            "--base-sha",
            BASE_SHA,
        ]
    )
    lines = capsys.readouterr().out.splitlines()
    assert exit_code == 2
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["status"] == "BLOCKED"
    assert payload["details"]["axis"] == "finding_text"


def test_comparator_blocks_missing_leg() -> None:
    base = _finding_observation("base", "finding-A")
    opt_in = RouteObservation(route="opt_in", steps=())

    with pytest.raises(EquivalenceBlocked) as raised:
        compare_observations("missing_leg", base, opt_in)
    assert raised.value.details["failure"] == "missing"
    assert raised.value.details["axis"] == "gating_legs"


def test_comparator_blocks_when_both_routes_have_zero_gating_legs() -> None:
    base = RouteObservation(route="base", steps=())
    opt_in = RouteObservation(route="opt_in", steps=())

    with pytest.raises(EquivalenceBlocked) as raised:
        compare_observations("zero_gating_legs", base, opt_in)
    assert raised.value.details == {
        "case": "zero_gating_legs",
        "axis": "gating_legs",
        "failure": "missing",
        "index": 0,
        "base": None,
        "opt_in": None,
    }


def test_compare_cli_blocks_empty_corpus(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(phase_equivalence, "CORPUS", ())

    assert main(
        [
            "compare",
            "--output-root",
            str(tmp_path / "empty-corpus"),
            "--python",
            str(PYTHON),
            "--base-sha",
            BASE_SHA,
        ]
    ) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "BLOCKED"
    assert payload["exit_code"] == 2
    assert payload["details"]["axis"] == "corpus"


@pytest.mark.parametrize(
    ("outer", "failure"),
    [({}, "missing"), ({"stdout": "{"}, "parse_failure")],
)
def test_comparator_blocks_inner_report_parse_failures(
    outer: dict[str, object], failure: str
) -> None:
    with pytest.raises(EquivalenceBlocked) as raised:
        phase_equivalence._observation_from_outer(
            outer, route="base"
        )
    assert raised.value.details["failure"] == failure
    assert raised.value.details["axis"] == "inner_report"


def _copy_phase(source: Path, target: Path) -> None:
    target.mkdir()
    for source_path in source.rglob("*"):
        relative = source_path.relative_to(source)
        target_path = target / relative
        if source_path.is_dir():
            target_path.mkdir()
        else:
            target_path.write_bytes(source_path.read_bytes())


def _finding_observation(route: str, finding: str) -> RouteObservation:
    return RouteObservation(
        route=route,
        steps=(
            LegObservation(
                name="implementer-reviewer",
                verdict="PASS",
                exit_code=0,
                stdout_preview=finding,
                gating=True,
                skipped=False,
            ),
        ),
    )


def _write_phase_with_finding(phase_dir: Path, finding: str) -> None:
    manifest_path = phase_dir / "phase-manifest.json"
    report_path = phase_dir / "PHASE_REPORT.md"
    artifact_ref = "runs/20260831-010203-example/04-implementer-reviewer.envelope.json"
    artifact_path = phase_dir / artifact_ref
    artifact_path.parent.mkdir(parents=True)
    artifact = {
        "duration_s": 1.25,
        "stdin_path": "C:/run/04.stdin.txt",
        "stdout_path": "C:/run/04.stdout.txt",
        "stderr_path": "C:/run/04.stderr.txt",
        "envelope_path": "C:/run/04.envelope.json",
        "stdout_preview": finding,
        "stderr_sanitized": "",
    }
    artifact_raw = json.dumps(artifact, ensure_ascii=False).encode("utf-8")
    artifact_path.write_bytes(artifact_raw)
    entry = {
        "artifact_ref": artifact_ref,
        "content_sha256": hashlib.sha256(artifact_raw).hexdigest(),
        "recorded_at_utc": "2026-08-31T01:02:03.000Z",
    }
    manifest = {
        "created_at_utc": "2026-08-31T01:02:03.000Z",
        "entries": [entry],
    }
    manifest_raw = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    manifest_path.write_bytes(manifest_raw)
    report_path.write_bytes(
        (
            "# PHASE_REPORT\n\n"
            f"manifest_sha256: {hashlib.sha256(manifest_raw).hexdigest()}\n\n"
            f"- {json.dumps(entry, ensure_ascii=False, separators=(',', ':'))}\n"
        ).encode("utf-8")
    )
