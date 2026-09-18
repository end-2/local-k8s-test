"""Verify generation and streaming against a running Qwen3 API server."""

import json
from concurrent.futures import ThreadPoolExecutor
import os
import threading
import unittest

import httpx


class GPUContractTests(unittest.TestCase):
    def test_concurrent_requests_keep_individual_lengths_and_usage(self):
        barrier = threading.Barrier(3)
        requests = [
            ("Say hello.", 8, False),
            ("대한민국의 수도와 특징을 설명하세요.", 16, True),
            ("List three reasons to learn programming and explain each briefly.", 32, True),
        ]
        with httpx.Client(
            base_url=os.environ.get("API_SERVER_URL", "http://127.0.0.1:8000"), timeout=90,
        ) as client:
            def send(item):
                prompt, limit, stream = item
                payload = {
                    "model": "Qwen/Qwen3-0.6B", "messages": [{"role": "user", "content": prompt}],
                    "max_completion_tokens": limit, "ignore_eos": True, "stream": stream,
                }
                if stream:
                    payload["stream_options"] = {"include_usage": True}
                barrier.wait(timeout=10)
                response = client.post("/v1/chat/completions", json=payload)
                response.raise_for_status()
                if stream:
                    frames = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
                    self.assertEqual(frames[-1], "[DONE]")
                    chunks = [json.loads(frame) for frame in frames[:-1]]
                    usage = chunks[-1]["usage"]
                    finish = chunks[-2]["choices"][0]["finish_reason"]
                    text = "".join(choice["delta"].get("content", "") for chunk in chunks for choice in chunk["choices"])
                else:
                    data = response.json()
                    usage = data["usage"]
                    finish = data["choices"][0]["finish_reason"]
                    text = data["choices"][0]["message"]["content"]
                self.assertEqual(usage["completion_tokens"], limit)
                self.assertEqual(usage["total_tokens"], usage["prompt_tokens"] + limit)
                self.assertEqual(finish, "length")
                self.assertTrue(text)
                return usage["prompt_tokens"]

            with ThreadPoolExecutor(max_workers=3) as pool:
                prompt_lengths = list(pool.map(send, requests))
            self.assertGreater(len(set(prompt_lengths)), 1)

    def test_generation_limits_streaming_and_usage(self):
        payload = {
            "model": "Qwen/Qwen3-0.6B",
            "messages": [{
                "role": "user",
                "content": "대한민국의 수도는 어디인가요? 한 문장으로 답하세요.",
            }],
            "max_completion_tokens": 64,
        }
        with httpx.Client(
            base_url=os.environ.get("API_SERVER_URL", "http://127.0.0.1:8000"), timeout=90
        ) as client:
            response = client.post("/v1/chat/completions", json=payload)
            response.raise_for_status()
            stopped = response.json()
            self.assertEqual(stopped["choices"][0]["finish_reason"], "stop")
            self.assertLess(stopped["usage"]["completion_tokens"], 64)
            self.assertIn("서울", stopped["choices"][0]["message"]["content"])

            payload["ignore_eos"] = True
            response = client.post("/v1/chat/completions", json=payload)
            response.raise_for_status()
            normal = response.json()
            self.assertEqual(normal["usage"]["completion_tokens"], 64)
            self.assertEqual(normal["choices"][0]["finish_reason"], "length")

            with client.stream("POST", "/v1/chat/completions", json={
                **payload, "stream": True, "stream_options": {"include_usage": True}
            }) as response:
                response.raise_for_status()
                frames = [line[6:] for line in response.iter_lines() if line.startswith("data: ")]
            self.assertEqual(frames[-1], "[DONE]")
            chunks = [json.loads(frame) for frame in frames[:-1]]
            text = "".join(
                choice["delta"].get("content", "")
                for chunk in chunks for choice in chunk["choices"]
            )
            self.assertEqual(text, normal["choices"][0]["message"]["content"])
            self.assertEqual(chunks[-1]["usage"], normal["usage"])
            self.assertEqual(chunks[-2]["choices"][0]["finish_reason"], "length")

            payload["messages"][0]["content"] = "hello " * 3000
            response = client.post("/v1/chat/completions", json=payload)
            self.assertEqual(response.status_code, 400)
            self.assertIn("--max-input-tokens", response.json()["error"]["message"])


if __name__ == "__main__":
    unittest.main()
