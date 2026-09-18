#!/bin/sh
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
TEST_SHELL=${TEST_SHELL:-sh}
sandbox=$(mktemp -d "${TMPDIR:-/tmp}/local-k8s-install-test.XXXXXX")
trap 'rm -rf "$sandbox"' 0
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -p "$sandbox/mocks" "$sandbox/downloads"
export MOCK_ROOT="$sandbox/downloads" MOCK_TRACE="$sandbox/trace"
export LOCAL_K8S_BIN_DIR="$sandbox/installed tools"
unset KIND_VERSION KUBECTL_VERSION
# shellcheck source-path=SCRIPTDIR
# shellcheck source=../config/versions.env
. "$ROOT/config/versions.env"
printf 'kind binary\n' > "$MOCK_ROOT/kind"
printf 'kubectl binary\n' > "$MOCK_ROOT/kubectl"
for binary in kind kubectl; do
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$MOCK_ROOT/$binary" > "$MOCK_ROOT/$binary.sha256"
    else
        shasum -a 256 "$MOCK_ROOT/$binary" > "$MOCK_ROOT/$binary.sha256"
    fi
done
cat > "$sandbox/mocks/uname" <<'EOF'
#!/bin/sh
case $1 in
    -s) printf '%s\n' "$MOCK_OS" ;;
    -m) printf '%s\n' "$MOCK_ARCH" ;;
esac
EOF
cat > "$sandbox/mocks/curl" <<'EOF'
#!/bin/sh
set -eu
output=
previous=
for arg in "$@"; do
    if [ "$previous" = --output ]; then output=$arg; fi
    previous=$arg
done
url=$arg
printf '%s\n' "$url" >> "$MOCK_TRACE"
case $url in
    *kind-*.sha256sum) file=kind.sha256 ;;
    *kind-*) file=kind ;;
    */kubectl.sha256) file=kubectl.sha256 ;;
    */kubectl) file=kubectl ;;
    *) exit 2 ;;
esac
[ ! -f "$MOCK_ROOT/fail-download" ] || exit 22
if [ -f "$MOCK_ROOT/bad-checksum" ] && [ "$file" = kubectl.sha256 ]; then
    printf '%064d\n' 0 > "$output"
else
    cp "$MOCK_ROOT/$file" "$output"
fi
EOF
chmod +x "$sandbox/mocks/"*
PATH="$sandbox/mocks:$PATH"
export PATH

fail() { printf 'FAIL: %s\n' "$*" >&2; cat "$sandbox/output" >&2; exit 1; }
install_tools() { "$TEST_SHELL" "$ROOT/scripts/install-tools.sh" > "$sandbox/output" 2>&1; }

for os in Linux Darwin; do
    for arch in x86_64 aarch64 arm64; do
        export MOCK_OS=$os MOCK_ARCH=$arch
        install_tools || fail "Install failed for $os/$arch"
        case $os in Linux) expected_os=linux ;; Darwin) expected_os=darwin ;; esac
        case $arch in x86_64) expected_arch=amd64 ;; *) expected_arch=arm64 ;; esac
        grep -Fq "/$KIND_VERSION/kind-$expected_os-$expected_arch" "$MOCK_TRACE" || fail 'Wrong kind asset'
        grep -Fq "/$KUBECTL_VERSION/bin/$expected_os/$expected_arch/kubectl" "$MOCK_TRACE" || fail 'Wrong kubectl asset'
        cmp "$LOCAL_K8S_BIN_DIR/kind" "$MOCK_ROOT/kind" || fail 'Wrong kind binary'
        cmp "$LOCAL_K8S_BIN_DIR/kubectl" "$MOCK_ROOT/kubectl" || fail 'Wrong kubectl binary'
        [ -x "$LOCAL_K8S_BIN_DIR/kind" ] || fail 'kind not executable'
        [ -x "$LOCAL_K8S_BIN_DIR/kubectl" ] || fail 'kubectl not executable'
    done
done

printf 'existing kind\n' > "$LOCAL_K8S_BIN_DIR/kind"
printf 'existing kubectl\n' > "$LOCAL_K8S_BIN_DIR/kubectl"
touch "$MOCK_ROOT/bad-checksum"
if install_tools; then fail 'Accepted a bad checksum'; fi
grep -Fq 'Checksum mismatch' "$sandbox/output" || fail 'Missing checksum error'
[ "$(cat "$LOCAL_K8S_BIN_DIR/kind")" = 'existing kind' ] || fail 'Replaced kind on failed verification'
[ "$(cat "$LOCAL_K8S_BIN_DIR/kubectl")" = 'existing kubectl' ] || fail 'Replaced kubectl on failed verification'
rm "$MOCK_ROOT/bad-checksum"
touch "$MOCK_ROOT/fail-download"
if install_tools; then fail 'Accepted a failed download'; fi
[ "$(cat "$LOCAL_K8S_BIN_DIR/kind")" = 'existing kind' ] || fail 'Replaced kind on failed download'
rm "$MOCK_ROOT/fail-download"
MOCK_OS=Windows
if install_tools; then fail 'Accepted an unsupported operating system'; fi
MOCK_OS=Linux MOCK_ARCH=riscv64
if install_tools; then fail 'Accepted an unsupported architecture'; fi
for leftover in "$LOCAL_K8S_BIN_DIR"/.install.*; do
    [ ! -e "$leftover" ] || fail 'Temporary downloads were not cleaned up'
done
printf 'PASS: platform selection, checksums, download failures, and cleanup (%s)\n' "$TEST_SHELL"
