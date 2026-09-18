# Transformers API와 AIPerf 통합 실행

[통합 Job 설정](../config/benchmarks/transformers-api-aiperf/)은 base 또는 enhanced를 선택해 concurrency별로 새 Pod에서 Transformers API와 AIPerf를 함께 실행합니다. 클라이언트는 같은 Pod의 `http://127.0.0.1:8000`으로 접속합니다. 두 구현과 모든 concurrency가 [공통 Job](../config/benchmarks/transformers-api-aiperf/common/job.yaml)의 workload와 자원 설정을 공유합니다.

## 실행 순서

1. 새 Pod의 `api` 컨테이너가 GPU를 할당받고 모델을 로드합니다.
2. `/readyz` startup probe가 성공하면 `aiperf` 컨테이너가 시작됩니다.
3. AIPerf가 warmup 후 해당 concurrency의 측정 요청을 보냅니다.
4. AIPerf가 종료되면 API 서버도 종료되고 GPU가 반환됩니다.
5. 다음 concurrency는 별도 Pod와 새 서버 프로세스로 실행합니다.

서버는 `initContainers`의 `restartPolicy: Always`로 선언한 [Kubernetes native sidecar](https://kubernetes.io/docs/concepts/workloads/pods/sidecar-containers/)입니다. startup probe가 클라이언트 시작을 제어하고, 서버가 실행 중이어도 클라이언트 종료 후 Job이 완료됩니다. 저장소의 Kubernetes 구성은 이 기능을 지원합니다.

## 준비

클러스터와 모델은 [모델 가이드](models.md)를 따릅니다. 통합 Job은 API 컨테이너를 직접 실행하므로 별도 Deployment와 Service가 필요하지 않습니다.

저장소 루트에서 `api_variant`를 `base` 또는 `enhanced`로 지정하고 이미지를 빌드합니다. 구현별 태그를 사용하므로 두 이미지를 함께 로드해도 서로 덮어쓰지 않습니다.

```sh
api_variant=enhanced
api_image="local/transformers-api-${api_variant}:0.1.0"
docker build --network=host -t "$api_image" "inference/transformers-api-$api_variant"
docker build --network=host -t local/aiperf:0.12.0 benchmarks/aiperf
mkdir -p .local-k8s/image-tmp
TMPDIR="$PWD/.local-k8s/image-tmp" ./scripts/local-k8s.sh load-image \
  "$api_image" local/aiperf:0.12.0
```

이미지 빌드 시 디스크 준비 사항은 [공통 API 가이드](transformers-api.md#이미지-빌드와-실행)를 참고합니다. [결과 저장 설정](../config/benchmarks/transformers-api-aiperf/results.yaml)은 PVC와 결과 조회용 Pod를 생성하며 kind의 `standard` StorageClass를 사용합니다. Job과 조회용 Pod는 모델이 있는 노드에서 같은 `ReadWriteOnce` 볼륨을 공유합니다.

| 선택 | API 이미지 | Job 설정 경로 |
| --- | --- | --- |
| base | `local/transformers-api-base:0.1.0` | `config/benchmarks/transformers-api-aiperf/base/concurrency-<값>` |
| enhanced | `local/transformers-api-enhanced:0.1.0` | `config/benchmarks/transformers-api-aiperf/enhanced/concurrency-<값>` |

이미지 태그는 [base 설정](../config/benchmarks/transformers-api-aiperf/base/common/kustomization.yaml) 또는 [enhanced 설정](../config/benchmarks/transformers-api-aiperf/enhanced/common/kustomization.yaml)에서 변경합니다. enhanced 전용 서버 옵션을 추가할 때는 enhanced 설정에만 `initContainers`의 `api.args`를 패치합니다. 옵션의 의미는 [enhanced 가이드](transformers-api-enhanced.md)를 참고합니다.

같은 구현의 concurrency를 비교할 때는 모델 파일, 이미지, 서버 옵션과 자원 설정을 동일하게 유지합니다. 요청 수 256건, 길이 분포, seed와 warmup 8건은 [공통 Job](../config/benchmarks/transformers-api-aiperf/common/job.yaml)에서 관리합니다. 클라이언트 옵션과 결과 해석은 [AIPerf 가이드](aiperf.md#측정-조건과-결과-해석)를 참고합니다.

서버의 입력 한도는 두 구현 모두 `--max-input-tokens 4096`입니다. 2048토큰 합성 프롬프트에 채팅 템플릿이 추가되는 길이를 수용합니다.

Job 하나는 GPU 1개를 사용합니다. 다른 서버가 GPU를 사용 중이면 Job이 대기하므로 해당 서버를 종료한 뒤 실행합니다. 서버 프로세스와 CUDA 상태는 매번 새로 생성되지만, GPU 온도와 호스트의 파일 캐시는 실행 간 차이가 있을 수 있습니다.

## 개별 실행

아래 수동 실행은 결과용 PVC와 조회 Pod를 먼저 준비합니다. 범위 실행 스크립트는 자체 namespace에서 자동으로 준비합니다.

```sh
./scripts/local-k8s.sh kubectl apply -f config/benchmarks/transformers-api-aiperf/results.yaml
./scripts/local-k8s.sh kubectl wait --for=condition=Ready pod/aiperf-results --timeout=120s
```

`concurrency`에는 `1, 2, 4, 8, 16, 32, 64, 128` 중 하나를 지정합니다.

```sh
api_variant=enhanced
concurrency=1
job=$(./scripts/local-k8s.sh kubectl create \
  -k "config/benchmarks/transformers-api-aiperf/$api_variant/concurrency-$concurrency" -o name)
./scripts/local-k8s.sh kubectl wait --for=condition=Complete "$job" --timeout=7300s
./scripts/local-k8s.sh kubectl logs "$job" -c aiperf
./scripts/local-k8s.sh kubectl logs "$job" -c api
```

Job 이름은 `<api_variant>-transformers-api-aiperf-c<concurrency>`입니다. 같은 이름의 Job이 남아 있으면 결과를 수집하고 삭제한 뒤 다시 실행합니다. API 준비가 완료되기 전에는 AIPerf 컨테이너가 `PodInitializing` 상태로 대기합니다.

## 범위를 선택해 실행하고 보고서 저장

[실행 스크립트](../scripts/run-transformers-api-aiperf.py)는 Python 3 표준 라이브러리만 사용합니다. 이미지와 모델을 준비한 뒤 저장소 루트에서 실행합니다. 결과용 PVC와 조회 Pod는 실행마다 별도 namespace에 자동 생성합니다.

```sh
# base에서 concurrency 1, 2 실행
python3 scripts/run-transformers-api-aiperf.py --variant base --max-concurrency 2

# enhanced에서 concurrency 1, 2, 4, 8 실행
python3 scripts/run-transformers-api-aiperf.py --variant enhanced --max-concurrency 8

# 두 구현을 concurrency별 base, enhanced 순서로 실행
python3 scripts/run-transformers-api-aiperf.py --variant both --max-concurrency 2
```

`--max-concurrency`는 `1, 2, 4, 8, 16, 32, 64, 128` 중 하나입니다. 1부터 선택한 값까지 이 목록의 concurrency만 실행합니다. 매번 새 서버를 시작하고 이전 Job을 수집 및 삭제한 뒤 다음 케이스를 실행합니다. `--cooldown-seconds`는 서버 시작 전 대기 시간이며 기본값은 20초입니다. 요청 수와 길이는 공통 Job 설정을 그대로 사용합니다.

기본 출력 경로는 `reports/<UTC 시각>-transformers-api-aiperf-<구현>-c<최대값>/`입니다. `--output-dir`로 새 경로를 지정할 수 있으며, 기존 디렉터리는 덮어쓰지 않습니다. 실행 계획만 확인하려면 `--dry-run`을 추가합니다. 이 경우 Job 설정과 계획을 기록하고 Kubernetes 리소스를 생성하지 않습니다.

```sh
python3 scripts/run-transformers-api-aiperf.py \
  --variant enhanced --max-concurrency 128 \
  --output-dir reports/enhanced-c128-check --dry-run
```

| 경로 | 내용 |
| --- | --- |
| `progress.json` | 실행 상태, 현재 케이스, 완료 개수, namespace |
| `summary.md`, `summary.csv`, `summary.json` | 완료된 케이스의 TTFT, ITL, latency와 처리량 |
| `requests.csv` | warmup과 본 측정을 구분한 전체 요청별 지표 |
| `by-length.csv`, `by-length.json` | 본 측정의 출력 목표 길이별 지표 |
| `comparison.csv` | `both` 실행 시 같은 concurrency의 구현별 지표와 처리량 비율 |
| `<구현>/concurrency-<값>/raw/` | AIPerf 원본 JSONL, JSON, CSV, 입력 payload와 로그 |
| `<구현>/concurrency-<값>/` | 요청별 CSV, 검증 결과, Job과 Pod, 컨테이너 로그, GPU 표본 |
| `environment/` | 실행 순서, 설정과 소스 사본, 패키지 버전, 모델 해시와 노드 정보 |

이미지 내부 소스와 현재 checkout이 다르거나 모델 체크섬이 맞지 않으면 측정 전에 중단합니다. 케이스 간 데이터셋, 실제 이미지 ID와 실행 노드를 비교하고, 서버 재시작이나 요청 오류가 있으면 실패로 기록합니다. 모든 요청의 출력 토큰 수가 목표 길이와 일치해야 성공으로 처리합니다. 시간 단위는 ms이며 요약에는 warmup을 포함하지 않습니다.

성공하면 결과를 로컬에 복사한 뒤 해당 실행의 namespace와 PVC를 삭제합니다. 실패하거나 Ctrl+C로 중단하면 활성 Job을 정리하고 수집 가능한 결과를 저장합니다. 복구를 위해 namespace와 PVC는 남기며 다음 케이스는 실행하지 않습니다. 상태는 `progress.json`, 수집 오류는 케이스의 `collection.json`에서 확인합니다. 네트워크 장애로 정리하지 못한 리소스도 해당 namespace에서 확인합니다.

```sh
# progress.json에 기록된 namespace로 교체
run_namespace='aiperf-<실행-ID>'
./scripts/local-k8s.sh kubectl -n "$run_namespace" get jobs,pods,pvc
./scripts/local-k8s.sh kubectl -n "$run_namespace" exec aiperf-results -- ls /results
./scripts/local-k8s.sh kubectl -n "$run_namespace" cp \
  "aiperf-results:/results/<Pod-이름>" "reports/<복구-경로>"
# 필요한 데이터를 복사한 뒤 실행 리소스 삭제
./scripts/local-k8s.sh kubectl delete namespace "$run_namespace"
```

스크립트 검증은 `python3 tests/test-aiperf-runner.py`로 실행합니다.

## 결과와 종료 확인

결과는 PVC의 `/results/<Pod 이름>/`에 저장됩니다. Pod 이름에 구현과 concurrency가 포함되어 결과 디렉터리도 구분됩니다. 완료된 컨테이너 대신 조회용 Pod에서 복사합니다.

```sh
pod=$(./scripts/local-k8s.sh kubectl get pods \
  -l "batch.kubernetes.io/job-name=${job##*/}" \
  -o jsonpath='{.items[0].metadata.name}')
mkdir -p .local-k8s/aiperf
./scripts/local-k8s.sh kubectl cp \
  "aiperf-results:/results/$pod" ".local-k8s/aiperf/$pod"
```

측정 파일과 성공 여부 확인은 [클라이언트 결과 설명](aiperf.md#결과-확인)을 참고합니다. 여러 컨테이너를 포함하므로 로그에는 `-c api` 또는 `-c aiperf`를 지정합니다.

```sh
./scripts/local-k8s.sh kubectl get pods \
  -l "batch.kubernetes.io/job-name=${job##*/}" -o yaml
```

Pod의 `status.initContainerStatuses`에서 `api`의 `restartCount`가 `0`인지 확인합니다. 서버가 중간에 재시작한 실행은 비교에서 제외합니다. 완료 후에는 API와 AIPerf 모두 `state.terminated`를 가져야 합니다. Pod의 `spec.nodeName`과 각 컨테이너의 `imageID`로 실행 노드와 실제 이미지도 확인할 수 있습니다.

실패한 Job은 자동 재시도하지 않으며 실행 상한은 2시간입니다. `kubectl wait`는 성공 조건을 기다리므로 실패한 Job에서도 제한 시간까지 기다릴 수 있습니다. `ErrImageNeverPull`은 선택한 구현의 이미지 로드 여부, `Pending`은 GPU와 PVC를 확인합니다. 서버 로딩 실패는 `api` 로그, 요청 오류는 `aiperf` 로그와 측정 결과에서 확인합니다.

완료된 Job과 Pod는 한 시간 뒤 자동 삭제되지만 PVC의 결과는 남습니다. Pod가 삭제된 뒤에는 조회용 Pod에서 디렉터리 이름을 확인해 복사합니다.

```sh
./scripts/local-k8s.sh kubectl exec aiperf-results -- ls /results
```

결과를 복사한 뒤 삭제합니다. 실행 중인 Job을 삭제하면 서버와 클라이언트가 함께 종료됩니다. PVC는 유지됩니다.

```sh
./scripts/local-k8s.sh kubectl delete "$job" --cascade=foreground --wait=true
```

모든 측정을 종료하고 결과를 복사한 뒤 저장 공간이 필요 없으면 조회용 Pod와 PVC를 삭제합니다. PVC를 삭제하면 보관된 결과도 삭제됩니다.

```sh
./scripts/local-k8s.sh kubectl delete -f config/benchmarks/transformers-api-aiperf/results.yaml
```
