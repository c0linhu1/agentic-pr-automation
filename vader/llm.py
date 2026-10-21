"""The one model call: complete(model, instructions, content) -> JSON findings text.

Works with any OpenAI-compatible server (Ollama, LM Studio, llama.cpp, vLLM); switch with LLM_BASE_URL."""
from functools import lru_cache

import openai

from . import config, models
from .findings import SCHEMA


class LlmError(Exception):
    """The model call failed. For a reviewer this is a FAILED review, not a clean one."""


@lru_cache(maxsize=1)
def _client() -> openai.OpenAI:
    return openai.OpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY,
                         timeout=config.TIMEOUT_SECONDS, max_retries=0)


# Shared parts first, the validator's instructions last, so the server can reuse the prompt start.
SYSTEM = ("You are one of several independent code reviewers. The message shows a change to review, followed by "
          "your specific task. Follow the task's instructions exactly.")
TASK_HEADING = "=== YOUR TASK ==="


def messages(instructions: str, content: str) -> list[dict]:
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"{content}\n\n{TASK_HEADING}\n{instructions}"}]


def _estimated_tokens(text: str) -> int:
    return len(text) // 3  # rough; code tokenizes denser than prose, so err high


def _effort_for(model: str) -> str | None:
    try:
        return models.reasoning_effort(model)
    except Exception:  # noqa: BLE001 - a broken models.yaml shouldn't break the call itself
        return None


def complete(model: str, instructions: str, content: str, schema: dict = SCHEMA) -> str:
    # An overflowing prompt can be cut off silently and still look clean: refuse it.
    budget = config.CONTEXT_TOKENS - config.OUTPUT_TOKENS
    needed = _estimated_tokens(SYSTEM) + _estimated_tokens(instructions) + _estimated_tokens(content)
    if needed > budget:
        raise LlmError(f"prompt is ~{needed} tokens but the context budget is {budget}; "
                       "send a smaller diff or raise the server's context and LLM_CONTEXT_TOKENS")

    # Structured outputs: the server enforces this schema.
    extra = {"response_format": {"type": "json_schema",
                                 "json_schema": {"name": "reply", "strict": True, "schema": schema}}}
    effort = _effort_for(model)
    if effort:
        extra["reasoning_effort"] = effort  # for reasoning models like gpt-oss
    try:
        resp = _client().chat.completions.create(
            model=model,
            messages=messages(instructions, content),
            temperature=0.1,
            max_tokens=config.OUTPUT_TOKENS,
            **extra,
        )
    except openai.APITimeoutError:
        raise LlmError(f"{model} timed out after {config.TIMEOUT_SECONDS:g}s") from None
    except openai.APIConnectionError:
        raise LlmError(f"can't reach the model server at {config.LLM_BASE_URL} -- is it running?") from None
    except openai.NotFoundError:
        raise LlmError(f"model {model} isn't available on the model server (pulled/downloaded?)") from None
    except openai.APIError as e:
        raise LlmError(f"{model} call failed: {e}") from None

    choice = resp.choices[0]
    if choice.finish_reason == "length":
        raise LlmError(f"{model} hit the output limit ({config.OUTPUT_TOKENS} tokens) before finishing its "
                       "answer; thinking models spend part of that budget reasoning, so raise LLM_OUTPUT_TOKENS")
    return choice.message.content or ""
