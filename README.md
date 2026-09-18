# Inference engine performance test framework

NVIDIA GPU를 사용하는 kind 기반 로컬 Kubernetes 환경입니다. 기본 구성은 단일 노드이며 inference engine 성능 테스트에 사용합니다.

## 준비 사항

- NVIDIA GPU와 드라이버가 설치된 Linux 호스트, POSIX 셸
- CDI를 지원하는 rootful Docker와 NVIDIA Container Toolkit ([준비 안내](docs/gpu.md))
- 도구 설치 시 curl 또는 wget, sha256sum 또는 shasum과 인터넷 연결

## 빠른 시작

Docker를 실행한 뒤 저장소 루트에서 실행합니다.

```sh
./scripts/local-k8s.sh install
./scripts/local-k8s.sh doctor
./scripts/local-k8s.sh up
./scripts/local-k8s.sh status
./scripts/local-k8s.sh test
```

`install`은 kind와 kubectl을 `.bin/`에 설치합니다. 호환되는 도구가 PATH에 있으면 생략할 수 있습니다. `up`은 GPU 자원 등록까지 기다리며, `test`는 CUDA 연산을 검증합니다. kubeconfig는 `.local-k8s/`에 저장합니다.

클러스터가 준비되면 `./scripts/local-k8s.sh kubectl`로 Kubernetes 명령을 실행합니다.

```sh
./scripts/local-k8s.sh kubectl get pods -A
```

사용 후 클러스터를 삭제합니다. 노드 내부의 영구 볼륨 데이터도 삭제됩니다.

```sh
./scripts/local-k8s.sh down
```

멀티 노드 설정과 문제 해결은 [사용 가이드](docs/local-k8s.md), GPU 준비는 [GPU 가이드](docs/gpu.md), Qwen3-0.6B 다운로드와 볼륨 마운트는 [모델 가이드](docs/models.md)를 참고하세요.

HTTP 서버 배포와 요청 형식은 [Transformers API 가이드](docs/transformers-api.md), 부하 생성과 결과 수집은 [AIPerf 클라이언트 가이드](docs/aiperf.md)를 참고하세요.

concurrency마다 새 서버를 시작하는 구성은 [Transformers API와 AIPerf 통합 실행](docs/transformers-api-aiperf.md)을 참고하세요.
