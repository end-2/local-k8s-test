# Transformers API 공통 사용법

[base](../inference/transformers-api-base/)와 [enhanced](../inference/transformers-api-enhanced/)는 로컬 Qwen3-0.6B 모델을 사용하는 OpenAI 호환 채팅 API입니다. 두 구현은 같은 요청 형식, JSON 응답과 SSE 스트리밍을 지원합니다. 모델과 클러스터는 [모델 가이드](models.md)를 따라 준비합니다.

이 문서는 공통 실행 방법과 API 형식을 다룹니다. 구현별 구조와 설정은 [enhanced 가이드](transformers-api-enhanced.md), 부하 생성과 측정 결과 수집은 [AIPerf 클라이언트 가이드](aiperf.md)를 참고합니다.

## 이미지 빌드와 실행

저장소 루트에서 `api_variant`를 `base` 또는 `enhanced`로 지정합니다. 각 디렉터리의 Dockerfile과 requirements.txt에 의존성 버전이 고정되어 있습니다.

```sh
api_variant=base
IMAGE_TAG=0.1.0 ./scripts/build-inference-images.sh "$api_variant"
IMAGE_TAG=0.1.0 ./scripts/load-inference-images.sh "$api_variant"
```

`--network=host`는 빌드 중 패키지 다운로드에 사용합니다. 이미지는 압축 해제 후 약 10GiB를 사용합니다. 처음 빌드하고 로드할 때는 Docker 저장소에 약 25GiB 이상의 여유 공간을 준비합니다. 이미지 로드용 임시 아카이브는 `.local-k8s/image-tmp`에 저장하고 완료 후 삭제합니다.

완성된 이미지 아카이브가 있으면 빌드와 `load-image` 대신 다음 명령을 사용합니다.

```sh
./scripts/local-k8s.sh load-archive .local-k8s/images/transformers-api-0.1.0.tar
```

### 배포와 구현 전환

[base](../config/serving/transformers-api-base/app.yaml)와 [enhanced](../config/serving/transformers-api-enhanced/app.yaml)는 각각 GPU 1개와 읽기 전용 모델 볼륨을 사용합니다. 구현을 전환하려면 위의 `api_variant`를 바꾸고 해당 이미지를 빌드하고 로드합니다.

기존 port-forward를 종료한 뒤 아래 명령을 실행합니다. 이름이 `transformers-api`인 서버를 포함한 API Deployment와 Service를 정리하고, GPU를 반환하도록 Pod 종료를 기다린 뒤 선택한 구현을 배포합니다. 리소스가 없는 최초 배포에도 사용할 수 있으며, 전환 중에는 API가 중단됩니다.

```sh
./scripts/local-k8s.sh kubectl delete deployment,service \
  transformers-api transformers-api-base transformers-api-enhanced \
  --ignore-not-found --cascade=foreground --wait=true --timeout=180s &&
./scripts/local-k8s.sh kubectl apply -f config/serving/transformers-api-"$api_variant"/app.yaml &&
./scripts/local-k8s.sh kubectl rollout status deployment/transformers-api-"$api_variant" --timeout=300s &&
./scripts/local-k8s.sh kubectl port-forward service/transformers-api-"$api_variant" 8000:8000
```

### 동일 구현의 코드 갱신

이미 배포한 구현의 코드를 같은 이미지 태그로 갱신할 때는 해당 `api_variant`로 이미지를 빌드하고 로드합니다. 기존 port-forward를 종료한 뒤 Deployment를 재시작하고 port-forward를 다시 실행합니다.

```sh
./scripts/local-k8s.sh kubectl rollout restart deployment/transformers-api-"$api_variant" &&
./scripts/local-k8s.sh kubectl rollout status deployment/transformers-api-"$api_variant" --timeout=300s &&
./scripts/local-k8s.sh kubectl port-forward service/transformers-api-"$api_variant" 8000:8000
```

## 엔드포인트

| 경로 | 용도 |
| --- | --- |
| `GET /healthz` | HTTP 서버 생존 확인 |
| `GET /readyz` | 모델 로딩이 끝난 서버의 준비 상태 |
| `GET /v1/models` | 제공하는 모델 이름 |
| `POST /v1/chat/completions` | 텍스트 채팅 생성 |
| `GET /docs` | 요청 스키마와 지원 필드 |

port-forward를 실행한 상태에서 다른 터미널로 확인합니다.

```sh
curl --fail http://127.0.0.1:8000/readyz
curl --fail http://127.0.0.1:8000/v1/models
```

## 채팅 요청과 JSON 응답

```sh
curl --fail http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"대한민국의 수도는 어디인가요? 한 문장으로 답하세요."}],"max_completion_tokens":64}'
```

| 필드 | 용도 |
| --- | --- |
| `model` | `/v1/models`에서 확인한 모델 이름 |
| `messages` | `system`, `user`, `assistant` 역할의 메시지 목록. `content`는 문자열 또는 `type: text` 콘텐츠 배열 |
| `max_tokens`, `max_completion_tokens` | 출력 토큰 상한. 둘 중 하나만 지정 |
| `temperature`, `top_p` | 샘플링 설정. `temperature: 0`은 greedy 디코딩 |
| `n` | 생성할 응답 수. `1`만 지원 |
| `ignore_eos` | EOS에 의한 조기 종료를 무시할지 여부 |
| `stream` | SSE 스트리밍 사용 여부 |
| `stream_options.include_usage` | 스트리밍 응답 끝에 토큰 사용량 포함. `stream: true`일 때만 지정 |

JSON 응답 형식 예시입니다. ID, 시각, 텍스트와 토큰 수는 요청마다 달라집니다.

```json
{
  "id": "chatcmpl-example",
  "created": 0,
  "model": "Qwen/Qwen3-0.6B",
  "object": "chat.completion",
  "choices": [{
    "index": 0,
    "message": {"role": "assistant", "content": "서울입니다."},
    "finish_reason": "stop"
  }],
  "usage": {"prompt_tokens": 31, "completion_tokens": 6, "total_tokens": 37}
}
```

`finish_reason`은 EOS로 끝나면 `stop`, 출력 토큰 상한에 도달하면 `length`입니다. `usage`의 입력 토큰 수에는 채팅 템플릿이 포함되며, 출력 토큰 수에는 생성된 특수 토큰도 포함됩니다.

## 스트리밍 응답

```sh
curl --fail -N http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"대한민국의 수도는 어디인가요?"}],"max_completion_tokens":64,"stream":true,"stream_options":{"include_usage":true}}'
```

응답의 Content-Type은 `text/event-stream`입니다. 각 `data:` 프레임은 빈 줄로 구분하며, 정상 응답은 다음 순서로 전달합니다.

1. `choices[0].delta`에 `role: assistant`와 빈 `content`를 포함한 시작 청크
2. `choices[0].delta.content`에 생성된 텍스트를 담은 청크
3. 빈 `delta`와 `finish_reason`을 담은 종료 청크
4. `include_usage: true`일 때 `choices: []`와 `usage`를 담은 사용량 청크
5. `data: [DONE]`

JSON 청크의 `object`는 `chat.completion.chunk`이며, 같은 요청의 청크는 동일한 `id`, `created`, `model`을 사용합니다. 한 텍스트 청크에 여러 토큰이 포함될 수 있습니다.

## 오류와 공통 설정

오류 응답은 `error.message`, `error.type`, `error.param`, `error.code`를 포함합니다. 요청 검증 오류와 입력 및 출력 제한 초과는 HTTP 400, 알려지지 않은 모델은 404, 일반 응답의 생성 실패는 500으로 반환합니다. 스트리밍 시작 이후 생성이 실패하면 오류 프레임을 보내고 `[DONE]` 없이 종료합니다.

도구 호출, 이미지, `stop` 등 지원하지 않는 필드는 400 오류로 반환합니다. 입력을 자동으로 자르지 않습니다.

두 구현은 `--model`, `--served-model-name`, `--host`, `--port`, `--max-input-tokens`, `--max-output-tokens`, `--default-output-tokens`를 지원합니다. 기본값은 실행할 이미지의 도움말로 확인하고 Deployment의 `args`에서 조정합니다.

```sh
docker run --rm --network=none local/transformers-api-"$api_variant":0.1.0 --help
```

모델은 오프라인으로 로드합니다. 로컬 테스트용 API이며 인증은 제공하지 않습니다. 클라이언트 연결이 끊기면 해당 요청을 취소합니다.

## 검증과 종료

실행 중인 서버의 EOS 처리, 출력 길이, JSON과 SSE 응답 및 토큰 사용량을 검증합니다. 다른 주소는 `API_SERVER_URL` 환경 변수로 지정합니다.

```sh
python3 -m venv .local-k8s/api-test-venv
.local-k8s/api-test-venv/bin/pip install httpx==0.28.1
.local-k8s/api-test-venv/bin/python tests/test-transformers-api-gpu.py
```

Pod가 `Pending`이면 GPU 자원 할당과 모델 볼륨 설정을 확인하고, 로딩이나 생성 오류는 서버 로그를 확인합니다. 사용 후 port-forward를 종료하고 Deployment와 Service를 삭제하면 GPU가 반환됩니다.

```sh
./scripts/local-k8s.sh kubectl logs deployment/transformers-api-"$api_variant"
./scripts/local-k8s.sh kubectl delete -f config/serving/transformers-api-"$api_variant"/app.yaml
```
