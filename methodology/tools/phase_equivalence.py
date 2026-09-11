#!/usr/bin/env python3
"""Deterministic run-phase corpus, projection, comparison, and counters."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Never


PASS = "PASS"
BLOCKED = "BLOCKED"
BASE_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
RUNDIR = re.compile(r"\d{8}-\d{6}-")
TS = re.compile(r"\d{4}-\d{2}-\d{2}T[\d:.]+Z")
_ARTIFACT_PATH_FIELDS = ("stdin_path", "stdout_path", "stderr_path", "envelope_path")
REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = REPO_ROOT / "runtimes" / "ztr"


class EquivalenceBlocked(RuntimeError):
    """A malformed or inconsistent artifact that must fail closed."""

    def __init__(
        self, message: str, *, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.details = details or {}


class JsonArgumentParser(argparse.ArgumentParser):
    """Keep argparse errors inside the one-line JSON contract."""

    def error(self, message: str) -> Never:
        raise EquivalenceBlocked(f"invalid arguments: {message}")


@dataclass(frozen=True)
class CorpusCase:
    """One fixed four-leg corpus shape."""

    name: str
    mechanical_mode: str = "pass"
    test_mode: str = "pass"
    reviewer_mode: str = "review_pass"
    timeout_s: float = 2.0


@dataclass(frozen=True)
class CanonicalProjection:
    """Canonical comparison material after original integrity verification."""

    manifest: str
    report: str


@dataclass(frozen=True)
class RunResult:
    """One run-phase subprocess result and its optional projection."""

    route: str
    returncode: int
    outer: dict[str, Any]
    phase_dir: Path | None
    projection: CanonicalProjection | None


@dataclass(frozen=True)
class LegObservation:
    """Comparison and spawn evidence from one inner-report step."""

    name: str
    verdict: str
    exit_code: int
    stdout_preview: str
    gating: bool
    skipped: bool


@dataclass(frozen=True)
class RouteObservation:
    """Validated structural observations for one BASE or opt-in execution."""

    route: str
    steps: tuple[LegObservation, ...]

    @property
    def gating_legs(self) -> tuple[LegObservation, ...]:
        return tuple(step for step in self.steps if step.gating)

    @property
    def external_cli_calls(self) -> int:
        return sum(not step.skipped for step in self.steps)

    @property
    def full_suite_runs(self) -> int:
        return sum(step.name == "test" and not step.skipped for step in self.steps)


_CORPUS_CONTRACT: tuple[CorpusCase, ...] = (
    CorpusCase("pass"),
    CorpusCase("changes_requested", test_mode="changes"),
    CorpusCase("blocked", mechanical_mode="blocked"),
    CorpusCase("timeout", test_mode="sleep", timeout_s=2.0),
    CorpusCase("partial_malformed", reviewer_mode="malformed"),
    CorpusCase("multiple_findings", reviewer_mode="multiple_findings"),
    CorpusCase("no_findings", reviewer_mode="no_findings"),
)
CORPUS = _CORPUS_CONTRACT


_STUB_BYTES = br"""from __future__ import annotations
import sys
import time

mode = sys.argv[1]
_stdin = sys.stdin.read()
# Intentionally consume stdin but never reflect its content or length.
if mode == "sleep":
    time.sleep(8.0)
elif mode == "changes":
    raise SystemExit(1)
elif mode == "blocked":
    raise SystemExit(2)
elif mode == "malformed":
    sys.stdout.buffer.write(b'{"findings":[{"id":"cut"')
elif mode == "multiple_findings":
    sys.stdout.buffer.write(
        '{"findings":["finding-A","\uc548\uc804 \uacbd\uacc4 finding-B"]}\n'
        'ZTR_VERDICT: PASS\n'.encode("utf-8")
    )
elif mode == "no_findings":
    sys.stdout.buffer.write(b"ZTR_VERDICT: PASS\n")
elif mode == "review_pass":
    sys.stdout.buffer.write(b'{"findings":[]}\nZTR_VERDICT: PASS\n')
else:
    sys.stdout.buffer.write(("leg=" + mode + "\n").encode("utf-8"))
"""


def norm(value: str) -> str:
    """Mask exactly the three measured nondeterminism sources."""

    value = RUNDIR.sub("<RUNDIR>-", value)
    value = TS.sub("<TS>", value)
    value = re.sub(r'"duration_s":\s*[0-9.]+', '"duration_s":<D>', value)
    value = re.sub(r"duration_s: [0-9.]+", "duration_s: <D>", value)
    value = re.sub(
        r'"(std(in|out|err)_path|envelope_path)":\s*"[^"]*[\\/]([^"\\/]+)"',
        r'"\1":"<P>/\3"',
        value,
    )
    return value


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _load_manifest(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise EquivalenceBlocked("manifest is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise EquivalenceBlocked("manifest root must be an object")
    return value


def _report_manifest_sha(report_text: str) -> str:
    matches = re.findall(r"(?m)^manifest_sha256:\s*([0-9a-f]{64})$", report_text)
    if len(matches) != 1:
        raise EquivalenceBlocked("report must contain exactly one manifest_sha256")
    return matches[0]


def _artifact_path(phase_dir: Path, artifact_ref: str) -> Path:
    candidate = (phase_dir / Path(artifact_ref)).resolve()
    phase_root = phase_dir.resolve()
    if not candidate.is_relative_to(phase_root):
        raise EquivalenceBlocked("artifact_ref escapes phase directory")
    return candidate


def _canonical_artifact_fingerprint(raw: bytes, *, index: int) -> str:
    """Normalize only measured envelope metadata; keep CLI output opaque."""

    try:
        artifact = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise EquivalenceBlocked(f"artifact {index} is not strict UTF-8 JSON") from exc
    if not isinstance(artifact, dict):
        raise EquivalenceBlocked(f"artifact {index} root must be an object")

    projected = copy.deepcopy(artifact)
    duration = projected.get("duration_s")
    if isinstance(duration, bool) or not isinstance(duration, int | float):
        raise EquivalenceBlocked(f"artifact {index} duration_s is malformed")
    projected["duration_s"] = "<D>"
    for field in _ARTIFACT_PATH_FIELDS:
        value = projected.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            raise EquivalenceBlocked(f"artifact {index} {field} is malformed")
        projected[field] = f"<P>/{re.split(r'[\\\\/]', value)[-1]}"

    canonical = json.dumps(
        projected, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return _sha256(canonical.encode("utf-8"))


def canonical_projection(phase_dir: Path) -> CanonicalProjection:
    """Verify both original integrity axes, then build canonical projections."""

    manifest_path = phase_dir / "phase-manifest.json"
    report_path = phase_dir / "PHASE_REPORT.md"
    try:
        manifest_raw = manifest_path.read_bytes()
    except OSError as exc:
        raise EquivalenceBlocked("manifest is unavailable") from exc
    manifest = _load_manifest(manifest_raw)

    try:
        report_raw = report_path.read_bytes()
        report_text = report_raw.decode("utf-8", errors="strict")
    except (OSError, UnicodeError) as exc:
        raise EquivalenceBlocked("report is unavailable or not strict UTF-8") from exc
    if _report_manifest_sha(report_text) != _sha256(manifest_raw):
        raise EquivalenceBlocked("manifest_sha256 does not match original manifest bytes")

    projected = copy.deepcopy(manifest)
    entries = projected.get("entries")
    if not isinstance(entries, list):
        raise EquivalenceBlocked("manifest entries must be an array")
    source_entries = manifest.get("entries")
    if not isinstance(source_entries, list) or len(source_entries) != len(entries):
        raise EquivalenceBlocked("manifest entries are invalid")

    projected["created_at_utc"] = "<TS>"
    artifact_fingerprints: dict[str, str] = {}
    for index, (source_entry, entry) in enumerate(
        zip(source_entries, entries, strict=True)
    ):
        if not isinstance(source_entry, dict) or not isinstance(entry, dict):
            raise EquivalenceBlocked(f"manifest entry {index} must be an object")
        if "recorded_at_utc" in entry:
            entry["recorded_at_utc"] = "<TS>"

        artifact_ref = source_entry.get("artifact_ref")
        stored_hash = source_entry.get("content_sha256")
        if artifact_ref is None and stored_hash is None:
            continue
        if not isinstance(artifact_ref, str) or not isinstance(stored_hash, str):
            raise EquivalenceBlocked(f"manifest entry {index} has an incomplete artifact hash pair")
        artifact = _artifact_path(phase_dir, artifact_ref)
        try:
            artifact_raw = artifact.read_bytes()
        except OSError as exc:
            raise EquivalenceBlocked(f"artifact is unavailable for entry {index}") from exc
        if _sha256(artifact_raw) != stored_hash:
            raise EquivalenceBlocked(
                f"content_sha256 does not match original artifact bytes for entry {index}"
            )
        entry["artifact_ref"] = RUNDIR.sub("<RUNDIR>-", artifact_ref)
        # The stored hash remains untouched in the source manifest.  The
        # comparison copy removes that derived, run-specific value and records
        # a distinctly named canonical fingerprint instead.
        entry.pop("content_sha256", None)
        canonical_hash = _canonical_artifact_fingerprint(artifact_raw, index=index)
        entry["canonical_content_sha256"] = canonical_hash
        artifact_fingerprints[stored_hash] = canonical_hash

    manifest_projection = json.dumps(
        projected, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    report_projection = norm(report_text)
    report_projection = report_projection.replace(
        f"manifest_sha256: {_sha256(manifest_raw)}",
        f"manifest_sha256: {_sha256(manifest_projection.encode('utf-8'))}",
        1,
    )
    for stored_hash, canonical_hash in artifact_fingerprints.items():
        report_projection = report_projection.replace(
            f'"content_sha256":"{stored_hash}"',
            f'"content_sha256":"{canonical_hash}"',
        )
    return CanonicalProjection(manifest_projection, report_projection)


def _single_outer(stdout: bytes) -> dict[str, Any]:
    try:
        text = stdout.decode("utf-8", errors="strict")
        lines = [line for line in text.splitlines() if line.strip()]
        value = json.loads(lines[-1]) if lines else None
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise EquivalenceBlocked("run-phase stdout is not a valid outer Envelope") from exc
    if not isinstance(value, dict):
        raise EquivalenceBlocked("run-phase stdout has no outer Envelope object")
    return value


def _write_fixture(root: Path) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=False)
    prompt = root / "prompt.md"
    stub = root / "fixed_leg.py"
    prompt.write_bytes(b"deterministic phase prompt\n")
    stub.write_bytes(_STUB_BYTES)
    return prompt, stub


def _environment(repo_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    pythonpath = os.pathsep.join((str(repo_root / "runtimes" / "ztr"), str(repo_root)))
    env["PYTHONPATH"] = pythonpath
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _require_contracted_corpus() -> None:
    if CORPUS != _CORPUS_CONTRACT:
        raise EquivalenceBlocked(
            "CORPUS does not match the contracted seven shapes",
            details={
                "axis": "corpus",
                "failure": "contract_mismatch",
                "expected": len(_CORPUS_CONTRACT),
                "actual": len(CORPUS),
            },
        )


def execute_case(
    case: CorpusCase,
    *,
    route: str,
    run_root: Path,
    prompt: Path,
    stub: Path,
    python: Path,
    repo_root: Path,
    base_sha: str,
) -> RunResult:
    """Execute one corpus case through BASE or the opt-in projection path."""

    if route not in {"base", "opt_in"}:
        raise EquivalenceBlocked(f"unsupported route: {route}")
    run_root.mkdir(parents=True, exist_ok=False)
    phase_dir = run_root / "phase" if route == "opt_in" else None
    output_dir = phase_dir / "runs" if phase_dir is not None else run_root / "runs"
    def leg(mode: str) -> str:
        return json.dumps([str(python), str(stub), mode], ensure_ascii=False)

    command = [
        str(python),
        "-m",
        "src",
        "run-phase",
        "--prompt-file",
        str(prompt),
        "--phase-id",
        f"equivalence-{case.name}",
        "--output-dir",
        str(output_dir),
        "--timeout",
        str(case.timeout_s),
        "--implementer-cmd",
        leg("implementer"),
        "--mechanical-cmd",
        leg(case.mechanical_mode),
        "--test-cmd",
        leg(case.test_mode),
        "--reviewer-cmd",
        leg(case.reviewer_mode),
    ]
    if phase_dir is not None:
        command.extend(("--phase-dir", str(phase_dir), "--base-sha", base_sha))
    try:
        completed = subprocess.run(
            command,
            cwd=repo_root / "runtimes" / "ztr",
            env=_environment(repo_root),
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EquivalenceBlocked(f"run-phase execution failed for {case.name}/{route}") from exc
    (run_root / "outer-envelope.json").write_bytes(completed.stdout)
    (run_root / "stderr.txt").write_bytes(completed.stderr)
    outer = _single_outer(completed.stdout)
    if outer.get("exit_code") != completed.returncode or outer.get("status") not in {
        "PASS",
        "CHANGES_REQUESTED",
        "BLOCKED",
    }:
        raise EquivalenceBlocked(f"outer Envelope contract mismatch for {case.name}/{route}")
    projection = canonical_projection(phase_dir) if phase_dir is not None else None
    return RunResult(route, completed.returncode, outer, phase_dir, projection)


def run_corpus(
    output_root: Path,
    *,
    python: Path,
    repo_root: Path = REPO_ROOT,
    base_sha: str,
) -> dict[str, Any]:
    """Run all seven shapes twice through both routes and assert reproducibility."""

    _require_contracted_corpus()
    if not BASE_SHA_PATTERN.fullmatch(base_sha):
        raise EquivalenceBlocked("base_sha must be 40 lowercase hexadecimal characters")
    if output_root.exists():
        raise EquivalenceBlocked("output_root must not already exist")
    fixture_root = output_root / "fixture"
    prompt, stub = _write_fixture(fixture_root)
    cases_payload: list[dict[str, Any]] = []
    for case in CORPUS:
        opt_in_projections: list[CanonicalProjection] = []
        returncodes: dict[str, list[int]] = {"base": [], "opt_in": []}
        for repeat in range(2):
            for route in ("base", "opt_in"):
                result = execute_case(
                    case,
                    route=route,
                    run_root=output_root / case.name / f"repeat-{repeat + 1}" / route,
                    prompt=prompt,
                    stub=stub,
                    python=python,
                    repo_root=repo_root,
                    base_sha=base_sha,
                )
                returncodes[route].append(result.returncode)
                if result.projection is not None:
                    opt_in_projections.append(result.projection)
        if len(opt_in_projections) != 2:
            raise EquivalenceBlocked(f"missing opt-in projections for {case.name}")
        first, second = opt_in_projections
        manifest_ok = first.manifest == second.manifest
        report_ok = first.report == second.report
        if not manifest_ok or not report_ok:
            raise EquivalenceBlocked(f"canonical projection differs for {case.name}")
        cases_payload.append(
            {
                "name": case.name,
                "base_returncodes": returncodes["base"],
                "opt_in_returncodes": returncodes["opt_in"],
                "manifest": "OK",
                "report": "OK",
            }
        )
    return {"status": PASS, "exit_code": 0, "cases": cases_payload}


def _observation_from_outer(
    outer: dict[str, Any], *, route: str
) -> RouteObservation:
    """Parse structural step evidence; keep stdout_preview completely opaque."""

    inner_raw = outer.get("stdout")
    if not isinstance(inner_raw, str):
        raise EquivalenceBlocked(
            "inner report is missing",
            details={"route": route, "axis": "inner_report", "failure": "missing"},
        )
    try:
        inner = json.loads(inner_raw)
    except json.JSONDecodeError as exc:
        raise EquivalenceBlocked(
            "inner report is not valid JSON",
            details={
                "route": route,
                "axis": "inner_report",
                "failure": "parse_failure",
            },
        ) from exc
    if not isinstance(inner, dict) or "steps" not in inner:
        raise EquivalenceBlocked(
            "inner report steps are missing",
            details={"route": route, "axis": "steps", "failure": "missing"},
        )
    raw_steps = inner["steps"]
    if not isinstance(raw_steps, list):
        raise EquivalenceBlocked(
            "inner report steps are malformed",
            details={"route": route, "axis": "steps", "failure": "parse_failure"},
        )

    required = {"name", "status", "exit_code", "stdout_preview", "gating", "skipped"}
    observations: list[LegObservation] = []
    for index, step in enumerate(raw_steps):
        missing = sorted(required.difference(step)) if isinstance(step, dict) else []
        if missing:
            raise EquivalenceBlocked(
                "inner report step fields are missing",
                details={
                    "route": route,
                    "axis": "steps",
                    "failure": "missing",
                    "index": index,
                    "fields": missing,
                },
            )
        if not isinstance(step, dict):
            raise EquivalenceBlocked(
                "inner report step is malformed",
                details={
                    "route": route,
                    "axis": "steps",
                    "failure": "parse_failure",
                    "index": index,
                },
            )
        name, verdict = step["name"], step["status"]
        exit_code, stdout_preview = step["exit_code"], step["stdout_preview"]
        gating, skipped = step["gating"], step["skipped"]
        if not (
            isinstance(name, str)
            and name
            and verdict in {"PASS", "CHANGES_REQUESTED", "BLOCKED"}
            and type(exit_code) is int
            and isinstance(stdout_preview, str)
            and type(gating) is bool
            and type(skipped) is bool
        ):
            raise EquivalenceBlocked(
                "inner report step fields are malformed",
                details={
                    "route": route,
                    "axis": "steps",
                    "failure": "parse_failure",
                    "index": index,
                },
            )
        observations.append(
            LegObservation(name, verdict, exit_code, stdout_preview, gating, skipped)
        )
    return RouteObservation(route, tuple(observations))


def compare_observations(
    case_name: str, base: RouteObservation, opt_in: RouteObservation
) -> dict[str, Any]:
    """Require exact ordered gating values and exact unmasked stdout previews."""

    base_legs = base.gating_legs
    opt_in_legs = opt_in.gating_legs
    base_values = tuple((leg.name, leg.verdict, leg.exit_code) for leg in base_legs)
    opt_in_values = tuple(
        (leg.name, leg.verdict, leg.exit_code) for leg in opt_in_legs
    )
    if not base_legs or not opt_in_legs:
        raise EquivalenceBlocked(
            f"gating leg missing for {case_name}",
            details={
                "case": case_name,
                "axis": "gating_legs",
                "failure": "missing",
                "index": 0,
                "base": base_values[0] if base_values else None,
                "opt_in": opt_in_values[0] if opt_in_values else None,
            },
        )
    if base_values != opt_in_values:
        paired = zip(base_values, opt_in_values)
        index = next(
            (i for i, (base_value, opt_value) in enumerate(paired) if base_value != opt_value),
            min(len(base_values), len(opt_in_values)),
        )
        failure = "missing" if index == min(len(base_values), len(opt_in_values)) else "mismatch"
        raise EquivalenceBlocked(
            f"gating leg {failure} for {case_name}",
            details={
                "case": case_name,
                "axis": "gating_legs",
                "failure": failure,
                "index": index,
                "base": base_values[index] if index < len(base_values) else None,
                "opt_in": opt_in_values[index] if index < len(opt_in_values) else None,
            },
        )

    for index, (base_leg, opt_in_leg) in enumerate(
        zip(base_legs, opt_in_legs, strict=True)
    ):
        if base_leg.stdout_preview != opt_in_leg.stdout_preview:
            raise EquivalenceBlocked(
                f"finding text differs for {case_name}",
                details={
                    "case": case_name,
                    "axis": "finding_text",
                    "failure": "mismatch",
                    "index": index,
                    "leg": base_leg.name,
                    "base": base_leg.stdout_preview,
                    "opt_in": opt_in_leg.stdout_preview,
                },
            )
    return {
        "name": case_name,
        "gating_legs": "MATCH",
        "finding_text": "MATCH",
    }


def compare_corpus(
    output_root: Path,
    *,
    python: Path,
    repo_root: Path = REPO_ROOT,
    base_sha: str,
) -> dict[str, Any]:
    """Run all shapes once on both routes, compare them, and count actual legs."""

    _require_contracted_corpus()
    if not BASE_SHA_PATTERN.fullmatch(base_sha):
        raise EquivalenceBlocked("base_sha must be 40 lowercase hexadecimal characters")
    if output_root.exists():
        raise EquivalenceBlocked("output_root must not already exist")
    prompt, stub = _write_fixture(output_root / "fixture")
    cases_payload: list[dict[str, Any]] = []
    counters = {
        "external_cli_calls": {"base": 0, "opt_in": 0},
        "full_suite_runs": {"base": 0, "opt_in": 0},
    }
    for case in CORPUS:
        try:
            results = {
                route: execute_case(
                    case,
                    route=route,
                    run_root=output_root / case.name / route,
                    prompt=prompt,
                    stub=stub,
                    python=python,
                    repo_root=repo_root,
                    base_sha=base_sha,
                )
                for route in ("base", "opt_in")
            }
            observations = {
                route: _observation_from_outer(result.outer, route=route)
                for route, result in results.items()
            }
            comparison = compare_observations(
                case.name, observations["base"], observations["opt_in"]
            )
        except EquivalenceBlocked as exc:
            details = dict(exc.details)
            details.setdefault("case", case.name)
            details["completed_cases"] = cases_payload
            raise EquivalenceBlocked(str(exc), details=details) from exc

        case_counts: dict[str, dict[str, int]] = {
            "external_cli_calls": {},
            "full_suite_runs": {},
        }
        for route, observation in observations.items():
            case_counts["external_cli_calls"][route] = observation.external_cli_calls
            case_counts["full_suite_runs"][route] = observation.full_suite_runs
            counters["external_cli_calls"][route] += observation.external_cli_calls
            counters["full_suite_runs"][route] += observation.full_suite_runs
        comparison["counters"] = case_counts
        cases_payload.append(comparison)
    return {
        "status": PASS,
        "exit_code": 0,
        "cases": cases_payload,
        "counters": counters,
    }


def _build_parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(prog="python -m methodology.tools.phase_equivalence")
    subparsers = parser.add_subparsers(dest="command", required=True)
    corpus = subparsers.add_parser("corpus")
    corpus.add_argument("--output-root", required=True, type=Path)
    corpus.add_argument("--python", type=Path, default=Path(sys.executable))
    corpus.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    corpus.add_argument("--base-sha", required=True)
    compare = subparsers.add_parser("compare")
    compare.add_argument("--output-root", required=True, type=Path)
    compare.add_argument("--python", type=Path, default=Path(sys.executable))
    compare.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    compare.add_argument("--base-sha", required=True)
    project = subparsers.add_parser("project")
    project.add_argument("--phase-dir", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Emit exactly one compact JSON line and return its declared exit code."""

    try:
        args = _build_parser().parse_args(argv)
        if args.command == "corpus":
            payload = run_corpus(
                args.output_root,
                python=args.python,
                repo_root=args.repo_root,
                base_sha=args.base_sha,
            )
        elif args.command == "compare":
            payload = compare_corpus(
                args.output_root,
                python=args.python,
                repo_root=args.repo_root,
                base_sha=args.base_sha,
            )
        else:
            projection = canonical_projection(args.phase_dir)
            payload = {
                "status": PASS,
                "exit_code": 0,
                "manifest": projection.manifest,
                "report": projection.report,
            }
    except EquivalenceBlocked as exc:
        payload = {"status": BLOCKED, "exit_code": 2, "message": str(exc)}
        if exc.details:
            payload["details"] = exc.details
    except Exception as exc:
        payload = {
            "status": BLOCKED,
            "exit_code": 2,
            "message": f"internal failure: {type(exc).__name__}",
        }
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return int(payload["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
