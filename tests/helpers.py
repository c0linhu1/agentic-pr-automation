"""Shared fakes for the tests: a model, a clock, an engine harness, and GitHub."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import unquote
from unittest import mock

from vader import config
from vader.agents import VALIDATORS
from vader.engine import Engine, Events, Review
from vader.github import GitHubPoller
from vader.state import State


def wait_for(cond, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


class FakeClock:
    """Stands in for the engine's `time` module (patch vader.engine.time), so time limits and estimates
    are exact and take no real time."""

    strftime = staticmethod(time.strftime)  # before time() below, which hides the time module in this class

    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now


class FakeModel:
    """Stands in for the model server. Records calls; a test picks each validator's reply, and can make
    calls take (fake) time or wait until it releases them."""

    def __init__(self, replies=None, delay=0.0, clock=None, seconds=0.0, gate=None):
        self.replies, self.delay, self.clock, self.seconds, self.gate = replies or {}, delay, clock, seconds, gate
        self.calls, self.models, self.active, self.max_active = [], [], 0, 0
        self._lock = threading.Lock()
        self._keys = {v.instructions(): k for k, v in VALIDATORS.items()}

    def complete(self, model, instructions, content, **kw):
        with self._lock:
            self.models.append(model)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        if self.gate:
            self.gate.wait(5)
        time.sleep(self.delay)
        if self.clock:
            self.clock.now += self.seconds
        key = self._keys[instructions]
        with self._lock:
            self.calls.append((key, content))
            self.active -= 1
        reply = self.replies.get(key, '{"findings": []}')
        if isinstance(reply, Exception):
            raise reply
        return reply


class EngineCase(unittest.TestCase):
    """A temp state file and events. finish() runs a review the way the worker does, minus the thread."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = State(Path(self.tmp.name) / "state.json", default_model="fake:1b")
        self.events = Events()
        self.loaded = []  # models prepared, in order

    def engine(self, complete=None, assignments=None, prepare=None, start=False) -> Engine:
        engine = Engine(self.state, self.events, complete=complete or FakeModel().complete,
                        prepare_model=prepare or self.loaded.append, assignments=lambda: assignments or {})
        if start:
            engine.start()
        return engine

    def finish(self, review: Review, engine: Engine | None = None, **engine_args) -> Review:
        (engine or self.engine(**engine_args))._process(review)
        return review


def response(status_code=200, body=None):
    return mock.Mock(status_code=status_code, json=lambda: {} if body is None else body, text=str(body),
                     raise_for_status=(mock.Mock(side_effect=RuntimeError(f"HTTP {status_code}"))
                                       if status_code >= 400 else lambda: None))


def use_github(case: unittest.TestCase, gh: "FakeGitHub | None" = None) -> "FakeGitHub":
    """Point Vader's GitHub client at a fake, with a token set, for the rest of the test. It uses whatever
    `case.gh` is at the time, so a test can swap in a fresh fake."""
    case.gh = gh or FakeGitHub()
    case.enterContext(mock.patch.object(GitHubPoller, "_client", side_effect=lambda: case.gh))
    case.enterContext(mock.patch.object(config, "GITHUB_TOKEN", "t"))
    return case.gh


SHA = "abcdef1234567"
PASSED = [{"name": "build-test", "status": "completed", "conclusion": "success"}]


class FakeGitHub:
    """One repo, o/r: an open PR #18 into staging, closed PRs, issues, and CI checks. Records what Vader
    writes; a test changes the data or the replies to closing and merging."""

    def __init__(self, closed_prs=(), issues=()):
        self.pr = {"number": 18, "title": "Add order search", "state": "open", "merged": False, "draft": False,
                   "mergeable": True, "base": {"ref": "staging", "sha": "base000"}, "head": {"ref": "add-order-search", "sha": SHA}}
        self.prs, self.issues = list(closed_prs), {i["number"]: i for i in issues}
        self.checks = PASSED
        self.files, self.base_files = {}, {}  # path -> contents at the PR's commit, and at its base
        self.behind_by = 0  # commits on the base branch the PR doesn't have; None: GitHub can't say
        self.pr_files = []  # the PR's file list, as GitHub returns it; None: listing it fails
        self.merge_reply = response(200, {"merged": True, "sha": "9876543210"})
        self.close_reply = None  # a response here makes closing an issue fail
        self.comment_reply = None  # likewise for posting a comment
        self.merges, self.closes, self.comments, self.pages = [], [], [], []
        self.open_reads = 0  # times the open PRs were listed

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, path, params=None, **kw):
        kind, _, rest = path.removeprefix("/repos/o/r/").partition("/")
        if path == "/repos/o/r/pulls" and params["state"] == "open":
            self.open_reads += 1
            return response(200, [dict(self.pr)] if self.pr["state"] == "open" else [])
        if path == "/repos/o/r/pulls":
            assert (params["state"], params["sort"], params["direction"]) == ("closed", "updated", "desc"), params
            self.pages.append(params["page"])
            newest_first = sorted(self.prs, key=lambda p: p["updated_at"], reverse=True)
            size = params["per_page"]
            return response(200, newest_first[(params["page"] - 1) * size:params["page"] * size])
        if kind == "pulls" and rest == f"{self.pr['number']}/files":
            if self.pr_files is None:
                return response(500, {"message": "Server Error"})
            page, size = params.get("page", 1), params["per_page"]
            return response(200, self.pr_files[(page - 1) * size:page * size])
        if kind == "pulls" and int(rest) == self.pr["number"]:
            return response(200, dict(self.pr))
        if kind == "commits":
            size, page = params["per_page"], params.get("page", 1)
            return response(200, {"total_count": len(self.checks),
                                  "check_runs": self.checks[(page - 1) * size:page * size]})
        if kind == "compare":
            return response(200, {"behind_by": self.behind_by}) if self.behind_by is not None else response(404)
        files = self.base_files if (params or {}).get("ref") == self.pr["base"].get("sha") else self.files
        if kind == "contents" and unquote(rest) in files:
            return response(200, files[unquote(rest)])
        if kind == "issues" and int(rest) in self.issues:
            return response(200, self.issues[int(rest)])
        return response(404, {"message": "Not Found"})

    def put(self, path, json, **kw):
        self.merges.append((path, json))
        return self.merge_reply

    def patch(self, path, json, **kw):
        if self.close_reply:
            return self.close_reply
        number = int(path.rsplit("/", 1)[1])
        self.closes.append((number, json))
        self.issues[number]["state"] = "closed"
        return response(200, self.issues[number])

    def post(self, path, json, **kw):
        if self.comment_reply:
            return self.comment_reply
        self.comments.append((int(path.split("/")[-2]), json["body"]))
        return response(201, {"html_url": f"https://github.com{path}#c1"})
