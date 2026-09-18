#!/bin/sh
set -eu
umask 077

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
Usage: ./scripts/load-inference-images.sh [VARIANT]

  VARIANT  base, enhanced, or both (default: both)

Environment:
  IMAGE_TAG            Image tag for both variants (default: 0.1.0)
  TMPDIR               Temporary directory for the image archive.
                       Default: .local-k8s/image-tmp in this repository.

Examples:
  ./scripts/load-inference-images.sh
  ./scripts/load-inference-images.sh base
  ./scripts/load-inference-images.sh enhanced
EOF
}

variant=${1:-both}
[ "$#" -eq 0 ] || shift
case $variant in
    -h|--help|help) usage; exit 0 ;;
    base|enhanced|both) ;;
    *) usage >&2; die "Unknown variant: $variant" ;;
esac
[ "$#" -eq 0 ] || { usage >&2; die "load-inference-images.sh takes at most one argument."; }

IMAGE_TAG=${IMAGE_TAG:-0.1.0}
case $IMAGE_TAG in
    ''|*[!a-zA-Z0-9_.-]*|.*|*-) die "IMAGE_TAG must be a valid docker tag." ;;
esac

images=
case $variant in
    base) images="local/transformers-api-base:$IMAGE_TAG" ;;
    enhanced) images="local/transformers-api-enhanced:$IMAGE_TAG" ;;
    both) images="local/transformers-api-base:$IMAGE_TAG local/transformers-api-enhanced:$IMAGE_TAG" ;;
esac

command -v docker >/dev/null 2>&1 || die "Missing docker."
for image in $images; do
    docker image inspect "$image" >/dev/null 2>&1 || \
        die "Missing local image: $image. Build it first with ./scripts/build-inference-images.sh $variant."
done

# local-k8s.sh load-image stages a temporary archive under TMPDIR.
case ${TMPDIR:-} in
    '') TMPDIR="$ROOT/.local-k8s/image-tmp" ;;
    *) case $TMPDIR in *:*) die "TMPDIR must not contain a colon." ;; esac ;;
esac
(umask 022; mkdir -p "$TMPDIR")
export TMPDIR

# shellcheck disable=SC2086
exec "$ROOT/scripts/local-k8s.sh" load-image $images
