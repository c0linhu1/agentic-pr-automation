import json
import logging
import threading
import unittest
from unittest import mock

from tests.helpers import EngineCase, FakeClock, FakeModel, wait_for
from vader import config, llm, models
from vader.agents import VALIDATORS
from vader.engine import VERIFY_TASK, Review, Stopped, merge_findings, parse_sections

CRITICAL = json.dumps({"findings": [{"severity": "critical", "description": "bad"}]})


class EngineTests(EngineCase):
    def test_validators_run_on_their_own_models_grouped_to_swap_least(self):
        fake = FakeModel()
        review = self.finish(Review(source="slack", validators=["security", "qa", "correctness", "performance"],
                                    diff="+x"),
                             complete=fake.complete, assignments={"security": "a:1b", "performance": "a:1b", "qa": "b:1b"})
        self.assertEqual(self.loaded, ["a:1b", "b:1b", "fake:1b"])
        self.assertEqual([(r["validator"], r["model"]) for r in review.results],
                         [("security", "a:1b"), ("performance", "a:1b"), ("qa", "b:1b"), ("correctness", "fake:1b")])
        self.assertEqual(fake.models, ["a:1b", "a:1b", "b:1b", "fake:1b"])

    def test_next_review_starts_with_the_model_already_loaded(self):
        engine = self.engine(assignments={"qa": "b:1b"})
        self.finish(Review(source="slack", validators=["security", "qa"], diff="+x"), engine)
        second = self.finish(Review(source="slack", validators=["security", "qa"], diff="+y"), engine)
        # the first review ends on b:1b, so the second starts there instead of swapping back and forth
        self.assertEqual(self.loaded, ["fake:1b", "b:1b", "b:1b", "fake:1b"])
        self.assertEqual([r["validator"] for r in second.results], ["qa", "security"])

    def test_finished_review_releases_its_diff(self):
        review = self.finish(Review(source="slack", validators=["qa"], diff="+x" * 1000))
        self.assertEqual((review.diff, review._parts), ("", []))
        self.assertEqual(review.files, ["(diff)"])  # what was reviewed is still on record

    def test_old_finished_reviews_are_forgotten(self):
        engine = self.engine()
        with mock.patch("vader.engine.KEEP_REVIEWS", 3):
            done = [Review(source="slack", validators=["qa"], status="done") for _ in range(4)]
            for r in done:
                engine._reviews[r.id] = r
            engine.submit(Review(source="slack", validators=["qa"], diff="+x"))  # queued, never dropped
        self.assertEqual(len(engine._reviews), 3)
        self.assertNotIn(done[0].id, engine._reviews)
        self.assertNotIn(done[1].id, engine._reviews)

    def test_estimate_is_updated_after_every_call(self):
        clock = FakeClock()
        with mock.patch("vader.engine.time", clock):
            review = self.finish(Review(source="slack", validators=["security", "qa", "performance"], diff="+x"),
                                 complete=FakeModel(clock=clock, seconds=10).complete)
        self.assertEqual(review._calls_done, 3)
        self.assertEqual(review.estimate_s, review.finished - review.started)  # 30 s, exactly

    def test_a_model_that_fails_to_load_fails_only_its_validators(self):
        def prepare(model):
            if model == "b:1b":
                raise RuntimeError("isn't pulled")
        with self.assertLogs("vader.engine", "WARNING"):
            review = self.finish(Review(source="slack", validators=["security", "qa"], diff="+x"),
                                 prepare=prepare, assignments={"qa": "b:1b"})
        self.assertEqual([r["status"] for r in review.results], ["ok", "failed"])
        self.assertIn("b:1b", review.results[1]["error"])
        self.assertEqual(review.verdict, "HOLD")

    def test_every_validator_finishes_even_after_a_critical(self):
        review = self.finish(Review(source="slack", validators=list(VALIDATORS), diff="+x"),
                             complete=FakeModel({"security": CRITICAL}).complete)
        self.assertEqual([r["validator"] for r in review.results], list(VALIDATORS))
        self.assertEqual(review.verdict, "BLOCK")

    def test_failed_validator_is_recorded_and_others_continue(self):
        fake = FakeModel({"qa": llm.LlmError("timed out"), "security": "not json"})
        with self.assertLogs("vader.engine", "WARNING"):
            review = self.finish(Review(source="slack", validators=["security", "qa", "performance"], diff="+x"),
                                 complete=fake.complete)
        self.assertEqual([r["status"] for r in review.results], ["failed", "failed", "ok"])
        self.assertEqual(review.verdict, "HOLD")

    def test_a_model_that_thinks_until_it_runs_out_is_asked_again_without_thinking(self):
        # A thinking model can spend its whole reply budget thinking; that mustn't hold the PR on its own.
        asked = []

        def complete(model, instructions, content, think=True, **kw):
            asked.append(think)
            if think:
                raise llm.OutputLimit("m hit the output limit")
            return json.dumps({"findings": [{"severity": "medium", "description": "slow"}]})

        with self.assertLogs("vader.engine", "WARNING"):
            review = self.finish(Review(source="slack", validators=["architecture"], diff="+x"), complete=complete)
        result, = review.results
        self.assertEqual((asked, result["status"], review.verdict), ([True, False], "ok", "MINOR"))
        self.assertIn("answered without thinking", result["note"])

    def test_a_minor_finding_most_repeated_asks_didnt_report_is_verified(self):
        def f(severity, description, file="a.py", line=10, category="logic"):
            return {"severity": severity, "category": category, "description": description, "file": file, "line": line}
        replies = iter([json.dumps({"findings": [f("high", "rare high"), f("medium", "seen twice", line=20)]}),
                        ValueError("garbled"),
                        json.dumps({"findings": [f("medium", "seen twice, two lines on", line=22),
                                                 f("medium", "seen once, real", file="b.py", category="edge-case"),
                                                 f("low", "seen once, made up", file="c.py")]})])
        verified = []

        def complete(model, instructions, content, **kw):
            if instructions.startswith(VERIFY_TASK):
                claim = json.loads(instructions[len(VERIFY_TASK):])
                verified.append(claim["description"])
                verdict = "rejected" if "made up" in claim["description"] else "confirmed"
                return json.dumps({"reason": "r", "verdict": verdict})
            reply = next(replies)
            if isinstance(reply, Exception):
                return "not json"
            return reply

        with mock.patch.object(config, "VALIDATOR_ASKS", {"correctness": 3}), \
                mock.patch.object(config, "VERIFY_SEVERITIES", set()), \
                mock.patch.object(config, "PARSE_RETRIES", 0), self.assertLogs("vader.engine", "WARNING"):
            review = self.finish(Review(source="slack", validators=["correctness"], diff="+x"), complete=complete)
        result, = review.results
        self.assertEqual(result["status"], "ok")  # one unusable ask out of three still counts as reviewed
        self.assertEqual(verified, ["seen once, real", "seen once, made up"])  # agreed ones and highs aren't
        self.assertEqual([f["description"] for f in result["findings"]],
                         ["rare high", "seen twice", "seen twice, two lines on", "seen once, real"])
        self.assertFalse({"_ask", "_verify"} & set(result["findings"][3]))
        self.assertEqual([x["description"] for x in result["rejected"]], ["seen once, made up"])

    def test_each_validator_can_only_use_its_own_skills_categories(self):
        schemas = {}

        def complete(model, instructions, content, schema=None, **kw):
            schemas[instructions] = schema["properties"]["findings"]["items"]["properties"]["category"]
            return '{"findings": []}'

        self.finish(Review(source="slack", validators=["qa", "performance"], diff="+x"), complete=complete)
        allowed = {VALIDATORS[k].instructions(): k for k in ("qa", "performance")}
        by_key = {allowed[i]: c.get("enum") for i, c in schemas.items()}
        self.assertIn("weak-assertion", by_key["qa"])
        self.assertIn("n-plus-one", by_key["performance"])
        self.assertNotIn("n-plus-one", by_key["qa"])

    def test_a_one_validator_pr_review_doesnt_count_as_reviewing_the_commit(self):
        engine = self.engine()
        meta = {"pr_key": "o/r#12", "sha": "abc"}
        self.finish(Review(source="slack", validators=["security"], diff="+x", meta=dict(meta)), engine)
        self.assertIsNone(self.state.reviewed_sha("o/r#12"))  # automation still gives it a full review
        self.finish(Review(source="github", validators=list(VALIDATORS), diff="+x", meta=dict(meta)), engine)
        self.assertEqual(self.state.reviewed_sha("o/r#12"), "abc")

    def test_on_done_runs_after_each_review(self):
        engine, seen = self.engine(), []
        engine.on_done = seen.append
        review = self.finish(Review(source="slack", validators=["qa"], diff="+x"), engine)
        self.assertEqual(seen, [review])

    def test_a_crashed_pr_review_is_recorded_so_the_poller_doesnt_requeue_it(self):
        engine = self.engine()
        review = Review(source="github", validators=list(VALIDATORS), diff="+x", meta={"pr_key": "o/r#3", "sha": "s3"})
        with mock.patch.object(engine, "_plan", side_effect=ValueError("broken SKILL.md")), \
             self.assertLogs("vader.engine", "ERROR"):
            self.finish(review, engine)
        self.assertEqual(review.verdict, "HOLD")
        self.assertEqual(self.state.reviewed_sha("o/r#3"), "s3")
        self.assertFalse(engine.is_pending("o/r#3", "s3"))

    def test_a_commit_held_for_its_findings_is_remembered_but_not_one_that_only_failed(self):
        # So a lucky clean rerun of the same commit can't merge it on its own.
        timeouts = {k: llm.LlmError("timed out") for k in VALIDATORS}
        for what, model, held in [("a critical", {"security": CRITICAL}, "BLOCK: bad"),
                                  ("only timeouts", timeouts, None)]:
            with self.subTest(what), mock.patch.object(logging.getLogger("vader.engine"), "disabled", True):
                self.state.set(held={})
                self.finish(Review(source="github", validators=list(VALIDATORS), diff="+x",
                                   meta={"pr_key": "o/r#5", "sha": "s5"}), complete=FakeModel(model).complete)
                self.assertEqual(self.state.held_reason("o/r#5", "s5"), held)
                self.assertIsNone(self.state.held_reason("o/r#5", "s6"))  # a new commit starts fresh


class ServerDownTests(EngineCase):
    """A review that never reached the model server is no evidence about the PR: it isn't recorded as reviewed,
    so it's tried again. Every other failure (config, timeouts, bad replies) is recorded, so nothing loops."""

    def test_only_a_review_that_reached_no_server_is_tried_again(self):
        down = {k: llm.LlmUnavailable("can't reach the model server") for k in VALIDATORS}
        cases = [  # (what happened, engine arguments, tried again?)
            ("down when loading", {"prepare": mock.Mock(side_effect=models.ModelServerDown("can't reach Ollama"))},
             True),
            ("down on every call", {"complete": FakeModel(down).complete}, True),
            ("one bad reply is still a reply", {"complete": FakeModel(dict(down, qa="not json")).complete}, False),
            ("a config error", {"prepare": mock.Mock(side_effect=RuntimeError("`x:1b` isn't pulled"))}, False),
            ("timeouts", {"complete": FakeModel({k: llm.LlmError("timed out") for k in VALIDATORS}).complete}, False),
        ]
        for what, engine_args, retried in cases:
            with self.subTest(what), mock.patch.object(logging.getLogger("vader.engine"), "disabled", True):
                self.state.set(reviewed={})
                review = self.finish(Review(source="github", validators=list(VALIDATORS), diff="+x",
                                            meta={"pr_key": "o/r#7", "sha": "s7"}), **engine_args)
                self.assertEqual(review.unavailable, retried)
                self.assertEqual(review.verdict, "HOLD")
                self.assertEqual(self.state.reviewed_sha("o/r#7"), None if retried else "s7")
                self.assertEqual("couldn't reach the model server" in review.verdict_reason, retried)


class QueueTests(EngineCase):
    """The worker thread: order, one review at a time, stop and start."""

    def test_reviews_run_one_at_a_time_in_order_and_events_follow(self):
        fake = FakeModel(delay=0.002)
        engine = self.engine(fake.complete, start=True)
        reviews = [Review(source="slack", validators=["security", "qa"], diff=f"+{i}") for i in range(3)]
        positions = [engine.submit(r) for r in reviews]
        self.assertEqual(positions[1:], [2, 3])
        self.assertTrue(wait_for(lambda: all(r.status == "done" for r in reviews)))
        self.assertEqual(fake.max_active, 1)
        self.assertEqual([u.split("+")[-1] for _, u in fake.calls], ["0", "0", "1", "1", "2", "2"])
        kinds = [e["type"] for e in self.events.since(0) if e.get("review", {}).get("id") == reviews[2].id]
        self.assertEqual(kinds, ["review_queued", "review_started", "validator_done", "validator_done", "review_done"])

    def test_stop_lets_the_running_review_finish_and_holds_the_rest_until_start(self):
        gate = threading.Event()  # model calls wait here until the test lets them go
        engine = self.engine(FakeModel(gate=gate).complete, start=True)
        running = Review(source="slack", validators=list(VALIDATORS), diff="+x")
        waiting = Review(source="slack", validators=["qa"], diff="+y")
        engine.submit(running)
        engine.submit(waiting)
        self.assertTrue(wait_for(lambda: running.status == "running"))
        self.state.set(enabled=False)
        with self.assertRaises(Stopped):
            engine.submit(Review(source="slack", validators=["qa"], diff="+z"))
        gate.set()
        self.assertTrue(wait_for(lambda: running.status == "done"))
        self.assertEqual(len(running.results), 6)
        self.assertFalse(wait_for(lambda: waiting.status != "queued", timeout=0.05))
        self.state.set(enabled=True)
        engine.wake()
        self.assertTrue(wait_for(lambda: waiting.status == "done"))


def big_diff(files=6, lines=60):
    out = ""
    for i in range(files):
        out += f"diff --git a/src/F{i}.cs b/src/F{i}.cs\n--- a/src/F{i}.cs\n+++ b/src/F{i}.cs\n@@ -1,1 +1,{lines} @@\n"
        out += "".join(f"+    var value{j} = Compute{j}(input);\n" for j in range(lines))
    return out


class LargePrTests(EngineCase):
    """Small context so a modest diff has to be split into parts."""

    def setUp(self):
        super().setUp()
        for p in [mock.patch.object(config, "CONTEXT_TOKENS", 2000), mock.patch.object(config, "OUTPUT_TOKENS", 200),
                  mock.patch.object(config, "REVIEW_TIME_LIMIT_MINUTES", 0)]:
            self.enterContext(p)

    def review(self, fake, diff, validators=("qa",)) -> Review:
        return self.finish(Review(source="github", validators=list(validators), title="Big change", diff=diff),
                           complete=fake.complete)

    def test_large_pr_is_split_and_every_part_reviewed_part_by_part(self):
        fake = FakeModel()
        review = self.review(fake, big_diff(), validators=("security", "qa"))
        self.assertGreater(review.parts, 1)
        self.assertIn(f"part 1 of {review.parts}", fake.calls[0][1])
        self.assertIn("FILES IN THIS PR (6 reviewed)", fake.calls[0][1])
        # part 1 for every validator, then part 2, ... so consecutive calls share a prompt start
        order = [(key, user.split("This is part ")[1].split(" ")[0]) for key, user in fake.calls]
        self.assertEqual(order, [(k, str(i)) for i in range(1, review.parts + 1) for k in ("security", "qa")])
        self.assertEqual([(r["validator"], r["status"], r["parts_done"]) for r in review.results],
                         [("security", "ok", review.parts), ("qa", "ok", review.parts)])
        estimates = [e for e in self.events.since(0) if e["type"] == "review_estimate"]
        self.assertEqual([e["calls"] for e in estimates], [2 * review.parts])  # posted once, after the first call

    def test_duplicate_findings_across_parts_are_merged(self):
        dup = json.dumps({"findings": [
            {"severity": "low", "category": "flaky", "file": "src/F0.cs", "line": 3, "description": "a"},
            {"severity": "high", "category": "flaky", "file": "src/F0.cs", "line": 3, "description": "a, worse"}]})
        review = self.review(FakeModel({"qa": dup}), big_diff())
        self.assertEqual([f["severity"] for f in review.results[0]["findings"]], ["high"])

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

        review = self.review(Flaky(), big_diff())
        self.assertEqual(review.results[0]["status"], "partial")
        self.assertIn("part 2", review.results[0]["error"])
        self.assertEqual(review.verdict, "BLOCK")

    def test_time_limit_skips_remaining_work_and_holds(self):
        clock = FakeClock()
        with mock.patch("vader.engine.time", clock), mock.patch.object(config, "REVIEW_TIME_LIMIT_MINUTES", 1):
            review = self.review(FakeModel(clock=clock, seconds=25), big_diff(),
                                 validators=("security", "qa", "performance"))
        self.assertTrue(review.time_limited)
        self.assertEqual(review.verdict, "HOLD")
        self.assertIn("1-minute time limit", review.verdict_reason)
        self.assertEqual(len(review.results), 3)  # every validator still reports, even if skipped
        self.assertTrue(any(r["status"] != "ok" for r in review.results))

    def test_all_generated_pr_has_nothing_to_review(self):
        diff = ("diff --git a/package-lock.json b/package-lock.json\n--- a/package-lock.json\n"
                "+++ b/package-lock.json\n@@ -1 +1 @@\n+{}\n")
        review = self.review(FakeModel(), diff)
        self.assertEqual(review.results[0]["status"], "failed")
        self.assertIn("nothing to review", review.results[0]["error"])
        self.assertEqual(review.skipped_files,
                         [{"path": "package-lock.json", "reason": "lockfile changed without package.json"}])

    def test_every_part_fits_the_context_with_the_file_list_included(self):
        # Real budgets and many long paths: the file list a split PR's header adds must still fit.
        sent = []

        def complete(model, instructions, content, **kw):
            sent.append(llm.prompt_tokens(instructions, content))
            return '{"findings": []}'

        diff = ""
        for i in range(150):
            p = f"src/Company.App.Web/Features/Billing/Invoicing/Handlers/InvoiceRequestHandler{i:03}.cs"
            diff += (f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1,1 +1,8 @@\n"
                     + "".join(f"+    var v{j} = Compute{j}(input);\n" for j in range(8)))
        with mock.patch.object(config, "CONTEXT_TOKENS", 32768), mock.patch.object(config, "OUTPUT_TOKENS", 12288):
            review = self.finish(Review(source="github", validators=list(VALIDATORS), diff=diff), complete=complete)
        self.assertGreater(review.parts, 1)
        self.assertLessEqual(max(sent), 32768 - 12288)


class SectionTests(unittest.TestCase):
    def test_labeled_and_unlabeled_input(self):
        self.assertEqual(parse_sections("TITLE: Fix it\nDESCRIPTION:\nAdds tests\nDIFF:\n+a\n-b"),
                         {"title": "Fix it", "description": "Adds tests", "diff": "+a\n-b"})
        self.assertEqual(parse_sections("+a\n-b"), {"diff": "+a\n-b"})


class DuplicateFindingTests(unittest.TestCase):
    def test_same_file_line_and_category_is_one_finding_and_the_worst_copy_wins(self):
        f = {"severity": "low", "category": "flaky", "file": "src/A.cs", "line": None, "description": "uses Now"}
        self.assertEqual(merge_findings([dict(f), dict(f, severity="high")]), [dict(f, severity="high")])

    def test_different_findings_on_the_same_file_without_a_line_are_both_kept(self):
        findings = [{"severity": "high", "category": "missing-tests", "file": "src/A.cs", "line": None,
                     "description": "Price() has no test"},
                    {"severity": "medium", "category": "missing-tests", "file": "src/A.cs", "line": None,
                     "description": "Total() has no test"}]
        self.assertEqual(len(merge_findings(findings)), 2)


if __name__ == "__main__":
    unittest.main()
