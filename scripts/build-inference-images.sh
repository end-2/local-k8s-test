#!/bin/sh
set -eu
umask 022

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
Usage: ./scripts/build-inference-images.sh [VARIANT] [DOCKER_BUILD_ARGS...]

  VARIANT  base, enhanced, or both (default: both)

Environment:
  IMAGE_TAG  Image tag for both variants (default: 0.1.0)

Examples:
  ./scripts/build-inference-images.sh
  ./scripts/build-inference-images.sh base
  ./scripts/build-inference-images.sh enhanced --no-cache
  IMAGE_TAG=0.2.0 ./scripts/build-inference-images.sh both
EOF
}

variant=${1:-both}
[ "$#" -eq 0 ] || shift
case $variant in
    -h|--help|help) usage; exit 0 ;;
    base|enhanced|both) ;;
    *) usage >&2; die "Unknown variant: $variant" ;;
esac

command -v docker >/dev/null 2>&1 || die "Missing docker."
docker info >/dev/null 2>&1 || die "Cannot access docker. Check its service and user permissions."

IMAGE_TAG=${IMAGE_TAG:-0.1.0}
case $IMAGE_TAG in
    ''|*[!a-zA-Z0-9_.-]*|.*|*-) die "IMAGE_TAG must be a valid docker tag." ;;
esac

build_image() {
    name=$1
    shift
    image="local/transformers-api-$name:$IMAGE_TAG"
    context="$ROOT/inference/transformers-api-$name"
    [ -f "$context/Dockerfile" ] || die "Missing Dockerfile: $context/Dockerfile"
    printf 'Building %s from %s...\n' "$image" "$context"
    # --network=host is used for package downloads during the build.
    docker build --network=host -t "$image" "$@" "$context"
    docker image inspect "$image" --format 'Built {{.RepoTags}} ({{.Id}})\n'
}

case $variant in
    base) build_image base "$@" ;;
    enhanced) build_image enhanced "$@" ;;
    both)
        build_image base "$@"
        build_image enhanced "$@"
        ;;
esac
