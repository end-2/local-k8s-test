#!/bin/sh
set -eu
umask 077

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
Usage: ./scripts/load-benchmark-images.sh

Environment:
  AIPERF_IMAGE_TAG  Image tag for the AIPerf benchmark image (default: 0.12.0)
  TMPDIR            Temporary directory for the image archive.
                    Default: .local-k8s/image-tmp in this repository.

Examples:
  ./scripts/load-benchmark-images.sh
  AIPERF_IMAGE_TAG=0.12.0 ./scripts/load-benchmark-images.sh
EOF
}

case ${1:-} in
    -h|--help|help) usage; exit 0 ;;
esac
[ "$#" -eq 0 ] || { usage >&2; die "load-benchmark-images.sh takes no arguments."; }

AIPERF_IMAGE_TAG=${AIPERF_IMAGE_TAG:-0.12.0}
case $AIPERF_IMAGE_TAG in
    ''|*[!a-zA-Z0-9_.-]*|.*|*-) die "AIPERF_IMAGE_TAG must be a valid docker tag." ;;
esac

image="local/aiperf:$AIPERF_IMAGE_TAG"

command -v docker >/dev/null 2>&1 || die "Missing docker."
docker image inspect "$image" >/dev/null 2>&1 || \
    die "Missing local image: $image. Build it first with ./scripts/build-benchmark-images.sh."

# local-k8s.sh load-image stages a temporary archive under TMPDIR.
case ${TMPDIR:-} in
    '') TMPDIR="$ROOT/.local-k8s/image-tmp" ;;
    *) case $TMPDIR in *:*) die "TMPDIR must not contain a colon." ;; esac ;;
esac
(umask 022; mkdir -p "$TMPDIR")
export TMPDIR

exec "$ROOT/scripts/local-k8s.sh" load-image "$image"
