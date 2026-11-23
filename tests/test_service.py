import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from tests.helpers import wait_for
from vader import config
from vader.engine import Engine, Events, Review
from vader.github import parse_pr_ref
from vader.service import create_app
from vader.state import State

KEY = {"X-Vader-Key": "test-key"}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name) / "state.json", default_model="fake:1b")
        self.events = Events()
        self.engine = Engine(self.state, self.events, complete=lambda *a, **k: '{"findings": []}',
                             prepare_model=lambda m: None, assignments=dict)
        app = create_app(self.state, self.events, self.engine, poller=None, api_key="test-key")
        self.client = TestClient(app).__enter__()
        for patcher in [mock.patch("vader.service.models.loaded", return_value=[]),
                        mock.patch("vader.service.models.available", return_value={"fake:1b"})]:
            self.enterContext(patcher)

    def tearDown(self):
        self.client.__exit__(None, None, None)
        # A review still finishing would save state.json while the folder is being deleted.
        wait_for(lambda: not (self.engine.busy or self.engine.status()["queue"]))
        self.tmp.cleanup()

    def test_requests_without_the_key_are_refused(self):
        self.assertEqual(self.client.get("/status").status_code, 401)
        self.assertEqual(self.client.get("/status", headers={"X-Vader-Key": "wrong"}).status_code, 401)

    def test_status(self):
        s = self.client.get("/status", headers=KEY).json()
        self.assertTrue(s["enabled"])
        self.assertEqual(s["model"], "fake:1b")

    def test_stop_then_submit_is_refused(self):
        self.client.post("/control", json={"action": "stop"}, headers=KEY)
        r = self.client.post("/reviews", json={"text": "+x"}, headers=KEY)
        self.assertEqual(r.status_code, 409)

    def test_submit_runs_and_events_report_it(self):
        r = self.client.post("/reviews", json={"text": "TITLE: t\nDIFF:\n+x", "validators": ["logic"]}, headers=KEY)
        self.assertEqual(r.status_code, 200)
        review_id = r.json()["id"]
        status = lambda: self.client.get(f"/reviews/{review_id}", headers=KEY).json()["status"]  # noqa: E731
        self.assertTrue(wait_for(lambda: status() == "done"))
        review = self.client.get(f"/reviews/{review_id}", headers=KEY).json()
        self.assertEqual(review["validators"], ["correctness"])
        self.assertEqual(review["verdict"], "PASS")
        kinds = [e["type"] for e in self.client.get("/events", headers=KEY).json()["events"]]
        self.assertIn("review_done", kinds)

    def test_unknown_validator_and_empty_diff_are_rejected(self):
        self.assertEqual(self.client.post("/reviews", json={"text": "+x", "validators": ["nope"]},
                                          headers=KEY).status_code, 400)
        self.assertEqual(self.client.post("/reviews", json={"text": "TITLE: only a title"},
                                          headers=KEY).status_code, 400)

    def test_pr_review_fetches_through_vader(self):
        fake_poller = mock.Mock()
        fake_poller.review_for_pr.return_value = Review(
            source="github", validators=[], title="Add order search", diff="+x",
            meta={"repo": "o/r", "number": 8, "url": "https://github.com/o/r/pull/8", "author": "me",
                  "pr_key": "o/r#8", "sha": "abc"})
        app = create_app(self.state, self.events, self.engine, poller=fake_poller, api_key="test-key",
                         start_threads=False)
        with mock.patch.object(config, "GITHUB_TOKEN", "t"), mock.patch.object(config, "GITHUB_REPOS", ["o/r"]), \
             TestClient(app) as client:
            r = client.post("/reviews/pr", json={"ref": "#8", "validators": ["qa"]}, headers=KEY)
        self.assertEqual(r.status_code, 200, r.text)
        fake_poller.review_for_pr.assert_called_once_with("o/r", 8)
        review = self.engine.get(r.json()["id"])
        self.assertEqual(review.validators, ["qa"])
        self.assertEqual(r.json()["title"], "Add order search")

    def test_pr_review_needs_github(self):
        with mock.patch.object(config, "GITHUB_TOKEN", ""):
            r = self.client.post("/reviews/pr", json={"ref": "8"}, headers=KEY)
        self.assertEqual(r.status_code, 400)

    def test_model_switch_rules(self):
        with mock.patch("vader.service.models.check_selectable", return_value="`x:1b` isn't in the allowed list"):
            r = self.client.post("/model", json={"name": "x:1b"}, headers=KEY)
        self.assertEqual(r.status_code, 400)
        with mock.patch("vader.service.models.check_selectable", return_value=None), \
             mock.patch("vader.service.models.ensure_only"):
            r = self.client.post("/model", json={"name": "gpt-oss:20b"}, headers=KEY)
            self.assertEqual(r.json()["model"], "gpt-oss:20b")
            self.assertEqual(self.state.model, "gpt-oss:20b")
            self.engine._running = Review(source="slack", validators=["qa"])  # a review in progress
            r = self.client.post("/model", json={"name": "fake:1b"}, headers=KEY)
            self.engine._running = None
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.state.model, "gpt-oss:20b")

    def test_the_slack_bots_other_endpoints(self):
        r = self.client.post("/automation", json={"enabled": True}, headers=KEY).json()
        self.assertEqual((r, self.state.automation), ({"automation": True, "github_configured": False}, True))
        self.assertEqual(self.client.post("/control", json={"action": "pause"}, headers=KEY).status_code, 400)
        with mock.patch("vader.service.models.catalog", return_value=("fake:1b", [])), \
             mock.patch("vader.service.models.assignments", return_value={"qa": "other:1b"}):
            m = self.client.get("/models", headers=KEY).json()
        self.assertEqual((m["active"], m["validators"]["qa"], m["validators"]["security"]),
                         ("fake:1b", "other:1b", "fake:1b"))
        # Where an automatic review's results go, once the bot has posted its Slack thread.
        review = Review(source="github", validators=["qa"])
        self.engine._reviews[review.id] = review
        thread = {"channel": "C1", "thread_ts": "1.2"}
        self.client.post(f"/reviews/{review.id}/reply-to", json={"reply_to": thread}, headers=KEY)
        self.assertEqual(review.reply_to, thread)
        self.assertEqual(self.client.post("/reviews/nope/reply-to", json={"reply_to": thread},
                                          headers=KEY).status_code, 404)


class PrRefTests(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(parse_pr_ref("11", "o/r"), ("o/r", 11))
        self.assertEqual(parse_pr_ref("#11", "o/r"), ("o/r", 11))
        self.assertEqual(parse_pr_ref("x/y#4", "o/r"), ("x/y", 4))
        self.assertEqual(parse_pr_ref("<https://github.com/owner/repo/pull/9>", None),
                         ("owner/repo", 9))

    def test_bad_refs(self):
        with self.assertRaises(ValueError):
            parse_pr_ref("eleven", "o/r")
        with self.assertRaises(ValueError):
            parse_pr_ref("11", None)


if __name__ == "__main__":
    unittest.main()
