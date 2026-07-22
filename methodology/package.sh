#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="$ROOT_DIR/dist"
STAMP="$(date +%Y%m%d_%H%M%S)"
NAME="agent-workflow-beta-$STAMP"

mkdir -p "$OUT_DIR"

if command -v zip >/dev/null 2>&1; then
  (cd "$ROOT_DIR" && zip -r "$OUT_DIR/$NAME.zip" \
    README.md METHODOLOGY.md MULTI_AGENT.md DOC_TAXONOMY.md \
    .agents adapters config docs plugins nitpicker install.sh package.sh \
    -x "*/.git/*" "dist/*" "cubi-deploy/*" "nitpicker/nitpicker.config.json" \
       "*/__pycache__/*" "*/__pycache__/" "*.pyc" "*.pyo")
  echo "$OUT_DIR/$NAME.zip"
else
  tar -czf "$OUT_DIR/$NAME.tar.gz" \
    -C "$ROOT_DIR" \
    --exclude='nitpicker/nitpicker.config.json' \
    --exclude='*/__pycache__' \
    --exclude='*/__pycache__/*' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    README.md METHODOLOGY.md MULTI_AGENT.md DOC_TAXONOMY.md \
    .agents adapters config docs plugins nitpicker install.sh package.sh
  echo "$OUT_DIR/$NAME.tar.gz"
fi

# Optional: bundle the ztr relay runtime SOURCE for cross-machine deploy (PACKAGE_ZTR=1).
# Separate artifact (mixed roots 회피). 제외: .venv(132M)/egg-info/__pycache__/.ztr 런타임 산출물.
# 타깃 머신: 압축 해제 → python -m venv .venv → pip install -e . → install.sh --with-ztr 또는 ZTR_HOME 지정.
if [[ "${PACKAGE_ZTR:-0}" == "1" ]]; then
  ZTR_SRC="$ROOT_DIR/../runtimes/ztr"
  if [[ -d "$ZTR_SRC" ]]; then
    ZNAME="ztr-runtime-$STAMP"
    tar -czf "$OUT_DIR/$ZNAME.tar.gz" \
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
    echo "WARN: ztr runtime not found at $ZTR_SRC — PACKAGE_ZTR skipped" >&2
  fi
fi
