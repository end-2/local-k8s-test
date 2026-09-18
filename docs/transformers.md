# Transformers GPU 추론

Qwen3-0.6B를 로컬 모델 볼륨에서 읽고 Kubernetes Job으로 응답을 생성합니다. Job은 실행 후 종료하며 GPU를 반환합니다. 모델 준비는 [모델 가이드](models.md)를 따릅니다.

## 이미지 빌드와 배포

저장소 루트에서 실행합니다. [Dockerfile](../inference/transformers/Dockerfile)은 Python slim 이미지에 CUDA용 PyTorch와 Transformers를 설치합니다. 버전과 CUDA 배포판은 Dockerfile에 고정합니다. 모델은 이미지에 복사하지 않습니다.

```sh
docker build --network=host -t local/transformers-qwen3:0.1.0 inference/transformers
mkdir -p .local-k8s/image-tmp
TMPDIR="$PWD/.local-k8s/image-tmp" ./scripts/local-k8s.sh load-image local/transformers-qwen3:0.1.0

job=$(./scripts/local-k8s.sh kubectl create -f config/transformers-inference.yaml -o name)
./scripts/local-k8s.sh kubectl wait --for=condition=Complete "$job" --timeout=300s
./scripts/local-k8s.sh kubectl logs "$job"
```

`--network=host`는 빌드 중 패키지 다운로드에 사용하며 Docker 기본 브리지가 없는 로컬 Linux 호스트에서도 동작합니다. Pod는 Kubernetes 기본 네트워크를 사용합니다. 이미지를 수정하면 빌드와 `load-image`를 다시 실행한 뒤 새 Job을 생성합니다.

이미지 로드용 임시 아카이브는 `.local-k8s/image-tmp`에 저장하고 완료 후 삭제합니다. `/tmp`가 있는 시스템 디스크 대신 저장소가 있는 디스크의 여유 공간을 사용합니다.

직접 설치하는 Python 패키지는 `torch`와 `transformers`입니다. 각 패키지의 필수 전이 의존성과 CUDA 라이브러리도 포함됩니다. Accelerate, torchvision, torchaudio, FlashAttention, 웹 서버, 컴파일 도구는 추가하지 않습니다. 이미지는 압축 해제 후 약 10GiB를 사용합니다. Docker와 kind가 이미지를 각각 보관하므로 처음 빌드하고 로드할 때는 Docker 저장소에 약 25GiB 이상의 여유 공간을 준비합니다.

[추론 Job](../config/transformers-inference.yaml)은 GPU 1개를 요청하고 `/model`을 읽기 전용으로 마운트합니다. 일반 사용자 권한으로 실행하며 임시 파일은 `/tmp`에 씁니다. `HF_HUB_OFFLINE=1`과 `local_files_only=True`를 사용하므로 모델 로딩에 인터넷 연결이 필요하지 않습니다.

## 이미지 아카이브 재사용

저장된 이미지 아카이브가 있으면 Docker 로컬 이미지 없이 kind에 직접 로드할 수 있습니다. 이후 위의 Job 생성 명령을 실행합니다.

```sh
./scripts/local-k8s.sh load-archive .local-k8s/images/transformers-qwen3-0.1.0.tar
```

새로 빌드한 이미지를 아카이브로 저장할 때는 다음 명령을 사용합니다. 아카이브는 저장소가 있는 디스크에 보관하며, kind 노드에 로드된 이미지는 Docker 로컬 이미지와 별도로 관리됩니다.

```sh
mkdir -p .local-k8s/images
docker image save --platform linux/amd64 \
  --output .local-k8s/images/transformers-qwen3-0.1.0.tar \
  local/transformers-qwen3:0.1.0
```

## 입력과 배치 크기

[Python 스크립트](../inference/transformers/infer.py)는 FP16, PyTorch SDPA, thinking 비활성화와 greedy decoding을 사용합니다. 런타임 컴파일러가 필요하지 않도록 PyTorch Python native CUDA 경로를 끄고 사전 빌드된 CUDA 커널을 사용합니다. GPU가 없으면 오류로 종료합니다. 옵션은 다음 명령으로 확인합니다.

```sh
python3 inference/transformers/infer.py --help
```

Job의 `args`에서 `--prompt`, `--batch-size`, `--max-new-tokens` 값을 바꾼 뒤 새 Job을 생성합니다. 예를 들어 배치 4개는 다음 인수를 사용합니다.

```yaml
args:
  - --model
  - /model
  - --prompt
  - 대한민국의 수도는 어디인가요? 한 문장으로 답하세요.
  - --batch-size
  - "4"
  - --max-new-tokens
  - "128"
```

배치 크기는 같은 프롬프트를 한 번의 `generate()` 호출로 처리하는 요청 수입니다. HTTP 동시 요청이나 continuous batching을 측정하려면 별도의 서버와 부하 생성기가 필요합니다. GPU 한 개를 사용하는 이 환경에서는 Job을 순서대로 실행합니다.

## 결과와 검증

로그는 JSON Lines 형식이며 다음 이벤트를 출력합니다.

| 이벤트 | 내용 |
| --- | --- |
| `runtime` | GPU, 라이브러리 버전, 정밀도와 배치 크기 |
| `loaded` | 모델과 토크나이저 로딩 시간 |
| `response` | 각 요청의 생성 토큰 수와 응답 |
| `complete` | 생성 시간, 전체 출력 tokens/sec, PyTorch 최대 GPU 메모리 |

생성 시간은 CUDA 동기화를 포함해 측정하며 입력 처리와 토큰 생성을 포함합니다. 모델 로딩과 문자열 디코딩은 제외합니다. warmup 없는 1회 실행이므로 성능 비교 시 같은 프롬프트와 출력 길이를 사용해야 합니다. 토큰 수에는 첫 EOS가 포함되고 배치 패딩은 제외됩니다. GPU 메모리는 PyTorch allocator 기준이며 CUDA 컨텍스트 등 프로세스 전체 사용량과 다릅니다.

Job의 `Complete` 상태와 `complete` 로그를 확인합니다. 빈 응답이나 GPU 오류는 실패로 종료합니다. 완료된 Job과 로그는 한 시간 후 자동으로 삭제됩니다.

```sh
./scripts/local-k8s.sh kubectl describe "$job"
./scripts/local-k8s.sh kubectl logs "$job"
```

`ErrImageNeverPull`이면 이미지를 해당 클러스터에 다시 로드합니다. `Pending`이면 다른 Job의 GPU 점유 여부를 확인합니다. CUDA 메모리가 부족하면 배치 크기와 입력 또는 출력 토큰 수를 줄입니다.
