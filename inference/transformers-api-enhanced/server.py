"""Serve a local CUDA model through the chat API used by NVIDIA AIPerf."""

import argparse
import logging
from pathlib import Path

from api import create_app
from settings import Settings


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def nonnegative_int(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=Path("/model"))
    parser.add_argument("--served-model-name", default="Qwen/Qwen3-0.6B")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=positive_int, default=8000)
    parser.add_argument("--max-input-tokens", type=positive_int, default=2048)
    parser.add_argument("--max-output-tokens", type=positive_int, default=1024)
    parser.add_argument("--default-output-tokens", type=positive_int, default=128)
    parser.add_argument("--max-batch-size", type=positive_int, default=4)
    parser.add_argument("--max-batch-tokens", type=positive_int, default=8192)
    parser.add_argument("--batch-wait-ms", type=nonnegative_int, default=5)
    args = parser.parse_args()
    if args.default_output_tokens > args.max_output_tokens:
        parser.error("--default-output-tokens must not exceed --max-output-tokens")
    import uvicorn

    settings = Settings(**{name: getattr(args, name) for name in Settings.__dataclass_fields__})
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(settings), host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
    main()
