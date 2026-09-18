"""Validate chat requests and expose JSON and SSE responses through FastAPI."""

import asyncio
from contextlib import aclosing, asynccontextmanager
import json
import time
from typing import Literal
from uuid import uuid4

import anyio
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from contracts import GenerationRequest
from engine import InferenceEngine
from settings import Settings


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


def create_app(settings=None, engine_factory=InferenceEngine):
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        engine = await asyncio.to_thread(engine_factory, settings)
        app.state.engine = engine
        await engine.start()
        try:
            yield
        finally:
            await engine.close()
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

    @app.post("/v1/chat/completions")
    async def chat(body: ChatRequest, request: Request):
        if body.model != settings.served_model_name:
            return JSONResponse(error_body(f"Unknown model: {body.model}"), status_code=404)
        output_tokens = body.max_completion_tokens or body.max_tokens or settings.default_output_tokens
        if output_tokens > settings.max_output_tokens:
            return JSONResponse(error_body("Output exceeds --max-output-tokens."), status_code=400)
        generation = GenerationRequest(
            messages=[
                {
                    "role": message.role,
                    "content": message.content if isinstance(message.content, str)
                    else "".join(part.text for part in message.content),
                }
                for message in body.messages
            ],
            output_tokens=output_tokens,
            temperature=body.temperature,
            top_p=body.top_p,
            ignore_eos=body.ignore_eos,
            stream=body.stream,
        )
        try:
            prepared = await app.state.engine.prepare(generation)
        except ValueError as exc:
            return JSONResponse(error_body(str(exc)), status_code=400)
        prompt_tokens = prepared.prompt_tokens
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
                async with aclosing(app.state.engine.events(prepared)) as source:
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
            async with aclosing(app.state.engine.events(prepared)) as source:
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
