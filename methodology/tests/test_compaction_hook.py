#!/usr/bin/env python3
"""Determinism + fallback regression tests for the shared compaction boot hook.

Runs `scripts/emit_compaction_boot_context.py` as a subprocess (the real
install/execution path) against a fixed-HEAD fixture repo and asserts:
  - single valid JSON on stdout with hookEventName=SessionStart,
  - format-stable output for fixed inputs (byte-equal across two runs),
  - exact fallback/sentinel strings for non-repo / git-missing / malformed /
    source!=compact / artifact missing / artifact stale,
  - UTF-8 (Korean) cwd + path handling with errors="replace",
  - 4,096-byte artifact excerpt cap and 8,192-byte additionalContext cap that
    never split a multibyte boundary,
  - read-only: git working tree is unchanged before/after the hook.

Deterministic by construction: the hook generates no timestamp/random value;
the fixture repo has a pinned commit and the artifact has a pinned
`generated_at_utc`. No pytest dependency (run: `python test_compaction_hook.py`).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = HERE.parent / "plugins" / "agent-workflow"
SCRIPT = PLUGIN_ROOT / "scripts" / "emit_compaction_boot_context.py"
ARTIFACT_REL = os.path.join(".agent-workflow", "compaction", "resume-prompt.md")

# Import the hook module in-process for deterministic timeout/budget/capture
# unit tests (subprocess stub + clock injection, per reviewer P2-3).
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))
import emit_compaction_boot_context as mod  # noqa: E402


class _EagerStdout:
    """Fake pipe returning `data[:n]` on the first read, then EOF."""

    def __init__(self, data):
        self._data = data
        self._done = False

    def read(self, n):
        if self._done:
            return b""
        self._done = True
        return self._data[:n]

    def close(self):
        pass


class _EagerPopen:
    """Non-blocking fake git process that echoes preset bytes; rc 0."""

    def __init__(self, data):
        self.stdout = _EagerStdout(data)
        self.returncode = None

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class _BlockingStdout:
    """Fake pipe whose read() blocks until the process is killed."""

    def __init__(self):
        self._ev = threading.Event()

    def read(self, n):
        self._ev.wait()
        return b""

    def close(self):
        self._ev.set()


class _BlockingPopen:
    """Fake git process that never produces output until killed."""

    def __init__(self):
        self.stdout = _BlockingStdout()
        self.returncode = None

    def kill(self):
        self.returncode = -9
        self.stdout._ev.set()

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired(cmd="git", timeout=timeout)
        return self.returncode


def _popen_factory(fast_map, blocking_keys=()):
    blocking = set(blocking_keys)

    def _factory(argv, **kwargs):
        key = argv[1] if len(argv) > 1 else ""
        if key in blocking:
            return _BlockingPopen()
        return _EagerPopen(fast_map.get(key, b""))

    return _factory

FIXED_ENV_NAME = "AUTHOR"
FIXED_ENV = {
    "GIT_AUTHOR_NAME": "fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.com",
    "GIT_COMMITTER_NAME": "fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.com",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00 +0000",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00 +0000",
}

_failures = []


def check(cond, msg):
    if cond:
        print(f"PASS {msg}")
    else:
        print(f"FAIL {msg}")
        _failures.append(msg)


def run_hook(stdin_obj, cwd, extra_env=None, raw_stdin=None, plugin_root=None):
    env = dict(os.environ)
    # Deliberately do NOT set PYTHONUTF8: the real hook command runs `python`
    # under the ambient console locale, so the script itself must emit UTF-8
    # regardless. Forcing UTF-8 here would mask that production path.
    env.pop("PYTHONUTF8", None)
    env.pop("PYTHONIOENCODING", None)
    env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root or PLUGIN_ROOT)
    env.pop("PLUGIN_ROOT", None)
    if extra_env:
        env.update(extra_env)
    if raw_stdin is not None:
        payload = raw_stdin
    else:
        payload = json.dumps(stdin_obj).encode("utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=str(cwd),
        input=payload,
        capture_output=True,
        env=env,
        timeout=30,
    )
    return proc


def context_of(proc):
    check(proc.returncode == 0, f"exit 0 (got {proc.returncode}; stderr={proc.stderr[:200]!r})")
    data = json.loads(proc.stdout.decode("utf-8"))
    hso = data["hookSpecificOutput"]
    check(hso["hookEventName"] == "SessionStart", "hookEventName == SessionStart")
    return hso["additionalContext"]


def git(args, cwd):
    env = dict(os.environ)
    env.update(FIXED_ENV)
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, env=env)


def make_fixture_repo(base, korean=False):
    name = "저장소_한국어" if korean else "repo"
    root = Path(base) / name
    root.mkdir(parents=True)
    git(["init", "-q"], root)
    git(["config", "commit.gpgsign", "false"], root)
    (root / "file.txt").write_text("hello\n", encoding="utf-8")
    git(["add", "file.txt"], root)
    git(["commit", "-q", "-m", "초기 커밋"], root)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, text=True
    ).stdout.strip()
    return root, head


def write_artifact(root, captured_head, body_extra=""):
    path = Path(root) / ARTIFACT_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        f"generated_at_utc: 2026-07-13T00:00:00Z\n"
        f"workspace_root: {root}\n"
        f"captured_head: {captured_head}\n\n"
        f"# RESUME PROMPT — 압축 후 재개\n압축 요약은 손실적이다.\n{body_extra}"
    )
    path.write_text(content, encoding="utf-8")
    return path


def run_inprocess_unit_tests(tmp):
    """Deterministic timeout/budget/capture tests via subprocess+clock stubs.

    No long real sleeps: the timeout path uses a genuinely-blocking fake pipe
    with PER_COMMAND_TIMEOUT shrunk to 50ms (the read can never return, so the
    join always times out — no race), and budget exhaustion uses an injected
    monotonic clock. All git wall-clock is bounded by ONE shared deadline.
    """
    orig_popen = mod.subprocess.Popen
    orig_monotonic = mod.time.monotonic
    orig_percmd = mod.PER_COMMAND_TIMEOUT
    try:
        # 10. per-command timeout: a blocking git command -> degraded facts.
        mod.PER_COMMAND_TIMEOUT = 0.05
        mod.subprocess.Popen = _popen_factory(
            {"rev-parse": b"true\n"}, blocking_keys={"rev-parse"}
        )
        start = time.monotonic()
        ctx_t = mod.build_context(str(tmp))
        elapsed = time.monotonic() - start
        check(
            "LIVE_GIT_FACTS: degraded (git command timed out after 3.0s)." in ctx_t,
            "per-command timeout -> degraded git-timeout sentinel",
        )
        check(elapsed < 2.0, f"timeout path returns fast under shared budget (took {elapsed:.2f}s)")

        # 11. partial facts: branch/HEAD ok, then status blocks -> timeout mid-way.
        mod.PER_COMMAND_TIMEOUT = 0.05
        fast = {
            "rev-parse": b"master\n",  # abbrev-ref/HEAD/is-inside all use rev-parse
            "log": b"abc123 msg\n",
        }
        # is-inside must return "true"; use a dedicated factory that special-cases it.
        def _mixed(argv, **kwargs):
            key = argv[1] if len(argv) > 1 else ""
            sub = argv[2] if len(argv) > 2 else ""
            if key == "status":
                return _BlockingPopen()
            if key == "rev-parse" and sub == "--is-inside-work-tree":
                return _EagerPopen(b"true\n")
            if key == "rev-parse" and sub == "--abbrev-ref":
                return _EagerPopen(b"master\n")
            if key == "rev-parse":
                return _EagerPopen(b"deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n")
            return _EagerPopen(fast.get(key, b""))
        mod.subprocess.Popen = _mixed
        ctx_p = mod.build_context(str(tmp))
        check("note: partial" in ctx_p, "partial: budget/timeout note present")
        check("branch: master" in ctx_p, "partial: branch fact captured before timeout")
        check(
            "status --short --branch:\n(unavailable)" in ctx_p,
            "partial: timed-out status field marked unavailable",
        )

        # 12. cumulative deadline exhaustion via injected clock -> partial facts.
        mod.PER_COMMAND_TIMEOUT = orig_percmd
        clock = {"seq": [100.0, 100.0], "default": 999.0, "i": 0}
        def _fake_monotonic():
            i = clock["i"]
            clock["i"] += 1
            seq = clock["seq"]
            return seq[i] if i < len(seq) else clock["default"]
        mod.time.monotonic = _fake_monotonic
        mod.subprocess.Popen = _popen_factory({"rev-parse": b"true\n"})
        ctx_b = mod.build_context(str(tmp))
        check("note: partial" in ctx_b, "budget-exhaustion: partial note present")
        check("HEAD: unknown" in ctx_b, "budget-exhaustion: HEAD skipped after deadline")
        mod.time.monotonic = orig_monotonic

        # 13. workspace-root lookup shares the SAME deadline (no new budget).
        runner = mod._GitRunner("MYCWD")
        runner._deadline = time.monotonic() - 1.0  # already past
        popen_calls = {"n": 0}
        def _counting(argv, **kwargs):
            popen_calls["n"] += 1
            return _EagerPopen(b"")
        mod.subprocess.Popen = _counting
        root = mod._workspace_root(runner, "MYCWD")
        check(root == "MYCWD", "shared budget: workspace-root falls back to cwd when exhausted")
        check(popen_calls["n"] == 0, "shared budget: no new git process spawned after deadline")
        check(runner.budget_exhausted is True, "shared budget: exhaustion flag set")

        # 14. huge git stdout is captured under an explicit byte cap.
        runner2 = mod._GitRunner("X")
        mod.subprocess.Popen = _popen_factory({"log": b"Z" * 500000})
        res = runner2.run(["log", "--oneline", "-15"])
        check(res.truncated is True, "huge stdout: capture flagged truncated")
        check(
            len(res.text.encode("utf-8")) <= mod.GIT_CAPTURE_MAX_BYTES,
            f"huge stdout: capture bounded to {mod.GIT_CAPTURE_MAX_BYTES} bytes (got {len(res.text)})",
        )
        # And end-to-end, huge status/log still yields a bounded final context.
        def _huge_git(argv, **kwargs):
            key = argv[1] if len(argv) > 1 else ""
            sub = argv[2] if len(argv) > 2 else ""
            if key == "rev-parse" and sub == "--is-inside-work-tree":
                return _EagerPopen(b"true\n")
            if key in ("status", "log"):
                return _EagerPopen(b"Z" * 500000)
            return _EagerPopen(b"x\n")
        mod.subprocess.Popen = _huge_git
        ctx_h = mod.build_context("X")
        final = mod._cap_utf8(ctx_h).encode("utf-8")
        check(len(final) <= 8192, f"huge stdout: final context bounded to 8192 bytes (got {len(final)})")
        check(
            "GIT_OUTPUT_TRUNCATED" in ctx_h,
            "huge stdout: per-field truncation marker present before final cap",
        )
    finally:
        mod.subprocess.Popen = orig_popen
        mod.time.monotonic = orig_monotonic
        mod.PER_COMMAND_TIMEOUT = orig_percmd


def main():
    if not SCRIPT.exists():
        print(f"FAIL script missing: {SCRIPT}")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="mam_hook_test_"))
    try:
        # --- 1. compact + real repo + fresh artifact: determinism + facts ---
        repo, head = make_fixture_repo(tmp)
        write_artifact(repo, head)
        stdin = {"source": "compact", "cwd": str(repo), "session_id": "x", "model": "m"}
        p1 = run_hook(stdin, repo)
        p2 = run_hook(stdin, repo)
        check(p1.stdout == p2.stdout, "format-stable: identical stdout across two runs")
        ctx = context_of(p1)
        check(ctx.encode("utf-8").__len__() <= 8192, "additionalContext <= 8192 bytes")
        check("This session was compacted." in ctx, "policy sentence present")
        check("LIVE_GIT_FACTS:" in ctx, "LIVE_GIT_FACTS block present")
        check("branch: " in ctx and "HEAD: " + head in ctx, "branch/HEAD facts present")
        check("status --short --branch:" in ctx, "status facts present")
        check("log --oneline -15:" in ctx, "log facts present")
        artifact_abs = os.path.realpath(os.path.join(os.path.realpath(str(repo)), ARTIFACT_REL))
        check("RESUME_ARTIFACT: " + artifact_abs in ctx, "artifact absolute path present")
        check("RESUME_ARTIFACT_STATE: stale" not in ctx, "fresh artifact not marked stale")

        # read-only: working tree unchanged before/after
        before = subprocess.run(
            ["git", "status", "--porcelain=v1"], cwd=str(repo), capture_output=True, text=True
        ).stdout
        run_hook(stdin, repo)
        after = subprocess.run(
            ["git", "status", "--porcelain=v1"], cwd=str(repo), capture_output=True, text=True
        ).stdout
        check(before == after, "hook does not mutate git working tree")

        # --- 2. stale artifact (captured_head differs from live HEAD) ---
        repo2, head2 = make_fixture_repo(tmp / "s2")
        write_artifact(repo2, "0" * 40)
        ctx2 = context_of(run_hook({"source": "compact", "cwd": str(repo2)}, repo2))
        check("RESUME_ARTIFACT_STATE: stale" in ctx2, "stale artifact marked stale")

        # --- 3. artifact missing ---
        repo3, _ = make_fixture_repo(tmp / "s3")
        ctx3 = context_of(run_hook({"source": "compact", "cwd": str(repo3)}, repo3))
        check(
            "RESUME_ARTIFACT: unavailable (.agent-workflow/compaction/resume-prompt.md not found)."
            in ctx3,
            "missing artifact -> exact unavailable sentinel",
        )

        # --- 4. non-repo cwd ---
        nonrepo = tmp / "plain"
        nonrepo.mkdir()
        ctx4 = context_of(run_hook({"source": "compact", "cwd": str(nonrepo)}, nonrepo))
        check(
            "LIVE_GIT_FACTS: unavailable (cwd is not inside a Git work tree)." in ctx4,
            "non-repo -> exact not-a-worktree sentinel",
        )

        # --- 5. malformed input ---
        pbad = run_hook(None, tmp, raw_stdin=b"\xff\xfe not json")
        cbad = context_of(pbad)
        check(
            cbad == "COMPACTION_BOOT_CONTEXT: unavailable (invalid SessionStart input).",
            "malformed input -> exact invalid sentinel",
        )
        pbad2 = run_hook(None, tmp, raw_stdin=b"[1,2,3]")
        check(
            context_of(pbad2) == "COMPACTION_BOOT_CONTEXT: unavailable (invalid SessionStart input).",
            "non-object JSON -> invalid sentinel",
        )

        # --- 6. source != compact ---
        c6 = context_of(run_hook({"source": "startup", "cwd": str(repo)}, repo))
        check(
            c6 == "COMPACTION_BOOT_CONTEXT: skipped (SessionStart source is not compact).",
            "non-compact source -> skipped, no heavy context",
        )

        # --- 7. git executable not found (PATH without git) ---
        fakebin = tmp / "nogit"
        fakebin.mkdir()
        no_git_env = {"PATH": str(fakebin)}
        # On Windows the interpreter dir must remain reachable for subprocess;
        # emptying PATH still lets absolute sys.executable run, and git resolves via PATH.
        c7 = context_of(run_hook({"source": "compact", "cwd": str(repo)}, repo, extra_env=no_git_env))
        check(
            "LIVE_GIT_FACTS: unavailable (git executable not found)." in c7,
            "git missing -> exact git-not-found sentinel",
        )

        # --- 8. Korean UTF-8 path + large artifact -> 4096-byte excerpt cap ---
        krepo, khead = make_fixture_repo(tmp / "k", korean=True)
        # Marker beyond the 4096-byte artifact read window must NOT appear.
        big = ("가나다라마바사아자차" * 2000) + "\nTAIL_MARKER_BEYOND_4096\n"  # multibyte
        write_artifact(krepo, khead, body_extra=big)
        c8 = context_of(run_hook({"source": "compact", "cwd": str(krepo)}, krepo))
        enc = c8.encode("utf-8")
        check(len(enc) <= 8192, f"korean+big: additionalContext <= 8192 bytes (got {len(enc)})")
        check(enc.decode("utf-8") == c8, "korean+big: output is valid UTF-8 (no split boundary)")
        check("저장소_한국어" in c8, "korean path present in output")
        check("TAIL_MARKER_BEYOND_4096" not in c8, "artifact read capped at 4096 bytes (tail absent)")

        # --- 8b. huge git log subject -> exercise the 8192-byte total cap ---
        brepo = Path(tmp) / "big"
        brepo.mkdir()
        git(["init", "-q"], brepo)
        git(["config", "commit.gpgsign", "false"], brepo)
        (brepo / "f.txt").write_text("x\n", encoding="utf-8")
        git(["add", "f.txt"], brepo)
        huge_subject = "제" * 9000  # single commit subject > 8192 bytes on the log line
        git(["commit", "-q", "-m", huge_subject], brepo)
        c8b = context_of(run_hook({"source": "compact", "cwd": str(brepo)}, brepo))
        enc8b = c8b.encode("utf-8")
        check(len(enc8b) <= 8192, f"8192 total cap enforced (got {len(enc8b)})")
        check(enc8b.decode("utf-8") == c8b, "8192-capped output still valid UTF-8")
        check(
            c8b.endswith("[TRUNCATED: output capped at 8192 UTF-8 bytes]"),
            "8192 cap sentinel applied when total exceeds cap",
        )

        # --- 8c. _cap_utf8 boundary unit test (no split multibyte at the cap) ---
        for n in range(8180, 8210):
            capped = mod._cap_utf8("가" * n)  # 3 bytes each
            cb = capped.encode("utf-8")
            check_ok = len(cb) <= 8192 and cb.decode("utf-8") == capped
            if not check_ok:
                check(False, f"_cap_utf8 boundary broken at n={n} (len={len(cb)})")
                break
        else:
            check(True, "_cap_utf8 holds 8192 cap + UTF-8 boundary across sizes")

        # --- 9. dirty status (staged + unstaged + untracked) in status facts ---
        drepo, _ = make_fixture_repo(tmp / "dirty")
        (drepo / "file.txt").write_text("changed\n", encoding="utf-8")   # unstaged mod
        (drepo / "staged.txt").write_text("s\n", encoding="utf-8")
        git(["add", "staged.txt"], drepo)                                # staged add
        (drepo / "untracked.txt").write_text("u\n", encoding="utf-8")    # untracked
        c9 = context_of(run_hook({"source": "compact", "cwd": str(drepo)}, drepo))
        check("file.txt" in c9, "dirty: unstaged modification in status facts")
        check("staged.txt" in c9, "dirty: staged add in status facts")
        check("untracked.txt" in c9 and "??" in c9, "dirty: untracked file in status facts")

        run_inprocess_unit_tests(tmp)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if _failures:
        print(f"\n{len(_failures)} FAILURE(S)")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
