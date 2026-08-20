"""tests/vocab_extract.py — codex rollout 로그의 event_msg 어휘를 결정론적으로 추출.

`tests/fixtures/vocab/codex_event_types.json`의 단일 출처. 손으로 적은 어휘는
"실측"이 아니므로, 이 추출기만이 artifact를 만든다.

사용:
    python -m tests.vocab_extract            # 현재 로그로 추출해 stdout에 JSON 출력
    python -m tests.vocab_extract --write    # artifact 갱신

계약 테스트(`test_event_vocabulary.py`)는 로그가 있는 환경에서 이 추출 결과와
artifact를 비교해 **새 event type이 나타나면 실패**한다. 로그가 없으면 skip한다.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

VOCAB_PATH = Path(__file__).resolve().parent / "fixtures" / "vocab" / "codex_event_types.json"

# 표본 상한 — 전수 스캔은 느리고, 최근 로그면 어휘 커버리지에 충분하다.
SAMPLE_FILES = 60


def extract(sessions_base: Path, sample_files: int = SAMPLE_FILES) -> Counter[str]:
    """최근 rollout 파일에서 event_msg 타입 분포를 센다(결정론: mtime 정렬)."""
    files = sorted(sessions_base.rglob("rollout-*.jsonl"), key=lambda p: (p.stat().st_mtime, p.name))
    counts: Counter[str] = Counter()
    for path in files[-sample_files:]:
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("type") != "event_msg":
                    continue
                event_type = obj.get("payload", {}).get("type")
                if isinstance(event_type, str):
                    counts[event_type] += 1
    return counts


def load_artifact() -> dict[str, Any]:
    return json.loads(VOCAB_PATH.read_text(encoding="utf-8"))


def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--sessions-base")
    args = parser.parse_args()

    if args.sessions_base:
        base = Path(args.sessions_base)
    else:
        from acp.config import AppConfig

        base = AppConfig.load("config/paths.yaml").get_path("codex_sessions")

    counts = extract(base)
    payload = {
        "provenance": {
            "source": "codex rollout jsonl (event_msg.payload.type)",
            "sample_files": SAMPLE_FILES,
            "extractor": "tests/vocab_extract.py",
            "note": "시점 고정 스냅샷. 갱신은 --write로만.",
        },
        "event_types": sorted(counts),
        "counts": dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    if args.write:
        VOCAB_PATH.parent.mkdir(parents=True, exist_ok=True)
        VOCAB_PATH.write_text(text, encoding="utf-8")
        print(f"wrote {VOCAB_PATH}")
    else:
        print(text)


if __name__ == "__main__":
    _main()
