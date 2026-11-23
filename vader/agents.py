"""The six validators: each is a name, how Slack shows it, and the skill that holds its instructions."""
from dataclasses import dataclass

from . import config
from .findings import schema_for
from .skill_loader import Skill, load_skill


@dataclass(frozen=True)
class Validator:
    key: str
    display_name: str
    icon: str
    skill: str

    def _skill(self) -> Skill:
        return load_skill(config.SKILLS_DIR / self.skill)  # re-read each time, so edits apply live

    def instructions(self) -> str:
        """The skill, as this validator's task instructions."""
        return self._skill().prompt

    def schema(self) -> dict:
        """The reply shape, with categories limited to the ones in this validator's skill."""
        return schema_for(self._skill().categories)


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
    """A validator by key or alias, any case (`/vader run perf …`); None if there's no such one."""
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
