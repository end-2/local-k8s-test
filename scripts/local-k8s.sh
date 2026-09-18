#!/bin/sh
set -eu
umask 077

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
# shellcheck source-path=SCRIPTDIR
# shellcheck source=../config/versions.env
. "$ROOT/config/versions.env"
BIN_DIR=${LOCAL_K8S_BIN_DIR:-$ROOT/.bin}
PATH="$BIN_DIR:$PATH"
export PATH

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
require() { command -v "$1" >/dev/null 2>&1 || die "Missing $1. Run $ROOT/scripts/local-k8s.sh install or add it to PATH."; }

usage() {
    cat <<'EOF'
Usage: ./scripts/local-k8s.sh COMMAND [ARGS]

  install              Download pinned kind and kubectl into .bin
  doctor               Check tools, Docker, and NVIDIA GPU access
  up                   Create or reuse the cluster with NVIDIA GPU support
  test                 Run a CUDA Job requesting one GPU
  down                 Delete this cluster and its kubeconfig
  status               Show nodes, pods, and allocatable GPUs
  kubeconfig           Print the isolated kubeconfig path
  kubectl ARGS...      Run kubectl against this cluster
  load-image IMAGE...  Load images from the selected runtime into the nodes
  load-archive TAR...  Load saved container image archives into the nodes
  logs [DIRECTORY]    Export kind logs

Environment:
  CLUSTER_NAME                 Default: local-k8s
  KIND_CONFIG                  Default: config/kind.yaml in this repository
  KIND_EXPERIMENTAL_PROVIDER    docker (default); auto also selects Docker
  KIND_NODE_IMAGE               Default: pinned in config/versions.env
  WAIT_TIMEOUT                 Default: 180s per readiness check
  LOCAL_K8S_STATE_DIR           Default: .local-k8s in this repository
  LOCAL_K8S_BIN_DIR             Default: .bin in this repository
  LOCAL_K8S_MODELS_DIR          Default: .models, mounted read-only at /models on the GPU node
EOF
}

command=${1:-help}
[ "$#" -eq 0 ] || shift
case $command in
    help|-h|--help) usage; exit 0 ;;
    install) exec sh "$ROOT/scripts/install-tools.sh" "$@" ;;
    doctor|up|test|down|status|kubeconfig) [ "$#" -eq 0 ] || die "$command takes no arguments." ;;
    kubectl|load-image|load-archive) [ "$#" -gt 0 ] || die "$command requires arguments." ;;
    logs) [ "$#" -le 1 ] || die "logs accepts one output directory." ;;
    *) usage >&2; die "Unknown command: $command" ;;
esac

CLUSTER_NAME=${CLUSTER_NAME:-local-k8s}
case $CLUSTER_NAME in
    *[!a-z0-9-]*|''|-*|*-) die "CLUSTER_NAME must contain lowercase letters, digits, or interior hyphens." ;;
esac
[ "${#CLUSTER_NAME}" -le 63 ] || die "CLUSTER_NAME must be at most 63 characters."
STATE_ROOT=${LOCAL_K8S_STATE_DIR:-$ROOT/.local-k8s}
case $STATE_ROOT in /*) ;; *) STATE_ROOT="$PWD/$STATE_ROOT" ;; esac
STATE_DIR="$STATE_ROOT/$CLUSTER_NAME"
KIND_CONFIG=${KIND_CONFIG:-$ROOT/config/kind.yaml}
WAIT_TIMEOUT=${WAIT_TIMEOUT:-180s}
# All kind and kubectl commands, including deletion, use only this kubeconfig.
KUBECONFIG="$STATE_DIR/kubeconfig"
export CLUSTER_NAME KUBECONFIG

select_provider() {
    saved_provider=
    if [ -f "$STATE_DIR/provider" ]; then
        saved_provider=$(cat "$STATE_DIR/provider")
    fi
    case ${KIND_EXPERIMENTAL_PROVIDER:-docker} in
        docker|auto) provider=docker ;;
        *) die "NVIDIA GPU support requires Docker. KIND_EXPERIMENTAL_PROVIDER must be docker or auto." ;;
    esac
    if [ -n "$saved_provider" ] && [ "$saved_provider" != "$provider" ]; then
        die "Cluster state uses $saved_provider; this script requires Docker."
    fi
    command -v "$provider" >/dev/null 2>&1 || die "Missing container runtime CLI: $provider"
    "$provider" info >/dev/null || die "Cannot access $provider. Check its service, VM, connection, and user permissions."
    KIND_EXPERIMENTAL_PROVIDER=$provider
    export KIND_EXPERIMENTAL_PROVIDER
}

cluster_exists() {
    # A failed runtime query must not be mistaken for an absent cluster.
    clusters=$(kind get clusters) || die "Failed to list kind clusters."
    printf '%s\n' "$clusters" | grep -Fxq "$CLUSTER_NAME"
}

require_cluster() {
    cluster_exists || die "Cluster $CLUSTER_NAME does not exist. Run up first."
}

require_kubeconfig() {
    [ -s "$KUBECONFIG" ] || die "Missing kubeconfig. Run up to create or recover $KUBECONFIG."
}

kube() {
    kubectl --kubeconfig "$KUBECONFIG" --context "kind-$CLUSTER_NAME" "$@"
}

# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/nvidia.sh
. "$ROOT/scripts/lib/nvidia.sh"

prepare_models_dir() {
    LOCAL_K8S_MODELS_DIR=${LOCAL_K8S_MODELS_DIR:-$ROOT/.models}
    case $LOCAL_K8S_MODELS_DIR in *:*) die "LOCAL_K8S_MODELS_DIR must not contain a colon." ;; esac
    (umask 022; mkdir -p "$LOCAL_K8S_MODELS_DIR")
    LOCAL_K8S_MODELS_DIR=$(CDPATH='' cd -- "$LOCAL_K8S_MODELS_DIR" && pwd)
    export LOCAL_K8S_MODELS_DIR
}

check_models_mount() {
    mounted_models=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/models"}}{{.Source}}:{{.RW}}{{end}}{{end}}' "$GPU_NODE")
    [ "$mounted_models" = "$LOCAL_K8S_MODELS_DIR:false" ] || \
        die "Cluster $CLUSTER_NAME needs a read-only mount from $LOCAL_K8S_MODELS_DIR to /models. Back up node data, then run down and up. See docs/models.md."
}

load_images() {
    # Docker's containerd store can export indexes with missing platform blobs.
    if [ "$provider" = docker ] &&
        docker info --format '{{.DriverStatus}}' | grep -q 'io.containerd.snapshotter.v1' &&
        docker image save --help | grep -q -- '--platform'; then
        architecture=$(docker info --format '{{.Architecture}}')
        case $architecture in
            x86_64|amd64) platform=linux/amd64 ;;
            aarch64|arm64) platform=linux/arm64 ;;
            *) die "Unsupported Docker server architecture: $architecture" ;;
        esac
        image_dir=$(mktemp -d "${TMPDIR:-/tmp}/local-k8s-images.XXXXXX")
        trap 'rm -rf "$image_dir"' 0
        trap 'exit 130' INT
        trap 'exit 143' TERM
        docker image save --platform "$platform" --output "$image_dir/images.tar" -- "$@"
        kind load image-archive --name "$CLUSTER_NAME" -- "$image_dir/images.tar"
    else
        kind load docker-image --name "$CLUSTER_NAME" -- "$@"
    fi
}

case $command in
    kubeconfig)
        require_kubeconfig
        printf '%s\n' "$KUBECONFIG"
        exit 0
        ;;
    kubectl|status|test)
        require kubectl
        require_kubeconfig
        if [ "$command" = kubectl ]; then
            kube "$@"
        elif [ "$command" = test ]; then
            test_gpu
        else
            kube get nodes -o wide
            kube get pods -A
            kube get nodes '-o=custom-columns=NAME:.metadata.name,GPU:.status.allocatable.nvidia\.com/gpu'
        fi
        exit 0
        ;;
esac

require kind
select_provider
case $command in
    doctor)
        require kubectl
        kind version
        kubectl version --client=true
        printf 'Runtime: %s\nCluster: %s\nConfig: %s\nKubeconfig: %s\n' \
            "$provider" "$CLUSTER_NAME" "$KIND_CONFIG" "$KUBECONFIG"
        [ -r "$KIND_CONFIG" ] || die "Cannot read config: $KIND_CONFIG"
        check_gpu_host
        ;;
    up)
        require kubectl
        [ -r "$KIND_CONFIG" ] || die "Cannot read config: $KIND_CONFIG"
        check_gpu_host
        prepare_models_dir
        exists=false
        if cluster_exists; then
            exists=true
            find_gpu_node
            check_models_mount
        fi
        mkdir -p "$STATE_DIR"
        printf '%s\n' "$provider" > "$STATE_DIR/provider"
        if [ "$exists" = true ]; then
            printf 'Reusing cluster %s; creation settings are not reapplied.\n' "$CLUSTER_NAME"
            kind export kubeconfig --name "$CLUSTER_NAME" --kubeconfig "$KUBECONFIG"
        else
            LOCAL_K8S_DOCKER=$(command -v docker)
            export LOCAL_K8S_DOCKER
            if ! PATH="$ROOT/scripts/lib/gpu-docker:$PATH" kind create cluster --name "$CLUSTER_NAME" --config "$KIND_CONFIG" \
                --image "$KIND_NODE_IMAGE" --kubeconfig "$KUBECONFIG" --wait "$WAIT_TIMEOUT" --retain; then
                die "Cluster creation failed. Use logs to inspect retained nodes, then down before retrying."
            fi
            find_gpu_node
            check_models_mount
        fi
        chmod 600 "$KUBECONFIG"
        kube wait --for=condition=Ready nodes --all --timeout="$WAIT_TIMEOUT"
        kube -n kube-system rollout status deployment/coredns --timeout="$WAIT_TIMEOUT"
        setup_gpu
        printf 'Models: %s -> %s:/models (read-only)\n' "$LOCAL_K8S_MODELS_DIR" "$GPU_NODE"
        printf 'Cluster %s is ready. Kubeconfig: %s\n' "$CLUSTER_NAME" "$KUBECONFIG"
        ;;
    down)
        kind delete cluster --name "$CLUSTER_NAME" --kubeconfig "$KUBECONFIG"
        rm -f "$KUBECONFIG" "$STATE_DIR/provider"
        printf 'Deleted cluster %s. Node data and local persistent volumes are removed; host model files are preserved.\n' "$CLUSTER_NAME"
        ;;
    load-image)
        require_cluster
        load_images "$@"
        ;;
    load-archive)
        require_cluster
        for archive in "$@"; do
            [ -r "$archive" ] || die "Cannot read image archive: $archive"
        done
        for archive in "$@"; do
            kind load image-archive --name "$CLUSTER_NAME" -- "$archive"
        done
        ;;
    logs)
        require_cluster
        kind export logs --name "$CLUSTER_NAME" "${1:-$STATE_DIR/logs}"
        ;;
esac
