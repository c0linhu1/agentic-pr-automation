import json
import unittest
from unittest import mock

from tests.helpers import EngineCase, FakeClock
from vader import config, report
from vader.engine import VERIFY_TASK, Review


def finding(severity, description):
    # a distinct line per finding, so duplicate-merging leaves them separate
    return {"severity": severity, "category": "logic", "file": "a.cs", "line": len(description),
            "description": description, "suggestedFix": None}


class VerifyTests(EngineCase):
    """A second look at high/critical findings; only an explicit "rejected" drops one."""

    def run_review(self, findings, verdicts, diff="+x", description=""):
        calls, self.claims = [], []

        def complete(model, instructions, content, schema=None):
            if instructions.startswith(VERIFY_TASK):
                claim = json.loads(instructions[len(VERIFY_TASK):])
                calls.append(claim["description"])
                self.claims.append(claim)
                v = verdicts[claim["description"]]
                if isinstance(v, Exception):
                    raise v
                return json.dumps({"verdict": v, "reason": f"{v} because"})
            return json.dumps({"findings": findings})

        with mock.patch.object(config, "VERIFY_SEVERITIES", {"critical", "high"}):
            review = self.finish(Review(source="slack", validators=["correctness"], diff=diff,
                                        description=description), complete=complete)
        return review, calls

    def test_rejected_high_finding_is_dropped_and_recorded(self):
        review, calls = self.run_review([finding("high", "wrong weekday"), finding("medium", "style")],
                                        {"wrong weekday": "rejected"})
        result = review.results[0]
        self.assertEqual(calls, ["wrong weekday"])  # medium isn't re-checked
        self.assertEqual([f["description"] for f in result["findings"]], ["style"])
        self.assertEqual(result["rejected"][0]["description"], "wrong weekday")
        self.assertEqual(review.verdict, "MINOR")  # the false high no longer HOLDs it
        self.assertNotIn("_part", json.dumps(result["findings"]))

    def test_a_finding_quoting_code_that_isnt_there_is_dropped_before_the_recheck(self):
        diff = ("diff --git a/api/search.py b/api/search.py\n--- a/api/search.py\n+++ b/api/search.py\n"
                "@@ -40,2 +40,3 @@ def search(term):\n     cur = db.cursor()\n"
                "+    cur.execute(\"SELECT * FROM items WHERE name = '\" + term + \"'\")\n     return cur.fetchall()\n")
        real = dict(finding("critical", "sql injection"), file="search.py", line=1,
                    evidence="cur.execute(\"SELECT * FROM items")
        made_up = dict(finding("high", "no timeout"), file="api/search.py", evidence="requests.get(url)")
        review, calls = self.run_review([real, made_up], {"sql injection": "confirmed"}, diff=diff)
        result = review.results[0]
        self.assertEqual(calls, ["sql injection"])  # the made-up one never cost a model call
        self.assertEqual(self.claims[0]["evidence"], real["evidence"])  # the second look sees the quote
        kept, = result["findings"]
        self.assertEqual((kept["file"], kept["line"]), ("api/search.py", 41))
        self.assertEqual(result["rejected"], [{"severity": "high", "description": "no timeout",
                                               "reason": "the code it quotes isn't in api/search.py",
                                               "evidence": "requests.get(url)"}])
        self.assertEqual(review.verdict, "BLOCK")
        self.assertIn("_It quoted:_ `requests.get(url)`", report.markdown(review))  # to judge the drop

    def test_a_claim_quoting_the_pr_description_is_kept(self):
        claim = dict(finding("high", "no tests added"), file=None, line=None, evidence="adds unit tests for it")
        review, calls = self.run_review([claim], {"no tests added": "confirmed"}, diff="+    if x is None: return",
                                        description="Adds a None check and adds unit tests for it.")
        self.assertEqual(calls, ["no tests added"])
        self.assertEqual(review.results[0]["rejected"], [])

    def test_confirmed_or_unverifiable_findings_are_kept(self):
        with self.assertLogs("vader.engine", "WARNING"):
            review, _ = self.run_review([finding("critical", "sql injection"), finding("high", "flaky check")],
                                        {"sql injection": "confirmed", "flaky check": RuntimeError("timed out")})
        self.assertEqual(len(review.results[0]["findings"]), 2)
        self.assertEqual(review.results[0]["rejected"], [])
        self.assertEqual(review.verdict, "BLOCK")

    def test_past_the_time_limit_findings_are_kept_unchecked(self):
        clock, rechecks = FakeClock(), []

        def complete(model, instructions, content, schema=None):
            if instructions.startswith(VERIFY_TASK):
                rechecks.append(instructions)
                return json.dumps({"verdict": "rejected", "reason": "r"})
            clock.now += 90  # the review itself takes 1.5 minutes
            return json.dumps({"findings": [finding("high", "race")]})

        with mock.patch("vader.engine.time", clock), mock.patch.object(config, "REVIEW_TIME_LIMIT_MINUTES", 1), \
                mock.patch.object(config, "VERIFY_SEVERITIES", {"critical", "high"}):
            review = self.finish(Review(source="slack", validators=["correctness"], diff="+x"), complete=complete)
        self.assertEqual(rechecks, [])
        self.assertEqual([f["description"] for f in review.results[0]["findings"]], ["race"])
        self.assertEqual(review.verdict, "HOLD")


if __name__ == "__main__":
    unittest.main()
