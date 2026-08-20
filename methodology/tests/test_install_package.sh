#!/usr/bin/env bash
set -euo pipefail

CANONICAL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="$(mktemp -d)"
ROOT_DIR="$TMP_ROOT/source"
PYTHON_CMD=()

cleanup() {
  rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

mkdir -p "$ROOT_DIR"
cp -R "$CANONICAL_ROOT/." "$ROOT_DIR"
rm -rf "$ROOT_DIR/dist"
SOURCE_CONFIG="$ROOT_DIR/nitpicker/nitpicker.config.json"

python_works() {
  "$@" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 8) else 1)
PY
}

if [[ -n "${PYTHON:-}" ]]; then
  PYTHON_CMD=("$PYTHON")
  if ! python_works "${PYTHON_CMD[@]}"; then
    read -r -a PYTHON_CMD <<<"$PYTHON"
    [[ "${#PYTHON_CMD[@]}" -gt 0 ]] && python_works "${PYTHON_CMD[@]}" || {
      echo "Configured PYTHON is unusable" >&2
      exit 2
    }
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

write_json() {
  local path="$1"
  local payload="$2"
  mkdir -p "$(dirname "$path")"
  "${PYTHON_CMD[@]}" - "$path" "$payload" <<'PY'
import json
import sys
from pathlib import Path
Path(sys.argv[1]).write_text(json.dumps(json.loads(sys.argv[2]), indent=2) + "\n", encoding="utf-8")
PY
}

write_config() {
  local path="$1"
  local provider="$2"
  local sentinel="${3:-}"
  write_json "$path" "{\"provider\":\"$provider\",\"sentinel\":\"$sentinel\"}"
}

assert_provider() {
  "${PYTHON_CMD[@]}" - "$1" "$2" <<'PY'
import json
import sys
from pathlib import Path
value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if value["provider"] != sys.argv[2]:
    raise SystemExit(f"provider mismatch: {value['provider']} != {sys.argv[2]}")
PY
}

assert_contains_sentinel() {
  "${PYTHON_CMD[@]}" - "$1" "$2" <<'PY'
import json
import sys
from pathlib import Path
value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if value.get("sentinel") != sys.argv[2]:
    raise SystemExit("sentinel mismatch")
PY
}

tree_digest() {
  "${PYTHON_CMD[@]}" - "$1" <<'PY'
import hashlib
import os
import sys
from pathlib import Path
root = Path(sys.argv[1])
digest = hashlib.sha256()
if not root.exists():
    print("ABSENT")
    raise SystemExit(0)
for path in sorted(root.rglob("*")):
    relative = path.relative_to(root).as_posix()
    if path.is_symlink():
        data = f"SYMLINK:{os.readlink(path)}".encode()
    elif path.is_file():
        data = path.read_bytes()
    else:
        continue
    digest.update(relative.encode("utf-8") + b"\0" + data + b"\0")
print(digest.hexdigest())
PY
}

assert_files_identical() {
  [[ -f "$2" ]] || { echo "missing installed file: $2" >&2; exit 1; }
  cmp -s "$1" "$2" || { echo "installed file differs: $2" >&2; exit 1; }
}

assert_tree_excludes_python_cache() {
  "${PYTHON_CMD[@]}" - "$1" <<'PY'
import sys
from pathlib import Path
blocked = [str(p) for p in Path(sys.argv[1]).rglob("*") if "__pycache__" in p.parts or p.suffix in {".pyc", ".pyo"}]
if blocked:
    raise SystemExit("Python cache leaked: " + ", ".join(blocked))
PY
}

assert_openai_yaml_contracts() {
  local plugin_root="$1"
  local expected_count="$2"
  "${PYTHON_CMD[@]}" - "$plugin_root" "$expected_count" <<'PY'
import sys
from pathlib import Path
root = Path(sys.argv[1])
expected = int(sys.argv[2])
paths = sorted(root.glob("skills/*/agents/openai.yaml"))
if len(paths) != expected:
    raise SystemExit(f"expected {expected} openai.yaml files, observed {len(paths)}")
required = ("interface:", "display_name:", "short_description:", "default_prompt:")
for path in paths:
    text = path.read_text(encoding="utf-8")
    missing = [key for key in required if key not in text]
    if missing:
        raise SystemExit(f"missing {missing} in {path}")
PY
}

assert_marketplace_contract() {
  local path="$1"
  local surface="$2"
  local unknown="${3:-}"
  "${PYTHON_CMD[@]}" - "$path" "$surface" "$unknown" <<'PY'
import json
import sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
plugins = data["plugins"]
for name in ("agent-workflow", "ai-research"):
    rows = [item for item in plugins if isinstance(item, dict) and item.get("name") == name]
    if len(rows) != 1:
        raise SystemExit(f"{name} count is {len(rows)}")
    row = rows[0]
    if sys.argv[2] == "claude":
        if row.get("source") != f"./plugins/{name}":
            raise SystemExit(f"Claude source mismatch for {name}")
    else:
        expected = {"source": "local", "path": f"./plugins/{name}"}
        if row.get("source") != expected:
            raise SystemExit(f"Codex source mismatch for {name}")
        if row.get("policy") != {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}:
            raise SystemExit(f"Codex policy mismatch for {name}")
        if row.get("category") != "Productivity":
            raise SystemExit(f"Codex category mismatch for {name}")
if sys.argv[2] == "claude" and data.get("metadata", {}).get("version") != "1.2.0":
    raise SystemExit("Claude marketplace metadata version mismatch")
if sys.argv[3] and sum(1 for item in plugins if isinstance(item, dict) and item.get("name") == sys.argv[3]) != 1:
    raise SystemExit("unknown marketplace entry was not preserved")
PY
}

assert_package_contract() {
  "${PYTHON_CMD[@]}" - "$1" <<'PY'
import sys
import tarfile
import zipfile
from pathlib import Path
artifact = Path(sys.argv[1])
if artifact.suffix == ".zip":
    with zipfile.ZipFile(artifact) as bundle:
        names = set(bundle.namelist())
elif artifact.name.endswith(".tar.gz"):
    with tarfile.open(artifact, "r:gz") as bundle:
        names = set(bundle.getnames())
else:
    raise SystemExit(f"unsupported artifact: {artifact}")
required = (
    ".claude-plugin/marketplace.json",
    ".agents/plugins/marketplace.json",
    "plugins/agent-workflow/.codex-plugin/plugin.json",
    "plugins/ai-research/.codex-plugin/plugin.json",
    "tools/workflow_doctor.py",
    "tools/p5_fresh_discovery.py",
    "install.sh",
    "package.sh",
)
for suffix in required:
    if not any(name.endswith(suffix) for name in names):
        raise SystemExit(f"package missing {suffix}")
blocked = [
    name for name in names
    if "tests" in Path(name).parts
    or "dist" in Path(name).parts
    or "__pycache__" in Path(name).parts
    or name.endswith((".pyc", ".pyo", "nitpicker/nitpicker.config.json"))
]
if blocked:
    raise SystemExit("excluded content leaked: " + ", ".join(sorted(blocked)))
PY
}

assert_codex_hook_bundle() {
  "${PYTHON_CMD[@]}" - "$1" <<'PY'
import json
import sys
from pathlib import Path
root = Path(sys.argv[1])
manifest = json.loads((root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
if manifest.get("hooks") != "./hooks/hooks.json":
    raise SystemExit("agent-workflow hooks path mismatch")
for relative in ("hooks/hooks.json", "scripts/emit_compaction_boot_context.py"):
    if not (root / relative).is_file():
        raise SystemExit(f"missing hook asset: {relative}")
PY
}

assert_orchestrator_contract() {
  "${PYTHON_CMD[@]}" - "$1" "$2" <<'PY'
import sys
from pathlib import Path
skill = Path(sys.argv[1]).read_text(encoding="utf-8")
config = Path(sys.argv[2]).read_text(encoding="utf-8")
for token in ("remediation_adapter.py --human-triggered", "--accept-leg orchestrator-accepted-review", "--record --max-rounds <N>", "reapply-status", "exact integer `1..5`", "while/retry/prose-autofix"):
    if token not in skill:
        raise SystemExit(f"orchestrator contract missing {token}")
if config.count("fix_rounds_max: 3") != 1:
    raise SystemExit("config fix_rounds_max mismatch")
PY
}

install_fake_zip_tripwire() {
  local fake_bin="$1"
  local marker="$2"
  mkdir -p "$fake_bin"
  cat >"$fake_bin/zip" <<SH
#!/usr/bin/env bash
printf called >"$marker"
exit 99
SH
  chmod +x "$fake_bin/zip"
}

install_fake_backup_mkdir_failure() {
  local fake_bin="$1"
  local real_mkdir="$2"
  mkdir -p "$fake_bin"
  cat >"$fake_bin/mkdir" <<SH
#!/usr/bin/env bash
if [[ "\$#" -eq 1 && "\$1" == */.agent-workflow-backup/* ]]; then
  exit 1
fi
exec "$real_mkdir" "\$@"
SH
  chmod +x "$fake_bin/mkdir"
}

make_spaced_python_shim() {
  local path="$1"
  local interpreter="$2"
  mkdir -p "$(dirname "$path")"
  cat >"$path" <<SH
#!/usr/bin/env bash
exec "$interpreter" "\$@"
SH
  chmod +x "$path"
}

echo "SMOKE_ROOT=$TMP_ROOT"

assert_marketplace_contract "$ROOT_DIR/.claude-plugin/marketplace.json" claude
assert_marketplace_contract "$ROOT_DIR/.agents/plugins/marketplace.json" codex
assert_openai_yaml_contracts "$ROOT_DIR/plugins/agent-workflow" 4
assert_openai_yaml_contracts "$ROOT_DIR/plugins/ai-research" 2
assert_orchestrator_contract \
  "$ROOT_DIR/plugins/agent-workflow/skills/phase-cycle-orchestrator/SKILL.md" \
  "$ROOT_DIR/config/project.config.example.md"
echo "PASS canonical marketplaces, plugin inventories, and existing orchestrator contract"

# Existing nitpicker provider and dry-run behavior remain intact, using only the source copy.
write_config "$SOURCE_CONFIG" ollama source-live
target_a="$TMP_ROOT/fresh-source-live"
mkdir -p "$target_a"
"$ROOT_DIR/install.sh" --target "$target_a" --provider mock >/tmp/mam_install_a.log
assert_provider "$target_a/nitpicker/nitpicker.config.json" mock

rm -f "$SOURCE_CONFIG"
target_b="$TMP_ROOT/fresh-clean-source"
mkdir -p "$target_b"
"$ROOT_DIR/install.sh" --target "$target_b" --provider mock >/tmp/mam_install_b.log
assert_provider "$target_b/nitpicker/nitpicker.config.json" mock

target_c="$TMP_ROOT/reinstall-preserve"
mkdir -p "$target_c/nitpicker"
write_config "$target_c/nitpicker/nitpicker.config.json" ollama keep-me
"$ROOT_DIR/install.sh" --target "$target_c" --provider mock >/tmp/mam_install_c.log
assert_provider "$target_c/nitpicker/nitpicker.config.json" ollama
assert_contains_sentinel "$target_c/nitpicker/nitpicker.config.json" keep-me

target_d="$TMP_ROOT/dry-run"
mkdir -p "$target_d/nitpicker"
write_config "$target_d/nitpicker/nitpicker.config.json" ollama dry-run
before_d="$(tree_digest "$target_d")"
"$ROOT_DIR/install.sh" --target "$target_d" --surface both --provider mock --dry-run >/tmp/mam_install_d.log
[[ "$(tree_digest "$target_d")" == "$before_d" ]] || { echo "dry-run mutated target" >&2; exit 1; }
echo "PASS existing provider preservation and dry-run behavior"

# Both surfaces preserve unrelated skills/entries while canonical duplicates converge.
target_merge="$TMP_ROOT/merge-target"
mkdir -p "$target_merge/.claude/skills/sentinel" "$target_merge/.codex/skills/sentinel"
printf sentinel >"$target_merge/.claude/skills/sentinel/SKILL.md"
printf sentinel >"$target_merge/.codex/skills/sentinel/SKILL.md"
mkdir -p "$target_merge/.claude/skills/daily-ai-news" "$target_merge/.codex/skills/daily-ai-news"
printf old >"$target_merge/.claude/skills/daily-ai-news/SKILL.md"
printf old >"$target_merge/.codex/skills/daily-ai-news/SKILL.md"
mkdir -p "$target_merge/plugins/agent-workflow" "$target_merge/plugins/ai-research"
printf old >"$target_merge/plugins/agent-workflow/old.txt"
printf old >"$target_merge/plugins/ai-research/old.txt"
write_json "$target_merge/.claude-plugin/marketplace.json" '{"name":"custom","plugins":[{"name":"third-party","source":"./third"},{"name":"ai-research","source":"bad"},{"name":"ai-research","source":"bad2"}]}'
write_json "$target_merge/.agents/plugins/marketplace.json" '{"name":"custom","plugins":[{"name":"third-party","source":{"source":"local","path":"./third"}},{"name":"agent-workflow","source":{}},{"name":"agent-workflow","source":{}}]}'

"$ROOT_DIR/install.sh" --target "$target_merge" --surface both --provider mock >/tmp/mam_install_merge_1.log
assert_marketplace_contract "$target_merge/.claude-plugin/marketplace.json" claude third-party
assert_marketplace_contract "$target_merge/.agents/plugins/marketplace.json" codex third-party
cmp -s "$target_merge/.claude/skills/sentinel/SKILL.md" "$target_merge/.codex/skills/sentinel/SKILL.md"
for plugin in agent-workflow ai-research; do
  assert_tree_excludes_python_cache "$target_merge/plugins/$plugin"
  for skill_dir in "$ROOT_DIR/plugins/$plugin/skills"/*; do
    [[ -f "$skill_dir/SKILL.md" ]] || continue
    skill="$(basename "$skill_dir")"
    assert_files_identical "$skill_dir/SKILL.md" "$target_merge/.claude/skills/$skill/SKILL.md"
    assert_files_identical "$skill_dir/SKILL.md" "$target_merge/.codex/skills/$skill/SKILL.md"
  done
done
assert_openai_yaml_contracts "$target_merge/plugins/agent-workflow" 4
assert_openai_yaml_contracts "$target_merge/plugins/ai-research" 2
assert_codex_hook_bundle "$target_merge/plugins/agent-workflow"

claude_skills_before="$(tree_digest "$target_merge/.claude/skills")"
codex_skills_before="$(tree_digest "$target_merge/.codex/skills")"
plugins_before="$(tree_digest "$target_merge/plugins")"
"$ROOT_DIR/install.sh" --target "$target_merge" --surface both --provider mock >/tmp/mam_install_merge_2.log
[[ "$(tree_digest "$target_merge/.claude/skills")" == "$claude_skills_before" ]]
[[ "$(tree_digest "$target_merge/.codex/skills")" == "$codex_skills_before" ]]
[[ "$(tree_digest "$target_merge/plugins")" == "$plugins_before" ]]
backup_count="$(find "$target_merge/.agent-workflow-backup" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')"
[[ "$backup_count" -ge 2 ]] || { echo "expected independent backup snapshots" >&2; exit 1; }
find "$target_merge/.agent-workflow-backup" -path '*/plugins/agent-workflow' -type d | grep -q .
find "$target_merge/.agent-workflow-backup" -path '*/plugins/ai-research' -type d | grep -q .
find "$target_merge/.agent-workflow-backup" -path '*/claude-skills/ai-research/daily-ai-news' -type d | grep -q .
find "$target_merge/.agent-workflow-backup" -path '*/codex-skills/ai-research/daily-ai-news' -type d | grep -q .
echo "PASS dual plugin merge, sentinel preservation, per-plugin/skill backup, and idempotence"

# Any malformed target marketplace blocks before the first target mutation.
for fixture in malformed nonarray; do
  target_bad="$TMP_ROOT/bad-$fixture"
  mkdir -p "$target_bad/sentinel"
  printf protected >"$target_bad/sentinel/value.txt"
  mkdir -p "$target_bad/.claude-plugin"
  if [[ "$fixture" == malformed ]]; then
    printf '{bad' >"$target_bad/.claude-plugin/marketplace.json"
  else
    write_json "$target_bad/.claude-plugin/marketplace.json" '{"plugins":{}}'
  fi
  before_bad="$(tree_digest "$target_bad")"
  if "$ROOT_DIR/install.sh" --target "$target_bad" --surface both --provider mock >/tmp/mam_install_bad.log 2>&1; then
    echo "malformed marketplace unexpectedly installed" >&2
    exit 1
  else
    rc=$?
  fi
  [[ "$rc" -eq 2 ]] || { echo "malformed preflight exit was $rc" >&2; exit 1; }
  [[ "$(tree_digest "$target_bad")" == "$before_bad" ]] || { echo "malformed preflight mutated target" >&2; exit 1; }
done
echo "PASS malformed/non-array marketplace preflight fails closed with unchanged target bytes"

# A failed backup mkdir without a colliding candidate must terminate instead of retrying forever.
mkdir_failure_bin="$TMP_ROOT/mkdir-failure-bin"
install_fake_backup_mkdir_failure "$mkdir_failure_bin" "$(command -v mkdir)"
target_mkdir_failure="$TMP_ROOT/backup-mkdir-failure"
mkdir -p "$target_mkdir_failure"
if PATH="$mkdir_failure_bin:$PATH" "$ROOT_DIR/install.sh" --target "$target_mkdir_failure" --provider mock >/tmp/mam_install_mkdir_failure.log 2>&1; then
  echo "backup mkdir failure unexpectedly installed" >&2
  exit 1
else
  rc=$?
fi
[[ "$rc" -eq 2 ]] || { echo "backup mkdir failure exit was $rc" >&2; exit 1; }
[[ ! -e "$target_mkdir_failure/.claude-plugin/marketplace.json" ]]
echo "PASS backup mkdir failure exits without collision retry"

# PYTHON first treats a space-containing executable as one path for both install and package.
host_python="$(command -v python3 || command -v python)"
python_works "$host_python" || { echo "host Python is unusable" >&2; exit 1; }
spaced_python="$TMP_ROOT/python path/python shim"
make_spaced_python_shim "$spaced_python" "$host_python"
target_spaced_python="$TMP_ROOT/spaced-python-install"
mkdir -p "$target_spaced_python"
PYTHON="$spaced_python" "$ROOT_DIR/install.sh" --target "$target_spaced_python" --provider mock >/tmp/mam_install_spaced_python.log
assert_provider "$target_spaced_python/nitpicker/nitpicker.config.json" mock

# Force each package backend exactly once. An external zip at PATH head is a tripwire only.
write_config "$SOURCE_CONFIG" ollama source-copy-only
fake_bin="$TMP_ROOT/fake-bin"
zip_marker="$TMP_ROOT/external-zip-called"
install_fake_zip_tripwire "$fake_bin" "$zip_marker"
zip_output="$(PATH="$fake_bin:$PATH" PACKAGE_FORMAT=zip PACKAGE_ZTR=0 "$ROOT_DIR/package.sh")"
zip_artifact="$(printf '%s\n' "$zip_output" | tail -n 1)"
[[ "$zip_artifact" == *.zip ]]
[[ "$zip_artifact" == */multiagent-methodology-beta-*.zip ]]
[[ ! -e "$zip_marker" ]] || { echo "external zip tripwire was called" >&2; exit 1; }
assert_package_contract "$zip_artifact"

space_zip_output="$(PYTHON="$spaced_python" PACKAGE_FORMAT=zip PACKAGE_ZTR=0 "$ROOT_DIR/package.sh")"
space_zip_artifact="$(printf '%s\n' "$space_zip_output" | tail -n 1)"
assert_package_contract "$space_zip_artifact"

tar_output="$(PACKAGE_FORMAT=tar PACKAGE_ZTR=0 "$ROOT_DIR/package.sh")"
tar_artifact="$(printf '%s\n' "$tar_output" | tail -n 1)"
[[ "$tar_artifact" == *.tar.gz ]]
[[ "$tar_artifact" == */multiagent-methodology-beta-*.tar.gz ]]
assert_package_contract "$tar_artifact"

artifact_count="$(find "$ROOT_DIR/dist" -maxdepth 1 -type f | wc -l | tr -d ' ')"
if PACKAGE_FORMAT=rar PACKAGE_ZTR=0 "$ROOT_DIR/package.sh" >/tmp/mam_package_unknown.log 2>&1; then
  echo "unknown package selector unexpectedly passed" >&2
  exit 1
fi
[[ "$(find "$ROOT_DIR/dist" -maxdepth 1 -type f | wc -l | tr -d ' ')" == "$artifact_count" ]]
if PYTHON=definitely-missing-python PACKAGE_FORMAT=zip PACKAGE_ZTR=0 "$ROOT_DIR/package.sh" >/tmp/mam_package_no_python.log 2>&1; then
  echo "missing Python zip backend unexpectedly passed" >&2
  exit 1
fi
[[ "$(find "$ROOT_DIR/dist" -maxdepth 1 -type f | wc -l | tr -d ' ')" == "$artifact_count" ]]
if TAR=definitely-missing-tar PACKAGE_FORMAT=tar PACKAGE_ZTR=0 "$ROOT_DIR/package.sh" >/tmp/mam_package_no_tar.log 2>&1; then
  echo "missing tar backend unexpectedly passed" >&2
  exit 1
fi
[[ "$(find "$ROOT_DIR/dist" -maxdepth 1 -type f | wc -l | tr -d ' ')" == "$artifact_count" ]]
echo "PASS forced stdlib ZIP/tar packages, exclusions, tripwire, and selector fail-closed behavior"

# The tracked source was only read; all live-config mutation occurred in ROOT_DIR under TMP_ROOT.
if [[ -e "$CANONICAL_ROOT/nitpicker/nitpicker.config.json" ]]; then
  canonical_state="$(tree_digest "$CANONICAL_ROOT/nitpicker/nitpicker.config.json")"
else
  canonical_state=ABSENT
fi
[[ "$canonical_state" == "ABSENT" || -n "$canonical_state" ]]
echo "PASS package/install smoke used an isolated source copy only"
