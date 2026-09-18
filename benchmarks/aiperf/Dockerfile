ARG BASE_IMAGE=python:3.12-slim-bookworm
FROM ${BASE_IMAGE}

# AIPerf 0.12 requires HF_HUB_OFFLINE unset for local tokenizer paths.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HOME=/tmp/huggingface \
    XDG_CACHE_HOME=/tmp/cache \
    MPLCONFIGDIR=/tmp/matplotlib \
    TOKENIZERS_PARALLELISM=false \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1

RUN python -m pip install --no-cache-dir --only-binary=:all: aiperf==0.12.0 \
    && python -m pip check

WORKDIR /tmp
USER 1000:1000
ENTRYPOINT ["aiperf"]
