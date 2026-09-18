#!/bin/sh
set -eu
umask 077

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
# shellcheck source-path=SCRIPTDIR
# shellcheck source=../config/versions.env
. "$ROOT/config/versions.env"
BIN_DIR=${LOCAL_K8S_BIN_DIR:-$ROOT/.bin}

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

[ "$#" -eq 0 ] || die "Usage: $0"
case $(uname -s) in
    Linux) os=linux ;;
    Darwin) os=darwin ;;
    *) die "Supported systems: Linux, macOS, and Windows via WSL2." ;;
esac
case $(uname -m) in
    x86_64|amd64) arch=amd64 ;;
    aarch64|arm64) arch=arm64 ;;
    *) die "Supported architectures: amd64 and arm64." ;;
esac

if command -v curl >/dev/null 2>&1; then
    downloader=curl
elif command -v wget >/dev/null 2>&1; then
    downloader=wget
else
    die "Install curl or wget, or provide kind and kubectl on PATH."
fi
if command -v sha256sum >/dev/null 2>&1; then
    hasher=sha256sum
elif command -v shasum >/dev/null 2>&1; then
    hasher=shasum
else
    die "Install sha256sum or shasum for checksum verification."
fi

download() {
    if [ "$downloader" = curl ]; then
        curl --fail --location --silent --show-error --retry 3 \
            --connect-timeout 15 --max-time 300 --output "$2" "$1"
    else
        wget -q --timeout=30 --tries=3 -O "$2" "$1"
    fi
}

verify() {
    expected=$(awk 'NR == 1 { print $1 }' "$2")
    case $expected in ''|*[!0-9a-fA-F]*) die "Invalid checksum for $1" ;; esac
    [ "${#expected}" -eq 64 ] || die "Invalid checksum length for $1"
    if [ "$hasher" = sha256sum ]; then
        digest=$(sha256sum "$1")
    else
        digest=$(shasum -a 256 "$1")
    fi
    actual=${digest%% *}
    [ "$actual" = "$expected" ] || die "Checksum mismatch for $1; installed tools were not changed."
}

mkdir -p "$BIN_DIR"
BIN_DIR=$(CDPATH='' cd -- "$BIN_DIR" && pwd)
staging=$(mktemp -d "$BIN_DIR/.install.XXXXXX")
trap 'rm -rf "$staging"' 0
trap 'exit 130' INT
trap 'exit 143' TERM

kind_url="https://github.com/kubernetes-sigs/kind/releases/download/$KIND_VERSION/kind-$os-$arch"
kubectl_url="https://dl.k8s.io/release/$KUBECTL_VERSION/bin/$os/$arch/kubectl"
printf 'Downloading kind %s and kubectl %s for %s/%s...\n' "$KIND_VERSION" "$KUBECTL_VERSION" "$os" "$arch"
download "$kind_url" "$staging/kind"
download "$kind_url.sha256sum" "$staging/kind.sha256"
verify "$staging/kind" "$staging/kind.sha256"
download "$kubectl_url" "$staging/kubectl"
download "$kubectl_url.sha256" "$staging/kubectl.sha256"
verify "$staging/kubectl" "$staging/kubectl.sha256"

# Verify both downloads before replacing any installed executable.
chmod 755 "$staging/kind" "$staging/kubectl"
mv -f "$staging/kind" "$BIN_DIR/kind"
mv -f "$staging/kubectl" "$BIN_DIR/kubectl"
printf 'Installed tools in %s\n' "$BIN_DIR"
