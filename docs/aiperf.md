# Kubernetes에서 AIPerf 실행

[AIPerf Job](../config/aiperf-job.yaml)은 `http://transformers-api:8000`으로 부하를 보내고 결과를 PVC에 저장합니다. CPU와 메모리만 사용하며 GPU를 요청하지 않습니다. API 서버와 같은 namespace에서 실행합니다.

## 준비

[Transformers API 가이드](transformers-api.md)를 따라 서버를 배포합니다. 클러스터 내부 Service로 접속하므로 port-forward는 필요하지 않습니다. 로컬 모델 볼륨과 kind의 `standard` StorageClass를 사용합니다.

저장소 루트에서 벤치마크 이미지를 빌드하고 로드합니다. [Dockerfile](../benchmarks/aiperf/Dockerfile)에 AIPerf 버전이 고정되어 있으며 CUDA와 PyTorch는 설치하지 않습니다.

```sh
docker build --network=host -t local/aiperf:0.12.0 benchmarks/aiperf
mkdir -p .local-k8s/image-tmp
TMPDIR="$PWD/.local-k8s/image-tmp" ./scripts/local-k8s.sh load-image local/aiperf:0.12.0
./scripts/local-k8s.sh kubectl rollout status deployment/transformers-api --timeout=300s
./scripts/local-k8s.sh kubectl apply -f config/aiperf-results.yaml
./scripts/local-k8s.sh kubectl wait --for=condition=Ready pod/aiperf-results --timeout=120s
```

[결과 저장 설정](../config/aiperf-results.yaml)은 PVC와 결과 조회용 Pod를 생성합니다. Job과 조회용 Pod는 로컬 토크나이저가 있는 GPU 노드에 배치되지만 GPU 자원을 점유하지 않습니다. 여러 노드가 있는 환경에서도 같은 노드의 `ReadWriteOnce` 볼륨을 공유합니다.

## 벤치마크 실행

```sh
job=$(./scripts/local-k8s.sh kubectl create -f config/aiperf-job.yaml -o name)
./scripts/local-k8s.sh kubectl wait --for=condition=Complete "$job" --timeout=600s
./scripts/local-k8s.sh kubectl logs "$job"
```

실행할 때마다 새 Job 이름과 결과 디렉터리를 사용합니다. Job의 `args`에서 입력과 출력 길이, 동시 요청 수, warmup과 측정 요청 수를 바꿉니다. 일반 JSON 응답을 측정하려면 `--streaming`을 제거합니다. 실패한 Job은 자동 재시도하지 않으며 실행 상한은 10분입니다.

토큰 수와 스트리밍 지연 시간의 해석은 [API 서버 가이드](transformers-api.md#aiperf-실행), CLI 옵션은 [NVIDIA AIPerf 문서](https://docs.nvidia.com/aiperf/reference/command-line-options)를 참고합니다. API 서버는 GPU 추론을 직렬 처리하므로 동시 요청 수를 늘리면 대기 시간도 포함됩니다. 같은 노드의 CPU를 사용하는 부하 생성기와 API 서버가 서로 영향을 줄 수 있습니다.

## 결과 가져오기

결과는 `/results/<Pod 이름>/`에 저장되며 Job을 삭제해도 PVC에 남습니다. 완료된 컨테이너 대신 조회용 Pod에서 복사합니다.

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

실패하면 Job과 Pod 상태를 확인합니다. `ErrImageNeverPull`은 이미지 로드 여부, `Pending`은 PVC와 노드 선택 조건, 접속 실패는 API 서버의 준비 상태를 확인합니다.

```sh
./scripts/local-k8s.sh kubectl describe "$job"
./scripts/local-k8s.sh kubectl logs "$job"
```

결과를 복사한 뒤 해당 Job을 삭제합니다. 모든 벤치마크 Job이 종료되고 결과가 필요 없으면 조회용 Pod와 PVC도 삭제합니다. PVC 또는 클러스터를 삭제하면 저장된 결과가 사라집니다.

```sh
./scripts/local-k8s.sh kubectl delete "$job"
./scripts/local-k8s.sh kubectl delete -f config/aiperf-results.yaml
```

API 서버 종료는 [API 서버 가이드](transformers-api.md#검증과-종료)를 따릅니다.
