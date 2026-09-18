"""Shared model, request, and batch scheduling settings."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    model: Path = Path("/model")
    served_model_name: str = "Qwen/Qwen3-0.6B"
    max_input_tokens: int = 2048
    max_output_tokens: int = 1024
    default_output_tokens: int = 128
    max_batch_size: int = 4
    max_batch_tokens: int = 8192
    batch_wait_ms: int = 5

    def __post_init__(self):
        if self.max_batch_size < 1 or self.max_batch_tokens < 1:
            raise ValueError("Batch size and token budget must be greater than zero.")
        if self.batch_wait_ms < 0:
            raise ValueError("Batch wait must not be negative.")
