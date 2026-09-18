# NVIDIA GPU 사용

`local-k8s.sh up`은 호스트의 NVIDIA GPU를 kind 노드에 연결하고 NVIDIA device plugin을 설치합니다. 기본 클러스터 이름은 `local-k8s`입니다.

## 준비 사항

호스트 OS, NVIDIA 드라이버, Container Toolkit과 Docker 준비는 [최소 요구사항](requirements.md)을 따릅니다. CDI 목록이 비어 있거나 드라이버 변경 후 오래된 상태라면 [CDI 갱신 안내](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/cdi-support.html)를 확인합니다.

단일 노드 구성에서는 control-plane에, 멀티 노드 구성에서는 첫 번째 worker에 전체 GPU를 연결합니다. MIG, GPU time-slicing과 여러 노드로의 GPU 분배는 구성하지 않습니다.

## 실행

클러스터 생성은 [빠른 시작](../README.md#빠른-시작)을 따릅니다. GPU 상태와 CUDA 연산은 다음 명령으로 확인합니다.

```sh
./scripts/local-k8s.sh status
./scripts/local-k8s.sh test
```

`up`은 노드에 GPU를 연결하고 할당 가능한 `nvidia.com/gpu` 수를 확인합니다. `test`는 GPU 1개를 요청하는 CUDA 벡터 덧셈 Job을 실행합니다. 성공 시 `Test PASSED`가 출력되며 완료된 Job은 5분 후 정리됩니다.

기존 클러스터의 설정 변경과 kubeconfig 경로는 [사용 가이드](local-k8s.md)를 참고하세요.

## Workload 배포

GPU가 필요한 컨테이너에 다음 자원 제한을 추가합니다.

```yaml
resources:
  limits:
    nvidia.com/gpu: 1
```

```sh
./scripts/local-k8s.sh kubectl apply -f path/to/workload.yaml
```

device plugin이 CDI를 통해 할당한 GPU와 드라이버 라이브러리를 전달하므로 workload에 별도 RuntimeClass는 필요하지 않습니다. GPU 1개를 사용 중이면 추가 GPU 요청 Pod는 자원이 반환될 때까지 대기합니다. 실행 예시는 [CUDA 테스트 Job](../config/platform/gpu/smoke-test.yaml)을 참고하세요.

## 구성과 유지 관리

- [기본 kind 설정](../config/cluster/kind.yaml)과 [멀티 노드 설정](../config/cluster/kind-multi-node.yaml): GPU 연결 대상과 노드 내부 containerd 설정
- [device plugin](../config/platform/gpu/nvidia-device-plugin.yaml): NVIDIA RuntimeClass와 GPU 자원 등록
- [Docker 래퍼](../scripts/lib/gpu-docker/docker): 지정한 kind 노드 생성 명령에만 CDI 장치 옵션 추가

노드 생성은 [Docker CDI](https://docs.docker.com/reference/cli/docker/container/run/#cdi-devices)를 사용합니다. 노드에는 호스트의 NVIDIA Toolkit 실행 파일을 복사하고 CDI 명세를 생성합니다. device plugin은 [CDI-CRI 방식](https://github.com/NVIDIA/k8s-device-plugin#configuration-option-details)으로 workload에 GPU를 할당합니다.

사용자 지정 kind 설정에는 GPU를 연결할 노드 하나에만 `local-k8s.nvidia-gpu: "true"` 라벨과 예제의 `/dev/null` 마운트를 지정하고, `containerdConfigPatches`도 포함합니다. 마운트는 Docker 래퍼가 GPU 연결 대상을 식별하는 표식입니다. 생성에는 `local-k8s.sh up`을 사용합니다.

드라이버 또는 Toolkit을 업데이트하면 클러스터를 다시 생성해 노드의 라이브러리와 CDI 명세를 갱신합니다. `down`은 노드 내부의 영구 볼륨 데이터도 삭제합니다.

```sh
./scripts/local-k8s.sh down
./scripts/local-k8s.sh up
```

## 문제 해결

```sh
./scripts/local-k8s.sh doctor
./scripts/local-k8s.sh kubectl -n kube-system logs daemonset/nvidia-device-plugin
./scripts/local-k8s.sh kubectl get pods -A
./scripts/local-k8s.sh logs
```

`doctor`가 실패하면 드라이버, CDI 목록과 Docker 접근 권한을 확인합니다. GPU 수가 표시되지 않으면 device plugin 로그를 확인합니다. CUDA Job이 대기하면 다른 workload가 GPU를 점유했는지 확인합니다.

스크립트 회귀 검증은 `sh tests/test-local-k8s.sh`로 실행합니다. 실제 GPU 검증은 위의 `test` 명령을 사용합니다.
