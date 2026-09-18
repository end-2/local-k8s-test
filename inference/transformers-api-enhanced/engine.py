"""Coordinate request lifetimes, batch scheduling, and worker execution."""

import asyncio
from dataclasses import dataclass, field
import logging
from uuid import uuid4

import anyio

from contracts import ScheduledRequest
from model_runtime import ModelRuntime
from scheduler import Scheduler
from worker import TransformersWorker


@dataclass
class ResponseChannel:
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    finished: bool = False


class InferenceEngine:
    def __init__(self, settings, runtime_factory=ModelRuntime, worker_factory=TransformersWorker):
        self.settings = settings
        self.runtime = runtime_factory(settings)
        self.worker = worker_factory(self.runtime.model, self.runtime.tokenizer)
        self.scheduler = Scheduler(settings)
        self.channels = {}
        self.wake = asyncio.Event()
        self.task = None
        self.closing = False

    async def start(self):
        self.task = asyncio.create_task(self._run())

    async def prepare(self, generation):
        prepared = await asyncio.to_thread(self.runtime.prepare, generation)
        if prepared.prompt_tokens + generation.output_tokens > self.settings.max_batch_tokens:
            raise ValueError("Input and output tokens exceed --max-batch-tokens.")
        return prepared

    async def events(self, prepared):
        if self.closing:
            yield "error", "Server is shutting down."
            return
        request = ScheduledRequest(uuid4().hex, prepared)
        channel = ResponseChannel()
        self.scheduler.add(request)
        self.channels[request.request_id] = channel
        self.wake.set()
        try:
            while True:
                kind, value = await channel.queue.get()
                yield kind, value
                if kind != "text":
                    break
        finally:
            self.scheduler.cancel(request.request_id)
            self.channels.pop(request.request_id, None)

    def _publish(self, request_id, kind, value):
        channel = self.channels.get(request_id)
        if channel is not None and not channel.finished:
            channel.finished = kind != "text"
            channel.queue.put_nowait((kind, value))

    async def _run(self):
        loop = asyncio.get_running_loop()

        def publish(request_id, kind, value):
            loop.call_soon_threadsafe(self._publish, request_id, kind, value)

        while not self.closing:
            await self.wake.wait()
            self.wake.clear()
            if not self.scheduler.waiting:
                continue
            # Give requests arriving together a short window to form a batch.
            await asyncio.sleep(self.settings.batch_wait_ms / 1000)
            if self.closing:
                break
            batch = self.scheduler.schedule()
            if not batch:
                continue
            logging.info(
                "Generation batch: requests=%d, reserved_tokens=%d",
                len(batch), self.scheduler.token_cost(batch),
            )
            try:
                # Only this loop dispatches work; client cancellation never releases a running batch.
                await asyncio.to_thread(self.worker.execute_batch, batch, publish)
            except Exception:
                logging.exception("Generation batch failed")
                for request in batch:
                    self._publish(request.request_id, "error", "Model generation failed. See server logs.")
            finally:
                self.scheduler.finish_batch()
            if self.scheduler.waiting:
                self.wake.set()

    async def close(self):
        self.closing = True
        self.scheduler.cancel_all()
        for request_id in list(self.channels):
            self._publish(request_id, "error", "Server is shutting down.")
        self.wake.set()
        if self.task is not None:
            with anyio.CancelScope(shield=True):
                await asyncio.shield(self.task)
