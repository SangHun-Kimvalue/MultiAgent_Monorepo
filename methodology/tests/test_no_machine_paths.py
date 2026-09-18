"""Guard portable methodology gates against machine-specific user-home paths."""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
TARGET_DIRS = (
    Path("methodology/tests"),
    Path("methodology/t8"),
    Path("methodology/plugins/agent-workflow/skills/phased-implementation-handoff/scripts"),
    Path("methodology/plugins/agent-workflow/skills/phase-learning-debrief"),
)
EXCLUDED_PARTS = {".venv", "__pycache__"}
DRIVE_PREFIX = r"[A-Za-z]:"
PATH_SEPARATOR = r"[\\/]"
USER_HOME_ROOT = r"(?:Users|home)"
MACHINE_HOME_PATTERN = re.compile(
    DRIVE_PREFIX + PATH_SEPARATOR + USER_HOME_ROOT + PATH_SEPARATOR,
    re.IGNORECASE,
)


def test_python_gate_files_have_no_machine_user_home_paths() -> None:
    failures: list[str] = []

    paths = sorted(
        path
        for target_dir in TARGET_DIRS
        for path in (REPO_ROOT / target_dir).rglob("*.py")
        if not EXCLUDED_PARTS.intersection(path.relative_to(REPO_ROOT).parts)
    )
    for path in paths:
        relative = path.relative_to(REPO_ROOT).as_posix()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError as exc:
            failures.append(f"{relative}: UTF-8 decode failed at byte {exc.start}: {exc}")
            continue

        for line_number, line in enumerate(lines, start=1):
            if MACHINE_HOME_PATTERN.search(line):
                failures.append(f"{relative}:{line_number}: {line}")

    assert not failures, "machine paths or UTF-8 decode failures found:\n" + "\n".join(
        failures
    )
