"""The one model contract: complete(model, system, user, json_output) -> text.

Talks to any OpenAI-compatible server (Ollama, LM Studio, llama.cpp's
llama-server, vLLM). Switching runtimes is LLM_BASE_URL in .env, not a code change."""
import os
from functools import lru_cache

import openai


class LlmError(Exception):
    """The model call failed. For a reviewer this is a FAILED review, not a clean one."""


def _env(name: str, default: str) -> str:
    return os.getenv(name) or default


@lru_cache(maxsize=1)
def _client() -> openai.OpenAI:
    return openai.OpenAI(
        base_url=_env("LLM_BASE_URL", "http://localhost:11434/v1"),
        api_key=_env("LLM_API_KEY", "ollama"),  # local runtimes ignore it, but the SDK requires one
        timeout=float(_env("LLM_TIMEOUT_SECONDS", "300")),
        max_retries=0,
    )


def _estimated_tokens(text: str) -> int:
    return len(text) // 3  # rough; code tokenizes denser than prose, so err high


def complete(model: str, system: str, user: str, json_output: bool = False) -> str:
    # Ollama silently drops the start of a prompt that overflows its context window,
    # and the model still answers in valid JSON -- that would look like a clean review.
    # Refuse up front instead. Keep LLM_CONTEXT_TOKENS in step with OLLAMA_CONTEXT_LENGTH.
    budget = int(_env("LLM_CONTEXT_TOKENS", "4096")) - int(_env("LLM_OUTPUT_TOKENS", "1024"))
    needed = _estimated_tokens(system) + _estimated_tokens(user)
    if needed > budget:
        raise LlmError(f"prompt is ~{needed} tokens but the context budget is {budget}; "
                       "send a smaller diff or raise OLLAMA_CONTEXT_LENGTH and LLM_CONTEXT_TOKENS")

    try:
        resp = _client().chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.1 if json_output else 0.4,
            max_tokens=int(_env("LLM_OUTPUT_TOKENS", "1024")),
            **({"response_format": {"type": "json_object"}} if json_output else {}),
        )
    except openai.APITimeoutError:
        raise LlmError(f"{model} timed out") from None
    except openai.APIConnectionError:
        raise LlmError(f"can't reach the model server at {_client().base_url} -- is Ollama running?") from None
    except openai.NotFoundError:
        raise LlmError(f"model {model} isn't available -- run `ollama pull {model}`") from None
    except openai.APIError as e:
        raise LlmError(f"{model} call failed: {e}") from None

    choice = resp.choices[0]
    if choice.finish_reason == "length":
        raise LlmError(f"{model} hit the output limit before finishing its answer")
    return choice.message.content or ""


def available_models() -> set[str] | None:
    """Model names the server has, or None if it can't be reached."""
    try:
        return {m.id for m in _client().models.list()}
    except openai.APIError:
        return None
