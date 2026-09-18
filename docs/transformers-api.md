# AIPerf용 Transformers API 서버

[API 서버](../inference/transformers-api/server.py)는 Qwen3-0.6B를 GPU에 한 번 로드하고 OpenAI 호환 채팅 요청을 처리합니다. NVIDIA AIPerf의 `chat` endpoint, 일반 JSON 응답과 SSE 스트리밍을 지원합니다. 모델과 클러스터는 [모델 가이드](models.md)를 따라 준비합니다.

## 이미지 빌드와 실행

저장소 루트에서 실행합니다. [Dockerfile](../inference/transformers-api/Dockerfile)은 Python slim 이미지에 CUDA용 PyTorch, Transformers, FastAPI와 Uvicorn을 설치합니다. CUDA와 추론 라이브러리 버전은 Dockerfile, 웹 서버 패키지 버전은 [requirements.txt](../inference/transformers-api/requirements.txt)에 고정합니다. 모델은 읽기 전용 볼륨에서 로드합니다.

```sh
docker build --network=host -t local/transformers-api:0.1.0 inference/transformers-api
mkdir -p .local-k8s/image-tmp
TMPDIR="$PWD/.local-k8s/image-tmp" ./scripts/local-k8s.sh load-image local/transformers-api:0.1.0
./scripts/local-k8s.sh kubectl apply -f config/transformers-api.yaml
./scripts/local-k8s.sh kubectl rollout status deployment/transformers-api --timeout=300s
./scripts/local-k8s.sh kubectl port-forward service/transformers-api 8000:8000
```

`--network=host`는 빌드 중 패키지 다운로드에 사용합니다. 이미지는 압축 해제 후 약 10GiB를 사용합니다. Docker와 kind가 이미지를 각각 보관하므로 처음 빌드하고 로드할 때는 Docker 저장소에 약 25GiB 이상의 여유 공간을 준비합니다. 이미지 로드용 임시 아카이브는 `.local-k8s/image-tmp`에 저장하고 완료 후 삭제합니다.

[Deployment와 Service](../config/transformers-api.yaml)는 GPU 1개와 읽기 전용 모델 볼륨을 사용합니다. 서버가 실행되는 동안 GPU를 점유합니다. `Recreate` 전략으로 업데이트 시 기존 Pod를 먼저 종료합니다. 같은 태그로 이미지를 수정하면 빌드와 로드 후 `kubectl rollout restart deployment/transformers-api`를 저장소의 `local-k8s.sh` 래퍼로 실행합니다.

다른 터미널에서 확인합니다.

```sh
curl --fail http://127.0.0.1:8000/v1/models
curl --fail -N http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"대한민국의 수도는 어디인가요?"}],"max_completion_tokens":64,"stream":true,"stream_options":{"include_usage":true}}'
```

완성된 API 이미지 아카이브가 있으면 빌드와 `load-image` 대신 다음 명령을 사용한 뒤 Deployment를 적용합니다.

```sh
./scripts/local-k8s.sh load-archive .local-k8s/images/transformers-api-0.1.0.tar
```

빌드한 이미지를 아카이브로 보관하려면 다음 명령을 사용합니다.

```sh
mkdir -p .local-k8s/images
docker image save --platform linux/amd64 \
  --output .local-k8s/images/transformers-api-0.1.0.tar \
  local/transformers-api:0.1.0
```

## AIPerf 실행

클러스터 내부의 별도 Job으로 실행하려면 [Kubernetes AIPerf 가이드](aiperf.md)를 따릅니다. 아래는 호스트에서 port-forward로 접속하는 방법입니다.

호스트에 Python 3.11부터 3.13 중 하나와 `venv`가 필요합니다. 벤치마크 도구는 추론 이미지와 별도로 설치합니다.

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
  --artifact-dir .local-k8s/aiperf/transformers-api
```

모델 경로를 바꿨으면 `--tokenizer`에도 같은 호스트 경로를 지정합니다. AIPerf는 URL에 `/v1/chat/completions`를 붙이므로 `--url`에는 서버 주소만 지정합니다. CLI 옵션은 [NVIDIA AIPerf 문서](https://docs.nvidia.com/aiperf/reference/command-line-options)를 참고합니다.

`ignore_eos:true`는 EOS 조기 종료를 막아 지정한 출력 길이로 부하를 생성합니다. 일반 응답 품질을 확인할 때는 생략합니다. 서버 토큰 수에는 채팅 템플릿과 생성된 특수 토큰이 포함됩니다. `--streaming`을 제거하면 일반 JSON 응답을 측정합니다. Prometheus `/metrics`는 제공하지 않으므로 수집을 비활성화합니다.

이 서버는 모델 생성 작업을 한 요청씩 처리합니다. `--concurrency`를 높이면 GPU 대기 시간이 지연 시간에 포함되며, continuous batching은 지원하지 않습니다. FP16, SDPA, thinking 비활성화를 사용하고 기본 디코딩은 greedy입니다. 스트리밍은 Transformers `TextStreamer`가 단어 단위로 디코딩한 문자열을 즉시 전송합니다. 한 SSE 청크에 여러 토큰이 포함될 수 있으므로 TTFT와 ITL은 클라이언트가 관측한 텍스트 도착 기준이며 GPU의 개별 토큰 생성 시각과 다릅니다. [Transformers 스트리머 문서](https://huggingface.co/docs/transformers/internal/generation_utils#transformers.TextStreamer)

## API와 설정

| 경로 | 용도 |
| --- | --- |
| `GET /healthz` | HTTP 서버 생존 확인 |
| `GET /readyz` | 모델 로딩이 끝난 서버의 준비 상태 |
| `GET /v1/models` | 제공하는 모델 이름 |
| `POST /v1/chat/completions` | 텍스트 채팅 생성 |
| `GET /docs` | 요청 스키마와 지원 필드 |

`messages`는 `system`, `user`, `assistant` 역할과 문자열 또는 `type: text` 콘텐츠 배열을 받습니다. 출력 제한은 `max_tokens`와 `max_completion_tokens` 중 하나를 지정합니다. `temperature`, `top_p`, `n: 1`, `ignore_eos`, `stream`, `stream_options.include_usage`를 지원합니다. 도구 호출, 이미지, `stop` 등 지원하지 않는 필드는 400 오류로 반환합니다. 알려지지 않은 모델은 404로 반환합니다.

기본 입력 상한은 2,048토큰, 출력 상한은 1,024토큰이고, 출력 제한을 생략하면 128토큰입니다. 입력을 자르지 않으며 입력 상한이나 모델의 전체 컨텍스트 길이를 넘으면 400 오류를 반환합니다. CLI 옵션은 다음 명령으로 확인하고 Deployment의 `args`에서 조정합니다.

```sh
docker run --rm --network=none local/transformers-api:0.1.0 --help
```

모델은 오프라인으로 로드하며 Uvicorn worker는 하나만 사용합니다. 클라이언트 연결이 끊기면 진행 중인 생성은 다음 중단 확인 지점에서 멈추고 대기 중인 요청은 큐에서 빠집니다. 서버는 로컬 벤치마크용으로 인증을 제공하지 않습니다.

## 검증과 종료

GPU 없이 API 형식, 스트리밍, 오류 처리, 동시 요청 직렬화와 연결 종료를 검증합니다.

```sh
python3 -m venv .local-k8s/api-test-venv
.local-k8s/api-test-venv/bin/pip install -r inference/transformers-api/requirements.txt httpx==0.28.1
PYTHONDONTWRITEBYTECODE=1 .local-k8s/api-test-venv/bin/python tests/test-transformers-api.py
```

실행 중인 기본 Qwen3 서버의 EOS 처리, 고정 출력 길이와 일반 응답 및 스트리밍 일치를 확인합니다. 다른 포트는 `API_SERVER_URL` 환경 변수로 지정합니다.

```sh
.local-k8s/api-test-venv/bin/python tests/test-transformers-api-gpu.py
```

성능 측정은 위의 AIPerf 명령으로 확인합니다. `Pending`이면 다른 워크로드의 GPU 점유 여부를 확인하고, 로딩이나 생성 오류는 서버 로그를 확인합니다.

```sh
./scripts/local-k8s.sh kubectl logs deployment/transformers-api
./scripts/local-k8s.sh kubectl delete -f config/transformers-api.yaml
```

사용 후 port-forward를 종료하고 Deployment와 Service를 삭제하면 GPU가 반환됩니다.
