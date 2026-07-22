"""Deterministic subprocess actors for remediation adapter E2E tests."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


BAD_ACCEPTED = "BAD_ACCEPTED"
FIXED_ACCEPTED = "FIXED_ACCEPTED"
REJECTED_TOKEN = "KEEP_REJECTED"
DEFERRED_TOKEN = "KEEP_DEFERRED"
FIX_INSTRUCTION = "Replace BAD_ACCEPTED with FIXED_ACCEPTED."
NON_ACCEPT_BODIES = ("REJECTED_BODY_SECRET", "DEFERRED_BODY_SECRET")


def _implement(target: Path, prompt: Path) -> int:
    prompt_text = prompt.read_text(encoding="utf-8")
    target_text = target.read_text(encoding="utf-8")
    if FIX_INSTRUCTION not in prompt_text or target_text.count(BAD_ACCEPTED) != 1:
        return 2
    updated = target_text.replace(BAD_ACCEPTED, FIXED_ACCEPTED, 1)
    target.write_text(updated, encoding="utf-8")
    print(json.dumps({"status": "PASS", "corrective_rounds": 1}, separators=(",", ":")))
    return 0


def _gate(target: Path) -> int:
    text = target.read_text(encoding="utf-8")
    passed = (
        FIXED_ACCEPTED in text
        and BAD_ACCEPTED not in text
        and REJECTED_TOKEN in text
        and DEFERRED_TOKEN in text
    )
    print(json.dumps({"status": "PASS" if passed else "BLOCKED"}, separators=(",", ":")))
    return 0 if passed else 2


def _review(target: Path, prompt: Path) -> int:
    target_text = target.read_text(encoding="utf-8")
    prompt_text = prompt.read_text(encoding="utf-8")
    passed = (
        FIXED_ACCEPTED in target_text
        and BAD_ACCEPTED not in target_text
        and REJECTED_TOKEN in target_text
        and DEFERRED_TOKEN in target_text
        and all(body not in prompt_text for body in NON_ACCEPT_BODIES)
    )
    if not passed:
        print("fixture contract mismatch")
        print("ZTR_VERDICT: BLOCKED")
        return 2
    print("accepted token fixed; non-accepted tokens and prompt isolation verified")
    print("ZTR_VERDICT: PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("implement", "gate", "review"))
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--prompt", type=Path)
    args = parser.parse_args()
    if args.mode == "implement":
        if args.prompt is None:
            return 2
        return _implement(args.target, args.prompt)
    if args.mode == "review":
        if args.prompt is None:
            return 2
        return _review(args.target, args.prompt)
    return _gate(args.target)


if __name__ == "__main__":
    raise SystemExit(main())
