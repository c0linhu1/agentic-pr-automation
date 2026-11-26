import unittest
from unittest import mock

from tests.helpers import EngineCase, use_github
from vader.engine import Events, Review
from vader.github import GitHubPoller


def run(name, conclusion, status="completed"):
    return {"name": name, "status": status, "conclusion": conclusion}


class CiStatusTests(unittest.TestCase):
    def test_status(self):
        r = Review(source="github", validators=["qa"])
        self.assertIsNone(r.ci_status)                                   # unknown
        r.ci = [run("build-test", "success"), run("lint", "skipped")]
        self.assertEqual(r.ci_status, "passed")
        r.ci = [run("build-test", "failure"), run("lint", "success")]
        self.assertEqual(r.ci_status, "failed")
        r.ci = [run("build-test", None, status="in_progress")]
        self.assertIsNone(r.ci_status)                                   # still running

    def test_ci_line_goes_in_the_shared_header(self):
        r = Review(source="github", validators=["qa"], title="t")
        self.assertNotIn("CI:", r.header())                               # pasted diffs: no CI line
        r.ci = [run("build-test", "success")]
        self.assertIn("CI: build-test success. Every check passed", r.header())
        self.assertIn("doesn't prove the tests themselves are right", r.header())


class CiInVerdictTests(EngineCase):
    def test_failed_ci_with_no_findings_is_minor(self):
        engine = self.engine()
        engine.before_review = lambda r: setattr(r, "ci", [run("build-test", "failure")])
        review = self.finish(Review(source="github", validators=["qa"], diff="+x"), engine)
        self.assertEqual((review.verdict, review.verdict_reason), ("MINOR", "CI failed"))


class AttachCiTests(unittest.TestCase):
    def test_reads_check_runs_for_the_commit(self):
        use_github(self).checks = [{"name": "build-test", "status": "completed", "conclusion": "success", "extra": 1}]
        review = Review(source="github", validators=["qa"], meta={"repo": "o/r", "sha": "abc", "pr_key": "o/r#42"})
        GitHubPoller(mock.Mock(), Events(), mock.Mock()).attach_ci(review)
        self.assertEqual(review.ci, [run("build-test", "success")])

    def test_a_failure_past_the_first_page_is_still_seen(self):
        # A big matrix build: the one failing check is number 150, on GitHub's second page.
        use_github(self).checks = [run(f"job {i}", "success") for i in range(149)] + [run("job 149", "failure")]
        review = Review(source="github", validators=["qa"], meta={"repo": "o/r", "sha": "abc", "pr_key": "o/r#42"})
        GitHubPoller(mock.Mock(), Events(), mock.Mock()).attach_ci(review)
        self.assertEqual((len(review.ci), review.ci_status), (150, "failed"))
        with mock.patch("vader.github.CHECK_RUN_PAGES", 1), self.assertLogs("vader.github", "WARNING"):
            GitHubPoller(mock.Mock(), Events(), mock.Mock()).attach_ci(review)
        self.assertIsNone(review.ci)  # couldn't read them all: unknown, never "passed"

    def test_no_pr_means_no_ci(self):
        review = Review(source="slack", validators=["qa"], diff="+x")
        GitHubPoller(mock.Mock(), Events(), mock.Mock()).attach_ci(review)
        self.assertIsNone(review.ci)


if __name__ == "__main__":
    unittest.main()
