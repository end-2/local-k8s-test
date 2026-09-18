ARG BASE_IMAGE=python:3.12-slim-bookworm
FROM ${BASE_IMAGE}

USER 0

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HUB_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HOME=/tmp/huggingface \
    TOKENIZERS_PARALLELISM=false \
    OMP_NUM_THREADS=2

# PyTorch wheels supply CUDA libraries; no separate CUDA toolkit is needed.
RUN python -m pip install --no-cache-dir --only-binary=:all: \
        torch==2.14.0 --index-url https://download.pytorch.org/whl/cu126 \
    && python -m pip install --no-cache-dir --only-binary=:all: transformers==5.17.0 \
    && python -m pip check \
    && python -c "from transformers import AutoModelForCausalLM, AutoTokenizer; import torch; assert torch.version.cuda == '12.6'"

WORKDIR /app
COPY infer.py /app/infer.py
USER 1000:1000
ENTRYPOINT ["python", "/app/infer.py"]
