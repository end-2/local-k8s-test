# AIPerf 클라이언트 사용법

AIPerf로 채팅 API에 부하를 보내고 지연 시간, 처리량과 토큰 사용량을 측정합니다. Kubernetes Job 또는 호스트 CLI로 실행할 수 있습니다.

접속 가능한 API 주소, 제공하는 모델 이름과 로컬 토크나이저 경로를 준비합니다. 서버 배포와 호스트 접속 방법은 [공통 API 가이드](transformers-api.md)를 참고합니다. CLI 옵션은 [NVIDIA AIPerf 문서](https://docs.nvidia.com/aiperf/reference/command-line-options)에 있습니다.

## Kubernetes에서 실행

[AIPerf Job](../config/aiperf-job.yaml)은 `http://transformers-api:8000`으로 요청하고 결과를 PVC에 저장합니다. CPU와 메모리만 사용하며 GPU를 요청하지 않습니다. 이 주소를 사용할 때는 대상 Service와 같은 namespace에서 실행합니다.

저장소 루트에서 클라이언트 이미지를 빌드하고 로드합니다. 버전은 [Dockerfile](../benchmarks/aiperf/Dockerfile)에 고정되어 있습니다.

```sh
docker build --network=host -t local/aiperf:0.12.0 benchmarks/aiperf
mkdir -p .local-k8s/image-tmp
TMPDIR="$PWD/.local-k8s/image-tmp" ./scripts/local-k8s.sh load-image local/aiperf:0.12.0
./scripts/local-k8s.sh kubectl apply -f config/aiperf-results.yaml
./scripts/local-k8s.sh kubectl wait --for=condition=Ready pod/aiperf-results --timeout=120s
```

[결과 저장 설정](../config/aiperf-results.yaml)은 PVC와 결과 조회용 Pod를 생성하며 kind의 `standard` StorageClass를 사용합니다. Job과 조회용 Pod는 로컬 토크나이저가 있는 노드에 배치되어 같은 `ReadWriteOnce` 볼륨을 공유합니다.

```sh
job=$(./scripts/local-k8s.sh kubectl create -f config/aiperf-job.yaml -o name)
./scripts/local-k8s.sh kubectl wait --for=condition=Complete "$job" --timeout=600s
./scripts/local-k8s.sh kubectl logs "$job"
```

실행할 때마다 새 Job 이름과 결과 디렉터리를 사용합니다. 실패한 Job은 자동 재시도하지 않으며 실행 상한은 10분입니다.

## 호스트 CLI로 실행

호스트에 Python 3.11부터 3.13 중 하나와 `venv`가 필요합니다. 다음 예제는 `http://127.0.0.1:8000`으로 접속합니다.

```sh
python3 -m venv .local-k8s/aiperf-venv
.local-k8s/aiperf-venv/bin/pip install aiperf==0.12.0

.local-k8s/aiperf-venv/bin/aiperf profile \
  --model Qwen/Qwen3-0.6B \
  --tokenizer "$PWD/.models/Qwen3-0.6B" \
  --url http://127.0.0.1:8000 \
  --endpoint-type chat \
  --streaming \
  --use-server-token-count \
  --synthetic-input-tokens-mean 128 \
  --synthetic-input-tokens-stddev 0 \
  --output-tokens-mean 64 \
  --output-tokens-stddev 0 \
  --extra-inputs ignore_eos:true \
  --concurrency 1 \
  --warmup-request-count 2 \
  --request-count 20 \
  --no-server-metrics \
  --ui simple \
  --artifact-dir ".local-k8s/aiperf/run-$(date -u +%Y%m%dT%H%M%SZ)"
```

모델 경로를 바꿨으면 `--tokenizer`에도 같은 호스트 경로를 지정합니다. `chat` endpoint는 `/v1/chat/completions`를 붙이므로 `--url`에는 서버 주소만 지정합니다.

## 측정 조건과 결과 해석

Job의 `args` 또는 호스트 CLI에서 입력과 출력 길이, 동시 요청 수, warmup과 측정 요청 수를 지정합니다.

- `--streaming`을 제거하면 일반 JSON 응답을 측정합니다.
- `--use-server-token-count`는 응답에 포함된 토큰 사용량으로 집계합니다. 필드의 의미는 [API 응답 형식](transformers-api.md#채팅-요청과-json-응답)을 참고합니다.
- `--extra-inputs ignore_eos:true`는 고정 출력 길이 측정을 위한 요청 옵션입니다. 일반 응답 품질을 확인할 때는 생략합니다.
- `--no-server-metrics`는 서버 메트릭 수집을 끕니다.

TTFT와 ITL은 클라이언트가 수신한 스트리밍 텍스트를 기준으로 해석합니다. 응답 청크 하나에 여러 토큰이 들어갈 수 있으므로 GPU의 개별 토큰 연산 시간을 나타내지 않습니다. 측정 조건을 비교할 때는 모델, 입력과 출력 길이, 동시 요청 수와 클라이언트 자원을 함께 기록합니다.

## 결과 가져오기

호스트 실행 결과는 `--artifact-dir`에 저장됩니다. Kubernetes 실행 결과는 PVC의 `/results/<Pod 이름>/`에 저장되며 Job을 삭제해도 남습니다. 완료된 컨테이너 대신 조회용 Pod에서 복사합니다.

```sh
pod=$(./scripts/local-k8s.sh kubectl get pods \
  -l "batch.kubernetes.io/job-name=${job##*/}" \
  -o jsonpath='{.items[0].metadata.name}')
mkdir -p .local-k8s/aiperf
./scripts/local-k8s.sh kubectl cp \
  "aiperf-results:/results/$pod" ".local-k8s/aiperf/$pod"
```

요약은 `profile_export_aiperf.json`과 `profile_export_aiperf.csv`, 개별 요청은 `profile_export.jsonl`, 실행 로그는 `logs/aiperf.log`에서 확인합니다. 완료된 Job과 해당 Pod는 한 시간 뒤 자동 삭제됩니다. 이후에는 조회용 Pod에서 보관된 디렉터리 이름을 확인하고 같은 복사 명령을 사용합니다.

```sh
./scripts/local-k8s.sh kubectl exec aiperf-results -- ls /results
```

## 문제 해결과 정리

실패하면 Job과 Pod 상태를 확인합니다. `ErrImageNeverPull`은 클라이언트 이미지 로드 여부, `Pending`은 PVC와 노드 선택 조건, 접속 실패는 `--url`과 네트워크 연결을 확인합니다.

```sh
./scripts/local-k8s.sh kubectl describe "$job"
./scripts/local-k8s.sh kubectl logs "$job"
```

결과를 복사한 뒤 해당 Job을 삭제합니다. 모든 벤치마크 Job이 종료되고 결과가 필요 없으면 조회용 Pod와 PVC도 삭제합니다. PVC 또는 클러스터를 삭제하면 저장된 결과가 사라집니다.

```sh
./scripts/local-k8s.sh kubectl delete "$job"
./scripts/local-k8s.sh kubectl delete -f config/aiperf-results.yaml
```
