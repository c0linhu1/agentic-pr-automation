"""The six validators under Vader. Each is a skill (what to look for) plus the name
and icon it posts under. Which model each runs on is set in models.yaml.

Skills are re-read for every review, so edits to skills/*/SKILL.md apply without a restart."""
from dataclasses import dataclass

from . import config
from .skill_loader import load_skill


@dataclass(frozen=True)
class Validator:
    key: str
    display_name: str
    icon: str
    skill: str

    def instructions(self) -> str:
        """The skill, as this validator's task instructions (re-read each time, so edits apply live)."""
        return load_skill(config.SKILLS_DIR / self.skill).prompt


VALIDATORS: dict[str, Validator] = {v.key: v for v in [
    Validator("security", "Security Validator", ":shield:", "security-review"),
    Validator("correctness", "Correctness Validator", ":dart:", "correctness-review"),
    Validator("qa", "QA Validator", ":test_tube:", "qa-review"),
    Validator("architecture", "Architecture Validator", ":building_construction:", "architecture-review"),
    Validator("compliance", "Compliance Validator", ":scales:", "compliance-review"),
    Validator("performance", "Performance Validator", ":zap:", "performance-review"),
]}

ALIASES = {"logic": "correctness", "integrity": "correctness", "tests": "qa", "perf": "performance"}


def resolve(name: str) -> Validator | None:
    key = name.strip().lower()
    return VALIDATORS.get(ALIASES.get(key, key))


def check_skills() -> list[str]:
    """Problems loading any skill; empty when all six load."""
    problems = []
    for v in VALIDATORS.values():
        try:
            v.instructions()
        except Exception as e:  # noqa: BLE001 - report every broken skill, not just the first
            problems.append(f"{v.skill}: {e}")
    return problems
