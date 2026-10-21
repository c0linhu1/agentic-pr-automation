"""Allowed models (models.yaml) and the one-model-at-a-time rule, using Ollama's API to see
what's pulled and loaded, and to load and unload models."""
import time
from dataclasses import dataclass

import httpx
import yaml

from . import config

TIMEOUT = httpx.Timeout(10.0, read=600.0)  # loading a large model can take minutes
LOAD_ATTEMPTS = 2


@dataclass(frozen=True)
class ModelEntry:
    name: str
    tier: str
    enabled: bool
    note: str


def normalize(name: str) -> str:
    return name if ":" in name else f"{name}:latest"


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


def reasoning_effort(name: str) -> str | None:
    """A model's `reasoning_effort:` from models.yaml (low/medium/high), or None to leave it
    to the model. Sent as the standard OpenAI-API parameter, so it isn't tied to Ollama."""
    name = normalize(name)
    for m in _read().get("models", []):
        if normalize(str(m.get("name", ""))) == name:
            value = str(m.get("reasoning_effort", "")).lower()
            return value if value in EFFORT_LEVELS else None
    return None


def assignments() -> dict[str, str]:
    """validator key -> model, from the `validators:` section. Validators not listed use
    the active model. Re-read on every call so edits apply live."""
    return {str(k).lower(): normalize(str(v)) for k, v in (_read().get("validators") or {}).items() if v}


def pulled() -> set[str] | None:
    """Models Ollama has downloaded, or None if Ollama can't be reached."""
    try:
        r = httpx.get(f"{config.OLLAMA_URL}/api/tags", timeout=5)
        r.raise_for_status()
    except httpx.HTTPError:
        return None
    return {m["name"] for m in r.json().get("models", [])}


def loaded() -> list[dict] | None:
    """Models currently in memory: [{name, size_gb, vram_gb}], or None if Ollama can't be reached."""
    try:
        r = httpx.get(f"{config.OLLAMA_URL}/api/ps", timeout=5)
        r.raise_for_status()
    except httpx.HTTPError:
        return None
    return [{"name": m["name"], "size_gb": round(m.get("size", 0) / 1e9, 1),
             "vram_gb": round(m.get("size_vram", 0) / 1e9, 1), "context": m.get("context_length")}
            for m in r.json().get("models", [])]


def unload(name: str) -> None:
    httpx.post(f"{config.OLLAMA_URL}/api/generate", json={"model": name, "keep_alive": 0}, timeout=TIMEOUT)


def ensure_only(name: str) -> None:
    """Unload every other model, then load this one. Raises if Ollama can't do it."""
    name = normalize(name)
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
    """Refuse a model Ollama loaded with less context than Vader budgets for: prompts would be
    cut off silently, and a cut-off review still looks clean."""
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
    """Load this model for a review, if models.yaml allows it. Raises otherwise."""
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
    have = pulled()
    if have is None:
        return "can't reach Ollama to check which models are pulled"
    if name not in have:
        return f"`{name}` isn't pulled yet; run `ollama pull {name}` on the Vader machine"
    return None
