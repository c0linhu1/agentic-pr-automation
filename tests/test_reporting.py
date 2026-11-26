import unittest
from unittest import mock

from slack_bot import fmt
from tests.helpers import response, use_github
from vader import config, report
from vader.engine import Events, Review
from vader.github import GitHubPoller


CRITICAL = {"severity": "critical", "category": "injection", "file": "api/search.py", "line": 3,
            "description": "SQL built from user input.", "suggestedFix": "Use parameters."}


def done_review() -> Review:
    r = Review(source="github", validators=["security", "qa"], title="Add search",
               meta={"repo": "o/r", "number": 7, "url": "https://github.com/o/r/pull/7", "sha": "abcdef123",
                     "pr_key": "o/r#7"})
    r.status, r.verdict, r.verdict_reason = "done", "BLOCK", "1 critical finding(s)"
    r.started, r.finished, r.model, r.files = 100.0, 160.0, "m:1b", ["api/search.py"]
    r.models = {"security": "m:1b", "qa": "m:1b"}
    r.results = [
        {"validator": "security", "display_name": "Security Validator", "icon": ":shield:", "status": "ok",
         "findings": [dict(CRITICAL)], "error": None},
        {"validator": "qa", "display_name": "QA Validator", "icon": ":test_tube:", "status": "failed",
         "findings": [], "error": "timed out"},
    ]
    return r


class PrCommentTests(unittest.TestCase):
    def setUp(self):
        self.events = Events()
        self.poller = GitHubPoller(mock.Mock(), self.events, mock.Mock())
        use_github(self)
        self.enterContext(mock.patch.object(config, "GITHUB_COMMENT", True))

    def test_posts_the_report_on_the_pr(self):
        self.poller.comment_on_pr(done_review())
        (number, body), = self.gh.comments
        self.assertEqual(number, 7)
        self.assertIn("BLOCK", body)
        self.assertEqual([(e["type"], e.get("url")) for e in self.events.since(0)],
                         [("pr_comment_posted", "https://github.com/repos/o/r/issues/7/comments#c1")])

    def test_missing_write_permission_is_reported_not_raised(self):
        self.gh.comment_reply = response(403, "forbidden")
        self.poller.comment_on_pr(done_review())
        event, = self.events.since(0)
        self.assertEqual(event["type"], "pr_comment_failed")
        self.assertIn("Read and write", event["error"])

    def test_pasted_diffs_and_disabled_comments_are_skipped(self):
        self.poller.comment_on_pr(Review(source="slack", validators=["qa"], diff="+x"))
        with mock.patch.object(config, "GITHUB_COMMENT", False):
            self.poller.comment_on_pr(done_review())
        self.assertEqual(self.gh.comments, [])


class ReportTests(unittest.TestCase):
    def test_github_markdown(self):
        text = report.markdown(done_review())
        self.assertIn("Vader review: **BLOCK**", text)
        self.assertIn("Commit `abcdef1`", text)
        self.assertIn("| QA Validator | ⚠️ failed: timed out |", text)
        self.assertIn("`api/search.py:3`", text)
        self.assertIn("Use parameters.", text)

    def test_hidden_and_malformed_findings_are_counted_and_errors_stay_in_one_row(self):
        r = done_review()
        r.results[0].update(omitted=4, dropped=1)
        r.results[1]["error"] = "part 1: timed out\npart 2: bad | reply"
        text = report.markdown(r)
        self.assertIn("| Security Validator | 1 finding(s), and 4 lower-severity one(s) not shown "
                      "(1 malformed finding(s) dropped) |", text)
        self.assertIn("| QA Validator | ⚠️ failed: part 1: timed out part 2: bad / reply |", text)

    def test_slack_report(self):
        text = fmt.verdict(done_review().summary())
        self.assertIn("*Verdict: BLOCK*", text)
        self.assertIn("<https://github.com/o/r/pull/7|o/r#7>", text)
        self.assertIn("Security Validator: :red_circle: 1 finding(s), worst *critical*", text)
        self.assertIn("QA Validator: :warning: failed: timed out", text)
        self.assertIn("*Most serious*", text)


if __name__ == "__main__":
    unittest.main()
