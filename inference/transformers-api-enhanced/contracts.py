"""Requests and results shared by the inference layers."""

from dataclasses import dataclass, field
import threading


@dataclass(frozen=True)
class GenerationRequest:
    messages: list[dict[str, str]]
    output_tokens: int
    temperature: float
    top_p: float
    ignore_eos: bool
    stream: bool


@dataclass(frozen=True)
class PreparedRequest:
    generation: GenerationRequest
    prompt_token_ids: list[int]

    @property
    def prompt_tokens(self):
        return len(self.prompt_token_ids)


@dataclass
class ScheduledRequest:
    request_id: str
    prepared: PreparedRequest
    cancelled: threading.Event = field(default_factory=threading.Event)


@dataclass(frozen=True)
class Result:
    text: str
    completion_tokens: int
    finish_reason: str
