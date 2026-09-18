# shellcheck shell=sh
# Sourced by local-k8s.sh; uses its scoped kubeconfig and cluster name.

check_gpu_host() {
    [ "$(uname -s)" = Linux ] || die "NVIDIA GPU support requires a local Linux host."
    for tool in nvidia-smi nvidia-ctk nvidia-cdi-hook nvidia-container-runtime; do
        command -v "$tool" >/dev/null 2>&1 || die "Missing $tool; see docs/gpu.md."
    done
    security=$(docker info --format '{{json .SecurityOptions}}')
    case $security in *rootless*) die "NVIDIA GPU support requires rootful Docker." ;; esac
    host_gpus=$(nvidia-smi --query-gpu=uuid --format=csv,noheader)
    [ -n "$host_gpus" ] || die "No NVIDIA GPUs found on the host."
    nvidia-ctk cdi list
    container_gpus=$(docker run --rm --network none --runtime=runc \
        --device=nvidia.com/gpu=all --entrypoint=nvidia-smi "$KIND_NODE_IMAGE" \
        --query-gpu=uuid --format=csv,noheader) || die "Docker CDI GPU access failed. See docs/gpu.md."
    [ "$host_gpus" = "$container_gpus" ] || die "Docker must expose the same GPUs as the local host."
    nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv
}

find_gpu_node() {
    nodes=$(kind get nodes --name "$CLUSTER_NAME") || die "Failed to list cluster nodes."
    GPU_NODE=
    for node in $nodes; do
        label=$(docker inspect --format '{{index .Config.Labels "local-k8s.nvidia-gpu"}}' "$node")
        if [ "$label" = true ]; then
            [ -z "$GPU_NODE" ] || die "Only one node may expose the host GPUs; check KIND_CONFIG."
            GPU_NODE=$node
        fi
    done
    [ -n "$GPU_NODE" ] || die "Cluster $CLUSTER_NAME has no GPU access. Back up any node data, then run down and up with the documented kind configuration."
}

configure_gpu_node() {
    docker exec "$GPU_NODE" nvidia-smi -L
    if docker exec "$GPU_NODE" test -f /etc/local-k8s-gpu.ready; then
        return
    fi
    # Copy the host's toolkit to match its driver without installing packages in the node.
    for tool in nvidia-ctk nvidia-cdi-hook nvidia-container-runtime; do
        docker cp -L "$(command -v "$tool")" "$GPU_NODE:/usr/local/bin/$tool"
    done
    docker exec "$GPU_NODE" mkdir -p /etc/cdi /etc/nvidia-container-runtime
    docker exec -i "$GPU_NODE" sh -c 'cat > /etc/nvidia-container-runtime/config.toml' <<'EOF'
[nvidia-container-runtime]
mode = "cdi"
runtimes = ["runc"]
[nvidia-container-runtime.modes.cdi]
default-kind = "nvidia.com/gpu"
spec-dirs = ["/etc/cdi"]
EOF
    docker exec "$GPU_NODE" nvidia-ctk --quiet cdi generate --output=/etc/cdi/nvidia.yaml \
        --nvidia-cdi-hook-path=/usr/local/bin/nvidia-cdi-hook
    docker exec "$GPU_NODE" touch /etc/local-k8s-gpu.ready
}

setup_gpu() {
    labeled_nodes=$(kube get nodes -l local-k8s.nvidia-gpu=true -o name)
    [ "$labeled_nodes" = "node/$GPU_NODE" ] || die "Exactly the GPU-connected node must have the local-k8s.nvidia-gpu=true label."
    configure_gpu_node
    kube apply -f "$ROOT/config/platform/gpu/nvidia-device-plugin.yaml"
    kube -n kube-system rollout status daemonset/nvidia-device-plugin --timeout="$WAIT_TIMEOUT"
    devices=$(docker exec "$GPU_NODE" nvidia-smi --query-gpu=uuid --format=csv,noheader)
    count=$(printf '%s\n' "$devices" | awk 'NF { count++ } END { print count+0 }')
    [ "$count" -gt 0 ] || die "No NVIDIA GPUs found inside node $GPU_NODE."
    kube wait '--for=jsonpath={.status.allocatable.nvidia\.com/gpu}='"$count" \
        "node/$GPU_NODE" --timeout="$WAIT_TIMEOUT"
    printf 'NVIDIA GPUs: %s on %s\n' "$count" "$GPU_NODE"
}

test_gpu() {
    job=$(kube create -f "$ROOT/config/platform/gpu/smoke-test.yaml" -o name)
    if ! kube wait --for=condition=Complete "$job" --timeout="$WAIT_TIMEOUT"; then
        kube describe "$job" >&2 || true
        kube logs "$job" >&2 || true
        die "CUDA test failed: $job"
    fi
    kube logs "$job"
}
