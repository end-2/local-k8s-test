#!/bin/sh
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
sandbox=$(mktemp -d "${TMPDIR:-/tmp}/model-download-test.XXXXXX")
trap 'rm -rf "$sandbox"' 0
trap 'exit 130' INT
trap 'exit 143' TERM
project="$sandbox/project with spaces"
mkdir -p "$project/scripts" "$project/config/models" "$sandbox/bin" "$sandbox/fixtures"
cp "$ROOT/scripts/download-model.sh" "$project/scripts/"
cp "$ROOT/config/models/qwen3-0.6b.env" "$project/config/models/"
printf '{"model_type":"qwen3"}\n' > "$sandbox/fixtures/config.json"
printf 'model weight fixture\n' > "$sandbox/fixtures/model.safetensors"
(cd "$sandbox/fixtures" && sha256sum config.json model.safetensors) > "$project/config/models/qwen3-0.6b.sha256"
export MOCK_DOWNLOAD_ROOT="$sandbox"
export LOCAL_K8S_MODELS_DIR='models with spaces'
PATH="$sandbox/bin:$PATH"
export PATH
cat > "$sandbox/bin/curl" <<'EOF'
#!/bin/sh
set -eu
output=
previous=
for argument in "$@"; do
    if [ "$previous" = --output ]; then output=$argument; fi
    previous=$argument
done
file=${argument##*/}
printf '%s\n' "$file" >> "$MOCK_DOWNLOAD_ROOT/calls"
[ ! -f "$MOCK_DOWNLOAD_ROOT/offline" ] || exit 7
if [ "$file" = model.safetensors ] && [ -f "$MOCK_DOWNLOAD_ROOT/interrupt" ]; then
    printf 'partial weights' > "$output"
    exit 18
fi
if [ -f "$MOCK_DOWNLOAD_ROOT/corrupt" ]; then
    printf 'invalid content' > "$output"
else
    cp "$MOCK_DOWNLOAD_ROOT/fixtures/$file" "$output"
fi
EOF
chmod +x "$sandbox/bin/curl"

fail() { printf 'FAIL: %s\n' "$*" >&2; cat "$sandbox/output" >&2; exit 1; }
run() { sh "$project/scripts/download-model.sh" "$@" > "$sandbox/output" 2>&1; }
reject() { if run "$@"; then fail 'Unexpected success'; fi; }
cd "$sandbox"
model_dir="$sandbox/$LOCAL_K8S_MODELS_DIR/Qwen3-0.6B"
staging="$sandbox/$LOCAL_K8S_MODELS_DIR/.Qwen3-0.6B.partial"
lock="$sandbox/$LOCAL_K8S_MODELS_DIR/.Qwen3-0.6B.lock"

reject extra-argument
LOCAL_K8S_MODELS_DIR='invalid:models' reject
touch "$sandbox/interrupt"
reject
[ ! -e "$model_dir" ] || fail 'Partial download was published'
[ -f "$staging/config.json" ] || fail 'Verified file was not retained'
[ -f "$staging/model.safetensors.part" ] || fail 'Partial file was not retained'
[ ! -e "$lock" ] || fail 'Failed download left its lock'
rm "$sandbox/interrupt"

touch "$sandbox/corrupt"
reject
[ ! -e "$model_dir" ] || fail 'Invalid checksum was published'
[ ! -e "$staging/model.safetensors.part" ] || fail 'Corrupt partial file was retained'
rm "$sandbox/corrupt"
cp "$sandbox/fixtures/model.safetensors" "$staging/model.safetensors.part"
touch "$sandbox/offline"
run || fail 'Completed partial file could not be recovered offline'
[ -f "$model_dir/SHA256SUMS" ] || fail 'Checksum manifest missing'
[ -s "$model_dir/REVISION" ] || fail 'Revision metadata missing'
[ ! -e "$staging" ] || fail 'Staging directory was not moved'
[ "$(grep -c '^config.json$' "$sandbox/calls")" -eq 1 ] || fail 'Verified file was downloaded again'

calls=$(wc -l < "$sandbox/calls")
run || fail 'Offline verification failed'
[ "$(wc -l < "$sandbox/calls")" -eq "$calls" ] || fail 'Existing model caused a download'
printf 'damaged weights' > "$model_dir/model.safetensors"
reject
grep -q 'differ from the pinned revision' "$sandbox/output" || fail 'Existing corruption was not reported'
mkdir "$lock"
reject
grep -q 'Another download holds' "$sandbox/output" || fail 'Concurrent download was not rejected'
rmdir "$lock"
printf 'PASS: interrupted downloads, integrity checks, atomic publication, offline reuse, and download locking\n'
