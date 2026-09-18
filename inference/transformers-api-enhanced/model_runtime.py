"""Initialize Transformers resources and tokenize requests before scheduling."""

from contracts import GenerationRequest, PreparedRequest
from settings import Settings


class ModelRuntime:
    def __init__(self, settings: Settings):
        import torch
        import torch.backends.python_native as python_native
        from transformers import AutoModelForCausalLM, AutoTokenizer

        if not settings.model.is_dir():
            raise ValueError(f"Model directory does not exist: {settings.model}")
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA GPU is required.")
        # Use prebuilt CUDA kernels so the image needs no runtime compiler.
        python_native.disable_dispatch_keys("CUDA")
        torch.set_num_threads(2)
        self.tokenizer = AutoTokenizer.from_pretrained(
            settings.model, local_files_only=True, padding_side="left",
        )
        if self.tokenizer.pad_token_id is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        if self.tokenizer.pad_token_id is None:
            raise ValueError("The tokenizer requires a padding or EOS token for batching.")
        self.model = AutoModelForCausalLM.from_pretrained(
            settings.model,
            local_files_only=True,
            use_safetensors=True,
            dtype=torch.float16,
            attn_implementation="sdpa",
        ).to("cuda").eval()
        self.settings = settings

    def prepare(self, request: GenerationRequest):
        text = self.tokenizer.apply_chat_template(
            request.messages, tokenize=False, add_generation_prompt=True, enable_thinking=False,
        )
        token_ids = self.tokenizer(text, add_special_tokens=False)["input_ids"]
        if len(token_ids) > self.settings.max_input_tokens:
            raise ValueError("Input exceeds --max-input-tokens; prompts are not truncated.")
        if len(token_ids) + request.output_tokens > self.model.config.max_position_embeddings:
            raise ValueError("Input and output tokens exceed the model context length.")
        return PreparedRequest(request, token_ids)
