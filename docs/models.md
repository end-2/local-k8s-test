# 로컬 모델 볼륨

Qwen3-0.6B를 호스트에 내려받고 GPU 노드와 Pod에 읽기 전용으로 마운트합니다. 기존 Docker, kind, kubectl 외에 다운로드용 `curl`과 `sha256sum`만 사용합니다. Hugging Face CLI, Git LFS, 별도 스토리지 서비스는 필요하지 않습니다.

## 다운로드와 연결

저장소 루트에서 실행합니다. 모델 파일에 약 1.5GB의 디스크 공간이 필요합니다. 모델 리비전은 [모델 설정](../config/models/qwen3-0.6b.env), 파일별 SHA-256은 [체크섬 목록](../config/models/qwen3-0.6b.sha256)에 고정되어 있습니다.

```sh
./scripts/download-model.sh
./scripts/local-k8s.sh up
```

다운로드는 임시 디렉터리에 저장하고 파일을 검증한 뒤 `Qwen3-0.6B/`로 옮깁니다. 중단되면 같은 명령으로 이어받습니다. 완료된 모델이 있으면 네트워크 접근 없이 체크섬만 검증합니다.

| 위치 | 경로 |
| --- | --- |
| 호스트 | `.models/Qwen3-0.6B/` |
| GPU가 연결된 kind 노드 | `/models/Qwen3-0.6B/` |
| 아래 예제의 Pod | `/model/` |

`up`은 기존 Docker 래퍼를 통해 GPU 노드에만 모델 디렉터리를 bind mount합니다. 멀티 노드에서는 GPU가 연결된 첫 번째 worker에 마운트합니다. Pod의 `hostPath`는 kind 노드 내부 경로를 가리킵니다. [kind 호스트 볼륨 안내](https://kind.sigs.k8s.io/docs/user/configuration/#extra-mounts), [Kubernetes hostPath 안내](https://kubernetes.io/docs/concepts/storage/volumes/#hostpath)

기존 클러스터에 마운트가 없거나 호스트 경로가 달라지면 `up`이 오류를 출력합니다. 필요한 노드 데이터와 PVC를 백업한 뒤 클러스터를 재생성합니다. 호스트의 `.models/`는 삭제되지 않습니다.

```sh
./scripts/local-k8s.sh down
./scripts/local-k8s.sh up
```

다른 디스크를 쓰려면 다운로드와 클러스터 생성에 같은 경로를 지정합니다. 이후 `up`에도 같은 값을 사용합니다. 상대 경로는 명령 실행 디렉터리 기준이며, 경로에 콜론은 사용할 수 없습니다.

```sh
export LOCAL_K8S_MODELS_DIR=/path/to/models
./scripts/download-model.sh
./scripts/local-k8s.sh up
```

## 마운트 검증

[검증 Job](../config/qwen3-model-check.yaml)은 일반 사용자 권한으로 모든 모델 파일의 SHA-256과 읽기 전용 마운트를 확인합니다. 추론 라이브러리와 GPU 할당 없이 실행하며, 완료 후 5분 뒤 삭제됩니다. 이미지가 없으면 처음 실행할 때 다운로드합니다.

```sh
job=$(./scripts/local-k8s.sh kubectl create -f config/qwen3-model-check.yaml -o name)
./scripts/local-k8s.sh kubectl wait --for=condition=Complete "$job" --timeout=180s
./scripts/local-k8s.sh kubectl logs "$job"
```

## 추론 Pod에서 사용

[Transformers 추론 가이드](transformers.md)에 Python 스크립트, 이미지 빌드와 실행 방법이 있습니다. 볼륨과 GPU 할당은 [추론 Job](../config/transformers-inference.yaml)을 참고합니다. 모델 ID 대신 `/model` 경로를 지정하고 FP16으로 로드합니다.

모델 볼륨은 읽기 전용입니다. 런타임 캐시와 출력은 `/tmp`나 별도 쓰기 가능한 볼륨에 저장합니다. 모델 파일과 토크나이저는 [공식 모델 저장소](https://huggingface.co/Qwen/Qwen3-0.6B)에서 가져옵니다.

## 문제 해결

- `hostPath type check failed`: 다운로드 완료 여부, GPU 노드 선택과 `/models` 마운트를 확인합니다. `Directory`를 사용하므로 잘못된 경로를 빈 디렉터리로 생성하지 않습니다.
- `Permission denied`: 모델 디렉터리에 탐색 권한, 파일에 읽기 권한이 있어야 합니다. 다운로드 스크립트는 일반 사용자도 읽을 수 있는 권한으로 생성합니다.
- 다운로드 잠금 오류: 실행 중인 다운로드가 없다면 오류에 표시된 빈 `.lock` 디렉터리를 `rmdir`로 지우고 다시 실행합니다.
- 체크섬 불일치: 완료된 모델 경로를 사용하는 Pod를 먼저 종료하고 해당 디렉터리를 옮긴 뒤 다시 다운로드합니다.
