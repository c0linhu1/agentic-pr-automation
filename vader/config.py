"""Vader's settings, read from .env.vader (each is explained in .env.example).

Vader is the only program that holds the GitHub token and the model server address.
The Slack bot has its own .env.slack and never sees these."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
# Another settings file, or empty to read none (the tests use only their own settings).
_env_file = os.getenv("VADER_ENV_FILE", str(ROOT / ".env.vader"))
if _env_file:
    load_dotenv(_env_file)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name) or default


def _flag(name: str, default: bool = False) -> bool:
    return _env(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _list(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


# Vader's API, for the Slack bot
HOST = _env("VADER_HOST", "127.0.0.1")
PORT = int(_env("VADER_PORT", "8700"))
API_KEY = _env("VADER_API_KEY")

# The model server
_ollama_url = os.getenv("OLLAMA_URL", "").rstrip("/")
LLM_BASE_URL = _env("LLM_BASE_URL", f"{_ollama_url or 'http://localhost:11434'}/v1").rstrip("/")
MODEL_RUNTIME = _env("MODEL_RUNTIME", "generic").strip().lower()
OLLAMA_URL = _ollama_url or LLM_BASE_URL.removesuffix("/v1")
LLM_API_KEY = _env("LLM_API_KEY", "local")  # the OpenAI SDK requires one, even for a local server
TIMEOUT_SECONDS = float(_env("LLM_TIMEOUT_SECONDS", "600"))
# Must match the server's context (Ollama: OLLAMA_CONTEXT_LENGTH), or prompts are cut off.
CONTEXT_TOKENS = int(_env("LLM_CONTEXT_TOKENS", "32768"))
OUTPUT_TOKENS = int(_env("LLM_OUTPUT_TOKENS", "12288"))

# Reviews. os.getenv, not _env, where an empty value means off rather than the default.
SKILLS_DIR = Path(_env("SKILLS_DIR", str(ROOT / "skills")))
MODELS_FILE = Path(_env("MODELS_FILE", str(ROOT / "models.yaml")))
STATE_FILE = Path(_env("STATE_FILE", str(ROOT / "vader_state.json")))
_failed_dir = os.getenv("FAILED_REPLIES_DIR", str(ROOT / "failed_replies"))
FAILED_REPLIES_DIR = Path(_failed_dir) if _failed_dir else None
PARSE_RETRIES = int(_env("PARSE_RETRIES", "1"))
VALIDATOR_ASKS = {k.strip(): max(1, int(n)) for k, _, n in
                  (p.partition("=") for p in _list(_env("VALIDATOR_ASKS"))) if n}
VERIFY_SEVERITIES = set(_list(os.getenv("VERIFY_SEVERITIES", "critical,high")))
REVIEW_TIME_LIMIT_MINUTES = float(_env("REVIEW_TIME_LIMIT_MINUTES", "0"))

# GitHub
GITHUB_TOKEN = _env("GITHUB_TOKEN")
GITHUB_API = _env("GITHUB_API", "https://api.github.com").rstrip("/")
GITHUB_REPOS = _list(_env("GITHUB_REPOS"))
POLL_SECONDS = int(_env("POLL_SECONDS", "60"))
REVIEW_DRAFTS = _flag("REVIEW_DRAFTS")
GITHUB_COMMENT = _flag("GITHUB_COMMENT", True)
CLOSE_LINKED_ISSUES = _flag("CLOSE_LINKED_ISSUES")
REQUIRE_CI = _flag("REQUIRE_CI")
AUTO_MERGE_BRANCH = _env("AUTO_MERGE_BRANCH").strip()
NEEDS_PERSON_PATHS = _list(_env("NEEDS_PERSON_PATHS"))
TRUSTED_PACKAGE_HOSTS = {h.lower() for h in _list(_env("TRUSTED_PACKAGE_HOSTS"))}
