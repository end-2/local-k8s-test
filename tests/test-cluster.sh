#!/bin/sh
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
SCRIPT="$ROOT/scripts/local-k8s.sh"
test_dir=$(mktemp -d "${TMPDIR:-/tmp}/local-k8s-smoke.XXXXXX")
export LOCAL_K8S_STATE_DIR="$test_dir/state"
export LOCAL_K8S_MODELS_DIR="$test_dir/models"
mkdir -m 755 "$LOCAL_K8S_MODELS_DIR"
printf 'model mount fixture\n' > "$LOCAL_K8S_MODELS_DIR/mount-check"
CLUSTER_NAME="local-k8s-smoke-$(date +%s)-$$"
export CLUSTER_NAME
SMOKE_IMAGE=${SMOKE_IMAGE:-docker.io/library/busybox:1.37.0}

cleanup() {
    result=$?
    trap - 0
    if [ "$result" -ne 0 ]; then
        "$SCRIPT" logs "$test_dir/logs" || true
    fi
    if "$SCRIPT" down; then
        if [ "$result" -eq 0 ]; then
            rm -rf "$test_dir"
        else
            printf 'Test diagnostics: %s\n' "$test_dir" >&2
        fi
    else
        printf 'Cleanup failed. Retry with CLUSTER_NAME=%s LOCAL_K8S_STATE_DIR=%s\n' \
            "$CLUSTER_NAME" "$LOCAL_K8S_STATE_DIR" >&2
        result=1
    fi
    exit "$result"
}
trap cleanup 0
trap 'exit 130' INT
trap 'exit 143' TERM

"$SCRIPT" up
"$SCRIPT" up
"$SCRIPT" status
"$SCRIPT" test
gpu_capacity=$("$SCRIPT" kubectl get nodes -o 'jsonpath={range .items[*]}{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}')
advertised=$(printf '%s\n' "$gpu_capacity" | awk '{ count += $1 } END { print count+0 }')
physical=$(nvidia-smi --query-gpu=uuid --format=csv,noheader | awk 'NF { count++ } END { print count+0 }')
[ "$advertised" -eq "$physical" ] || { printf 'GPU capacity does not match physical devices\n' >&2; exit 1; }
provider=$(cat "$LOCAL_K8S_STATE_DIR/$CLUSTER_NAME/provider")
"$provider" pull "$SMOKE_IMAGE"
"$SCRIPT" load-image "$SMOKE_IMAGE"
"$SCRIPT" kubectl run dns-check --image="$SMOKE_IMAGE" --image-pull-policy=Never \
    --restart=Never --overrides='{"spec":{"nodeSelector":{"local-k8s.nvidia-gpu":"true"},"containers":[{"name":"dns-check","volumeMounts":[{"name":"models","mountPath":"/models","readOnly":true}]}],"volumes":[{"name":"models","hostPath":{"path":"/models","type":"Directory"}}]}}' --override-type=strategic \
    --command -- sh -ec 'test ! -e /dev/nvidiactl; test ! -e /dev/nvidia0; grep -qx "model mount fixture" /models/mount-check; if touch /models/write-check 2>/dev/null; then exit 1; fi; nslookup kubernetes.default.svc.cluster.local'
"$SCRIPT" kubectl wait --for=jsonpath='{.status.phase}'=Succeeded pod/dns-check --timeout=120s
"$SCRIPT" kubectl logs dns-check
printf 'PASS: real cluster readiness, reuse, model mount, CUDA, GPU capacity and isolation, image loading, and DNS\n'
