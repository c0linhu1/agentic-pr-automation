import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from vader import config
from vader.engine import Engine, Events, Review
from vader.github import GitHubPoller
from vader.state import State


def run(name, conclusion, status="completed"):
    return {"name": name, "status": status, "conclusion": conclusion}


class CiStatusTests(unittest.TestCase):
    def test_status(self):
        r = Review(source="github", validators=["qa"])
        self.assertIsNone(r.ci_status)                                   # unknown
        r.ci = [run("build-test", "success"), run("docker", "skipped")]
        self.assertEqual(r.ci_status, "passed")
        r.ci = [run("build-test", "failure"), run("docker", "success")]
        self.assertEqual(r.ci_status, "failed")
        r.ci = [run("build-test", None, status="in_progress")]
        self.assertIsNone(r.ci_status)                                   # still running

    def test_ci_line_goes_in_the_shared_header(self):
        r = Review(source="github", validators=["qa"], title="t")
        self.assertNotIn("CI:", r.header())                               # pasted diffs: no CI line
        r.ci = [run("build-test", "success")]
        self.assertIn("CI: build-test success. Every check passed", r.header())
        self.assertIn("doesn't prove the tests themselves are right", r.header())


class CiInVerdictTests(unittest.TestCase):
    def test_failed_ci_with_no_findings_is_auto_fix(self):
        with tempfile.TemporaryDirectory() as d:
            engine = Engine(State(Path(d) / "s.json", "m:1b"), Events(), complete=lambda *a, **k: '{"findings": []}',
                            prepare_model=lambda m: None, assignments=dict)
            engine.before_review = lambda r: setattr(r, "ci", [run("build-test", "failure")])
            engine.start()
            review = Review(source="github", validators=["qa"], diff="+x")
            engine.submit(review)
            end = time.time() + 5
            while review.status != "done" and time.time() < end:
                time.sleep(0.02)
        self.assertEqual((review.verdict, review.verdict_reason), ("AUTO-FIX", "CI failed"))


class AttachCiTests(unittest.TestCase):
    def test_reads_check_runs_for_the_commit(self):
        gh = mock.MagicMock()
        gh.__enter__.return_value = gh
        gh.get.return_value = mock.Mock(status_code=200, json=lambda: {"check_runs": [
            {"name": "build-test", "status": "completed", "conclusion": "success", "extra": 1}]})
        poller = GitHubPoller(mock.Mock(), Events(), mock.Mock())
        review = Review(source="github", validators=["qa"], meta={"repo": "o/r", "sha": "abc", "pr_key": "o/r#18"})
        with mock.patch.object(GitHubPoller, "_client", return_value=gh), mock.patch.object(config, "GITHUB_TOKEN", "t"):
            poller.attach_ci(review)
        self.assertEqual(gh.get.call_args.args[0], "/repos/o/r/commits/abc/check-runs")
        self.assertEqual(review.ci, [run("build-test", "success")])

    def test_no_pr_means_no_ci(self):
        review = Review(source="slack", validators=["qa"], diff="+x")
        GitHubPoller(mock.Mock(), Events(), mock.Mock()).attach_ci(review)
        self.assertIsNone(review.ci)


if __name__ == "__main__":
    unittest.main()
