import unittest

from slack_bot import fmt


class SlackTextTests(unittest.TestCase):
    """What the bot posts. Slack sends &, <, and > in message text as &amp;, &lt;, and &gt;, and reads them the
    same way, so text from a PR or a model is escaped before it's posted."""

    def test_pasted_code_gets_its_angle_brackets_and_ampersands_back(self):
        sent = "+    var xs = new List&lt;int&gt;();\n+    if (a &amp;&amp; b) return;\n+    x = \"&amp;lt;\";"
        self.assertEqual(fmt.unescape(sent),
                         "+    var xs = new List<int>();\n+    if (a && b) return;\n+    x = \"&lt;\";")

    def test_pr_title_and_author_cant_ping_the_channel(self):
        review = {"title": "<!channel> urgent", "meta": {"url": "https://github.com/o/r/pull/1", "repo": "o/r",
                                                         "number": 1, "author": "<@U123>"}}
        text = fmt.pr_header(review, 1)
        self.assertNotIn("<!channel>", text)
        self.assertNotIn("<@U123>", text)
        self.assertIn("&lt;!channel&gt; urgent", text)
        self.assertIn("<https://github.com/o/r/pull/1|o/r#1>", text)  # Vader's own link still works

    def test_findings_cant_ping_or_hide_links(self):
        finding = {"severity": "high", "category": "authz", "file": "a.cs", "line": 1,
                   "description": "<!here> see <https://evil.example|docs>", "suggestedFix": "x & y"}
        result = {"validator": "security", "display_name": "Security Validator", "icon": ":shield:",
                  "status": "ok", "findings": [finding], "error": None, "seconds": 1.0, "model": "m:1b",
                  "rejected": [{"severity": "high", "description": "<!channel>", "reason": "<!here>"}]}
        review = {"verdict": "HOLD", "verdict_reason": "1 high finding(s)", "started": 0, "finished": 1,
                  "validators": ["security"], "model": "m:1b", "models": {}, "meta": {}, "results": [result]}
        for text in (fmt.validator_result(result), fmt.verdict(review)):
            self.assertNotIn("<!here>", text)
            self.assertNotIn("<!channel>", text)
            self.assertNotIn("<https://evil", text)
        self.assertIn("x &amp; y", fmt.validator_result(result))

    def test_merge_and_issue_messages_are_escaped(self):
        text = fmt.merge("conflict", "`<!channel>` has merge conflicts with `staging`.")
        self.assertTrue(text.startswith(":x: "))
        self.assertNotIn("<!channel>", text)
        text = fmt.issue_closed({"repo": "o/r", "issue": 15, "title": "<!channel> bug", "pr": 16, "merged": True,
                                 "url": "https://github.com/o/r/issues/15"})
        self.assertIn("<https://github.com/o/r/issues/15|o/r#15>", text)
        self.assertIn("PR #16 was merged", text)
        self.assertNotIn("<!channel>", text)

    def test_findings_dropped_after_the_recheck_are_shown_with_the_reason(self):
        text = fmt.validator_result({"status": "ok", "findings": [], "seconds": 1, "model": "m", "dropped": 0,
                                     "rejected": [{"severity": "high", "description": "wrong weekday",
                                                   "reason": "2028-04-13 is a Thursday"}]})
        self.assertIn("Dropped after checking (high): wrong weekday", text)
        self.assertIn("2028-04-13 is a Thursday", text)


class StatusTests(unittest.TestCase):
    BASE = {"enabled": True, "automation": False, "model": "m", "running": None, "queue": [],
            "github": {"configured": True, "repos": ["o/r"]}}

    def status(self, **changes):
        return fmt.status(dict(self.BASE, **changes))

    def test_model_server(self):
        self.assertIn("can't reach the model server at http://lmstudio/v1",
                      self.status(loaded=None, model_server={"url": "http://lmstudio/v1", "reachable": False}))
        self.assertIn("Loaded: up to the model server", self.status(loaded=None, model_server={"reachable": True}))
        self.assertIn("`m` (4.8 GB)", self.status(loaded=[{"name": "m", "size_gb": 4.8}],
                                                  model_server={"reachable": True}))

    def test_auto_merge_and_issue_closing(self):
        self.assertIn("Auto-merge: PASS → `staging` · Closing linked issues: on",
                      self.status(loaded=[], auto_merge="staging", close_issues=True))
        self.assertIn("Auto-merge: off · Closing linked issues: off", self.status(loaded=[]))


if __name__ == "__main__":
    unittest.main()
