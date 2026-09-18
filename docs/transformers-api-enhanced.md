# Transformers API enhanced 구현

[enhanced](../inference/transformers-api-enhanced/)는 요청 단위 동적 배치를 사용하는 서버입니다. 빌드, 실행과 HTTP 요청 및 응답은 [공통 API 가이드](transformers-api.md)를 따릅니다.

## 코드 구조

- [server.py](../inference/transformers-api-enhanced/server.py): CLI 옵션을 읽고 Uvicorn을 실행합니다.
- [api.py](../inference/transformers-api-enhanced/api.py): 요청 검증, HTTP 엔드포인트, JSON과 SSE 응답, 연결 종료 감지를 담당합니다.
- [engine.py](../inference/transformers-api-enhanced/engine.py): 요청 등록, 스케줄러와 워커 실행 루프, 결과 전달, 취소와 종료 대기를 연결합니다.
- [scheduler.py](../inference/transformers-api-enhanced/scheduler.py): 대기 요청을 생성 옵션, 배치 크기와 토큰 예산에 따라 묶고 대기 및 실행 상태를 관리합니다.
- [worker.py](../inference/transformers-api-enhanced/worker.py): 초기화된 모델로 배치 하나를 `generate()` 한 번에 실행하고 요청별 텍스트와 결과를 반환합니다.
- [model_runtime.py](../inference/transformers-api-enhanced/model_runtime.py): CUDA와 Transformers 초기화, 모델과 토크나이저 로딩, 프롬프트 토큰화와 컨텍스트 길이 검사를 담당합니다.
- [contracts.py](../inference/transformers-api-enhanced/contracts.py): 계층 간 요청, 준비된 입력과 결과 형식을 정의합니다.
- [settings.py](../inference/transformers-api-enhanced/settings.py): 모델 경로, 요청 제한과 배치 설정을 정의합니다.

API는 검증한 요청을 엔진에 전달합니다. 엔진은 모델 런타임으로 입력을 준비하고, 스케줄러가 선택한 배치를 워커에 넘깁니다. 스케줄러는 모델이나 HTTP 계층에 의존하지 않으며, 워커는 모델 로딩과 요청 대기열을 관리하지 않습니다.

## 배치 스케줄링

스케줄러는 가장 오래 기다린 요청을 기준으로 `temperature`, 유효한 `top_p`, `ignore_eos`가 같은 요청을 묶습니다. Greedy 디코딩에서는 `top_p` 차이를 무시합니다. 스트리밍 여부와 출력 길이가 다른 요청도 같은 배치에 들어갈 수 있으며, 요청별 출력 제한과 EOS 종료를 적용합니다.

| 옵션 | 용도 |
| --- | --- |
| `--max-batch-size` | 한 번에 실행할 최대 요청 수 |
| `--max-batch-tokens` | `요청 수 × (최대 입력 길이 + 최대 출력 제한)`으로 계산한 배치 토큰 예산 |
| `--batch-wait-ms` | 함께 도착하는 요청을 모으는 대기 시간, 0이면 추가 대기 없음 |

기본값은 [Settings](../inference/transformers-api-enhanced/settings.py)를 따릅니다. 토큰 예산은 왼쪽 패딩과 생성 중 KV 캐시 크기를 고려한 상한이며 GPU 메모리 바이트 단위의 한도는 아닙니다. 단일 요청이 예산을 넘으면 400 오류를 반환합니다. `--max-batch-size 1 --batch-wait-ms 0`으로 한 요청씩 처리하는 기준 성능을 측정할 수 있습니다.

배치가 실행되는 동안 새 요청은 다음 배치를 기다립니다. 토큰 단계 continuous batching은 지원하지 않습니다. 모델은 FP16, SDPA와 thinking 비활성화를 사용합니다.

## 스트리밍과 취소

배치의 각 요청은 별도 Transformers `TextStreamer`로 텍스트를 디코딩합니다. 한 SSE 청크에 여러 토큰이 포함될 수 있습니다. [Transformers 스트리머 문서](https://huggingface.co/docs/transformers/internal/generation_utils#transformers.TextStreamer)

클라이언트 연결이 끊기면 해당 요청만 중단하고 대기 중인 요청은 큐에서 제거합니다. 같은 배치의 다른 요청은 계속 실행합니다. 완료되거나 취소된 행의 연산 슬롯은 `generate()`가 반환할 때 해제하며, 다음 배치는 그 이후에 실행합니다. 서버 종료 시 대기 요청과 실행 요청을 취소하고 워커가 반환할 때까지 기다립니다.

## 구현 테스트

GPU 없이 API 형식, 스트리밍, 오류 처리, 배치 구성과 예산, 요청별 취소와 엔진 종료를 검증합니다.

```sh
python3 -m venv .local-k8s/api-test-venv
.local-k8s/api-test-venv/bin/pip install -r inference/transformers-api-enhanced/requirements.txt httpx==0.28.1
PYTHONDONTWRITEBYTECODE=1 .local-k8s/api-test-venv/bin/python tests/test-transformers-api.py
PYTHONDONTWRITEBYTECODE=1 .local-k8s/api-test-venv/bin/python tests/test-transformers-scheduler.py
```

enhanced 이미지에 설치된 Transformers와 작은 CPU 모델로 실제 배치 생성, 패딩, EOS, 출력 길이와 취소를 검증합니다.

```sh
docker run --rm --network=none --entrypoint python \
  -v "$PWD:/workspace:ro" local/transformers-api:0.1.0 \
  /workspace/tests/test-transformers-worker.py
```
