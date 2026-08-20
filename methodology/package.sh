#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="$ROOT_DIR/dist"
STAMP="$(date +%Y%m%d_%H%M%S)"
NAME="multiagent-methodology-beta-$STAMP"
FORMAT="${PACKAGE_FORMAT:-}"
TAR_BIN="${TAR:-tar}"
PYTHON_CMD=()

python_works() {
  "$@" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 8) else 1)
PY
}

resolve_python() {
  if [[ -n "${PYTHON:-}" ]]; then
    PYTHON_CMD=("$PYTHON")
    if ! python_works "${PYTHON_CMD[@]}"; then
      read -r -a PYTHON_CMD <<<"$PYTHON"
      [[ "${#PYTHON_CMD[@]}" -gt 0 ]] && python_works "${PYTHON_CMD[@]}"
    fi
  elif command -v python3 >/dev/null 2>&1 && python_works python3; then
    PYTHON_CMD=(python3)
  elif command -v python >/dev/null 2>&1 && python_works python; then
    PYTHON_CMD=(python)
  elif command -v py >/dev/null 2>&1 && python_works py -3; then
    PYTHON_CMD=(py -3)
  else
    return 1
  fi
}

if [[ -n "$FORMAT" && "$FORMAT" != "zip" && "$FORMAT" != "tar" ]]; then
  echo "PACKAGE_FORMAT must be zip or tar: $FORMAT" >&2
  exit 2
fi

if [[ -z "$FORMAT" ]]; then
  if resolve_python; then
    FORMAT="zip"
  elif command -v "$TAR_BIN" >/dev/null 2>&1; then
    FORMAT="tar"
  else
    echo "Neither a usable Python 3 zipfile backend nor tar is available" >&2
    exit 2
  fi
elif [[ "$FORMAT" == "zip" ]]; then
  if ! resolve_python; then
    echo "PACKAGE_FORMAT=zip requires a usable Python 3 interpreter" >&2
    exit 2
  fi
elif ! command -v "$TAR_BIN" >/dev/null 2>&1; then
  echo "PACKAGE_FORMAT=tar requires the tar executable" >&2
  exit 2
fi

mkdir -p "$OUT_DIR"

PACKAGE_ITEMS=(
  README.md METHODOLOGY.md MULTI_AGENT.md DOC_TAXONOMY.md
  .claude-plugin .agents adapters config docs plugins nitpicker tools
  install.sh package.sh
)

if [[ "$FORMAT" == "zip" ]]; then
  ARTIFACT="$OUT_DIR/$NAME.zip"
  TEMP_ARTIFACT="$ARTIFACT.tmp.$$"
  rm -f "$TEMP_ARTIFACT"
  "${PYTHON_CMD[@]}" - "$ROOT_DIR" "$TEMP_ARTIFACT" "${PACKAGE_ITEMS[@]}" <<'PY'
import sys
import zipfile
from pathlib import Path

root = Path(sys.argv[1]).resolve()
artifact = Path(sys.argv[2])
items = sys.argv[3:]

def excluded(path: Path) -> bool:
    relative = path.relative_to(root)
    parts = relative.parts
    return (
        any(part in {".git", "dist", "tests", "__pycache__"} for part in parts)
        or relative.as_posix() == "nitpicker/nitpicker.config.json"
        or path.suffix in {".pyc", ".pyo"}
    )

try:
    with zipfile.ZipFile(artifact, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in items:
            source = root / item
            if not source.exists():
                continue
            paths = [source] if source.is_file() else sorted(source.rglob("*"))
            for path in paths:
                if path.is_file() and not excluded(path):
                    archive.write(path, path.relative_to(root).as_posix())
except Exception:
    artifact.unlink(missing_ok=True)
    raise
PY
  mv "$TEMP_ARTIFACT" "$ARTIFACT"
else
  ARTIFACT="$OUT_DIR/$NAME.tar.gz"
  TEMP_ARTIFACT="$ARTIFACT.tmp.$$"
  rm -f "$TEMP_ARTIFACT"
  "$TAR_BIN" -czf "$TEMP_ARTIFACT" \
    -C "$ROOT_DIR" \
    --exclude='.git' \
    --exclude='dist' \
    --exclude='tests' \
    --exclude='nitpicker/nitpicker.config.json' \
    --exclude='*/__pycache__' \
    --exclude='*/__pycache__/*' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    "${PACKAGE_ITEMS[@]}"
  mv "$TEMP_ARTIFACT" "$ARTIFACT"
fi

echo "$ARTIFACT"

# Optional: bundle the ztr relay runtime SOURCE for cross-machine deploy.
if [[ "${PACKAGE_ZTR:-0}" == "1" ]]; then
  ZTR_SRC="$ROOT_DIR/../runtimes/ztr"
  if [[ -d "$ZTR_SRC" ]]; then
    ZNAME="ztr-runtime-$STAMP"
    "$TAR_BIN" -czf "$OUT_DIR/$ZNAME.tar.gz" \
      -C "$ROOT_DIR/.." \
      --exclude='runtimes/ztr/.venv' \
      --exclude='*/__pycache__' \
      --exclude='*.egg-info' \
      --exclude='runtimes/ztr/.ztr' \
      --exclude='runtimes/ztr/.mypy_cache' \
      --exclude='runtimes/ztr/.pytest_cache' \
      --exclude='runtimes/ztr/.ruff_cache' \
      runtimes/ztr
    echo "$OUT_DIR/$ZNAME.tar.gz"
  else
    echo "WARN: ztr runtime not found at $ZTR_SRC - PACKAGE_ZTR skipped" >&2
  fi
fi
