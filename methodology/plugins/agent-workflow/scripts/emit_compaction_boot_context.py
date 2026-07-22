#!/usr/bin/env python3
"""Emit post-compaction boot context for Claude/Codex SessionStart(compact) hooks.

This is a shared surface adapter (one script, two providers). It reads a
SessionStart JSON envelope from stdin, and when the session was started by a
compaction (`source == "compact"`) it prints a single JSON object whose
`hookSpecificOutput.additionalContext` carries:

  1. a project-policy sentence telling the next model request to re-derive
     state from the SoT instead of trusting the lossy compaction summary,
  2. read-only live git facts measured from the hook `cwd`, and
  3. the absolute path + a bounded excerpt of the fixed resume artifact that
     `prepare-session-compaction` writes.

Hard boundaries (see PREPARE_SESSION_COMPACTION_SKILL_IMPL_PROMPT.md §D):
  - never reads diff bodies, arbitrary files, secrets, or env values; the only
    file-read exception is the fixed artifact under the workspace root,
  - all git calls are argv arrays (shell=False), read-only, and share ONE
    wall-clock deadline (GIT_WALLCLOCK_BUDGET) with a PER_COMMAND_TIMEOUT cap
    each — the workspace-root lookup draws from the same deadline, so total git
    wall-clock stays under the outer 5s hook timeout with margin,
  - each git command's stdout is captured under an explicit byte cap
    (GIT_CAPTURE_MAX_BYTES) as it is read, so a pathological repo cannot balloon
    memory before the final 8,192-byte context cap,
  - the hook never blocks compaction: any failure degrades to a safe JSON and
    exit 0, and the injected text is context, not a new user turn or SoT.

Output is deterministic (format-stable) for fixed inputs: no timestamp or
random value is generated here; `generated_at_utc` comes from the artifact.
"""

import json
import os
import subprocess
import sys
import threading
import time

# Authoritative bounds (seconds / bytes).
PER_COMMAND_TIMEOUT = 3.0
GIT_WALLCLOCK_BUDGET = 4.0
GIT_CAPTURE_MAX_BYTES = 16384
ARTIFACT_MAX_BYTES = 4096
CONTEXT_MAX_BYTES = 8192
TRUNCATION_SENTINEL = "\n[TRUNCATED: output capped at 8192 UTF-8 bytes]"
GIT_FIELD_TRUNCATED_MARKER = "\n[GIT_OUTPUT_TRUNCATED: captured stdout capped at 16384 bytes]"
# Short grace to reap a process after we already have enough (or timed-out) output.
_REAP_GRACE = 0.2

ARTIFACT_RELPATH = os.path.join(".agent-workflow", "compaction", "resume-prompt.md")

POLICY_SENTENCE = (
    "This session was compacted. Project policy requires the next model request "
    "to begin by re-reading CLAUDE.md/AGENTS.md and the current SoT, comparing "
    "them with the attached live git facts, then reporting current position "
    "before other work."
)

INVALID_INPUT_CONTEXT = "COMPACTION_BOOT_CONTEXT: unavailable (invalid SessionStart input)."

# Git command result status codes.
_OK = "ok"
_NO_GIT = "no_git"
_TIMEOUT = "timeout"
_BUDGET = "budget"
_ERROR = "error"


class _GitResult:
    __slots__ = ("status", "text", "truncated")

    def __init__(self, status, text="", truncated=False):
        self.status = status
        self.text = text
        self.truncated = truncated


def _emit(additional_context):
    """Print a single valid SessionStart JSON object and exit 0.

    Written as UTF-8 bytes straight to the stdout buffer: the fixed hook
    command runs `python` under the ambient console locale (cp949 on this
    Windows workstation), so a text-layer write of non-ASCII git facts would
    raise UnicodeEncodeError and fail the hook. Bytes bypass that layer.
    """
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": additional_context,
        }
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    raise SystemExit(0)


def _cap_utf8(text, cap=CONTEXT_MAX_BYTES):
    """Cap `text` to `cap` UTF-8 bytes, ending with a truncation sentinel.

    The result always decodes as valid UTF-8 (no split multibyte sequence).
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= cap:
        return text
    sentinel_bytes = TRUNCATION_SENTINEL.encode("utf-8")
    budget = cap - len(sentinel_bytes)
    if budget < 0:
        budget = 0
    head = encoded[:budget].decode("utf-8", errors="ignore")
    return head + TRUNCATION_SENTINEL


def _terminate(proc):
    """Kill a git process and release its stdout pipe (no orphan / deadlock)."""
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=_REAP_GRACE)
    except Exception:
        pass
    try:
        if proc.stdout is not None:
            proc.stdout.close()
    except Exception:
        pass


class _GitRunner:
    """Run read-only git argv commands under ONE shared wall-clock deadline.

    Every git call (including the workspace-root lookup) draws remaining time
    from a single monotonic deadline set at construction, so the aggregate git
    wall-clock cannot exceed GIT_WALLCLOCK_BUDGET plus a small reap grace —
    comfortably under the outer 5s hook timeout. stdout is captured under an
    explicit byte cap as it streams in, bounding intermediate memory.
    """

    def __init__(self, cwd):
        self.cwd = cwd
        self._deadline = time.monotonic() + GIT_WALLCLOCK_BUDGET
        # Sticky failure flags surfaced to the context builder.
        self.timed_out = False
        self.no_git = False
        self.budget_exhausted = False

    def _remaining(self):
        return self._deadline - time.monotonic()

    def run(self, args):
        """Run `git <args>` and return a _GitResult (never raises)."""
        remaining = self._remaining()
        if remaining <= 0:
            self.budget_exhausted = True
            return _GitResult(_BUDGET)
        timeout = min(PER_COMMAND_TIMEOUT, remaining)

        try:
            proc = subprocess.Popen(
                ["git", *args],
                cwd=self.cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            self.no_git = True
            return _GitResult(_NO_GIT)
        except OSError:
            self.no_git = True
            return _GitResult(_NO_GIT)

        captured = {"data": b"", "truncated": False}

        def _reader():
            # read(cap+1) returns as soon as cap+1 bytes are available OR EOF,
            # so a huge-output command never loads more than cap(+1) bytes.
            try:
                data = proc.stdout.read(GIT_CAPTURE_MAX_BYTES + 1)
            except Exception:
                data = b""
            if data is None:
                data = b""
            if len(data) > GIT_CAPTURE_MAX_BYTES:
                captured["truncated"] = True
                data = data[:GIT_CAPTURE_MAX_BYTES]
            captured["data"] = data

        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()
        reader.join(timeout)

        if reader.is_alive():
            # Command produced too little too slowly within its slice: kill it.
            self.timed_out = True
            _terminate(proc)
            return _GitResult(_TIMEOUT)

        # Reader finished (EOF or cap hit). Reap the process briefly.
        try:
            proc.wait(timeout=_REAP_GRACE)
        except subprocess.TimeoutExpired:
            # Cap was hit while the process kept writing; we have enough.
            _terminate(proc)
        returncode = proc.returncode
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except Exception:
            pass

        text = captured["data"].decode("utf-8", errors="replace")
        truncated = captured["truncated"]
        # A truncated capture is still usable output even if the killed process
        # reports a nonzero/None return code.
        if not truncated and returncode not in (0,):
            return _GitResult(_ERROR, text=text, truncated=truncated)
        return _GitResult(_OK, text=text, truncated=truncated)


def _field(result):
    """Render a git field value, appending a marker if its capture was cut."""
    if result.status != _OK:
        return "(unavailable)"
    value = result.text.rstrip("\n") if result.text else ""
    if not value:
        value = "(unavailable)"
    if result.truncated:
        value = value + GIT_FIELD_TRUNCATED_MARKER
    return value


def _git_facts(runner):
    """Build the LIVE_GIT_FACTS block and return (text, live_head_or_None)."""
    inside = runner.run(["rev-parse", "--is-inside-work-tree"])

    if runner.no_git:
        return "LIVE_GIT_FACTS: unavailable (git executable not found).", None
    if inside.status == _TIMEOUT:
        return "LIVE_GIT_FACTS: degraded (git command timed out after 3.0s).", None
    if inside.status == _BUDGET:
        return "LIVE_GIT_FACTS: degraded (git command timed out after 3.0s).", None
    if inside.status != _OK or inside.text.strip() != "true":
        return "LIVE_GIT_FACTS: unavailable (cwd is not inside a Git work tree).", None

    branch = runner.run(["rev-parse", "--abbrev-ref", "HEAD"])
    head = runner.run(["rev-parse", "HEAD"])
    status = runner.run(["status", "--short", "--branch"])
    log = runner.run(["log", "--oneline", "-15"])

    if runner.no_git:
        return "LIVE_GIT_FACTS: unavailable (git executable not found).", None

    live_head = head.text.strip() if head.status == _OK and head.text.strip() else None
    branch_name = branch.text.strip() if branch.status == _OK and branch.text.strip() else "unknown"

    lines = ["LIVE_GIT_FACTS:"]
    lines.append("branch: " + branch_name)
    lines.append("HEAD: " + (live_head or "unknown"))
    if runner.timed_out or runner.budget_exhausted:
        lines.append(
            "note: partial (git wall-clock budget exhausted or a command timed "
            "out; some facts skipped)."
        )
    lines.append("status --short --branch:")
    lines.append(_field(status))
    lines.append("log --oneline -15:")
    lines.append(_field(log))
    return "\n".join(lines), live_head


def _workspace_root(runner, cwd):
    """git toplevel (shares the runner deadline), else cwd."""
    result = runner.run(["rev-parse", "--show-toplevel"])
    if result.status != _OK:
        return cwd
    top = result.text.strip()
    return top or cwd


def _artifact_block(workspace_root, live_head):
    """Return the RESUME_ARTIFACT block (bounded excerpt or unavailable)."""
    root = os.path.realpath(workspace_root)
    artifact_path = os.path.realpath(os.path.join(root, ARTIFACT_RELPATH))

    # Confine to a regular file inside the workspace root.
    inside = artifact_path == root or artifact_path.startswith(root + os.sep)
    if not inside or not os.path.isfile(artifact_path):
        return (
            "RESUME_ARTIFACT: unavailable "
            "(.agent-workflow/compaction/resume-prompt.md not found)."
        )

    try:
        with open(artifact_path, "rb") as fh:
            raw = fh.read(ARTIFACT_MAX_BYTES)
    except OSError:
        return (
            "RESUME_ARTIFACT: unavailable "
            "(.agent-workflow/compaction/resume-prompt.md not found)."
        )

    excerpt = raw.decode("utf-8", errors="replace")

    captured_head = None
    for line in excerpt.splitlines():
        stripped = line.strip()
        if stripped.startswith("captured_head:"):
            captured_head = stripped.split(":", 1)[1].strip()
            break

    lines = ["RESUME_ARTIFACT: " + artifact_path]
    if live_head and captured_head and captured_head not in ("NOT_AVAILABLE", live_head):
        # Live HEAD moved past the captured one: excerpt is not ground truth.
        lines.append("RESUME_ARTIFACT_STATE: stale")
    lines.append("--- resume-prompt excerpt (bounded, not ground truth) ---")
    lines.append(excerpt)
    return "\n".join(lines)


def build_context(cwd):
    """Assemble the additionalContext string for a compact SessionStart."""
    runner = _GitRunner(cwd)
    git_block, live_head = _git_facts(runner)
    # Same runner => same shared deadline covers the workspace-root lookup too.
    workspace_root = _workspace_root(runner, cwd)
    artifact_block = _artifact_block(workspace_root, live_head)
    return "\n\n".join([POLICY_SENTENCE, git_block, artifact_block])


def main():
    raw = sys.stdin.buffer.read()
    try:
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("top-level JSON is not an object")
    except (UnicodeDecodeError, ValueError):
        _emit(INVALID_INPUT_CONTEXT)
        return

    source = data.get("source")
    if source != "compact":
        # Matcher should already filter to compact; guard defensively without
        # injecting heavy boot context on unrelated SessionStart sources.
        _emit("COMPACTION_BOOT_CONTEXT: skipped (SessionStart source is not compact).")
        return

    cwd = data.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        cwd = os.getcwd()

    _emit(_cap_utf8(build_context(cwd)))


if __name__ == "__main__":
    main()
