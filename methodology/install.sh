#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  ./install.sh --target /path/to/project [--surface claude|codex|both] [--mode lite|strict] [--with-nitpicker] [--with-ztr] [--without-config] [--provider ollama|mock] [--dry-run]

Default:
  --surface claude --mode lite --with-nitpicker --provider ollama  (config: on, ztr: off)

Rules:
  - Does not create branches or tags.
  - Does not commit.
  - Preflights both marketplace files before changing any target path.
  - Backs up adapters, plugin-owned roots, and each replaced skill into a unique snapshot.
  - Preserves unrelated flat skills and third-party marketplace entries.
  - Installs per-project .claude/phased-handoff.config.md from config/project.config.example.md (relay/orchestrator leg bindings; fill before use). Existing config is kept (backed up), not clobbered.
  - --with-ztr writes .claude/ztr-run-phase.sh wrapper pointing at this monorepo's runtimes/ztr (relay engine).
  - Strict mode is reserved for future hooks and currently behaves like lite.
USAGE
}

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET=""
SURFACE="claude"
MODE="lite"
WITH_NITPICKER=1
WITH_ZTR=0
WITH_CONFIG=1
PROVIDER="ollama"
DRY_RUN=0
PYTHON_BIN="${PYTHON:-}"
PYTHON_CMD=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)
      TARGET="${2:-}"
      shift 2
      ;;
    --surface)
      SURFACE="${2:-}"
      shift 2
      ;;
    --mode)
      MODE="${2:-}"
      shift 2
      ;;
    --with-nitpicker)
      WITH_NITPICKER=1
      shift
      ;;
    --without-nitpicker)
      WITH_NITPICKER=0
      shift
      ;;
    --with-ztr)
      WITH_ZTR=1
      shift
      ;;
    --without-ztr)
      WITH_ZTR=0
      shift
      ;;
    --without-config)
      WITH_CONFIG=0
      shift
      ;;
    --provider)
      PROVIDER="${2:-}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ -z "$TARGET" ]]; then
  echo "--target is required" >&2
  usage
  exit 2
fi

if [[ "$MODE" != "lite" && "$MODE" != "strict" ]]; then
  echo "--mode must be lite or strict" >&2
  exit 2
fi

if [[ "$SURFACE" != "claude" && "$SURFACE" != "codex" && "$SURFACE" != "both" ]]; then
  echo "--surface must be claude, codex, or both" >&2
  exit 2
fi

if [[ "$PROVIDER" != "ollama" && "$PROVIDER" != "mock" ]]; then
  echo "--provider must be ollama or mock" >&2
  exit 2
fi

TARGET="$(cd "$TARGET" && pwd)"
STAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_PARENT="$TARGET/.agent-workflow-backup"
BACKUP_DIR="$BACKUP_PARENT/$STAMP"
PLUGIN_NAMES=(agent-workflow ai-research)
# ztr relay runtime = monorepo sibling of methodology/ (this script's parent).
ZTR_HOME="$(cd "$ROOT_DIR/.." 2>/dev/null && pwd)/runtimes/ztr"

python_works() {
  "$@" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 8) else 1)
PY
}

if [[ -n "$PYTHON_BIN" ]]; then
  PYTHON_CMD=("$PYTHON_BIN")
  if ! python_works "${PYTHON_CMD[@]}"; then
    read -r -a PYTHON_CMD <<<"$PYTHON_BIN"
    if [[ "${#PYTHON_CMD[@]}" -eq 0 ]] || ! python_works "${PYTHON_CMD[@]}"; then
      echo "Configured PYTHON is not a usable Python 3 interpreter: $PYTHON_BIN" >&2
      exit 2
    fi
  fi
elif command -v python3 >/dev/null 2>&1 && python_works python3; then
  PYTHON_CMD=(python3)
elif command -v python >/dev/null 2>&1 && python_works python; then
  PYTHON_CMD=(python)
elif command -v py >/dev/null 2>&1 && python_works py -3; then
  PYTHON_CMD=(py -3)
else
  echo "A usable Python 3 interpreter is required" >&2
  exit 2
fi

run() {
  echo "+ $*"
  if [[ "$DRY_RUN" -eq 0 ]]; then
    "$@"
  fi
}

surface_has() {
  [[ "$SURFACE" == "$1" || "$SURFACE" == "both" ]]
}

copy_dir() {
  local src="$1"
  local dst="$2"
  run mkdir -p "$(dirname "$dst")"
  if [[ "$DRY_RUN" -eq 0 ]]; then
    rm -rf "$dst"
  else
    echo "+ rm -rf $dst"
  fi
  run cp -R "$src" "$dst"
  run find "$dst" -type d -name __pycache__ -prune -exec rm -rf -- '{}' '+'
  run find "$dst" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
}

backup_path() {
  local path="$1"
  local name="$2"
  if [[ -e "$path" ]]; then
    run mkdir -p "$(dirname "$BACKUP_DIR/$name")"
    run cp -R "$path" "$BACKUP_DIR/$name"
  fi
}

preflight_marketplace() {
  local marketplace="$1"
  local label="$2"
  "${PYTHON_CMD[@]}" - "$marketplace" "$label" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
label = sys.argv[2]
if not path.exists():
    raise SystemExit(0)
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError) as exc:
    print(f"{label} marketplace is not valid UTF-8 JSON: {path}: {exc}", file=sys.stderr)
    raise SystemExit(2)
if not isinstance(data, dict):
    print(f"{label} marketplace root must be a JSON object: {path}", file=sys.stderr)
    raise SystemExit(2)
if not isinstance(data.get("plugins"), list):
    print(f"{label} marketplace plugins must be an array: {path}", file=sys.stderr)
    raise SystemExit(2)
PY
}

reserve_backup_dir() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "+ mkdir $BACKUP_DIR"
    return
  fi
  if ! mkdir -p "$BACKUP_PARENT"; then
    echo "Cannot create backup parent directory: $BACKUP_PARENT" >&2
    return 2
  fi
  local candidate="$BACKUP_DIR"
  local suffix=0
  while ! mkdir "$candidate" 2>/dev/null; do
    if [[ ! -e "$candidate" && ! -L "$candidate" ]]; then
      echo "Cannot create backup directory: $candidate" >&2
      return 2
    fi
    suffix=$((suffix + 1))
    candidate="$BACKUP_PARENT/$STAMP.$suffix"
  done
  BACKUP_DIR="$candidate"
}

upsert_marketplace() {
  local source_marketplace="$1"
  local target_marketplace="$2"
  run mkdir -p "$(dirname "$target_marketplace")"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "+ merge canonical marketplace entries into $target_marketplace"
    return
  fi
  "${PYTHON_CMD[@]}" - "$source_marketplace" "$target_marketplace" <<'PY'
import json
import sys
from pathlib import Path

source_path = Path(sys.argv[1])
target_path = Path(sys.argv[2])
source = json.loads(source_path.read_text(encoding="utf-8"))
if target_path.exists():
    data = json.loads(target_path.read_text(encoding="utf-8"))
else:
    data = {key: value for key, value in source.items() if key != "plugins"}
    data["plugins"] = []

for key, value in source.items():
    if key != "plugins":
        data[key] = value
canonical = {
    item["name"]: item
    for item in source["plugins"]
    if isinstance(item, dict) and isinstance(item.get("name"), str)
}
plugins = [
    item
    for item in data["plugins"]
    if not (isinstance(item, dict) and item.get("name") in canonical)
]
plugins.extend(canonical.values())
data["plugins"] = plugins

with target_path.open("w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
PY
}

install_plugin_roots() {
  local plugin
  for plugin in "${PLUGIN_NAMES[@]}"; do
    backup_path "$TARGET/plugins/$plugin" "plugins/$plugin"
    copy_dir "$ROOT_DIR/plugins/$plugin" "$TARGET/plugins/$plugin"
  done
}

merge_plugin_skills() {
  local surface_name="$1"
  local target_root="$2"
  local plugin skill_dir skill
  for plugin in "${PLUGIN_NAMES[@]}"; do
    for skill_dir in "$ROOT_DIR/plugins/$plugin/skills"/*; do
      [[ -d "$skill_dir" && -f "$skill_dir/SKILL.md" ]] || continue
      skill="$(basename "$skill_dir")"
      backup_path "$target_root/$skill" "$surface_name-skills/$plugin/$skill"
      copy_dir "$skill_dir" "$target_root/$skill"
    done
  done
}

preflight_marketplace \
  "$ROOT_DIR/.claude-plugin/marketplace.json" "source Claude"
preflight_marketplace \
  "$ROOT_DIR/.agents/plugins/marketplace.json" "source Codex"
preflight_marketplace \
  "$TARGET/.claude-plugin/marketplace.json" "target Claude"
preflight_marketplace \
  "$TARGET/.agents/plugins/marketplace.json" "target Codex"

echo "Installing MultiAgent Methodology beta"
echo "- target: $TARGET"
echo "- surface: $SURFACE"
echo "- mode: $MODE"
echo "- provider: $PROVIDER"
echo "- dry-run: $DRY_RUN"

if [[ ! -d "$TARGET/.git" ]]; then
  echo "WARN: target is not a git repository: $TARGET" >&2
fi

reserve_backup_dir

install_plugin_roots

if surface_has "claude"; then
  backup_path "$TARGET/CLAUDE.md" "CLAUDE.md"
  run cp "$ROOT_DIR/adapters/claude/CLAUDE.md" "$TARGET/CLAUDE.md"

  merge_plugin_skills "claude" "$TARGET/.claude/skills"

  backup_path "$TARGET/.claude-plugin/marketplace.json" "claude-marketplace.json"
  upsert_marketplace \
    "$ROOT_DIR/.claude-plugin/marketplace.json" \
    "$TARGET/.claude-plugin/marketplace.json"

  if [[ "$WITH_CONFIG" -eq 1 ]]; then
    cfg="$TARGET/.claude/phased-handoff.config.md"
    if [[ -f "$cfg" ]]; then
      backup_path "$cfg" "phased-handoff.config.md"
      echo "+ keep existing $cfg (backed up; fill manually if stale)"
    else
      run mkdir -p "$TARGET/.claude"
      run cp "$ROOT_DIR/config/project.config.example.md" "$cfg"
      echo "+ NOTE: fill $cfg (leg argv / build·verify 명령 / provider capability) before relay/orchestrator use"
    fi
  fi
fi

if surface_has "codex"; then
  backup_path "$TARGET/AGENTS.md" "AGENTS.md"
  run cp "$ROOT_DIR/adapters/codex/AGENTS.md" "$TARGET/AGENTS.md"

  merge_plugin_skills "codex" "$TARGET/.codex/skills"

  backup_path "$TARGET/.agents/plugins/marketplace.json" "codex-marketplace.json"
  upsert_marketplace \
    "$ROOT_DIR/.agents/plugins/marketplace.json" \
    "$TARGET/.agents/plugins/marketplace.json"
fi

if [[ "$WITH_NITPICKER" -eq 1 ]]; then
  backup_path "$TARGET/nitpicker" "nitpicker"
  copy_dir "$ROOT_DIR/nitpicker" "$TARGET/nitpicker"
  [[ "$DRY_RUN" -eq 0 ]] && rm -f "$TARGET/nitpicker/nitpicker.config.json"
  if [[ "$DRY_RUN" -eq 0 && -f "$BACKUP_DIR/nitpicker/nitpicker.config.json" ]]; then
    cp "$BACKUP_DIR/nitpicker/nitpicker.config.json" "$TARGET/nitpicker/nitpicker.config.json"
  elif [[ "$DRY_RUN" -eq 0 && ! -f "$TARGET/nitpicker/nitpicker.config.json" ]]; then
    cp "$TARGET/nitpicker/nitpicker.config.example.json" "$TARGET/nitpicker/nitpicker.config.json"
    "${PYTHON_CMD[@]}" - "$TARGET/nitpicker/nitpicker.config.json" "$PROVIDER" <<'PY'
import json
import sys
path, provider = sys.argv[1], sys.argv[2]
with open(path, "r", encoding="utf-8") as fh:
    data = json.load(fh)
data["provider"] = provider
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
PY
  fi
fi

if [[ "$WITH_ZTR" -eq 1 ]]; then
  if [[ -d "$ZTR_HOME" ]]; then
    wrapper="$TARGET/.claude/ztr-run-phase.sh"
    backup_path "$wrapper" "ztr-run-phase.sh"
    run mkdir -p "$TARGET/.claude"
    if [[ "$DRY_RUN" -eq 0 ]]; then
      {
        echo '#!/usr/bin/env bash'
        echo '# Auto-generated by agent-workflow install.sh — drives ztr run-phase against this project.'
        echo '# cwd는 이 프로젝트 루트여야 한다(relay leg가 cwd 상속). 예: cd <project> && .claude/ztr-run-phase.sh --phase-id ... --prompt-file ...'
        echo 'set -euo pipefail'
        echo '# ZTR_HOME: 런타임 env로 override 가능(크로스머신). 미설정 시 설치 시점 경로 baked default.'
        echo "ZTR_HOME=\"\${ZTR_HOME:-$ZTR_HOME}\""
        echo 'if [[ -x "$ZTR_HOME/.venv/Scripts/python.exe" ]]; then PY="$ZTR_HOME/.venv/Scripts/python.exe";'
        echo 'elif [[ -x "$ZTR_HOME/.venv/bin/python" ]]; then PY="$ZTR_HOME/.venv/bin/python";'
        echo 'else PY="python"; fi'
        echo 'PYTHONPATH="$ZTR_HOME" "$PY" -m src run-phase "$@"'
      } > "$wrapper"
      chmod +x "$wrapper" 2>/dev/null || true
    fi
    echo "+ wrote ztr wrapper: $wrapper (ZTR_HOME=$ZTR_HOME)"
  else
    echo "WARN: ztr runtime not found at $ZTR_HOME — --with-ztr skipped" >&2
  fi
fi

if [[ "$MODE" == "strict" ]]; then
  echo "NOTE: strict hooks are not enabled in beta v1. Installed lite assets only."
fi

echo
echo "Installed. Next commands in target project:"
echo "  cd \"$TARGET\""
if [[ "$WITH_CONFIG" -eq 1 ]]; then
  echo "  # fill .claude/phased-handoff.config.md (leg argv / build·verify / provider) before relay 사용"
fi
if [[ "$WITH_ZTR" -eq 1 ]]; then
  echo "  .claude/ztr-run-phase.sh --phase-id <id> --prompt-file <prompt> --implementer-cmd <argv> ...  # relay"
fi
if surface_has "codex"; then
  echo "  # Codex: restart or Force Reload Skills, then use /skills or the Codex plugin UI"
fi
echo "  ${PYTHON_CMD[*]} nitpicker/run_nit.py --self-test"
echo "  ${PYTHON_CMD[*]} nitpicker/run_nit.py --provider mock --changed"
if [[ "$PROVIDER" == "ollama" ]]; then
  echo "  ollama list"
  echo "  ${PYTHON_CMD[*]} nitpicker/run_nit.py --changed"
fi
