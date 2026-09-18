"""Generate text from a local Qwen3 model on one CUDA GPU."""

import argparse
import json
from pathlib import Path
import time


def positive_int(value):
    value = int(value)
    if value < 1:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return value


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=Path("/model"))
    parser.add_argument(
        "--prompt", default="대한민국의 수도는 어디인가요? 한 문장으로 답하세요."
    )
    parser.add_argument("--batch-size", type=positive_int, default=1)
    parser.add_argument("--max-input-tokens", type=positive_int, default=512)
    parser.add_argument("--max-new-tokens", type=positive_int, default=128)
    args = parser.parse_args()
    if not args.model.is_dir():
        parser.error(f"Model directory does not exist: {args.model}")
    if not args.prompt.strip():
        parser.error("--prompt must not be empty")

    import torch
    import torch.backends.python_native as python_native
    import transformers
    from transformers import AutoModelForCausalLM, AutoTokenizer, GenerationConfig

    if not torch.cuda.is_available():
        parser.error("CUDA GPU is required. Request nvidia.com/gpu: 1 in the Pod.")

    # Use prebuilt CUDA kernels so this image needs no runtime compiler.
    python_native.disable_dispatch_keys("CUDA")
    transformers.utils.logging.disable_progress_bar()
    torch.set_num_threads(2)
    torch.cuda.reset_peak_memory_stats()
    emit(
        "runtime",
        torch=torch.__version__,
        transformers=transformers.__version__,
        cuda=torch.version.cuda,
        gpu=torch.cuda.get_device_name(0),
        model=str(args.model),
        dtype="float16",
        attention="sdpa",
        batch_size=args.batch_size,
    )

    started = time.perf_counter()
    tokenizer = AutoTokenizer.from_pretrained(args.model, local_files_only=True)
    tokenizer.padding_side = "left"
    tokenizer.truncation_side = "left"
    model = AutoModelForCausalLM.from_pretrained(
        args.model,
        local_files_only=True,
        use_safetensors=True,
        dtype=torch.float16,
        attn_implementation="sdpa",
    ).to("cuda").eval()
    torch.cuda.synchronize()
    emit("loaded", seconds=round(time.perf_counter() - started, 3))

    text = tokenizer.apply_chat_template(
        [{"role": "user", "content": args.prompt}],
        tokenize=False,
        add_generation_prompt=True,
        enable_thinking=False,
    )
    inputs = tokenizer(
        [text] * args.batch_size,
        return_tensors="pt",
        padding=True,
        truncation=True,
        max_length=args.max_input_tokens,
        add_special_tokens=False,
    ).to("cuda")
    input_length = inputs.input_ids.shape[1]
    if input_length + args.max_new_tokens > model.config.max_position_embeddings:
        parser.error("Input and output tokens exceed the model context length.")

    generation = GenerationConfig(
        max_new_tokens=args.max_new_tokens,
        do_sample=False,
        temperature=1.0,
        top_p=1.0,
        top_k=50,
        use_cache=True,
        pad_token_id=tokenizer.pad_token_id,
        eos_token_id=model.generation_config.eos_token_id,
    )
    torch.cuda.synchronize()
    started = time.perf_counter()
    with torch.inference_mode():
        outputs = model.generate(**inputs, generation_config=generation)
    torch.cuda.synchronize()
    elapsed = time.perf_counter() - started

    eos_ids = generation.eos_token_id
    eos_ids = {eos_ids} if isinstance(eos_ids, int) else set(eos_ids)
    total_tokens = 0
    for index, row in enumerate(outputs[:, input_length:].tolist()):
        # Include the first EOS in the count, but exclude subsequent batch padding.
        stop = next((i + 1 for i, token in enumerate(row) if token in eos_ids), len(row))
        tokens = row[:stop]
        answer = tokenizer.decode(tokens, skip_special_tokens=True).strip()
        if not answer:
            raise RuntimeError(f"Empty response for batch item {index}")
        total_tokens += len(tokens)
        emit("response", index=index, generated_tokens=len(tokens), text=answer)
    emit(
        "complete",
        batch_size=args.batch_size,
        input_tokens_per_request=input_length,
        generated_tokens_total=total_tokens,
        generation_seconds=round(elapsed, 3),
        output_tokens_per_second=round(total_tokens / elapsed, 2),
        peak_allocated_mib=round(torch.cuda.max_memory_allocated() / 2**20, 1),
        peak_reserved_mib=round(torch.cuda.max_memory_reserved() / 2**20, 1),
    )


if __name__ == "__main__":
    main()
