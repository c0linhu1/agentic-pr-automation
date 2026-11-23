"""Allowed models (models.yaml), what the model server has, and the one-model-at-a-time rule.

Works with any OpenAI-compatible server: the model list comes from its /models endpoint, and the server
loads models itself (MODEL_RUNTIME=generic). With MODEL_RUNTIME=ollama, Vader also loads one model at a
time through Ollama's own API, checks the context it was loaded with, and reports memory use."""
import time
from dataclasses import dataclass

import httpx
import yaml

from . import config

TIMEOUT = httpx.Timeout(10.0, read=600.0)  # loading a large model can take minutes
LOAD_ATTEMPTS = 2


class ModelServerDown(Exception):
    """The model server can't be reached at all (not running, or a wrong address): a review that hit this
    saw nothing, so it's tried again rather than recorded."""


@dataclass(frozen=True)
class ModelEntry:
    name: str
    tier: str
    enabled: bool
    note: str


def _ollama() -> bool:
    return config.MODEL_RUNTIME == "ollama"


def normalize(name: str) -> str:
    """Ollama reads a name without a tag as `:latest`; other servers use names exactly as given."""
    return f"{name}:latest" if _ollama() and ":" not in name else name


def _read() -> dict:
    return yaml.safe_load(config.MODELS_FILE.read_text(encoding="utf-8")) or {}


def catalog() -> tuple[str | None, list[ModelEntry]]:
    """(default model, allowed models). Re-read on every call so edits apply live."""
    data = _read()
    entries = [ModelEntry(normalize(m["name"]), str(m.get("tier", "")), bool(m.get("enabled", True)),
                          str(m.get("note", ""))) for m in data.get("models", [])]
    default = data.get("default")
    return (normalize(default) if default else None), entries


EFFORT_LEVELS = ("low", "medium", "high")
DEFAULT_TEMPERATURE = 0.1


def _entry(name: str) -> dict:
    name = normalize(name)
    return next((m for m in _read().get("models", []) if normalize(str(m.get("name", ""))) == name), {})


def reasoning_effort(name: str) -> str | None:
    """A model's `reasoning_effort:` from models.yaml (low/medium/high), or None to leave it to the model.
    Sent as the standard OpenAI-API parameter."""
    value = str(_entry(name).get("reasoning_effort", "")).lower()
    return value if value in EFFORT_LEVELS else None


def temperature(name: str) -> float:
    """A model's `temperature:` from models.yaml, else 0.1. Thinking models want what their makers
    recommend: near 0 they can reason in circles until they run out of reply space."""
    try:
        return float(_entry(name).get("temperature", DEFAULT_TEMPERATURE))
    except (TypeError, ValueError):
        return DEFAULT_TEMPERATURE


def assignments() -> dict[str, str]:
    """validator key -> model, from the `validators:` section. Validators not listed use
    the active model. Re-read on every call so edits apply live."""
    return {str(k).lower(): normalize(str(v)) for k, v in (_read().get("validators") or {}).items() if v}


def available() -> set[str] | None:
    """Models the server can run (pulled or downloaded), or None if it can't be reached."""
    try:
        r = httpx.get(f"{config.LLM_BASE_URL}/models", timeout=5,
                      headers={"Authorization": f"Bearer {config.LLM_API_KEY}"})
        r.raise_for_status()
        return {m["id"] for m in r.json().get("data", [])}
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None


def loaded() -> list[dict] | None:
    """Models in memory: [{name, size_gb, vram_gb, context}], or None when that's unknown (a generic
    server doesn't say, or Ollama can't be reached)."""
    if not _ollama():
        return None
    try:
        r = httpx.get(f"{config.OLLAMA_URL}/api/ps", timeout=5)
        r.raise_for_status()
    except httpx.HTTPError:
        return None
    return [{"name": m["name"], "size_gb": round(m.get("size", 0) / 1e9, 1),
             "vram_gb": round(m.get("size_vram", 0) / 1e9, 1), "context": m.get("context_length")}
            for m in r.json().get("models", [])]


def unload(name: str) -> None:
    """Free a model's memory (Ollama only). Best effort: a server that's gone has nothing loaded."""
    if not _ollama():
        return
    try:
        httpx.post(f"{config.OLLAMA_URL}/api/generate", json={"model": name, "keep_alive": 0}, timeout=TIMEOUT)
    except httpx.HTTPError:
        pass


def ensure_only(name: str) -> None:
    """With Ollama: unload every other model, then load this one, and raise if that fails. A generic server
    loads the model on its first request, so there's nothing to do here."""
    if not _ollama():
        return
    try:
        _ensure_only(normalize(name))
    except httpx.ConnectError as e:
        raise ModelServerDown(f"can't reach Ollama at {config.OLLAMA_URL} ({e}); is it running?") from None


def _ensure_only(name: str) -> None:
    for m in loaded() or []:
        if m["name"] != name:
            unload(m["name"])
    for attempt in range(LOAD_ATTEMPTS):
        r = httpx.post(f"{config.OLLAMA_URL}/api/generate", json={"model": name}, timeout=TIMEOUT)
        if r.status_code == 404:
            raise RuntimeError(f"model {name} isn't pulled; run `ollama pull {name}`")
        # Ollama's GPU runner sometimes crashes on its first load after a restart; a retry works.
        if r.status_code < 500 or attempt == LOAD_ATTEMPTS - 1:
            r.raise_for_status()
            break
        time.sleep(3)
    _check_context(name)


def _check_context(name: str) -> None:
    """Refuse a model Ollama loaded with less context than Vader budgets for, before any prompt is cut off.
    (On every server, llm.py also checks each reply's token counts for a cut-off prompt.)"""
    ctx = next((m["context"] for m in loaded() or [] if m["name"] == name), None)
    if ctx and ctx < config.CONTEXT_TOKENS:
        raise RuntimeError(f"Ollama loaded {name} with a {ctx}-token context but LLM_CONTEXT_TOKENS is "
                           f"{config.CONTEXT_TOKENS}; set OLLAMA_CONTEXT_LENGTH={config.CONTEXT_TOKENS} and "
                           "restart Ollama, or lower LLM_CONTEXT_TOKENS")


def check_allowed(name: str) -> str | None:
    """Why models.yaml doesn't allow this model, or None if it does."""
    name = normalize(name)
    _, entries = catalog()
    entry = next((e for e in entries if e.name == name), None)
    if entry is None:
        return f"`{name}` isn't in the allowed list (models.yaml)"
    if not entry.enabled:
        return f"`{name}` is in the list but disabled"
    return None


def prepare(name: str) -> None:
    """Get this model ready for a review, if models.yaml allows it. Raises otherwise."""
    problem = check_allowed(name)
    if problem:
        raise RuntimeError(problem)
    ensure_only(name)


def check_selectable(name: str) -> str | None:
    """Why this model can't be selected, or None if it can."""
    name = normalize(name)
    problem = check_allowed(name)
    if problem:
        return problem
    have = available()
    if have is None:
        return f"can't reach the model server at {config.LLM_BASE_URL} to check which models it has"
    if name not in have:
        return f"`{name}` isn't on the model server yet; pull or download it there first"
    return None
