"""Serve a local CUDA model through the chat API used by NVIDIA AIPerf."""

import argparse
import asyncio
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import threading
import time
from typing import Literal
from uuid import uuid4

import anyio
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextPart(APIModel):
    type: Literal["text"]
    text: str


class Message(APIModel):
    role: Literal["system", "user", "assistant"]
    content: str | list[TextPart]


class StreamOptions(APIModel):
    include_usage: bool = False


class ChatRequest(APIModel):
    model: str
    messages: list[Message] = Field(min_length=1)
    stream: bool = False
    stream_options: StreamOptions | None = None
    max_tokens: int | None = Field(default=None, gt=0, strict=True)
    max_completion_tokens: int | None = Field(default=None, gt=0, strict=True)
    temperature: float = Field(default=0, ge=0, le=2)
    top_p: float = Field(default=1, gt=0, le=1)
    n: Literal[1] = 1
    ignore_eos: bool = False

    @model_validator(mode="after")
    def check_options(self):
        if self.max_tokens is not None and self.max_completion_tokens is not None:
            raise ValueError("Specify only one output token limit.")
        if self.stream_options is not None and not self.stream:
            raise ValueError("stream_options requires stream=true.")
        return self


@dataclass(frozen=True)
class Settings:
    model: Path = Path("/model")
    served_model_name: str = "Qwen/Qwen3-0.6B"
    max_input_tokens: int = 2048
    max_output_tokens: int = 1024
    default_output_tokens: int = 128


@dataclass
class Result:
    text: str
    completion_tokens: int
    finish_reason: str


class TransformersEngine:
    def __init__(self, settings):
        import torch
        import torch.backends.python_native as python_native
        from transformers import AutoModelForCausalLM, AutoTokenizer

        if not settings.model.is_dir():
            raise ValueError(f"Model directory does not exist: {settings.model}")
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA GPU is required.")
        # Use prebuilt CUDA kernels so the image needs no runtime compiler.
        python_native.disable_dispatch_keys("CUDA")
        torch.set_num_threads(2)
        self.tokenizer = AutoTokenizer.from_pretrained(settings.model, local_files_only=True)
        self.model = AutoModelForCausalLM.from_pretrained(
            settings.model,
            local_files_only=True,
            use_safetensors=True,
            dtype=torch.float16,
            attn_implementation="sdpa",
        ).to("cuda").eval()
        self.settings = settings

    def prepare(self, body, output_tokens):
        messages = [
            {
                "role": message.role,
                "content": message.content if isinstance(message.content, str)
                else "".join(part.text for part in message.content),
            }
            for message in body.messages
        ]
        text = self.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True, enable_thinking=False
        )
        inputs = self.tokenizer(text, return_tensors="pt", add_special_tokens=False)
        prompt_tokens = inputs.input_ids.shape[1]
        if prompt_tokens > self.settings.max_input_tokens:
            raise ValueError("Input exceeds --max-input-tokens; prompts are not truncated.")
        if prompt_tokens + output_tokens > self.model.config.max_position_embeddings:
            raise ValueError("Input and output tokens exceed the model context length.")
        return inputs, prompt_tokens

    def generate(self, inputs, body, output_tokens, emit, cancelled):
        import torch
        from transformers import GenerationConfig, StoppingCriteria, TextStreamer

        class Cancelled(StoppingCriteria):
            def __call__(self, input_ids, scores, **kwargs):
                return cancelled.is_set()

        class Streamer(TextStreamer):
            def on_finalized_text(self, text, stream_end=False):
                if text:
                    emit(text)

        if cancelled.is_set():
            return Result("", 0, "stop")
        generation = GenerationConfig(
            max_new_tokens=output_tokens,
            do_sample=body.temperature > 0,
            temperature=body.temperature if body.temperature > 0 else 1.0,
            top_p=body.top_p if body.temperature > 0 else 1.0,
            top_k=50,
            use_cache=True,
            pad_token_id=self.tokenizer.pad_token_id,
            # None is filled from the model defaults by Transformers.
            eos_token_id=[] if body.ignore_eos else self.model.generation_config.eos_token_id,
        )
        streamer = Streamer(
            self.tokenizer, skip_prompt=True, skip_special_tokens=True,
            clean_up_tokenization_spaces=False,
        ) if body.stream else None
        with torch.inference_mode():
            outputs = self.model.generate(
                **inputs.to("cuda"), generation_config=generation,
                streamer=streamer, stopping_criteria=[Cancelled()],
            )
        tokens = outputs[0, inputs.input_ids.shape[1]:].tolist()
        eos = generation.eos_token_id
        eos_ids = {eos} if isinstance(eos, int) else set(eos or [])
        stopped = bool(tokens and tokens[-1] in eos_ids)
        return Result(
            self.tokenizer.decode(
                tokens, skip_special_tokens=True, clean_up_tokenization_spaces=False
            ),
            len(tokens), "stop" if stopped else "length",
        )


def error_body(message, error_type="invalid_request_error"):
    return {"error": {"message": message, "type": error_type, "param": None, "code": None}}


def usage(prompt_tokens, completion_tokens):
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def sse(payload):
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def create_app(settings=None, engine_factory=TransformersEngine):
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        app.state.engine = await asyncio.to_thread(engine_factory, settings)
        app.state.generation_lock = asyncio.Lock()
        yield
        del app.state.engine

    app = FastAPI(title="Transformers inference API", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse(error_body(str(exc)), status_code=400)

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.get("/readyz")
    async def ready():
        return {"status": "ready"}

    @app.get("/v1/models")
    async def models():
        return {"object": "list", "data": [{
            "id": settings.served_model_name, "object": "model",
            "created": 0, "owned_by": "local",
        }]}

    async def events(inputs, body, output_tokens):
        # Hold the GPU lock until the worker exits, including on client disconnect.
        async with app.state.generation_lock:
            loop = asyncio.get_running_loop()
            queue = asyncio.Queue()
            cancelled = threading.Event()

            def publish(kind, value):
                loop.call_soon_threadsafe(queue.put_nowait, (kind, value))

            def run():
                try:
                    result = app.state.engine.generate(
                        inputs, body, output_tokens,
                        lambda text: publish("text", text), cancelled,
                    )
                    publish("result", result)
                except Exception:
                    logging.exception("Generation failed")
                    publish("error", "Model generation failed. See server logs.")

            worker = asyncio.create_task(asyncio.to_thread(run))
            try:
                while True:
                    kind, value = await queue.get()
                    yield kind, value
                    if kind != "text":
                        break
            finally:
                cancelled.set()
                with anyio.CancelScope(shield=True):
                    await asyncio.shield(worker)

    @app.post("/v1/chat/completions")
    async def chat(body: ChatRequest, request: Request):
        if body.model != settings.served_model_name:
            return JSONResponse(error_body(f"Unknown model: {body.model}"), status_code=404)
        output_tokens = body.max_completion_tokens or body.max_tokens or settings.default_output_tokens
        if output_tokens > settings.max_output_tokens:
            return JSONResponse(error_body("Output exceeds --max-output-tokens."), status_code=400)
        try:
            inputs, prompt_tokens = await asyncio.to_thread(
                app.state.engine.prepare, body, output_tokens
            )
        except ValueError as exc:
            return JSONResponse(error_body(str(exc)), status_code=400)
        metadata = {
            "id": f"chatcmpl-{uuid4().hex}", "created": int(time.time()), "model": body.model,
        }

        if body.stream:
            async def stream():
                def chunk(delta, finish_reason=None):
                    return {
                        **metadata, "object": "chat.completion.chunk",
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
                    }

                yield sse(chunk({"role": "assistant", "content": ""}))
                async with aclosing(events(inputs, body, output_tokens)) as source:
                    async for kind, value in source:
                        if kind == "text":
                            yield sse(chunk({"content": value}))
                        elif kind == "error":
                            yield sse(error_body(value, "server_error"))
                            return
                        else:
                            yield sse(chunk({}, value.finish_reason))
                            if body.stream_options and body.stream_options.include_usage:
                                yield sse({
                                    **metadata, "object": "chat.completion.chunk", "choices": [],
                                    "usage": usage(prompt_tokens, value.completion_tokens),
                                })
                yield "data: [DONE]\n\n"

            return StreamingResponse(stream(), media_type="text/event-stream", headers={
                "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
            })

        async def complete():
            async with aclosing(events(inputs, body, output_tokens)) as source:
                async for kind, value in source:
                    if kind == "error":
                        return JSONResponse(error_body(value, "server_error"), status_code=500)
                    if kind == "result":
                        return {
                            **metadata, "object": "chat.completion",
                            "choices": [{"index": 0, "message": {"role": "assistant", "content": value.text},
                                         "finish_reason": value.finish_reason}],
                            "usage": usage(prompt_tokens, value.completion_tokens),
                        }

        task = asyncio.create_task(complete())
        try:
            while not task.done():
                if await request.is_disconnected():
                    return JSONResponse(error_body("Client disconnected."), status_code=499)
                await asyncio.wait({task}, timeout=0.1)
            return task.result()
        finally:
            if not task.done():
                task.cancel()
            with anyio.CancelScope(shield=True):
                await asyncio.gather(task, return_exceptions=True)

    return app


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be greater than zero")
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
    args = parser.parse_args()
    if args.default_output_tokens > args.max_output_tokens:
        parser.error("--default-output-tokens must not exceed --max-output-tokens")
    import uvicorn

    settings = Settings(**{name: getattr(args, name) for name in Settings.__dataclass_fields__})
    uvicorn.run(create_app(settings), host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
    main()
