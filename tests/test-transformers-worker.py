"""Exercise real Transformers batch generation with a tiny CPU model."""

from pathlib import Path
import sys
import unittest

import torch
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from transformers import LogitsProcessor, PreTrainedTokenizerFast, Qwen3Config, Qwen3ForCausalLM

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "inference/transformers-api-enhanced"))

from contracts import GenerationRequest, PreparedRequest, ScheduledRequest
from worker import TransformersWorker


def request(name, prompt, output_tokens, stream=True, ignore_eos=False):
    return ScheduledRequest(name, PreparedRequest(GenerationRequest(
        messages=[], output_tokens=output_tokens, temperature=0,
        top_p=1, ignore_eos=ignore_eos, stream=stream,
    ), prompt))


class WorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        torch.set_num_threads(1)
        cls.tokenizer = PreTrainedTokenizerFast(
            tokenizer_object=Tokenizer(WordLevel({
                "[PAD]": 0, "[EOS]": 1, "hello": 2, "world": 3,
                "alpha": 4, "beta": 5, "prompt": 6, "[UNK]": 7,
            }, unk_token="[UNK]")),
            pad_token="[PAD]", eos_token="[EOS]", unk_token="[UNK]", padding_side="left",
        )
        cls.model = Qwen3ForCausalLM(Qwen3Config(
            vocab_size=8, hidden_size=16, intermediate_size=32, num_hidden_layers=1,
            num_attention_heads=2, num_key_value_heads=2, head_dim=8,
            max_position_embeddings=64, pad_token_id=0, eos_token_id=1,
        )).eval()

    def execute(self, batch, sequences, cancel_step=None):
        model = self.model
        events = {item.request_id: [] for item in batch}
        calls = []

        class ScriptedTokens(LogitsProcessor):
            def __init__(self):
                self.step = 0

            def __call__(self, input_ids, scores):
                if cancel_step == self.step:
                    batch[0].cancelled.set()
                scores.fill_(-float("inf"))
                for row, sequence in enumerate(sequences):
                    scores[row, sequence[min(self.step, len(sequence) - 1)]] = 0
                self.step += 1
                return scores

        class Model:
            device = model.device
            generation_config = model.generation_config

            def generate(self, **kwargs):
                calls.append(kwargs["input_ids"].tolist())
                return model.generate(**kwargs, logits_processor=[ScriptedTokens()])

        worker = TransformersWorker(Model(), self.tokenizer)
        worker.execute_batch(batch, lambda name, kind, value: events[name].append((kind, value)))
        return events, calls

    def test_one_generate_call_routes_eos_lengths_padding_and_streams(self):
        batch = [request("short", [6], 8), request("long", [6, 6, 6], 4), request("json", [6, 6], 3, stream=False)]
        events, calls = self.execute(batch, [[2, 1], [4, 5, 4, 5], [3, 2, 3]])
        self.assertEqual(calls, [[[0, 0, 6], [6, 6, 6], [0, 6, 6]]])
        for name, count, finish, text in [
            ("short", 2, "stop", "hello"),
            ("long", 4, "length", "alpha beta alpha beta"),
            ("json", 3, "length", "world hello world"),
        ]:
            with self.subTest(name=name):
                self.assertEqual(events[name][-1][0], "result")
                result = events[name][-1][1]
                self.assertEqual(result.completion_tokens, count)
                self.assertEqual(result.finish_reason, finish)
                self.assertEqual(result.text, text)
                chunks = [value for kind, value in events[name] if kind == "text"]
                self.assertEqual("".join(chunks), "" if name == "json" else text)

    def test_ignore_eos_keeps_each_request_output_limit(self):
        batch = [request("a", [6], 2, ignore_eos=True), request("b", [6, 6], 4, ignore_eos=True)]
        events, _ = self.execute(batch, [[1, 2, 3, 4], [2, 1, 4, 5]])
        for name, limit in [("a", 2), ("b", 4)]:
            result = events[name][-1][1]
            self.assertEqual(result.completion_tokens, limit)
            self.assertEqual(result.finish_reason, "length")

    def test_cancellation_does_not_stop_other_batch_members(self):
        batch = [request("cancelled", [6], 8), request("survivor", [6, 6], 4)]
        events, calls = self.execute(batch, [[2, 3], [4, 5]], cancel_step=1)
        self.assertEqual(len(calls), 1)
        self.assertFalse(any(kind == "result" for kind, _ in events["cancelled"]))
        self.assertEqual(events["survivor"][-1][1].completion_tokens, 4)
        self.assertEqual(events["survivor"][-1][1].finish_reason, "length")

    def test_cancelled_batch_never_calls_model(self):
        batch = [request("cancelled", [6], 8)]
        batch[0].cancelled.set()
        events, calls = self.execute(batch, [[2]])
        self.assertFalse(calls)
        self.assertEqual(events, {"cancelled": []})


if __name__ == "__main__":
    unittest.main()
