#!/bin/sh
set -eu
umask 022

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
Usage: ./scripts/build-benchmark-images.sh [DOCKER_BUILD_ARGS...]

Environment:
  AIPERF_IMAGE_TAG  Image tag for the AIPerf benchmark image (default: 0.12.0)

Examples:
  ./scripts/build-benchmark-images.sh
  ./scripts/build-benchmark-images.sh --no-cache
  AIPERF_IMAGE_TAG=0.12.0 ./scripts/build-benchmark-images.sh
EOF
}

case ${1:-} in
    -h|--help|help) usage; exit 0 ;;
esac

command -v docker >/dev/null 2>&1 || die "Missing docker."
docker info >/dev/null 2>&1 || die "Cannot access docker. Check its service and user permissions."

AIPERF_IMAGE_TAG=${AIPERF_IMAGE_TAG:-0.12.0}
case $AIPERF_IMAGE_TAG in
    ''|*[!a-zA-Z0-9_.-]*|.*|*-) die "AIPERF_IMAGE_TAG must be a valid docker tag." ;;
esac

image="local/aiperf:$AIPERF_IMAGE_TAG"
context="$ROOT/benchmarks/aiperf"
[ -f "$context/Dockerfile" ] || die "Missing Dockerfile: $context/Dockerfile"
printf 'Building %s from %s...\n' "$image" "$context"
# --network=host is used for package downloads during the build.
docker build --network=host -t "$image" "$@" "$context"
docker image inspect "$image" --format 'Built {{.RepoTags}} ({{.Id}})\n'
