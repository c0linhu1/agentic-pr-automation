import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from vader import config
from vader.engine import Engine, Events, Review
from vader.findings import parse_findings
from vader.state import State

ONE = {"severity": "high", "category": "config", "description": "hardcoded URL"}


class SchemaTests(unittest.TestCase):
    def test_schema_matches_what_the_parser_accepts(self):
        from vader.findings import SCHEMA, SEVERITIES
        item = SCHEMA["properties"]["findings"]["items"]
        self.assertEqual(item["properties"]["severity"]["enum"], SEVERITIES)
        self.assertIn("description", item["required"])


class ParseTests(unittest.TestCase):
    def test_standard_shape(self):
        p = parse_findings(json.dumps({"findings": [ONE]}))
        self.assertEqual((p.failure, len(p.findings)), (None, 1))

    def test_empty_findings_is_a_clean_answer(self):
        self.assertEqual(parse_findings('{"findings": []}').findings, [])
        self.assertIsNone(parse_findings('{"findings": []}').failure)

    def test_anything_but_the_schema_shape_fails(self):
        for raw in ["{}", '{"issues": []}', json.dumps([ONE]), "not json", '"text"']:
            self.assertIsNotNone(parse_findings(raw).failure, raw)


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name) / "state.json", default_model="m:1b")

    def tearDown(self):
        self.tmp.cleanup()

    def run_review(self, replies):
        calls = []

        def complete(*a, **k):
            calls.append(1)
            return replies[min(len(calls), len(replies)) - 1]

        engine = Engine(self.state, Events(), complete=complete, prepare_model=lambda m: None, assignments=dict)
        engine.start()
        review = Review(source="slack", validators=["architecture"], diff="+x")
        engine.submit(review)
        end = time.time() + 5
        while review.status != "done" and time.time() < end:
            time.sleep(0.02)
        return review, len(calls)

    def test_a_bad_reply_is_asked_again(self):
        saved = Path(self.tmp.name) / "failed"
        with mock.patch.object(config, "FAILED_REPLIES_DIR", saved):
            review, calls = self.run_review(["{}", json.dumps({"findings": [ONE]})])
        self.assertEqual(calls, 2)
        self.assertEqual(review.results[0]["status"], "ok")
        files = list(saved.iterdir())
        self.assertEqual(len(files), 1)
        self.assertIn("JSON had no findings list", files[0].read_text(encoding="utf-8"))

    def test_still_fails_if_the_retry_is_bad_too(self):
        review, calls = self.run_review(["{}", "{}"])
        self.assertEqual(calls, 2)
        self.assertEqual(review.results[0]["status"], "failed")
        self.assertEqual(review.verdict, "HOLD")


if __name__ == "__main__":
    unittest.main()
