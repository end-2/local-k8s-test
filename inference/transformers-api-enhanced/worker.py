"""Execute a scheduled batch on an already initialized Transformers model."""

from contracts import Result


class TransformersWorker:
    def __init__(self, model, tokenizer):
        self.model = model
        self.tokenizer = tokenizer

    def execute_batch(self, batch, emit):
        import torch
        from transformers import GenerationConfig, StoppingCriteria, TextStreamer

        batch = [item for item in batch if not item.cancelled.is_set()]
        if not batch:
            return
        options = batch[0].prepared.generation
        inputs = self.tokenizer.pad(
            {"input_ids": [item.prepared.prompt_token_ids for item in batch]},
            padding=True, return_tensors="pt",
        ).to(self.model.device)
        prompt_width = inputs.input_ids.shape[1]
        eos = [] if options.ignore_eos else self.model.generation_config.eos_token_id
        eos_ids = {eos} if isinstance(eos, int) else set(eos or [])
        tokenizer = self.tokenizer

        class RequestLimits(StoppingCriteria):
            def __call__(self, input_ids, scores, **kwargs):
                generated = input_ids.shape[1] - prompt_width
                return torch.tensor([
                    item.cancelled.is_set() or generated >= item.prepared.generation.output_tokens
                    for item in batch
                ], dtype=torch.bool, device=input_ids.device)

        class RequestStreamer(TextStreamer):
            def __init__(self, item):
                super().__init__(
                    tokenizer, skip_special_tokens=True, clean_up_tokenization_spaces=False,
                )
                self.item = item

            def on_finalized_text(self, text, stream_end=False):
                if text and not self.item.cancelled.is_set():
                    emit(self.item.request_id, "text", text)

        class BatchStreamer:
            def __init__(self):
                self.is_prompt = True
                self.tokens = [[] for _ in batch]
                self.finished = [False for _ in batch]
                self.streamers = [
                    RequestStreamer(item) if item.prepared.generation.stream else None
                    for item in batch
                ]

            def finish(self, index, reason):
                if self.finished[index]:
                    return
                self.finished[index] = True
                if self.streamers[index] is not None:
                    self.streamers[index].end()
                item = batch[index]
                if not item.cancelled.is_set():
                    tokens = self.tokens[index]
                    emit(item.request_id, "result", Result(
                        tokenizer.decode(tokens, skip_special_tokens=True, clean_up_tokenization_spaces=False),
                        len(tokens), reason,
                    ))

            def put(self, value):
                if self.is_prompt:
                    self.is_prompt = False
                    return
                for index, token in enumerate(value.tolist()):
                    item = batch[index]
                    if self.finished[index]:
                        continue
                    if item.cancelled.is_set():
                        self.finish(index, "stop")
                        continue
                    self.tokens[index].append(token)
                    if self.streamers[index] is not None:
                        self.streamers[index].put(value[index:index + 1])
                    if token in eos_ids:
                        self.finish(index, "stop")
                    elif len(self.tokens[index]) >= item.prepared.generation.output_tokens:
                        self.finish(index, "length")

            def end(self):
                for index in range(len(batch)):
                    self.finish(index, "stop" if batch[index].cancelled.is_set() else "length")

        generation = GenerationConfig(
            max_new_tokens=max(item.prepared.generation.output_tokens for item in batch),
            do_sample=options.temperature > 0,
            temperature=options.temperature if options.temperature > 0 else 1.0,
            top_p=options.top_p if options.temperature > 0 else 1.0,
            top_k=50,
            use_cache=True,
            pad_token_id=tokenizer.pad_token_id,
            eos_token_id=eos,
        )
        with torch.inference_mode():
            self.model.generate(
                **inputs, generation_config=generation,
                streamer=BatchStreamer(), stopping_criteria=[RequestLimits()],
            )
