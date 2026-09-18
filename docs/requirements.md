# 실행을 위한 최소 요구사항

기본 kind 클러스터, Transformers API와 AIPerf를 실행하기 위한 준비 조건입니다. 기본 버전은 [도구 설정](../config/versions.env)과 각 Dockerfile을 기준으로 합니다. 다른 버전의 지원 여부는 실제 실행으로 확인해야 합니다.

## 호스트 OS와 컨테이너 환경

| 항목 | 요구사항 |
| --- | --- |
| OS | Ubuntu 등 로컬 Linux. 선택한 NVIDIA 드라이버와 Container Toolkit, Docker가 지원하는 배포판과 커널 |
| cgroup | 기본 Kubernetes 구성에 필요한 cgroup v2 활성화 |
| CPU 아키텍처 | x86_64 |
| Docker | 로컬 rootful Docker Engine, `runc`, CDI 장치 지원과 현재 사용자의 Docker 접근 권한 |
| 노드 실행 | kind의 privileged 노드 컨테이너 실행과 호스트 디렉터리 bind mount가 가능한 환경 |
| 셸 | POSIX `sh`와 `awk`, `grep`, `mktemp` 등 일반적인 Linux 명령 |

Ubuntu의 특정 릴리스나 커널 패치 버전을 코드에서 강제하지 않습니다. 기본 노드의 systemd cgroup 설정은 [kind 설정](../config/cluster/kind.yaml)을 따릅니다. cgroup 확인 방법은 [Kubernetes 안내](https://kubernetes.io/docs/concepts/architecture/cgroups/)를 참고합니다.

Docker Engine 28.3 이상을 권장합니다. 이 버전부터 CDI가 기본 활성화됩니다. 이전 버전은 CDI 지원과 활성화 여부를 확인해야 합니다. [Docker CDI 안내](https://docs.docker.com/reference/cli/docker/container/run/#cdi-devices)

빌드와 클러스터 실행에는 같은 로컬 Docker 데몬을 사용합니다.

## NVIDIA GPU, 드라이버와 Toolkit

- 사용할 PyTorch CUDA 빌드가 지원하는 NVIDIA GPU가 1개 이상 필요합니다. 추론 서버는 FP16과 SDPA를 사용하며 CPU 실행으로 전환하지 않습니다.
- 호스트 커널과 GPU에 맞는 NVIDIA 드라이버가 설치되어 있고 `nvidia-smi`가 정상 동작해야 합니다.
- NVIDIA Container Toolkit의 `nvidia-ctk`, `nvidia-cdi-hook`, `nvidia-container-runtime`이 PATH에 있어야 합니다. `nvidia-ctk cdi generate`와 `--nvidia-cdi-hook-path` 옵션을 사용할 수 있어야 합니다.
- 현재 장치와 드라이버를 반영하는 CDI 명세가 있어야 합니다. `nvidia-ctk cdi list`와 Docker CDI 실행에서 호스트와 같은 GPU가 보여야 합니다.

현재 [추론 이미지](../inference/transformers-api-base/Dockerfile)는 CUDA 12.6용 PyTorch를 사용합니다. NVIDIA가 명시한 CUDA 12.x의 Linux x86_64 minor version compatibility 하한은 드라이버 525.60.13입니다. 이는 이 저장소 전체의 실행을 보장하는 최소 드라이버 버전은 아닙니다. GPU 세대, 사용 라이브러리, PTX JIT와 신규 CUDA 기능에 따라 더 최신 드라이버가 필요할 수 있습니다. [CUDA 드라이버 요구사항](https://docs.nvidia.com/cuda/archive/12.6.3/cuda-toolkit-release-notes/index.html), [호환성 제한](https://docs.nvidia.com/deploy/cuda-compatibility/minor-version-compatibility.html)

CUDA 런타임 라이브러리는 추론 이미지에 설치됩니다. 기본 실행을 위해 호스트에 CUDA Toolkit이나 `nvcc`를 별도로 설치할 필요는 없습니다. NVIDIA Container Toolkit은 별도로 필요합니다.

드라이버와 Toolkit 설치는 [NVIDIA 설치 안내](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html), CDI 생성과 갱신은 [CDI 안내](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/cdi-support.html)를 따릅니다. 드라이버나 Toolkit 변경 후에는 [GPU 유지 관리 절차](gpu.md#구성과-유지-관리)에 따라 노드 내부 파일과 CDI를 갱신합니다.

## kind와 Kubernetes

[versions.env](../config/versions.env)의 kind, kubectl과 노드 이미지 조합을 사용합니다. 노드 이미지는 호스트와 같은 CPU 아키텍처여야 합니다. 버전을 변경할 때는 [kind 릴리스의 지원 이미지](https://github.com/kubernetes-sigs/kind/releases)와 [kubectl 버전 차이 정책](https://kubernetes.io/releases/version-skew-policy/#kubectl)을 확인합니다.

기본 구성은 다음 기능에 의존합니다.

- [containerd 설정](../config/cluster/kind.yaml)의 CDI 처리, NVIDIA runtime과 systemd cgroup 설정
- [device plugin](../config/platform/gpu/nvidia-device-plugin.yaml)의 `cdi-cri` 장치 전달
- [통합 벤치마크 Job](../config/benchmarks/transformers-api-aiperf/common/job.yaml)의 네이티브 sidecar (`initContainers`의 `restartPolicy: Always`)
- GPU 노드 한 개의 `local-k8s.nvidia-gpu: "true"` 라벨과 모델 마운트

CDI device plugin 지원은 Kubernetes 1.31에서 GA가 되었고, 네이티브 sidecar는 1.29부터 기본 활성화되었습니다. 이 기능별 기준만으로 이전 Kubernetes 버전에서 저장소 전체가 동작한다고 보장하지는 않습니다. [Device plugin 안내](https://kubernetes.io/docs/concepts/extend-kubernetes/compute-storage-net/device-plugins/), [Sidecar 안내](https://kubernetes.io/docs/concepts/workloads/pods/sidecar-containers/)

클러스터는 `local-k8s.sh up`으로 생성합니다. [Docker 래퍼](../scripts/lib/gpu-docker/docker)는 kind의 실행 인자 형식을 사용해 GPU와 모델을 연결하므로 kind를 바꾸면 이 경로도 검증해야 합니다. 전체 GPU를 노드 하나에 연결하며 MIG, time-slicing과 여러 노드로의 GPU 분배는 구성하지 않습니다. 설정 변경은 [클러스터 가이드](local-k8s.md)를 참고합니다.

## 작업별 추가 도구

| 작업 | 추가 요구사항 |
| --- | --- |
| kind와 kubectl 설치 | `curl` 또는 `wget`, `sha256sum` 또는 `shasum` |
| 모델 다운로드 | `curl`, `sha256sum` |
| 추론 이미지와 AIPerf 이미지 빌드 | `docker build`와 빌드 중 `--network=host` 사용이 가능한 환경 |
| 벤치마크 자동 실행 | 호스트 `python3`, `git`, Git checkout. Namespace 생성과 삭제, Pod와 Job 관리, 로그 및 exec 접근 권한 |
| 호스트 AIPerf CLI | [AIPerf 설치 조건](aiperf.md#호스트-cli로-실행)의 Python과 `venv` |
| Makefile 명령 | `make`. 각 스크립트를 직접 실행할 때는 불필요 |

추론과 AIPerf를 컨테이너로 실행할 때 해당 Python 패키지는 호스트에 설치하지 않습니다. 자동 실행기의 `lscpu`는 선택 사항이며, 없으면 CPU 상세 정보 수집을 생략합니다. 테스트별 준비 조건은 [클러스터 검증](local-k8s.md#검증)과 [구현 테스트](transformers-api-enhanced.md#구현-테스트)를 따릅니다.

## 모델, 이미지와 저장소

- [고정된 모델 리비전](../config/models/qwen3-0.6b.env)의 파일을 [모델 가이드](models.md)에 따라 준비합니다. 자동 벤치마크는 체크섬을 검증하므로 다른 모델을 사용할 때는 검증 설정도 바꿔야 합니다.
- 모델은 호스트 디렉터리에서 GPU 노드로 bind mount하고, Pod에서 `hostPath`로 읽습니다. 컨테이너의 UID/GID 1000이 모델 디렉터리를 탐색하고 파일을 읽을 수 있어야 합니다.
- [서버](../config/serving/transformers-api-base/app.yaml)와 벤치마크 이미지는 `imagePullPolicy: Never`를 사용합니다. 같은 이름과 태그의 이미지를 kind 노드에 먼저 로드해야 합니다. [이미지 준비](transformers-api.md#이미지-빌드와-실행)
- 통합 벤치마크에는 [결과 저장 설정](../config/benchmarks/transformers-api-aiperf/results.yaml)의 `standard` StorageClass와 `ReadWriteOnce` PVC가 필요합니다. 벤치마크와 결과 조회 Pod가 같은 GPU 노드에서 볼륨을 사용할 수 있어야 합니다.
- 기존 클러스터로 매니페스트를 옮길 경우 노드 라벨, 모델 경로, StorageClass와 UID 정책을 맞춰야 합니다. `hostPath`를 금지하는 [Pod Security 정책](https://kubernetes.io/docs/concepts/security/pod-security-standards/)에는 현재 매니페스트를 그대로 적용할 수 없습니다.

이미지의 Python과 직접 의존성 버전은 [base Dockerfile](../inference/transformers-api-base/Dockerfile), [enhanced Dockerfile](../inference/transformers-api-enhanced/Dockerfile), [AIPerf Dockerfile](../benchmarks/aiperf/Dockerfile)을 따릅니다. 다른 베이스 이미지를 사용하면 모든 의존성의 바이너리 wheel이 제공되는지 확인해야 합니다. `--only-binary=:all:` 설치는 소스 빌드로 대체하지 않습니다.

베이스 이미지 태그와 전이 의존성 전체는 고정되어 있지 않으므로 재빌드 결과가 달라질 수 있습니다. 동일한 측정 환경을 재사용하려면 완성된 이미지 아카이브와 모델 체크섬을 보관합니다. AIPerf 버전을 바꿀 때는 CLI 옵션과 결과 파일 형식을 사용하는 [벤치마크 실행기](../scripts/run-transformers-api-aiperf.py)와 [결과 파서](../scripts/lib/aiperf_report.py)도 검증합니다.

## CPU, 메모리와 디스크

| 실행 대상 | 기본 Pod의 자원 요청 |
| --- | --- |
| API 서버 1개 | CPU 1개, 메모리 2Gi, GPU 1개 |
| API와 AIPerf 통합 Job 1개 | 합계 CPU 2개, 메모리 2.5Gi, GPU 1개 |

위 수치는 [서버 설정](../config/serving/transformers-api-base/app.yaml)과 [Job 설정](../config/benchmarks/transformers-api-aiperf/common/job.yaml)의 스케줄링 기준입니다. 호스트 최소 사양이나 실제 메모리 사용량을 의미하지 않습니다. OS, Kubernetes, 결과 조회 Pod, 모델 로딩과 실행 중 사용량을 감당할 여유가 추가로 필요합니다. GPU가 사용 중이면 새 GPU 요청은 대기할 수 있습니다.

전체 실행을 위한 고정된 RAM과 VRAM 최소 용량은 검증되지 않았습니다. 모델 크기, 입력 및 출력 길이와 배치 크기에 맞춰 GPU 메모리를 확보합니다. 다른 GPU에서는 실제 추론까지 검증합니다.

디스크에는 모델 파일, Docker 이미지와 빌드 캐시, kind 노드의 이미지 사본, 임시 아카이브와 결과를 저장할 공간이 필요합니다. 용량 준비 기준은 [모델 다운로드](models.md#다운로드와-연결)와 [이미지 빌드 안내](transformers-api.md#이미지-빌드와-실행)를 따릅니다. `TMPDIR`와 Docker 데이터 경로의 여유 공간도 확인합니다.

## 네트워크와 오프라인 실행

최초 설치와 빌드에는 사용하는 파일의 배포처에 HTTPS로 접근할 수 있어야 합니다.

| 대상 | 주요 배포처 |
| --- | --- |
| kind와 kubectl | GitHub 릴리스, `dl.k8s.io` |
| 노드, GPU plugin과 보조 이미지 | Docker Hub, `nvcr.io` |
| Python 패키지 | PyPI, `download.pytorch.org` |
| 모델 파일 | Hugging Face |

프록시나 방화벽 환경에서는 리다이렉트되는 다운로드 호스트와 CDN도 허용해야 합니다. 도구, 이미지와 모델을 미리 준비하면 [오프라인 절차](local-k8s.md#환경-준비)로 실행할 수 있습니다. 기본 추론 서버는 로컬 모델 파일을 사용합니다.

기본 네트워크는 IPv4이며 Kubernetes API는 로컬 `127.0.0.1`에 바인딩됩니다. Docker 네트워크와 클러스터 DNS가 동작하고 Pod 및 Service 대역이 호스트나 VPN 대역과 충돌하지 않아야 합니다. 원격 접속이나 IPv6 구성이 필요하면 [클러스터 설정](local-k8s.md#클러스터-설정)을 변경해야 합니다.

## 실행 전 확인

호스트 준비와 도구 설치를 마친 뒤 저장소 루트에서 확인합니다.

```sh
uname -s
uname -m
stat -fc %T /sys/fs/cgroup/
nvidia-smi
nvidia-ctk cdi list
./scripts/local-k8s.sh doctor
```

OS는 `Linux`, CPU 아키텍처는 `x86_64`, cgroup 파일시스템은 `cgroup2fs`여야 합니다. `doctor`는 도구 존재, Docker 접근과 GPU 노출을 확인하지만 모든 버전 조합, 파일 권한, VRAM과 추론 호환성을 보장하지 않습니다.

클러스터 생성과 CUDA 연산 확인은 [빠른 시작](../README.md#빠른-시작), 모델 권한 확인은 [모델 마운트 검증](models.md#마운트-검증), 실제 추론 확인은 [API 검증](transformers-api.md#검증과-종료)을 따릅니다.
