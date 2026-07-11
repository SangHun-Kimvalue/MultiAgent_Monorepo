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

blocked = [name for name in names if name.endswith("nitpicker/nitpicker.config.json")]
if blocked:
    raise SystemExit("live nitpicker config leaked into package: " + ", ".join(blocked))
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
echo "PACKAGE_ARTIFACT=$artifact"
fake_bin="$TMP_ROOT/fake-bin"
install_fake_zip "$fake_bin"
zip_package_output="$(PATH="$fake_bin:$PATH" PACKAGE_ZTR=0 "$ROOT_DIR/package.sh")"
echo "$zip_package_output" >/tmp/mam_package_e_zip.log
zip_artifact="$(printf "%s\n" "$zip_package_output" | tail -n 1)"
assert_package_excludes_live_config "$zip_artifact"
echo "PACKAGE_ZIP_ARTIFACT=$zip_artifact"
echo "PASS e package excludes nitpicker.config.json from tar and zip paths"
