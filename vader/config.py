"""Vader's settings, read from .env.vader.

Vader is the only program that holds the GitHub token and the Ollama address.
The Slack bot has its own .env.slack and never sees these."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env.vader")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name) or default


def _flag(name: str, default: bool = False) -> bool:
    return _env(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


HOST = _env("VADER_HOST", "127.0.0.1")
PORT = int(_env("VADER_PORT", "8700"))
API_KEY = _env("VADER_API_KEY")

OLLAMA_URL = _env("OLLAMA_URL", "http://localhost:11434").rstrip("/")
# Any OpenAI-compatible server, e.g. LM Studio: http://localhost:1234/v1
LLM_BASE_URL = _env("LLM_BASE_URL", f"{OLLAMA_URL}/v1")
LLM_API_KEY = _env("LLM_API_KEY", "local")  # the SDK requires one
TIMEOUT_SECONDS = float(_env("LLM_TIMEOUT_SECONDS", "600"))  # per model call

SKILLS_DIR = Path(_env("SKILLS_DIR", str(ROOT / "skills")))
MODELS_FILE = Path(_env("MODELS_FILE", str(ROOT / "models.yaml")))
STATE_FILE = Path(_env("STATE_FILE", str(ROOT / "vader_state.json")))
# Where unparseable replies are saved; empty turns it off.
_failed_dir = os.getenv("FAILED_REPLIES_DIR", str(ROOT / "failed_replies"))
FAILED_REPLIES_DIR = Path(_failed_dir) if _failed_dir else None
# Times to re-ask after a reply in the wrong shape.
PARSE_RETRIES = int(_env("PARSE_RETRIES", "1"))
# Severities re-checked before posting; empty turns it off.
VERIFY_SEVERITIES = {s.strip() for s in os.getenv("VERIFY_SEVERITIES", "critical,high").split(",") if s.strip()}

GITHUB_TOKEN = _env("GITHUB_TOKEN")
GITHUB_API = _env("GITHUB_API", "https://api.github.com").rstrip("/")
GITHUB_REPOS = [r.strip() for r in _env("GITHUB_REPOS").split(",") if r.strip()]
POLL_SECONDS = int(_env("POLL_SECONDS", "60"))
REVIEW_DRAFTS = _flag("REVIEW_DRAFTS")  # Copilot opens draft PRs while it's still working
# Post each PR review as a PR comment (token needs Pull requests: write).
GITHUB_COMMENT = _flag("GITHUB_COMMENT", True)

REQUIRE_CI = _flag("REQUIRE_CI")  # missing CI results hold a release

# Context window and reply size, in tokens; match the server's (OLLAMA_CONTEXT_LENGTH).
CONTEXT_TOKENS = int(_env("LLM_CONTEXT_TOKENS", "32768"))
OUTPUT_TOKENS = int(_env("LLM_OUTPUT_TOKENS", "12288"))  # thinking models reason inside this budget

# Per-review time limit: remaining work is skipped and the verdict is HOLD. 0 = no limit.
REVIEW_TIME_LIMIT_MINUTES = float(_env("REVIEW_TIME_LIMIT_MINUTES", "0"))
