import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from slack_bot import fmt
from vader import config
from vader.engine import VERIFY_TASK, Engine, Events, Review
from vader.state import State


def finding(severity, description):
    # a distinct line per finding, so duplicate-merging leaves them separate
    return {"severity": severity, "category": "logic", "file": "a.cs", "line": len(description),
            "description": description, "suggestedFix": None}


class VerifyTests(unittest.TestCase):
    """A second look at high/critical findings; only an explicit "rejected" drops one."""

    def run_review(self, findings, verdicts):
        calls = []

        def complete(model, instructions, content, schema=None):
            if instructions.startswith(VERIFY_TASK):
                claim = json.loads(instructions[len(VERIFY_TASK):])
                calls.append(claim["description"])
                v = verdicts[claim["description"]]
                if isinstance(v, Exception):
                    raise v
                return json.dumps({"verdict": v, "reason": f"{v} because"})
            return json.dumps({"findings": findings})

        with tempfile.TemporaryDirectory() as d, mock.patch.object(config, "VERIFY_SEVERITIES", {"critical", "high"}):
            engine = Engine(State(Path(d) / "s.json", "m:1b"), Events(), complete=complete,
                            prepare_model=lambda m: None, assignments=dict)
            engine.start()
            review = Review(source="slack", validators=["correctness"], diff="+x")
            engine.submit(review)
            end = time.time() + 5
            while review.status != "done" and time.time() < end:
                time.sleep(0.02)
        return review, calls

    def test_rejected_high_finding_is_dropped_and_recorded(self):
        review, calls = self.run_review([finding("high", "wrong weekday"), finding("medium", "style")],
                                        {"wrong weekday": "rejected"})
        result = review.results[0]
        self.assertEqual(calls, ["wrong weekday"])                       # medium isn't re-checked
        self.assertEqual([f["description"] for f in result["findings"]], ["style"])
        self.assertEqual(result["rejected"][0]["description"], "wrong weekday")
        self.assertEqual(review.verdict, "AUTO-FIX")                     # the false high no longer HOLDs it
        self.assertNotIn("_part", json.dumps(result["findings"]))

    def test_confirmed_or_unverifiable_findings_are_kept(self):
        review, _ = self.run_review([finding("critical", "sql injection"), finding("high", "flaky check")],
                                    {"sql injection": "confirmed", "flaky check": RuntimeError("timed out")})
        self.assertEqual(len(review.results[0]["findings"]), 2)
        self.assertEqual(review.results[0]["rejected"], [])
        self.assertEqual(review.verdict, "BLOCK")

    def test_slack_shows_what_was_dropped(self):
        text = fmt.validator_result({"status": "ok", "findings": [], "seconds": 1, "model": "m", "dropped": 0,
                                     "rejected": [{"severity": "high", "description": "wrong weekday",
                                                   "reason": "2028-04-13 is a Thursday"}]})
        self.assertIn("Dropped after re-check (high): wrong weekday", text)
        self.assertIn("2028-04-13 is a Thursday", text)


if __name__ == "__main__":
    unittest.main()
