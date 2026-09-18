"""Verify batch admission and engine cancellation without CUDA."""

import asyncio
from contextlib import aclosing
from dataclasses import replace
from pathlib import Path
import sys
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "inference/transformers-api-enhanced"))

from contracts import GenerationRequest, PreparedRequest, Result, ScheduledRequest
from engine import InferenceEngine
from scheduler import Scheduler
from settings import Settings


def request(name, prompt_tokens=10, **options):
    generation = GenerationRequest(
        messages=[{"role": "user", "content": name}], output_tokens=8,
        temperature=0, top_p=1, ignore_eos=False, stream=False,
    )
    return ScheduledRequest(name, PreparedRequest(
        replace(generation, **options), list(range(prompt_tokens)),
    ))


class SchedulerTests(unittest.TestCase):
    def test_batch_size_with_mixed_streaming_and_output_limits(self):
        scheduler = Scheduler(Settings(max_batch_size=2))
        items = [request("a"), request("b", output_tokens=32, stream=True), request("c")]
        for item in items:
            scheduler.add(item)
        self.assertEqual(scheduler.schedule(), items[:2])
        self.assertEqual(scheduler.schedule(), [])
        scheduler.finish_batch()
        self.assertEqual(scheduler.schedule(), items[2:])

    def test_incompatible_options_wait_without_starvation(self):
        for options in ({"temperature": 0.7}, {"ignore_eos": True}):
            with self.subTest(options=options):
                scheduler = Scheduler(Settings())
                items = [request("a"), request("b", **options), request("c")]
                for item in items:
                    scheduler.add(item)
                self.assertEqual(scheduler.schedule(), [items[0], items[2]])
                scheduler.finish_batch()
                scheduler.add(request("d"))
                self.assertEqual(scheduler.schedule(), [items[1]])

    def test_sampling_top_p_is_part_of_batch_compatibility(self):
        scheduler = Scheduler(Settings())
        items = [request("a", temperature=0.7, top_p=0.8), request("b", temperature=0.7)]
        for item in items:
            scheduler.add(item)
        self.assertEqual(scheduler.schedule(), items[:1])

    def test_greedy_top_p_does_not_split_a_batch(self):
        scheduler = Scheduler(Settings())
        items = [request("a", top_p=0.2), request("b", top_p=0.9)]
        for item in items:
            scheduler.add(item)
        self.assertEqual(scheduler.schedule(), items)

    def test_token_budget_includes_padding_and_longest_output(self):
        scheduler = Scheduler(Settings(max_batch_tokens=200))
        items = [request("a", prompt_tokens=100), request("b", prompt_tokens=5, output_tokens=16)]
        for item in items:
            scheduler.add(item)
        self.assertEqual(scheduler.token_cost(items), 232)
        self.assertEqual(scheduler.schedule(), items[:1])
        scheduler.finish_batch()
        self.assertEqual(scheduler.schedule(), items[1:])

    def test_oversized_request_is_rejected_before_queueing(self):
        scheduler = Scheduler(Settings(max_batch_tokens=17))
        with self.assertRaisesRegex(ValueError, "--max-batch-tokens"):
            scheduler.add(request("large"))
        self.assertEqual(scheduler.schedule(), [])

    def test_cancellation_keeps_running_batch_reserved_until_worker_returns(self):
        scheduler = Scheduler(Settings(max_batch_size=1))
        active, removed, following = [request(name) for name in ("a", "b", "c")]
        for item in (active, removed, following):
            scheduler.add(item)
        self.assertEqual(scheduler.schedule(), [active])
        scheduler.cancel(active.request_id)
        scheduler.cancel(removed.request_id)
        self.assertTrue(active.cancelled.is_set())
        self.assertTrue(removed.cancelled.is_set())
        self.assertEqual(scheduler.schedule(), [])
        scheduler.finish_batch()
        self.assertEqual(scheduler.schedule(), [following])

    def test_cancel_all_cancels_waiting_and_running_requests(self):
        scheduler = Scheduler(Settings(max_batch_size=1))
        items = [request("a"), request("b")]
        for item in items:
            scheduler.add(item)
        scheduler.schedule()
        scheduler.cancel_all()
        self.assertTrue(all(item.cancelled.is_set() for item in items))
        self.assertFalse(scheduler.waiting)

    def test_invalid_batch_settings(self):
        for options in ({"max_batch_size": 0}, {"max_batch_tokens": 0}, {"batch_wait_ms": -1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                Settings(**options)


class Runtime:
    model = None
    tokenizer = None

    def __init__(self, settings):
        pass


class BlockingWorker:
    def __init__(self, model, tokenizer):
        self.release = threading.Event()
        self.batches = []

    def execute_batch(self, batch, emit):
        self.batches.append(batch)
        for item in batch:
            emit(item.request_id, "text", "started")
        if not self.release.wait(3):
            raise RuntimeError("Test did not release the worker")
        for item in batch:
            if not item.cancelled.is_set():
                emit(item.request_id, "result", Result("done", 1, "stop"))


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = InferenceEngine(
            Settings(batch_wait_ms=0), runtime_factory=Runtime, worker_factory=BlockingWorker,
        )
        await self.engine.start()

    async def asyncTearDown(self):
        self.engine.worker.release.set()
        await self.engine.close()

    async def test_disconnect_does_not_dispatch_over_a_worker_still_exiting(self):
        source = self.engine.events(request("a").prepared)
        self.assertEqual(await anext(source), ("text", "started"))
        await source.aclose()
        self.assertTrue(self.engine.worker.batches[0][0].cancelled.is_set())
        async with aclosing(self.engine.events(request("b").prepared)) as following:
            pending = asyncio.create_task(anext(following))
            try:
                done, _ = await asyncio.wait({pending}, timeout=0.03)
                self.assertFalse(done)
                self.assertEqual(len(self.engine.worker.batches), 1)
                self.engine.worker.release.set()
                self.assertEqual(await asyncio.wait_for(pending, 1), ("text", "started"))
                self.assertEqual(len(self.engine.worker.batches), 2)
            finally:
                self.engine.worker.release.set()
                await pending

    async def test_shutdown_cancels_requests_and_waits_for_worker(self):
        async with aclosing(self.engine.events(request("a").prepared)) as source:
            await anext(source)
            closing = asyncio.create_task(self.engine.close())
            try:
                done, _ = await asyncio.wait({closing}, timeout=0.03)
                self.assertFalse(done)
                self.assertTrue(self.engine.worker.batches[0][0].cancelled.is_set())
                self.assertEqual((await anext(source))[0], "error")
                self.engine.worker.release.set()
                await asyncio.wait_for(closing, 1)
            finally:
                self.engine.worker.release.set()
                await closing
        self.assertFalse(self.engine.scheduler.waiting)
        self.assertFalse(self.engine.scheduler.running)
        self.assertFalse(self.engine.channels)

    async def test_shutdown_rejects_new_submissions(self):
        await self.engine.close()
        events = [event async for event in self.engine.events(request("a").prepared)]
        self.assertEqual(events, [("error", "Server is shutting down.")])
        self.assertFalse(self.engine.worker.batches)

    async def test_cancelling_one_batch_member_keeps_the_other_running(self):
        first = self.engine.events(request("a").prepared)
        async with aclosing(self.engine.events(request("b").prepared)) as second:
            await asyncio.gather(anext(first), anext(second))
            self.assertEqual(len(self.engine.worker.batches), 1)
            self.assertEqual(len(self.engine.worker.batches[0]), 2)
            await first.aclose()
            self.engine.worker.release.set()
            kind, value = await anext(second)
            self.assertEqual(kind, "result")
            self.assertEqual(value.text, "done")


if __name__ == "__main__":
    unittest.main()
