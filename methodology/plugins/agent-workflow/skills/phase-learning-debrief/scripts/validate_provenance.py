"""Fail-closed, read-only validation for a phase learning provenance sidecar."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

PASS = "PASS"
BLOCKED = "BLOCKED"
MAX_FILES = 8
MAX_REFS = 12
MAX_BYTES = 125_000
SCHEMA = Path(__file__).resolve().parent.parent / "assets" / "provenance.schema.json"


class _FailClosedArgumentParser(argparse.ArgumentParser):
    def _print_message(self, message: str | None, file: Any = None) -> None:
        del message, file

    def error(self, message: str) -> None:
        del message
        raise ValueError("argument parsing failed")

    def exit(self, status: int = 0, message: str | None = None) -> None:
        del status
        raise ValueError(message or "argument parsing stopped")


def _object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid UTF-8 JSON: {type(exc).__name__}") from None
    if not isinstance(value, dict):
        raise ValueError(f"{label} root must be an object")
    return value


def _relative(value: str, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ValueError(f"{label} must be a non-empty strict POSIX path")
    if re.match(r"^[A-Za-z]:", value) or value.startswith("/"):
        raise ValueError(f"{label} must be repo-relative")
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError(f"{label} contains a forbidden segment")
    return path


def _inside(repo: Path, rel: PurePosixPath, label: str) -> Path:
    target = repo.joinpath(*rel.parts).resolve()
    if not target.is_relative_to(repo):
        raise ValueError(f"{label} escapes repository")
    return target


def _git(repo: Path, *args: str, binary: bool = False) -> str | bytes:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, check=False,
        text=not binary, encoding=None if binary else "utf-8", errors=None if binary else "strict",
    )
    if result.returncode != 0:
        raise ValueError(f"Git fact unavailable: {' '.join(args[:2])}")
    return result.stdout if binary else result.stdout.strip()


def _selected_bytes(blob: bytes, start: int, end: int) -> int:
    blob.decode("utf-8", errors="strict")
    # splitlines treats form-feed/U+2028 as boundaries, so use LF bytes only.
    lines = []
    offset = 0
    for index, byte in enumerate(blob):
        if byte == 10:
            lines.append(blob[offset : index + 1])
            offset = index + 1
    if offset < len(blob):
        lines.append(blob[offset:])
    if start > end or end > len(lines):
        raise ValueError("source range is reversed or out of bounds")
    return sum(len(line) for line in lines[start - 1 : end])


def validate(repo: Path, sidecar_rel: str, root_rel: str, expected_auth: str) -> tuple[list[str], dict[str, Any]]:
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        return ["python dependency unavailable: jsonschema"], {}
    try:
        repo = repo.resolve()
        top = Path(str(_git(repo, "rev-parse", "--show-toplevel"))).resolve()
        if top != repo:
            raise ValueError("repo-root is not the Git top-level")
        root = _relative(root_rel, "approved-output-root")
        sidecar = _relative(sidecar_rel, "sidecar")
        if sidecar != root / "source_snapshot.json":
            raise ValueError("sidecar is not the approved root sidecar")
        data = _object(_inside(repo, sidecar, "sidecar"), "sidecar")
        schema = _object(SCHEMA, "schema")
        errors = sorted(Draft202012Validator(schema).iter_errors(data), key=lambda e: list(e.absolute_path))
        if errors:
            raise ValueError(f"schema violation at {errors[0].json_path}")
        if data["persistence_authorization"] != expected_auth:
            raise ValueError("authorization mismatch")

        commit = data["commit_sha"]
        if _git(repo, "cat-file", "-t", commit) != "commit":
            raise ValueError("bound object is not a commit")
        tree = str(_git(repo, "rev-parse", f"{commit}^{{tree}}"))
        if tree != data["tree_sha"]:
            raise ValueError("tree mismatch")

        refs = data["source_refs"]
        paths = {_relative(ref["path"], "source path").as_posix() for ref in refs}
        if len(paths) > MAX_FILES or len(refs) > MAX_REFS:
            raise ValueError("source limits exceeded")
        ranges: dict[str, list[tuple[int, int]]] = {}
        selected = 0
        for ref in refs:
            path = _relative(ref["path"], "source path").as_posix()
            start, end = ref["start_line"], ref["end_line"]
            if start > end:
                raise ValueError("source range is reversed")
            prior = ranges.setdefault(path, [])
            if any(start <= old_end and old_start <= end for old_start, old_end in prior):
                raise ValueError("duplicate or overlapping source range")
            prior.append((start, end))
            oid = str(_git(repo, "rev-parse", f"{commit}:{path}"))
            if oid != ref["blob_oid"] or _git(repo, "cat-file", "-t", oid) != "blob":
                raise ValueError("blob mismatch")
            blob = _git(repo, "show", f"{commit}:{path}", binary=True)
            assert isinstance(blob, bytes)
            selected += _selected_bytes(blob, start, end)
        if selected > MAX_BYTES:
            raise ValueError("selected source byte limit exceeded")

        artifact = data["artifact_refs"][0]
        artifact_path = _relative(artifact["path"], "artifact path")
        if artifact_path != root / "learning_debrief.md":
            raise ValueError("artifact is not the approved capsule")
        capsule = _inside(repo, artifact_path, "artifact path").read_bytes()
        if hashlib.sha256(capsule).hexdigest() != artifact["sha256"]:
            raise ValueError("capsule digest mismatch")
        facts = {
            "commit_sha": commit, "tree_sha": tree, "source_ref_count": len(refs),
            "unique_source_count": len(paths), "selected_source_bytes": selected,
            "artifact_count": 1, "authorization_matches": True,
        }
        return [], facts
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError) as exc:
        return [str(exc).strip() or type(exc).__name__], {}


def execute(argv: Sequence[str] | None = None) -> tuple[dict[str, Any], int]:
    parser = _FailClosedArgumentParser(description="Validate phase learning provenance")
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--sidecar", required=True)
    parser.add_argument("--approved-output-root", required=True)
    parser.add_argument("--expected-authorization", required=True)
    try:
        args = parser.parse_args(argv)
        diagnostics, facts = validate(
            args.repo_root, args.sidecar, args.approved_output_root, args.expected_authorization
        )
    except Exception as exc:
        diagnostics, facts = [str(exc).strip() or type(exc).__name__], {}
    status = PASS if not diagnostics else BLOCKED
    payload = {
        "status": status, "diagnostics": diagnostics, "facts": facts,
        "mutations_performed": False,
    }
    return payload, 0 if status == PASS else 2


def main(argv: Sequence[str] | None = None) -> int:
    payload, code = execute(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return code


if __name__ == "__main__":
    sys.exit(main())
