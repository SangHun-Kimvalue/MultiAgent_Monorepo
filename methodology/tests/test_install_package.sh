#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="$(mktemp -d)"
SOURCE_CONFIG="$ROOT_DIR/nitpicker/nitpicker.config.json"
SOURCE_BACKUP="$(mktemp)"
SOURCE_CONFIG_EXISTED=0
PYTHON_CMD=()

python_works() {
  "$@" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 8) else 1)
PY
}

if [[ -n "${PYTHON:-}" ]]; then
  # Intentionally split so PYTHON="py -3" works in Windows Git Bash.
  # shellcheck disable=SC2206
  PYTHON_CMD=($PYTHON)
  if ! python_works "${PYTHON_CMD[@]}"; then
    echo "Configured PYTHON is not a usable Python 3 interpreter: $PYTHON" >&2
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

cleanup() {
  rm -rf "$TMP_ROOT"
  if [[ "$SOURCE_CONFIG_EXISTED" -eq 1 ]]; then
    cp "$SOURCE_BACKUP" "$SOURCE_CONFIG"
  else
    rm -f "$SOURCE_CONFIG"
  fi
  rm -f "$SOURCE_BACKUP"
}
trap cleanup EXIT

if [[ -f "$SOURCE_CONFIG" ]]; then
  SOURCE_CONFIG_EXISTED=1
  cp "$SOURCE_CONFIG" "$SOURCE_BACKUP"
fi

write_config() {
  local path="$1"
  local provider="$2"
  local sentinel="${3:-}"
  mkdir -p "$(dirname "$path")"
  "${PYTHON_CMD[@]}" - "$path" "$provider" "$sentinel" <<'PY'
import json
import sys

path, provider, sentinel = sys.argv[1], sys.argv[2], sys.argv[3]
data = {"provider": provider}
if sentinel:
    data["sentinel"] = sentinel
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
PY
}

provider_of() {
  "${PYTHON_CMD[@]}" - "$1" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    print(json.load(fh)["provider"])
PY
}

assert_provider() {
  local path="$1"
  local expected="$2"
  local actual
  actual="$(provider_of "$path")"
  if [[ "$actual" != "$expected" ]]; then
    echo "provider mismatch for $path: expected=$expected actual=$actual" >&2
    exit 1
  fi
}

assert_contains_sentinel() {
  local path="$1"
  local expected="$2"
  "${PYTHON_CMD[@]}" - "$path" "$expected" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    actual = json.load(fh).get("sentinel")
if actual != sys.argv[2]:
    raise SystemExit(f"sentinel mismatch: expected={sys.argv[2]} actual={actual}")
PY
}

assert_package_excludes_live_config() {
  local artifact="$1"
  "${PYTHON_CMD[@]}" - "$artifact" <<'PY'
import sys
import tarfile
import zipfile
from pathlib import Path

artifact = Path(sys.argv[1])
if artifact.suffix == ".zip":
    with zipfile.ZipFile(artifact) as zf:
        names = zf.namelist()
elif artifact.name.endswith(".tar.gz"):
    with tarfile.open(artifact, "r:gz") as tf:
        names = tf.getnames()
else:
    raise SystemExit(f"unsupported artifact type: {artifact}")

blocked = [
    name
    for name in names
    if name.endswith("nitpicker/nitpicker.config.json")
    or "__pycache__" in Path(name).parts
    or name.endswith((".pyc", ".pyo"))
]
if blocked:
    raise SystemExit("private or generated file leaked into package: " + ", ".join(blocked))
PY
}

assert_files_identical() {
  local a="$1"
  local b="$2"
  if [[ ! -f "$b" ]]; then
    echo "expected installed file missing: $b" >&2
    exit 1
  fi
  if ! cmp -s "$a" "$b"; then
    echo "installed file differs from source: $b" >&2
    exit 1
  fi
}

assert_tree_excludes_python_cache() {
  local root="$1"
  "${PYTHON_CMD[@]}" - "$root" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
blocked = [
    str(path)
    for path in root.rglob("*")
    if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}
]
if blocked:
    raise SystemExit("Python cache leaked into installed tree: " + ", ".join(blocked))
PY
}

assert_yaml_interface() {
  local path="$1"
  "${PYTHON_CMD[@]}" - "$path" <<'PY'
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as fh:
    text = fh.read()
required = ("interface:", "display_name:", "short_description:", "default_prompt:")
missing = [k for k in required if k not in text]
if missing:
    raise SystemExit(f"openai.yaml missing keys {missing} in {path}")
PY
}

assert_openai_yaml_contracts() {
  local plugin_root="$1"
  "${PYTHON_CMD[@]}" - "$plugin_root" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
paths = sorted(root.glob("skills/*/agents/openai.yaml"))
if len(paths) != 4:
    raise SystemExit(f"expected 4 openai.yaml files, observed {len(paths)}")
required = ("interface:", "display_name:", "short_description:", "default_prompt:")
for path in paths:
    text = path.read_text(encoding="utf-8")
    missing = [key for key in required if key not in text]
    if missing:
        raise SystemExit(f"openai.yaml missing keys {missing} in {path}")
phased = root / "skills" / "phased-implementation-handoff" / "agents" / "openai.yaml"
if "$phased-implementation-handoff" not in phased.read_text(encoding="utf-8"):
    raise SystemExit("phased handoff openai.yaml lacks required skill invocation token")
PY
}

assert_codex_hook_bundle() {
  local plugin_root="$1"
  "${PYTHON_CMD[@]}" - "$plugin_root" <<'PY'
import json
import os
import sys

root = sys.argv[1]
manifest = os.path.join(root, ".codex-plugin", "plugin.json")
with open(manifest, "r", encoding="utf-8") as fh:
    data = json.load(fh)
hooks = data.get("hooks")
if hooks != "./hooks/hooks.json":
    raise SystemExit(f"codex manifest hooks path unexpected: {hooks!r}")
norm = os.path.normpath(hooks)
if norm.startswith("..") or os.path.isabs(norm):
    raise SystemExit(f"codex hooks path escapes plugin root: {hooks!r}")
for rel in ("hooks/hooks.json", "scripts/emit_compaction_boot_context.py"):
    if not os.path.isfile(os.path.join(root, rel)):
        raise SystemExit(f"missing installed hook asset: {rel}")
with open(os.path.join(root, "hooks", "hooks.json"), "r", encoding="utf-8") as fh:
    hj = json.load(fh)
session_start = hj["hooks"]["SessionStart"][0]
if session_start.get("matcher") != "compact":
    raise SystemExit("SessionStart matcher is not 'compact'")
handler = session_start["hooks"][0]
extra = set(handler) - {"type", "command", "timeout"}
if extra:
    raise SystemExit(f"hook handler has non-common keys: {sorted(extra)}")
PY
}

assert_orchestrator_contract() {
  local skill_path="$1"
  local config_path="$2"
  "${PYTHON_CMD[@]}" - "$skill_path" "$config_path" <<'PY'
import sys
from pathlib import Path

skill = Path(sys.argv[1]).read_text(encoding="utf-8")
config = Path(sys.argv[2]).read_text(encoding="utf-8")
skill_tokens = (
    "remediation_adapter.py --human-triggered",
    "--accept-leg orchestrator-accepted-review",
    "--record --max-rounds <N>",
    "reapply-status",
    "exact integer `1..5`",
    "while/retry/autofix",
)
missing = [token for token in skill_tokens if token not in skill]
if missing:
    raise SystemExit(f"phase-cycle Step 7 contract missing tokens: {missing}")
if config.count("fix_rounds_max: 3") != 1:
    raise SystemExit("installed project config must contain exactly one fix_rounds_max: 3")
for token in ("기본값은 `3`", "exact integer `1..5`", "runtime validator"):
    if token not in config:
        raise SystemExit(f"project config corrective-round contract missing: {token}")
PY
}

assert_protected_current_config_anchors() {
  local config_path="$1"
  "${PYTHON_CMD[@]}" - "$config_path" <<'PY'
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
protected = (
    'verified_cli_version: "codex-cli 0.142.2"',
    'model: "default (무pin — 0.142.2에서 무pin exec 정상, 2026-07-10 세션 다수 실측)"',
    'verified_cli_version: "2.1.176 (Claude Code)"',
    'result: "(superseded 2026-07-10) 구버전 0.130 실측: 무pin 시 gpt-5.3-codex unsupported-model 오류 → -m gpt-5.5 pin. npm 0.142.2 재바인딩 후 무pin exec 정상(세션 다수 실측)이라 pin 제거."',
)
for anchor in protected:
    if text.count(anchor) != 1:
        raise SystemExit(f"protected capability/historical anchor changed or duplicated: {anchor}")
current = (
    "- Codex version: `codex-cli 0.145.0`",
    "- Claude version: `2.1.215 (Claude Code)`",
    "> ✅ **재바인딩 완료(2026-07-22)**",
)
for anchor in current:
    if text.count(anchor) != 1:
        raise SystemExit(f"current-version anchor missing or duplicated: {anchor}")
PY
}

assert_package_orchestrator_contract() {
  local artifact="$1"
  "${PYTHON_CMD[@]}" - "$artifact" <<'PY'
import sys
import tarfile
import zipfile
from pathlib import Path

artifact = Path(sys.argv[1])
required_suffixes = {
    "skill": "plugins/agent-workflow/skills/phase-cycle-orchestrator/SKILL.md",
    "config": "config/project.config.example.md",
}
if artifact.suffix == ".zip":
    with zipfile.ZipFile(artifact) as archive:
        members = {name: archive.read(name).decode("utf-8") for name in archive.namelist()}
elif artifact.name.endswith(".tar.gz"):
    with tarfile.open(artifact, "r:gz") as archive:
        members = {
            member.name: archive.extractfile(member).read().decode("utf-8")
            for member in archive.getmembers()
            if member.isfile()
        }
else:
    raise SystemExit(f"unsupported artifact type: {artifact}")
resolved = {}
for label, suffix in required_suffixes.items():
    matches = [text for name, text in members.items() if name.endswith(suffix)]
    if len(matches) != 1:
        raise SystemExit(f"expected exactly one packaged {label} ending {suffix}, found {len(matches)}")
    resolved[label] = matches[0]
if "--accept-leg orchestrator-accepted-review" not in resolved["skill"]:
    raise SystemExit("packaged Step 7 lacks accepted-only fix-round binding")
if resolved["config"].count("fix_rounds_max: 3") != 1:
    raise SystemExit("packaged project config lacks unique fix_rounds_max: 3")
PY
}

install_fake_zip() {
  local fake_bin="$1"
  mkdir -p "$fake_bin"
  cat >"$fake_bin/zip" <<'SH'
#!/usr/bin/env bash
set -euo pipefail

PYTHON_CMD=()
python_works() {
  "$@" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 8) else 1)
PY
}

if command -v python3 >/dev/null 2>&1 && python_works python3; then
  PYTHON_CMD=(python3)
elif command -v python >/dev/null 2>&1 && python_works python; then
  PYTHON_CMD=(python)
elif command -v py >/dev/null 2>&1 && python_works py -3; then
  PYTHON_CMD=(py -3)
else
  echo "fake zip requires Python 3" >&2
  exit 2
fi

if [[ "${1:-}" != "-r" || $# -lt 3 ]]; then
  echo "fake zip only supports: zip -r OUT ITEMS... -x EXCLUDES..." >&2
  exit 2
fi
shift
out="$1"
shift
args_file="$(mktemp)"
trap 'rm -f "$args_file"' EXIT
printf '%s\n' "$@" >"$args_file"
"${PYTHON_CMD[@]}" - "$out" "$args_file" <<'PY'
import fnmatch
import os
import sys
import zipfile
from pathlib import Path

out = Path(sys.argv[1])
args = Path(sys.argv[2]).read_text(encoding="utf-8").splitlines()
items: list[str] = []
excludes: list[str] = []
target = items
for arg in args:
    if arg == "-x":
        target = excludes
        continue
    target.append(arg)

def is_excluded(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in excludes)

out.parent.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(out, "w") as zf:
    for item in items:
        root = Path(item)
        if root.is_dir():
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                arcname = path.as_posix()
                if not is_excluded(arcname):
                    zf.write(path, arcname)
        elif root.is_file():
            arcname = root.as_posix()
            if not is_excluded(arcname):
                zf.write(root, arcname)
PY
SH
  chmod +x "$fake_bin/zip"
}

echo "SMOKE_ROOT=$TMP_ROOT"

assert_orchestrator_contract \
  "$ROOT_DIR/plugins/agent-workflow/skills/phase-cycle-orchestrator/SKILL.md" \
  "$ROOT_DIR/config/project.config.example.md"
assert_protected_current_config_anchors "$ROOT_DIR/../.claude/phased-handoff.config.md"
echo "PASS source phase-cycle Step 7, corrective-round config, and protected anchors"

# (a) load-bearing: ignored source live config must not override --provider.
write_config "$SOURCE_CONFIG" "ollama" "source-live"
target_a="$TMP_ROOT/fresh-source-live"
mkdir -p "$target_a"
"$ROOT_DIR/install.sh" --target "$target_a" --provider mock >/tmp/mam_install_a.log
assert_provider "$target_a/nitpicker/nitpicker.config.json" "mock"
echo "PASS a fresh source live config removed before provider generation"

# (b) clean source still generates the requested provider.
rm -f "$SOURCE_CONFIG"
target_b="$TMP_ROOT/fresh-clean-source"
mkdir -p "$target_b"
"$ROOT_DIR/install.sh" --target "$target_b" --provider mock >/tmp/mam_install_b.log
assert_provider "$target_b/nitpicker/nitpicker.config.json" "mock"
echo "PASS b fresh clean source provider mock"

# (c) existing target config is preserved after reinstall.
target_c="$TMP_ROOT/reinstall-preserve"
mkdir -p "$target_c/nitpicker"
write_config "$target_c/nitpicker/nitpicker.config.json" "ollama" "keep-me"
"$ROOT_DIR/install.sh" --target "$target_c" --provider mock >/tmp/mam_install_c.log
assert_provider "$target_c/nitpicker/nitpicker.config.json" "ollama"
assert_contains_sentinel "$target_c/nitpicker/nitpicker.config.json" "keep-me"
echo "PASS c reinstall preserves target user config"

# (d) dry-run must not mutate target files.
target_d="$TMP_ROOT/dry-run"
mkdir -p "$target_d/nitpicker"
write_config "$target_d/nitpicker/nitpicker.config.json" "ollama" "dry-run"
before_d="$TMP_ROOT/dry-run-before.json"
cp "$target_d/nitpicker/nitpicker.config.json" "$before_d"
"$ROOT_DIR/install.sh" --target "$target_d" --provider mock --dry-run >/tmp/mam_install_d.log
cmp "$before_d" "$target_d/nitpicker/nitpicker.config.json" >/dev/null
echo "PASS d dry-run leaves target config unchanged"

# (e) package artifact must not contain ignored live config.
write_config "$SOURCE_CONFIG" "ollama" "source-live"
package_output="$(PACKAGE_ZTR=0 "$ROOT_DIR/package.sh")"
echo "$package_output" >/tmp/mam_package_e.log
artifact="$(printf "%s\n" "$package_output" | tail -n 1)"
assert_package_excludes_live_config "$artifact"
assert_package_orchestrator_contract "$artifact"
echo "PACKAGE_ARTIFACT=$artifact"
fake_bin="$TMP_ROOT/fake-bin"
install_fake_zip "$fake_bin"
zip_package_output="$(PATH="$fake_bin:$PATH" PACKAGE_ZTR=0 "$ROOT_DIR/package.sh")"
echo "$zip_package_output" >/tmp/mam_package_e_zip.log
zip_artifact="$(printf "%s\n" "$zip_package_output" | tail -n 1)"
assert_package_excludes_live_config "$zip_artifact"
assert_package_orchestrator_contract "$zip_artifact"
echo "PACKAGE_ZIP_ARTIFACT=$zip_artifact"
echo "PASS e package excludes nitpicker.config.json and Python caches from tar and zip paths"

# (f) claude surface installs prepare-session-compaction skill identical to source.
skill_src="$ROOT_DIR/plugins/agent-workflow/skills/prepare-session-compaction/SKILL.md"
orchestrator_skill_src="$ROOT_DIR/plugins/agent-workflow/skills/phase-cycle-orchestrator/SKILL.md"
target_f="$TMP_ROOT/surface-claude"
mkdir -p "$target_f"
"$ROOT_DIR/install.sh" --target "$target_f" --provider mock >/tmp/mam_install_f.log
assert_files_identical "$skill_src" "$target_f/.claude/skills/prepare-session-compaction/SKILL.md"
assert_files_identical "$orchestrator_skill_src" "$target_f/.claude/skills/phase-cycle-orchestrator/SKILL.md"
assert_tree_excludes_python_cache "$target_f/.claude/skills"
assert_orchestrator_contract \
  "$target_f/.claude/skills/phase-cycle-orchestrator/SKILL.md" \
  "$target_f/.claude/phased-handoff.config.md"
echo "PASS f claude surface installs prepare-session-compaction SKILL.md (identical to source)"

# (g) codex surface installs skill + shared compaction hook bundle.
target_g="$TMP_ROOT/surface-codex"
mkdir -p "$target_g"
"$ROOT_DIR/install.sh" --target "$target_g" --surface codex --provider mock >/tmp/mam_install_g.log
assert_files_identical "$skill_src" "$target_g/.codex/skills/prepare-session-compaction/SKILL.md"
assert_files_identical "$orchestrator_skill_src" "$target_g/plugins/agent-workflow/skills/phase-cycle-orchestrator/SKILL.md"
assert_yaml_interface "$target_g/.codex/skills/prepare-session-compaction/agents/openai.yaml"
assert_codex_hook_bundle "$target_g/plugins/agent-workflow"
assert_openai_yaml_contracts "$target_g/plugins/agent-workflow"
assert_tree_excludes_python_cache "$target_g/.codex/skills"
assert_tree_excludes_python_cache "$target_g/plugins/agent-workflow"
echo "PASS g codex surface installs skill + hook bundle + manifest hooks path"
