"""The one model call: complete(model, instructions, content) -> JSON findings text.

Works with any OpenAI-compatible server (Ollama, LM Studio, llama.cpp, vLLM); switch with LLM_BASE_URL."""
from functools import lru_cache

import openai

from . import config, models
from .findings import SCHEMA


class LlmError(Exception):
    """The model call failed. For a reviewer this is a FAILED review, not a clean one."""


class LlmUnavailable(LlmError, models.ModelServerDown):
    """The model server couldn't be reached, so nothing was reviewed."""


class OutputLimit(LlmError):
    """The model used the whole reply budget (usually on thinking) before it answered."""


# Code runs about 2.4-2.7 characters per token, prose about 4. Assume the densest, so the
# estimate errs high; characters outside ASCII (accents, CJK, emoji) count as a token each.
CHARS_PER_TOKEN = 2.4
# A server that cuts an overlong prompt keeps part of it and still answers (Ollama keeps about half its
# window). A prompt that comes back this much smaller than the estimate was cut, not just dense.
CUT_OFF_BELOW = 0.4


@lru_cache(maxsize=1)
def _client() -> openai.OpenAI:
    return openai.OpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY,
                         timeout=config.TIMEOUT_SECONDS, max_retries=0)


# Shared parts first, the validator's instructions last, so the server can reuse the prompt start. The
# message layout and the rules every validator shares are explained here once, before the model reads them.
SYSTEM = """You are one of several independent code reviewers. Each looks at the same change for something different; yours is the task at the end of the message. Follow its instructions exactly.

The message has these parts, in this order. Any of them may be missing except DIFF.
- TITLE, TICKET, DESCRIPTION: what the change is meant to do, as its author and its ticket describe it.
- CI: the results of the automated checks for this commit.
- TESTS: which kinds of code the repository has test files for, and any kind this change touches that has none.
- FILES IN THIS PR, NOT REVIEWED: on a large change, every file it touches, and the files skipped as generated, lockfiles, or binary.
- NOTE: on a large change, which part of it this message holds. Files not shown are reviewed in the other parts.
- CONTEXT: the full contents of the changed files after the change, for reading only: what the changed code calls, and the patterns around it.
- DIFF: the change itself.

When you report findings:
- Report only problems in what the DIFF changes, or in existing code whose behavior it changes.
- An empty findings list is a good answer. Report only what the code shows; don't invent problems.
- Don't state a fact that has to be worked out (a date's weekday, a holiday's date, a total, a unit conversion) unless you work it out step by step. If you can't, leave it out or report it as info.
- Never repeat a secret's value or personal data; refer to it by file and line.
- At most 10 findings. Keep the most serious."""
TASK_HEADING = "=== YOUR TASK ==="


def messages(instructions: str, content: str) -> list[dict]:
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"{content}\n\n{TASK_HEADING}\n{instructions}"}]


def estimated_tokens(text: str) -> int:
    ascii_chars = len(text.encode("ascii", "ignore"))
    return round(ascii_chars / CHARS_PER_TOKEN) + len(text) - ascii_chars


def prompt_tokens(instructions: str, content: str) -> int:
    """Estimated size of the whole prompt: system message, the change, and the task."""
    return estimated_tokens(SYSTEM) + estimated_tokens(instructions) + estimated_tokens(content)


def _settings(model: str) -> tuple[str | None, float]:
    try:
        return models.reasoning_effort(model), models.temperature(model)
    except Exception:  # noqa: BLE001 - a broken models.yaml shouldn't break the call itself
        return None, models.DEFAULT_TEMPERATURE


def complete(model: str, instructions: str, content: str, schema: dict = SCHEMA, think: bool = True) -> str:
    """The model's reply text. Raises LlmError when the prompt doesn't fit, the call fails, or the reply
    shows the answer or the prompt was cut short."""
    # An overflowing prompt can be cut off silently and still look clean: refuse it.
    budget = config.CONTEXT_TOKENS - config.OUTPUT_TOKENS
    needed = prompt_tokens(instructions, content)
    if needed > budget:
        raise LlmError(f"prompt is ~{needed} tokens but the context budget is {budget}; "
                       "send a smaller diff or raise the server's context and LLM_CONTEXT_TOKENS")
    resp = request(model, instructions, content, schema, think)
    check_reply(model, resp, needed)
    return resp.choices[0].message.content or ""


def request(model: str, instructions: str, content: str, schema: dict = SCHEMA, think: bool = True):
    """One chat request in Vader's format, returning the server's reply as is (bench.py measures it).
    think=False asks a thinking model to answer straight away (reasoning_effort "none")."""
    # Structured outputs: the server enforces this schema.
    extra = {"response_format": {"type": "json_schema",
                                 "json_schema": {"name": "reply", "strict": True, "schema": schema}}}
    effort, temperature = _settings(model)
    if not think:
        effort = "none"
    if effort:
        extra["reasoning_effort"] = effort  # for reasoning models like gpt-oss
    try:
        resp = _client().chat.completions.create(
            model=model,
            messages=messages(instructions, content),
            temperature=temperature,
            max_tokens=config.OUTPUT_TOKENS,
            **extra,
        )
    except openai.APITimeoutError:
        raise LlmError(f"{model} timed out after {config.TIMEOUT_SECONDS:g}s") from None
    except openai.APIConnectionError:  # a timeout is one too, caught above: a slow server isn't a down one
        raise LlmUnavailable(f"can't reach the model server at {config.LLM_BASE_URL} -- is it running?") from None
    except openai.NotFoundError:
        raise LlmError(f"model {model} isn't available on the model server (pulled/downloaded?)") from None
    except openai.APIError as e:
        raise LlmError(f"{model} call failed: {e}") from None
    return resp


def check_reply(model: str, resp, estimated: int) -> None:
    """Raise unless the model finished its answer and read the whole prompt (by the server's own token
    counts, when it reports them)."""
    if resp.choices[0].finish_reason == "length":
        raise OutputLimit(f"{model} hit the output limit ({config.OUTPUT_TOKENS} tokens) before finishing its "
                       "answer; thinking models spend part of that budget reasoning, so raise LLM_OUTPUT_TOKENS")
    usage = resp.usage
    read, wrote = getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None) or 0
    if not read:
        return
    if read < estimated * CUT_OFF_BELOW:
        raise LlmError(f"{model} read only {read} of the prompt's ~{estimated} tokens: the server cut the prompt off "
                       f"(its context window is probably smaller than LLM_CONTEXT_TOKENS={config.CONTEXT_TOKENS})")
    if read + wrote > config.CONTEXT_TOKENS:
        raise LlmError(f"{model}'s prompt ({read} tokens) and reply ({wrote}) overflowed the "
                       f"{config.CONTEXT_TOKENS}-token context, so the start of the prompt may have been dropped; "
                       "lower LLM_OUTPUT_TOKENS or raise the context")
