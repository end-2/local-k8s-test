# AIPerf 클라이언트 사용법

AIPerf 호스트 CLI로 채팅 API에 부하를 보내고 지연 시간, 처리량과 토큰 사용량을 측정합니다.

접속 가능한 API 주소, 제공하는 모델 이름과 로컬 토크나이저 경로를 준비합니다. 서버 배포와 호스트 접속 방법은 [공통 API 가이드](transformers-api.md)를 참고합니다. CLI 옵션은 [NVIDIA AIPerf 문서](https://docs.nvidia.com/aiperf/reference/command-line-options)에 있습니다.

Kubernetes에서 서버와 함께 실행하는 방법은 [통합 실행 가이드](transformers-api-aiperf.md)를 참고합니다.

## 호스트 CLI로 실행

호스트에 Python 3.11부터 3.13 중 하나와 `venv`가 필요합니다. 다음 예제는 `http://127.0.0.1:8000`으로 접속합니다.

```sh
python3 -m venv .local-k8s/aiperf-venv
.local-k8s/aiperf-venv/bin/pip install aiperf==0.12.0

concurrency=1
run_dir=".local-k8s/aiperf/run-c${concurrency}-$(date -u +%Y%m%dT%H%M%SZ)"
.local-k8s/aiperf-venv/bin/aiperf profile \
  --model Qwen/Qwen3-0.6B \
  --tokenizer "$PWD/.models/Qwen3-0.6B" \
  --url http://127.0.0.1:8000 \
  --endpoint-type chat \
  --streaming \
  --use-server-token-count \
  --sequence-distribution '128,64:25;512,128:25;1024,256:25;2048,512:25' \
  --num-dataset-entries 256 \
  --random-seed 42 \
  --dataset-sampling-strategy sequential \
  --extra-inputs ignore_eos:true \
  --concurrency "$concurrency" \
  --warmup-request-count 8 \
  --warmup-concurrency 1 \
  --request-count 256 \
  --request-timeout-seconds 3600 \
  --no-server-metrics \
  --ui simple \
  --artifact-dir "$run_dir"
```

모델 경로를 바꿨으면 `--tokenizer`에도 같은 호스트 경로를 지정합니다. `chat` endpoint는 `/v1/chat/completions`를 붙이므로 `--url`에는 서버 주소만 지정합니다.

`concurrency`를 `1, 2, 4, 8, 16, 32, 64, 128` 중 하나로 지정하고 CLI를 실행합니다. 여러 측정이 같은 API에 동시에 부하를 보내지 않도록 순서대로 실행합니다.

## 측정 조건과 결과 해석

위 예제는 warmup 8건을 concurrency 1로 실행한 뒤 측정 요청 256건을 보냅니다. warmup은 측정 요청 수에 포함되지 않습니다. 최대 concurrency 128에서도 측정 요청 수가 동시 요청 수보다 많도록 설정합니다. 요청 하나의 제한 시간은 대기 시간을 포함해 1시간입니다.

[길이 분포 옵션](https://docs.nvidia.com/aiperf/tutorials/datasets-inputs/sequence-length-distributions-for-advanced-benchmarking)으로 다음 네 종류를 섞습니다. 비율은 데이터 생성 시 선택 확률이며 각 종류의 실제 요청 수가 정확히 같다는 뜻은 아닙니다.

| 프롬프트 목표 토큰 수 | 출력 목표 토큰 수 | 선택 확률 |
| --- | --- | --- |
| 128 | 64 | 25% |
| 512 | 128 | 25% |
| 1024 | 256 | 25% |
| 2048 | 512 | 25% |

합성 데이터 256개, random seed `42`와 순차 선택 방식을 공통으로 사용합니다. 프롬프트 길이는 합성 텍스트의 목표값이며 토큰화와 채팅 템플릿에 따라 응답의 `prompt_tokens`와 차이가 있을 수 있습니다.

길이 분포, 요청 수와 동시 요청 수는 CLI 옵션으로 변경합니다.

- `--streaming`을 제거하면 일반 JSON 응답을 측정합니다.
- `--use-server-token-count`는 응답에 포함된 토큰 사용량으로 집계합니다. 필드의 의미는 [API 응답 형식](transformers-api.md#채팅-요청과-json-응답)을 참고합니다.
- `--extra-inputs ignore_eos:true`는 각 요청의 출력 목표까지 EOS로 조기 종료하지 않도록 요청합니다. 목표는 요청마다 64, 128, 256, 512토큰 중 하나이며, 응답의 `completion_tokens`가 해당 요청의 목표와 같은지 확인합니다. 일반 응답 품질을 확인할 때는 생략합니다.
- `--no-server-metrics`는 서버 메트릭 수집을 끕니다.

TTFT와 ITL은 클라이언트가 수신한 스트리밍 텍스트를 기준으로 해석합니다. 응답 청크 하나에 여러 토큰이 들어갈 수 있으므로 GPU의 개별 토큰 연산 시간을 나타내지 않습니다. 측정 조건을 비교할 때는 모델, 입력과 출력 길이, 동시 요청 수와 클라이언트 자원을 함께 기록합니다.

## 결과 확인

결과는 `--artifact-dir`로 지정한 호스트 디렉터리에 저장됩니다.

요약은 `profile_export_aiperf.json`과 `profile_export_aiperf.csv`, 개별 요청은 `profile_export.jsonl`, 실행 로그는 `logs/aiperf.log`에서 확인합니다. 성공한 측정 요청 256건, 실패 0건과 출력 길이 분포를 확인합니다. 오류나 타임아웃으로 요청이 누락된 실행은 전체 workload를 완료한 결과로 취급하지 않습니다.

## 문제 해결

접속 실패는 `--url`과 port-forward 상태를 확인합니다. 토크나이저 로딩 실패는 `--tokenizer` 경로와 파일 권한, 결과 저장 실패는 `--artifact-dir` 쓰기 권한과 디스크 여유 공간을 확인합니다. 상세 오류는 결과 디렉터리의 `logs/aiperf.log`에 기록됩니다.
