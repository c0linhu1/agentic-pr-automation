"""Loading a skill's instructions, and Vader's saved state."""
import tempfile
import unittest
from pathlib import Path

from vader.skill_loader import load_skill
from vader.state import State


def write_skill(folder: Path, text: str, refs: dict[str, str] | None = None) -> Path:
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(text, encoding="utf-8")
    for name, body in (refs or {}).items():
        (folder / "references").mkdir(exist_ok=True)
        (folder / "references" / f"{name}.md").write_text(body, encoding="utf-8")
    return folder


class SkillLoaderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_routing_text_is_left_out_wherever_it_is_and_references_are_added(self):
        for where, body in [("first", "## When to Activate\nOn PRs.\n\n## Scope\n| logic | x |\n"),
                            ("last", "## Scope\n| logic | x |\n\n## When to Activate\nOn PRs.\n")]:
            with self.subTest(where=where):
                skill = load_skill(write_skill(self.root / where, f"---\nname: s\ndescription: d\n---\n{body}",
                                               refs={"dates": "Weekdays matter."}))
                self.assertNotIn("Activate", skill.prompt)
                self.assertIn("## Reference: dates\n\nWeekdays matter.", skill.prompt)
                self.assertEqual(skill.categories, ("logic",))

    def test_frontmatter_without_a_name_says_so(self):
        folder = write_skill(self.root / "bad", "---\ndescription: d\n---\nBody\n")
        with self.assertRaisesRegex(ValueError, "needs a name and a description"):
            load_skill(folder)


class StateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "state.json"

    def test_survives_a_restart(self):
        State(self.path, default_model="m:1b").mark_held("o/r#1", "abc", "HOLD: race")
        again = State(self.path, default_model="other:1b")
        self.assertEqual((again.model, again.held_reason("o/r#1", "abc")), ("m:1b", "HOLD: race"))
        self.assertIsNone(again.held_reason("o/r#1", "newer"))  # a new commit isn't held by the old review

    def test_a_misspelled_setting_is_refused_not_saved(self):
        state = State(self.path)
        with self.assertRaises(KeyError):
            state.set(automaton=True)
        self.assertFalse(self.path.exists())

    def test_a_broken_file_stops_startup_with_a_clear_message(self):
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "isn't valid JSON"):
            State(self.path)


if __name__ == "__main__":
    unittest.main()
