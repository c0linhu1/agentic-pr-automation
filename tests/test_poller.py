"""The GitHub poller's background jobs: queuing new PR commits for review, and closing linked issues."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from tests.helpers import FakeClock, FakeGitHub, response, use_github
from vader import config, models
from vader.engine import Engine, Events, Review
from vader.github import RETRY_UNAVAILABLE_SECONDS, GitHubPoller, linked_issues
from vader.state import State


def closed_pr(number, body, closed_at, merged=True):
    return {"number": number, "body": body, "closed_at": closed_at, "updated_at": closed_at,
            "merged_at": closed_at if merged else None, "base": {"ref": "staging"}}


def issue(number, state="open", **extra):
    return {"number": number, "state": state, "title": f"Issue {number}",
            "html_url": f"https://github.com/o/r/issues/{number}", **extra}


class CloseLinkedIssuesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = State(Path(self.tmp.name) / "state.json")
        self.events = Events()
        self.poller = GitHubPoller(self.state, self.events, mock.Mock())
        use_github(self)
        for p in [mock.patch.object(config, "GITHUB_REPOS", ["o/r"]), mock.patch.object(config, "CLOSE_LINKED_ISSUES", True)]:
            self.enterContext(p)

    def sync(self, gh) -> int:
        self.gh = gh
        return self.poller.sync_closed_prs()

    def test_a_merged_pr_closes_its_issue_as_completed(self):
        gh = FakeGitHub([closed_pr(40, "Fixes #15", "2026-10-20T12:00:00Z")], [issue(15)])
        self.assertEqual(self.sync(gh), 1)
        self.assertEqual(gh.closes, [(15, {"state": "closed", "state_reason": "completed"})])
        self.assertEqual(gh.comments, [(15, "Closed by Vader: #40 was merged into `staging`.")])
        event, = [e for e in self.events.since(0) if e["type"] == "issue_closed"]
        self.assertEqual((event["issue"], event["pr"], event["merged"]), (15, 40, True))

    def test_a_pr_closed_without_merging_closes_its_issue_as_not_planned(self):
        gh = FakeGitHub([closed_pr(7, "Closes #1", "2026-10-19T10:00:00Z", merged=False)], [issue(1)])
        self.sync(gh)
        self.assertEqual(gh.closes, [(1, {"state": "closed", "state_reason": "not_planned"})])
        self.assertIn("closed without being merged", gh.comments[0][1])

    def test_a_pr_replaced_by_an_open_one_leaves_its_issues_to_it(self):
        # #7 and #8 were combined into the open #42, which still says "Closes #1, #2"; #9's issue #3 isn't in it.
        gh = FakeGitHub([closed_pr(7, "Closes #1", "2026-10-19T10:00:00Z", merged=False),
                         closed_pr(8, "Closes #2", "2026-10-19T11:00:00Z", merged=False),
                         closed_pr(9, "Closes #3", "2026-10-19T12:00:00Z", merged=False)], [issue(1), issue(2), issue(3)])
        gh.pr["body"] = "Closes #1\nCloses #2"
        self.assertEqual(self.sync(gh), 1)
        self.assertEqual([n for n, _ in gh.closes], [3])
        self.assertEqual(gh.open_reads, 1)  # listed once, not per PR
        gh.pr.update(state="closed", merged=True)  # when #42 merges, it closes them as completed
        gh.prs.append(dict(closed_pr(42, gh.pr["body"], "2026-10-21T09:00:00Z")))
        self.assertEqual(self.sync(gh), 2)
        self.assertEqual(gh.closes[1:], [(1, {"state": "closed", "state_reason": "completed"}),
                                         (2, {"state": "closed", "state_reason": "completed"})])

    def test_every_linked_issue_is_closed(self):
        gh = FakeGitHub([closed_pr(20, "Fixes #2 and resolves #3. Closes: #2", "2026-10-20T01:00:00Z")],
                        [issue(2), issue(3)])
        self.assertEqual(self.sync(gh), 2)
        self.assertEqual([n for n, _ in gh.closes], [2, 3])

    def test_already_closed_issues_pull_requests_and_missing_numbers_are_left_alone(self):
        gh = FakeGitHub([closed_pr(21, "Fixes #4, fixes #12, fixes #999", "2026-10-20T01:00:00Z")],
                        [issue(4, state="closed"), issue(12, pull_request={"url": "x"})])
        self.assertEqual(self.sync(gh), 0)
        self.assertEqual((gh.closes, gh.comments), ([], []))

    def test_catches_up_once_then_only_handles_newly_closed_prs(self):
        gh = FakeGitHub([closed_pr(7, "Closes #1", "2026-10-19T10:00:00Z", merged=False),
                         closed_pr(40, "Closes #15", "2026-10-20T12:00:00Z")], [issue(1), issue(15)])
        self.assertEqual(self.sync(gh), 2)
        gh.issues[15]["state"] = "open"  # a person reopens it: Vader must not close it again
        self.assertEqual(self.sync(gh), 0)
        gh.prs.append(closed_pr(19, "Closes #17", "2026-10-21T09:00:00Z"))
        gh.issues[17] = issue(17)
        self.assertEqual(self.sync(gh), 1)
        self.assertEqual([n for n, _ in gh.closes], [1, 15, 17])
        self.assertEqual(self.state.issues_synced_through("o/r"), "2026-10-21T09:00:00Z")

    def test_where_it_got_to_survives_a_restart(self):
        gh = FakeGitHub([closed_pr(40, "Closes #15", "2026-10-20T12:00:00Z")], [issue(15)])
        self.sync(gh)
        gh.issues[15]["state"] = "open"
        self.poller.state = State(Path(self.tmp.name) / "state.json")
        self.assertEqual(self.sync(gh), 0)

    def test_after_catching_up_one_page_is_read(self):
        prs = [closed_pr(n, "", f"2026-01-01T00:{n // 60:02}:{n % 60:02}Z") for n in range(1, 251)]
        gh = FakeGitHub(prs, [])
        self.sync(gh)
        self.assertEqual(gh.pages, [1, 2, 3])  # the catch-up reads every page
        gh.pages.clear()
        self.sync(gh)
        self.assertEqual(gh.pages, [1])

    def test_a_refused_close_is_retried_on_the_next_check(self):
        gh = FakeGitHub([closed_pr(40, "Closes #15", "2026-10-20T12:00:00Z")], [issue(15)])
        gh.close_reply = response(403, {"message": "Resource not accessible by personal access token"})
        with self.assertRaisesRegex(RuntimeError, "Issues: Read and write"):
            self.sync(gh)
        self.assertIsNone(self.state.issues_synced_through("o/r"))
        gh.close_reply = None  # the token gets the permission
        self.assertEqual(self.sync(gh), 1)

    def test_a_github_outage_keeps_the_prs_already_handled(self):
        gh = FakeGitHub([closed_pr(7, "Closes #1", "2026-10-19T10:00:00Z"),
                         closed_pr(40, "Closes #15", "2026-10-20T12:00:00Z")], [issue(1), issue(15)])
        patch = gh.patch
        gh.patch = lambda path, json, **kw: (response(502, {}) if path.endswith("/15") else patch(path, json))
        with self.assertRaises(RuntimeError):
            self.sync(gh)
        self.assertEqual(self.state.issues_synced_through("o/r"), "2026-10-19T10:00:00Z")
        gh.patch = patch
        self.assertEqual(self.sync(gh), 1)
        self.assertEqual([n for n, _ in gh.closes], [1, 15])

    def test_an_issue_github_wont_close_is_reported_and_skipped(self):
        gh = FakeGitHub([closed_pr(40, "Closes #15", "2026-10-20T12:00:00Z")], [issue(15)])
        gh.close_reply = response(422, {"message": "Validation Failed"})
        self.assertEqual(self.sync(gh), 0)
        self.assertEqual(self.state.issues_synced_through("o/r"), "2026-10-20T12:00:00Z")
        self.assertIn("issue_close_failed", [e["type"] for e in self.events.since(0)])


class BackgroundWorkTests(unittest.TestCase):
    """Each round of polling: which jobs run, and what stops them."""

    def setUp(self):
        self.state = mock.Mock(enabled=True, automation=False)
        self.events = Events()
        self.poller = GitHubPoller(self.state, self.events, mock.Mock())
        patches = [mock.patch.object(config, "GITHUB_TOKEN", "t"), mock.patch.object(config, "GITHUB_REPOS", ["o/r"]),
                   mock.patch.object(config, "CLOSE_LINKED_ISSUES", True),
                   mock.patch.object(GitHubPoller, "poll_once"), mock.patch.object(GitHubPoller, "sync_closed_prs")]
        for p in patches:
            self.enterContext(p)

    def test_issues_are_closed_even_with_automation_off(self):
        self.poller.tick()
        self.poller.sync_closed_prs.assert_called_once()
        self.poller.poll_once.assert_not_called()

    def test_stop_pauses_everything(self):
        self.state.enabled = False
        self.poller.tick()
        self.poller.sync_closed_prs.assert_not_called()

    def test_off_by_setting(self):
        with mock.patch.object(config, "CLOSE_LINKED_ISSUES", False):
            self.poller.tick()
        self.poller.sync_closed_prs.assert_not_called()

    def test_one_job_failing_doesnt_stop_the_other_and_is_reported_once(self):
        self.state.automation = True
        self.poller.poll_once.side_effect = RuntimeError("boom")
        with self.assertLogs("vader.github", "WARNING"):
            self.poller.tick()
            self.poller.tick()
        self.assertEqual(self.poller.sync_closed_prs.call_count, 2)
        errors = [e["message"] for e in self.events.since(0) if e["type"] == "poller_error"]
        self.assertEqual(errors, ["Automatic PR reviews failed: boom"])


class PrPollTests(unittest.TestCase):
    """Automatic reviews: each new PR commit is queued once, and only while the model server is up."""

    def setUp(self):
        self.enterContext(mock.patch.object(config, "GITHUB_REPOS", ["o/r"]))

    def test_a_pr_github_wont_serve_doesnt_stop_the_others(self):
        with tempfile.TemporaryDirectory() as d:
            state = State(Path(d) / "state.json", default_model="m:1b")
            events = Events()
            engine = Engine(state, events, complete=lambda *a, **k: '{"findings": []}',
                            prepare_model=lambda m: None, assignments=dict)
            poller = GitHubPoller(state, events, engine)
            prs = [{"number": n, "head": {"sha": f"s{n}"}, "draft": False} for n in (1, 2)]

            def review_for(gh, repo, pr, pr_key, sha):
                if pr["number"] == 1:
                    request = httpx.Request("GET", "https://api.github.com")
                    raise httpx.HTTPStatusError("406", request=request, response=httpx.Response(406, request=request))
                return mock.Mock(meta={"pr_key": pr_key, "sha": sha})

            client = mock.MagicMock()
            client.__enter__.return_value.get.return_value = mock.Mock(json=lambda: prs,
                                                                       raise_for_status=lambda: None)
            with mock.patch.object(models, "available", return_value=set()), \
                    mock.patch.object(poller, "_client", return_value=client), \
                    mock.patch.object(poller, "_review_for", side_effect=review_for), \
                    mock.patch.object(engine, "submit") as submit:
                self.assertEqual(poller.poll_once(), 1)
                self.assertEqual(poller.poll_once(), 1)  # #1 isn't retried until it gets a new push
            self.assertEqual(submit.call_count, 2)
            errors = [e for e in events.since(0) if e["type"] == "poller_error"]
            self.assertEqual(len(errors), 1)
            self.assertIn("o/r#1", errors[0]["message"])

    def test_every_page_of_open_prs_is_read(self):
        prs = [{"number": n} for n in range(1, 151)]

        def get(path, params):
            start = (params["page"] - 1) * params["per_page"]
            return response(200, prs[start:start + params["per_page"]])

        self.assertEqual(GitHubPoller._open_prs(mock.Mock(get=get), "o/r"), prs)

    def test_nothing_is_queued_while_the_model_server_is_down(self):
        poller = GitHubPoller(mock.Mock(), Events(), mock.Mock())
        with mock.patch.object(models, "available", return_value=None), \
                mock.patch.object(poller, "_client") as client:
            with self.assertRaises(models.ModelServerDown):
                poller.poll_once()
        client.assert_not_called()  # not even GitHub is asked

    def test_a_review_that_couldnt_reach_the_server_is_tried_again_after_a_pause(self):
        state, engine = mock.Mock(enabled=True), mock.Mock()
        state.reviewed_sha.return_value, engine.is_pending.return_value = None, False
        poller = GitHubPoller(state, Events(), engine)
        gh = mock.MagicMock()
        gh.__enter__.return_value = gh
        gh.get.return_value = mock.Mock(json=lambda: [{"number": 7, "head": {"sha": "s7"}, "draft": False}],
                                        raise_for_status=lambda: None)
        review = Review(source="github", validators=["qa"], verdict="HOLD", unavailable=True,
                        meta={"pr_key": "o/r#7", "sha": "s7", "repo": "o/r", "number": 7})
        clock = FakeClock()
        with mock.patch.object(models, "available", return_value=set()), \
                mock.patch.object(config, "GITHUB_TOKEN", "t"), mock.patch.object(config, "GITHUB_COMMENT", True), \
                mock.patch.object(poller, "_client", return_value=gh), mock.patch("vader.github.time", clock), \
                mock.patch.object(poller, "_review_for", return_value=mock.Mock()):
            poller.finish(review)
            gh.post.assert_not_called()  # no PR comment for a review that saw nothing
            self.assertEqual(poller.poll_once(), 0)  # still pausing
            clock.now += RETRY_UNAVAILABLE_SECONDS + 1
            self.assertEqual(poller.poll_once(), 1)


class LinkedIssueTests(unittest.TestCase):
    def test_keywords(self):
        self.assertEqual(linked_issues("Closes #17. Also fixes #3, resolved #3, Fix: #9"), [17, 3, 9])
        self.assertEqual(linked_issues("Related to #4; see #5"), [])
        self.assertEqual(linked_issues(None), [])


if __name__ == "__main__":
    unittest.main()
