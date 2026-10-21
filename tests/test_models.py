import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from vader import config, models
from vader.engine import Engine, Events
from vader.github import GitHubPoller
from vader.state import State


def ok_load(*a, **k):
    return httpx.Response(200, request=httpx.Request("POST", "http://ollama"))


class ContextCheckTests(unittest.TestCase):
    """Ollama loading a model with less context than Vader budgets for would cut prompts off silently."""

    def load(self, context):
        loaded = [{"name": "m:1b", "size_gb": 1, "vram_gb": 1, "context": context}]
        with mock.patch.object(models, "loaded", return_value=loaded), \
             mock.patch.object(models.httpx, "post", side_effect=ok_load), \
             mock.patch.object(config, "CONTEXT_TOKENS", 32768):
            models.ensure_only("m:1b")

    def test_smaller_context_than_configured_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "OLLAMA_CONTEXT_LENGTH=32768"):
            self.load(4096)

    def test_matching_or_unknown_context_is_fine(self):
        self.load(32768)
        self.load(None)  # older Ollama versions don't report it


class PollerTests(unittest.TestCase):
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
            with mock.patch.object(config, "GITHUB_REPOS", ["o/r"]), \
                 mock.patch.object(poller, "_client", return_value=client), \
                 mock.patch.object(poller, "_review_for", side_effect=review_for), \
                 mock.patch.object(engine, "submit") as submit:
                self.assertEqual(poller.poll_once(), 1)
                self.assertEqual(poller.poll_once(), 1)  # #1 isn't retried until it gets a new push
            self.assertEqual(submit.call_count, 2)
            errors = [e for e in events.since(0) if e["type"] == "poller_error"]
            self.assertEqual(len(errors), 1)
            self.assertIn("o/r#1", errors[0]["message"])
