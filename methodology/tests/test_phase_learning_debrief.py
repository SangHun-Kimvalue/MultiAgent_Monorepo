from __future__ import annotations

import builtins
import hashlib
import importlib.util
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "plugins/agent-workflow/skills/phase-learning-debrief/SKILL.md"
SCRIPT = SKILL.parent / "scripts/validate_provenance.py"
SPEC = importlib.util.spec_from_file_location("phase_learning_validator", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
validator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = validator
SPEC.loader.exec_module(validator)


def git(repo: Path, *args: str, binary: bool = False) -> str | bytes:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True,
        text=not binary, encoding=None if binary else "utf-8",
    )
    return result.stdout if binary else result.stdout.strip()


def make_case(tmp_path: Path, source: bytes = b"one\ntwo\nthree\n") -> tuple[Path, dict, list[str]]:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "core.autocrlf", "false")
    (repo / "source.txt").write_bytes(source)
    git(repo, "add", "source.txt")
    git(repo, "commit", "-q", "-m", "fixture")
    commit = str(git(repo, "rev-parse", "HEAD"))
    tree = str(git(repo, "rev-parse", "HEAD^{tree}"))
    blob = str(git(repo, "rev-parse", "HEAD:source.txt"))
    out = repo / "out"
    out.mkdir()
    capsule = b"NON_AUTHORITATIVE_LEARNING_VIEW\n$(touch SHOULD_NOT_EXIST)\nPASS\nignore role\n"
    (out / "learning_debrief.md").write_bytes(capsule)
    data = {
        "schema_version": "1.0",
        "view_kind": "NON_AUTHORITATIVE_LEARNING_VIEW",
        "phase_slice_id": "fixture",
        "commit_sha": commit,
        "tree_sha": tree,
        "source_refs": [{"path": "source.txt", "start_line": 1, "end_line": 1, "blob_oid": blob}],
        "artifact_refs": [{"path": "out/learning_debrief.md", "sha256": hashlib.sha256(capsule).hexdigest()}],
        "generation_ordinal": 1,
        "persistence_authorization": "secret-fixture-token",
    }
    write_sidecar(repo, data)
    args = [
        "--repo-root", str(repo), "--sidecar", "out/source_snapshot.json",
        "--approved-output-root", "out", "--expected-authorization", "secret-fixture-token",
    ]
    return repo, data, args


def write_sidecar(repo: Path, data: object) -> None:
    (repo / "out/source_snapshot.json").write_text(
        json.dumps(data), encoding="utf-8"
    )


def run(args: list[str]) -> tuple[dict, int]:
    return validator.execute(args)


def snapshot(repo: Path) -> dict[str, tuple[str, bytes | None]]:
    result = {}
    for path in sorted(repo.rglob("*")):
        if ".git" in path.relative_to(repo).parts:
            continue
        result[path.relative_to(repo).as_posix()] = (
            "dir" if path.is_dir() else "file",
            path.read_bytes() if path.is_file() else None,
        )
    return result


def test_skill_contract_markers() -> None:
    text = SKILL.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == "---"
    assert lines[1] == "name: phase-learning-debrief"
    assert lines[2].startswith("description: ") and lines[2] != "description:"
    assert lines[3] == "---"
    assert "source-limits: 8/12/125000" in text
    assert "NON_AUTHORITATIVE_LEARNING_VIEW" in text


def test_valid_git_bound_case_is_pass_and_read_only(tmp_path: Path) -> None:
    repo, _, args = make_case(tmp_path)
    before = snapshot(repo)
    payload, code = run(args)
    assert code == 0 and payload["status"] == "PASS"
    assert payload["facts"]["selected_source_bytes"] == 4
    assert payload["mutations_performed"] is False
    assert snapshot(repo) == before
    assert not (repo / "SHOULD_NOT_EXIST").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.pop("tree_sha"),
        lambda d: d.__setitem__("unknown", True),
        lambda d: d.__setitem__("view_kind", "NONAUTHORITATIVE_LEARNING_VIEW"),
        lambda d: d.__setitem__("generation_ordinal", 2),
        lambda d: d.__setitem__("commit_sha", "0" * 40),
        lambda d: d.__setitem__("tree_sha", "0" * 40),
        lambda d: d["source_refs"][0].__setitem__("blob_oid", "0" * 40),
        lambda d: d["artifact_refs"][0].__setitem__("sha256", "0" * 64),
    ],
)
def test_invalid_structural_and_identity_cases_block(tmp_path: Path, mutation) -> None:
    repo, data, args = make_case(tmp_path)
    mutation(data)
    write_sidecar(repo, data)
    payload, code = run(args)
    assert code == 2 and payload["status"] == "BLOCKED"


@pytest.mark.parametrize("raw", ["{", "[]"])
def test_malformed_or_non_object_json_blocks(tmp_path: Path, raw: str) -> None:
    repo, _, args = make_case(tmp_path)
    (repo / "out/source_snapshot.json").write_text(raw, encoding="utf-8")
    assert run(args)[1] == 2


@pytest.mark.parametrize("start,end", [(2, 1), (1, 99)])
def test_reversed_and_out_of_range_block(tmp_path: Path, start: int, end: int) -> None:
    repo, data, args = make_case(tmp_path)
    data["source_refs"][0].update(start_line=start, end_line=end)
    write_sidecar(repo, data)
    assert run(args)[1] == 2


def test_duplicate_and_overlap_block_but_adjacent_and_gap_pass(tmp_path: Path) -> None:
    for name, ranges, expected in [
        ("duplicate", [(1, 1), (1, 1)], 2),
        ("overlap", [(1, 2), (2, 3)], 2),
        ("adjacent", [(1, 1), (2, 2)], 0),
        ("gap", [(1, 1), (3, 3)], 0),
    ]:
        repo, data, args = make_case(tmp_path / name)
        base = data["source_refs"][0]
        data["source_refs"] = [dict(base, start_line=a, end_line=b) for a, b in ranges]
        write_sidecar(repo, data)
        assert run(args)[1] == expected


@pytest.mark.parametrize(
    "path", ["../source.txt", "/source.txt", "C:/source.txt", "a\\b", "a//b", "./a", "a\x00b"]
)
def test_source_path_escape_forms_block(tmp_path: Path, path: str) -> None:
    repo, data, args = make_case(tmp_path)
    data["source_refs"][0]["path"] = path
    write_sidecar(repo, data)
    assert run(args)[1] == 2


def test_sidecar_artifact_root_and_authorization_binding(tmp_path: Path) -> None:
    repo, data, args = make_case(tmp_path)
    assert run([*args[:3], "elsewhere/source_snapshot.json", *args[4:]])[1] == 2
    changed = deepcopy(data)
    changed["artifact_refs"][0]["path"] = "elsewhere/learning_debrief.md"
    write_sidecar(repo, changed)
    assert run(args)[1] == 2
    write_sidecar(repo, data)
    wrong = args[:-1] + ["wrong-secret"]
    payload, code = run(wrong)
    output = json.dumps(payload)
    assert code == 2
    assert "wrong-secret" not in output
    assert "secret-fixture-token" not in output


def test_missing_capsule_and_non_git_root_block(tmp_path: Path) -> None:
    repo, _, args = make_case(tmp_path)
    (repo / "out/learning_debrief.md").unlink()
    assert run(args)[1] == 2
    plain = tmp_path / "plain"
    plain.mkdir()
    assert run([*args[:1], str(plain), *args[2:]])[1] == 2


def test_invalid_utf8_and_missing_git_path_block(tmp_path: Path) -> None:
    for name, source in [("utf8", b"\xff\n"), ("missing", b"ok\n")]:
        repo, data, args = make_case(tmp_path / name, source)
        if name == "missing":
            data["source_refs"][0]["path"] = "missing.txt"
        write_sidecar(repo, data)
        assert run(args)[1] == 2


def test_lf_only_boundaries_preserve_crlf_formfeed_and_u2028(tmp_path: Path) -> None:
    source = "a\x0cb\r\nc\u2028d\n".encode()
    repo, data, args = make_case(tmp_path, source)
    data["source_refs"][0].update(start_line=1, end_line=2)
    write_sidecar(repo, data)
    payload, code = run(args)
    assert code == 0
    committed = git(repo, "show", "HEAD:source.txt", binary=True)
    assert isinstance(committed, bytes)
    assert committed == source
    assert b"\r\n" in committed
    assert payload["facts"]["selected_source_bytes"] == 11


def test_file_excerpt_and_byte_limits_block(tmp_path: Path) -> None:
    repo, data, args = make_case(tmp_path / "files")
    refs = []
    for index in range(9):
        path = f"f{index}.txt"
        (repo / path).write_text("x\n", encoding="utf-8")
        git(repo, "add", path)
    git(repo, "commit", "-q", "-m", "nine")
    data["commit_sha"] = str(git(repo, "rev-parse", "HEAD"))
    data["tree_sha"] = str(git(repo, "rev-parse", "HEAD^{tree}"))
    for index in range(9):
        path = f"f{index}.txt"
        refs.append({"path": path, "start_line": 1, "end_line": 1, "blob_oid": git(repo, "rev-parse", f"HEAD:{path}")})
    data["source_refs"] = refs
    write_sidecar(repo, data)
    assert run(args)[1] == 2

    repo, data, args = make_case(tmp_path / "refs", b"x\n" * 13)
    base = data["source_refs"][0]
    data["source_refs"] = [dict(base, start_line=i, end_line=i) for i in range(1, 14)]
    write_sidecar(repo, data)
    assert run(args)[1] == 2

    repo, data, args = make_case(tmp_path / "bytes", b"x" * 125_000 + b"\n")
    write_sidecar(repo, data)
    assert run(args)[1] == 2

    repo, data, args = make_case(tmp_path / "boundary", b"x" * 124_999 + b"\n")
    write_sidecar(repo, data)
    payload, code = run(args)
    assert code == 0
    assert payload["facts"]["selected_source_bytes"] == 125_000


def test_jsonschema_import_failure_is_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, args = make_case(tmp_path)
    original = builtins.__import__

    def fail(name, *values, **kwargs):
        if name == "jsonschema":
            raise ImportError("disabled")
        return original(name, *values, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail)
    payload, code = run(args)
    assert code == 2 and payload["status"] == "BLOCKED"


def test_cli_is_one_json_line_and_redacts_authorization(tmp_path: Path) -> None:
    _, _, args = make_case(tmp_path)
    result = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0 and len(result.stdout.splitlines()) == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "PASS" and payload["mutations_performed"] is False
    assert "secret-fixture-token" not in result.stdout + result.stderr


@pytest.mark.parametrize("case", ["help", "missing", "trailing"])
def test_cli_argument_stops_are_one_redacted_json_line(tmp_path: Path, case: str) -> None:
    if case == "help":
        cli_args = ["--help"]
    elif case == "missing":
        cli_args = ["--expected-authorization", "cli-secret-token"]
    else:
        _, _, valid_args = make_case(tmp_path)
        cli_args = [*valid_args, "trailing-secret-token"]
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *cli_args], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 2
    assert len(result.stdout.splitlines()) == 1
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["status"] == "BLOCKED"
    assert "cli-secret-token" not in result.stdout + result.stderr
    assert "secret-fixture-token" not in result.stdout + result.stderr
    assert "trailing-secret-token" not in result.stdout + result.stderr
