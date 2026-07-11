#!/usr/bin/env python3
"""Local Nitpicker wrapper for Claude Code beta deployments.

The wrapper reads git diffs inside Python as UTF-8 and sends them to a local
provider, usually Ollama. It intentionally avoids passing raw diffs through
shell arguments, which keeps Korean text, quotes, spaces, and newlines intact.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path.cwd()
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "nitpicker.config.json"
EXAMPLE_CONFIG_PATH = SCRIPT_DIR / "nitpicker.config.example.json"
HEADER_STATUS_SCAN_LINES = 5
STATUS_PRIORITY = ("BLOCKED", "CHANGES_REQUESTED", "ALL PASS")
STATUS_PATTERNS = {
    "BLOCKED": re.compile(r"^BLOCKED\b"),
    "CHANGES_REQUESTED": re.compile(r"^CHANGES_REQUESTED\b"),
    "ALL PASS": re.compile(r"^ALL PASS\b"),
}
STATUS_PREFIX_RE = re.compile(r"^(?:STATUS|RESULT)\s*:\s*", re.IGNORECASE)


DEFAULT_CONFIG: dict[str, Any] = {
    "provider": "ollama",
    "base_url": "http://localhost:11434",
    "model": "qwen2.5-coder:7b",
    "default_scope": "changed",
    "review_docs_by_default": False,
    "timeout_seconds": 120,
    "max_diff_chars": 60000,
    "include_extensions": [
        ".c",
        ".cc",
        ".cpp",
        ".cxx",
        ".h",
        ".hh",
        ".hpp",
        ".hxx",
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cmake",
    ],
    "exclude_prefixes": [
        ".git/",
        ".venv/",
        "node_modules/",
        "dist/",
        "build/",
        "out/",
        "output/",
        "outputs/",
        "tmp/",
        ".claude/",
        ".agent-workflow-backup/",
    ],
}


def load_config() -> dict[str, Any]:
    path = CONFIG_PATH if CONFIG_PATH.exists() else EXAMPLE_CONFIG_PATH
    if not path.exists():
        return dict(DEFAULT_CONFIG)
    with path.open("r", encoding="utf-8") as fh:
        loaded = json.load(fh)
    config = dict(DEFAULT_CONFIG)
    config.update(loaded)
    return config


def run_git(args: list[str], *, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=REPO,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout


def normalize_path(path: str) -> str:
    return path.replace("\\", "/").strip()


def is_reviewable(path: str, config: dict[str, Any], *, include_all: bool) -> bool:
    normalized = normalize_path(path)
    if not normalized:
        return False
    if not include_all:
        if any(normalized.startswith(prefix) for prefix in config["exclude_prefixes"]):
            return False
        if Path(normalized).suffix.lower() not in set(config["include_extensions"]):
            return False
    return (REPO / normalized).is_file()


def changed_files(config: dict[str, Any], *, staged: bool, include_all: bool) -> list[str]:
    args = ["diff", "--name-only"]
    if staged:
        args.append("--cached")
    else:
        args.append("HEAD")
    args.append("--")
    files = [normalize_path(line) for line in run_git(args).splitlines()]
    if not staged:
        files.extend(
            normalize_path(line)
            for line in run_git(["ls-files", "--others", "--exclude-standard"]).splitlines()
        )

    seen: set[str] = set()
    result: list[str] = []
    for path in files:
        if path in seen:
            continue
        if is_reviewable(path, config, include_all=include_all):
            seen.add(path)
            result.append(path)
    return result


def diff_for_file(path: str, *, staged: bool, max_diff_chars: int) -> str:
    args = ["diff"]
    if staged:
        args.append("--cached")
    else:
        args.append("HEAD")
    args.extend(["--", path])
    diff = run_git(args)
    if diff.strip():
        return truncate_diff(diff, max_diff_chars)

    full_path = REPO / path
    if full_path.exists():
        content = full_path.read_text(encoding="utf-8", errors="replace")
        synthetic = f"diff --git a/{path} b/{path}\n--- /dev/null\n+++ b/{path}\n"
        synthetic += "".join(f"+{line}" for line in content.splitlines(keepends=True))
        return truncate_diff(synthetic, max_diff_chars)
    return ""


def truncate_diff(diff: str, max_chars: int) -> str:
    if len(diff) <= max_chars:
        return diff
    return diff[:max_chars] + "\n\n[TRUNCATED: diff exceeded max_diff_chars]\n"


def build_prompt(path: str, diff: str) -> str:
    return f"""You are Nitpicker, a strict senior code reviewer.

Review the git diff below. Focus on correctness, regressions, ownership/SSOT,
runtime risk, validation gaps, and maintainability. Do not nitpick style unless
it can cause real confusion or defects.

The first line must contain only one status token exactly: ALL PASS,
CHANGES_REQUESTED, or BLOCKED. Do not add Markdown, STATUS:/RESULT: prefixes,
or any prose before the token.

Return exactly one of these statuses at the top:
- ALL PASS
- CHANGES_REQUESTED
- BLOCKED

Then list findings with severity and evidence. If no actionable issue exists,
explain briefly why the diff is acceptable.

File: {path}

```diff
{diff}
```
"""


def call_ollama(config: dict[str, Any], prompt: str) -> str:
    base_url = str(config["base_url"]).rstrip("/")
    payload = {
        "model": config["model"],
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    request = urllib.request.Request(
        f"{base_url}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    timeout = int(config["timeout_seconds"])
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Ollama request failed: {exc}") from exc
    return data.get("message", {}).get("content", "").strip()


def mock_review(path: str, diff: str) -> str:
    marker = "TODO_" + "NITPICKER_BLOCK"
    if marker in diff:
        return f"CHANGES_REQUESTED\n- P2 {path}: mock marker found."
    return f"ALL PASS\n- Mock provider checked {path}; no marker findings."


def _normalize_status_line(line: str) -> str:
    normalized = line.strip().lstrip(" \t*#>`").strip()
    normalized = STATUS_PREFIX_RE.sub("", normalized, count=1).strip()
    return normalized.upper()


def _extract_status(output: str) -> str:
    lines = output.splitlines()
    start_index = next((index for index, line in enumerate(lines) if line.strip()), None)
    if start_index is None:
        return "BLOCKED"

    found: set[str] = set()
    header = lines[start_index : start_index + HEADER_STATUS_SCAN_LINES]
    for line in header:
        normalized = _normalize_status_line(line)
        for status in STATUS_PRIORITY:
            if STATUS_PATTERNS[status].match(normalized):
                found.add(status)

    for status in STATUS_PRIORITY:
        if status in found:
            return status
    return "BLOCKED"


def review_file(path: str, config: dict[str, Any], *, provider: str, staged: bool) -> tuple[str, str]:
    diff = diff_for_file(path, staged=staged, max_diff_chars=int(config["max_diff_chars"]))
    if not diff.strip():
        return "ALL PASS", f"ALL PASS\n- {path}: no diff."
    if provider == "mock":
        output = mock_review(path, diff)
    elif provider == "ollama":
        output = call_ollama(config, build_prompt(path, diff))
    else:
        raise RuntimeError(f"unsupported provider: {provider}")

    status = _extract_status(output)
    return status, output


def self_test(config: dict[str, Any]) -> int:
    problems: list[str] = []
    try:
        run_git(["rev-parse", "--show-toplevel"])
    except Exception as exc:  # noqa: BLE001
        problems.append(f"git repository check failed: {exc}")
    if not isinstance(config.get("provider"), str):
        problems.append("config.provider must be a string")
    if not isinstance(config.get("include_extensions"), list):
        problems.append("config.include_extensions must be a list")

    if problems:
        print("BLOCKED")
        for problem in problems:
            print(f"- {problem}")
        return 3

    print("ALL PASS")
    print(f"- repo: {REPO}")
    print(f"- config: {CONFIG_PATH if CONFIG_PATH.exists() else EXAMPLE_CONFIG_PATH}")
    print("- mock provider available")
    print("- ollama provider configured but not contacted by --self-test")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local Nitpicker review.")
    parser.add_argument("files", nargs="*", help="Specific files to review")
    parser.add_argument("--repo", help="Git repository root to review. Defaults to current working directory.")
    parser.add_argument("--changed", action="store_true", help="Review changed files")
    parser.add_argument("--staged", action="store_true", help="Review staged files")
    parser.add_argument("--include-all", action="store_true", help="Include docs and non-default extensions")
    parser.add_argument("--keep-going", action="store_true", help="Continue after a file returns findings")
    parser.add_argument("--provider", choices=["ollama", "mock"], help="Override configured provider")
    parser.add_argument("--self-test", action="store_true", help="Validate local wrapper setup without calling Ollama")
    return parser.parse_args()


def main() -> int:
    global REPO
    args = parse_args()
    if args.repo:
        REPO = Path(args.repo).resolve()
    config = load_config()
    provider = args.provider or str(config["provider"])

    if args.self_test:
        return self_test(config)

    staged = bool(args.staged)
    if args.files:
        files = [normalize_path(path) for path in args.files]
    else:
        files = changed_files(config, staged=staged, include_all=args.include_all)

    if not files:
        print("ALL PASS")
        print("- No reviewable files found.")
        return 0

    overall = "ALL PASS"
    for path in files:
        try:
            status, output = review_file(path, config, provider=provider, staged=staged)
        except Exception as exc:  # noqa: BLE001
            status = "BLOCKED"
            output = f"BLOCKED\n- {path}: {exc}"

        print(f"\n===== {path} =====")
        print(output)

        if status == "BLOCKED":
            overall = "BLOCKED"
            if not args.keep_going:
                break
        elif status == "CHANGES_REQUESTED" and overall != "BLOCKED":
            overall = "CHANGES_REQUESTED"
            if not args.keep_going:
                break

    print(f"\nNitpicker: {overall}")
    if overall == "ALL PASS":
        return 0
    if overall == "CHANGES_REQUESTED":
        return 2
    return 3


if __name__ == "__main__":
    sys.exit(main())
