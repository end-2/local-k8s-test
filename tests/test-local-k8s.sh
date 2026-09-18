#!/bin/sh
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
TEST_SHELL=${TEST_SHELL:-sh}
sandbox=$(mktemp -d "${TMPDIR:-/tmp}/local-k8s-test.XXXXXX")
trap 'rm -rf "$sandbox"' 0
trap 'exit 130' INT
trap 'exit 143' TERM
project="$sandbox/repository with spaces"
mkdir -p "$project" "$sandbox/mocks" "$sandbox/data"
cp -R "$ROOT/scripts" "$ROOT/config" "$project/"
export MOCK_ROOT="$sandbox/data" MOCK_TRACE="$sandbox/trace"
export LOCAL_K8S_BIN_DIR="$sandbox/mocks" LOCAL_K8S_STATE_DIR="$sandbox/state with spaces"
export KIND_EXPERIMENTAL_PROVIDER=auto
export KUBECONFIG="$sandbox/unrelated-kubeconfig"
unset CLUSTER_NAME KIND_CONFIG KIND_NODE_IMAGE WAIT_TIMEOUT LOCAL_K8S_MODELS_DIR
printf 'unrelated context\n' > "$KUBECONFIG"
: > "$MOCK_TRACE"

cat > "$sandbox/mocks/runtime" <<'EOF'
#!/bin/sh
set -eu
runtime=${0##*/}
{
    printf 'runtime[%s]' "$runtime"
    printf ' <%s>' "$@"
    printf '\n'
} >> "$MOCK_TRACE"
[ ! -f "$MOCK_ROOT/offline-$runtime" ]
case "$*" in
    info) ;;
    'info --format {{json .SecurityOptions}}')
        if [ -f "$MOCK_ROOT/rootless" ]; then printf rootless; else printf '[]'; fi
        ;;
    'run --rm '*)
        [ ! -f "$MOCK_ROOT/fail-cdi" ] || exit 1
        printf '%s\n' "${MOCK_CONTAINER_GPU:-GPU-test}"
        ;;
    'run '*)
        name=
        gpu=false
        model_mount=
        previous=
        for arg in "$@"; do
            if [ "$previous" = --name ]; then name=$arg; fi
            if [ "$arg" = local-k8s.nvidia-gpu=true ]; then gpu=true; fi
            if [ "$previous" = --volume ]; then model_mount=$arg; fi
            previous=$arg
        done
        if [ "$gpu" = true ]; then touch "$MOCK_ROOT/gpu-$name"; fi
        if [ -n "$model_mount" ]; then printf '%s' "${model_mount%:/models:ro}:false" > "$MOCK_ROOT/models-$name"; fi
        ;;
    'inspect '*'.Mounts'*)
        for arg in "$@"; do node=$arg; done
        if [ ! -f "$MOCK_ROOT/missing-models" ]; then cat "$MOCK_ROOT/models-$node"; fi
        ;;
    'inspect '*)
        for arg in "$@"; do node=$arg; done
        if [ ! -f "$MOCK_ROOT/cpu-cluster" ] &&
            { [ -f "$MOCK_ROOT/gpu-$node" ] || [ -f "$MOCK_ROOT/duplicate-gpu" ]; }; then
            printf true
        else
            printf '<no value>'
        fi
        ;;
    'exec '*'/etc/local-k8s-gpu.ready')
        case "$3" in
            test) [ -f "$MOCK_ROOT/configured-$2" ] ;;
            touch) touch "$MOCK_ROOT/configured-$2" ;;
        esac
        ;;
    'exec '*nvidia-smi*) printf 'GPU-test\n' ;;
    'exec -i '*) cat >/dev/null ;;
    'exec '*|cp*) ;;
    'info --format {{.DriverStatus}}')
        if [ -f "$MOCK_ROOT/containerd-store" ]; then printf 'io.containerd.snapshotter.v1\n'; fi
        ;;
    'info --format {{.Architecture}}') printf '%s\n' "${MOCK_RUNTIME_ARCH:-x86_64}" ;;
    'image save --help') printf '%s\n' '--platform' ;;
    'image save '*)
        [ ! -f "$MOCK_ROOT/fail-save" ] || exit 1
        previous=
        for arg in "$@"; do
            if [ "$previous" = --output ]; then printf 'image archive\n' > "$arg"; fi
            previous=$arg
        done
        ;;
    *) exit 2 ;;
esac
EOF
cp "$sandbox/mocks/runtime" "$sandbox/mocks/docker"
cat > "$sandbox/mocks/kind" <<'EOF'
#!/bin/sh
set -eu
{
    printf 'kind[%s] kubeconfig[%s]' "$KIND_EXPERIMENTAL_PROVIDER" "$KUBECONFIG"
    printf ' <%s>' "$@"
    printf '\n'
} >> "$MOCK_TRACE"
name=
config=
previous=
for arg in "$@"; do
    if [ "$previous" = --name ]; then name=$arg; fi
    if [ "$previous" = --config ]; then config=$arg; fi
    previous=$arg
done
clusters="$MOCK_ROOT/clusters-$KIND_EXPERIMENTAL_PROVIDER"
case "$*" in
    version) printf 'kind mock\n' ;;
    'get clusters')
        [ ! -f "$MOCK_ROOT/fail-list" ] || exit 1
        if [ -f "$clusters" ]; then cat "$clusters"; fi
        ;;
    'create cluster '*)
        printf '%s\n' "$name" >> "$clusters"
        [ ! -f "$MOCK_ROOT/fail-create" ] || exit 1
        printf '%s-control-plane\n' "$name" > "$MOCK_ROOT/nodes-$name"
        case $config in
            *kind-multi-node.yaml)
                printf '%s-worker\n%s-worker2\n' "$name" "$name" >> "$MOCK_ROOT/nodes-$name"
                gpu_node="$name-worker"
                ;;
            *) gpu_node="$name-control-plane" ;;
        esac
        printf '%s\n' "$gpu_node" > "$MOCK_ROOT/selected-$name"
        while IFS= read -r node; do
            if [ "$node" = "$gpu_node" ]; then
                docker run --name "$node" --label "io.x-k8s.kind.cluster=$name" \
                    --volume=/dev/null:/var/run/local-k8s/nvidia-gpu:ro kindest/node:test
            else
                docker run --name "$node" --label "io.x-k8s.kind.cluster=$name" kindest/node:test
            fi
        done < "$MOCK_ROOT/nodes-$name"
        printf 'test kubeconfig\n' > "$KUBECONFIG"
        ;;
    'get nodes '*) cat "$MOCK_ROOT/nodes-$name" ;;
    'export kubeconfig '*) printf 'test kubeconfig\n' > "$KUBECONFIG" ;;
    'delete cluster '*)
        [ ! -f "$MOCK_ROOT/fail-delete" ] || exit 1
        if [ -f "$clusters" ]; then
            awk -v name="$name" '$0 != name' "$clusters" > "$clusters.next"
            mv "$clusters.next" "$clusters"
        fi
        if [ -f "$MOCK_ROOT/nodes-$name" ]; then
            while IFS= read -r node; do
                rm -f "$MOCK_ROOT/gpu-$node" "$MOCK_ROOT/configured-$node" "$MOCK_ROOT/models-$node"
            done < "$MOCK_ROOT/nodes-$name"
            rm "$MOCK_ROOT/nodes-$name" "$MOCK_ROOT/selected-$name"
        fi
        ;;
    'load image-archive '*) [ "$#" -eq 6 ] ;;
    'load docker-image '*|'export logs '*) ;;
    *) exit 2 ;;
esac
EOF
cat > "$sandbox/mocks/kubectl" <<'EOF'
#!/bin/sh
set -eu
{
    printf kubectl
    printf ' <%s>' "$@"
    printf '\n'
} >> "$MOCK_TRACE"
[ ! -f "$MOCK_ROOT/fail-ready" ] || exit 1
case "$*" in
    *'get nodes -l local-k8s.nvidia-gpu=true -o name')
        if [ ! -f "$MOCK_ROOT/missing-label" ]; then
            printf 'node/%s\n' "$(cat "$MOCK_ROOT/selected-${4#kind-}")"
        fi
        ;;
    *'create -f '*) printf 'job.batch/gpu-smoke-test\n' ;;
    *'wait --for=condition=Complete '*) [ ! -f "$MOCK_ROOT/fail-job" ] ;;
    *'wait --for=jsonpath='*allocatable*) [ ! -f "$MOCK_ROOT/fail-capacity" ] ;;
esac
EOF
cat > "$sandbox/mocks/nvidia-smi" <<'EOF'
#!/bin/sh
printf 'GPU-test\n'
EOF
cat > "$sandbox/mocks/uname" <<'EOF'
#!/bin/sh
printf '%s\n' "${MOCK_OS:-Linux}"
EOF
for tool in nvidia-ctk nvidia-cdi-hook nvidia-container-runtime; do
    printf '#!/bin/sh\nexit 0\n' > "$sandbox/mocks/$tool"
done
chmod +x "$sandbox/mocks/"*

fail() { printf 'FAIL: %s\n' "$*" >&2; cat "$sandbox/output" >&2; exit 1; }
run() { "$TEST_SHELL" "$project/scripts/local-k8s.sh" "$@" > "$sandbox/output" 2>&1; }
ok() { run "$@" || fail "$*"; }
reject() { if run "$@"; then fail "Unexpected success: $*"; fi; }
contains() { grep -Fq -- "$1" "$2" || fail "Missing expected text: $1"; }

# Run outside the checkout to exercise script-relative config and spaced paths.
cd "$sandbox"
ok help
reject invalid-command
reject up extra-argument
CLUSTER_NAME='../escape' reject down
KIND_EXPERIMENTAL_PROVIDER=invalid reject doctor
KIND_EXPERIMENTAL_PROVIDER=podman reject up
MOCK_OS=Darwin reject doctor
touch "$MOCK_ROOT/rootless"
reject doctor
rm "$MOCK_ROOT/rootless"
touch "$MOCK_ROOT/fail-cdi"
reject up
rm "$MOCK_ROOT/fail-cdi"
MOCK_CONTAINER_GPU=GPU-remote reject up
if grep -q '<create> <cluster>' "$MOCK_TRACE"; then fail 'Failed GPU preflight created a cluster'; fi
ok doctor
ok up
export CLUSTER_NAME=local-k8s
isolated="$LOCAL_K8S_STATE_DIR/$CLUSTER_NAME/kubeconfig"
[ -s "$isolated" ] || fail 'Isolated kubeconfig not created'
contains '<--config> <'"$project"'/config/kind.yaml>' "$MOCK_TRACE"
contains '<--for=condition=Ready> <nodes> <--all>' "$MOCK_TRACE"
contains '<deployment/coredns>' "$MOCK_TRACE"
contains '<--name> <local-k8s>' "$MOCK_TRACE"
contains '<run> <--runtime=runc> <--device=nvidia.com/gpu=all> <--label> <local-k8s.nvidia-gpu=true>' "$MOCK_TRACE"
contains '<--volume> <'"$project"'/.models:/models:ro>' "$MOCK_TRACE"
[ -d "$project/.models" ] || fail 'Model directory was not created'
printf 'persistent model\n' > "$project/.models/test-model"
contains '<--for=jsonpath={.status.allocatable.nvidia\.com/gpu}=1> <node/local-k8s-control-plane>' "$MOCK_TRACE"
[ -f "$MOCK_ROOT/configured-local-k8s-control-plane" ] || fail 'Toolkit configuration did not finish'
copies=$(grep -c '^runtime\[docker\] <cp>' "$MOCK_TRACE")
[ "$copies" -eq 3 ] || fail 'Toolkit executables were not copied'
ok kubeconfig
[ "$(cat "$sandbox/output")" = "$isolated" ] || fail 'Unexpected kubeconfig path'

rm "$isolated"
ok up
[ "$(grep -c '^runtime\[docker\] <cp>' "$MOCK_TRACE")" -eq "$copies" ] || fail 'Reused node was reconfigured'
[ "$(grep -c '<create> <cluster>' "$MOCK_TRACE")" -eq 1 ] || fail 'up recreated an existing cluster'
[ -s "$isolated" ] || fail 'up did not recover kubeconfig'
touch "$MOCK_ROOT/missing-models"
reject up
contains 'needs a read-only mount' "$sandbox/output"
rm "$MOCK_ROOT/missing-models"
LOCAL_K8S_MODELS_DIR="$sandbox/other models" reject up
contains 'needs a read-only mount' "$sandbox/output"
LOCAL_K8S_MODELS_DIR="$sandbox/invalid:models" reject up
contains 'must not contain a colon' "$sandbox/output"
ok status
ok kubectl get pods -l 'app in (a,b)'
contains '<--kubeconfig> <'"$isolated"'> <--context> <kind-local-k8s> <get> <pods> <-l> <app in (a,b)>' "$MOCK_TRACE"
ok test
touch "$MOCK_ROOT/fail-job"
reject test
contains '<describe> <job.batch/gpu-smoke-test>' "$MOCK_TRACE"
rm "$MOCK_ROOT/fail-job"
touch "$MOCK_ROOT/fail-capacity"
reject up
rm "$MOCK_ROOT/fail-capacity"
touch "$MOCK_ROOT/cpu-cluster"
reject up
contains 'has no GPU access' "$sandbox/output"
rm "$MOCK_ROOT/cpu-cluster"
touch "$MOCK_ROOT/missing-label"
reject up
rm "$MOCK_ROOT/missing-label"
ok load-image example:test another:test
contains '<load> <docker-image> <--name> <local-k8s> <--> <example:test> <another:test>' "$MOCK_TRACE"
touch "$sandbox/image one.tar" "$sandbox/image two.tar"
ok load-archive "$sandbox/image one.tar" "$sandbox/image two.tar"
[ "$(grep -c '<load> <image-archive>' "$MOCK_TRACE")" -eq 2 ] || fail 'Archives must load separately'
reject load-archive "$sandbox/missing.tar"
ok logs "$sandbox/log directory"

touch "$MOCK_ROOT/containerd-store"
ok load-image example:test
contains 'runtime[docker] <image> <save> <--platform> <linux/amd64>' "$MOCK_TRACE"
MOCK_RUNTIME_ARCH=aarch64 ok load-image example:test
contains 'runtime[docker] <image> <save> <--platform> <linux/arm64>' "$MOCK_TRACE"
MOCK_RUNTIME_ARCH=riscv64 reject load-image example:test
touch "$MOCK_ROOT/fail-save"
loads=$(grep -c '<load> <image-archive>' "$MOCK_TRACE")
reject load-image example:test
[ "$(grep -c '<load> <image-archive>' "$MOCK_TRACE")" -eq "$loads" ] || fail 'Failed save triggered image loading'
rm "$MOCK_ROOT/containerd-store" "$MOCK_ROOT/fail-save"

touch "$MOCK_ROOT/offline-docker"
reject up
KIND_EXPERIMENTAL_PROVIDER=podman reject down
rm "$MOCK_ROOT/offline-docker"
touch "$MOCK_ROOT/fail-ready"
reject up
rm "$MOCK_ROOT/fail-ready"
touch "$MOCK_ROOT/fail-delete"
reject down
[ -s "$isolated" ] || fail 'Failed deletion removed kubeconfig'
rm "$MOCK_ROOT/fail-delete"
ok down
[ ! -f "$isolated" ] || fail 'down left kubeconfig behind'
[ -f "$project/.models/test-model" ] || fail 'down removed host model files'
ok down
reject status

touch "$MOCK_ROOT/fail-list"
creates=$(grep -c '<create> <cluster>' "$MOCK_TRACE")
reject up
[ "$(grep -c '<create> <cluster>' "$MOCK_TRACE")" -eq "$creates" ] || fail 'List failure triggered cluster creation'
rm "$MOCK_ROOT/fail-list"
touch "$MOCK_ROOT/fail-create"
reject up
contains 'creation failed' "$sandbox/output"
[ ! -f "$isolated" ] || fail 'Failed creation produced kubeconfig'
rm "$MOCK_ROOT/fail-create"
ok down

export LOCAL_K8S_MODELS_DIR='custom models'
KIND_CONFIG="$project/config/kind-multi-node.yaml" ok up
contains 'docker' "$LOCAL_K8S_STATE_DIR/$CLUSTER_NAME/provider"
contains '<--config> <'"$project"'/config/kind-multi-node.yaml>' "$MOCK_TRACE"
contains '<--for=jsonpath={.status.allocatable.nvidia\.com/gpu}=1> <node/local-k8s-worker>' "$MOCK_TRACE"
[ -f "$MOCK_ROOT/gpu-local-k8s-worker" ] || fail 'Multi-node cluster worker has no GPU'
[ ! -f "$MOCK_ROOT/gpu-local-k8s-control-plane" ] || fail 'Multi-node cluster exposes GPU on the control plane'
[ ! -f "$MOCK_ROOT/gpu-local-k8s-worker2" ] || fail 'Multi-node cluster duplicated GPU access'
contains "$sandbox/custom models:false" "$MOCK_ROOT/models-local-k8s-worker"
[ ! -f "$MOCK_ROOT/models-local-k8s-control-plane" ] || fail 'CPU node mounted model files'
[ ! -f "$MOCK_ROOT/models-local-k8s-worker2" ] || fail 'Second worker mounted model files'
ok up
touch "$MOCK_ROOT/duplicate-gpu"
reject up
rm "$MOCK_ROOT/duplicate-gpu"
ok down
touch "$MOCK_ROOT/offline-docker"
reject doctor
rm "$MOCK_ROOT/offline-docker"

# Unmarked nodes and other clusters must preserve the original Docker arguments.
export LOCAL_K8S_DOCKER="$sandbox/mocks/docker"
"$TEST_SHELL" "$project/scripts/lib/gpu-docker/docker" run --name local-k8s-worker \
    --label io.x-k8s.kind.cluster=local-k8s 'image with spaces'
contains '<run> <--name> <local-k8s-worker> <--label> <io.x-k8s.kind.cluster=local-k8s> <image with spaces>' "$MOCK_TRACE"
"$TEST_SHELL" "$project/scripts/lib/gpu-docker/docker" run --name other-worker \
    --label io.x-k8s.kind.cluster=other --volume=/dev/null:/var/run/local-k8s/nvidia-gpu:ro image:test
contains '<run> <--name> <other-worker> <--label> <io.x-k8s.kind.cluster=other> <--volume=/dev/null:/var/run/local-k8s/nvidia-gpu:ro> <image:test>' "$MOCK_TRACE"
"$TEST_SHELL" "$project/scripts/lib/gpu-docker/docker" info
contains 'runtime[docker] <info>' "$MOCK_TRACE"

[ "$(cat "$KUBECONFIG")" = 'unrelated context' ] || fail 'Unrelated kubeconfig changed'
if grep -Fq "$KUBECONFIG" "$MOCK_TRACE"; then fail 'A command used the unrelated kubeconfig'; fi
printf 'PASS: GPU lifecycle, model mounts and persistence, CUDA failures, node selection, isolation, and argument handling (%s)\n' "$TEST_SHELL"
