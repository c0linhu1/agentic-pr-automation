"""Every test runs on the settings below and the fake skills in tests/fixtures/skills, never on .env.vader,
the real skills, or the shell's environment, so the tests pass the same on any machine and never touch a real
GitHub repo or save files. A test that needs a setting turns it on itself."""
import os
from pathlib import Path

os.environ.update({
    "VADER_ENV_FILE": "",
    "SKILLS_DIR": str(Path(__file__).parent / "fixtures" / "skills"),
    "MODEL_RUNTIME": "generic", "LLM_BASE_URL": "http://localhost:11434/v1", "OLLAMA_URL": "", "LLM_API_KEY": "local",
    "LLM_CONTEXT_TOKENS": "32768", "LLM_OUTPUT_TOKENS": "12288", "LLM_TIMEOUT_SECONDS": "600",
    "FAILED_REPLIES_DIR": "", "PARSE_RETRIES": "1", "VALIDATOR_ASKS": "", "VERIFY_SEVERITIES": "",
    "REVIEW_TIME_LIMIT_MINUTES": "0", "REQUIRE_CI": "false",
    "GITHUB_TOKEN": "", "GITHUB_API": "https://api.github.com", "GITHUB_REPOS": "", "GITHUB_COMMENT": "true", "REVIEW_DRAFTS": "false",
    "AUTO_MERGE_BRANCH": "", "CLOSE_LINKED_ISSUES": "false", "NEEDS_PERSON_PATHS": "", "TRUSTED_PACKAGE_HOSTS": "",
})
