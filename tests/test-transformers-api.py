"""Exercise the HTTP contract without loading a model or requiring CUDA."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import socket
import sys
import threading
import time
import unittest

import httpx
from fastapi.testclient import TestClient
import uvicorn


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "inference_api", ROOT / "inference/transformers-api/server.py"
)
api = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = api
spec.loader.exec_module(api)


class Engine:
    def __init__(self, settings):
        self.active = 0
        self.peak = 0
        self.calls = 0
        self.cancelled = threading.Event()
        self.started = threading.Event()

    def prepare(self, body, output_tokens):
        if body.messages[0].content == "too long":
            raise ValueError("Input exceeds the context length.")
        return None, 7

    def generate(self, inputs, body, output_tokens, emit, cancelled):
        self.calls += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.started.set()
        try:
            if body.messages[0].content == "fail":
                raise RuntimeError("backend failed")
            if body.messages[0].content == "wait":
                emit("first ")
                if cancelled.wait(5):
                    self.cancelled.set()
                else:
                    raise RuntimeError("Client disconnect did not cancel generation")
            else:
                time.sleep(0.03)
                emit("서울")
                emit("입니다.")
            return api.Result("서울입니다.", 3, "stop")
        finally:
            self.active -= 1


def payload(**kwargs):
    return {"model": "Qwen/Qwen3-0.6B", "messages": [
        {"role": "user", "content": "hello"}
    ], **kwargs}


def chunks(response):
    return [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.app = api.create_app(engine_factory=Engine)
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def test_models_and_health(self):
        for endpoint in ("/healthz", "/readyz"):
            self.assertEqual(self.client.get(endpoint).status_code, 200)
        self.assertEqual(self.client.get("/v1/models").json()["data"][0]["id"], payload()["model"])

    def test_non_streaming_and_both_token_limit_names(self):
        for field in ("max_tokens", "max_completion_tokens"):
            response = self.client.post("/v1/chat/completions", json=payload(**{field: 8}))
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["object"], "chat.completion")
            self.assertEqual(data["choices"][0]["message"]["content"], "서울입니다.")
            self.assertEqual(data["usage"], {
                "prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10
            })

    def test_streaming_content_finish_usage_and_done(self):
        response = self.client.post("/v1/chat/completions", json=payload(
            stream=True, max_completion_tokens=8, stream_options={"include_usage": True}
        ))
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        frames = chunks(response)
        self.assertEqual(frames[-1], "[DONE]")
        data = [json.loads(frame) for frame in frames[:-1]]
        self.assertEqual(len({frame["id"] for frame in data}), 1)
        self.assertTrue(all(frame["object"] == "chat.completion.chunk" for frame in data))
        self.assertEqual("".join(
            choice["delta"].get("content", "") for frame in data for choice in frame["choices"]
        ), "서울입니다.")
        self.assertEqual(data[-2]["choices"][0]["finish_reason"], "stop")
        self.assertEqual(data[-1]["choices"], [])
        self.assertEqual(data[-1]["usage"]["completion_tokens"], 3)

    def test_stream_without_usage(self):
        response = self.client.post("/v1/chat/completions", json=payload(stream=True))
        self.assertNotIn('"usage"', response.text)
        self.assertEqual(chunks(response)[-1], "[DONE]")

    def test_invalid_requests(self):
        invalid = [
            {"messages": []}, {"max_tokens": 0}, {"max_tokens": True},
            {"max_tokens": 1025}, {"max_tokens": 8, "max_completion_tokens": 8},
            {"temperature": -1}, {"top_p": 0}, {"n": 2}, {"stop": "unsupported"},
            {"stream_options": {"include_usage": True}},
            {"messages": [{"role": "user", "content": "too long"}]},
            {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {}}]}]},
        ]
        for extra in invalid:
            with self.subTest(extra=extra):
                response = self.client.post("/v1/chat/completions", json=payload(**extra))
                self.assertEqual(response.status_code, 400)
                self.assertIn("message", response.json()["error"])
        self.assertEqual(self.client.post(
            "/v1/chat/completions", json=payload(model="missing")
        ).status_code, 404)

    def test_aiperf_text_content_parts(self):
        response = self.client.post("/v1/chat/completions", json=payload(messages=[{
            "role": "user", "content": [{"type": "text", "text": "hello"}]
        }]))
        self.assertEqual(response.status_code, 200)

    def test_failure_and_lock_recovery(self):
        for stream in (False, True):
            with self.assertLogs(level="ERROR"):
                response = self.client.post("/v1/chat/completions", json=payload(
                    stream=stream, messages=[{"role": "user", "content": "fail"}]
                ))
            if stream:
                self.assertIn('"server_error"', response.text)
                self.assertNotIn("[DONE]", response.text)
            else:
                self.assertEqual(response.status_code, 500)
            self.assertEqual(self.client.post("/v1/chat/completions", json=payload()).status_code, 200)

    def test_concurrent_requests_serialize_generation(self):
        def send(_):
            return self.client.post("/v1/chat/completions", json=payload()).status_code

        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(list(pool.map(send, range(4))), [200] * 4)
        self.assertEqual(self.app.state.engine.peak, 1)


class DisconnectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = api.create_app(engine_factory=Engine)
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self.socket.getsockname()[1]}"
        self.server = uvicorn.Server(uvicorn.Config(self.app, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.socket]})
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                return
            await asyncio.sleep(0.01)
        self.fail("Server did not start")

    async def asyncTearDown(self):
        self.server.should_exit = True
        await asyncio.to_thread(self.thread.join, 10)
        self.socket.close()
        self.assertFalse(self.thread.is_alive())

    async def assert_recovered(self):
        self.assertTrue(await asyncio.to_thread(self.app.state.engine.cancelled.wait, 2))
        async with httpx.AsyncClient(base_url=self.url) as client:
            response = await client.post("/v1/chat/completions", json=payload())
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.app.state.engine.peak, 1)

    async def test_stream_is_incremental_and_disconnect_stops_worker(self):
        async with httpx.AsyncClient(base_url=self.url) as client:
            async with client.stream("POST", "/v1/chat/completions", json=payload(
                stream=True, messages=[{"role": "user", "content": "wait"}]
            )) as response:
                async for line in response.aiter_lines():
                    if "first " in line:
                        break
                self.assertEqual(self.app.state.engine.active, 1)
                self.assertEqual((await client.get("/healthz")).status_code, 200)
        await self.assert_recovered()

    async def test_non_streaming_disconnect_stops_worker(self):
        async with httpx.AsyncClient(base_url=self.url) as client:
            task = asyncio.create_task(client.post("/v1/chat/completions", json=payload(
                messages=[{"role": "user", "content": "wait"}]
            )))
            self.assertTrue(await asyncio.to_thread(self.app.state.engine.started.wait, 2))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        await self.assert_recovered()

    async def test_disconnected_queued_request_does_not_generate(self):
        async with httpx.AsyncClient(base_url=self.url) as client:
            task = asyncio.create_task(client.post("/v1/chat/completions", json=payload(
                messages=[{"role": "user", "content": "wait"}]
            )))
            self.assertTrue(await asyncio.to_thread(self.app.state.engine.started.wait, 2))
            async with client.stream("POST", "/v1/chat/completions", json=payload(stream=True)) as response:
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        break
            self.assertEqual((await client.get("/healthz")).status_code, 200)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        await self.assert_recovered()
        self.assertEqual(self.app.state.engine.calls, 2)


if __name__ == "__main__":
    unittest.main()
