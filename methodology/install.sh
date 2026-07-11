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
  - Backs up existing CLAUDE.md, AGENTS.md, .claude/skills, .codex/skills, Codex plugin assets, nitpicker, and phased-handoff.config.md before replacing them.
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
BACKUP_DIR="$TARGET/.agent-workflow-backup/$STAMP"
# ztr relay runtime = monorepo sibling of methodology/ (this script's parent).
ZTR_HOME="$(cd "$ROOT_DIR/.." 2>/dev/null && pwd)/runtimes/ztr"

python_works() {
  "$@" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 8) else 1)
PY
}

if [[ -n "$PYTHON_BIN" ]]; then
  # Intentionally split so PYTHON="py -3" works in Windows Git Bash.
  # shellcheck disable=SC2206
  PYTHON_CMD=($PYTHON_BIN)
  if ! python_works "${PYTHON_CMD[@]}"; then
    echo "Configured PYTHON is not a usable Python 3 interpreter: $PYTHON_BIN" >&2
    exit 2
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
}

backup_path() {
  local path="$1"
  local name="$2"
  if [[ -e "$path" ]]; then
    run mkdir -p "$BACKUP_DIR"
    run cp -R "$path" "$BACKUP_DIR/$name"
  fi
}

install_codex_marketplace() {
  local marketplace="$TARGET/.agents/plugins/marketplace.json"
  run mkdir -p "$(dirname "$marketplace")"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "+ update marketplace entry in $marketplace"
    return
  fi
  "${PYTHON_CMD[@]}" - "$marketplace" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if path.exists():
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
else:
    data = {
        "name": "local-repo",
        "interface": {"displayName": "Local Repo"},
        "plugins": [],
    }

data.setdefault("name", "local-repo")
if not isinstance(data.get("interface"), dict):
    data["interface"] = {}
data["interface"].setdefault("displayName", data["name"])
if not isinstance(data.get("plugins"), list):
    data["plugins"] = []
plugins = data["plugins"]
entry = {
    "name": "agent-workflow",
    "source": {
        "source": "local",
        "path": "./plugins/agent-workflow",
    },
    "policy": {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL",
    },
    "category": "Productivity",
}

for index, item in enumerate(plugins):
    if isinstance(item, dict) and item.get("name") == "agent-workflow":
        plugins[index] = entry
        break
else:
    plugins.append(entry)

with path.open("w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
PY
}

echo "Installing agent workflow beta"
echo "- target: $TARGET"
echo "- surface: $SURFACE"
echo "- mode: $MODE"
echo "- provider: $PROVIDER"
echo "- dry-run: $DRY_RUN"

if [[ ! -d "$TARGET/.git" ]]; then
  echo "WARN: target is not a git repository: $TARGET" >&2
fi

run mkdir -p "$BACKUP_DIR"

if surface_has "claude"; then
  backup_path "$TARGET/CLAUDE.md" "CLAUDE.md"
  run cp "$ROOT_DIR/adapters/claude/CLAUDE.md" "$TARGET/CLAUDE.md"

  backup_path "$TARGET/.claude/skills" "claude-skills"
  copy_dir "$ROOT_DIR/plugins/agent-workflow/skills" "$TARGET/.claude/skills"

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

  backup_path "$TARGET/.codex/skills" "codex-skills"
  copy_dir "$ROOT_DIR/plugins/agent-workflow/skills" "$TARGET/.codex/skills"

  backup_path "$TARGET/plugins/agent-workflow" "codex-plugin-agent-workflow"
  copy_dir "$ROOT_DIR/plugins/agent-workflow" "$TARGET/plugins/agent-workflow"

  backup_path "$TARGET/.agents/plugins/marketplace.json" "codex-marketplace.json"
  install_codex_marketplace
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
