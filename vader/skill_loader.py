"""Loads a SKILL.md folder into a system prompt for a plain Ollama chat model.

Coding agents (Claude Code etc.) read references/ on demand. A plain chat
completion can't open files, so the loader inlines the references and drops the
"When to Activate" section, which is for dispatch, not for the reviewer."""
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.S)
ACTIVATE_SECTION = re.compile(r"^## When to Activate\n.*?(?=^## )", re.S | re.M)


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    prompt: str


def load_skill(skill_dir: Path) -> Skill:
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    m = FRONTMATTER.match(text)
    if not m:
        raise ValueError(f"{skill_dir}/SKILL.md has no frontmatter")
    meta = yaml.safe_load(m.group(1))
    body = ACTIVATE_SECTION.sub("", m.group(2)).strip()

    refs_dir = skill_dir / "references"
    refs = sorted(refs_dir.glob("*.md")) if refs_dir.is_dir() else []
    parts = [body] + [f"## Reference: {p.stem}\n\n{p.read_text(encoding='utf-8').strip()}" for p in refs]
    return Skill(name=meta["name"], description=str(meta["description"]).strip(), prompt="\n\n".join(parts))
