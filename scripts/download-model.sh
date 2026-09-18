#!/bin/sh
set -eu
umask 022

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
# shellcheck source-path=SCRIPTDIR
# shellcheck source=../config/models/qwen3-0.6b.env
. "$ROOT/config/models/qwen3-0.6b.env"

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
[ "$#" -eq 0 ] || die "Usage: $0 (LOCAL_K8S_MODELS_DIR overrides .models)"
for tool in curl sha256sum; do
    command -v "$tool" >/dev/null 2>&1 || die "Missing $tool."
done

models_dir=${LOCAL_K8S_MODELS_DIR:-$ROOT/.models}
case $models_dir in *:*) die "LOCAL_K8S_MODELS_DIR must not contain a colon." ;; esac
mkdir -p "$models_dir"
models_dir=$(CDPATH='' cd -- "$models_dir" && pwd)
model_dir="$models_dir/$MODEL_DIRECTORY"
checksums="$ROOT/config/models/qwen3-0.6b.sha256"

# Keep incomplete downloads separate from the directory mounted by workloads.
lock="$models_dir/.$MODEL_DIRECTORY.lock"
mkdir "$lock" 2>/dev/null || die "Another download holds $lock. Remove it only if no download is running."
trap 'rmdir "$lock"' 0
trap 'exit 130' INT
trap 'exit 143' TERM
if [ -d "$model_dir" ]; then
    (cd "$model_dir" && sha256sum -c "$checksums") || die "Model files differ from the pinned revision. Move $model_dir aside and retry."
    printf 'Model verified: %s\n' "$model_dir"
    exit 0
fi

staging="$models_dir/.$MODEL_DIRECTORY.partial"
mkdir -p "$staging"
while read -r expected file; do
    if [ -f "$staging/$file" ]; then
        actual=$(sha256sum "$staging/$file")
        [ "${actual%% *}" != "$expected" ] || continue
    fi
    if [ -f "$staging/$file.part" ]; then
        actual=$(sha256sum "$staging/$file.part")
        if [ "${actual%% *}" = "$expected" ]; then
            mv "$staging/$file.part" "$staging/$file"
            continue
        fi
    fi
    printf 'Downloading %s at %s: %s\n' "$MODEL_ID" "$MODEL_REVISION" "$file"
    curl --fail --location --silent --show-error --retry 3 \
        --connect-timeout 15 --max-time 3600 --continue-at - \
        --output "$staging/$file.part" \
        "https://huggingface.co/$MODEL_ID/resolve/$MODEL_REVISION/$file"
    actual=$(sha256sum "$staging/$file.part")
    if [ "${actual%% *}" != "$expected" ]; then
        rm -f "$staging/$file.part"
        die "Checksum mismatch: $file. Retry the download."
    fi
    mv "$staging/$file.part" "$staging/$file"
done < "$checksums"
cp "$checksums" "$staging/SHA256SUMS"
printf '%s\n' "$MODEL_ID@$MODEL_REVISION" > "$staging/REVISION"
mv "$staging" "$model_dir"
printf 'Model ready: %s\n' "$model_dir"
