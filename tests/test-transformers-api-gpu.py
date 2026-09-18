"""Verify generation and streaming against a running Qwen3 API server."""

import json
import os
import unittest

import httpx


class GPUContractTests(unittest.TestCase):
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
