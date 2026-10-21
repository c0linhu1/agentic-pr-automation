import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from slack_bot import fmt
from vader import config, report
from vader.engine import Engine, Events, Review
from vader.github import GitHubPoller
from vader.state import State


def wait_for(cond, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


CRITICAL = json.dumps({"findings": [{"severity": "critical", "category": "injection", "file": "api/search.py",
                                     "line": 3, "description": "SQL built from user input.",
                                     "suggestedFix": "Use parameters."}]})


def done_review() -> Review:
    r = Review(source="github", validators=["security", "qa"], title="Add search",
               meta={"repo": "o/r", "number": 7, "url": "https://github.com/o/r/pull/7", "sha": "abcdef123",
                     "pr_key": "o/r#7"})
    r.status, r.verdict, r.verdict_reason = "done", "BLOCK", "1 critical finding(s)"
    r.started, r.finished, r.model, r.files = 100.0, 160.0, "m:1b", ["api/search.py"]
    r.models = {"security": "m:1b", "qa": "m:1b"}
    r.results = [
        {"validator": "security", "display_name": "Security Validator", "icon": ":shield:", "status": "ok",
         "findings": json.loads(CRITICAL)["findings"], "error": None},
        {"validator": "qa", "display_name": "QA Validator", "icon": ":test_tube:", "status": "failed",
         "findings": [], "error": "timed out"},
    ]
    return r


class OnDoneTests(unittest.TestCase):
    def test_on_done_runs_after_each_review(self):
        with tempfile.TemporaryDirectory() as d:
            state = State(Path(d) / "state.json", default_model="m:1b")
            engine = Engine(state, Events(), complete=lambda *a, **k: '{"findings": []}',
                            prepare_model=lambda m: None, assignments=dict)
            seen = []
            engine.on_done = seen.append
            engine.start()
            r = Review(source="slack", validators=["qa"], diff="+x")
            engine.submit(r)
            self.assertTrue(wait_for(lambda: seen == [r]))


class PrCommentTests(unittest.TestCase):
    def setUp(self):
        self.events = Events()
        self.poller = GitHubPoller(mock.Mock(), self.events, mock.Mock())
        self.gh = mock.MagicMock()
        self.gh.__enter__.return_value = self.gh
        patches = [mock.patch.object(GitHubPoller, "_client", return_value=self.gh),
                   mock.patch.object(config, "GITHUB_TOKEN", "t"), mock.patch.object(config, "GITHUB_COMMENT", True)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_posts_the_report_on_the_pr(self):
        self.gh.post.return_value = mock.Mock(status_code=201, json=lambda: {"html_url": "https://x/c/1"})
        self.poller.comment_on_pr(done_review())
        path, = self.gh.post.call_args.args
        self.assertEqual(path, "/repos/o/r/issues/7/comments")
        self.assertIn("BLOCK", self.gh.post.call_args.kwargs["json"]["body"])
        self.assertEqual([(e["type"], e.get("url")) for e in self.events.since(0)],
                         [("pr_comment_posted", "https://x/c/1")])

    def test_missing_write_permission_is_reported_not_raised(self):
        self.gh.post.return_value = mock.Mock(status_code=403, text="forbidden")
        self.poller.comment_on_pr(done_review())
        event, = self.events.since(0)
        self.assertEqual(event["type"], "pr_comment_failed")
        self.assertIn("Read and write", event["error"])

    def test_pasted_diffs_and_disabled_comments_are_skipped(self):
        self.poller.comment_on_pr(Review(source="slack", validators=["qa"], diff="+x"))
        with mock.patch.object(config, "GITHUB_COMMENT", False):
            self.poller.comment_on_pr(done_review())
        self.gh.post.assert_not_called()


class ReportTests(unittest.TestCase):
    def test_github_markdown(self):
        text = report.markdown(done_review())
        self.assertIn("Vader review: **BLOCK**", text)
        self.assertIn("Commit `abcdef1`", text)
        self.assertIn("| QA Validator | ⚠️ failed: timed out |", text)
        self.assertIn("`api/search.py:3`", text)
        self.assertIn("Use parameters.", text)

    def test_slack_report(self):
        text = fmt.verdict(done_review().summary())
        self.assertIn("*Verdict: BLOCK*", text)
        self.assertIn("<https://github.com/o/r/pull/7|o/r#7>", text)
        self.assertIn("Security Validator: :red_circle: 1 finding(s), worst *critical*", text)
        self.assertIn("QA Validator: :warning: failed: timed out", text)
        self.assertIn("*Most serious*", text)


if __name__ == "__main__":
    unittest.main()
