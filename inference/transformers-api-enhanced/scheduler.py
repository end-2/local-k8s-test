"""Select compatible waiting requests within batch size and token budgets."""

from collections import deque

from contracts import ScheduledRequest
from settings import Settings


class Scheduler:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.waiting = deque()
        self.running = []

    @staticmethod
    def batch_key(request: ScheduledRequest):
        options = request.prepared.generation
        # Greedy decoding ignores top_p, so those requests can share a batch.
        return (
            options.temperature,
            options.top_p if options.temperature > 0 else 1.0,
            options.ignore_eos,
        )

    @staticmethod
    def token_cost(batch):
        if not batch:
            return 0
        # Reserve the padded prompt and longest decode for every batch row.
        return len(batch) * (
            max(item.prepared.prompt_tokens for item in batch)
            + max(item.prepared.generation.output_tokens for item in batch)
        )

    def add(self, request: ScheduledRequest):
        if self.token_cost([request]) > self.settings.max_batch_tokens:
            raise ValueError("Input and output tokens exceed --max-batch-tokens.")
        self.waiting.append(request)

    def schedule(self):
        if self.running:
            return []
        batch = []
        remaining = deque()
        # Anchor each batch on the oldest request to avoid starving other options.
        for request in self.waiting:
            if request.cancelled.is_set():
                continue
            if (
                len(batch) < self.settings.max_batch_size
                and (not batch or self.batch_key(request) == self.batch_key(batch[0]))
                and self.token_cost([*batch, request]) <= self.settings.max_batch_tokens
            ):
                batch.append(request)
            else:
                remaining.append(request)
        self.waiting = remaining
        self.running = batch
        return batch

    def finish_batch(self):
        self.running = []

    def cancel(self, request_id):
        for request in [*self.waiting, *self.running]:
            if request.request_id == request_id:
                request.cancelled.set()
        self.waiting = deque(item for item in self.waiting if not item.cancelled.is_set())

    def cancel_all(self):
        for request in [*self.waiting, *self.running]:
            request.cancelled.set()
        self.waiting.clear()
