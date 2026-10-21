import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from vader import config, llm
from vader.agents import VALIDATORS
from vader.engine import Engine, Events, Review, Stopped, merge_findings, parse_sections
from vader.state import State


def wait_for(cond, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class FakeModel:
    """Stands in for Ollama. Records calls and lets a test pick each validator's reply."""

    def __init__(self, replies=None, delay=0.0):
        self.replies, self.delay = replies or {}, delay
        self.calls, self.models, self.active, self.max_active = [], [], 0, 0
        self._lock = threading.Lock()

    def complete(self, model, system, user, json_output=False):
        with self._lock:
            self.models.append(model)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(self.delay)
        key = next(k for k, v in VALIDATORS.items() if v.instructions() == system)
        with self._lock:
            self.calls.append((key, user))
            self.active -= 1
        reply = self.replies.get(key, '{"findings": []}')
        if isinstance(reply, Exception):
            raise reply
        return reply


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name) / "state.json", default_model="fake:1b")
        self.events = Events()
        self.loaded = []

    def tearDown(self):
        self.tmp.cleanup()

    def engine(self, fake, assignments=None):
        e = Engine(self.state, self.events, complete=fake.complete, prepare_model=self.loaded.append,
                   assignments=lambda: assignments or {})
        e.start()
        return e

    def test_validators_run_on_their_own_models_grouped_to_swap_least(self):
        fake = FakeModel()
        engine = self.engine(fake, {"security": "a:1b", "performance": "a:1b", "qa": "b:1b"})
        review = Review(source="slack", validators=["security", "qa", "correctness", "performance"], diff="+x")
        engine.submit(review)
        self.assertTrue(wait_for(lambda: review.status == "done"))
        self.assertEqual(self.loaded, ["a:1b", "b:1b", "fake:1b"])
        self.assertEqual([(r["validator"], r["model"]) for r in review.results],
                         [("security", "a:1b"), ("performance", "a:1b"), ("qa", "b:1b"), ("correctness", "fake:1b")])
        self.assertEqual(fake.models, ["a:1b", "a:1b", "b:1b", "fake:1b"])

    def test_next_review_starts_with_the_model_already_loaded(self):
        engine = self.engine(FakeModel(), {"qa": "b:1b"})
        first = Review(source="slack", validators=["security", "qa"], diff="+x")
        second = Review(source="slack", validators=["security", "qa"], diff="+y")
        engine.submit(first)
        engine.submit(second)
        self.assertTrue(wait_for(lambda: second.status == "done"))
        # the first review ends on b:1b, so the second starts there instead of swapping back and forth
        self.assertEqual(self.loaded, ["fake:1b", "b:1b", "b:1b", "fake:1b"])
        self.assertEqual([r["validator"] for r in second.results], ["qa", "security"])

    def test_finished_review_releases_its_diff(self):
        engine = self.engine(FakeModel())
        review = Review(source="slack", validators=["qa"], diff="+x" * 1000)
        engine.submit(review)
        self.assertTrue(wait_for(lambda: review.status == "done"))
        self.assertEqual((review.diff, review._parts), ("", []))
        self.assertEqual(review.files, ["(diff)"])  # what was reviewed is still on record

    def test_old_finished_reviews_are_forgotten(self):
        engine = Engine(self.state, self.events, complete=FakeModel().complete, prepare_model=lambda m: None,
                        assignments=dict)
        with mock.patch("vader.engine.KEEP_REVIEWS", 3):
            done = [Review(source="slack", validators=["qa"], status="done") for _ in range(4)]
            for r in done:
                engine._reviews[r.id] = r
            engine.submit(Review(source="slack", validators=["qa"], diff="+x"))  # queued, never dropped
        self.assertEqual(len(engine._reviews), 3)
        self.assertNotIn(done[0].id, engine._reviews)
        self.assertNotIn(done[1].id, engine._reviews)

    def test_estimate_is_updated_after_every_call(self):
        engine = self.engine(FakeModel(delay=0.02))
        review = Review(source="slack", validators=["security", "qa", "performance"], diff="+x")
        engine.submit(review)
        self.assertTrue(wait_for(lambda: review.status == "done"))
        self.assertEqual(review._calls_done, 3)
        self.assertLessEqual(abs(review.estimate_s - (review.finished - review.started)), 1)

    def test_a_model_that_fails_to_load_fails_only_its_validators(self):
        def prepare(model):
            if model == "b:1b":
                raise RuntimeError("isn't pulled")
        engine = Engine(self.state, self.events, complete=FakeModel().complete, prepare_model=prepare,
                        assignments=lambda: {"qa": "b:1b"})
        engine.start()
        review = Review(source="slack", validators=["security", "qa"], diff="+x")
        engine.submit(review)
        self.assertTrue(wait_for(lambda: review.status == "done"))
        self.assertEqual([r["status"] for r in review.results], ["ok", "failed"])
        self.assertIn("b:1b", review.results[1]["error"])
        self.assertEqual(review.verdict, "HOLD")

    def test_every_validator_finishes_even_after_a_critical(self):
        critical = json.dumps({"findings": [{"severity": "critical", "description": "bad"}]})
        fake = FakeModel({"security": critical})
        engine = self.engine(fake)
        review = Review(source="slack", validators=list(VALIDATORS), diff="+x")
        engine.submit(review)
        self.assertTrue(wait_for(lambda: review.status == "done"))
        self.assertEqual([r["validator"] for r in review.results], list(VALIDATORS))
        self.assertEqual(review.verdict, "BLOCK")

    def test_reviews_run_one_at_a_time_in_order(self):
        fake = FakeModel(delay=0.02)
        engine = self.engine(fake)
        reviews = [Review(source="slack", validators=["security", "qa"], diff=f"+{i}") for i in range(3)]
        positions = [engine.submit(r) for r in reviews]
        self.assertEqual(positions[1:], [2, 3])
        self.assertTrue(wait_for(lambda: all(r.status == "done" for r in reviews)))
        self.assertEqual(fake.max_active, 1)
        self.assertEqual([u.split("+")[-1] for _, u in fake.calls], ["0", "0", "1", "1", "2", "2"])

    def test_failed_validator_is_recorded_and_others_continue(self):
        fake = FakeModel({"qa": llm.LlmError("timed out"), "security": "not json"})
        engine = self.engine(fake)
        review = Review(source="slack", validators=["security", "qa", "performance"], diff="+x")
        engine.submit(review)
        self.assertTrue(wait_for(lambda: review.status == "done"))
        self.assertEqual([r["status"] for r in review.results], ["failed", "failed", "ok"])
        self.assertEqual(review.verdict, "HOLD")

    def test_stop_refuses_new_work_and_queue_waits_until_start(self):
        engine = self.engine(FakeModel())
        self.state.set(enabled=False)
        with self.assertRaises(Stopped):
            engine.submit(Review(source="slack", validators=["qa"], diff="+x"))
        self.state.set(enabled=True)
        review = Review(source="slack", validators=["qa"], diff="+x")
        engine.submit(review)
        self.assertTrue(wait_for(lambda: review.status == "done"))

    def test_running_review_finishes_after_stop(self):
        fake = FakeModel(delay=0.05)
        engine = self.engine(fake)
        review = Review(source="slack", validators=list(VALIDATORS), diff="+x")
        waiting = Review(source="slack", validators=["qa"], diff="+y")
        engine.submit(review)
        engine.submit(waiting)
        self.assertTrue(wait_for(lambda: review.status == "running"))
        self.state.set(enabled=False)
        self.assertTrue(wait_for(lambda: review.status == "done"))
        self.assertEqual(len(review.results), 6)
        time.sleep(0.2)
        self.assertEqual(waiting.status, "queued")
        self.state.set(enabled=True)
        engine.wake()
        self.assertTrue(wait_for(lambda: waiting.status == "done"))

    def test_model_is_prepared_before_each_review(self):
        engine = self.engine(FakeModel())
        review = Review(source="slack", validators=["qa"], diff="+x")
        engine.submit(review)
        self.assertTrue(wait_for(lambda: review.status == "done"))
        self.assertEqual(self.loaded, ["fake:1b"])

    def test_events_follow_the_review(self):
        engine = self.engine(FakeModel())
        review = Review(source="slack", validators=["qa", "security"], diff="+x")
        engine.submit(review)
        self.assertTrue(wait_for(lambda: review.status == "done"))
        kinds = [e["type"] for e in self.events.since(0)]
        self.assertEqual(kinds, ["review_queued", "review_started", "validator_done", "validator_done", "review_done"])

    def test_a_one_validator_pr_review_doesnt_count_as_reviewing_the_commit(self):
        engine = self.engine(FakeModel())
        meta = {"pr_key": "o/r#12", "sha": "abc"}
        one = Review(source="slack", validators=["security"], diff="+x", meta=dict(meta))
        engine.submit(one)
        self.assertTrue(wait_for(lambda: one.status == "done"))
        self.assertIsNone(self.state.reviewed_sha("o/r#12"))  # automation still gives it a full review
        full = Review(source="github", validators=list(VALIDATORS), diff="+x", meta=dict(meta))
        engine.submit(full)
        self.assertTrue(wait_for(lambda: full.status == "done"))
        self.assertEqual(self.state.reviewed_sha("o/r#12"), "abc")

    def test_a_crashed_pr_review_is_recorded_so_the_poller_doesnt_requeue_it(self):
        engine = self.engine(FakeModel())
        review = Review(source="github", validators=list(VALIDATORS), diff="+x", meta={"pr_key": "o/r#3", "sha": "s3"})
        with mock.patch.object(engine, "_plan", side_effect=ValueError("broken SKILL.md")):
            engine.submit(review)
            self.assertTrue(wait_for(lambda: review.status == "done"))
        self.assertEqual(review.verdict, "HOLD")
        self.assertEqual(self.state.reviewed_sha("o/r#3"), "s3")
        self.assertFalse(engine.is_pending("o/r#3", "s3"))


def big_diff(files=6, lines=60):
    out = ""
    for i in range(files):
        out += f"diff --git a/src/F{i}.cs b/src/F{i}.cs\n--- a/src/F{i}.cs\n+++ b/src/F{i}.cs\n@@ -1,1 +1,{lines} @@\n"
        out += "".join(f"+    var value{j} = Compute{j}(input);\n" for j in range(lines))
    return out


class LargePrTests(unittest.TestCase):
    """Small context so a modest diff has to be split into parts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name) / "state.json", default_model="fake:1b")
        self.events = Events()
        patches = [mock.patch.object(config, "CONTEXT_TOKENS", 2000), mock.patch.object(config, "OUTPUT_TOKENS", 200),
                   mock.patch.object(config, "REVIEW_TIME_LIMIT_MINUTES", 0)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def run_review(self, fake, diff, validators=("qa",)):
        engine = Engine(self.state, self.events, complete=fake.complete, prepare_model=lambda m: None)
        engine.start()
        review = Review(source="github", validators=list(validators), title="Big change", diff=diff)
        engine.submit(review)
        self.assertTrue(wait_for(lambda: review.status == "done", timeout=10))
        return review

    def test_large_pr_is_split_and_every_part_reviewed(self):
        fake = FakeModel()
        review = self.run_review(fake, big_diff())
        self.assertGreater(review.parts, 1)
        self.assertEqual(len(fake.calls), review.parts)
        self.assertIn(f"part 1 of {review.parts}", fake.calls[0][1])
        self.assertIn("FILES IN THIS PR (6 reviewed)", fake.calls[0][1])
        self.assertEqual(review.results[0]["status"], "ok")
        self.assertEqual(review.results[0]["parts_done"], review.parts)

    def test_large_pr_goes_part_by_part_so_prompts_can_be_reused(self):
        fake = FakeModel()
        review = self.run_review(fake, big_diff(), validators=("security", "qa"))
        self.assertGreater(review.parts, 1)
        order = [(key, user.split("This is part ")[1].split(" ")[0]) for key, user in fake.calls]
        expected = [(k, str(i)) for i in range(1, review.parts + 1) for k in ("security", "qa")]
        self.assertEqual(order, expected)
        self.assertEqual([r["validator"] for r in review.results], ["security", "qa"])

    def test_duplicate_findings_across_parts_are_merged(self):
        dup = json.dumps({"findings": [
            {"severity": "low", "category": "flaky", "file": "src/F0.cs", "line": 3, "description": "a"},
            {"severity": "high", "category": "flaky", "file": "src/F0.cs", "line": 3, "description": "a, worse"}]})
        review = self.run_review(FakeModel({"qa": dup}), big_diff())
        self.assertEqual(len(review.results[0]["findings"]), 1)
        self.assertEqual(review.results[0]["findings"][0]["severity"], "high")

    def test_a_failed_part_makes_the_validator_partial_but_keeps_findings(self):
        calls = {"n": 0}
        crit = json.dumps({"findings": [{"severity": "critical", "category": "x", "file": "a", "line": 1,
                                         "description": "bad"}]})

        class Flaky(FakeModel):
            def complete(self, *a, **k):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise llm.LlmError("timed out")
                return crit

        review = self.run_review(Flaky(), big_diff())
        self.assertEqual(review.results[0]["status"], "partial")
        self.assertIn("part 2", review.results[0]["error"])
        self.assertEqual(review.verdict, "BLOCK")

    def test_time_limit_skips_remaining_work_and_holds(self):
        with mock.patch.object(config, "REVIEW_TIME_LIMIT_MINUTES", 0.001):  # 0.06 s
            review = self.run_review(FakeModel(delay=0.05), big_diff(), validators=("security", "qa", "performance"))
        self.assertTrue(review.time_limited)
        self.assertEqual(review.verdict, "HOLD")
        self.assertIn("time limit", review.verdict_reason)
        self.assertEqual(len(review.results), 3)  # every validator still reports, even if skipped
        self.assertTrue(any(r["status"] != "ok" for r in review.results))

    def test_estimate_event_after_first_call(self):
        review = self.run_review(FakeModel(), big_diff())
        estimates = [e for e in self.events.since(0) if e["type"] == "review_estimate"]
        self.assertEqual(len(estimates), 1)
        self.assertEqual(estimates[0]["calls"], review.parts)

    def test_all_generated_pr_has_nothing_to_review(self):
        diff = ("diff --git a/package-lock.json b/package-lock.json\n--- a/package-lock.json\n"
                "+++ b/package-lock.json\n@@ -1 +1 @@\n+{}\n")
        review = self.run_review(FakeModel(), diff)
        self.assertEqual(review.results[0]["status"], "failed")
        self.assertIn("nothing to review", review.results[0]["error"])
        self.assertEqual(review.skipped_files, [{"path": "package-lock.json", "reason": "lockfile"}])

    def test_every_part_fits_the_context_with_the_file_list_included(self):
        # Real budgets and many long paths: the file list a split PR's header adds must still fit.
        sent = []

        def complete(model, instructions, content, **kw):
            sent.append(llm._estimated_tokens(llm.SYSTEM) + llm._estimated_tokens(instructions)
                        + llm._estimated_tokens(content))
            return '{"findings": []}'

        diff = ""
        for i in range(150):
            p = f"src/OptionsDesk.Web/Features/Pricing/Strategies/Handlers/StrategyHandler{i:03}.cs"
            diff += (f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1,1 +1,8 @@\n"
                     + "".join(f"+    var v{j} = Compute{j}(input);\n" for j in range(8)))
        fake = FakeModel()
        fake.complete = complete
        with mock.patch.object(config, "CONTEXT_TOKENS", 32768), mock.patch.object(config, "OUTPUT_TOKENS", 12288):
            review = self.run_review(fake, diff, validators=list(VALIDATORS))
        self.assertGreater(review.parts, 1)
        self.assertLessEqual(max(sent), 32768 - 12288)


class SectionTests(unittest.TestCase):
    def test_labeled_sections(self):
        s = parse_sections("TITLE: Fix it\nDESCRIPTION:\nAdds tests\nDIFF:\n+a\n-b")
        self.assertEqual(s, {"title": "Fix it", "description": "Adds tests", "diff": "+a\n-b"})

    def test_unlabeled_is_all_diff(self):
        self.assertEqual(parse_sections("+a\n-b"), {"diff": "+a\n-b"})


class MergeTests(unittest.TestCase):
    def test_different_findings_on_the_same_file_without_a_line_are_both_kept(self):
        findings = [{"severity": "high", "category": "missing-tests", "file": "src/A.cs", "line": None,
                     "description": "Price() has no test"},
                    {"severity": "medium", "category": "missing-tests", "file": "src/A.cs", "line": None,
                     "description": "Greeks() has no test"}]
        self.assertEqual(len(merge_findings(findings)), 2)

    def test_the_same_finding_from_two_parts_is_still_one(self):
        f = {"severity": "low", "category": "flaky", "file": "src/A.cs", "line": None, "description": "uses Now"}
        self.assertEqual(merge_findings([dict(f), dict(f, severity="high")]), [dict(f, severity="high")])


if __name__ == "__main__":
    unittest.main()
