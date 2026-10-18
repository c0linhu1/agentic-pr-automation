"""Agent registry. A reviewer is a config row: role -> (skill folder, model).
Changing which model plays a role is a one-line edit. Pick models with `ollama list`.
Spread roles across model families so their blind spots don't overlap."""
from dataclasses import dataclass
from pathlib import Path

from skill_loader import load_skill

SKILLS_DIR = Path(__file__).parent / "skills"


@dataclass(frozen=True)
class Agent:
    name: str
    model: str
    system_prompt: str
    json_output: bool = False


REVIEWERS = {
    # role          skill folder            model (placeholder: swap for what you pulled)
    "security":     ("security-review",     "qwen2.5-coder:7b"),
    "correctness":  ("correctness-review",  "llama3.1:8b"),
    "qa":           ("qa-review",           "gemma3:4b"),
    "architecture": ("architecture-review", "mistral:7b"),
    "compliance":   ("compliance-review",   "phi4-mini"),
    "performance":  ("performance-review",  "qwen2.5-coder:7b"),  # +1 role; shares a model for now
}


def _build() -> dict[str, Agent]:
    agents = {
        "echo": Agent("echo", "llama3.2:3b", "You are a test agent. Answer in one or two short sentences."),
    }
    for role, (skill_folder, model) in REVIEWERS.items():
        skill = load_skill(SKILLS_DIR / skill_folder)
        agents[role] = Agent(role, model, skill.prompt, json_output=True)
    agents["logic"] = agents["correctness"]  # alias
    agents["integrity"] = agents["correctness"]  # alias: PR title/description/ticket vs diff
    return agents


AGENTS: dict[str, Agent] = _build()


def get_agent(name: str | None) -> Agent | None:
    return AGENTS.get(name.lower()) if name else None
